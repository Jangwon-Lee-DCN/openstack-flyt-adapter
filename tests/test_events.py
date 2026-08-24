from __future__ import annotations

import unittest

from flyt_adapter.events import InvalidNotification, NovaEventConsumer
from flyt_adapter.fakes import (
    FakeCapacityProvider,
    FakeCredentialIssuer,
    FakeFlytBackend,
    FakeQuotaProvider,
    InMemoryApprovalRegistry,
)
from flyt_adapter.models import Flavor, Image, ImageApproval, InstanceRequest, SessionState
from flyt_adapter.service import FlytLifecycleService


class NovaEventConsumerTest(unittest.TestCase):
    def setUp(self) -> None:
        approvals = InMemoryApprovalRegistry({
            "image-1": ImageApproval("image-1", "sum", "v1", "cuda-v1")
        })
        self.capacity = FakeCapacityProvider({"gpu-small": 2})
        self.quotas = FakeQuotaProvider({("project-1", "gpu-small"): 2})
        self.backend = FakeFlytBackend()
        self.lifecycle = FlytLifecycleService(
            approvals=approvals,
            quotas=self.quotas,
            capacity=self.capacity,
            backend=self.backend,
            credentials=FakeCredentialIssuer(),
        )
        self.lifecycle.admit_build(InstanceRequest(
            instance_uuid="instance-1",
            project_id="project-1",
            user_id="user-1",
            flavor=Flavor("flavor-1", "flyt.small", {
                "flyt:enabled": "true", "flyt:profile": "gpu-small"
            }),
            image=Image("image-1", "sum", {
                "flyt:injectable": "true",
                "flyt:injection_schema": "v1",
                "flyt:compatibility": "cuda-v1",
            }),
        ))
        self.consumer = NovaEventConsumer(self.lifecycle)

    def test_create_and_delete_notifications_reconcile(self) -> None:
        ready = self.consumer.handle("instance.create.end", {"instance_uuid": "instance-1"})
        self.assertEqual(SessionState.READY, ready.state)
        deleted = self.consumer.handle("instance.delete.end", {"uuid": "instance-1"})
        self.assertEqual(SessionState.DELETED, deleted.state)
        self.assertFalse(self.capacity.reservations)
        self.assertFalse(self.quotas.reservations)
        self.assertFalse(self.backend.sessions)

    def test_unknown_or_malformed_event_fails_closed(self) -> None:
        with self.assertRaises(InvalidNotification):
            self.consumer.handle("instance.resize.end", {"instance_uuid": "instance-1"})
        with self.assertRaises(InvalidNotification):
            self.consumer.handle("instance.create.end", {})

    def test_versioned_create_start_materializes_before_guest_spawn(self) -> None:
        request = InstanceRequest(
            instance_uuid="instance-start",
            project_id="project-1",
            user_id="user-1",
            flavor=Flavor("flavor-1", "flyt.small", {
                "flyt:enabled": "true", "flyt:profile": "gpu-small"
            }),
            image=Image("image-1", "sum", {
                "flyt:injectable": "true",
                "flyt:injection_schema": "v1",
                "flyt:compatibility": "cuda-v1",
            }),
        )
        consumer = NovaEventConsumer(
            self.lifecycle, request_from_notification=lambda payload: request
        )
        record = consumer.handle("instance.create.start", {
            "nova_object.data": {
                "uuid": "instance-start",
                "tenant_id": "project-1",
                "user_id": "user-1",
            }
        })
        self.assertEqual(SessionState.READY, record.state)
        self.assertIsNotNone(record.bootstrap_token)


if __name__ == "__main__":
    unittest.main()
