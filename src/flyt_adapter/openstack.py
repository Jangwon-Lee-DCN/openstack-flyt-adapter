"""Dependency-light OpenStack REST adapters."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import quote

from .fakes import NoCapacityError
from .models import Flavor, Image, InstanceRequest, ManagedPort


class OpenStackError(RuntimeError):
    pass


class OpenStackHttpError(OpenStackError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class JsonTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> tuple[int, Mapping[str, Any]]: ...


@dataclass(frozen=True)
class UrllibJsonTransport:
    timeout: float = 10.0
    ca_file: str | None = None

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> tuple[int, Mapping[str, Any]]:
        request_headers = {"Accept": "application/json", **dict(headers or {})}
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url, data=body, headers=request_headers, method=method
        )
        context = ssl.create_default_context(cafile=self.ca_file)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=context) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise OpenStackError("OpenStack response exceeds 4 MiB")
                value = json.loads(raw) if raw else {}
                if not isinstance(value, Mapping):
                    raise OpenStackError("OpenStack response is not a JSON object")
                return response.status, value
        except urllib.error.HTTPError as exc:
            detail = exc.read(64 * 1024).decode(errors="replace")
            raise OpenStackHttpError(
                exc.code, f"{method} {url} failed with {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise OpenStackError(f"{method} {url} failed: {exc}") from exc


@dataclass(frozen=True)
class OpenStackCatalog:
    nova_endpoint: str
    glance_endpoint: str
    token: str
    transport: JsonTransport

    def flavor(self, flavor_id: str) -> Flavor:
        _, value = self.transport.request(
            "GET",
            f"{self.nova_endpoint.rstrip('/')}/flavors/{quote(flavor_id, safe='')}",
            headers=self._headers(),
        )
        flavor = value.get("flavor")
        if not isinstance(flavor, Mapping):
            raise OpenStackError("Nova flavor response is malformed")
        extra_specs = flavor.get("extra_specs", {})
        if not isinstance(extra_specs, Mapping):
            raise OpenStackError("Nova flavor extra_specs are malformed")
        return Flavor(
            id=_required_string(flavor, "id"),
            name=_required_string(flavor, "name"),
            extra_specs=_string_map(extra_specs),
        )

    def image(self, image_id: str) -> Image:
        _, value = self.transport.request(
            "GET",
            f"{self.glance_endpoint.rstrip('/')}/v2/images/{quote(image_id, safe='')}",
            headers=self._headers(),
        )
        checksum = value.get("os_hash_value") or value.get("checksum")
        if not isinstance(checksum, str) or not checksum:
            raise OpenStackError("Glance image has no checksum")
        properties = {
            key: item for key, item in value.items()
            if key.startswith("flyt:") and isinstance(item, str)
        }
        return Image(
            id=_required_string(value, "id"),
            checksum=checksum,
            properties=properties,
        )

    def instance_request(self, instance_uuid: str, port: ManagedPort) -> InstanceRequest:
        _, value = self.transport.request(
            "GET",
            f"{self.nova_endpoint.rstrip('/')}/servers/{quote(instance_uuid, safe='')}",
            headers=self._headers(),
        )
        server = value.get("server")
        if not isinstance(server, Mapping):
            raise OpenStackError("Nova server response is malformed")
        flavor = server.get("flavor")
        image = server.get("image")
        if not isinstance(flavor, Mapping) or not isinstance(image, Mapping):
            raise OpenStackError("Nova server lacks flavor or image identity")
        return InstanceRequest(
            instance_uuid=instance_uuid,
            project_id=_required_string(server, "tenant_id"),
            user_id=_required_string(server, "user_id"),
            flavor=self.flavor(_required_string(flavor, "id")),
            image=self.image(_required_string(image, "id")),
            service_port=port,
        )

    def _headers(self) -> dict[str, str]:
        return {"X-Auth-Token": self.token}


@dataclass(frozen=True)
class NovaInstanceStatusInventory:
    nova_endpoint: str
    token: str
    transport: JsonTransport

    def statuses(self, instance_uuids: tuple[str, ...]) -> dict[str, str]:
        values: dict[str, str] = {}
        for instance_uuid in instance_uuids:
            try:
                _, response = self.transport.request(
                    "GET",
                    f"{self.nova_endpoint.rstrip('/')}/servers/"
                    f"{quote(instance_uuid, safe='')}",
                    headers={"X-Auth-Token": self.token},
                )
            except OpenStackHttpError as exc:
                if exc.status == 404:
                    continue
                raise
            server = response.get("server")
            if not isinstance(server, Mapping):
                raise OpenStackError("Nova server response is malformed")
            status = server.get("status")
            if not isinstance(status, str) or not status:
                raise OpenStackError("Nova server has no status")
            values[instance_uuid] = status.upper()
        return values


@dataclass
class PlacementCapacityProvider:
    endpoint: str
    token: str
    transport: JsonTransport
    provider_namespace: uuid.UUID = uuid.UUID("7d7a9258-d463-4b92-bbc8-26dafd65cb4c")

    def available(self, *, profile: str) -> bool:
        resource_class = _resource_class(profile)
        provider_id = provider_uuid(profile)
        try:
            _, candidates = self.transport.request(
                "GET",
                f"{self.endpoint.rstrip('/')}/allocation_candidates?resources="
                f"{quote(resource_class + ':1', safe='')}",
                headers=self._headers("1.39"),
            )
        except OpenStackError:
            return False
        requests = candidates.get("allocation_requests")
        return isinstance(requests, list) and any(
            provider_id in _allocation_providers(item) for item in requests
        )

    def reserve(
        self, *, profile: str, consumer_uuid: str, project_id: str, user_id: str
    ) -> str:
        resource_class = _resource_class(profile)
        provider_uuid = str(uuid.uuid5(self.provider_namespace, profile))
        headers = self._headers("1.39")
        try:
            _, value = self.transport.request(
                "GET",
                f"{self.endpoint.rstrip('/')}/allocations/{quote(consumer_uuid, safe='')}",
                headers=headers,
            )
        except OpenStackError as exc:
            raise NoCapacityError(str(exc)) from exc
        allocations = value.get("allocations")
        if not isinstance(allocations, Mapping):
            raise OpenStackError("Placement consumer allocation is malformed")
        provider_allocation = allocations.get(provider_uuid)
        if not isinstance(provider_allocation, Mapping):
            raise NoCapacityError(f"consumer has no FLYT allocation for profile {profile}")
        resources = provider_allocation.get("resources")
        if not isinstance(resources, Mapping) or resources.get(resource_class) != 1:
            raise NoCapacityError(f"consumer has no FLYT allocation for profile {profile}")
        return consumer_uuid

    def release(self, reservation_id: str) -> None:
        # Nova owns and releases the Placement allocation with the VM.
        return None

    def _headers(self, microversion: str) -> dict[str, str]:
        return {
            "X-Auth-Token": self.token,
            "OpenStack-API-Version": f"placement {microversion}",
        }


@dataclass
class PlacementInventoryManager:
    endpoint: str
    token: str
    transport: JsonTransport

    def ensure_profile(
        self,
        *,
        profile: str,
        total: int,
        aggregate_uuid: str,
        compute_provider_uuids: tuple[str, ...] = (),
    ) -> str:
        if total < 0:
            raise ValueError("Placement inventory total cannot be negative")
        uuid.UUID(aggregate_uuid)
        resource_class = _resource_class(profile)
        provider_id = provider_uuid(profile)
        headers = self._headers("1.39")
        self.transport.request(
            "PUT",
            f"{self.endpoint.rstrip('/')}/resource_classes/{resource_class}",
            headers=headers,
        )
        for trait in ("CUSTOM_FLYT_REMOTE_GPU", "MISC_SHARES_VIA_AGGREGATE"):
            self.transport.request(
                "PUT",
                f"{self.endpoint.rstrip('/')}/traits/{trait}",
                headers=headers,
            )
        _, providers = self.transport.request(
            "GET",
            f"{self.endpoint.rstrip('/')}/resource_providers?uuid={provider_id}",
            headers=headers,
        )
        values = providers.get("resource_providers")
        if not isinstance(values, list):
            raise OpenStackError("Placement resource provider response is malformed")
        if not values:
            self.transport.request(
                "POST",
                f"{self.endpoint.rstrip('/')}/resource_providers",
                headers=headers,
                payload={"uuid": provider_id, "name": f"flyt-pool-{profile}"},
            )
        generation = self._generation(provider_id)
        self.transport.request(
            "PUT",
            f"{self.endpoint.rstrip('/')}/resource_providers/{provider_id}/inventories",
            headers=headers,
            payload={
                "resource_provider_generation": generation,
                "inventories": {
                    resource_class: {
                        "total": total,
                        "reserved": 0,
                        "min_unit": 1,
                        "max_unit": 1,
                        "step_size": 1,
                        "allocation_ratio": 1.0,
                    }
                },
            },
        )
        generation = self._generation(provider_id)
        self.transport.request(
            "PUT",
            f"{self.endpoint.rstrip('/')}/resource_providers/{provider_id}/traits",
            headers=headers,
            payload={
                "resource_provider_generation": generation,
                "traits": ["CUSTOM_FLYT_REMOTE_GPU", "MISC_SHARES_VIA_AGGREGATE"],
            },
        )
        self._ensure_aggregate(provider_id, aggregate_uuid)
        for compute_provider_uuid in compute_provider_uuids:
            self._ensure_aggregate(compute_provider_uuid, aggregate_uuid)
        return provider_id

    def _ensure_aggregate(self, provider_id: str, aggregate_uuid: str) -> None:
        headers = self._headers("1.39")
        _, value = self.transport.request(
            "GET",
            f"{self.endpoint.rstrip('/')}/resource_providers/{provider_id}/aggregates",
            headers=headers,
        )
        aggregates = value.get("aggregates", [])
        generation = value.get("resource_provider_generation")
        if not isinstance(aggregates, list) or not isinstance(generation, int):
            raise OpenStackError("Placement aggregate response is malformed")
        desired = sorted({*aggregates, aggregate_uuid})
        if desired != sorted(aggregates):
            self.transport.request(
                "PUT",
                f"{self.endpoint.rstrip('/')}/resource_providers/{provider_id}/aggregates",
                headers=headers,
                payload={
                    "aggregates": desired,
                    "resource_provider_generation": generation,
                },
            )

    def _generation(self, provider_id: str) -> int:
        _, value = self.transport.request(
            "GET",
            f"{self.endpoint.rstrip('/')}/resource_providers/{provider_id}",
            headers=self._headers("1.39"),
        )
        generation = value.get("generation")
        if not isinstance(generation, int):
            raise OpenStackError("Placement provider has no generation")
        return generation

    def _headers(self, microversion: str) -> dict[str, str]:
        return {
            "X-Auth-Token": self.token,
            "OpenStack-API-Version": f"placement {microversion}",
        }


def provider_uuid(profile: str) -> str:
    return str(uuid.uuid5(PlacementCapacityProvider.provider_namespace, profile))


def _resource_class(profile: str) -> str:
    normalized = "".join(character if character.isalnum() else "_" for character in profile)
    return f"CUSTOM_FLYT_{normalized.upper()}"


def _allocation_providers(value: Any) -> set[str]:
    if not isinstance(value, Mapping):
        return set()
    allocations = value.get("allocations")
    return set(allocations) if isinstance(allocations, Mapping) else set()


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise OpenStackError(f"OpenStack response has no valid {key}")
    return item


def _string_map(value: Mapping[Any, Any]) -> dict[str, str]:
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise OpenStackError("OpenStack property map contains non-string data")
    return dict(value)
