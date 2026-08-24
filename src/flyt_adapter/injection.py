"""Render executable cloud-init vendor data for client package injection."""

from __future__ import annotations

import base64
import ipaddress
import re
import shlex
import urllib.parse
from dataclasses import dataclass


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class ClientPackage:
    url: str
    digest: str
    ca_certificate: str | None = None
    resolve_address: str | None = None

    def __post_init__(self) -> None:
        if not self.url.startswith("https://"):
            raise ValueError("client package URL must use HTTPS")
        if not _DIGEST.fullmatch(self.digest):
            raise ValueError("client package digest must be sha256:<64 lowercase hex>")
        if self.ca_certificate is not None and "BEGIN CERTIFICATE" not in self.ca_certificate:
            raise ValueError("client package CA is not a PEM certificate")
        if self.resolve_address is not None:
            ipaddress.ip_address(self.resolve_address)


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
    session_lines = " ".join(shlex.quote(value) for value in (
        f"FLYT_INSTANCE_UUID={instance_uuid}",
        f"FLYT_GENERATION={generation}",
        f"FLYT_MANAGER_ENDPOINT={manager_endpoint}",
    ))
    ca_setup = ""
    curl_ca = ""
    curl_resolve = ""
    if package.ca_certificate:
        encoded_ca = shlex.quote(base64.b64encode(package.ca_certificate.encode()).decode())
        ca_setup = (
            f"printf '%s' {encoded_ca} | base64 -d > /run/flyt/package-ca.crt; "
            "chmod 0600 /run/flyt/package-ca.crt; "
        )
        curl_ca = "--cacert /run/flyt/package-ca.crt "
    if package.resolve_address:
        parsed = urllib.parse.urlsplit(package.url)
        curl_resolve = "--resolve " + shlex.quote(
            f"{parsed.hostname}:{parsed.port or 443}:{package.resolve_address}"
        ) + " "
    setup_command = ("install -d -m 0700 /run/flyt; "
        f"printf '%s' {token} > /run/flyt/bootstrap-token; "
        "chmod 0600 /run/flyt/bootstrap-token; "
        f"printf '%s\\n' {session_lines} > /run/flyt/openstack-session.env; "
        "chmod 0600 /run/flyt/openstack-session.env; " + ca_setup).rstrip("; ")
    commands = (
        ("setup", setup_command),
        ("download", f"curl {curl_ca}{curl_resolve}-fsS --retry 24 --retry-all-errors "
        "--retry-delay 5 --connect-timeout 10 --max-time 300 "
        f"{url} -o /run/flyt-client.pkg"),
        ("verify", f"echo '{expected}  /run/flyt-client.pkg' | sha256sum -c -"),
        ("extract-installer", 'install_dir=$(mktemp -d /run/flyt-bootstrap.XXXXXX); '
        'tar -xzf /run/flyt-client.pkg -C "$install_dir" '
        '--no-same-owner --no-same-permissions install-flyt-client; '
        'test -f "$install_dir/install-flyt-client"; '
        'test ! -L "$install_dir/install-flyt-client"; '
        'install -m 0700 "$install_dir/install-flyt-client" '
        '/run/install-flyt-client; rm -rf "$install_dir"'),
        ("install", "/bin/bash /run/install-flyt-client /run/flyt-client.pkg "
        "/run/flyt/bootstrap-token /run/flyt/openstack-session.env"),
        ("cleanup", "rm -f /run/install-flyt-client /run/flyt-client.pkg "
        "/run/flyt/bootstrap-token /run/flyt/openstack-session.env "
        "/run/flyt/package-ca.crt"),
    )
    script = (
        "exec >/dev/ttyS0 2>&1; set -e; step=initial; "
        "trap 'rc=$?; if [ $rc -ne 0 ]; then echo FLYT_BOOTSTRAP_FAILED step=$step rc=$rc; fi' EXIT; "
        + "; ".join(
            f"step={shlex.quote(name)}; echo FLYT_BOOTSTRAP_STEP=$step; {command}"
            for name, command in commands
        )
        + "; echo FLYT_BOOTSTRAP_COMPLETE"
    )
    # Dynamic vendordata2 is processed by cloud-init independently from user
    # data. A vendor shell script is not subject to cloud-config list merging
    # (a user's runcmd cannot replace it), and runs in the final stage after
    # OpenStack networking is online.
    return "#!/bin/sh\nexec sh -ec " + shlex.quote(script) + "\n"
