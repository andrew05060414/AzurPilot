"""ALAS/MAA 只读巡检：去重、合法等待抑制、卡死归类（PX-2039）。"""

import importlib.util
import json
import os
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

    def test_live_probe_decodes_chinese_and_uses_selected_instance(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / "dev_tools"
            tools.mkdir()
            (tools / "alas_maintenance_probe.py").write_text(
                'import json, sys\n'
                'print(json.dumps({"summary": {"消息": "告警", "argv": sys.argv[1:]}}, ensure_ascii=False))\n',
                encoding="utf-8",
            )
            with patch.object(patrol, "ALAS_ROOT", root), patch.dict(os.environ, {"PYTHONIOENCODING": "cp936"}):
                summary = patrol.run_probe("+08:00", "ap.test")
        self.assertEqual(summary["消息"], "告警")
        self.assertIn("--instance=ap.test", summary["argv"])

    def test_dry_run_does_not_write_explicit_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            prior = '{"open": {}}\n'
            state.write_text(prior, encoding="utf-8")
            args = ["--dry-run", "--state", str(state)]
            with patch.object(patrol, "LOG_PATH", root / "patrol.log"), \
                 patch.object(patrol, "ALAS_ROOT", root), patch.object(patrol, "MAA_ROOT", root), \
                 patch.object(patrol, "run_probe", return_value={"webui": {"available": False}}) as probe, \
                 patch.object(patrol, "post") as post:
                self.assertEqual(patrol.main(args), 0)
                probe.assert_called_once_with(patrol.local_offset(), "ap")
                self.assertTrue(post.call_args.args[1])
            self.assertEqual(state.read_text(encoding="utf-8"), prior)

    def test_instance_logs_include_both_dates_but_not_other_instances(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "log"
            log.mkdir()
            old_stuck = "\n".join(STUCK).replace("20:10:00", "23:50:00") + "\n"
            wait = "2026-11-23 23:55:00 | INFO | [AzurPilot] 等待直到 2026-11-24 02:00:00 执行任务 `Commission`\n"
            (log / "2026-11-23_ap.txt").write_text(old_stuck * 2 + wait, encoding="utf-8")
            current = log / "2026-11-24_ap.txt"
            now = datetime(2026, 11, 24, 0, 10)
            self.assertTrue(patrol.scheduler_is_sleeping(patrol.alas_log_lines(root, "ap", now, None), now))
            new_stuck = "\n".join(STUCK).replace("2026-11-23 20:10:00", "2026-11-24 00:05:00")
            current.write_text(new_stuck + "\n", encoding="utf-8")
            (log / "2026-11-24_alas.txt").write_text("other instance\n", encoding="utf-8")
            lines = patrol.alas_log_lines(root, "ap", now, None)
            self.assertEqual(len(patrol.stuck_findings(lines, now)), 1)
            self.assertNotIn("other instance", lines)
            self.assertEqual(patrol.alas_log_lines(root, "ap", now, current), new_stuck.splitlines())

    def test_degraded_alerts_report_ssh_or_missing_heartbeat_without_webui_duplicate(self):
        for reason in ("ssh_unreachable", "heartbeat_absent"):
            with self.subTest(reason=reason):
                summary = {"state": "degraded", "stalled_reasons": [reason]}
                key = "alas:ssh_unreachable" if reason == "ssh_unreachable" else "alas:stalled:heartbeat_absent"
                self.assertEqual([f["key"] for f in patrol.alas_findings(summary, [], NOW)],
                                 [key])
        summary = {"state": "degraded", "stalled_reasons": ["webui_unavailable"],
                   "webui": {"available": False}}
        self.assertEqual([f["key"] for f in patrol.alas_findings(summary, [], NOW)], ["alas:webui_down"])

    def test_expected_emulator_shutdown_preserves_other_alerts_until_wakeup(self):
        lines = [
            "2026-11-23 20:00:00 | INFO | [AzurPilot] 等待直到 2026-11-24 01:00:00 执行任务 `Commission`",
            "2026-11-23 20:00:01 | INFO | [AzurPilot] 等待期间已关闭模拟器",
        ]
        summary = {"state": "stalled", "emulator": {"online": False},
                   "stalled_reasons": ["heartbeat_expired", "emulator_offline", "ssh_unreachable"]}
        with patch.object(patrol, "log"):
            self.assertEqual([f["key"] for f in patrol.alas_findings(summary, lines, NOW)],
                             ["alas:ssh_unreachable"])
        lines.append("2026-11-24 00:50:00 | INFO | [AzurPilot] 长时间等待后启动模拟器")
        self.assertEqual([f["key"] for f in patrol.alas_findings(summary, lines, datetime(2026, 11, 24, 0, 51))],
                         ["alas:emulator_offline", "alas:ssh_unreachable", "alas:stalled:heartbeat_expired"])
        lines.append("2026-11-24 00:52:00 | INFO | [预热] 提前启动完成（模拟器/游戏 + 登录成功）")
        with patch.object(patrol, "log"):
            self.assertEqual([f["key"] for f in patrol.alas_findings(summary, lines, datetime(2026, 11, 24, 0, 58))],
                             ["alas:emulator_offline", "alas:ssh_unreachable"])
        lines.append("2026-11-24 01:00:00 | INFO | [AzurPilot] 调度器: 开始任务 `Commission`")
        self.assertEqual([f["key"] for f in patrol.alas_findings(summary, lines, datetime(2026, 11, 24, 1, 1))],
                         ["alas:emulator_offline", "alas:ssh_unreachable", "alas:stalled:heartbeat_expired"])

    def test_state_isolates_alas_instances_and_keeps_host_alerts_shared(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            args = ["--state", str(state), "--now", str(NOW)]
            webui_down = {"webui": {"available": False}}
            failed = {**webui_down, "state": "stalled", "stalled_reasons": ["task_error"]}
            maa = patrol.finding("maa:log_stale", "INFO", "t", "d", [])
            with patch.object(patrol, "LOG_PATH", root / "patrol.log"), \
                 patch.object(patrol, "ALAS_ROOT", root), patch.object(patrol, "post") as post, \
                 patch.object(patrol, "run_probe", side_effect=[failed, failed, webui_down, webui_down]), \
                 patch.object(patrol, "maa_findings", return_value=[maa]):
                self.assertEqual(patrol.main(args), 0)
                for _ in range(3):
                    self.assertEqual(patrol.main(args + ["--instance", "farm"]), 0)
            alerts = [call.args[0] for call in post.call_args_list]
            self.assertEqual([alert.get("instance") for alert in alerts], ["ap", None, None, "farm"])
            self.assertEqual([alert["key"] for alert in alerts].count("alas:webui_down"), 1)
            self.assertNotEqual(alerts[0]["fingerprint"], alerts[3]["fingerprint"])
            self.assertIn("ALAS 实例：farm", patrol.render(alerts[3]))
            data = json.loads(state.read_text(encoding="utf-8"))
            self.assertIn(maa["fingerprint"], data["open"])
            self.assertIn(alerts[0]["fingerprint"], data["instances"]["ap"]["open"])
            self.assertEqual(data["instances"]["farm"]["open"], {})

    def test_legacy_cooldown_is_preserved_only_for_confirmed_instance(self):
        key = "alas:stalled:task_error,webui_unavailable"
        record = {"key": key, "alerted_at": NOW.isoformat(), "clean_runs": 1}
        state = {"open": {patrol.fingerprint(key): record}}
        patrol.prepare_state(state, None)
        self.assertIn(patrol.fingerprint("alas:webui_down"), state["open"])
        self.assertIn(patrol.fingerprint("alas:stalled:task_error"), state["legacy_alas"])
        self.assertEqual(state["instances"], {})
        patrol.prepare_state(state, "alas")
        key = "alas:stalled:task_error"
        records = state["instances"]["alas"]["open"]
        fp = patrol.instance_fingerprint("alas", key)
        self.assertEqual(records[fp], {**record, "key": key})
        alert = {**patrol.finding(key, "WARNING", "t", "d", []), "fingerprint": fp}
        self.assertEqual(patrol.decide([alert], state["instances"]["alas"], NOW), [])
        self.assertEqual(state["legacy_alas"], {})


if __name__ == "__main__":
    unittest.main()
