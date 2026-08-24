"""oslo.messaging boundary for Nova lifecycle notifications."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .events import InvalidNotification, NovaEventConsumer


SUPPORTED = {
    "instance.create.start",
    "instance.create.end",
    "instance.create.error",
    "instance.delete.end",
}


@dataclass
class NotificationEndpoint:
    consumer: NovaEventConsumer

    def info(self, ctxt: Any, publisher_id: str, event_type: str,
             payload: Mapping[str, object], metadata: Mapping[str, object]) -> None:
        self._handle(event_type, payload)

    def error(self, ctxt: Any, publisher_id: str, event_type: str,
              payload: Mapping[str, object], metadata: Mapping[str, object]) -> None:
        self._handle(event_type, payload)

    def warn(self, ctxt: Any, publisher_id: str, event_type: str,
             payload: Mapping[str, object], metadata: Mapping[str, object]) -> None:
        self._handle(event_type, payload)

    def _handle(self, event_type: str, payload: Mapping[str, object]) -> None:
        if event_type not in SUPPORTED:
            return
        try:
            self.consumer.handle(event_type, payload)
        except InvalidNotification:
            raise


def run_notification_listener(transport_url: str, topics: tuple[str, ...],
                              consumer: NovaEventConsumer) -> None:
    try:
        from oslo_config import cfg
        import oslo_messaging
    except ImportError as exc:
        raise RuntimeError(
            "notification mode requires the 'notifications' package extra"
        ) from exc
    conf = cfg.ConfigOpts()
    transport = oslo_messaging.get_notification_transport(
        conf, url=transport_url
    )
    targets = [oslo_messaging.Target(topic=topic) for topic in topics]
    listener = oslo_messaging.get_notification_listener(
        transport, targets, [NotificationEndpoint(consumer)],
        executor="threading", allow_requeue=False,
    )
    listener.start()
    listener.wait()
