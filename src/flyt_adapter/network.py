"""Managed Neutron service-port lifecycle for FLYT VMs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import quote

from .models import ManagedPort
from .openstack import JsonTransport, OpenStackError, OpenStackHttpError


@dataclass
class FakeServicePortProvider:
    networks: Mapping[str, str]
    ports: dict[str, ManagedPort] = field(default_factory=dict)

    def ensure(self, *, instance_uuid: str, project_id: str,
               availability_zone: str) -> ManagedPort:
        existing = self.ports.get(instance_uuid)
        if existing:
            if existing.project_id != project_id or existing.availability_zone != availability_zone:
                raise RuntimeError("managed port identity mismatch")
            return existing
        network_id = self._network(availability_zone)
        suffix = len(self.ports) + 10
        value = ManagedPort(
            instance_uuid=instance_uuid,
            project_id=project_id,
            availability_zone=availability_zone,
            network_id=network_id,
            port_id=f"flyt-port-{instance_uuid}",
            mac_address=f"02:00:00:00:00:{suffix:02x}",
            service_ip=f"172.30.0.{suffix}",
        )
        self.ports[instance_uuid] = value
        return value

    def delete(self, instance_uuid: str) -> None:
        self.ports.pop(instance_uuid, None)

    def get(self, instance_uuid: str) -> ManagedPort | None:
        return self.ports.get(instance_uuid)

    def list_managed(self) -> tuple[ManagedPort, ...]:
        return tuple(self.ports.values())

    def _network(self, availability_zone: str) -> str:
        try:
            return self.networks[availability_zone]
        except KeyError as exc:
            raise RuntimeError(f"no FLYT service network for AZ {availability_zone}") from exc


@dataclass
class NeutronServicePortProvider:
    endpoint: str
    token: str
    transport: JsonTransport
    networks: Mapping[str, str]
    security_group_ids: tuple[str, ...] = ()

    def ensure(self, *, instance_uuid: str, project_id: str,
               availability_zone: str) -> ManagedPort:
        existing = self.get(instance_uuid)
        if existing:
            if existing.project_id != project_id or existing.availability_zone != availability_zone:
                raise OpenStackError("managed Neutron port identity mismatch")
            return existing
        network_id = self._network(availability_zone)
        _, response = self.transport.request(
            "POST", f"{self.endpoint.rstrip('/')}/v2.0/ports",
            headers={"X-Auth-Token": self.token},
            payload={"port": {
                "network_id": network_id,
                "project_id": project_id,
                "name": f"flyt-{instance_uuid}",
                "admin_state_up": True,
                "device_id": instance_uuid,
                "device_owner": "compute:flyt",
                "security_groups": list(self.security_group_ids),
                "tags": ["managed-by=flyt-adapter", f"instance-uuid={instance_uuid}"],
            }},
        )
        return self._decode(response.get("port"), availability_zone)

    def delete(self, instance_uuid: str) -> None:
        value = self.get(instance_uuid)
        if not value:
            return
        try:
            self.transport.request(
                "DELETE",
                f"{self.endpoint.rstrip('/')}/v2.0/ports/{quote(value.port_id, safe='')}",
                headers={"X-Auth-Token": self.token},
            )
        except OpenStackHttpError as exc:
            if exc.status != 404:
                raise

    def get(self, instance_uuid: str) -> ManagedPort | None:
        _, response = self.transport.request(
            "GET", f"{self.endpoint.rstrip('/')}/v2.0/ports"
            f"?device_id={quote(instance_uuid, safe='')}&device_owner=compute%3Aflyt",
            headers={"X-Auth-Token": self.token},
        )
        ports = response.get("ports")
        if not isinstance(ports, list):
            raise OpenStackError("Neutron ports response is malformed")
        if not ports:
            return None
        if len(ports) != 1:
            raise OpenStackError("multiple managed FLYT ports exist for one VM")
        network_id = ports[0].get("network_id") if isinstance(ports[0], Mapping) else None
        az = next((key for key, value in self.networks.items() if value == network_id), None)
        if not az:
            raise OpenStackError("managed port is attached to an unknown FLYT network")
        return self._decode(ports[0], az)

    def list_managed(self) -> tuple[ManagedPort, ...]:
        _, response = self.transport.request(
            "GET", f"{self.endpoint.rstrip('/')}/v2.0/ports"
            "?device_owner=compute%3Aflyt&tags=managed-by%3Dflyt-adapter",
            headers={"X-Auth-Token": self.token},
        )
        ports = response.get("ports")
        if not isinstance(ports, list):
            raise OpenStackError("Neutron ports response is malformed")
        values = []
        for port in ports:
            network_id = port.get("network_id") if isinstance(port, Mapping) else None
            az = next((key for key, value in self.networks.items() if value == network_id), None)
            if not az:
                raise OpenStackError("managed port is attached to an unknown FLYT network")
            values.append(self._decode(port, az))
        return tuple(values)

    def _network(self, availability_zone: str) -> str:
        try:
            return self.networks[availability_zone]
        except KeyError as exc:
            raise OpenStackError(f"no FLYT service network for AZ {availability_zone}") from exc

    @staticmethod
    def _decode(value: object, availability_zone: str) -> ManagedPort:
        if not isinstance(value, Mapping):
            raise OpenStackError("Neutron port response is malformed")
        fixed_ips = value.get("fixed_ips", [])
        if not isinstance(fixed_ips, list):
            raise OpenStackError("Neutron fixed_ips is malformed")
        addresses = [item.get("ip_address") for item in fixed_ips if isinstance(item, Mapping)]
        service_ip = next((item for item in addresses if isinstance(item, str) and item), None)
        try:
            return ManagedPort(
                instance_uuid=str(value["device_id"]), project_id=str(value["project_id"]),
                availability_zone=availability_zone, network_id=str(value["network_id"]),
                port_id=str(value["id"]), mac_address=str(value["mac_address"]),
                service_ip=service_ip,
                created_at=(str(value["created_at"]) if value.get("created_at") else None),
            )
        except KeyError as exc:
            raise OpenStackError("Neutron port response lacks required fields") from exc
