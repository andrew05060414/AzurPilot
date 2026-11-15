"""非原生 720p（如 1080p 缩放）下的模板阈值规则。

规则：在原阈值上降 0.05，但不低于 0.80；原阈值不高于 0.80 的保持不变；原生 720p 不变。
依据：1080p 缩放后真素材相似度只掉约 0.005（小图标最多约 0.10），
而 0.75~0.80 是长得像的兄弟素材（PR2/PR3、月度 Boss 普通/困难等）的灰区。
"""

import unittest
from unittest import mock

import numpy as np

from module.base import utils
from module.map_detection import grid_predictor
from module.map_detection.grid_predictor import GridPredictor


class LowerTemplateMatchSimilarityTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(
            utils.set_template_match_non_native_720p,
            utils.TEMPLATE_MATCH_NON_NATIVE_720P,
            utils.TEMPLATE_MATCH_NON_NATIVE_720P_RESOLUTION,
        )

    def test_native_720p_is_unchanged(self):
        utils.set_template_match_non_native_720p(False)
        for value in (0.7, 0.85, 0.97):
            self.assertEqual(utils.lower_template_match_similarity(value), value)

    def test_non_native_lowers_by_margin_with_floor(self):
        utils.set_template_match_non_native_720p(True, resolution=(1920, 1080))
        expected = {0.97: 0.92, 0.95: 0.90, 0.92: 0.87, 0.90: 0.85, 0.85: 0.80, 0.82: 0.80, 0.80: 0.80}
        for value, want in expected.items():
            self.assertAlmostEqual(utils.lower_template_match_similarity(value), want, places=6)

    def test_thresholds_not_above_floor_are_unchanged(self):
        utils.set_template_match_non_native_720p(True, resolution=(1920, 1080))
        for value in (0.6, 0.7, 0.75, 0.78):
            self.assertEqual(utils.lower_template_match_similarity(value), value)

    def test_sibling_assets_stay_rejected(self):
        # 素材互比：SHOP_SELECT_PR2/PR3 0.764、OS_MONTHBOSS_HARD/NORMAL 0.751，默认阈值 0.85 放宽后仍须高于它们
        utils.set_template_match_non_native_720p(True, resolution=(1920, 1080))
        self.assertGreater(utils.lower_template_match_similarity(0.85), 0.764)


class GridTemplatesStrictTests(unittest.TestCase):
    """当前舰队、潜艇模板在缩放帧上真假分数的空档在 0.83~0.86，不能放宽。"""

    def _grid(self):
        grid = GridPredictor.__new__(GridPredictor)
        grid.relative_crop = lambda *a, **k: np.zeros((50, 50, 3), dtype=np.uint8)
        grid.relative_hsv_count = lambda *a, **k: 1000
        return grid

    def test_current_fleet_uses_direct_match(self):
        grid = self._grid()
        with mock.patch.object(grid_predictor, 'color_similarity_2d', lambda image, color: image), \
                mock.patch.object(grid_predictor, 'TEMPLATE_FLEET_CURRENT') as template:
            template.match.return_value = True
            self.assertTrue(grid.predict_current_fleet())
            self.assertTrue(template.match.call_args.kwargs.get('direct_match'))

    def test_submarine_uses_direct_match(self):
        grid = self._grid()
        with mock.patch.object(grid_predictor, 'color_similarity_2d', lambda image, color: image), \
                mock.patch.object(grid_predictor, 'TEMPLATE_SUBMARINE') as template:
            template.match.return_value = True
            self.assertTrue(grid.predict_submarine())
            self.assertTrue(template.match.call_args.kwargs.get('direct_match'))


if __name__ == '__main__':
    unittest.main()
