from __future__ import annotations

import unittest

from flyt_adapter.kubernetes import (
    FakeKubernetesTransport, GpuCellProfile, KubernetesGpuCellProvider,
)
from flyt_adapter.fakes import FakeFlytBackend
from flyt_adapter.flyt import GpuCellFlytBackend
from flyt_adapter.models import GpuCellState, SessionRecord, SessionState


class KubernetesGpuCellTest(unittest.TestCase):
    def provider(self, transport=None):
        return KubernetesGpuCellProvider(
            namespace="flyt-system",
            image="registry.example/flyt-gpu-cell@sha256:" + "a" * 64,
            cluster_manager_address="flyt-cluster-manager:12401",
            profiles={"rtx3090ti-mps": GpuCellProfile(
                device_class="gpu.nvidia.com",
                selectors=("device.attributes['gpu.nvidia.com'].productName.lowerAscii().matches('^.*rtx 3090 ti.*$')",),
                expected_product_name="NVIDIA GeForce RTX 3090 Ti",
            )},
            transport=transport or FakeKubernetesTransport(),
            runtime_class_name="nvidia",
            host_network=True,
        )

    def test_whole_gpu_claim_and_cell_are_idempotent(self):
        provider = self.provider()
        first = provider.ensure(cell_profile="rtx3090ti-mps")
        second = provider.ensure(cell_profile="rtx3090ti-mps")
        self.assertEqual(first, second)
        self.assertEqual(GpuCellState.POD_PENDING, first.state)
        claim = next(value for path, value in provider.transport.objects.items()
                     if "/resourceclaims/" in path)
        request = claim["spec"]["devices"]["requests"][0]["exactly"]
        self.assertEqual("gpu.nvidia.com", request["deviceClassName"])
        self.assertEqual(1, request["count"])
        self.assertIn("rtx 3090 ti", request["selectors"][0]["cel"]["expression"])
        pod = next(value for path, value in provider.transport.objects.items()
                   if "/pods/" in path)
        env = {item["name"]: item["value"] for item in pod["spec"]["containers"][0]["env"]}
        self.assertEqual("NVIDIA GeForce RTX 3090 Ti", env["FLYT_EXPECTED_GPU_PRODUCT_NAME"])
        self.assertTrue(pod["spec"]["hostNetwork"])
        self.assertEqual("ClusterFirstWithHostNet", pod["spec"]["dnsPolicy"])

    def test_dra_readiness_requires_device_class_and_expected_product(self):
        provider = self.provider()
        result = provider.readiness("rtx3090ti-mps")
        self.assertFalse(result.ready)
        device_class_path = "/apis/resource.k8s.io/v1/deviceclasses/gpu.nvidia.com"
        slices_path = "/apis/resource.k8s.io/v1/resourceslices"
        provider.transport.objects[device_class_path] = {"metadata": {"name": "gpu.nvidia.com"}}
        provider.transport.objects[slices_path] = {"items": [{"spec": {
            "driver": "gpu.nvidia.com",
            "devices": [{"name": "gpu-0", "attributes": {
                "gpu.nvidia.com/productName": {"string": "NVIDIA RTX 6000 Ada"}
            }}],
        }}]}
        result = provider.readiness("rtx3090ti-mps")
        self.assertFalse(result.ready)
        self.assertIn("not published", result.reason)
        provider.transport.objects[slices_path]["items"][0]["spec"]["devices"][0][
            "attributes"
        ]["gpu.nvidia.com/productName"]["string"] = "NVIDIA GeForce RTX 3090 Ti"
        self.assertTrue(provider.readiness("rtx3090ti-mps").ready)

        attributes = provider.transport.objects[slices_path]["items"][0]["spec"][
            "devices"
        ][0]["attributes"]
        attributes["productName"] = attributes.pop("gpu.nvidia.com/productName")
        self.assertTrue(provider.readiness("rtx3090ti-mps").ready)

    def test_ready_status_and_idempotent_delete(self):
        provider = self.provider()
        cell = provider.ensure(cell_profile="rtx3090ti-mps")
        pod_path = next(path for path in provider.transport.objects if "/pods/" in path)
        provider.transport.objects[pod_path]["status"] = {
            "phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]
        }
        self.assertEqual(GpuCellState.READY, provider.get("rtx3090ti-mps").state)
        provider.delete("rtx3090ti-mps")
        provider.delete("rtx3090ti-mps")
        self.assertIsNone(provider.get("rtx3090ti-mps"))
        self.assertEqual("flyt-cell-rtx3090ti-mps", cell.name)

    def test_pod_failure_rolls_back_claim(self):
        transport = FakeKubernetesTransport(fail_pod_create=True)
        provider = self.provider(transport)
        with self.assertRaisesRegex(RuntimeError, "Pod creation failed"):
            provider.ensure(cell_profile="rtx3090ti-mps")
        self.assertFalse(any("resourceclaims/" in path for path in transport.objects))

    def test_unknown_physical_profile_fails_closed(self):
        provider = self.provider()
        with self.assertRaisesRegex(RuntimeError, "unknown GPU Cell profile"):
            provider.ensure(cell_profile="another-profile")

    def test_composite_backend_keeps_shared_cell_after_session_delete(self):
        provider = self.provider()
        manager = FakeFlytBackend()
        backend = GpuCellFlytBackend(manager, provider, {"gpu-small": "rtx3090ti-mps"})
        session = backend.create_session(SessionRecord(
            instance_uuid="vm-1", project_id="project-1", profile="gpu-small",
            reservation_id="reservation-vm-1", port_id="port-1",
            service_ip="192.0.2.10", bootstrap_token="secret",
        ))
        self.assertEqual(SessionState.PENDING_CAPACITY, session.state)
        self.assertIn(session.session_id, manager.sessions)
        pod_path = next(path for path in provider.transport.objects if "/pods/" in path)
        provider.transport.objects[pod_path]["status"] = {
            "phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]
        }
        refreshed = backend.refresh_session(SessionRecord(
            instance_uuid="vm-1", project_id="project-1", profile="gpu-small",
            reservation_id="reservation-vm-1", backend_session_id=session.session_id,
        ))
        self.assertEqual(SessionState.READY, refreshed.state)
        backend.delete_session(session.session_id)
        self.assertNotIn(session.session_id, manager.sessions)
        self.assertIsNotNone(provider.get("rtx3090ti-mps"))

    def test_two_logical_sessions_share_one_mps_cell(self):
        provider = self.provider()
        manager = FakeFlytBackend()
        backend = GpuCellFlytBackend(manager, provider, {"gpu-small": "rtx3090ti-mps"})
        for instance in ("vm-1", "vm-2"):
            backend.create_session(SessionRecord(
                instance_uuid=instance, project_id="project-1", profile="gpu-small",
                reservation_id=f"reservation-{instance}", port_id=f"port-{instance}",
                service_ip="192.0.2.10", bootstrap_token="secret",
            ))
        pod_creates = [request for request in provider.transport.requests
                       if request[0] == "POST" and request[1].endswith("/pods")]
        claim_creates = [request for request in provider.transport.requests
                         if request[0] == "POST" and request[1].endswith("/resourceclaims")]
        self.assertEqual(1, len(pod_creates))
        self.assertEqual(1, len(claim_creates))
        self.assertEqual({"flyt-vm-1", "flyt-vm-2"}, set(manager.sessions))

    def test_failed_cell_never_creates_a_logical_session(self):
        provider = self.provider()
        provider.ensure(cell_profile="rtx3090ti-mps")
        pod_path = next(path for path in provider.transport.objects if "/pods/" in path)
        provider.transport.objects[pod_path]["status"] = {"phase": "Failed"}
        manager = FakeFlytBackend()
        backend = GpuCellFlytBackend(manager, provider, {"gpu-small": "rtx3090ti-mps"})
        with self.assertRaisesRegex(RuntimeError, "GPU Cell .* is failed"):
            backend.create_session(SessionRecord(
                instance_uuid="vm-1", project_id="project-1", profile="gpu-small",
                reservation_id="reservation-vm-1", port_id="port-1",
                service_ip="192.0.2.10", bootstrap_token="secret",
            ))
        self.assertFalse(manager.sessions)


if __name__ == "__main__":
    unittest.main()
