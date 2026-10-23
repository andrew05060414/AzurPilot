# -*- coding: utf-8 -*-
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

from module.base.base import ModuleBase
from module.base.timer import Timer
from module.exception import GameStuckError, GameTooManyClickError
from module.ui.scroll import Scroll

FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "commission_scroll"
    / "commission_daily_stuck.png"
)


class TestCommissionScrollRecovery(unittest.TestCase):
    def setUp(self):
        self.area = (1254, 77, 1261, 676)

    def test_fixture_metrics(self):
        """验证实机截图中的滚动条识别几何：length 与可滚动距离。"""
        scroll = Scroll(self.area, color=(247, 211, 66), name="COMMISSION_SCROLL")
        img = Image.open(FIXTURE_PATH)

        class DummyMain(ModuleBase):
            def __init__(self, im):
                self.device = SimpleNamespace(image=np.array(im))

        dummy = DummyMain(img)
        self.assertTrue(scroll.appear(dummy))
        self.assertAlmostEqual(scroll.length, 497, delta=10)
        self.assertEqual(scroll.total, 599)
        # 可滚动区间仅约 100px (总高 599 - 长度 497)
        self.assertLess(scroll.total - scroll.length, 110)
        pos = scroll.cal_position(dummy)
        self.assertLess(pos, 0.06)
        self.assertFalse(scroll.at_bottom(dummy))

    def test_commission_scroll_raises_gamestuckerror_on_no_progress(self):
        """验证在滑动连续无进展时抛出 GameStuckError，而非打满 12 次触发 GameTooManyClickError。"""
        from module.commission.commission import CommissionScroll

        scroll = CommissionScroll(
            self.area, color=(247, 211, 66), name="COMMISSION_SCROLL"
        )
        scroll.length = 504
        # 使用快速计次计时器避免真实时间等待
        scroll.drag_interval = Timer(0, count=1)

        swipes = []
        click_record = []

        class MockDevice:
            def screenshot(self):
                pass

            def swipe(self, p1, p2, name="SWIPE", distance_check=True):
                swipes.append((p1, p2))
                click_record.append(name)
                if click_record.count("COMMISSION_SCROLL") >= 12:
                    raise GameTooManyClickError(
                        "[设备-点击] 按钮点击次数过多: COMMISSION_SCROLL"
                    )

        main = SimpleNamespace(device=MockDevice())
        # 位置在滑动后始终为 0.01（无进展）
        scroll.cal_position = Mock(return_value=0.01)

        with self.assertRaises(GameStuckError):
            scroll.set(1.0, main=main)

        # 3次无进展判定后停止，共执行 3 次滑动尝试，不触及 12 次点击保护
        self.assertEqual(len(swipes), 3)
        self.assertLess(len(swipes), 12)

    def test_delayed_frame_progress_does_not_false_positive(self):
        """验证滑动后延迟 1~2 帧才发生位移不会被判定为无进展。"""
        from module.commission.commission import CommissionScroll

        scroll = CommissionScroll(
            self.area, color=(247, 211, 66), name="COMMISSION_SCROLL"
        )
        scroll.length = 504
        # 每次滑动后需经历 3 次截图才完成间隔
        scroll.drag_interval = Timer(0, count=3)

        # 序列模拟：第一次滑动后前两帧未刷新(0.01, 0.01)，在间隔边界刷新为 0.5；
        # 第二次滑动后同样延迟，最终到达 0.98
        positions = [
            0.01,  # 初始
            0.01,
            0.01,
            0.50,  # 滑动1后间隔内的采样，在边界到达0.50
            0.50,
            0.50,
            0.98,  # 滑动2后间隔内的采样，在边界到达0.98
        ]
        pos_idx = [0]

        class MockDevice:
            def screenshot(self):
                if pos_idx[0] < len(positions) - 1:
                    pos_idx[0] += 1

            def swipe(self, p1, p2, name="SWIPE", distance_check=True):
                pass

        main = SimpleNamespace(device=MockDevice())
        scroll.cal_position = Mock(side_effect=lambda m: positions[pos_idx[0]])

        dragged = scroll.set(1.0, main=main)
        # 成功通过 2 次滑动到达目标，没有被中途报 stuck
        self.assertEqual(dragged, 2)

    def test_continuous_small_step_progress(self):
        """验证持续小步慢进展（超过4次但稳步前进）不会被过早截断。"""
        from module.commission.commission import CommissionScroll

        scroll = CommissionScroll(
            self.area, color=(247, 211, 66), name="COMMISSION_SCROLL"
        )
        scroll.length = 504
        scroll.drag_interval = Timer(0, count=1)

        # 每次滑动前进约 0.15，经历 6 次持续滑动到达 0.96 (>= 0.95)
        step_positions = [
            0.05,
            0.20,
            0.20,
            0.35,
            0.35,
            0.50,
            0.50,
            0.65,
            0.65,
            0.80,
            0.80,
            0.96,
            0.96,
        ]
        pos_idx = [0]

        class MockDevice:
            def screenshot(self):
                if pos_idx[0] < len(step_positions) - 1:
                    pos_idx[0] += 1

            def swipe(self, p1, p2, name="SWIPE", distance_check=True):
                pass

        main = SimpleNamespace(device=MockDevice())
        scroll.cal_position = Mock(side_effect=lambda m: step_positions[pos_idx[0]])

        dragged = scroll.set(1.0, main=main)
        self.assertEqual(dragged, 6)

    def test_disappearing_scrollbar_does_not_swipe_and_raises_stuck(self):
        """验证滚动条消失时（length=0）不发送任何输入手势，且针对 target=0.0 与 target=1.0 均拒绝误判成功并抛出 GameStuckError。"""
        from module.commission.commission import CommissionScroll

        for target in (0.0, 1.0):
            with self.subTest(target=target):
                scroll = CommissionScroll(
                    self.area, color=(247, 211, 66), name="COMMISSION_SCROLL"
                )
                scroll.length = 0
                scroll.drag_timeout = Timer(0, count=2)

                swipes = []

                class MockDevice:
                    def screenshot(self):
                        pass

                    def swipe(self, p1, p2, name="SWIPE", distance_check=True):
                        swipes.append((p1, p2))

                main = SimpleNamespace(device=MockDevice())
                # cal_position 在 length=0 时通常会回退为 0.0
                scroll.cal_position = Mock(return_value=0.0)

                with self.assertRaises(GameStuckError):
                    scroll.set(target, main=main)

                # 确认为 0 次滑动输入，无坐标手势外发
                self.assertEqual(len(swipes), 0)

    def test_commission_swipe_at_bottom_or_no_scrollable_area(self):
        """验证正向确认到底或无可滚动区时正常结束（返回 False），使用 patch 保护避免污染全局状态。"""
        from module.commission.commission import RewardCommission, COMMISSION_SCROLL

        reward = RewardCommission(config=SimpleNamespace(), device=SimpleNamespace())

        # 情况 1: 滚动条已到底
        with (
            patch.object(COMMISSION_SCROLL, "appear", return_value=True),
            patch.object(COMMISSION_SCROLL, "at_bottom", return_value=True),
        ):
            self.assertFalse(reward._commission_swipe())

        # 情况 2: 滚动条可滚动空间不足 10px（无可滚动区）
        with (
            patch.object(COMMISSION_SCROLL, "appear", return_value=True),
            patch.object(COMMISSION_SCROLL, "at_bottom", return_value=False),
            patch.object(COMMISSION_SCROLL, "total", 599),
            patch.object(COMMISSION_SCROLL, "length", 595),
        ):
            self.assertFalse(reward._commission_swipe())


if __name__ == "__main__":
    unittest.main()
