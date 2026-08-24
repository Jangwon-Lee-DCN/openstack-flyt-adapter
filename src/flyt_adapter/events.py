"""Normalize the Nova lifecycle notifications consumed by the adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from .models import InstanceRequest, SessionRecord
from .service import FlytLifecycleService


class InvalidNotification(ValueError):
    pass


@dataclass
class NovaEventConsumer:
    lifecycle: FlytLifecycleService
    request_from_notification: Callable[[Mapping[str, object]], InstanceRequest] | None = None

    def handle(self, event_type: str, payload: Mapping[str, object]) -> SessionRecord | None:
        payload = _object_data(payload)
        instance_uuid = payload.get("instance_uuid") or payload.get("uuid")
        if not isinstance(instance_uuid, str) or not instance_uuid:
            raise InvalidNotification("Nova notification has no instance UUID")
        if event_type == "instance.create.start":
            if self.request_from_notification is None:
                raise InvalidNotification("create.start admission is not configured")
            request = self.request_from_notification(payload)
            if request.instance_uuid != instance_uuid:
                raise InvalidNotification("notification request UUID mismatch")
            self.lifecycle.admit_build(request)
            # nova-compute emits this after scheduling/Placement claim and
            # before config-drive generation and guest spawn.
            return self.lifecycle.instance_active(instance_uuid)
        if event_type == "instance.create.end":
            return self.lifecycle.instance_active(instance_uuid)
        if event_type == "instance.delete.end":
            return self.lifecycle.delete_instance(instance_uuid)
        if event_type == "instance.create.error":
            reason = payload.get("exception")
            return self.lifecycle.abort_build(instance_uuid, str(reason or "Nova build failed"))
        raise InvalidNotification(f"unsupported Nova event type {event_type}")


def _object_data(payload: Mapping[str, object]) -> Mapping[str, object]:
    value = payload.get("nova_object.data")
    if isinstance(value, Mapping):
        return value
    return payload
