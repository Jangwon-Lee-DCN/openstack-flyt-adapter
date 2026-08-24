# Approved-image injection prerequisite

An image may receive `flyt:injectable=true` only after the fixed installer in
this directory is installed at `/usr/local/sbin/install-flyt-client` and its
behavior is included in image acceptance. The runtime package is a gzip tar
containing `flyt-client-manager`, `cricket-client.so`, and
`flyt-client-manager.service`. Nova vendor data verifies the whole package
digest before invoking the installer.

The installer writes the UUID, generation, manager endpoint, and high-entropy
session credential to a root-only TOML file. It never evaluates vendor-data
content as shell. Replacing CUDA runtime libraries is deliberately not done by
this generic installer; the approved base image must activate the tested FLYT
CUDA interception mechanism in its image-specific build step.
