"""ALAS 维护巡检边界、日志适配与实况/告警测试。"""

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from dev_tools.alas_maintenance_probe import (
    evaluate,
    read_log_tail,
    probe_webui,
    probe_adb_emulator,
    probe_ssh_tunnel,
    probe_heartbeat_and_tasks,
    read_live_summary,
    generate_actionable_alert,
    format_actionable_alert_markdown,
    AutoRecoveryConfig,
    AutoRecoveryManager,
)


BASE = "2026-10-23T10:00:00-04:00"


def sample(minutes=0, **values):
    timestamp = datetime.fromisoformat(BASE) + timedelta(minutes=minutes)
    return {"timestamp": timestamp.isoformat(), "instance": "test", **values}


class MaintenanceProbeTests(unittest.TestCase):
    def test_explicit_cancel_closes_episode_and_clears_baseline(self):
        for status in ("stopped", "updating"):
            with self.subTest(status=status):
                result = evaluate([
                    sample(status="running", ordinary_task_success=True,
                           success_task="Commission", success_id="base"),
                    sample(status="error", error="boom"),
                    sample(6, status="error", error="boom"),
                    sample(7, status=status, desired_running=False),
                    sample(8, status="running", ordinary_task_success=True,
                           success_task="Commission", success_id="after-cancel"),
                    sample(1440, status="error", error="boom"),
                    sample(1446, status="error", error="boom"),
                ])
                self.assertEqual(result[3]["state"], "cancelled")
                self.assertEqual(result[4]["state"], "unknown")
                self.assertEqual(result[5]["duration_seconds"], 0)
                self.assertFalse(result[5]["report"])
                self.assertTrue(result[6]["report"])

    def test_running_error_overrides_success_and_keeps_baseline(self):
        result = evaluate([
            sample(status="running", ordinary_task_success=True,
                   success_task="Commission", success_id="base"),
            sample(1, status="error", error="boom"),
            sample(6, status="error", error="boom"),
            sample(7, status="running", error="StillCrashing", ordinary_task_success=True,
                   success_task="Commission", success_id="new"),
            sample(8, status="running", ordinary_task_success=True,
                   success_task="Commission", success_id="new"),
        ])
        self.assertEqual(result[3]["state"], "stalled")
        self.assertFalse(result[3]["report"])
        self.assertEqual(result[4]["state"], "recovered")

    def test_unknown_or_missing_intent_keeps_episode_and_baseline(self):
        for interruption in ({"status": "mystery", "desired_running": False},
                             {"status": "stopped"}, {"status": "updating"}):
            with self.subTest(interruption=interruption):
                result = evaluate([
                    sample(status="running", ordinary_task_success=True,
                           success_task="Commission", success_id="base"),
                    sample(status="error", error="boom"),
                    sample(6, status="error", error="boom"),
                    sample(7, **interruption),
                    sample(1440, status="error", error="boom"),
                    sample(1441, status="running", ordinary_task_success=True,
                           success_task="Commission", success_id="new"),
                ])
                self.assertEqual(result[4]["duration_seconds"], 86400)
                self.assertFalse(result[4]["report"])
                self.assertEqual(result[5]["state"], "recovered")

    def test_episode_fingerprint_is_fixed_through_error_change_and_recovery(self):
        result = evaluate([
            sample(status="running", ordinary_task_success=True,
                   success_task="Commission", success_id="base"),
            sample(1, status="error", error="error A"),
            sample(6, status="error", error="error B"),
            sample(7, status="running", ordinary_task_success=True,
                   success_task="Commission", success_id="new"),
        ])
        self.assertTrue(result[2]["report"])
        self.assertEqual(result[1]["fingerprint"], result[2]["fingerprint"])
        self.assertEqual(result[2]["fingerprint"], result[3]["fingerprint"])

    def test_no_expected_progress_means_unknown_even_after_long_running(self):
        result = evaluate([sample(status="running", current_task="Sortie"),
                           sample(600, status="running", current_task="Sortie")])
        self.assertEqual([item["state"] for item in result], ["unknown", "unknown"])

    def test_expected_progress_retries_then_stalls_once(self):
        result = evaluate([sample(status="running", expected_progress=True, current_task="Sortie"),
                           sample(1, status="running", expected_progress=True, current_task="Sortie"),
                           sample(5, status="running", expected_progress=True, current_task="Sortie"),
                           sample(6, status="running", expected_progress=True, current_task="Sortie")])
        self.assertEqual([item["state"] for item in result], ["retry", "retry", "stalled", "stalled"])
        self.assertEqual([item.get("report", False) for item in result], [False, False, True, False])

    def test_first_success_id_is_only_baseline_and_new_success_recovers(self):
        result = evaluate([sample(status="running", ordinary_task_success=True, success_task="Commission", success_id="task-1"),
                           sample(1, status="error", error="network timeout"),
                           sample(6, status="error", error="network timeout"),
                           sample(7, status="running", ordinary_task_success=True,
                                  success_task="Commission", success_id="task-2")])
        self.assertEqual([item["state"] for item in result], ["unknown", "retry", "stalled", "recovered"])
        self.assertEqual(result[-1]["evidence"], "new_ordinary_task_success")

    def test_error_success_fields_do_not_recover_or_advance_baseline(self):
        result = evaluate([sample(status="running", ordinary_task_success=True,
                                  success_task="Commission", success_id="base"),
                           sample(1, status="error", error="boom", ordinary_task_success=True, success_id="fake"),
                           sample(2, status="running", ordinary_task_success=True,
                                  success_task="Commission", success_id="fake")])
        self.assertEqual(result[-1]["state"], "recovered")
        self.assertEqual(result[-1]["evidence"], "new_ordinary_task_success")

    def test_recovered_episode_resets_deduplication_and_timer(self):
        result = evaluate([sample(status="running", ordinary_task_success=True,
                                  success_task="Commission", success_id="1"),
                           sample(1, status="error", error="boom"),
                           sample(6, status="error", error="boom"),
                           sample(7, status="running", ordinary_task_success=True,
                                  success_task="Commission", success_id="2"),
                           sample(8, status="error", error="boom"),
                           sample(12, status="error", error="boom"),
                           sample(13, status="error", error="boom")])
        reports = [item for item in result if item.get("report")]
        self.assertEqual(len(reports), 2)
        self.assertEqual(reports[0]["fingerprint"], reports[1]["fingerprint"])
        self.assertEqual(reports[0]["duration_seconds"], 300)
        self.assertEqual(reports[1]["duration_seconds"], 300)

    def test_error_text_changes_and_short_unknown_keep_episode_timer(self):
        result = evaluate([sample(status="error", error="error A"),
                           sample(2, status="error", error="error B"),
                           sample(3, status="mystery"),
                           sample(5, status="error", error="error A"),
                           sample(6, status="error", error="error B")])
        reports = [item for item in result if item.get("report")]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["duration_seconds"], 300)
        self.assertEqual([item["state"] for item in result],
                         ["retry", "retry", "unknown", "stalled", "stalled"])

    def test_restart_or_unknown_status_success_claim_cannot_recover(self):
        result = evaluate([sample(status="running", ordinary_task_success=True,
                                  success_task="Commission", success_id="base"),
                           sample(1, status="error", error="boom"),
                           sample(6, status="error", error="boom"),
                           sample(7, status="running", current_task="Restart", ordinary_task_success=True,
                                  success_task="Restart", success_id="restart"),
                           sample(8, status="mystery", ordinary_task_success=True,
                                  success_task="Commission", success_id="fake"),
                           sample(9, status="running", ordinary_task_success=True,
                                  success_task="Commission", success_id="restart")])
        self.assertEqual([item["state"] for item in result],
                         ["unknown", "retry", "stalled", "unknown", "unknown", "recovered"])

    def test_empty_success_task_cannot_recover(self):
        result = evaluate([sample(status="running", ordinary_task_success=True,
                                  success_task="Commission", success_id="base"),
                           sample(1, status="error", error="boom"),
                           sample(6, status="error", error="boom"),
                           sample(7, status="running", ordinary_task_success=True,
                                  success_task="  ", success_id="fake")])
        self.assertEqual(result[-1]["state"], "unknown")

    def test_stopped_and_updating_are_normal_unless_desired_running(self):
        result = evaluate([sample(status="stopped"), sample(1, status="updating"),
                           sample(2, status="stopped", desired_running=True),
                           sample(8, status="stopped", desired_running=True)])
        self.assertEqual([item["state"] for item in result], ["normal", "normal", "retry", "stalled"])

    def test_malformed_fields_and_missing_instance_are_unknown(self):
        malformed = []
        for field in ("status", "error", "current_task"):
            for value in (["bad"], {"value": "bad"}):
                malformed.append(sample(**{field: value}))
        malformed.extend([{"timestamp": BASE, "status": "error", "error": "boom"},
                          sample(1, status="running")])
        result = evaluate(malformed)
        self.assertEqual([item["state"] for item in result], ["unknown"] * len(malformed))
        self.assertTrue(all(item["reason"] == "invalid_field_type" for item in result[:6]))
        self.assertEqual(result[6]["reason"], "missing_instance")

    def test_timestamps_are_ordered_per_instance(self):
        result = evaluate([sample(10, instance="a", status="running"),
                           sample(5, instance="b", status="running"),
                           sample(9, instance="a", status="running"),
                           sample(6, instance="b", status="running")])
        self.assertEqual([item["state"] for item in result], ["unknown", "unknown", "unknown", "unknown"])
        self.assertEqual(result[2]["reason"], "invalid_or_out_of_order_timestamp")
        self.assertNotEqual(result[3]["reason"], "invalid_or_out_of_order_timestamp")

    def test_invalid_or_naive_time_is_unknown(self):
        result = evaluate([sample(timestamp="bad", status="running"),
                           {"timestamp": "2026-10-23T10:00:00", "instance": "test", "status": "running"}])
        self.assertEqual([item["state"] for item in result], ["unknown", "unknown"])

    def test_log_adapter_replays_error_episode_without_echoing_log_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alas.log"
            path.write_text(
                "2026-10-23 10:00:00.000 | ERROR | private token must not print\n"
                "2026-10-23 10:00:01.000 | INFO | 调度器: 结束任务 `Commission`\n"
                "2026-10-23 10:05:00.000 | ERROR | private token must not print\n",
                encoding="utf-8")
            samples = read_log_tail(path, "test", timezone_offset="-04:00")
            self.assertEqual(len(samples), 2)
            self.assertEqual(samples[0]["status"], "error")
            result = evaluate(samples)
            self.assertEqual([item["state"] for item in result], ["retry", "stalled"])
            self.assertTrue(result[-1]["report"])
            self.assertNotIn("private token", json.dumps(result))

    def test_cli_reads_local_log_and_never_prints_log_message(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alas.log"
            path.write_text("2026-10-23 10:00:00.000 | ERROR | private-token-value\n"
                            "2026-10-23 10:05:00.000 | ERROR | private-token-value\n", encoding="utf-8")
            command = [sys.executable, "-m", "dev_tools.alas_maintenance_probe", "--log-path", str(path),
                       "--instance", "test", "--timezone-offset=-04:00"]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("private-token-value", result.stdout)
        self.assertEqual([item["state"] for item in json.loads(result.stdout)], ["retry", "stalled"])


class MaintenanceLiveProbeTests(unittest.TestCase):
    """实况只读状态巡检、可行动告警与自愈控制单测。"""

    @patch("urllib.request.urlopen")
    def test_probe_webui_ok_and_failure(self, mock_urlopen):
        # 1. 模拟 200 OK
        mock_resp = MagicMock()
        mock_resp.getcode.return_value = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp
        res_ok = probe_webui("http://127.0.0.1:25548")
        self.assertTrue(res_ok["available"])
        self.assertEqual(res_ok["status_code"], 200)

        # 2. 模拟网络异常
        mock_urlopen.side_effect = OSError("Connection refused")
        res_fail = probe_webui("http://127.0.0.1:25548")
        self.assertFalse(res_fail["available"])
        self.assertIsNone(res_fail["status_code"])
        self.assertIn("OSError", res_fail["detail"])

    @patch("subprocess.run")
    def test_probe_adb_emulator(self, mock_run):
        # 1. 模拟在线设备
        mock_proc = MagicMock()
        mock_proc.stdout = "List of devices attached\n127.0.0.1:16448 device product:PD2366 model:V2366GA\n"
        mock_run.return_value = mock_proc
        res = probe_adb_emulator(serial="auto")
        self.assertTrue(res["online"])
        self.assertEqual(res["serial"], "127.0.0.1:16448")
        self.assertEqual(res["device_state"], "device")

        # 2. 模拟无设备在线
        mock_proc.stdout = "List of devices attached\n"
        res_empty = probe_adb_emulator(serial="auto")
        self.assertFalse(res_empty["online"])
        self.assertEqual(res_empty["device_state"], "no_devices")

    def test_probe_ssh_tunnel(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "deploy.yaml"
            # 1. 禁用 SSH
            p.write_text("RemoteAccess:\n  EnableRemoteAccess: false\n", encoding="utf-8")
            res_off = probe_ssh_tunnel(p)
            self.assertFalse(res_off["enabled"])
            self.assertIsNone(res_off["reachable"])

            # 2. 启用 SSH 但服务器无法连接
            p.write_text("RemoteAccess:\n  EnableRemoteAccess: true\n  SSHServer: 127.0.0.1:59999\n", encoding="utf-8")
            with patch("socket.create_connection", side_effect=OSError("refused")):
                res_on = probe_ssh_tunnel(p, timeout=0.1)
                self.assertTrue(res_on["enabled"])
                self.assertFalse(res_on["reachable"])

    def test_probe_heartbeat_and_tasks(self):
        with tempfile.TemporaryDirectory() as d:
            log_file = Path(d) / "alas.txt"
            log_content = (
                "2026-10-24 10:00:00.000 | INFO | 调度器: 开始任务 `Commission`\n"
                "2026-10-24 10:01:00.000 | INFO | [登录] 登录成功\n"
                "2026-10-24 10:02:00.000 | ERROR | [错误] 游戏状态无法推进\n"
            )
            log_file.write_text(log_content, encoding="utf-8")
            hb = probe_heartbeat_and_tasks(log_file, instance="alas", timezone_offset="+00:00")
            self.assertEqual(hb["current_task"], "Commission")
            self.assertEqual(hb["latest_task_status"], "error")
            self.assertIn("游戏状态无法推进", hb["latest_error"])
            self.assertIsNotNone(hb["heartbeat_time"])

    def test_generate_actionable_alert_structure_and_fingerprint(self):
        stalled_summary = {
            "instance": "alas",
            "state": "stalled",
            "fingerprint": "abcd1234abcd1234",
            "stalled": True,
            "stalled_reasons": ["emulator_offline", "task_error"],
            "webui": {"available": False, "url": "http://127.0.0.1:25548"},
            "emulator": {"online": False, "device_state": "offline", "serial": "127.0.0.1:16448"},
            "heartbeat": {"current_task": "Commission", "latest_error": "游戏状态无法推进", "elapsed_seconds": 360},
        }
        alert = generate_actionable_alert(stalled_summary, threshold_seconds=300)
        self.assertIsNotNone(alert)
        self.assertEqual(alert["severity"], "CRITICAL")
        self.assertEqual(alert["fingerprint"], "abcd1234abcd1234")
        self.assertIn("模拟器连接中断/掉线", alert["phenomenon"])
        self.assertIn("任务卡死/执行异常", alert["phenomenon"])
        self.assertTrue(len(alert["recommended_human_actions"]) >= 2)
        self.assertEqual(alert["recovery_status"], "dry_run_disabled")
        self.assertIn("默认关闭状态", alert["recovery_message"])

        # Markdown 格式化检查
        md = format_actionable_alert_markdown(alert)
        self.assertIn("### ⚠️ [CRITICAL 告警]", md)
        self.assertIn("建议人工动作", md)
        self.assertIn("auto_recovery_enabled=False", md)

    def test_auto_recovery_manager_safety_switch(self):
        # 1. 默认关闭
        cfg_default = AutoRecoveryConfig()
        self.assertFalse(cfg_default.auto_recovery_enabled)

        mgr = AutoRecoveryManager(cfg_default)
        plan = {"action": "restart_adb", "instance": "alas"}
        # dry-run
        res_dry = mgr.execute_recovery(plan, dry_run=True)
        self.assertFalse(res_dry["executed"])
        self.assertTrue(res_dry["dry_run"])
        self.assertEqual(res_dry["status"], "dry_run_disabled")

        # 即使 dry_run=False，只要 auto_recovery_enabled=False 依然拒绝执行
        res_off = mgr.execute_recovery(plan, dry_run=False)
        self.assertFalse(res_off["executed"])
        self.assertEqual(res_off["status"], "dry_run_disabled")

    @patch("dev_tools.alas_maintenance_probe.probe_ssh_tunnel")
    @patch("dev_tools.alas_maintenance_probe.probe_adb_emulator")
    @patch("dev_tools.alas_maintenance_probe.probe_webui")
    def test_read_live_summary_synthesizes_state(self, mock_webui, mock_adb, mock_ssh):
        mock_webui.return_value = {"available": True, "url": "http://127.0.0.1:25548/healthz", "status_code": 200}
        mock_adb.return_value = {"online": True, "serial": "127.0.0.1:16448", "device_state": "device"}
        mock_ssh.return_value = {"enabled": True, "server": "remote.nanoda.work:1022", "reachable": True}
        with tempfile.TemporaryDirectory() as d:
            summary = read_live_summary(alas_root=d, instance="test", check_ssh=True)
            self.assertEqual(summary["instance"], "test")
            self.assertEqual(summary["state"], "healthy")
            self.assertFalse(summary["stalled"])
            self.assertEqual(summary["webui"]["status_code"], 200)

    def test_auto_recovery_cooldown_and_max_attempts(self):
        cfg_enabled = AutoRecoveryConfig(auto_recovery_enabled=True, max_attempts=2, cooldown_seconds=60)
        mgr = AutoRecoveryManager(cfg_enabled)
        plan = {"action": "restart_adb", "instance": "alas"}

        # 第 1 次执行
        res1 = mgr.execute_recovery(plan, dry_run=False)
        self.assertTrue(res1["executed"])
        self.assertEqual(res1["attempt"], 1)

        # 立即第 2 次执行（处于冷却中）
        res2 = mgr.execute_recovery(plan, dry_run=False)
        self.assertFalse(res2["executed"])
        self.assertEqual(res2["status"], "in_cooldown")

        # 模拟冷却时间过去
        mgr.last_action_time["alas"] = datetime.now(timezone.utc) - timedelta(seconds=70)
        res3 = mgr.execute_recovery(plan, dry_run=False)
        self.assertTrue(res3["executed"])
        self.assertEqual(res3["attempt"], 2)

        # 超过最大尝试次数 (2次)
        mgr.last_action_time["alas"] = datetime.now(timezone.utc) - timedelta(seconds=70)
        res4 = mgr.execute_recovery(plan, dry_run=False)
        self.assertFalse(res4["executed"])
        self.assertEqual(res4["status"], "max_attempts_exceeded")


if __name__ == "__main__":
    unittest.main()
