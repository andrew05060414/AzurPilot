"""敌人规模图标回归：非原生 720p 下不能把弹药气泡和潜艇图标认成小型敌人。

夹具是 13-4 全图扫描卡死时的真实截图（模拟器 1080p，缩放到 720p）。
非原生 720p 会把模板阈值统一放宽到 0.75，小型敌人图标因此匹配上
H2 弹药补给点的气泡（落在陆地格 H1）和 G2 潜艇下方的潜艇图标（落在 G3），
两处同时进入视野时地图合并判定「预测错误过多」，全图扫描原地空转到 GameStuckError。
"""

import os
import unittest

import cv2
import numpy as np

from module.base import utils
from module.config.config_manual import ManualConfig
from module.map_detection.grid_predictor import GridPredictor

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'enemy_scale', 'campaign_13_4_submarine_ammo.png')
# 按实机当时的单应矩阵加载该截图得到的格子四角：左上、右上、左下、右下。
CORNERS = {
    'ammo_bubble_on_h1': [[1030.5, 93.0], [1142.1, 93.0], [1046.5, 164.6], [1162.7, 164.6]],
    'submarine_icon_on_g3': [[942.7, 242.3], [1063.9, 242.3], [956.3, 326.9], [1082.9, 326.9]],
    'medium_enemy_on_d2': [[581.6, 164.6], [697.8, 164.6], [579.2, 242.3], [700.4, 242.3]],
    'large_enemy_on_h3': [[1063.9, 242.3], [1185.1, 242.3], [1082.9, 326.9], [1209.5, 326.9]],
}


class EnemyScaleNonNative720pTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        image = cv2.imread(FIXTURE)
        assert image is not None, FIXTURE
        cls.image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        cls.config = ManualConfig()

    def setUp(self):
        self.addCleanup(
            utils.set_template_match_non_native_720p,
            utils.TEMPLATE_MATCH_NON_NATIVE_720P,
            utils.TEMPLATE_MATCH_NON_NATIVE_720P_RESOLUTION,
        )
        utils.set_template_match_non_native_720p(True, resolution=(1920, 1080))

    def scale(self, name):
        grid = GridPredictor(
            location=(0, 0), image=self.image, corner=np.array(CORNERS[name]), config=self.config)
        return grid.predict_enemy_scale()

    def test_ammo_bubble_is_not_small_enemy(self):
        self.assertEqual(self.scale('ammo_bubble_on_h1'), 0)

    def test_submarine_icon_is_not_small_enemy(self):
        self.assertEqual(self.scale('submarine_icon_on_g3'), 0)

    def test_real_enemy_icons_still_detected(self):
        self.assertEqual(self.scale('medium_enemy_on_d2'), 2)
        self.assertEqual(self.scale('large_enemy_on_h3'), 3)


if __name__ == '__main__':
    unittest.main()
