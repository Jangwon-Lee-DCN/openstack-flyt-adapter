"""Deterministic fake external systems for GPU-free control-plane development."""

from __future__ import annotations

from dataclasses import dataclass, field
import secrets

from .models import BackendSession, ImageApproval, SessionRecord, SessionState


class NoCapacityError(RuntimeError):
    pass


class QuotaExceededError(RuntimeError):
    pass


@dataclass
class InMemoryApprovalRegistry:
    approvals: dict[str, ImageApproval] = field(default_factory=dict)

    def find(self, image_id: str) -> ImageApproval | None:
        return self.approvals.get(image_id)


@dataclass
class FakeCapacityProvider:
    inventory: dict[str, int] = field(default_factory=dict)
    reservations: dict[str, tuple[str, str, str, str]] = field(default_factory=dict)

    def available(self, *, profile: str) -> bool:
        used = sum(1 for value in self.reservations.values() if value[0] == profile)
        return used < self.inventory.get(profile, 0)

    def reserve(
        self, *, profile: str, consumer_uuid: str, project_id: str, user_id: str
    ) -> str:
        existing = next(
            (reservation_id for reservation_id, value in self.reservations.items()
             if value[1] == consumer_uuid),
            None,
        )
        if existing:
            return existing
        used = sum(1 for value in self.reservations.values() if value[0] == profile)
        if used >= self.inventory.get(profile, 0):
            raise NoCapacityError(f"no capacity for profile {profile}")
        reservation_id = f"reservation-{consumer_uuid}"
        self.reservations[reservation_id] = (profile, consumer_uuid, project_id, user_id)
        return reservation_id

    def release(self, reservation_id: str) -> None:
        self.reservations.pop(reservation_id, None)


@dataclass
class FakeQuotaProvider:
    limits: dict[tuple[str, str], int] = field(default_factory=dict)
    reservations: dict[str, tuple[str, str]] = field(default_factory=dict)

    def available(self, *, project_id: str, profile: str) -> bool:
        key = (project_id, profile)
        used = sum(1 for value in self.reservations.values() if value == key)
        return used < self.limits.get(key, 0)

    def reserve(self, *, project_id: str, profile: str, consumer_uuid: str) -> None:
        if consumer_uuid in self.reservations:
            return
        key = (project_id, profile)
        used = sum(1 for value in self.reservations.values() if value == key)
        if used >= self.limits.get(key, 0):
            raise QuotaExceededError(
                f"project {project_id} exceeds quota for profile {profile}"
            )
        self.reservations[consumer_uuid] = key

    def release(self, consumer_uuid: str) -> None:
        self.reservations.pop(consumer_uuid, None)


@dataclass
class SessionQuotaProvider:
    """Derive quota use from the shared session store across service processes."""

    limits: dict[tuple[str, str], int]
    sessions: object

    def available(self, *, project_id: str, profile: str) -> bool:
        used = sum(
            1 for record in self.sessions.values()
            if record.project_id == project_id and record.profile == profile and
            record.state not in {SessionState.FAILED, SessionState.DELETED}
        )
        return used < self.limits.get((project_id, profile), 0)

    def reserve(self, *, project_id: str, profile: str, consumer_uuid: str) -> None:
        existing = self.sessions.get(consumer_uuid)
        if existing is not None and existing.state not in {
            SessionState.FAILED, SessionState.DELETED
        }:
            return
        if not self.available(project_id=project_id, profile=profile):
            raise QuotaExceededError(
                f"project {project_id} exceeds quota for profile {profile}"
            )

    def release(self, consumer_uuid: str) -> None:
        # The authoritative release is the FAILED/DELETED state persisted by
        # FlytLifecycleService immediately after this callback.
        return None


@dataclass
class FakeFlytBackend:
    sessions: dict[str, str] = field(default_factory=dict)

    def create_session(self, record: SessionRecord) -> BackendSession:
        session_id = f"flyt-{record.instance_uuid}"
        self.sessions[session_id] = record.profile
        return BackendSession(session_id, SessionState.READY)

    def delete_session(self, backend_session_id: str) -> None:
        self.sessions.pop(backend_session_id, None)


@dataclass
class FakeCredentialIssuer:
    active: set[str] = field(default_factory=set)

    def issue(self, *, instance_uuid: str, project_id: str) -> str:
        token = secrets.token_urlsafe(32)
        self.active.add(token)
        return token

    def revoke(self, token: str) -> None:
        self.active.discard(token)
