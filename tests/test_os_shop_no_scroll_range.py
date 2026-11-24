"""滚动条无可滚动范围（内容一屏放得下）时的行为（PX-2039）。"""

import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from module.ui.scroll import AdaptiveScroll


def _scroll(total_height=510):
    return AdaptiveScroll((1176, 210, 1180, 210 + total_height), name="T")


class TestScrollHasRange(unittest.TestCase):
    def test_degenerate_length_longer_than_total(self):
        # 2026-11-23 真机日志：(324.0-257.5)/(510-515)，滑块 515 > 轨道 510
        s = _scroll()
        with patch.object(AdaptiveScroll, "match_color", lambda self, main: setattr(self, "length", 515) or np.zeros(510, bool)):
            self.assertFalse(s.has_range(MagicMock()))
            self.assertEqual(s.cal_position.__func__ is not None, True)

    def test_thumb_fills_track(self):
        s = _scroll()
        with patch.object(AdaptiveScroll, "match_color", lambda self, main: setattr(self, "length", 510) or np.zeros(510, bool)):
            self.assertFalse(s.has_range(MagicMock()))

    def test_normal_thumb_has_range(self):
        s = _scroll()
        with patch.object(AdaptiveScroll, "match_color", lambda self, main: setattr(self, "length", 200) or np.zeros(510, bool)):
            self.assertTrue(s.has_range(MagicMock()))


class TestPortShopScanSinglePage(unittest.TestCase):
    def test_scan_all_single_page_does_not_raise_when_no_range(self):
        from module.os_shop import port_shop

        shop = MagicMock()
        shop.init_slider.return_value = (-1.0, 0.0)
        shop.os_shop_get_items.return_value = [MagicMock(is_known_item=lambda: True)]
        shop.pre_scroll.side_effect = AssertionError("pre_scroll must not be called without scroll range")
        with patch.object(port_shop.OS_SHOP_SCROLL, "has_range", return_value=False), \
                patch.object(port_shop.OS_SHOP_SCROLL, "at_bottom", side_effect=AssertionError("at_bottom must not be consulted")):
            items = port_shop.PortShop.scan_all(shop)
        self.assertEqual(len(items), 4)  # 4 个标签页各扫一屏
        shop.pre_scroll.assert_not_called()

    def test_scan_all_scrollable_still_uses_pre_scroll(self):
        from module.os_shop import port_shop

        shop = MagicMock()
        shop.init_slider.return_value = (-1.0, 0.0)
        shop.pre_scroll.return_value = 0.0
        shop.os_shop_get_items.return_value = [MagicMock(is_known_item=lambda: True)]
        with patch.object(port_shop.OS_SHOP_SCROLL, "has_range", return_value=True), \
                patch.object(port_shop.OS_SHOP_SCROLL, "at_bottom", return_value=True):
            port_shop.PortShop.scan_all(shop)
        self.assertEqual(shop.pre_scroll.call_count, 4)


if __name__ == "__main__":
    unittest.main()
