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
        process.exe.return_value = r'C:\Emulator\MuMuPlayer.exe'
        process.cmdline.return_value = ['MuMuPlayer.exe', 'target']
        target = SimpleNamespace(
            path=r'c:/emulator/MuMuPlayer.exe', name='target', type='MuMuPlayer12'
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

    def test_instance_name_must_be_an_exact_token_for_mumu12(self):
        process = Mock()
        process.exe.return_value = r'C:\MuMu\MuMuPlayer.exe'
        process.cmdline.return_value = ['MuMuPlayer.exe', 'MuMuPlayer-12.0-10']
        target = SimpleNamespace(
            path=r'C:\MuMu\MuMuPlayer.exe', name='MuMuPlayer-12.0-1', type='MuMuPlayer12'
        )

        self.assertFalse(
            WindowsHumanInputMonitor._process_matches_instance(process, 0, target)
        )

    def test_mumu12_instance_numbers_do_not_prefix_match(self):
        for target_name, running_name in [
            ('MuMuPlayer-12.0-1', 'MuMuPlayer-12.0-10'),
            ('MuMuPlayer-12.0-10', 'MuMuPlayer-12.0-100'),
            ('MuMuPlayer-12.0-100', 'MuMuPlayer-12.0-1'),
        ]:
            with self.subTest(target_name=target_name, running_name=running_name):
                process = Mock()
                process.exe.return_value = r'C:\MuMu\MuMuPlayer.exe'
                process.cmdline.return_value = ['MuMuPlayer.exe', running_name]
                target = SimpleNamespace(
                    path=r'C:\MuMu\MuMuPlayer.exe', name=target_name, type='MuMuPlayer12'
                )
                self.assertFalse(
                    WindowsHumanInputMonitor._process_matches_instance(process, 0, target)
                )

    def test_window_title_instance_name_uses_boundaries(self):
        process = Mock()
        process.exe.return_value = r'C:\MuMu\MuMuPlayer.exe'
        process.cmdline.return_value = []
        target = SimpleNamespace(
            path=r'C:\MuMu\MuMuPlayer.exe', name='MuMuPlayer-12.0-1', type='MuMuPlayer12'
        )

        with patch.object(
            WindowsHumanInputMonitor, '_window_title', return_value='MuMuPlayer-12.0-10'
        ):
            self.assertFalse(
                WindowsHumanInputMonitor._process_matches_instance(process, 0, target)
            )

        with patch.object(
            WindowsHumanInputMonitor, '_window_title', return_value='MuMuPlayer-12.0-1 - Azur Lane'
        ):
            self.assertTrue(
                WindowsHumanInputMonitor._process_matches_instance(process, 0, target)
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

    def test_mumu12_homepage_only_allows_controlled_player_path_variant(self):
        process = Mock()
        process.exe.return_value = r'C:\MuMu\other\MuMuPlayer.exe'
        process.cmdline.return_value = ['MuMuPlayer.exe', 'MuMuPlayer-12.0-0']
        target = SimpleNamespace(
            path=r'C:\MuMu\shell\nx_main\MuMuNxMain.exe',
            name='MuMuPlayer-12.0-0',
            type='MuMuPlayer12',
        )

        self.assertFalse(
            WindowsHumanInputMonitor._process_matches_instance(process, 0, target)
        )

    def test_family_specific_instance_arguments_match_exactly(self):
        cases = [
            ('BlueStacks5', r'C:\BlueStacks\HD-Player.exe', ['HD-Player.exe', '--instance', 'Pie64'], 'Pie64'),
            ('BlueStacks4', r'C:\BlueStacks\Bluestacks.exe', ['Bluestacks.exe', '-vmname', 'Android_1'], 'Android_1'),
            ('NoxPlayer', r'C:\Nox\Nox.exe', ['Nox.exe', '-clone:Nox_1'], 'Nox_1'),
            ('MEmuPlayer', r'C:\MEmu\MEmu.exe', ['MEmu.exe', 'MEmu_0'], 'MEmu_0'),
            ('MuMuPlayerX', r'C:\MuMu\NemuPlayer.exe', ['NemuPlayer.exe', '-m', 'nemu-default'], 'nemu-default'),
        ]
        for emulator_type, path, command_line, name in cases:
            with self.subTest(emulator_type=emulator_type):
                process = Mock()
                process.exe.return_value = path
                process.cmdline.return_value = command_line
                target = SimpleNamespace(path=path, name=name, type=emulator_type)
                self.assertTrue(
                    WindowsHumanInputMonitor._process_matches_instance(process, 0, target)
                )

    def test_family_specific_instance_arguments_reject_other_instance(self):
        cases = [
            ('BlueStacks5', r'C:\BlueStacks\HD-Player.exe', ['HD-Player.exe', '--instance', 'Pie64'], 'Pie6410'),
            ('BlueStacks4', r'C:\BlueStacks\Bluestacks.exe', ['Bluestacks.exe', '-vmname', 'Android_10'], 'Android_1'),
            ('NoxPlayer', r'C:\Nox\Nox.exe', ['Nox.exe', '-clone:Nox_10'], 'Nox_1'),
            ('MEmuPlayer', r'C:\MEmu\MEmu.exe', ['MEmu.exe', 'MEmu_10'], 'MEmu_1'),
            ('MuMuPlayerX', r'C:\MuMu\NemuPlayer.exe', ['NemuPlayer.exe', '-m', 'nemu-100'], 'nemu-1'),
        ]
        for emulator_type, path, command_line, name in cases:
            with self.subTest(emulator_type=emulator_type):
                process = Mock()
                process.exe.return_value = path
                process.cmdline.return_value = command_line
                target = SimpleNamespace(path=path, name=name, type=emulator_type)
                self.assertFalse(
                    WindowsHumanInputMonitor._process_matches_instance(process, 0, target)
                )

    def test_manager_service_and_homepage_processes_are_ignored(self):
        cases = [
            ('BlueStacks5', r'C:\BlueStacks\BstkSVC.exe'),
            ('BlueStacks4', r'C:\BlueStacks\bsconsole.exe'),
            ('NoxPlayer', r'C:\Nox\MultiPlayerManager.exe'),
            ('MEmuPlayer', r'C:\MEmu\memuc.exe'),
        ]
        for emulator_type, process_path in cases:
            with self.subTest(emulator_type=emulator_type):
                process = Mock()
                process.exe.return_value = process_path
                process.cmdline.return_value = ['manager.exe', 'target']
                target = SimpleNamespace(
                    path=process_path, name='target', type=emulator_type
                )
                self.assertFalse(
                    WindowsHumanInputMonitor._process_matches_instance(process, 0, target)
                )

    def test_path_normalization_uses_windows_semantics_on_any_host(self):
        self.assertEqual(
            WindowsHumanInputMonitor._normalize_path(r'C:\Emulator\..\Player.exe'),
            'c:/player.exe',
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
