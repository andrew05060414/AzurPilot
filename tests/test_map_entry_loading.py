"""海图初始化必须等加载画面结束；自动搜索仍可交给战斗处理器。"""

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from module.exception import MapDetectionError
from module.map.map_operation import MapOperation


def entry(auto=False):
    instance = object.__new__(MapOperation)
    instance.config = SimpleNamespace(campaign_name='test', DropRecord_CombatRecord='do_not')
    instance.device = SimpleNamespace(screenshot=Mock(), click=Mock())
    instance.stat = MagicMock()
    instance.map_clear_percentage_timer = Mock()
    instance.map_is_auto_search = auto
    instance.is_in_map = Mock(return_value=False)
    instance.is_auto_search_running = Mock(return_value=False)
    instance.is_combat_executing = Mock(return_value=False)
    instance.appear = Mock(return_value=False)
    instance.appear_then_click = Mock(return_value=False)
    for name in (
        'handle_handover_conflict', 'handle_auto_search_continue', 'handle_retirement',
        'handle_use_data_key', 'handle_submarine_support_popup', 'handle_combat_low_emotion',
        'handle_urgent_commission', 'handle_2x_book_popup', 'handle_submarine_cost_popup',
        'handle_story_skip',
    ):
        setattr(instance, name, Mock(return_value=False))
    return instance


class MapEntryLoadingTests(unittest.TestCase):
    def test_manual_entry_can_handoff_an_already_started_combat(self):
        instance = entry()
        instance.is_combat_loading = Mock(return_value=True)
        instance.is_combat_executing.return_value = True
        timer = Mock()
        timer.reached.return_value = False
        with patch('module.map.map_operation.Timer', return_value=timer):
            instance.enter_map('ex')
        instance.device.screenshot.assert_not_called()

    def test_manual_map_waits_for_positive_map_detection(self):
        instance = entry()
        instance.is_combat_loading = Mock(side_effect=[True, True, False])
        instance.handle_in_map_with_enemy_searching = Mock(return_value=True)
        timer = Mock()
        timer.reached.return_value = False
        with patch('module.map.map_operation.Timer', return_value=timer):
            instance.enter_map('13-4')
        self.assertEqual(instance.device.screenshot.call_count, 2)
        instance.handle_in_map_with_enemy_searching.assert_called_once()

    def test_auto_search_can_handoff_loading_to_combat(self):
        instance = entry(auto=True)
        instance.is_combat_loading = Mock(return_value=True)
        timer = Mock()
        timer.reached.return_value = False
        with patch('module.map.map_operation.Timer', return_value=timer):
            instance.enter_map('13-4')
        instance.device.screenshot.assert_not_called()

    def test_manual_loading_has_finite_timeout(self):
        instance = entry()
        instance.is_combat_loading = Mock(return_value=True)
        timer = Mock()
        timer.reached.return_value = False
        timeout = Mock()
        timeout.started.return_value = True
        timeout.reached.return_value = True
        with patch('module.map.map_operation.Timer', side_effect=[timer, timer, timer, timeout]):
            with self.assertRaises(MapDetectionError):
                instance.enter_map('13-4')
