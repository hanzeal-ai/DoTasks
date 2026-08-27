from __future__ import annotations

import unittest

from taskboard.app_server import app_server_initialize_params
from taskboard.mcp_server import handle
from taskboard.server import VERSION as HTTP_VERSION
from taskboard.version import VERSION


class VersionTest(unittest.TestCase):
    def test_all_protocol_surfaces_share_the_package_version(self):
        initialized = handle({"id": 1, "method": "initialize", "params": {}})

        self.assertEqual(VERSION, HTTP_VERSION)
        self.assertEqual(VERSION, app_server_initialize_params()["clientInfo"]["version"])
        self.assertEqual(VERSION, initialized["result"]["serverInfo"]["version"])


if __name__ == "__main__":
    unittest.main()
