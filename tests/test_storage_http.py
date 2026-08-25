from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

from flyt_adapter.config import ServiceConfig
from flyt_adapter.http import Application, make_handler
from flyt_adapter.models import SessionState
from flyt_adapter.storage import SQLiteSessionStore


class StorageAndHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database = root / "sessions.db"
        self.config_path = root / "config.json"
        self.config_path.write_text(json.dumps({
            "api_token": "internal-test-token-1234",
            "database": str(self.database),
            "listen_host": "127.0.0.1",
            "listen_port": 8080,
            "client_manager_endpoint": "tcp://172.30.0.5:12402",
            "approvals": [{
                "image_id": "image-1",
                "checksum": "sum-1",
                "injection_schema": "v1",
                "compatibility": "cuda-v1",
            }],
            "inventory": {"gpu-small": 1},
            "quotas": [{"project_id": "project-1", "profile": "gpu-small", "limit": 1}],
            "client_package": {
                "url": "https://packages.example.invalid/flyt-client.pkg",
                "digest": "sha256:" + "a" * 64,
            },
            "service_network": {
                "networks": {"default": "fake-flyt-network"},
                "subnets": {},
                "security_group_ids": [],
                "endpoints": {"default": "192.0.2.44"},
            },
        }))
        self.config = ServiceConfig.load(self.config_path)
        self.app = Application.build(self.config)
        self.app.service_ports.ensure(
            instance_uuid="instance-1",
            project_id="project-1",
            availability_zone="default",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def admission_body(self) -> dict[str, object]:
        return {
            "instance_uuid": "instance-1",
            "project_id": "project-1",
            "user_id": "user-1",
            "flavor": {
                "id": "flavor-1",
                "name": "flyt.small",
                "extra_specs": {"flyt:enabled": "true", "flyt:profile": "gpu-small"},
            },
            "image": {
                "id": "image-1",
                "checksum": "sum-1",
                "properties": {
                    "flyt:injectable": "true",
                    "flyt:injection_schema": "v1",
                    "flyt:compatibility": "cuda-v1",
                },
            },
        }

    def test_http_contract_persists_and_delivers_vendor_data_idempotently(self) -> None:
        status, admitted, _ = self.app.handle("POST", "/v1/admissions", self.admission_body())
        self.assertEqual(201, status)
        self.assertEqual("RESERVED", admitted["state"])

        status, ready, _ = self.app.handle("POST", "/v1/events", {
            "event_type": "instance.create.end",
            "payload": {"instance_uuid": "instance-1"},
        })
        self.assertEqual(200, status)
        self.assertEqual("READY", ready["state"])

        status, cloud_config, content_type = self.app.handle(
            "POST", "/v1/vendor-data/instance-1", None
        )
        self.assertEqual(200, status)
        self.assertEqual("text/cloud-config", content_type)
        self.assertTrue(cloud_config.startswith("#!/bin/sh"))
        self.assertIn("--resolve packages.example.invalid:443:192.0.2.44", cloud_config)
        self.assertIn("tcp://192.0.2.44:12402", cloud_config)
        status, repeated, _ = self.app.handle(
            "POST", "/v1/vendor-data/instance-1", None
        )
        self.assertEqual(200, status)
        self.assertEqual(cloud_config, repeated)

        store = SQLiteSessionStore(self.database)
        self.assertEqual(SessionState.READY, store["instance-1"].state)
        self.assertIsNotNone(store["instance-1"].bootstrap_token)

        restarted = Application.build(self.config)
        status, value, _ = restarted.handle("GET", "/v1/sessions/instance-1", None)
        self.assertEqual(200, status)
        self.assertEqual("READY", value["state"])
        status, deleted, _ = restarted.handle("POST", "/v1/events", {
            "event_type": "instance.delete.end",
            "payload": {"instance_uuid": "instance-1"},
        })
        self.assertEqual(200, status)
        self.assertEqual("DELETED", deleted["state"])

    def test_nova_dynamic_vendor_data_contract(self) -> None:
        self.app.handle("POST", "/v1/admissions", self.admission_body())
        body = {
            "project-id": "project-1",
            "instance-id": "instance-1",
            "image-id": "image-1",
            "user-data": None,
            "hostname": "research-vm",
            "metadata": {},
            "boot-roles": "",
        }
        first = self.app.handle("POST", "/v1/nova/vendor-data", body)
        second = self.app.handle("POST", "/v1/nova/vendor-data", body)
        self.assertEqual(200, first[0])
        self.assertEqual("application/json", first[2])
        self.assertEqual(first[1], second[1])
        self.assertTrue(first[1].startswith("#!/bin/sh"))
        self.assertEqual(SessionState.READY, self.app.lifecycle.sessions["instance-1"].state)

        status, reconciled, _ = self.app.handle("POST", "/v1/events", {
            "event_type": "instance.create.end",
            "payload": {"instance_uuid": "instance-1"},
        })
        self.assertEqual(200, status)
        self.assertEqual("READY", reconciled["state"])

        non_flyt = dict(body, **{"instance-id": "ordinary-instance"})
        self.assertEqual(
            (200, None, "application/json"),
            self.app.handle("POST", "/v1/nova/vendor-data", non_flyt),
        )

        wrong_project = dict(body, **{"project-id": "other-project"})
        with self.assertRaisesRegex(Exception, "project"):
            self.app.handle("POST", "/v1/nova/vendor-data", wrong_project)

    def test_second_instance_is_rejected_by_quota_or_capacity(self) -> None:
        self.app.handle("POST", "/v1/admissions", self.admission_body())
        second = self.admission_body()
        second["instance_uuid"] = "instance-2"
        self.app.service_ports.ensure(
            instance_uuid="instance-2",
            project_id="project-1",
            availability_zone="default",
        )
        with self.assertRaisesRegex(Exception, "quota"):
            self.app.handle("POST", "/v1/admissions", second)

    def test_preflight_checks_without_creating_session(self) -> None:
        status, value, _ = self.app.handle("POST", "/v1/preflight", self.admission_body())
        self.assertEqual(200, status)
        self.assertEqual({"eligible": True, "profile": "gpu-small"}, value)
        self.assertFalse(self.app.lifecycle.sessions)

    def test_prebuild_creates_managed_port_and_error_event_rolls_it_back(self) -> None:
        body = self.admission_body()
        body["availability_zone"] = "default"
        with self.assertRaisesRegex(Exception, "no FLYT service network"):
            rejected = self.admission_body()
            rejected["instance_uuid"] = "instance-az2"
            rejected["availability_zone"] = "az2"
            self.app.handle("POST", "/v1/prebuild", rejected)
        status, value, _ = self.app.handle("POST", "/v1/prebuild", body)
        self.assertEqual(201, status)
        self.assertEqual("fake-flyt-network", value["port"]["network_id"])
        self.assertIn(value["session"]["state"], {"RESERVED", "PENDING_CAPACITY"})
        self.assertIn("instance-1", self.app.lifecycle.sessions)
        self.assertIsNotNone(self.app.service_ports.get("instance-1"))
        self.app.handle("POST", "/v1/events", {
            "event_type": "instance.create.error",
            "payload": {"instance_uuid": "instance-1", "exception": "build failed"},
        })
        self.assertIsNone(self.app.service_ports.get("instance-1"))

    def test_prebuild_uses_authenticated_nova_payload_without_catalog_reentry(self) -> None:
        body = self.admission_body()
        body["availability_zone"] = "default"
        self.app.catalog = Mock()
        self.app.catalog.flavor.side_effect = AssertionError("recursive Nova lookup")
        self.app.catalog.image.side_effect = AssertionError("recursive Glance lookup")
        status, value, _ = self.app.handle("POST", "/v1/prebuild", body)
        self.assertEqual(201, status)
        self.assertEqual("gpu-small", value["profile"])

    def test_explicit_prebuild_rollback_is_idempotent(self) -> None:
        body = self.admission_body()
        body["instance_uuid"] = "instance-rollback"
        body["availability_zone"] = "default"
        self.app.handle("POST", "/v1/prebuild", body)
        self.assertIsNotNone(self.app.service_ports.get("instance-rollback"))
        self.assertEqual(
            204,
            self.app.handle("DELETE", "/v1/prebuild/instance-rollback", None)[0],
        )
        self.assertEqual(
            SessionState.FAILED,
            self.app.lifecycle.sessions["instance-rollback"].state,
        )
        self.assertEqual(
            204,
            self.app.handle("DELETE", "/v1/prebuild/instance-rollback", None)[0],
        )

    def test_configuration_rejects_short_api_token(self) -> None:
        raw = json.loads(self.config_path.read_text())
        raw["api_token"] = "short"
        self.config_path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(ValueError, "api_token"):
            ServiceConfig.load(self.config_path)

    def test_gpu_cell_mapping_must_target_a_physical_profile(self) -> None:
        raw = json.loads(self.config_path.read_text())
        raw["kubernetes_gpu_cells"] = {
            "endpoint": "https://kubernetes.default.svc",
            "namespace": "flyt-system",
            "image": "example/cell@sha256:" + "a" * 64,
            "cluster_manager_address": "manager:12401",
            "session_profiles": {"gpu-small": "missing-cell"},
            "profiles": {
                "rtx3090ti-mps": {"device_class": "gpu.nvidia.com", "selectors": []}
            },
        }
        self.config_path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(ValueError, "mapping targets are undefined"):
            ServiceConfig.load(self.config_path)

    def test_gpu_cell_offering_catalog_must_be_consistent(self) -> None:
        raw = json.loads(self.config_path.read_text())
        raw["flyt_manager"] = {
            "socket_path": "/run/flyt/manager.sock",
            "profiles": {"another-offering": {"compute_units": 16, "memory_mb": 8192}},
        }
        raw["kubernetes_gpu_cells"] = {
            "endpoint": "https://kubernetes.default.svc",
            "namespace": "flyt-system",
            "image": "example/cell@sha256:" + "a" * 64,
            "cluster_manager_address": "manager:12401",
            "session_profiles": {"gpu-small": "rtx3090ti-mps"},
            "profiles": {
                "rtx3090ti-mps": {"device_class": "gpu.nvidia.com", "selectors": []}
            },
        }
        self.config_path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(ValueError, "must exactly match"):
            ServiceConfig.load(self.config_path)

    def test_real_http_boundary_requires_authentication(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            self.assertEqual(200, urllib.request.urlopen(f"{base}/healthz").status)
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(f"{base}/v1/sessions/instance-1")
            self.assertEqual(401, denied.exception.code)
            request = urllib.request.Request(
                f"{base}/v1/sessions/missing",
                headers={"Authorization": f"Bearer {self.config.api_token}"},
            )
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(request)
            self.assertEqual(404, missing.exception.code)
            service_request = urllib.request.Request(
                f"{base}/v1/sessions/missing",
                headers={"X-Auth-Token": self.config.api_token},
            )
            with self.assertRaises(urllib.error.HTTPError) as service_missing:
                urllib.request.urlopen(service_request)
            self.assertEqual(404, service_missing.exception.code)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
