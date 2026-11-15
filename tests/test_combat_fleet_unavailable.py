"""手动出击战败后「舰队无法成型」弹窗：结算循环必须点击确认，不得停在弹窗上。"""

import unittest
from pathlib import Path
from unittest.mock import patch

import module.config.server as server

server.server = 'cn'

from module.base.utils import load_image
from module.combat.assets import OPTS_INFO_D
from module.combat.combat import Combat
from module.handler.assets import FLEET_UNAVAILABLE
from tests.test_auto_search_defeat import screen
from tests.test_combat_results import PixelReplay

FIXTURES = Path(__file__).parent / 'fixtures' / 'combat_defeat'


class CombatFleetUnavailableTests(unittest.TestCase):
    def test_fixture_is_only_the_fleet_unavailable_popup(self):
        replay = PixelReplay([load_image(str(FIXTURES / 'fleet_unavailable.png'))])
        campaign = replay.make_campaign()
        self.assertTrue(campaign.appear(FLEET_UNAVAILABLE, offset=(160, 30)))
        self.assertFalse(campaign.appear(OPTS_INFO_D, offset=(30, 30)))

    def test_combat_status_clicks_popup_confirm_and_records_result(self):
        replay = PixelReplay([load_image(str(FIXTURES / 'fleet_unavailable.png')), screen('STAGE')])
        campaign = replay.make_campaign()
        campaign.combat_result_reset(emotion_reduce=False, fleet_index=1)
        self.assertIs(type(campaign).handle_fleet_unavailable_popup, Combat.handle_fleet_unavailable_popup)

        with patch('module.base.timer.time', side_effect=lambda: replay.time):
            campaign.combat_status(expected_end=lambda: 'STAGE' in replay.device.image)

        self.assertEqual(replay.clicks, [FLEET_UNAVAILABLE.name])
        self.assertTrue(campaign.combat_result.fleet_unavailable)
        self.assertTrue(campaign.combat_result.defeated)

    def test_popup_click_uses_fixed_button_area(self):
        replay = PixelReplay([load_image(str(FIXTURES / 'fleet_unavailable.png'))])
        campaign = replay.make_campaign()
        click_areas = []
        replay.device.click.side_effect = lambda button: (replay.click(button), click_areas.append(button.button))
        with patch('module.base.timer.time', side_effect=lambda: replay.time):
            self.assertTrue(campaign.handle_fleet_unavailable_popup())
            # 点击冷却内再次看到同一弹窗：仍要让出本帧，但不重复点击。
            self.assertTrue(campaign.handle_fleet_unavailable_popup())
        self.assertEqual(replay.clicks, [FLEET_UNAVAILABLE.name])
        # 正文偏移不能带动按钮：点击瞬间必须是固定区域。
        self.assertEqual([tuple(int(v) for v in a) for a in click_areas], [(553, 481, 728, 541)])

    def test_no_popup_returns_false_without_click(self):
        replay = PixelReplay([screen('STAGE')])
        campaign = replay.make_campaign()
        self.assertFalse(campaign.handle_fleet_unavailable_popup())
        self.assertEqual(replay.clicks, [])


if __name__ == '__main__':
    unittest.main()
