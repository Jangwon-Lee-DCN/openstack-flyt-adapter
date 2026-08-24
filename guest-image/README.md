# Approved-image injection prerequisite

An image may receive `flyt:injectable=true` only after its exact Glance checksum
passes the guest compatibility suite. The runtime package is a gzip tar
containing `install-flyt-client`, `flyt-client-manager`, `cricket-client.so`,
and `flyt-client-manager.service`. Nova vendor data verifies the whole package
digest before extracting and invoking the installer, so an otherwise compatible
Ubuntu cloud image does not need a preinstalled FLYT bootstrap script.

The installer writes the UUID, generation, manager endpoint, and high-entropy
session credential to a root-only TOML file. It never evaluates vendor-data
content as shell. Replacing CUDA runtime libraries is deliberately not done by
this generic installer. Approval is therefore scoped to the tested OS,
architecture, CUDA ABI and FLYT interception profile; cloud-init compatibility
alone is not enough.
