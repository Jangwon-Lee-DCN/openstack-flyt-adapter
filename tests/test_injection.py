from __future__ import annotations

import unittest
import shlex
import subprocess

from flyt_adapter.injection import ClientPackage, render_cloud_config


class InjectionTest(unittest.TestCase):
    def test_cloud_config_contains_verified_package_and_one_time_token(self) -> None:
        config = render_cloud_config(
            package=ClientPackage(
                url="https://packages.example.invalid/flyt-client.pkg",
                digest="sha256:" + "a" * 64,
                ca_certificate="-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n",
                resolve_address="192.0.2.10",
            ),
            bootstrap_token="one-time-token",
            instance_uuid="instance-1",
            generation=2,
            manager_endpoint="tcp://172.30.0.5:12402",
        )
        self.assertIn("sha256sum -c", config)
        self.assertIn("--cacert /run/flyt/package-ca.crt", config)
        self.assertIn("--retry-all-errors", config)
        self.assertIn("--resolve packages.example.invalid:443:192.0.2.10", config)
        self.assertIn("/run/install-flyt-client", config)
        self.assertIn("/bin/bash /run/install-flyt-client", config)
        self.assertIn("one-time-token", config)
        self.assertIn("instance-1", config)
        self.assertIn("FLYT_GENERATION=2", config)
        self.assertIn("tcp://172.30.0.5:12402", config)
        self.assertIn("rm -f", config)
        self.assertNotIn("Kubernetes", config)
        self.assertTrue(config.startswith("#!/bin/sh\n"))
        self.assertNotIn("runcmd", config)
        self.assertIn("FLYT_BOOTSTRAP_COMPLETE", config)
        command = shlex.split(config.splitlines()[1])
        self.assertEqual(["exec", "sh", "-ec"], command[:3])
        subprocess.run(["sh", "-n"], input=command[3], text=True, check=True)

    def test_package_requires_https_and_valid_digest(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            ClientPackage(url="http://example.invalid/client", digest="sha256:" + "a" * 64)
        with self.assertRaisesRegex(ValueError, "digest"):
            ClientPackage(url="https://example.invalid/client", digest="sha256:not-a-digest")
        with self.assertRaises(ValueError):
            ClientPackage(url="https://example.invalid/client", digest="sha256:" + "a" * 64,
                          resolve_address="not-an-ip")


if __name__ == "__main__":
    unittest.main()
