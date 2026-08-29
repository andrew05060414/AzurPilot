import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from module.device.human_input import WindowsHumanInputMonitor


class TestWindowsHumanInputMonitor(unittest.TestCase):
    def setUp(self):
        WindowsHumanInputMonitor._last_emulator_process_id = None

    def tearDown(self):
        WindowsHumanInputMonitor._last_emulator_process_id = None

    def test_process_must_match_script_target_path_and_instance_name(self):
        process = Mock()
        process.exe.return_value = r'C:\Emulator\player.exe'
        process.cmdline.return_value = ['player.exe', '--instance', 'target']
        target = SimpleNamespace(
            path=r'c:/emulator/player.exe', name='target', type='MuMuPlayer12'
        )

        self.assertTrue(
            WindowsHumanInputMonitor._process_matches_instance(process, 0, target)
        )

        other_instance = SimpleNamespace(
            path=target.path, name='other', type='MuMuPlayer12'
        )
        self.assertFalse(
            WindowsHumanInputMonitor._process_matches_instance(process, 0, other_instance)
        )

    def test_process_from_another_emulator_install_is_ignored(self):
        process = Mock()
        process.exe.return_value = r'C:\OtherEmulator\player.exe'
        target = SimpleNamespace(
            path=r'C:\ScriptEmulator\player.exe', name='target', type='MuMuPlayer12'
        )

        self.assertFalse(
            WindowsHumanInputMonitor._process_matches_instance(process, 0, target)
        )

    def test_unsupported_emulator_type_is_ignored(self):
        process = Mock()
        process.exe.return_value = r'C:\Emulator\player.exe'
        process.cmdline.return_value = ['player.exe', '--instance', 'target']
        target = SimpleNamespace(
            path=r'C:\Emulator\player.exe', name='target', type='LDPlayer9'
        )

        self.assertFalse(
            WindowsHumanInputMonitor._process_matches_instance(process, 0, target)
        )

    def test_mumu_homepage_process_is_ignored(self):
        process = Mock()
        process.exe.return_value = r'C:\MuMu\MuMuNxMain.exe'
        process.cmdline.return_value = ['MuMuNxMain.exe']
        target = SimpleNamespace(
            path=r'C:\MuMu\MuMuNxMain.exe',
            name='MuMuPlayer-12.0-0',
            type='MuMuPlayer12',
        )

        self.assertFalse(
            WindowsHumanInputMonitor._process_matches_instance(process, 0, target)
        )

    def test_mumu_player_process_is_matched_when_instance_path_is_mumu_homepage(self):
        process = Mock()
        process.exe.return_value = r'C:\MuMu\shell\MuMuPlayer.exe'
        process.cmdline.return_value = ['MuMuPlayer.exe', 'MuMuPlayer-12.0-0']
        target = SimpleNamespace(
            path=r'C:\MuMu\shell\nx_main\MuMuNxMain.exe',
            name='MuMuPlayer-12.0-0',
            type='MuMuPlayer12',
        )

        self.assertTrue(
            WindowsHumanInputMonitor._process_matches_instance(process, 0, target)
        )

    def test_focus_return_ignores_input_from_previous_foreground_window(self):
        with patch.object(
            WindowsHumanInputMonitor,
            'foreground_emulator_process_id',
            side_effect=[None, 1234],
        ), patch.object(
            WindowsHumanInputMonitor,
            'recent_input_seconds',
        ) as recent_input:
            WindowsHumanInputMonitor.wait_until_idle(30)
            WindowsHumanInputMonitor.wait_until_idle(30)

        recent_input.assert_not_called()

    def test_recent_input_still_pauses_when_emulator_stays_foreground(self):
        with patch.object(
            WindowsHumanInputMonitor,
            'foreground_emulator_process_id',
            return_value=1234,
        ), patch.object(
            WindowsHumanInputMonitor,
            'recent_input_seconds',
            side_effect=[0, 30],
        ), patch('module.device.human_input.time.sleep') as sleep:
            WindowsHumanInputMonitor._last_emulator_process_id = 1234
            WindowsHumanInputMonitor.wait_until_idle(30)

        sleep.assert_called_once_with(0.5)

    def test_foreground_emulator_change_ignores_previous_process_input(self):
        with patch.object(
            WindowsHumanInputMonitor,
            'foreground_emulator_process_id',
            side_effect=[1234, 5678, 5678],
        ), patch.object(
            WindowsHumanInputMonitor,
            'recent_input_seconds',
            return_value=0,
        ) as recent_input, patch('module.device.human_input.time.sleep'):
            WindowsHumanInputMonitor._last_emulator_process_id = 1234
            WindowsHumanInputMonitor.wait_until_idle(30)
            WindowsHumanInputMonitor.wait_until_idle(30)

        recent_input.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
