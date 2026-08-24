"""JSON boundary conversion with explicit field validation."""

from __future__ import annotations

from typing import Any, Mapping

from .models import Flavor, Image, InstanceRequest, SessionRecord


class InvalidPayload(ValueError):
    pass


def instance_request(payload: Mapping[str, Any]) -> InstanceRequest:
    try:
        flavor = payload["flavor"]
        image = payload["image"]
        return InstanceRequest(
            instance_uuid=_string(payload, "instance_uuid"),
            project_id=_string(payload, "project_id"),
            user_id=_string(payload, "user_id"),
            flavor=Flavor(
                id=_string(flavor, "id"),
                name=_string(flavor, "name"),
                extra_specs=_strings(flavor, "extra_specs"),
            ),
            image=Image(
                id=_string(image, "id"),
                checksum=_string(image, "checksum"),
                properties=_strings(image, "properties"),
            ),
        )
    except (KeyError, TypeError) as exc:
        raise InvalidPayload("invalid admission payload") from exc


def session(record: SessionRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "instance_uuid": record.instance_uuid,
        "project_id": record.project_id,
        "profile": record.profile,
        "reservation_id": record.reservation_id,
        "port_id": record.port_id,
        "service_ip": record.service_ip,
        "generation": record.generation,
        "state": record.state.value,
        "backend_session_id": record.backend_session_id,
        "failure_reason": record.failure_reason,
        "history": [state.value for state in record.history],
    }


def _string(value: Mapping[str, Any], key: str) -> str:
    item = value[key]
    if not isinstance(item, str) or not item:
        raise InvalidPayload(f"{key} must be a non-empty string")
    return item


def _strings(value: Mapping[str, Any], key: str) -> dict[str, str]:
    item = value[key]
    if not isinstance(item, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in item.items()
    ):
        raise InvalidPayload(f"{key} must be a string map")
    return dict(item)
