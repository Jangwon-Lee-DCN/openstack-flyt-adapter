# OpenStack FLYT adapter development rules

Read `/home/ubuntu/AGENTS.md` first. This repository owns only the
OpenStack-to-FLYT adapter implementation and its tests. OpenStack service
configuration belongs in `openstack-cloud-services`, immutable deployment
artifacts in `openstack-cloud-reproducibility`, and live topology, acceptance,
and promotion in `openstack-production-datacenter`.

The adapter must remain fail-closed: an unknown Flavor, unapproved image,
missing GPU capacity, stale reservation, or backend error must never produce a
FLYT-ready VM. Tests must cover idempotent create/delete and orphan cleanup.
