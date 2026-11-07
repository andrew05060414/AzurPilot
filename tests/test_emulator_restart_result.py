"""模拟器启停失败不能被调度器当成成功并清空恢复进度。"""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from alas import AzurLaneAutoScript


class TestEmulatorRestartResult(unittest.TestCase):
    def setUp(self):
        self.log = self.enterContext(patch('alas.logger'))
        self.enterContext(patch('alas.time.sleep'))
        self.script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
        self.script.consecutive_adb_offline = 0
        self.script.config = SimpleNamespace(
            Error_AdbOfflineThreshold=3,
            EmulatorManagement_DeepRestartAfterFailures=0,
        )
        self.device = SimpleNamespace(
            emulator_stop=Mock(return_value=True),
            emulator_start=Mock(return_value=True),
        )
        self.script.__dict__['device'] = self.device

    def test_failed_start_preserves_counter_and_device_for_next_round(self):
        self.device.emulator_start.return_value = False

        self.assertFalse(self.script._try_restart_emulator())

        self.assertEqual(self.script.consecutive_adb_offline, 1)
        self.assertIs(self.script.__dict__['device'], self.device)
        self.assertNotIn(
            '[AzurPilot] 模拟器重启完成',
            [args[0] for args, kwargs in self.log.info.call_args_list],
        )

    def test_failed_stop_does_not_start_or_clear_recovery_progress(self):
        self.script.consecutive_adb_offline = 1
        self.device.emulator_stop.return_value = False

        self.assertFalse(self.script._try_restart_emulator())

        self.device.emulator_start.assert_not_called()
        self.assertEqual(self.script.consecutive_adb_offline, 2)
        self.assertIs(self.script.__dict__['device'], self.device)

    def test_repeated_failed_starts_advance_wait_ladder_until_confirmed_success(self):
        self.device.emulator_start.side_effect = [False, False, True]
        self.script.config.EmulatorManagement_DeepRestartAfterFailures = 3

        self.assertFalse(self.script._try_restart_emulator())
        self.assertFalse(self.script._try_restart_emulator())
        self.assertTrue(self.script._try_restart_emulator())

        calls = self.device.emulator_start.call_args_list
        self.assertEqual([call.kwargs['failures'] for call in calls], [0, 1, 2])
        self.assertEqual([call.kwargs['deep'] for call in calls], [False, False, True])
        self.assertEqual(self.script.consecutive_adb_offline, 0)
        self.assertNotIn('device', self.script.__dict__)

    def test_legacy_platform_without_boolean_result_keeps_success_semantics(self):
        self.device.emulator_stop.return_value = None
        self.device.emulator_start.return_value = None

        self.assertTrue(self.script._try_restart_emulator())
        self.assertEqual(self.script.consecutive_adb_offline, 0)

    def test_already_online_emulator_is_not_stopped(self):
        """等待期间用户手动启动了模拟器：不能再把它关掉。"""
        self.script.consecutive_adb_offline = 5
        self.script._emulator_responsive = Mock(return_value=True)

        self.assertTrue(self.script._try_restart_emulator())

        self.device.emulator_stop.assert_not_called()
        self.device.emulator_start.assert_not_called()
        self.assertEqual(self.script.consecutive_adb_offline, 0)
        self.assertNotIn('device', self.script.__dict__)

    def test_unresponsive_emulator_still_goes_through_restart(self):
        self.script._emulator_responsive = Mock(return_value=False)

        self.assertTrue(self.script._try_restart_emulator())

        self.device.emulator_stop.assert_called_once()
        self.device.emulator_start.assert_called_once()

    def test_responsive_check_treats_errors_as_offline(self):
        self.script.config.Emulator_Serial = '127.0.0.1:16448'
        with patch('adbutils.AdbClient', side_effect=OSError('refused')):
            self.assertFalse(self.script._emulator_responsive())
        self.script.config.Emulator_Serial = 'auto'
        self.assertFalse(self.script._emulator_responsive())

    def test_online_skip_happens_only_once_per_outage(self):
        """ADB 能应答但实例有病（如截图持续超时）：第二次必须真正重启，不能无限跳过。"""
        self.script._emulator_responsive = Mock(return_value=True)

        self.assertTrue(self.script._try_restart_emulator())
        self.device.emulator_stop.assert_not_called()

        self.script.__dict__['device'] = self.device
        self.assertTrue(self.script._try_restart_emulator())
        self.device.emulator_stop.assert_called_once()
        self.device.emulator_start.assert_called_once()
        # 真正重启成功后，下一次故障又允许先检查在线
        self.assertFalse(self.script.online_restart_skipped)


if __name__ == '__main__':
    unittest.main()
