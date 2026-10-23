import unittest
from unittest.mock import Mock
import numpy as np
from module.combat.combat import Combat
from module.exception import GameStuckError


class TestDefeatExpectedEnd(unittest.TestCase):
    def _setup_combat(self, defeated=True, in_map=True):
        config = Mock()
        config.HpControl_UseHpBalance = False
        config.GET_SHIP_TRIGGERED = False
        device = Mock()
        device.image = np.zeros((720, 1280, 3), dtype=np.uint8)
        combat = Combat(config=config, device=device)
        combat.combat_result_reset(emotion_reduce=False, fleet_index=1)
        combat.combat_result.defeated = defeated
        combat.handle_story_skip = Mock(return_value=False)
        combat.handle_get_ship = Mock(return_value=False)
        combat.handle_get_items = Mock(return_value=False)
        combat.handle_popup_confirm = Mock(return_value=False)
        combat.handle_battle_status = Mock(return_value=False)
        combat.handle_exp_info = Mock(return_value=False)
        combat.handle_urgent_commission = Mock(return_value=False)
        combat.handle_guild_popup_cancel = Mock(return_value=False)
        combat.handle_vote_popup = Mock(return_value=False)
        combat.handle_mission_popup_ack = Mock(return_value=False)
        combat.handle_auto_search_exit = Mock(return_value=False)
        combat.handle_combat_mis_click = Mock(return_value=False)
        combat.handle_combat_defeat = Mock(return_value=False)
        combat.handle_in_stage = Mock(return_value=False)
        combat.handle_in_map_with_enemy_searching = Mock(return_value=False)
        combat.handle_in_map_no_enemy_searching = Mock(return_value=False)
        combat.is_in_map = Mock(return_value=in_map)
        return combat

    def test_defeat_map_raises_immediately_on_in_stage(self):
        combat = self._setup_combat(defeated=True, in_map=True)
        combat.loop = Mock(return_value=[None])
        with self.assertRaises(GameStuckError) as cm:
            combat.combat_status(expected_end="in_stage")
        self.assertIn("Boss战败后返回地图，交由上层恢复", str(cm.exception))

    def test_victory_does_not_exit_early_on_map(self):
        combat = self._setup_combat(defeated=False, in_map=True)
        iterated = []

        def mock_loop():
            yield None
            iterated.append(True)

        combat.loop = mock_loop
        combat.combat_status(expected_end="in_stage")
        self.assertTrue(iterated[0])

    def test_other_expected_end_unchanged(self):
        combat = self._setup_combat(defeated=True, in_map=True)
        combat.handle_in_map_with_enemy_searching = Mock(return_value=True)
        combat.loop = Mock(return_value=[None])
        # with_searching normal exit unchanged
        combat.combat_status(expected_end="with_searching")
        self.assertTrue(combat.handle_in_map_with_enemy_searching.called)


if __name__ == "__main__":
    unittest.main()
