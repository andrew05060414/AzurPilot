"""宿舍计划好感已满后按「好感满后依次换成」换人的回归。"""

from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import module.config.server as server
from module.private_quarters.private_quarters import PrivateQuarters


class PrivateQuartersNextTargetTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('module.private_quarters.private_quarters.logger'))
        self.enterContext(patch.object(server, 'server', 'tw'))
        self.task = object.__new__(PrivateQuarters)
        self.task.config = SimpleNamespace(
            PrivateQuarters_TargetShip='anchorage',
            PrivateQuarters_NextTargets='',
            multi_set=nullcontext,
        )
        self.task.shop_filter = ''
        self.task._pq_get_daily_count = Mock(return_value=3)

    def run_with_full(self, full):
        """full：好感已满的舰娘集合；返回实际尝试互动的顺序。"""
        tried = []

        def execute(target):
            tried.append(target)
            self.task.pq_intimacy_max = target in full

        self.task.pq_execute_interact = Mock(side_effect=execute)
        self.task.pq_run(buy_roses=False, buy_cake=False, target_interact=True,
                         target_ship=self.task.config.PrivateQuarters_TargetShip)
        return tried

    def test_switches_to_next_and_persists(self):
        self.task.config.PrivateQuarters_NextTargets = 'Noshiro > new jersey > sirius'
        tried = self.run_with_full({'anchorage', 'noshiro'})
        self.assertEqual(['anchorage', 'noshiro', 'new_jersey'], tried)
        self.assertEqual('new_jersey', self.task.config.PrivateQuarters_TargetShip)
        self.assertEqual('sirius', self.task.config.PrivateQuarters_NextTargets)

    def test_skips_unknown_unsupported_and_current(self):
        # implacable 在 TW 服不可用
        self.task.config.PrivateQuarters_NextTargets = 'foo，anchorage, implacable > taihou'
        tried = self.run_with_full({'anchorage'})
        self.assertEqual(['anchorage', 'taihou'], tried)
        self.assertEqual('taihou', self.task.config.PrivateQuarters_TargetShip)
        self.assertEqual('', self.task.config.PrivateQuarters_NextTargets)

    def test_empty_queue_keeps_target(self):
        for raw in ('', None):
            with self.subTest(raw=raw):
                self.task.config.PrivateQuarters_TargetShip = 'anchorage'
                self.task.config.PrivateQuarters_NextTargets = raw
                tried = self.run_with_full({'anchorage'})
                self.assertEqual(['anchorage'], tried)
                self.assertEqual('anchorage', self.task.config.PrivateQuarters_TargetShip)

    def test_not_full_does_not_switch(self):
        self.task.config.PrivateQuarters_NextTargets = 'sirius'
        tried = self.run_with_full(set())
        self.assertEqual(['anchorage'], tried)
        self.assertEqual('sirius', self.task.config.PrivateQuarters_NextTargets)


    def test_enter_room_full_flags_and_exits(self):
        from module.private_quarters.assets import PRIVATE_QUARTERS_ROOM_TARGET_INTIMACY_MAX
        self.enterContext(patch('module.private_quarters.interact.logger'))
        task = self.task
        task.ui_click = Mock()
        task.handle_popup_cancel = Mock(return_value=False)
        task._pq_handle_dialogue = Mock()
        task._pq_goto_room_exit = Mock()
        task._pq_goto_room_seek = Mock(return_value=True)
        task._pq_target_appear = Mock()
        task.appear = Mock(side_effect=lambda button, **kw: button == PRIVATE_QUARTERS_ROOM_TARGET_INTIMACY_MAX)
        self.assertFalse(task.pq_goto_room('anchorage'))
        self.assertTrue(task.pq_intimacy_max)
        task._pq_goto_room_exit.assert_called_once_with()
        task._pq_target_appear.assert_not_called()


if __name__ == '__main__':
    unittest.main()
