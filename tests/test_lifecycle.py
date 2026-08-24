from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor

from flyt_adapter.fakes import (
    FakeCapacityProvider,
    FakeCredentialIssuer,
    FakeFlytBackend,
    FakeQuotaProvider,
    InMemoryApprovalRegistry,
)
from flyt_adapter.models import Flavor, Image, ImageApproval, InstanceRequest, SessionState
from flyt_adapter.service import AdmissionError, FlytLifecycleService


class FlytLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.approval = ImageApproval(
            image_id="image-1",
            checksum="abc123",
            injection_schema="v1",
            compatibility="cuda-12.8-v1",
        )
        self.approvals = InMemoryApprovalRegistry({"image-1": self.approval})
        self.capacity = FakeCapacityProvider({"gpu-small": 1})
        self.quotas = FakeQuotaProvider({("project-1", "gpu-small"): 1})
        self.backend = FakeFlytBackend()
        self.credentials = FakeCredentialIssuer()
        self.service = FlytLifecycleService(
            approvals=self.approvals,
            quotas=self.quotas,
            capacity=self.capacity,
            backend=self.backend,
            credentials=self.credentials,
        )

    def request(self, *, image: Image | None = None) -> InstanceRequest:
        return InstanceRequest(
            instance_uuid="instance-1",
            project_id="project-1",
            user_id="user-1",
            flavor=Flavor(
                id="flavor-1",
                name="flyt.small",
                extra_specs={"flyt:enabled": "true", "flyt:profile": "gpu-small"},
            ),
            image=image or Image(
                id="image-1",
                checksum="abc123",
                properties={
                    "flyt:injectable": "true",
                    "flyt:injection_schema": "v1",
                    "flyt:compatibility": "cuda-12.8-v1",
                },
            ),
        )

    def test_complete_create_and_delete_is_idempotent(self) -> None:
        reserved = self.service.admit_build(self.request())
        self.assertEqual(SessionState.RESERVED, reserved.state)
        self.assertEqual(1, len(self.capacity.reservations))
        self.assertEqual(1, len(self.quotas.reservations))
        self.assertIs(reserved, self.service.admit_build(self.request()))

        ready = self.service.instance_active("instance-1")
        self.assertEqual(SessionState.READY, ready.state)
        self.assertEqual(1, len(self.backend.sessions))
        self.assertEqual(1, len(self.credentials.active))
        self.assertIs(ready, self.service.instance_active("instance-1"))

        deleted = self.service.delete_instance("instance-1")
        self.assertEqual(SessionState.DELETED, deleted.state)
        self.assertFalse(self.capacity.reservations)
        self.assertFalse(self.quotas.reservations)
        self.assertFalse(self.backend.sessions)
        self.assertFalse(self.credentials.active)
        self.assertIs(deleted, self.service.delete_instance("instance-1"))

        recreated = self.service.admit_build(self.request())
        self.assertEqual(2, recreated.generation)

    def test_zero_inventory_rejects_before_session_creation(self) -> None:
        self.capacity.inventory["gpu-small"] = 0
        with self.assertRaisesRegex(AdmissionError, "no capacity"):
            self.service.admit_build(self.request())
        self.assertFalse(self.service.sessions)
        self.assertFalse(self.quotas.reservations)
        self.assertFalse(self.backend.sessions)

    def test_quota_rejects_before_capacity_reservation(self) -> None:
        self.quotas.limits[("project-1", "gpu-small")] = 0
        with self.assertRaisesRegex(AdmissionError, "exceeds quota"):
            self.service.admit_build(self.request())
        self.assertFalse(self.quotas.reservations)
        self.assertFalse(self.capacity.reservations)

    def test_property_without_registry_approval_is_rejected(self) -> None:
        self.approvals.approvals.clear()
        with self.assertRaisesRegex(AdmissionError, "approval registry"):
            self.service.admit_build(self.request())

    def test_changed_image_checksum_is_rejected(self) -> None:
        image = Image(
            id="image-1",
            checksum="changed",
            properties={
                "flyt:injectable": "true",
                "flyt:injection_schema": "v1",
                "flyt:compatibility": "cuda-12.8-v1",
            },
        )
        with self.assertRaisesRegex(AdmissionError, "checksum"):
            self.service.admit_build(self.request(image=image))

    def test_abort_build_releases_reservation(self) -> None:
        self.service.admit_build(self.request())
        record = self.service.abort_build("instance-1", "Nova build failed")
        self.assertEqual(SessionState.FAILED, record.state)
        self.assertFalse(self.capacity.reservations)
        self.assertFalse(self.quotas.reservations)

    def test_concurrent_admission_never_exceeds_capacity(self) -> None:
        requests = [self.request(), InstanceRequest(
            instance_uuid="instance-2",
            project_id="project-1",
            user_id="user-2",
            flavor=self.request().flavor,
            image=self.request().image,
        )]

        def admit(request):
            try:
                return self.service.admit_build(request).instance_uuid
            except AdmissionError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(admit, requests))
        self.assertEqual(1, sum(result is not None for result in results))
        self.assertEqual(1, len(self.capacity.reservations))
        self.assertEqual(1, len(self.quotas.reservations))


if __name__ == "__main__":
    unittest.main()
