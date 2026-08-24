"""Kubernetes GPU Cell lifecycle without an additional controller."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
import json
import ssl
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .models import GpuCell, GpuCellState


class KubernetesTransport(Protocol):
    def request(self, method: str, path: str,
                body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]: ...


@dataclass
class UrllibKubernetesTransport:
    endpoint: str
    token_file: str
    ca_file: str
    timeout: float = 5.0

    def request(self, method: str, path: str,
                body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        token = Path(self.token_file).read_text().strip()
        if not token:
            raise RuntimeError("Kubernetes service-account token is empty")
        payload = None if body is None else json.dumps(body).encode()
        request = Request(
            self.endpoint.rstrip("/") + path, data=payload, method=method,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                     "Content-Type": "application/json"},
        )
        context = ssl.create_default_context(cafile=self.ca_file)
        try:
            response = urlopen(request, timeout=self.timeout, context=context)
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
        except HTTPError as error:
            raw = error.read()
            return error.code, (json.loads(raw) if raw else {})


@dataclass(frozen=True)
class GpuCellProfile:
    device_class: str = "gpu.nvidia.com"
    allocation_mode: str = "ExactCount"
    count: int = 1
    selectors: tuple[str, ...] = ()
    expected_product_name: str | None = None

    def __post_init__(self) -> None:
        if self.allocation_mode != "ExactCount" or self.count != 1:
            raise ValueError("a GPU Cell must exclusively claim exactly one GPU device")


@dataclass
class KubernetesGpuCellProvider:
    namespace: str
    image: str
    cluster_manager_address: str
    profiles: dict[str, GpuCellProfile]
    transport: KubernetesTransport
    runtime_class_name: str | None = None

    def ensure(self, *, cell_profile: str) -> GpuCell:
        cell_name = _resource_name("flyt-cell", cell_profile)
        claim_name = _resource_name("flyt-gpu", cell_profile)
        existing = self.get(cell_profile)
        if existing:
            return existing
        try:
            profile = self.profiles[cell_profile]
        except KeyError as exc:
            raise RuntimeError(f"unknown GPU Cell profile {cell_profile}") from exc
        status, _ = self.transport.request(
            "POST", self._claims_path(), self._claim(claim_name, cell_profile, profile)
        )
        if status not in {201, 409}:
            raise RuntimeError(f"ResourceClaim creation failed with HTTP {status}")
        status, _ = self.transport.request(
            "POST", self._pods_path(), self._pod(
                cell_name, claim_name, cell_profile, profile
            )
        )
        if status not in {201, 409}:
            self.transport.request("DELETE", f"{self._claims_path()}/{quote(claim_name)}")
            raise RuntimeError(f"GPU Cell Pod creation failed with HTTP {status}")
        return self.get(cell_profile) or GpuCell(
            cell_profile, cell_name, claim_name, cell_profile, GpuCellState.POD_PENDING
        )

    def get(self, cell_profile: str) -> GpuCell | None:
        name = _resource_name("flyt-cell", cell_profile)
        status, pod = self.transport.request("GET", f"{self._pods_path()}/{quote(name)}")
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(f"GPU Cell lookup failed with HTTP {status}")
        labels = pod.get("metadata", {}).get("labels", {})
        if labels.get("flyt.runtime/cell-id") != cell_profile:
            raise RuntimeError("GPU Cell identity mismatch")
        profile = labels.get("flyt.runtime/profile")
        if not isinstance(profile, str) or not profile:
            raise RuntimeError("GPU Cell profile label is missing")
        phase = pod.get("status", {}).get("phase")
        ready = any(
            value.get("type") == "Ready" and value.get("status") == "True"
            for value in pod.get("status", {}).get("conditions", [])
        )
        if phase == "Failed":
            state = GpuCellState.FAILED
        elif ready:
            state = GpuCellState.READY
        else:
            state = GpuCellState.POD_PENDING
        return GpuCell(cell_profile, name, _resource_name("flyt-gpu", cell_profile), profile, state)

    def delete(self, cell_profile: str) -> None:
        for path in (
            f"{self._pods_path()}/{quote(_resource_name('flyt-cell', cell_profile))}",
            f"{self._claims_path()}/{quote(_resource_name('flyt-gpu', cell_profile))}",
        ):
            status, _ = self.transport.request("DELETE", path)
            if status not in {200, 202, 404}:
                raise RuntimeError(f"GPU Cell cleanup failed with HTTP {status}")

    def _claim(self, name: str, cell_profile: str, profile: GpuCellProfile) -> dict[str, Any]:
        exactly: dict[str, Any] = {
            "allocationMode": profile.allocation_mode,
            "count": profile.count,
            "deviceClassName": profile.device_class,
        }
        if profile.selectors:
            exactly["selectors"] = [
                {"cel": {"expression": expression}} for expression in profile.selectors
            ]
        return {
            "apiVersion": "resource.k8s.io/v1", "kind": "ResourceClaim",
            "metadata": {"name": name, "namespace": self.namespace,
                         "labels": {"flyt.runtime/cell-id": cell_profile,
                                    "flyt.runtime/profile": cell_profile}},
            "spec": {"devices": {"requests": [{"name": "gpu", "exactly": exactly}]}},
        }

    def _pod(self, name: str, claim: str, cell_profile: str,
             profile: GpuCellProfile) -> dict[str, Any]:
        environment = [{"name": "FLYT_CLUSTER_MANAGER_ADDRESS",
                        "value": self.cluster_manager_address}]
        if profile.expected_product_name:
            environment.append({"name": "FLYT_EXPECTED_GPU_PRODUCT_NAME",
                                "value": profile.expected_product_name})
        spec: dict[str, Any] = {
            "restartPolicy": "Always", "automountServiceAccountToken": False,
            "resourceClaims": [{"name": "gpu", "resourceClaimName": claim}],
            "containers": [{
                "name": "gpu-cell", "image": self.image,
                "env": environment,
                "resources": {"claims": [{"name": "gpu"}]},
                "securityContext": {"allowPrivilegeEscalation": False},
                "readinessProbe": {"exec": {"command": [
                    "/bin/bash", "-lc", "pgrep -f '^/opt/flyt/bin/flyt-node-manager$' >/dev/null"
                ]}, "periodSeconds": 5},
                "volumeMounts": [
                    {"name": "runtime-config", "mountPath": "/etc/flyt"},
                    {"name": "mps-run", "mountPath": "/run/flyt-mps"},
                    {"name": "mps-log", "mountPath": "/var/log/flyt-mps"},
                    {"name": "shm", "mountPath": "/dev/shm"},
                ],
            }],
            "volumes": [
                {"name": "runtime-config", "emptyDir": {}},
                {"name": "mps-run", "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"}},
                {"name": "mps-log", "emptyDir": {"sizeLimit": "1Gi"}},
                {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "2Gi"}},
            ],
        }
        if self.runtime_class_name:
            spec["runtimeClassName"] = self.runtime_class_name
        return {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": self.namespace, "labels": {
                "app.kubernetes.io/name": "flyt-gpu-cell",
                "flyt.runtime/cell-id": cell_profile,
                "flyt.runtime/profile": cell_profile,
            }}, "spec": spec,
        }

    def _pods_path(self) -> str:
        return f"/api/v1/namespaces/{quote(self.namespace)}/pods"

    def _claims_path(self) -> str:
        return f"/apis/resource.k8s.io/v1/namespaces/{quote(self.namespace)}/resourceclaims"


@dataclass
class FakeKubernetesTransport:
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: list[tuple[str, str]] = field(default_factory=list)
    fail_pod_create: bool = False

    def request(self, method: str, path: str,
                body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        self.requests.append((method, path))
        if method == "GET":
            return (200, self.objects[path]) if path in self.objects else (404, {})
        if method == "POST":
            if self.fail_pod_create and path.endswith("/pods"):
                return 500, {}
            assert body is not None
            object_path = f"{path}/{body['metadata']['name']}"
            if object_path in self.objects:
                return 409, self.objects[object_path]
            self.objects[object_path] = body
            return 201, body
        if method == "DELETE":
            return (200, self.objects.pop(path)) if path in self.objects else (404, {})
        raise AssertionError(method)


def _resource_name(prefix: str, identity: str) -> str:
    safe = "".join(value if value.isalnum() else "-" for value in identity.lower())
    safe = "-".join(filter(None, safe.split("-")))
    if not safe:
        raise ValueError("identity cannot produce an empty Kubernetes name")
    return f"{prefix}-{safe}"[:63].rstrip("-")
