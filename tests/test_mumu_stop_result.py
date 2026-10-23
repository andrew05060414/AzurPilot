"""验证 MuMu12 模拟器停止命令结果校验与失败处理。"""

from subprocess import CompletedProcess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from module.device.env import IS_WINDOWS

if IS_WINDOWS:
    from module.device.platform.emulator_windows import EmulatorInstance
    from module.device.platform.platform_windows import PlatformWindows


@unittest.skipUnless(IS_WINDOWS, 'Windows 平台模拟器停止结果校验测试')
class TestMuMuStopResult(unittest.TestCase):
    def setUp(self):
        self.platform = PlatformWindows.__new__(PlatformWindows)
        self.platform.config = SimpleNamespace(EmulatorInfo_Emulator='MuMuPlayer12')
        self.instance = EmulatorInstance.__new__(EmulatorInstance)
        self.instance.type = 'MuMuPlayer12'
        self.instance.name = 'MuMuPlayer12_0'
        self.instance.MuMuPlayer12_id = 0
        self.instance.emulator = SimpleNamespace(path='F:/MuMu/shell/MuMuPlayer.exe')
        self.platform.emulator_instance = self.instance

    def test_mumu12_stop_success_when_returncode_zero(self):
        self.platform.execute = Mock(return_value=CompletedProcess(args='shutdown', returncode=0))
        res = self.platform._emulator_function_wrapper(self.platform._emulator_stop)
        self.assertTrue(res)

    def test_mumu12_stop_fails_when_timeout_none(self):
        self.platform.execute = Mock(return_value=None)
        res = self.platform._emulator_function_wrapper(self.platform._emulator_stop)
        self.assertFalse(res)

    def test_mumu12_stop_fails_when_returncode_nonzero(self):
        self.platform.execute = Mock(return_value=CompletedProcess(args='shutdown', returncode=1))
        res = self.platform._emulator_function_wrapper(self.platform._emulator_stop)
        self.assertFalse(res)

    def test_emulator_stop_retries_stop_only_and_never_launches(self):
        self.platform.execute = Mock(return_value=CompletedProcess(args='shutdown', returncode=1))
        self.platform._emulator_start = Mock()

        res = self.platform.emulator_stop()

        self.assertFalse(res)
        self.assertEqual(self.platform.execute.call_count, 3)
        self.platform._emulator_start.assert_not_called()

    def test_emulator_stop_recovers_on_second_attempt_without_launch(self):
        self.platform.execute = Mock(side_effect=[
            CompletedProcess(args='shutdown', returncode=1),
            CompletedProcess(args='shutdown', returncode=0),
        ])
        self.platform._emulator_start = Mock()

        res = self.platform.emulator_stop()

        self.assertTrue(res)
        self.assertEqual(self.platform.execute.call_count, 2)
        self.platform._emulator_start.assert_not_called()

    def test_emulator_start_aborts_when_pre_stop_fails(self):
        self.platform.execute = Mock(return_value=CompletedProcess(args='shutdown', returncode=1))
        self.platform._mumu12_wait_stopped = Mock()
        self.platform.emulator_start_watch = Mock()

        res = self.platform.emulator_start()

        self.assertFalse(res)
        self.platform._mumu12_wait_stopped.assert_not_called()
        self.platform.emulator_start_watch.assert_not_called()

    def test_other_emulator_platform_none_compatible(self):
        other_instance = EmulatorInstance.__new__(EmulatorInstance)
        other_instance.type = 'LDPlayer9'
        other_instance.name = 'LDPlayer_0'
        other_instance.LDPlayer_id = 0
        other_instance.emulator = SimpleNamespace(path='F:/LDPlayer9/dnplayer.exe')
        self.platform.emulator_instance = other_instance

        self.platform.execute = Mock(return_value=None)
        res = self.platform._emulator_function_wrapper(self.platform._emulator_stop)
        self.assertTrue(res)


if __name__ == '__main__':
    unittest.main()