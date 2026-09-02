from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from taskboard.config import CLOUD_MODE, LOCAL_MODE, ServerConfig


class ServerConfigTest(unittest.TestCase):
    def test_local_mode_remains_the_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfig.from_environment()
        self.assertEqual(LOCAL_MODE, config.mode)
        self.assertEqual("127.0.0.1", config.host)
        self.assertEqual(8765, config.port)

    def test_local_mode_refuses_public_binding(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            ServerConfig(mode=LOCAL_MODE, host="0.0.0.0").validate()

    def test_local_mode_preserves_hosts_with_and_without_port(self) -> None:
        config = ServerConfig().validate()
        self.assertIn("localhost", config.trusted_hosts(8765))
        self.assertIn("localhost:8765", config.trusted_hosts(8765))

    def test_cloud_mode_requires_public_url_and_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "DOTASKS_HTTP_USER"):
            ServerConfig(mode=CLOUD_MODE, host="0.0.0.0").validate()
        with self.assertRaisesRegex(ValueError, "DOTASKS_PUBLIC_URL"):
            ServerConfig(
                mode=CLOUD_MODE,
                host="0.0.0.0",
                http_user="dotasks",
                http_password="secret",
            ).validate()

    def test_cloud_mode_derives_public_trust_boundary(self) -> None:
        config = ServerConfig(
            mode=CLOUD_MODE,
            host="0.0.0.0",
            public_url="https://dotasks.example.com",
            http_user="dotasks",
            http_password="secret",
        ).validate()
        self.assertEqual({"dotasks.example.com"}, config.trusted_hosts(8765))
        self.assertEqual(
            {"https://dotasks.example.com"}, config.trusted_origins(8765)
        )


if __name__ == "__main__":
    unittest.main()
