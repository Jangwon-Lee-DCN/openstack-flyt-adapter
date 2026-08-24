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
    subnets: Mapping[str, str] = field(default_factory=dict)
    security_group_ids: tuple[str, ...] = ()

    def ensure(self, *, instance_uuid: str, project_id: str,
               availability_zone: str) -> ManagedPort:
        existing = self.get(instance_uuid)
        if existing:
            if existing.project_id != project_id or existing.availability_zone != availability_zone:
                raise OpenStackError("managed Neutron port identity mismatch")
            return existing
        network_id = self._network(availability_zone)
        subnet_id = self.subnets.get(availability_zone)
        _, response = self.transport.request(
            "POST", f"{self.endpoint.rstrip('/')}/v2.0/ports",
            headers={"X-Auth-Token": self.token},
            payload={"port": {
                "network_id": network_id,
                "project_id": project_id,
                "name": f"flyt-{instance_uuid}",
                "admin_state_up": True,
                # Nova only accepts an explicitly requested port while it is
                # unbound. Nova atomically assigns device_id/device_owner when
                # it claims the port for the pre-generated instance UUID.
                "device_id": "",
                "device_owner": "",
                "security_groups": list(self.security_group_ids),
                **({"fixed_ips": [{"subnet_id": subnet_id}]} if subnet_id else {}),
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
            f"?name={quote('flyt-' + instance_uuid, safe='')}",
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
        ports = []
        # Some Neutron deployments accept but discard tags during port create.
        # The UUID-derived name and dedicated network are portable API fields.
        for network_id in set(self.networks.values()):
            _, response = self.transport.request(
                "GET", f"{self.endpoint.rstrip('/')}/v2.0/ports"
                f"?network_id={quote(network_id, safe='')}",
                headers={"X-Auth-Token": self.token},
            )
            items = response.get("ports")
            if not isinstance(items, list):
                raise OpenStackError("Neutron ports response is malformed")
            ports.extend(port for port in items if isinstance(port, Mapping)
                         and str(port.get("name", "")).startswith("flyt-"))
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
            tags = value.get("tags", [])
            instance_tag = next(
                (tag.removeprefix("instance-uuid=") for tag in tags
                 if isinstance(tag, str) and tag.startswith("instance-uuid=")),
                None,
            ) if isinstance(tags, list) else None
            name = value.get("name")
            name_identity = name.removeprefix("flyt-") if (
                isinstance(name, str) and name.startswith("flyt-")
            ) else None
            instance_uuid = instance_tag or name_identity or value.get("device_id")
            if not isinstance(instance_uuid, str) or not instance_uuid:
                raise OpenStackError("managed Neutron port lacks instance identity")
            return ManagedPort(
                instance_uuid=instance_uuid, project_id=str(value["project_id"]),
                availability_zone=availability_zone, network_id=str(value["network_id"]),
                port_id=str(value["id"]), mac_address=str(value["mac_address"]),
                service_ip=service_ip,
                created_at=(str(value["created_at"]) if value.get("created_at") else None),
            )
        except KeyError as exc:
            raise OpenStackError("Neutron port response lacks required fields") from exc
