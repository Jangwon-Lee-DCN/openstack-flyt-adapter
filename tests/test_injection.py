from __future__ import annotations

import unittest

from flyt_adapter.injection import ClientPackage, render_cloud_config


class InjectionTest(unittest.TestCase):
    def test_cloud_config_contains_verified_package_and_one_time_token(self) -> None:
        config = render_cloud_config(
            package=ClientPackage(
                url="https://packages.example.invalid/flyt-client.pkg",
                digest="sha256:" + "a" * 64,
            ),
            bootstrap_token="one-time-token",
            instance_uuid="instance-1",
            generation=2,
            manager_endpoint="tcp://172.30.0.5:12402",
        )
        self.assertIn("sha256sum -c", config)
        self.assertIn("one-time-token", config)
        self.assertIn("instance-1", config)
        self.assertIn("FLYT_GENERATION=2", config)
        self.assertIn("tcp://172.30.0.5:12402", config)
        self.assertIn("rm -f", config)
        self.assertNotIn("Kubernetes", config)

    def test_package_requires_https_and_valid_digest(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            ClientPackage(url="http://example.invalid/client", digest="sha256:" + "a" * 64)
        with self.assertRaisesRegex(ValueError, "digest"):
            ClientPackage(url="https://example.invalid/client", digest="sha256:not-a-digest")


if __name__ == "__main__":
    unittest.main()
