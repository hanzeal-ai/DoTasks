from __future__ import annotations

import unittest
from pathlib import Path

from core.service import TaskboardService
from core.service.changes import TaskChangeMixin
from core.service.execution import TaskLifecycleMixin
from core.service.planning import TaskPlanningMixin
from core.service.queries import TaskQueryMixin
from core.service.requirements import TaskRequirementMixin
from core.service.reporting import TaskReportingMixin
from core.service.review import TaskReviewMixin
from core.service.runs import TaskRunMixin
from core.service.settings import TaskSettingsMixin


class TaskboardServiceStructureTest(unittest.TestCase):
    def test_core_and_adapter_layers_are_separate_packages(self):
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / "core" / "service" / "native_dispatch.py").is_file())
        self.assertTrue((root / "core" / "service" / "__init__.py").is_file())
        self.assertFalse((root / "core" / "service.py").exists())
        self.assertFalse(any((root / "core").glob("service_*.py")))
        self.assertFalse((root / "taskboard" / "dispatcher.py").exists())
        self.assertFalse((root / "taskboard" / "service.py").exists())

    def test_facade_delegates_each_domain_to_its_mixin(self):
        expected_owners = {
            "create_task": TaskPlanningMixin,
            "detect_task_change": TaskChangeMixin,
            "list_tasks": TaskQueryMixin,
            "transition_task": TaskLifecycleMixin,
            "report_run_blocked": TaskLifecycleMixin,
            "get_requirement": TaskRequirementMixin,
            "submit_requirement_decomposition": TaskRequirementMixin,
            "get_run": TaskRunMixin,
            "submit_delivery": TaskReviewMixin,
            "review_code": TaskReviewMixin,
            "board": TaskReportingMixin,
            "task_settings": TaskSettingsMixin,
        }

        for method_name, owner in expected_owners.items():
            with self.subTest(method=method_name):
                self.assertIs(getattr(TaskboardService, method_name), getattr(owner, method_name))


if __name__ == "__main__":
    unittest.main()
