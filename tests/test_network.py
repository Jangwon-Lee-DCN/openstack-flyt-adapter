from __future__ import annotations

import unittest

from flyt_adapter.network import FakeServicePortProvider, NeutronServicePortProvider
from flyt_adapter.openstack import OpenStackError


class RecordingTransport:
    def __init__(self) -> None:
        self.calls = []
        self.port = None

    def request(self, method, url, *, headers=None, payload=None):
        self.calls.append((method, url, headers, payload))
        if method == "GET":
            return 200, {"ports": [] if self.port is None else [self.port]}
        if method == "POST":
            requested = payload["port"]
            self.port = {
                **requested,
                "id": "port-1",
                "mac_address": "fa:16:3e:00:00:01",
                "fixed_ips": [{"ip_address": "172.30.1.10"}],
                "created_at": "2026-08-24T00:00:00Z",
            }
            return 201, {"port": self.port}
        if method == "DELETE":
            self.port = None
            return 204, {}
        raise AssertionError(method)


class ServicePortTest(unittest.TestCase):
    def test_fake_port_is_idempotent_and_az_scoped(self) -> None:
        provider = FakeServicePortProvider({"az1": "network-1"})
        first = provider.ensure(
            instance_uuid="vm-1", project_id="project-1", availability_zone="az1"
        )
        second = provider.ensure(
            instance_uuid="vm-1", project_id="project-1", availability_zone="az1"
        )
        self.assertEqual(first, second)
        self.assertEqual("network-1", first.network_id)
        provider.delete("vm-1")
        self.assertIsNone(provider.get("vm-1"))
        with self.assertRaisesRegex(RuntimeError, "no FLYT service network"):
            provider.ensure(
                instance_uuid="vm-2", project_id="project-1", availability_zone="az2"
            )

    def test_neutron_port_contract_and_cleanup(self) -> None:
        transport = RecordingTransport()
        provider = NeutronServicePortProvider(
            "https://neutron.invalid", "token", transport,
            {"az1": "network-1"}, {"az1": "subnet-1"}, ("sg-1",),
        )
        value = provider.ensure(
            instance_uuid="vm-1", project_id="project-1", availability_zone="az1"
        )
        self.assertEqual("port-1", value.port_id)
        self.assertEqual("172.30.1.10", value.service_ip)
        self.assertEqual((value,), provider.list_managed())
        create = next(call for call in transport.calls if call[0] == "POST")
        self.assertEqual("", create[3]["port"]["device_owner"])
        self.assertEqual("", create[3]["port"]["device_id"])
        self.assertIn("instance-uuid=vm-1", create[3]["port"]["tags"])
        self.assertEqual(["sg-1"], create[3]["port"]["security_groups"])
        self.assertEqual([{"subnet_id": "subnet-1"}], create[3]["port"]["fixed_ips"])
        self.assertEqual(value, provider.ensure(
            instance_uuid="vm-1", project_id="project-1", availability_zone="az1"
        ))
        provider.delete("vm-1")
        self.assertIsNone(provider.get("vm-1"))

    def test_unknown_az_fails_before_create(self) -> None:
        transport = RecordingTransport()
        provider = NeutronServicePortProvider(
            "https://neutron.invalid", "token", transport, {"az1": "network-1"}
        )
        with self.assertRaisesRegex(OpenStackError, "no FLYT service network"):
            provider.ensure(
                instance_uuid="vm-1", project_id="project-1", availability_zone="az2"
            )
        self.assertFalse(any(call[0] == "POST" for call in transport.calls))


if __name__ == "__main__":
    unittest.main()
