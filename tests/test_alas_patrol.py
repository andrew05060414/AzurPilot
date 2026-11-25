"""ALAS/MAA 只读巡检：去重、合法等待抑制、卡死归类（PX-2039）。"""

import importlib.util
import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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

    def test_log_payloads_are_not_exported(self):
        headers = STUCK[0]
        lines = [headers, '异常：GameStuckError: Authorization: Bearer test-only-credential'] * 3
        findings = patrol.stuck_findings(lines, NOW)
        self.assertEqual(len(findings), 1)
        self.assertNotIn("test-only-credential", json.dumps(findings))
        self.assertNotIn("test-only-credential", patrol.sanitize(lines[1]))
        with TemporaryDirectory() as directory:
            maa_log = Path(directory) / "asst.log"
            maa_log.write_text("[2026-11-23 20:10:00] [ERR] arbitrary test-only-payload",
                               encoding="utf-8")
            with patch.object(patrol, "maa_process_running", return_value=False):
                findings = patrol.maa_findings(maa_log, NOW)
        self.assertEqual(len(findings), 1)
        self.assertNotIn("test-only-payload", json.dumps(findings))

    def test_malformed_log_timestamp_does_not_hide_valid_findings(self):
        self.assertEqual(len(patrol.stuck_findings(["2026-99-23 20:10:00.000 | ERROR"] + STUCK * 3, NOW)), 1)

    def test_partial_delivery_keeps_only_successful_alerts_in_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "fixture.json"
            fixture.write_text(json.dumps({"alas_summary": {
                "webui": {"available": False}, "emulator": {"online": False},
            }}), encoding="utf-8")
            state = root / "custom" / "state.json"
            args = ["--fixture", str(fixture), "--state", str(state), "--alas-log", str(root / "alas.txt"),
                    "--maa-log", str(root / "asst.log"), "--now", str(NOW)]
            with patch.object(patrol, "LOG_PATH", root / "patrol.log"):
                with patch.object(patrol, "post", side_effect=[None, patrol.PatrolCommandError("failed")]) as post:
                    self.assertEqual(patrol.main(args), 1)
                    self.assertEqual(post.call_args.args[2], state.parent)
                records = json.loads(state.read_text(encoding="utf-8"))["open"].values()
                self.assertEqual([record["key"] for record in records], ["alas:webui_down"])
                with patch.object(patrol, "post") as post:
                    self.assertEqual(patrol.main(args), 0)
                    post.assert_called_once()
                    self.assertEqual(post.call_args.args[0]["key"], "alas:emulator_offline")


if __name__ == "__main__":
    unittest.main()
