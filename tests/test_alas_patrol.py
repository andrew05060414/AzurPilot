"""ALAS/MAA 只读巡检：去重、合法等待抑制、卡死归类（PX-2039）。"""

import importlib.util
import unittest
from datetime import datetime
from pathlib import Path

_spec = importlib.util.spec_from_file_location("alas_patrol", Path(__file__).resolve().parents[1] / "dev_tools" / "alas_patrol.py")
patrol = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(patrol)

NOW = datetime(2026, 11, 23, 20, 15, 0)
STUCK = ["2026-11-23 20:10:00.000 | ERROR | [错误] 游戏状态无法推进", "异常：GameStuckError: Scroll drag page error."]


class PatrolTest(unittest.TestCase):
    def test_sleeping_scheduler_is_not_stalled(self):
        lines = ["2026-11-23 19:42:56.278 | INFO | [AzurPilot] 等待直到 2026-11-23 20:12:27 执行任务 `Commission`"]
        self.assertTrue(patrol.scheduler_is_sleeping(lines, datetime(2026, 11, 23, 20, 14)))
        self.assertFalse(patrol.scheduler_is_sleeping(lines, datetime(2026, 11, 23, 20, 40)))

    def test_repeated_stuck_grouped_and_threshold(self):
        self.assertEqual(patrol.stuck_findings(STUCK * 2, NOW), [])
        f = patrol.stuck_findings(STUCK * 3, NOW)
        self.assertEqual(len(f), 1)
        self.assertIn("Scroll drag page error", f[0]["title"])

    def test_old_errors_outside_window_ignored(self):
        old = ["2026-11-23 10:10:00.000 | ERROR | [错误] 游戏状态无法推进", "异常：GameStuckError: x"]
        self.assertEqual(patrol.stuck_findings(old * 5, NOW), [])

    def test_dedup_then_recover_then_realert(self):
        f = patrol.finding("alas:test", "WARNING", "t", "d", ["a"])
        state = {"open": {}}
        self.assertEqual(len(patrol.decide([f], state, NOW)), 1)
        self.assertEqual(len(patrol.decide([f], state, datetime(2026, 11, 23, 20, 25))), 0)
        patrol.decide([], state, datetime(2026, 11, 23, 20, 35))
        patrol.decide([], state, datetime(2026, 11, 23, 20, 45))
        self.assertEqual(state["open"], {})
        self.assertEqual(len(patrol.decide([f], state, datetime(2026, 11, 23, 20, 55))), 1)

    def test_secrets_redacted(self):
        self.assertNotIn("abc123", patrol.sanitize("token=abc123 ok"))


if __name__ == "__main__":
    unittest.main()
