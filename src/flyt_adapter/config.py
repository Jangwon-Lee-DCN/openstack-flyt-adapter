"""Validated service configuration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .injection import ClientPackage
from .models import ImageApproval


@dataclass(frozen=True)
class OpenStackConfig:
    nova_endpoint: str
    glance_endpoint: str
    placement_endpoint: str
    neutron_endpoint: str
    token: str
    ca_file: str | None = None


@dataclass(frozen=True)
class ServiceNetworkConfig:
    networks: dict[str, str]
    security_group_ids: tuple[str, ...]


@dataclass(frozen=True)
class FlytManagerConfig:
    socket_path: str
    profiles: dict[str, tuple[int, int]]


@dataclass(frozen=True)
class PlacementSyncConfig:
    aggregate_uuid: str
    compute_provider_uuids: tuple[str, ...]


@dataclass(frozen=True)
class KubernetesGpuCellsConfig:
    endpoint: str
    namespace: str
    token_file: str
    ca_file: str
    image: str
    cluster_manager_address: str
    runtime_class_name: str | None
    session_profiles: dict[str, str]
    profiles: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class ServiceConfig:
    api_token: str
    database: str
    listen_host: str
    listen_port: int
    approvals: tuple[ImageApproval, ...]
    inventory: dict[str, int]
    quotas: dict[tuple[str, str], int]
    client_package: ClientPackage
    openstack: OpenStackConfig | None
    service_network: ServiceNetworkConfig
    notification_transport_url: str | None
    notification_topics: tuple[str, ...]
    flyt_manager: FlytManagerConfig | None
    reconcile_interval_seconds: int
    orphan_grace_seconds: int
    client_manager_endpoint: str
    placement_sync: PlacementSyncConfig | None
    kubernetes_gpu_cells: KubernetesGpuCellsConfig | None

    @classmethod
    def load(cls, path: str | Path) -> "ServiceConfig":
        raw = json.loads(Path(path).read_text())
        token = os.environ.get("FLYT_API_TOKEN") or raw.get("api_token")
        if not isinstance(token, str) or len(token) < 16:
            raise ValueError("api_token must contain at least 16 characters")
        inventory = _positive_int_map(raw.get("inventory", {}), allow_zero=True)
        quotas: dict[tuple[str, str], int] = {}
        for item in raw.get("quotas", []):
            project_id = _nonempty(item, "project_id")
            profile = _nonempty(item, "profile")
            limit = item.get("limit")
            if not isinstance(limit, int) or limit < 0:
                raise ValueError("quota limit must be a non-negative integer")
            quotas[(project_id, profile)] = limit
        approvals = tuple(
            ImageApproval(
                image_id=_nonempty(item, "image_id"),
                checksum=_nonempty(item, "checksum"),
                injection_schema=_nonempty(item, "injection_schema"),
                compatibility=_nonempty(item, "compatibility"),
            )
            for item in raw.get("approvals", [])
        )
        package = raw.get("client_package", {})
        port = raw.get("listen_port", 8080)
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("listen_port must be between 1 and 65535")
        openstack = None
        openstack_raw = raw.get("openstack")
        if openstack_raw is not None:
            if not isinstance(openstack_raw, dict):
                raise ValueError("openstack must be an object")
            openstack_token = os.environ.get("OS_TOKEN") or openstack_raw.get("token")
            if not isinstance(openstack_token, str) or not openstack_token:
                raise ValueError("OS_TOKEN is required for OpenStack mode")
            ca_file = openstack_raw.get("ca_file")
            if ca_file is not None and not isinstance(ca_file, str):
                raise ValueError("openstack.ca_file must be a string")
            openstack = OpenStackConfig(
                nova_endpoint=_nonempty(openstack_raw, "nova_endpoint"),
                glance_endpoint=_nonempty(openstack_raw, "glance_endpoint"),
                placement_endpoint=_nonempty(openstack_raw, "placement_endpoint"),
                neutron_endpoint=_nonempty(openstack_raw, "neutron_endpoint"),
                token=openstack_token,
                ca_file=ca_file,
            )
        network_raw = raw.get("service_network", {})
        if not isinstance(network_raw, dict):
            raise ValueError("service_network must be an object")
        networks = network_raw.get("networks", {"default": "fake-flyt-network"})
        if not isinstance(networks, dict) or not networks or not all(
            isinstance(key, str) and key and isinstance(value, str) and value
            for key, value in networks.items()
        ):
            raise ValueError("service_network.networks must be a non-empty string map")
        security_groups = network_raw.get("security_group_ids", [])
        if not isinstance(security_groups, list) or not all(
            isinstance(value, str) and value for value in security_groups
        ):
            raise ValueError("service_network.security_group_ids must be a string list")
        manager = None
        manager_raw = raw.get("flyt_manager")
        if manager_raw is not None:
            if not isinstance(manager_raw, dict):
                raise ValueError("flyt_manager must be an object")
            profiles: dict[str, tuple[int, int]] = {}
            for name, value in manager_raw.get("profiles", {}).items():
                if not isinstance(name, str) or not isinstance(value, dict):
                    raise ValueError("flyt_manager profile is malformed")
                compute_units = value.get("compute_units")
                memory_mb = value.get("memory_mb")
                if not isinstance(compute_units, int) or compute_units <= 0 or not isinstance(memory_mb, int) or memory_mb <= 0:
                    raise ValueError("FLYT profile resources must be positive integers")
                profiles[name] = (compute_units, memory_mb)
            manager = FlytManagerConfig(
                socket_path=_nonempty(manager_raw, "socket_path"), profiles=profiles
            )
        placement_sync = None
        placement_raw = raw.get("placement_sync")
        if placement_raw is not None:
            if not isinstance(placement_raw, dict):
                raise ValueError("placement_sync must be an object")
            providers = placement_raw.get("compute_provider_uuids", [])
            if not isinstance(providers, list) or not all(
                isinstance(value, str) and value for value in providers
            ):
                raise ValueError("compute_provider_uuids must be a string list")
            placement_sync = PlacementSyncConfig(
                aggregate_uuid=_nonempty(placement_raw, "aggregate_uuid"),
                compute_provider_uuids=tuple(providers),
            )
        gpu_cells = None
        gpu_cells_raw = raw.get("kubernetes_gpu_cells")
        if gpu_cells_raw is not None:
            if not isinstance(gpu_cells_raw, dict):
                raise ValueError("kubernetes_gpu_cells must be an object")
            image = _nonempty(gpu_cells_raw, "image")
            if "@sha256:" not in image:
                raise ValueError("GPU Cell image must use an immutable digest")
            profiles_raw = gpu_cells_raw.get("profiles")
            if not isinstance(profiles_raw, dict) or not profiles_raw:
                raise ValueError("kubernetes_gpu_cells.profiles must be non-empty")
            profiles: dict[str, dict[str, Any]] = {}
            for name, value in profiles_raw.items():
                if not isinstance(name, str) or not name or not isinstance(value, dict):
                    raise ValueError("GPU Cell profile is malformed")
                device_class = _nonempty(value, "device_class")
                selectors = value.get("selectors", [])
                if not isinstance(selectors, list) or not all(
                    isinstance(item, str) and item for item in selectors
                ):
                    raise ValueError("GPU Cell selectors must be strings")
                product_name = value.get("expected_product_name")
                if product_name is not None and (
                    not isinstance(product_name, str) or not product_name
                ):
                    raise ValueError("expected_product_name must be a non-empty string")
                if product_name and not selectors:
                    raise ValueError(
                        "a device-specific GPU Cell requires a DRA product selector"
                    )
                profiles[name] = {"device_class": device_class,
                                  "selectors": tuple(selectors),
                                  "expected_product_name": product_name}
            session_profiles = gpu_cells_raw.get("session_profiles")
            if not isinstance(session_profiles, dict) or not session_profiles or not all(
                isinstance(name, str) and name and isinstance(target, str) and target
                for name, target in session_profiles.items()
            ):
                raise ValueError("kubernetes_gpu_cells.session_profiles must be a non-empty string map")
            missing = set(session_profiles.values()) - set(profiles)
            if missing:
                raise ValueError(f"GPU Cell profile mapping targets are undefined: {sorted(missing)}")
            runtime_class = gpu_cells_raw.get("runtime_class_name")
            if runtime_class is not None and not isinstance(runtime_class, str):
                raise ValueError("runtime_class_name must be a string")
            gpu_cells = KubernetesGpuCellsConfig(
                endpoint=_nonempty(gpu_cells_raw, "endpoint"),
                namespace=_nonempty(gpu_cells_raw, "namespace"),
                token_file=str(gpu_cells_raw.get("token_file", "/var/run/secrets/kubernetes.io/serviceaccount/token")),
                ca_file=str(gpu_cells_raw.get("ca_file", "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")),
                image=image,
                cluster_manager_address=_nonempty(gpu_cells_raw, "cluster_manager_address"),
                runtime_class_name=runtime_class,
                session_profiles=dict(session_profiles),
                profiles=profiles,
            )
        if gpu_cells is not None:
            if manager is None:
                raise ValueError("kubernetes_gpu_cells requires flyt_manager")
            offering_ids = set(gpu_cells.session_profiles)
            if set(manager.profiles) != offering_ids:
                raise ValueError(
                    "flyt_manager profiles must exactly match GPU Cell session offerings"
                )
            if set(inventory) != offering_ids:
                raise ValueError(
                    "inventory profiles must exactly match GPU Cell session offerings"
                )
            unknown_quotas = {profile for _, profile in quotas} - offering_ids
            if unknown_quotas:
                raise ValueError(
                    f"quota profiles are not defined offerings: {sorted(unknown_quotas)}"
                )
        return cls(
            api_token=token,
            database=str(raw.get("database", "/var/lib/flyt-adapter/sessions.db")),
            listen_host=str(raw.get("listen_host", "0.0.0.0")),
            listen_port=port,
            approvals=approvals,
            inventory=inventory,
            quotas=quotas,
            client_package=ClientPackage(
                url=_nonempty(package, "url"),
                digest=_nonempty(package, "digest"),
            ),
            openstack=openstack,
            service_network=ServiceNetworkConfig(
                networks=dict(networks), security_group_ids=tuple(security_groups)
            ),
            notification_transport_url=(
                os.environ.get("FLYT_NOTIFICATION_TRANSPORT_URL") or
                (str(raw["notification_transport_url"])
                 if raw.get("notification_transport_url") else None)
            ),
            notification_topics=tuple(raw.get(
                "notification_topics", ["versioned_notifications"]
            )),
            flyt_manager=manager,
            reconcile_interval_seconds=_positive_integer(
                raw.get("reconcile_interval_seconds", 60),
                "reconcile_interval_seconds",
            ),
            orphan_grace_seconds=_positive_integer(
                raw.get("orphan_grace_seconds", 900), "orphan_grace_seconds"
            ),
            client_manager_endpoint=_nonempty(raw, "client_manager_endpoint"),
            placement_sync=placement_sync,
            kubernetes_gpu_cells=gpu_cells,
        )


def _nonempty(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{key} must be a non-empty string")
    return item


def _positive_int_map(value: Any, *, allow_zero: bool) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError("inventory must be an object")
    minimum = 0 if allow_zero else 1
    if not all(
        isinstance(key, str) and key and isinstance(item, int) and item >= minimum
        for key, item in value.items()
    ):
        raise ValueError("inventory values must be non-negative integers")
    return dict(value)


def _positive_integer(value: Any, name: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value
