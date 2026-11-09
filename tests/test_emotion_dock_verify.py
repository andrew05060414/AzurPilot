"""扫描船坞核对心情：读数提取、触发条件、重建基准与战役入口流程的定向回归。"""

import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from dev_tools.emotion_simulate import Clock, LuaOracle, MemoryConfig, QuietLogger, ShipSpec
import module.campaign.campaign_base as campaign_base
import module.combat.emotion as production
import module.config.config as config_source
from module.combat.emotion_state import EmotionRecoveryState
from module.exception import ScriptEnd
from module.retire.fleet_emotion import complete_fleet_minimums


def ships(prefix, emotions, level=100):
    return [{'name': f'{prefix}{i}', 'level': level, 'emotion': emotion} for i, emotion in enumerate(emotions)]


def scan(number=1, vanguard=(150, 149, 138), main=(149, 149, 140)):
    return {'vanguard': {str(number): ships('v', vanguard)}, 'main': {str(number): ships('m', main)},
            'ss': {}}


KNOWN = {f'{p}{i}' for p in 'vm' for i in range(3)}


class CompleteMinimumsTests(unittest.TestCase):
    def test_minimum_over_six_ships(self):
        self.assertEqual({1: 138}, complete_fleet_minimums(scan(), KNOWN))

    def test_incomplete_or_untrusted_fleets_are_dropped(self):
        incomplete = scan()
        incomplete['main']['1'].pop()
        unknown = scan()
        unknown['vanguard']['1'][0]['name'] = '未知'
        unreadable = scan()
        unreadable['main']['1'][1]['emotion'] = None
        duplicate = scan()
        duplicate['main']['1'][0]['name'] = 'v0'
        for name, result in [('缺一艘', incomplete), ('名字未确认', unknown),
                             ('心情读不出', unreadable), ('重名', duplicate)]:
            with self.subTest(name):
                self.assertEqual({}, complete_fleet_minimums(result, KNOWN))
        self.assertEqual({}, complete_fleet_minimums(scan(), set()))

    def test_out_of_range_emotion_is_rejected(self):
        bad = scan(main=(149, 151, 140))
        self.assertEqual({}, complete_fleet_minimums(bad, KNOWN))
        zero = scan(main=(149, 0, 140))
        self.assertEqual({1: 0}, complete_fleet_minimums(zero, KNOWN))


class DockVerifyTests(unittest.TestCase):
    def setUp(self):
        spec = ShipSpec('dormitory_floor_2', False)
        oracle = LuaOracle(spec, 40, 500_000, 0)
        self.clock = Clock(500_000, oracle)
        self.config = MemoryConfig(self.clock, spec, 40, 2, 'prevent_green_face')
        self.config.Emotion_VerifyByDock = True
        self.config.Campaign_Mode = 'normal'
        self.config.Fleet_Fleet1 = 1
        self.config.Fleet_Fleet2 = 2
        self.config.data = {}
        self.enterContext(patch.object(production, 'current_time', self.clock.now))
        self.enterContext(patch.object(config_source, 'current_time', self.clock.now))
        self.enterContext(patch.object(production, 'logger', QuietLogger()))
        self.enterContext(patch.object(config_source, 'logger', QuietLogger()))
        self.emotion = production.Emotion(self.config)

    def test_triggers_only_when_estimate_says_wait(self):
        targets = self.emotion.dock_verify_targets(8)
        self.assertEqual([(self.emotion.fleets[0], 1)], targets)
        self.config.Emotion_Fleet1Value = 150
        self.config.Emotion_Fleet1RecoveryState = EmotionRecoveryState.calibrate(
            150, self.clock.now(), 'dormitory_floor_2', False, False).export()
        self.config.Emotion_Fleet1Record = self.clock.now()
        self.assertEqual([], self.emotion.dock_verify_targets(8))

    def test_switch_and_conditions_disable_scan(self):
        self.config.Emotion_VerifyByDock = False
        self.assertEqual([], self.emotion.dock_verify_targets(8))
        self.config.Emotion_VerifyByDock = True
        self.config.Campaign_Mode = 'hard'
        self.assertEqual([], self.emotion.dock_verify_targets(8))
        self.config.Campaign_Mode = 'normal'
        self.config.Fleet_Fleet2 = 1
        self.assertEqual([], self.emotion.dock_verify_targets(8))
        self.config.Fleet_Fleet2 = 2
        self.config.Fleet_FleetOrder = 'unknown'
        self.assertEqual([], self.emotion.dock_verify_targets(8))
        self.config.Fleet_FleetOrder = 'fleet1_all_fleet2_standby'
        self.config.Emotion_Mode = 'ignore'
        self.assertEqual([], self.emotion.dock_verify_targets(8))

    def test_recent_scan_is_not_repeated(self):
        self.config.data = {'FleetInfo': {'FleetInfo': {'Record': self.clock.now() - timedelta(minutes=29)}}}
        self.assertEqual([], self.emotion.dock_verify_targets(8))
        self.config.data = {'FleetInfo': {'FleetInfo': {'Record': (self.clock.now() - timedelta(minutes=31)).isoformat()}}}
        self.assertEqual(1, len(self.emotion.dock_verify_targets(8)))
        self.config.data = {'FleetInfo': {'FleetInfo': {'Record': '2020-01-01 00:00:00'}}}
        self.assertEqual(1, len(self.emotion.dock_verify_targets(8)))

    def test_subclass_with_own_rules_is_skipped(self):
        class Special(production.Emotion):
            pass
        self.assertEqual([], Special(self.config).dock_verify_targets(8))

    def test_uncalibrated_ledger_is_scanned_without_error(self):
        self.config.Emotion_Fleet1RecoveryState = None
        self.assertEqual(1, len(self.emotion.dock_verify_targets(8)))

    def test_reading_far_above_estimate_rebuilds_baseline(self):
        targets = self.emotion.dock_verify_targets(8)
        self.assertEqual(1, self.emotion.apply_dock_readings(targets, {1: 138}))
        self.assertEqual(138, self.config.Emotion_Fleet1Value)
        self.assertEqual(self.clock.now(), self.config.Emotion_Fleet1Record)
        _, delay = self.emotion._check_reduce(8)
        self.assertFalse(delay)

    def test_reading_inside_range_changes_nothing(self):
        targets = self.emotion.dock_verify_targets(8)
        before = dict(self.config.fields)
        self.assertEqual(0, self.emotion.apply_dock_readings(targets, {1: 40}))
        self.assertEqual(before, self.config.fields)

    def test_missing_reading_keeps_estimate(self):
        targets = self.emotion.dock_verify_targets(8)
        before = dict(self.config.fields)
        self.assertEqual(0, self.emotion.apply_dock_readings(targets, {}))
        self.assertEqual(before, self.config.fields)

    def test_reading_below_estimate_also_rebuilds(self):
        self.config.Emotion_Fleet1Value = 120
        self.config.Emotion_Fleet1RecoveryState = EmotionRecoveryState.calibrate(
            120, self.clock.now(), 'dormitory_floor_2', False, False).export()
        self.config.Emotion_Fleet1Record = self.clock.now()
        targets = [(self.emotion.fleets[0], 1)]
        self.assertEqual(1, self.emotion.apply_dock_readings(targets, {1: 60}))
        self.assertEqual(60, self.config.Emotion_Fleet1Value)

    def test_uncalibrated_ledger_is_calibrated_by_reading(self):
        self.config.Emotion_Fleet1RecoveryState = None
        targets = self.emotion.dock_verify_targets(8)
        self.assertEqual(1, self.emotion.apply_dock_readings(targets, {1: 90}))
        self.emotion.update()
        self.assertEqual(90, self.emotion.fleets[0].lower)


class EntryFlowTests(unittest.TestCase):
    """`emotion_check_reduce` 的编排：不接真实设备，只核对决策与任务调度。"""

    def setUp(self):
        spec = ShipSpec('dormitory_floor_2', False)
        self.clock = Clock(500_000, LuaOracle(spec, 40, 500_000, 0))
        self.config = MemoryConfig(self.clock, spec, 40, 2, 'prevent_green_face')
        self.config.Emotion_VerifyByDock = True
        self.config.Campaign_Mode = 'normal'
        self.config.Fleet_Fleet1 = 1
        self.config.Fleet_Fleet2 = 2
        self.config.data = {}
        for module in (production, config_source):
            self.enterContext(patch.object(module, 'current_time', self.clock.now))
            self.enterContext(patch.object(module, 'logger', QuietLogger()))
        self.enterContext(patch.object(campaign_base, 'logger', QuietLogger()))
        self.campaign = SimpleNamespace(config=self.config, device=object(),
                                        emotion=production.Emotion(self.config))
        self.enter = lambda: campaign_base.CampaignBase.emotion_check_reduce(self.campaign, 8)  # type: ignore[arg-type]

    def fake_scanner(self, result):
        scanner = MagicMock()
        scanner.run.return_value = result
        scanner.last_known_names = KNOWN
        return self.enterContext(patch('module.retire.fleet_management.FleetManagement', return_value=scanner))

    def test_enough_mood_is_not_scanned(self):
        self.config.Emotion_Fleet1Value = 150
        self.config.Emotion_Fleet1RecoveryState = EmotionRecoveryState.calibrate(
            150, self.clock.now(), 'dormitory_floor_2', False, False).export()
        self.config.Emotion_Fleet1Record = self.clock.now()
        factory = self.fake_scanner(scan())
        self.enter()
        factory.assert_not_called()

    def test_wrong_estimate_is_fixed_and_task_restarts_immediately(self):
        factory = self.fake_scanner(scan())
        with self.assertRaises(ScriptEnd):
            self.enter()
        factory.assert_called_once()
        self.assertEqual(138, self.config.Emotion_Fleet1Value)
        self.assertEqual(self.clock.now().replace(microsecond=0), self.config.fields['Main.Scheduler.NextRun'])

    def test_genuinely_low_mood_still_delays_with_corrected_time(self):
        self.fake_scanner(scan(vanguard=(20, 30, 40), main=(30, 30, 30)))
        with self.assertRaises(ScriptEnd):
            self.enter()
        self.assertEqual(20, self.config.Emotion_Fleet1Value)
        self.assertGreater(self.config.fields['Main.Scheduler.NextRun'], self.clock.now())

    def test_unreadable_scan_falls_back_to_estimate_delay(self):
        bad = scan()
        bad['main']['1'][0]['emotion'] = None
        self.fake_scanner(bad)
        with self.assertRaises(ScriptEnd):
            self.enter()
        self.assertEqual(40, self.config.Emotion_Fleet1Value)
        self.assertGreater(self.config.fields['Main.Scheduler.NextRun'], self.clock.now())


if __name__ == '__main__':
    unittest.main()
