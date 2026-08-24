"""Periodic repair when Nova notifications are lost or reordered."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping, Protocol

from .models import InstanceRequest, ManagedPort, SessionState
from .service import AdmissionError
from .ports import ServicePortProvider
from .service import FlytLifecycleService


class NovaInstanceInventory(Protocol):
    def statuses(self, instance_uuids: tuple[str, ...]) -> Mapping[str, str]: ...


class InstanceRequestFactory(Protocol):
    def instance_request(self, instance_uuid: str, port: ManagedPort) -> InstanceRequest: ...


class GpuCellPoolProvider(Protocol):
    def ensure(self, *, cell_profile: str): ...


@dataclass
class GpuCellPoolReconciler:
    """Keep configured physical GPU Cells ready outside Nova's build path."""

    provider: GpuCellPoolProvider
    profiles: Iterable[str]

    def run_once(self) -> int:
        profiles = sorted(set(self.profiles))
        for profile in profiles:
            self.provider.ensure(cell_profile=profile)
        return len(profiles)


@dataclass(frozen=True)
class ReconcileResult:
    started: int = 0
    aborted: int = 0
    deleted: int = 0
    unchanged: int = 0


@dataclass
class SessionReconciler:
    lifecycle: FlytLifecycleService
    nova: NovaInstanceInventory

    def run_once(self) -> ReconcileResult:
        records = [
            record for record in self.lifecycle.sessions.values()
            if record.state != SessionState.DELETED
        ]
        statuses = self.nova.statuses(tuple(record.instance_uuid for record in records))
        started = aborted = deleted = unchanged = 0
        for record in records:
            status = statuses.get(record.instance_uuid)
            if status is None or status == "DELETED":
                self.lifecycle.delete_instance(record.instance_uuid)
                deleted += 1
            elif status == "ACTIVE" and record.state in {
                SessionState.RESERVED, SessionState.STARTING,
                SessionState.PENDING_CAPACITY,
            }:
                self.lifecycle.instance_active(record.instance_uuid)
                started += 1
            elif status == "ERROR" and record.state == SessionState.RESERVED:
                self.lifecycle.abort_build(record.instance_uuid, "Nova instance entered ERROR")
                aborted += 1
            else:
                unchanged += 1
        return ReconcileResult(started, aborted, deleted, unchanged)


@dataclass(frozen=True)
class PortReconcileResult:
    deleted: int = 0
    retained: int = 0
    admitted: int = 0


@dataclass
class OrphanPortReconciler:
    """Delete only old managed ports with neither a Nova VM nor a session."""

    lifecycle: FlytLifecycleService
    nova: NovaInstanceInventory
    ports: ServicePortProvider
    grace_seconds: int = 900
    requests: InstanceRequestFactory | None = None

    def run_once(self, now: datetime | None = None) -> PortReconcileResult:
        now = now or datetime.now(timezone.utc)
        managed = self.ports.list_managed()
        statuses = self.nova.statuses(tuple(port.instance_uuid for port in managed))
        deleted = retained = admitted = 0
        for port in managed:
            session = self.lifecycle.sessions.get(port.instance_uuid)
            status = statuses.get(port.instance_uuid)
            if session is None and status in {"BUILD", "ACTIVE"} and self.requests:
                try:
                    self.lifecycle.admit_build(
                        self.requests.instance_request(port.instance_uuid, port)
                    )
                    admitted += 1
                    if status == "ACTIVE":
                        self.lifecycle.instance_active(port.instance_uuid)
                    session = self.lifecycle.sessions.get(port.instance_uuid)
                except AdmissionError:
                    # Scheduling/allocation may not be complete yet. Retain the
                    # port and retry; ineligible requests remain fail-closed.
                    pass
            if port.instance_uuid in statuses or (
                session is not None and session.state != SessionState.DELETED
            ) or not self._expired(port.created_at, now):
                retained += 1
                continue
            self.ports.delete(port.instance_uuid)
            deleted += 1
        return PortReconcileResult(deleted=deleted, retained=retained, admitted=admitted)

    def _expired(self, created_at: str | None, now: datetime) -> bool:
        # Missing creation time is retained fail-safe. Explicit prebuild
        # rollback remains available for a known failed request.
        if not created_at:
            return False
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (now - created).total_seconds() >= self.grace_seconds
