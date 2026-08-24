from __future__ import annotations

import unittest

from flyt_adapter.messaging import NotificationEndpoint


class Consumer:
    def __init__(self):
        self.events = []

    def handle(self, event_type, payload):
        self.events.append((event_type, payload))


class MessagingTest(unittest.TestCase):
    def test_supported_notification_is_forwarded(self) -> None:
        consumer = Consumer()
        endpoint = NotificationEndpoint(consumer)
        payload = {"nova_object.data": {"uuid": "vm-1"}}
        endpoint.info(None, "nova-compute:host", "instance.create.start", payload, {})
        self.assertEqual([("instance.create.start", payload)], consumer.events)

    def test_unrelated_notification_is_ignored(self) -> None:
        consumer = Consumer()
        endpoint = NotificationEndpoint(consumer)
        endpoint.info(None, "nova-compute:host", "instance.update", {}, {})
        self.assertEqual([], consumer.events)


if __name__ == "__main__":
    unittest.main()
