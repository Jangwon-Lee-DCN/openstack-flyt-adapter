"""Immutable request objects and lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping


class SessionState(StrEnum):
    RESERVED = "RESERVED"
    STARTING = "STARTING"
    PENDING_CAPACITY = "PENDING_CAPACITY"
    READY = "READY"
    FAILED = "FAILED"
    DRAINING = "DRAINING"
    DELETED = "DELETED"


class GpuCellState(StrEnum):
    CLAIM_PENDING = "CLAIM_PENDING"
    POD_PENDING = "POD_PENDING"
    READY = "READY"
    FAILED = "FAILED"
    DELETED = "DELETED"


@dataclass(frozen=True)
class GpuCell:
    cell_id: str
    name: str
    claim_name: str
    profile: str
    state: GpuCellState


@dataclass(frozen=True)
class Flavor:
    id: str
    name: str
    extra_specs: Mapping[str, str]

    @property
    def flyt_enabled(self) -> bool:
        return self.extra_specs.get("flyt:enabled", "").lower() == "true"

    @property
    def flyt_profile(self) -> str | None:
        value = self.extra_specs.get("flyt:profile", "").strip()
        return value or None


@dataclass(frozen=True)
class Image:
    id: str
    checksum: str
    properties: Mapping[str, str]


@dataclass(frozen=True)
class InstanceRequest:
    instance_uuid: str
    project_id: str
    user_id: str
    flavor: Flavor
    image: Image
    service_port: ManagedPort | None = None


@dataclass(frozen=True)
class ImageApproval:
    image_id: str
    checksum: str
    injection_schema: str
    compatibility: str


@dataclass(frozen=True)
class ManagedPort:
    instance_uuid: str
    project_id: str
    availability_zone: str
    network_id: str
    port_id: str
    mac_address: str
    service_ip: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class BackendSession:
    session_id: str
    state: SessionState


@dataclass
class SessionRecord:
    instance_uuid: str
    project_id: str
    profile: str
    reservation_id: str
    port_id: str | None = None
    service_ip: str | None = None
    generation: int = 1
    state: SessionState = SessionState.RESERVED
    backend_session_id: str | None = None
    bootstrap_token: str | None = None
    failure_reason: str | None = None
    history: list[SessionState] = field(default_factory=lambda: [SessionState.RESERVED])

    def transition(self, state: SessionState) -> None:
        if self.state != state:
            self.state = state
            self.history.append(state)
