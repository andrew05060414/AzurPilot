"""相机进展回归：整格滑动不能被周期性的单应偏移判成未移动。"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from module.map.camera import Camera
from module.exception import GameStuckError, MapDetectionError


def camera_at(location=(3, 2)):
    camera = object.__new__(Camera)
    camera.camera = location
    camera.config = SimpleNamespace(MAP_ENSURE_EDGE_INSIGHT_CORNER='bottom')
    camera.map = SimpleNamespace(shape=(10, 6))
    camera.view = SimpleNamespace(
        left_edge=True, right_edge=False, lower_edge=False, upper_edge=False,
        backend=SimpleNamespace(homo_loca=np.array([25., 60.])),
        swipe_base=np.array([140., 100.]),
    )
    camera.update = Mock()
    return camera


class EdgeProgressTests(unittest.TestCase):
    def test_missing_edges_and_unmatched_fleet_cannot_integrate_forever(self):
        camera = camera_at()
        camera.view.left_edge = False
        camera.config.MAP_SWIPE_PREDICT = True
        camera.config.MAP_SWIPE_PREDICT_WITH_CURRENT_FLEET = True
        camera.config.MAP_SWIPE_PREDICT_WITH_SEA_GRIDS = False
        camera.view.center_loca = (3, 3)
        camera.view.shape = (8, 5)
        camera.predict = Mock()
        camera.show_camera = Mock()

        def swipe(vector):
            camera._prev_view = SimpleNamespace(predict_swipe=Mock(return_value=None))
            camera._prev_swipe = vector
            camera._update_view_data()
            return True

        camera.map_swipe = Mock(side_effect=swipe)
        with self.assertRaisesRegex(MapDetectionError, 'Map edge not found'):
            camera.ensure_edge_insight()
        self.assertLessEqual(camera.map_swipe.call_count, 6)

    def test_real_update_preserves_fleet_match_then_anchors_bottom_edge(self):
        camera = camera_at((3, 0))
        camera.config.MAP_SWIPE_PREDICT = True
        camera.config.MAP_SWIPE_PREDICT_WITH_CURRENT_FLEET = True
        camera.config.MAP_SWIPE_PREDICT_WITH_SEA_GRIDS = False
        camera.map = SimpleNamespace(shape=(10, 6))
        camera.view.center_loca = (3, 3)
        camera.view.shape = (8, 5)
        camera.predict = Mock()
        camera.show_camera = Mock()
        steps = []

        def swipe(vector):
            steps.append(tuple(vector))
            if not any(vector):
                return False
            camera._prev_view = SimpleNamespace(predict_swipe=Mock(return_value=(0, 2)))
            camera._prev_swipe = vector
            camera.view.upper_edge = len(steps) >= 2
            # 执行真实的相机积分和边缘锚定，而不是直接赋预期相机位置。
            camera._update_view_data()
            return True

        camera.map_swipe = Mock(side_effect=swipe)
        camera.ensure_edge_insight()
        self.assertEqual(camera.camera, (3, 4))
        self.assertTrue(camera.view.upper_edge)
        self.assertEqual(steps, [(0, 2), (0, 2), (0, 0)])

    def test_whole_grid_swipe_keeps_confirmed_camera_and_reaches_edge(self):
        camera = camera_at()
        calls = []

        def swipe(vector):
            calls.append(tuple(vector))
            if vector[1]:
                # 两格滑动前后 homo_loca 相同，但舰队匹配确认相机已移动。
                camera.camera = (3, camera.camera[1] + 2)
                if camera.camera[1] >= 6:
                    camera.view.upper_edge = True
            return bool(any(vector))

        camera.map_swipe = Mock(side_effect=swipe)
        camera.ensure_edge_insight()
        self.assertEqual(camera.camera, (3, 6))
        self.assertTrue(camera.view.upper_edge)
        self.assertEqual(calls.count((0, 2)), 2)

    def test_partial_swipe_does_not_undo_observed_position(self):
        camera = camera_at()

        def swipe(vector):
            if vector[1]:
                # 请求两格但实际只移动一格，应保留实际位置。
                camera.camera = (3, 3)
                camera.view.upper_edge = True
            return bool(any(vector))

        camera.map_swipe = Mock(side_effect=swipe)
        camera.ensure_edge_insight()
        self.assertEqual(camera.camera, (3, 3))

    def test_no_movement_stops_without_changing_camera(self):
        camera = camera_at()
        camera.map_swipe = Mock(return_value=True)
        camera.ensure_edge_insight()
        self.assertEqual(camera.camera, (3, 2))
        self.assertLessEqual(camera.map_swipe.call_count, 2)

    def test_stopped_axis_does_not_stop_other_axis(self):
        camera = camera_at()
        camera.view.left_edge = False

        def swipe(vector):
            if vector[1]:
                camera.camera = (3, camera.camera[1] + 2)
                if camera.camera[1] >= 6:
                    camera.view.upper_edge = True
            return True

        camera.map_swipe = Mock(side_effect=swipe)
        camera.ensure_edge_insight()
        self.assertEqual(camera.camera, (3, 6))
        self.assertLessEqual(camera.map_swipe.call_count, 4)


class FocusProgressTests(unittest.TestCase):
    def test_already_focused_does_not_send_recentering_gesture(self):
        camera = camera_at()
        camera.map_swipe = Mock(return_value=False)
        camera.focus_to((3, 2))
        camera.map_swipe.assert_not_called()

    def test_stagnant_gestures_stop_before_click_protection(self):
        camera = camera_at()
        camera.map_swipe = Mock(side_effect=[True, True, AssertionError('第三次无效滑动')])
        camera.focus_to((4, 2))
        self.assertEqual(camera.map_swipe.call_count, 2)

    def test_successful_focus_keeps_whole_grid_progress(self):
        camera = camera_at()

        def swipe(vector):
            if not any(vector):
                return False
            camera.camera = tuple(np.add(camera.camera, vector))
            return True

        camera.map_swipe = Mock(side_effect=swipe)
        camera.focus_to((9, 2), swipe_limit=(2, 2))
        self.assertEqual(camera.camera, (9, 2))
        self.assertEqual(camera.map_swipe.call_count, 3)


class FakeView(SimpleNamespace):
    def __contains__(self, item):
        return tuple(item) in self.grids

    def __getitem__(self, item):
        return self.grids[tuple(item)]


def camera_with_view(location, grids):
    camera = camera_at(location)
    camera.view = FakeView(**vars(camera.view), center_loca=(3, 3),
                           grids={g: 'grid%s%s' % g for g in grids})
    return camera


# 透视视野的角落格子不一定被识别，实机崩溃的 (8, 5) 就不在 view.grids 中。
VIEW_9X6 = [(x, y) for x in range(9) for y in range(6) if (x, y) != (8, 5)]


class ConvertGlobalToLocalTests(unittest.TestCase):
    def test_frozen_swipes_raise_recoverable_stuck_instead_of_key_error(self):
        # 实机 12-4：相机 A6，聚焦 F8 时画面不随滑动变化，原先 view[(8, 5)] 抛 KeyError。
        camera = camera_with_view((0, 5), VIEW_9X6)
        camera.map_swipe = Mock(return_value=True)
        with self.assertRaisesRegex(GameStuckError, 'F8'):
            camera.convert_global_to_local((5, 7))
        camera.update.assert_called()
        # 两轮 focus_to 各两次 + 边缘校准；不能无限滑动触发 MAP_SWIPE 点击保护。
        self.assertLessEqual(camera.map_swipe.call_count, 7)

    def test_edge_calibration_recovers_misestimated_camera(self):
        camera = camera_with_view((0, 5), VIEW_9X6)
        camera.map_swipe = Mock(return_value=True)

        def recalibrate():
            # 边缘校准发现相机其实已在 F7 附近。
            camera.camera = (5, 6)
        camera.update = Mock(side_effect=recalibrate)
        camera.view.left_edge = True
        camera.view.lower_edge = True
        self.assertEqual(camera.convert_global_to_local((5, 7)), 'grid34')

    def test_target_in_view_needs_no_swipe(self):
        camera = camera_with_view((3, 3), VIEW_9X6)
        camera.map_swipe = Mock()
        self.assertEqual(camera.convert_global_to_local((4, 3)), 'grid43')
        camera.map_swipe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
