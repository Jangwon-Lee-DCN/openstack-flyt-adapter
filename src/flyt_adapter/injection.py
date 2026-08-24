"""Render minimal cloud-init vendor data for client package injection."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class ClientPackage:
    url: str
    digest: str

    def __post_init__(self) -> None:
        if not self.url.startswith("https://"):
            raise ValueError("client package URL must use HTTPS")
        if not _DIGEST.fullmatch(self.digest):
            raise ValueError("client package digest must be sha256:<64 lowercase hex>")


def render_cloud_config(*, package: ClientPackage, bootstrap_token: str,
                        instance_uuid: str, generation: int,
                        manager_endpoint: str) -> str:
    """Return cloud-config that verifies the package before atomic installation.

    The token is a one-time bootstrap credential. The installed client must
    exchange it for its short-lived session identity and erase the file.
    """

    url = shlex.quote(package.url)
    expected = shlex.quote(package.digest.removeprefix("sha256:"))
    token = shlex.quote(bootstrap_token)
    uuid = shlex.quote(instance_uuid)
    endpoint = shlex.quote(manager_endpoint)
    return f"""#cloud-config
write_files:
  - path: /run/flyt/bootstrap-token
    permissions: '0600'
    content: {token}
  - path: /run/flyt/openstack-session.env
    permissions: '0600'
    content: |
      FLYT_INSTANCE_UUID={uuid}
      FLYT_GENERATION={generation}
      FLYT_MANAGER_ENDPOINT={endpoint}
runcmd:
  - [sh, -ec, "curl -fsS {url} -o /run/flyt-client.pkg"]
  - [sh, -ec, "echo '{expected}  /run/flyt-client.pkg' | sha256sum -c -"]
  - [sh, -ec, "/usr/local/sbin/install-flyt-client /run/flyt-client.pkg /run/flyt/bootstrap-token /run/flyt/openstack-session.env"]
  - [sh, -ec, "rm -f /run/flyt-client.pkg /run/flyt/bootstrap-token /run/flyt/openstack-session.env"]
"""
