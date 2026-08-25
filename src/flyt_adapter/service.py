"""Fail-closed FLYT VM admission and lifecycle reconciliation."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
from threading import RLock
from typing import MutableMapping

from .fakes import NoCapacityError, QuotaExceededError
from .models import InstanceRequest, SessionRecord, SessionState
from .ports import (
    ApprovalRegistry,
    CapacityProvider,
    CredentialIssuer,
    FlytBackend,
    QuotaProvider,
)


class AdmissionError(RuntimeError):
    """The VM request is not eligible for a FLYT allocation."""


def _synchronized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


@dataclass
class FlytLifecycleService:
    approvals: ApprovalRegistry
    quotas: QuotaProvider
    capacity: CapacityProvider
    backend: FlytBackend
    credentials: CredentialIssuer
    sessions: MutableMapping[str, SessionRecord] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    @_synchronized
    def preflight(self, request: InstanceRequest) -> str:
        """Validate image, Flavor, quota, and candidate capacity without claiming it."""

        profile = self._validate_request(request)
        if not self.quotas.available(project_id=request.project_id, profile=profile):
            raise AdmissionError(
                f"project {request.project_id} exceeds quota for profile {profile}"
            )
        if not self.capacity.available(profile=profile):
            raise AdmissionError(f"no capacity for profile {profile}")
        return profile

    @_synchronized
    def prepare_build(self, request: InstanceRequest) -> SessionRecord:
        """Reserve quota before scheduling without requiring a Placement allocation."""

        existing = self.sessions.get(request.instance_uuid)
        if existing and existing.state != SessionState.DELETED:
            return existing
        profile = self.preflight(request)
        try:
            self.quotas.reserve(
                project_id=request.project_id,
                profile=profile,
                consumer_uuid=request.instance_uuid,
            )
        except QuotaExceededError as exc:
            raise AdmissionError(str(exc)) from exc
        record = SessionRecord(
            instance_uuid=request.instance_uuid,
            project_id=request.project_id,
            profile=profile,
            reservation_id=f"pending-placement:{request.instance_uuid}",
            port_id=request.service_port.port_id if request.service_port else None,
            service_ip=request.service_port.service_ip if request.service_port else None,
            generation=(existing.generation + 1 if existing else 1),
        )
        self.sessions[request.instance_uuid] = record
        return record

    @_synchronized
    def admit_build(self, request: InstanceRequest) -> SessionRecord:
        """Validate and reserve before Nova starts building the VM."""

        existing = self.sessions.get(request.instance_uuid)
        if existing and existing.state != SessionState.DELETED:
            if existing.reservation_id.startswith("pending-placement:"):
                try:
                    existing.reservation_id = self.capacity.reserve(
                        profile=existing.profile,
                        consumer_uuid=existing.instance_uuid,
                        project_id=existing.project_id,
                        user_id=request.user_id,
                    )
                except NoCapacityError as exc:
                    raise AdmissionError(str(exc)) from exc
                self.sessions[existing.instance_uuid] = existing
            return existing
        profile = self._validate_request(request)
        try:
            self.quotas.reserve(
                project_id=request.project_id,
                profile=profile,
                consumer_uuid=request.instance_uuid,
            )
            reservation_id = self.capacity.reserve(
                profile=profile,
                consumer_uuid=request.instance_uuid,
                project_id=request.project_id,
                user_id=request.user_id,
            )
        except QuotaExceededError as exc:
            raise AdmissionError(str(exc)) from exc
        except NoCapacityError as exc:
            self.quotas.release(request.instance_uuid)
            raise AdmissionError(str(exc)) from exc
        record = SessionRecord(
            instance_uuid=request.instance_uuid,
            project_id=request.project_id,
            profile=profile,
            reservation_id=reservation_id,
            port_id=request.service_port.port_id if request.service_port else None,
            service_ip=request.service_port.service_ip if request.service_port else None,
            generation=(existing.generation + 1 if existing else 1),
        )
        self.sessions[request.instance_uuid] = record
        return record

    @_synchronized
    def finalize_scheduled_build(self, instance_uuid: str) -> SessionRecord:
        """Verify Nova's Placement claim before config-drive materialization."""

        record = self._record(instance_uuid)
        if not record.reservation_id.startswith("pending-placement:"):
            return record
        try:
            record.reservation_id = self.capacity.reserve(
                profile=record.profile,
                consumer_uuid=record.instance_uuid,
                project_id=record.project_id,
                user_id="nova-scheduled-consumer",
            )
        except NoCapacityError as exc:
            raise AdmissionError(str(exc)) from exc
        self.sessions[instance_uuid] = record
        return record

    @_synchronized
    def instance_active(self, instance_uuid: str) -> SessionRecord:
        """Materialize a reserved FLYT session after Nova reports ACTIVE."""

        record = self._record(instance_uuid)
        if record.state == SessionState.READY:
            return record
        if record.state == SessionState.PENDING_CAPACITY:
            refresh = getattr(self.backend, "refresh_session", None)
            if refresh is None:
                return record
            backend_session = refresh(record)
            record.transition(backend_session.state)
            self.sessions[instance_uuid] = record
            return record
        if record.state not in {SessionState.RESERVED, SessionState.STARTING}:
            raise RuntimeError(f"cannot start session in state {record.state}")
        record.transition(SessionState.STARTING)
        record.failure_reason = None
        self.sessions[instance_uuid] = record
        try:
            record.bootstrap_token = self.credentials.issue(
                instance_uuid=record.instance_uuid,
                project_id=record.project_id,
            )
            backend_session = self.backend.create_session(record)
            record.backend_session_id = backend_session.session_id
        except Exception as exc:
            if record.bootstrap_token:
                self.credentials.revoke(record.bootstrap_token)
                record.bootstrap_token = None
            record.failure_reason = str(exc)
            # Keep STARTING so reconciliation can retry a transient manager
            # failure. Build-abort failures use FAILED and are not retried.
            self.sessions[instance_uuid] = record
            raise
        record.transition(backend_session.state)
        self.sessions[instance_uuid] = record
        return record

    @_synchronized
    def delete_instance(self, instance_uuid: str) -> SessionRecord | None:
        """Idempotently remove credentials, backend session, and reservation."""

        record = self.sessions.get(instance_uuid)
        if record is None or record.state == SessionState.DELETED:
            return record
        record.transition(SessionState.DRAINING)
        if record.bootstrap_token:
            self.credentials.revoke(record.bootstrap_token)
            record.bootstrap_token = None
        if record.backend_session_id:
            self.backend.delete_session(record.backend_session_id)
            record.backend_session_id = None
        self.capacity.release(record.reservation_id)
        self.quotas.release(instance_uuid)
        record.transition(SessionState.DELETED)
        self.sessions[instance_uuid] = record
        return record

    @_synchronized
    def bootstrap_token(self, instance_uuid: str) -> str:
        """Return the token while it remains valid for guest bootstrap.

        Nova can evaluate DynamicJSON more than once (metadata service retries,
        config-drive generation, or cache expiry).  Fetching vendor data must
        therefore be idempotent.  The credential issuer, rather than the
        metadata read, owns the one-time exchange and revocation semantics.
        """

        record = self._record(instance_uuid)
        if record.state not in {SessionState.READY, SessionState.PENDING_CAPACITY}:
            raise RuntimeError(f"session is not ready: {record.state}")
        if not record.bootstrap_token:
            raise RuntimeError("bootstrap token is unavailable")
        return record.bootstrap_token

    @_synchronized
    def abort_build(self, instance_uuid: str, reason: str) -> SessionRecord | None:
        """Release a reservation when Nova fails before reaching ACTIVE."""

        record = self.sessions.get(instance_uuid)
        if record is None or record.state == SessionState.DELETED:
            return record
        record.failure_reason = reason
        record.transition(SessionState.FAILED)
        self.capacity.release(record.reservation_id)
        self.quotas.release(instance_uuid)
        self.sessions[instance_uuid] = record
        return record

    def _validate_request(self, request: InstanceRequest) -> str:
        flavor = request.flavor
        if not flavor.flyt_enabled:
            raise AdmissionError("Flavor is not FLYT-enabled")
        profile = flavor.flyt_profile
        if not profile:
            raise AdmissionError("FLYT Flavor has no flyt:profile")
        image = request.image
        if image.properties.get("flyt:injectable", "").lower() != "true":
            raise AdmissionError("image is not marked FLYT-injectable")
        approval = self.approvals.find(image.id)
        if approval is None:
            raise AdmissionError("image is absent from the approval registry")
        if approval.checksum != image.checksum:
            raise AdmissionError("image checksum does not match approval")
        if image.properties.get("flyt:injection_schema") != approval.injection_schema:
            raise AdmissionError("image injection schema does not match approval")
        if image.properties.get("flyt:compatibility") != approval.compatibility:
            raise AdmissionError("image compatibility does not match approval")
        return profile

    def _record(self, instance_uuid: str) -> SessionRecord:
        try:
            return self.sessions[instance_uuid]
        except KeyError as exc:
            raise RuntimeError(f"instance {instance_uuid} has no reservation") from exc
