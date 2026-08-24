# OpenStack FLYT Adapter

Control plane for presenting remote FLYT GPU capacity as an OpenStack Flavor.

Flavor display names follow
`gpu.<delivery>.<vendor>.<device-or-family>.<partition>.<size>`. For example,
`gpu.passthrough.nvidia.rtx3090ti.whole` is a Nova PCI passthrough product and
must not carry `flyt:enabled`; `gpu.remote.nvidia.rtx3090ti.mps.shared` is the
initial non-guaranteed FLYT remote-GPU development product. The immutable
`flyt:profile` offering ID remains the
scheduling identity even if an operator renames the display name.
It is intentionally runnable with zero GPU inventory so the OpenStack contract
can be completed before GPU workers arrive.

The GPU-free control plane implements:

- FLYT Flavor and approved Glance image admission;
- a Placement-like capacity reservation boundary;
- idempotent VM-to-FLYT-session lifecycle reconciliation;
- fake Placement and FLYT backends for GPU-free development;
- Nova DynamicJSON-compatible cloud-init vendor-data rendering for automatic
  client injection;
- durable SQLite session state and restart recovery;
- an authenticated HTTP admission/event/status boundary;
- OpenStack Nova/Glance lookup and Placement inventory/allocation adapters;
- AZ-scoped Neutron service-port creation, idempotent lookup, and rollback;
- normalized Nova `instance.create.start/end/error` and delete event handling,
  including versioned notification payloads;
- periodic session reconciliation for lost/reordered Nova notifications and
  grace-bounded cleanup of orphaned managed ports;
- a non-root image and digest-pinned Helm deployment.

The upstream FLYT revision inspected during development was
`ff843bdc9945006c4f0e553ac3f23908a7cc4ba1`. Its Cluster Manager has raw
TCP/Unix control protocols, VM-IP identity, and MongoDB resource records, but
no authenticated create/delete session API keyed by OpenStack instance UUID.
The `flyt-managed-runtime` downstream adds a platform-neutral managed-session
protocol. The adapter maps Nova instance UUID, Keystone project, Neutron port,
and service IP to the runtime's `workload_id`, `tenant_id`, `attachment_id`,
and `client_address`; the fake backend keeps the same contract available for
deterministic unit tests.

The Cluster Manager itself can run without a GPU and is intended to be placed
on `dcn-1b-utility-0` in development. The fork splits the GPU build feature,
reads the Cluster Manager configuration path at runtime, persists externally
managed workload identity in MongoDB, and builds the manager with
`--no-default-features` without CUDA or a GPU node.

There is no separate REST facade, Session Controller, or per-session Kubernetes
custom resource. The adapter itself owns desired state and reconciliation and
uses a `ClusterManagerBackend` to project Nova instance UUID, project, profile,
managed Neutron port, service IP, and generation into the Cluster Manager.
Kubernetes is only the deployment substrate. With zero Node Managers the
adapter session must remain `PENDING_CAPACITY`, never `READY`.

FLYT VMs use an automatically attached dedicated service NIC. The operator owns
AZ-local FLYT networks, subnets, routing, security groups, and QoS in the
`flyt-service` project; the adapter owns only per-VM managed ports. Users do not
select or modify those ports. The service NIC has no default route and reaches
only the FLYT client endpoint. Nova API pre-build integration must create the
port before guest spawn; `instance.create.start` is used only to verify the
Nova-owned Placement claim and materialize the Cluster Manager session.

Nova metadata/config-drive integration uses the stock DynamicJSON driver.  The
target name is significant because cloud-init's OpenStack datasource only
consumes the top-level `cloud-init` member:

```ini
[api]
vendordata_providers = StaticJSON,DynamicJSON
vendordata_dynamic_targets = cloud-init@https://flyt-adapter.openstack.svc/v1/nova/vendor-data
vendordata_dynamic_failure_fatal = true

[vendordata_dynamic_auth]
auth_type = token_endpoint
endpoint = https://flyt-adapter.openstack.svc
token = <same secret as adapter api_token>
```

The adapter accepts that token through `X-Auth-Token`. Production should mount
it from a secret and restrict the endpoint to Nova with network policy. A
non-FLYT instance receives JSON `null`, while an admitted and READY FLYT
instance receives a JSON string containing `#cloud-config`. Reads are
idempotent because Nova can request dynamic vendor data repeatedly. Generated
cloud-config supplies the instance UUID and dedicated-network Cluster Manager
endpoint to the pre-approved image's installer. Cluster Manager admission is
bound to the managed service IP recorded for that UUID.

Still intentionally blocked on physical GPU capacity:

- Node Manager and GPU Cell runtime validation;
- CUDA/cuDNN/cuBLAS/NCCL compatibility and workload acceptance;
- performance, isolation, MIG/MPS, and physical-GPU failure testing.

For a non-MIG GPU such as an RTX 3090 Ti, enable the Kubernetes GPU Cell
backend with physical profile `nvidia-rtx3090ti-whole-mps`,
`device_class: gpu.nvidia.com`, and a DRA `productName` selector for RTX 3090
Ti. The Adapter directly creates one whole-GPU ResourceClaim and one
long-lived GPU Cell Pod for that physical cell profile. Logical profiles such
as `flyt-nvidia-rtx3090ti-mps-shared-v1` map to that Cell, and FLYT CUDA MPS shares it
among VM sessions. A recommended user-visible Flavor name is
`gpu.remote.nvidia.rtx3090ti.mps.shared`; the stable `flyt:profile` extra spec,
not the display name, is the scheduling and quota identity.
Deleting a VM drains only its Cluster Manager session; it does not delete the
shared Pod or release the whole-GPU claim. GPU Cell teardown is a separate
pool-scoped operation. MIG remains an optional physical profile, not the
default.

The Nova downstream source is owned by `nova-extended-compute`; image packaging
and OpenStack service configuration are owned by their respective platform
repositories. Immutable Adapter, Cluster Manager,
and client-package digests must be produced through the platform image build
queue after the single reviewed feature revision is pushed.

Run the tests with:

```bash
python3 -m unittest discover -s tests -v
```

Run the fake zero-inventory service:

```bash
export FLYT_API_TOKEN='replace-with-a-long-random-development-token'
PYTHONPATH=src python3 -m flyt_adapter --config config.example.json
```

The service listens on `127.0.0.1:8080` in the example configuration. A FLYT
admission against `flyt-nvidia-rtx3090ti-mps-shared-v1` is expected to fail because its inventory is
zero. This is the correct contract before physical capacity is admitted.
