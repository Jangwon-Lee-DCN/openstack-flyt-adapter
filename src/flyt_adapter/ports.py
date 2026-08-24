"""External system boundaries used by the lifecycle service."""

from __future__ import annotations

from typing import Protocol

from .models import BackendSession, GpuCell, ImageApproval, ManagedPort, SessionRecord


class ApprovalRegistry(Protocol):
    def find(self, image_id: str) -> ImageApproval | None: ...


class CapacityProvider(Protocol):
    def available(self, *, profile: str) -> bool: ...

    def reserve(
        self, *, profile: str, consumer_uuid: str, project_id: str, user_id: str
    ) -> str: ...

    def release(self, reservation_id: str) -> None: ...


class QuotaProvider(Protocol):
    def available(self, *, project_id: str, profile: str) -> bool: ...

    def reserve(self, *, project_id: str, profile: str, consumer_uuid: str) -> None: ...

    def release(self, consumer_uuid: str) -> None: ...


class FlytBackend(Protocol):
    def create_session(self, record: SessionRecord) -> BackendSession: ...

    def delete_session(self, backend_session_id: str) -> None: ...


class GpuCellProvider(Protocol):
    def ensure(self, *, cell_profile: str) -> GpuCell: ...

    def delete(self, cell_profile: str) -> None: ...

    def get(self, cell_profile: str) -> GpuCell | None: ...


class CredentialIssuer(Protocol):
    def issue(self, *, instance_uuid: str, project_id: str) -> str: ...

    def revoke(self, token: str) -> None: ...


class ServicePortProvider(Protocol):
    def ensure(
        self, *, instance_uuid: str, project_id: str, availability_zone: str
    ) -> ManagedPort: ...

    def delete(self, instance_uuid: str) -> None: ...

    def get(self, instance_uuid: str) -> ManagedPort | None: ...

    def list_managed(self) -> tuple[ManagedPort, ...]: ...
