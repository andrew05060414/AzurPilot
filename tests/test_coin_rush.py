import unittest

from module.config.coin_rush import (
    COIN_FARMING_TASKS,
    FAST_ROUTINES_AHEAD,
    apply_coin_rush_schedule,
    boost_coin_rush_priority,
    demote_farming_task,
    filter_opsi_tasks,
    get_coin_rush_farming_task,
    get_coin_rush_min_oil,
    get_coin_rush_opsi_policy,
    get_coin_rush_target_coins,
    is_coin_rush_enabled,
    is_oil_sufficient,
    promote_farming_task,
    resolve_farming_task,
    should_exit_coin_rush,
)
from module.config.task_priority import parse_task_priority


class DummyFunc:
    def __init__(self, command):
        self.command = command

    def __repr__(self):
        return f'DummyFunc({self.command})'

    def __eq__(self, other):
        if not isinstance(other, DummyFunc):
            return False
        return self.command == other.command


class DummyConfig:
    def __init__(
        self,
        enable=True,
        farming_task='auto',
        target_coins=0,
        min_oil=300,
        opsi_policy='suppress',
        coin=0,
        oil=0,
        limit=0,
        enabled_tasks=(),
    ):
        self.data = {
            'Dashboard': {
                'Coin': {'Value': coin},
                'Oil': {'Value': oil, 'Limit': limit},
            }
        }
        self._coin_rush = {
            'Enable': enable,
            'FarmingTask': farming_task,
            'TargetCoins': target_coins,
            'MinOil': min_oil,
            'OpsiPolicy': opsi_policy,
        }
        self._enabled_tasks = set(enabled_tasks)
        self.modified = {}

    def cross_get(self, keys, default=None):
        mapping = {
            'Alas.CoinRush.Enable': self._coin_rush['Enable'],
            'Alas.CoinRush.FarmingTask': self._coin_rush['FarmingTask'],
            'Alas.CoinRush.TargetCoins': self._coin_rush['TargetCoins'],
            'Alas.CoinRush.MinOil': self._coin_rush['MinOil'],
            'Alas.CoinRush.OpsiPolicy': self._coin_rush['OpsiPolicy'],
        }
        return mapping.get(keys, default)

    def cross_set(self, keys, value):
        self.modified[keys] = value
        if keys == 'Alas.CoinRush.Enable':
            self._coin_rush['Enable'] = value

    def is_task_enabled(self, task):
        return task in self._enabled_tasks


class TestCoinRushConfig(unittest.TestCase):
    def test_getters(self):
        config = DummyConfig(
            enable=True,
            farming_task='Main2',
            target_coins=150000,
            min_oil=500,
            opsi_policy='idle_only',
        )
        self.assertTrue(is_coin_rush_enabled(config))
        self.assertEqual(get_coin_rush_farming_task(config), 'Main2')
        self.assertEqual(get_coin_rush_target_coins(config), 150000)
        self.assertEqual(get_coin_rush_min_oil(config), 500)
        self.assertEqual(get_coin_rush_opsi_policy(config), 'idle_only')


class TestCoinRushExitCondition(unittest.TestCase):
    def test_unlimited_when_zero(self):
        self.assertFalse(should_exit_coin_rush(target_coins=0, current_coin=999999))

    def test_under_target(self):
        self.assertFalse(should_exit_coin_rush(target_coins=150000, current_coin=149999))

    def test_at_target(self):
        self.assertTrue(should_exit_coin_rush(target_coins=150000, current_coin=150000))

    def test_over_target(self):
        self.assertTrue(should_exit_coin_rush(target_coins=150000, current_coin=160000))


class TestOilSufficiency(unittest.TestCase):
    def test_uninitialized_dashboard_allows_run(self):
        self.assertTrue(is_oil_sufficient(oil=0, limit=0, min_oil=300))

    def test_below_min_oil(self):
        self.assertFalse(is_oil_sufficient(oil=250, limit=25000, min_oil=300))

    def test_at_min_oil(self):
        self.assertTrue(is_oil_sufficient(oil=300, limit=25000, min_oil=300))

    def test_above_min_oil(self):
        self.assertTrue(is_oil_sufficient(oil=5000, limit=25000, min_oil=300))


class TestResolveFarmingTask(unittest.TestCase):
    def test_auto_picks_main2_first(self):
        task = resolve_farming_task('auto', ['Main', 'Main2', 'OpsiExplore'])
        self.assertEqual(task, 'Main2')

    def test_auto_picks_main_when_main2_disabled(self):
        task = resolve_farming_task('auto', ['Main', 'OpsiExplore'])
        self.assertEqual(task, 'Main')

    def test_user_preferred_task(self):
        task = resolve_farming_task('Main3', ['Main2', 'Main3', 'Event'])
        self.assertEqual(task, 'Main3')

    def test_user_preferred_task_fallback(self):
        task = resolve_farming_task('Event', ['Main2'])
        self.assertEqual(task, 'Main2')

    def test_no_task_available(self):
        task = resolve_farming_task('auto', ['OpsiExplore', 'Dorm'])
        self.assertIsNone(task)


class TestBoostPriority(unittest.TestCase):
    def test_fast_routines_stay_ahead_and_heavy_tasks_fall_behind(self):
        priority = (
            'Restart\n'
            '> OpsiCrossMonth\n'
            '> Commission > Tactical > Research\n'
            '> Exercise\n'
            '> Dorm > Meowfficer > Guild > Gacha\n'
            '> Reward\n'
            '> ShopFrequent > EventShop\n'
            '> OpsiDaily\n'
            '> IslandFarm\n'
            '> Main > Main2 > Main3'
        )
        new_priority = boost_coin_rush_priority(priority, 'Main2')
        tasks = parse_task_priority(new_priority)

        # 验证轻量日常全部在 Main2 之前
        for routine in ['Restart', 'OpsiCrossMonth', 'Commission', 'Tactical', 'Dorm', 'Guild', 'Reward']:
            self.assertIn(routine, tasks)
            self.assertLess(
                tasks.index(routine),
                tasks.index('Main2'),
                f'{routine} 应该在 Main2 之前执行',
            )

        # 验证耗时较长、非物资任务排在 Main2 之后
        for heavy in ['Research', 'Exercise', 'Meowfficer', 'Gacha', 'ShopFrequent', 'OpsiDaily', 'IslandFarm']:
            self.assertIn(heavy, tasks)
            self.assertGreater(
                tasks.index(heavy),
                tasks.index('Main2'),
                f'{heavy} 应该在 Main2 之后执行',
            )


class TestQueueManipulation(unittest.TestCase):
    def test_promote_farming_task(self):
        pending = [DummyFunc('Commission')]
        waiting = [DummyFunc('Main2'), DummyFunc('Reward')]
        p, w = promote_farming_task(pending, waiting, 'Main2')
        self.assertEqual([f.command for f in p], ['Commission', 'Main2'])
        self.assertEqual([f.command for f in w], ['Reward'])

    def test_demote_farming_task(self):
        pending = [DummyFunc('Commission'), DummyFunc('Main2')]
        waiting = [DummyFunc('Reward')]
        p, w = demote_farming_task(pending, waiting, 'Main2')
        self.assertEqual([f.command for f in p], ['Commission'])
        self.assertEqual([f.command for f in w], ['Reward', 'Main2'])


class TestFilterOpsiTasks(unittest.TestCase):
    def test_suppress_removes_opsi_except_cross_month(self):
        pending = [
            DummyFunc('OpsiCrossMonth'),
            DummyFunc('OpsiDaily'),
            DummyFunc('OpsiExplore'),
            DummyFunc('Commission'),
        ]
        result = filter_opsi_tasks(pending, 'suppress')
        self.assertEqual([f.command for f in result], ['OpsiCrossMonth', 'Commission'])

    def test_idle_only_keeps_all_opsi(self):
        pending = [DummyFunc('OpsiDaily'), DummyFunc('Commission')]
        result = filter_opsi_tasks(pending, 'idle_only')
        self.assertEqual([f.command for f in result], ['OpsiDaily', 'Commission'])


class TestApplyCoinRushSchedule(unittest.TestCase):
    def test_disabled_does_nothing(self):
        config = DummyConfig(enable=False)
        pending = [DummyFunc('Commission')]
        waiting = [DummyFunc('Main2')]
        priority = 'Commission\n> Main2'
        p, w, pr = apply_coin_rush_schedule(config, pending, waiting, priority)
        self.assertEqual(p, pending)
        self.assertEqual(w, waiting)
        self.assertEqual(pr, priority)

    def test_target_reached_auto_exits(self):
        config = DummyConfig(
            enable=True,
            target_coins=150000,
            coin=152000,
        )
        pending = [DummyFunc('Commission')]
        waiting = [DummyFunc('Main2')]
        priority = 'Commission\n> Main2'
        p, w, pr = apply_coin_rush_schedule(config, pending, waiting, priority)
        self.assertEqual(config.modified.get('Alas.CoinRush.Enable'), False)
        self.assertEqual(p, pending)
        self.assertEqual(w, waiting)
        self.assertEqual(pr, priority)

    def test_sufficient_oil_promotes_and_boosts_main2(self):
        config = DummyConfig(
            enable=True,
            farming_task='Main2',
            oil=2000,
            limit=25000,
            min_oil=300,
            opsi_policy='suppress',
        )
        pending = [DummyFunc('Commission'), DummyFunc('OpsiDaily')]
        waiting = [DummyFunc('Main2')]
        priority = 'Restart\n> Commission\n> Tactical\n> Dorm\n> Reward\n> Guild\n> OpsiDaily\n> Main2'

        p, w, pr = apply_coin_rush_schedule(config, pending, waiting, priority)
        # OpsiDaily 被 suppress 过滤，Main2 从 waiting 提升到 pending
        self.assertEqual([f.command for f in p], ['Commission', 'Main2'])
        self.assertEqual(w, [])
        tasks = parse_task_priority(pr)
        self.assertLess(tasks.index('Main2'), tasks.index('OpsiDaily'))

    def test_insufficient_oil_demotes_main2(self):
        config = DummyConfig(
            enable=True,
            farming_task='Main2',
            oil=150,
            limit=25000,
            min_oil=300,
            opsi_policy='suppress',
        )
        pending = [DummyFunc('Commission'), DummyFunc('Main2')]
        waiting = []
        priority = 'Commission\n> Main2'

        p, w, pr = apply_coin_rush_schedule(config, pending, waiting, priority)
        # Main2 石油不足被降级到 waiting
        self.assertEqual([f.command for f in p], ['Commission'])
        self.assertEqual([f.command for f in w], ['Main2'])


if __name__ == '__main__':
    unittest.main()
