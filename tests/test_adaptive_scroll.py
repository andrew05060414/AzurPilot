"""AdaptiveScroll 多峰去重与零范围保护回归测试（PX-2063）。"""

import unittest
from unittest.mock import MagicMock

import numpy as np

from module.ui.scroll import AdaptiveScroll, Scroll


class _DummyMain:
    def __init__(self, image: np.ndarray):
        self._image = image
        self.device = MagicMock()

    def image_crop(self, area, copy=False):
        x0, y0, x1, y1 = area
        crop = self._image[y0:y1, x0:x1]
        return crop.copy() if copy else crop


def _make_os_shop_scroll() -> AdaptiveScroll:
    scroll = AdaptiveScroll(
        (1176, 210, 1180, 720),
        parameters={"height": 255 - 99, "prominence": 40},
        name="OS_SHOP_SCROLL",
    )
    scroll.drag_threshold = 0.1
    scroll.edge_threshold = 0.1
    return scroll


def _make_double_edge_vertical_image(thumb_start: int, thumb_height: int) -> np.ndarray:
    """构造 720p 截图：在 (1176, 210, 1180, 720) 区域内生成边缘偏亮的双峰滑块。

    复现 2026-11-24 实机直布罗特大世界商店截图的灰度截面：
    背景约 40，4px 滑块为 [206, 195, 195, 206]，scipy.signal.find_peaks 每行检出 2 个峰。
    """
    img = np.full((720, 1280, 3), 40, dtype=np.uint8)
    y0 = 210 + thumb_start
    y1 = y0 + thumb_height
    img[y0:y1, 1176, :] = 206
    img[y0:y1, 1177, :] = 195
    img[y0:y1, 1178, :] = 195
    img[y0:y1, 1179, :] = 206
    return img


class TestAdaptiveScrollDeduplication(unittest.TestCase):
    def test_double_peak_vertical_thumb_length_and_position(self):
        # 258px 滑块位于轨道 [187..444]，双峰原逻辑会得出 length=516 > total=510
        scroll = _make_os_shop_scroll()
        main = _DummyMain(_make_double_edge_vertical_image(thumb_start=187, thumb_height=258))

        pos = scroll.cal_position(main)

        self.assertEqual(int(scroll.length), 258)
        expected_pos = (315.5 - 258 / 2) / (510 - 258)
        self.assertAlmostEqual(pos, expected_pos, places=3)
        self.assertFalse(scroll.at_top(main))
        self.assertFalse(scroll.at_bottom(main))

    def test_double_peak_boundary_255_rows_no_zero_division(self):
        # 255 行 × 2 峰 = 510 == total，原逻辑会触发除零得到 1.00 并在 set_top 时横向空滑死循环
        scroll = _make_os_shop_scroll()
        main = _DummyMain(_make_double_edge_vertical_image(thumb_start=175, thumb_height=255))

        pos = scroll.cal_position(main)

        self.assertEqual(int(scroll.length), 255)
        expected_pos = (302.0 - 255 / 2) / (510 - 255)
        self.assertAlmostEqual(pos, expected_pos, places=3)

    def test_next_page_advances_forward_instead_of_clamping_to_zero(self):
        scroll = _make_os_shop_scroll()
        main = _DummyMain(_make_double_edge_vertical_image(thumb_start=0, thumb_height=258))
        captured = {}

        def _fake_set(target, main, random_range=(-0.05, 0.05), skip_first_screenshot=True):
            captured["target"] = target
            return 1

        scroll.set = _fake_set
        scroll.next_page(main=main, page=0.5, skip_first_screenshot=True)

        self.assertEqual(int(scroll.length), 258)
        self.assertGreater(captured["target"], 0.4)

    def test_double_peak_horizontal_thumb_deduplicated(self):
        scroll = AdaptiveScroll(
            (200, 600, 700, 604),
            parameters={"height": 255 - 99, "prominence": 40},
            is_vertical=False,
            name="H_SCROLL",
        )
        img = np.full((720, 1280, 3), 40, dtype=np.uint8)
        # 水平滑块宽 260px，起始偏移 120px（位于轨道正中）
        x0, x1 = 200 + 120, 200 + 120 + 260
        img[600, x0:x1, :] = 206
        img[601, x0:x1, :] = 195
        img[602, x0:x1, :] = 195
        img[603, x0:x1, :] = 206
        main = _DummyMain(img)

        pos = scroll.cal_position(main)

        self.assertEqual(int(scroll.length), 260)
        self.assertAlmostEqual(pos, 0.5, places=2)


class TestScrollZeroRangeGuard(unittest.TestCase):
    def test_full_track_thumb_cal_position_and_drag_page_safe(self):
        scroll = Scroll((100, 100, 104, 200), color=(255, 255, 255), name="FULL_SCROLL")
        img = np.full((720, 1280, 3), 255, dtype=np.uint8)
        main = _DummyMain(img)

        pos = scroll.cal_position(main)
        dragged = scroll.drag_page(0.8, main=main, skip_first_screenshot=True)

        self.assertEqual(int(scroll.length), 100)
        self.assertEqual(pos, 0.0)
        self.assertEqual(dragged, 0)
        main.device.swipe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
