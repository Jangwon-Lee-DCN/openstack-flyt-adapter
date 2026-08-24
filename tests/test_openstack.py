from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from typing import Any, Mapping

from flyt_adapter.fakes import NoCapacityError
from flyt_adapter.openstack import (
    OpenStackCatalog,
    NovaInstanceStatusInventory,
    PlacementCapacityProvider,
    PlacementInventoryManager,
    _resource_class,
    provider_uuid,
)


@dataclass
class StubTransport:
    responses: list[tuple[int, Mapping[str, Any]]] = field(default_factory=list)
    calls: list[tuple[str, str, Mapping[str, str], Mapping[str, Any] | None]] = field(
        default_factory=list
    )

    def request(self, method, url, *, headers=None, payload=None):
        self.calls.append((method, url, dict(headers or {}), payload))
        if not self.responses:
            return 204, {}
        return self.responses.pop(0)


class OpenStackAdapterTest(unittest.TestCase):
    def test_profile_prefix_is_not_duplicated_in_resource_class(self) -> None:
        self.assertEqual(
            "CUSTOM_FLYT_NVIDIA_RTX3090TI_MPS_SHARED_V1",
            _resource_class("flyt-nvidia-rtx3090ti-mps-shared-v1"),
        )

    def test_catalog_reads_authoritative_flavor_and_image(self) -> None:
        transport = StubTransport(responses=[
            (200, {"flavor": {
                "id": "flavor-1",
                "name": "flyt.small",
            }}),
            (200, {"extra_specs": {"flyt:enabled": "true", "flyt:profile": "gpu-small"}}),
            (200, {
                "id": "image-1",
                "os_hash_value": "sha512-value",
                "flyt:injectable": "true",
                "flyt:injection_schema": "v1",
                "flyt:compatibility": "cuda-v1",
                "untrusted-property": {"not": "forwarded"},
            }),
        ])
        catalog = OpenStackCatalog("https://nova/v2.1/project", "https://glance", "token", transport)
        flavor = catalog.flavor("flavor-1")
        image = catalog.image("image-1")
        self.assertEqual("gpu-small", flavor.flyt_profile)
        self.assertEqual("sha512-value", image.checksum)
        self.assertNotIn("untrusted-property", image.properties)

    def test_capacity_provider_verifies_nova_owned_allocation(self) -> None:
        flyt_id = provider_uuid("gpu-small")
        transport = StubTransport(responses=[
            (200, {"allocations": {
                flyt_id: {"resources": {"CUSTOM_FLYT_GPU_SMALL": 1}}
            }}),
        ])
        provider = PlacementCapacityProvider("https://placement", "token", transport)
        reservation = provider.reserve(
            profile="gpu-small",
            consumer_uuid="instance-1",
            project_id="project-1",
            user_id="user-1",
        )
        self.assertEqual("instance-1", reservation)
        self.assertEqual("GET", transport.calls[0][0])
        provider.release(reservation)
        self.assertEqual(1, len(transport.calls))

    def test_capacity_provider_rejects_empty_candidates(self) -> None:
        transport = StubTransport(responses=[(200, {"allocations": {}})])
        provider = PlacementCapacityProvider("https://placement", "token", transport)
        with self.assertRaisesRegex(NoCapacityError, "no FLYT allocation"):
            provider.reserve(
                profile="gpu-small",
                consumer_uuid="instance-1",
                project_id="project-1",
                user_id="user-1",
            )

    def test_capacity_availability_uses_allocation_candidates(self) -> None:
        flyt_id = provider_uuid("gpu-small")
        transport = StubTransport(responses=[(200, {"allocation_requests": [{
            "allocations": {flyt_id: {"resources": {"CUSTOM_FLYT_GPU_SMALL": 1}}}
        }]})])
        provider = PlacementCapacityProvider("https://placement", "token", transport)
        self.assertTrue(provider.available(profile="gpu-small"))

    def test_inventory_manager_creates_zero_inventory_shared_provider(self) -> None:
        profile = "gpu-small"
        provider_id = provider_uuid(profile)
        aggregate = "11111111-1111-4111-8111-111111111111"
        transport = StubTransport(responses=[
            (204, {}),
            (204, {}),
            (200, {"resource_providers": []}),
            (200, {}),
            (200, {"generation": 0}),
            (200, {}),
            (200, {"generation": 1}),
            (200, {}),
            (200, {"aggregates": [], "resource_provider_generation": 2}),
            (200, {}),
        ])
        manager = PlacementInventoryManager("https://placement", "token", transport)
        self.assertEqual(provider_id, manager.ensure_profile(
            profile=profile, total=0, aggregate_uuid=aggregate
        ))
        inventory_call = next(call for call in transport.calls if call[1].endswith("/inventories"))
        self.assertEqual({}, inventory_call[3]["inventories"])
        self.assertFalse(any(
            method == "PUT" and url.endswith("/traits/MISC_SHARES_VIA_AGGREGATE")
            for method, url, _, _ in transport.calls
        ))

    def test_inventory_manager_joins_remote_provider_to_compute_aggregates(self) -> None:
        profile = "gpu-small"
        provider_id = provider_uuid(profile)
        compute_id = "22222222-2222-4222-8222-222222222222"
        sharing_aggregate = "11111111-1111-4111-8111-111111111111"
        rack_aggregate = "33333333-3333-4333-8333-333333333333"
        transport = StubTransport(responses=[
            (204, {}),
            (204, {}),
            (200, {"resource_providers": [{"uuid": provider_id}]}),
            (200, {"generation": 0}),
            (200, {}),
            (200, {"generation": 1}),
            (200, {}),
            (200, {"aggregates": [rack_aggregate], "resource_provider_generation": 4}),
            (200, {}),
            (200, {"aggregates": [rack_aggregate, sharing_aggregate],
                   "resource_provider_generation": 5}),
            (200, {"aggregates": [sharing_aggregate], "resource_provider_generation": 2}),
            (200, {}),
        ])
        manager = PlacementInventoryManager("https://placement", "token", transport)
        manager.ensure_profile(
            profile=profile,
            total=2,
            aggregate_uuid=sharing_aggregate,
            compute_provider_uuids=(compute_id,),
        )
        provider_update = [
            call for call in transport.calls
            if call[0] == "PUT" and call[1].endswith(f"/{provider_id}/aggregates")
        ][0]
        self.assertEqual(
            sorted([rack_aggregate, sharing_aggregate]),
            provider_update[3]["aggregates"],
        )

    def test_nova_inventory_normalizes_status(self) -> None:
        transport = StubTransport(responses=[(200, {"server": {"status": "active"}})])
        inventory = NovaInstanceStatusInventory("https://nova/v2.1/project", "token", transport)
        self.assertEqual(
            {"instance-1": "ACTIVE"}, inventory.statuses(("instance-1",))
        )


if __name__ == "__main__":
    unittest.main()
