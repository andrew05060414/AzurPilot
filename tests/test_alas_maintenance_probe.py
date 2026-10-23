"""ALAS 维护巡检边界、日志适配与 CLI 回放测试。"""

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from dev_tools.alas_maintenance_probe import evaluate, read_log_tail


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
                       "--instance", "test", "--timezone-offset", "-04:00"]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("private-token-value", result.stdout)
        self.assertEqual([item["state"] for item in json.loads(result.stdout)], ["retry", "stalled"])


if __name__ == "__main__":
    unittest.main()
