import unittest
from types import SimpleNamespace

from module.config.oil_overflow import (
    LEVEL_ALERT,
    LEVEL_NONE,
    LEVEL_THRESHOLD,
    OIL_DUMP_TASKS,
    apply_oil_overflow_schedule,
    boost_oil_dump_priority,
    oil_overflow_level,
    promote_dump_task,
    resolve_oil_dump_task,
    should_dump_oil,
    try_handle_oil_maxed,
)
from module.config.task_priority import parse_task_priority


class DummyFunc:
    def __init__(self, command):
        self.command = command

    def __repr__(self):
        return f'DummyFunc({self.command})'


class DummyConfig:
    def __init__(
        self,
        enable=True,
        task_call='auto',
        threshold=0,
        alert=300,
        reserve=2000,
        oil=0,
        limit=0,
        enabled_tasks=(),
        forced=False,
        active=False,
    ):
        self.data = {
            'Dashboard': {
                'Oil': {
                    'Value': oil,
                    'Limit': limit,
                }
            }
        }
        self._oil_overflow = {
            'Enable': enable,
            'TaskCall': task_call,
            'Threshold': threshold,
            'Alert': alert,
            'Reserve': reserve,
        }
        self._enabled_tasks = set(enabled_tasks)
        self._oil_overflow_forced = forced
        self._oil_overflow_active = active

    def cross_get(self, keys, default=None):
        mapping = {
            'Alas.OilOverflow.Enable': self._oil_overflow['Enable'],
            'Alas.OilOverflow.TaskCall': self._oil_overflow['TaskCall'],
            'Alas.OilOverflow.Threshold': self._oil_overflow['Threshold'],
            'Alas.OilOverflow.Alert': self._oil_overflow['Alert'],
            'Alas.OilOverflow.Reserve': self._oil_overflow['Reserve'],
        }
        return mapping.get(keys, default)

    def is_task_enabled(self, task):
        return task in self._enabled_tasks


class TestShouldDumpOil(unittest.TestCase):
    def test_at_cap_starts_dump(self):
        self.assertTrue(should_dump_oil(oil=25000, limit=25000, reserve=2000))

    def test_near_cap_starts_dump(self):
        self.assertTrue(should_dump_oil(oil=24750, limit=25000, reserve=2000))

    def test_far_from_cap_does_not_dump(self):
        self.assertFalse(should_dump_oil(oil=20000, limit=25000, reserve=2000))

    def test_hysteresis_keeps_dumping_until_reserve(self):
        self.assertTrue(
            should_dump_oil(oil=24000, limit=25000, reserve=2000, active=True)
        )
        self.assertFalse(
            should_dump_oil(oil=22500, limit=25000, reserve=2000, active=True)
        )

    def test_forced_dump_without_limit(self):
        self.assertTrue(should_dump_oil(oil=0, limit=0, reserve=2000, forced=True))
        self.assertFalse(should_dump_oil(oil=0, limit=0, reserve=2000, forced=False))

    def test_forced_dump_overrides_stale_headroom(self):
        self.assertTrue(
            should_dump_oil(oil=20000, limit=25000, reserve=2000, forced=True)
        )

    def test_active_keeps_dumping_when_limit_unknown(self):
        self.assertTrue(should_dump_oil(oil=0, limit=0, reserve=2000, active=True))

    def test_threshold_starts_dump_before_cap(self):
        self.assertEqual(
            oil_overflow_level(oil=21000, limit=25000, threshold=20000, alert=300),
            LEVEL_THRESHOLD,
        )
        self.assertEqual(
            oil_overflow_level(oil=19999, limit=25000, threshold=20000, alert=300),
            LEVEL_NONE,
        )

    def test_alert_remaining_starts_dump(self):
        self.assertEqual(
            oil_overflow_level(oil=24750, limit=25000, threshold=0, alert=500),
            LEVEL_ALERT,
        )
        self.assertEqual(
            oil_overflow_level(oil=24000, limit=25000, threshold=0, alert=500),
            LEVEL_NONE,
        )

    def test_alert_outranks_threshold_near_cap(self):
        self.assertEqual(
            oil_overflow_level(oil=24800, limit=25000, threshold=20000, alert=300),
            LEVEL_ALERT,
        )

    def test_threshold_hysteresis_stops_below_quota(self):
        self.assertTrue(
            should_dump_oil(oil=20500, limit=25000, reserve=2000, threshold=20000, active=True)
        )
        self.assertFalse(
            should_dump_oil(oil=19000, limit=25000, reserve=2000, threshold=20000, active=True)
        )


class TestResolveOilDumpTask(unittest.TestCase):
    def test_auto_picks_first_enabled_main(self):
        self.assertEqual(
            resolve_oil_dump_task('auto', ['OpsiExplore', 'Main', 'Event']),
            'Main',
        )

    def test_auto_skips_disabled_main(self):
        self.assertEqual(
            resolve_oil_dump_task('auto', ['Event', 'OpsiExplore']),
            'Event',
        )

    def test_explicit_task_call(self):
        self.assertEqual(
            resolve_oil_dump_task('GemsFarming', ['Main', 'GemsFarming']),
            'GemsFarming',
        )

    def test_explicit_fallback_when_disabled(self):
        self.assertEqual(
            resolve_oil_dump_task('Main', ['Event']),
            'Event',
        )

    def test_no_dump_task(self):
        self.assertIsNone(resolve_oil_dump_task('auto', ['OpsiExplore']))


class TestBoostOilDumpPriority(unittest.TestCase):
    def test_inserts_main_before_opsi_explore(self):
        priority = (
            'Restart\n> OpsiCrossMonth\n> Commission\n> OpsiExplore\n'
            '> OpsiDaily\n> Main\n> OpsiMeowfficerFarming'
        )
        boosted = parse_task_priority(boost_oil_dump_priority(priority, 'Main'))
        self.assertLess(boosted.index('Commission'), boosted.index('Main'))
        self.assertLess(boosted.index('Main'), boosted.index('OpsiExplore'))
        self.assertLess(boosted.index('OpsiCrossMonth'), boosted.index('Main'))

    def test_inserts_main_even_if_missing_from_priority(self):
        priority = 'Restart\n> Commission\n> OpsiExplore'
        boosted = parse_task_priority(boost_oil_dump_priority(priority, 'Main'))
        self.assertLess(boosted.index('Commission'), boosted.index('Main'))
        self.assertLess(boosted.index('Main'), boosted.index('OpsiExplore'))

    def test_empty_dump_task_keeps_priority(self):
        priority = 'Restart\n> OpsiExplore\n> Main'
        self.assertEqual(boost_oil_dump_priority(priority, ''), priority)


class TestPromoteAndApply(unittest.TestCase):
    def test_promote_waiting_main(self):
        pending = [DummyFunc('OpsiExplore')]
        waiting = [DummyFunc('Main')]
        pending, waiting = promote_dump_task(pending, waiting, 'Main')
        self.assertEqual([func.command for func in pending], ['OpsiExplore', 'Main'])
        self.assertEqual(waiting, [])

    def test_apply_boosts_main_when_oil_capped(self):
        config = DummyConfig(
            oil=25000,
            limit=25000,
            enabled_tasks=['Main', 'OpsiExplore'],
        )
        pending = [DummyFunc('OpsiExplore'), DummyFunc('Main')]
        waiting = []
        priority = 'Commission\n> OpsiExplore\n> Main'
        pending, waiting, new_priority = apply_oil_overflow_schedule(
            config, pending, waiting, priority
        )
        boosted = parse_task_priority(new_priority)
        self.assertLess(boosted.index('Main'), boosted.index('OpsiExplore'))
        self.assertTrue(config._oil_overflow_active)
        self.assertFalse(config._oil_overflow_forced)

    def test_apply_threshold_promotes_main_before_opsi(self):
        config = DummyConfig(oil=21000, limit=25000, threshold=20000)
        pending = [DummyFunc('OpsiExplore')]
        waiting = [DummyFunc('Main')]
        pending, waiting, new_priority = apply_oil_overflow_schedule(
            config, pending, waiting, 'OpsiExplore\n> Main'
        )
        boosted = parse_task_priority(new_priority)
        self.assertEqual([func.command for func in pending], ['OpsiExplore', 'Main'])
        self.assertLess(boosted.index('Main'), boosted.index('OpsiExplore'))

    def test_apply_promotes_waiting_main(self):
        config = DummyConfig(oil=24900, limit=25000)
        pending = [DummyFunc('OpsiExplore')]
        waiting = [DummyFunc('Main')]
        pending, waiting, _ = apply_oil_overflow_schedule(
            config, pending, waiting, 'OpsiExplore\n> Main'
        )
        self.assertEqual([func.command for func in pending], ['OpsiExplore', 'Main'])
        self.assertEqual(waiting, [])

    def test_apply_forced_overflow_ignores_stale_headroom(self):
        config = DummyConfig(oil=20000, limit=25000, forced=True)
        pending = [DummyFunc('OpsiExplore')]
        waiting = [DummyFunc('Main')]
        pending, waiting, new_priority = apply_oil_overflow_schedule(
            config, pending, waiting, 'OpsiExplore\n> Main'
        )
        boosted = parse_task_priority(new_priority)
        self.assertEqual([func.command for func in pending], ['OpsiExplore', 'Main'])
        self.assertLess(boosted.index('Main'), boosted.index('OpsiExplore'))
        self.assertFalse(config._oil_overflow_forced)
        self.assertTrue(config._oil_overflow_active)

    def test_disabled_feature_does_nothing(self):
        config = DummyConfig(enable=False, oil=25000, limit=25000)
        pending = [DummyFunc('OpsiExplore'), DummyFunc('Main')]
        waiting = []
        priority = 'OpsiExplore\n> Main'
        new_pending, new_waiting, new_priority = apply_oil_overflow_schedule(
            config, pending, waiting, priority
        )
        self.assertEqual(new_pending, pending)
        self.assertEqual(new_waiting, waiting)
        self.assertEqual(new_priority, priority)

    def test_no_dump_task_keeps_opsi_first(self):
        config = DummyConfig(oil=25000, limit=25000)
        pending = [DummyFunc('OpsiExplore')]
        waiting = []
        priority = 'OpsiExplore'
        _, _, new_priority = apply_oil_overflow_schedule(
            config, pending, waiting, priority
        )
        self.assertEqual(new_priority, priority)


class TestTryHandleOilMaxed(unittest.TestCase):
    def test_marks_overflow_when_dump_task_enabled(self):
        config = DummyConfig(enabled_tasks=['Main'])
        self.assertTrue(try_handle_oil_maxed(config))
        self.assertTrue(config._oil_overflow_forced)
        self.assertTrue(config._oil_overflow_active)

    def test_returns_false_when_no_dump_task(self):
        config = DummyConfig(enabled_tasks=['OpsiExplore'])
        self.assertFalse(try_handle_oil_maxed(config))

    def test_returns_false_when_disabled(self):
        config = DummyConfig(enable=False, enabled_tasks=['Main'])
        self.assertFalse(try_handle_oil_maxed(config))


class TestDumpTaskCatalog(unittest.TestCase):
    def test_main_is_preferred_candidate(self):
        self.assertEqual(OIL_DUMP_TASKS[0], 'Main')


if __name__ == '__main__':
    unittest.main()
