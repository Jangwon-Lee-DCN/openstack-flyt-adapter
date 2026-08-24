from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timezone

from flyt_adapter.fakes import (
    FakeCapacityProvider,
    FakeCredentialIssuer,
    FakeFlytBackend,
    FakeQuotaProvider,
    InMemoryApprovalRegistry,
)
from flyt_adapter.models import (
    BackendSession, Flavor, Image, ImageApproval, InstanceRequest, ManagedPort,
    SessionState,
)
from flyt_adapter.network import FakeServicePortProvider
from flyt_adapter.reconcile import (
    GpuCellPoolReconciler, OrphanPortReconciler, SessionReconciler,
)
from flyt_adapter.service import FlytLifecycleService


@dataclass
class FakeNovaInventory:
    values: dict[str, str]

    def statuses(self, instance_uuids):
        return {value: self.values[value] for value in instance_uuids if value in self.values}


@dataclass
class FakeRequestFactory:
    test: "ReconcileTest"

    def instance_request(self, instance_uuid, port):
        return self.test.request(instance_uuid, port)


class FakeGpuCellPool:
    def __init__(self):
        self.ensured = []

    def ensure(self, *, cell_profile):
        self.ensured.append(cell_profile)


class GpuCellPoolReconcileTest(unittest.TestCase):
    def test_prewarms_each_physical_profile_once(self) -> None:
        provider = FakeGpuCellPool()
        count = GpuCellPoolReconciler(
            provider, ["whole-mps", "whole-mps", "passthrough"]
        ).run_once()
        self.assertEqual(2, count)
        self.assertEqual(["passthrough", "whole-mps"], provider.ensured)


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.capacity = FakeCapacityProvider({"gpu-small": 3})
        self.quotas = FakeQuotaProvider({("project-1", "gpu-small"): 3})
        self.backend = FakeFlytBackend()
        self.lifecycle = FlytLifecycleService(
            approvals=InMemoryApprovalRegistry({
                "image-1": ImageApproval("image-1", "sum", "v1", "cuda-v1")
            }),
            quotas=self.quotas,
            capacity=self.capacity,
            backend=self.backend,
            credentials=FakeCredentialIssuer(),
        )
        for number in range(1, 4):
            self.lifecycle.admit_build(InstanceRequest(
                instance_uuid=f"instance-{number}",
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

    def request(self, instance_uuid, port=None):
        return InstanceRequest(
            instance_uuid=instance_uuid,
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
            service_port=port,
        )

    def test_repairs_active_error_and_missing_instances(self) -> None:
        result = SessionReconciler(self.lifecycle, FakeNovaInventory({
            "instance-1": "ACTIVE",
            "instance-2": "ERROR",
        })).run_once()
        self.assertEqual((1, 1, 1, 0), (
            result.started, result.aborted, result.deleted, result.unchanged
        ))
        self.assertEqual(SessionState.READY, self.lifecycle.sessions["instance-1"].state)
        self.assertEqual(SessionState.FAILED, self.lifecycle.sessions["instance-2"].state)
        self.assertEqual(SessionState.DELETED, self.lifecycle.sessions["instance-3"].state)
        self.assertEqual(1, len(self.capacity.reservations))
        self.assertEqual(1, len(self.backend.sessions))

    def test_promotes_pending_capacity_after_gpu_cell_becomes_ready(self) -> None:
        record = self.lifecycle.sessions["instance-1"]
        record.state = SessionState.PENDING_CAPACITY
        self.backend.refresh_session = lambda _record: BackendSession(
            _record.instance_uuid, SessionState.READY
        )
        result = SessionReconciler(
            self.lifecycle,
            FakeNovaInventory({
                "instance-1": "ACTIVE",
                "instance-2": "BUILD",
                "instance-3": "BUILD",
            }),
        ).run_once()
        self.assertEqual(1, result.started)
        self.assertEqual(SessionState.READY, record.state)

    def test_orphan_port_cleanup_observes_grace_vm_and_session(self) -> None:
        ports = FakeServicePortProvider({"az1": "network-1"})
        for instance_uuid in ("old-orphan", "new-orphan", "existing-vm", "existing-session"):
            port = ports.ensure(
                instance_uuid=instance_uuid,
                project_id="project-1",
                availability_zone="az1",
            )
            created_at = (
                "2026-08-24T00:59:00Z" if instance_uuid == "new-orphan"
                else "2026-08-24T00:00:00Z"
            )
            ports.ports[instance_uuid] = ManagedPort(
                **{**port.__dict__, "created_at": created_at}
            )
        self.lifecycle.sessions["existing-session"] = self.lifecycle.sessions["instance-1"]
        result = OrphanPortReconciler(
            self.lifecycle,
            FakeNovaInventory({"existing-vm": "BUILD"}),
            ports,
            grace_seconds=900,
        ).run_once(datetime(2026, 8, 24, 1, 0, tzinfo=timezone.utc))
        self.assertEqual((1, 3), (result.deleted, result.retained))
        self.assertIsNone(ports.get("old-orphan"))
        self.assertIsNotNone(ports.get("new-orphan"))

    def test_port_reconciler_recovers_lost_create_notification(self) -> None:
        self.capacity.inventory["gpu-small"] = 4
        self.quotas.limits[("project-1", "gpu-small")] = 4
        ports = FakeServicePortProvider({"az1": "network-1"})
        ports.ensure(
            instance_uuid="notification-lost",
            project_id="project-1",
            availability_zone="az1",
        )
        result = OrphanPortReconciler(
            self.lifecycle,
            FakeNovaInventory({"notification-lost": "ACTIVE"}),
            ports,
            requests=FakeRequestFactory(self),
        ).run_once()
        self.assertEqual(1, result.admitted)
        self.assertEqual(SessionState.READY, self.lifecycle.sessions["notification-lost"].state)


if __name__ == "__main__":
    unittest.main()
