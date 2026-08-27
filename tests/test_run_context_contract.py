from __future__ import annotations

import unittest

from core.run_context import compact_acceptance_commands


class RunContextContractTest(unittest.TestCase):
    def test_compaction_preserves_criterion_location_method_and_required(self):
        compacted = compact_acceptance_commands([
            {
                "criterion": "取消范围可选",
                "file": "web/Booking.tsx",
                "symbol": "CancelScope",
                "method": "manual browser verification",
                "expected": "显示三个范围选项",
                "check_type": "manual_runtime",
                "required": True,
                "artifact_refs": ["artifact://intake/LOC-1/reference.png"],
            }
        ])

        self.assertEqual(1, len(compacted))
        criterion = compacted[0]["criteria"][0]
        self.assertEqual("web/Booking.tsx", criterion["file"])
        self.assertEqual("CancelScope", criterion["symbol"])
        self.assertEqual("manual browser verification", criterion["method"])
        self.assertTrue(criterion["required"])
        self.assertEqual(
            ["artifact://intake/LOC-1/reference.png"], criterion["artifact_refs"]
        )


if __name__ == "__main__":
    unittest.main()
