from __future__ import annotations

import unittest

from core.self_healing import (
    classify_recoverable_failure,
)


class SelfHealingTest(unittest.TestCase):
    def test_failure_classifier_separates_project_environment_and_implementation(self):
        self.assertEqual("project", classify_recoverable_failure("缺少测试用例"))
        self.assertEqual("project", classify_recoverable_failure("npm ERR! Missing script: test"))
        self.assertEqual("environment", classify_recoverable_failure("ModuleNotFoundError: No module named 'httpx'"))
        self.assertEqual("implementation", classify_recoverable_failure("AssertionError: expected 2, got 3"))


if __name__ == "__main__":
    unittest.main()
