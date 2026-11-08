"""空袭引导图标回归：网格定位有偏差时仍能认出已选中的目标格。

夹具是 16-3 Boss 前空袭选格时的真实截图（目标 J6，瞄准框已出现）。
有雾的地图单应性每帧会抖 15 像素以上，旧实现只允许 ±5 像素偏移，
会因认不出图标而反复点击同一格，直到 GameTooManyClickError。
"""

import os
import unittest

import cv2
import numpy as np

from module.config.config_manual import ManualConfig
from module.map_detection.grid_predictor import GridPredictor

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'air_strike_icon', 'campaign_16_3_j6_fog.png')
# 离线加载该截图得到的目标格（局部 F6）四角：左上、右上、左下、右下。
TARGET_CORNER = np.array([[847., 513.], [987., 513.], [858., 624.], [1004., 624.]])


class AirStrikeIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        image = cv2.imread(FIXTURE)
        assert image is not None, FIXTURE
        cls.image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        cls.config = ManualConfig()

    def grid(self, dx=0., dy=0.):
        return GridPredictor(
            location=(5, 5), image=self.image, corner=TARGET_CORNER + [dx, dy], config=self.config)

    def test_exact_grid_matches(self):
        self.assertTrue(self.grid().predict_air_strike_icon())

    def test_vertical_homography_jitter_still_matches(self):
        for dy in (-25, -15, 15, 25):
            with self.subTest(dy=dy):
                self.assertTrue(self.grid(dy=dy).predict_air_strike_icon())

    def test_horizontal_jitter_still_matches(self):
        for dx in (-20, 20):
            with self.subTest(dx=dx):
                self.assertTrue(self.grid(dx=dx).predict_air_strike_icon())

    def test_other_grids_do_not_match(self):
        # 往左整一格、往上两格：图标在 pad 范围之外，不能误认成选中。
        for dx, dy in ((-140, 0), (-280, 0), (0, -220)):
            with self.subTest(dx=dx, dy=dy):
                self.assertFalse(self.grid(dx=dx, dy=dy).predict_air_strike_icon())


if __name__ == '__main__':
    unittest.main()
