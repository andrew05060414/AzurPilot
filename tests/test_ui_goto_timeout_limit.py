"""ui_goto 连续导航超时上限测试。

目标页面检测失效时，ui_goto 原本会无限重试（2026-11-07 宿舍计划循环 2 小时），
现在连续超时达到上限后抛出 GameStuckError，交给上层卡死恢复。
"""
import unittest
from unittest import mock

import module.ui.ui as ui_module
from module.exception import GameStuckError
from module.ui.page import page_main, page_private_quarters


class _AlwaysReached:
    """替代 Timer：每次检查都视为已超时。"""

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        return self

    def reset(self):
        return self

    def reached(self):
        return True


def _make_ui():
    ui = object.__new__(ui_module.UI)
    ui.device = mock.Mock()
    ui.ui_page_appear = mock.Mock(return_value=False)
    ui.is_in_main = mock.Mock(return_value=False)
    ui.appear = mock.Mock(return_value=False)
    ui.ui_additional = mock.Mock(return_value=False)
    ui.interval_clear = mock.Mock()
    ui.ui_button_interval_reset = mock.Mock()
    ui.ui_get_current_page = mock.Mock(return_value=page_main)
    return ui


class UiGotoTimeoutLimitTest(unittest.TestCase):
    def test_unrecognizable_destination_raises_after_limit(self):
        ui = _make_ui()
        with mock.patch.object(ui_module, 'Timer', _AlwaysReached):
            with self.assertRaises(GameStuckError):
                ui.ui_goto(page_private_quarters)
        # 达到上限前每次超时都尝试过一次识别与恢复
        self.assertEqual(ui.ui_get_current_page.call_count, ui_module.UI_GOTO_TIMEOUT_LIMIT - 1)

    def test_recovery_reaching_destination_returns_normally(self):
        ui = _make_ui()
        ui.ui_get_current_page = mock.Mock(return_value=page_private_quarters)
        with mock.patch.object(ui_module, 'Timer', _AlwaysReached):
            ui.ui_goto(page_private_quarters)
        self.assertEqual(ui.ui_get_current_page.call_count, 1)


if __name__ == '__main__':
    unittest.main()
