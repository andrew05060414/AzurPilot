"""物资冲刺：手动开启或石油偏高时，只调整已到期任务的顺序。"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from module.config import coin_rush
from module.config.config import AzurLaneConfig
from module.config.coin_rush import (
    activate_from_task_balancer,
    LEVEL_EMERGENCY,
    LEVEL_HIGH,
    LEVEL_NONE,
    apply_coin_rush_schedule,
    boost_rush_priority,
    emergency_food_units,
    filter_opsi_tasks,
    mark_oil_maxed,
    oil_level,
    plan_coin_rush,
    record_food_purchase,
)
from module.config.redirect_utils.utils import oil_start_redirect
from module.config.task_priority import parse_task_priority
from module.config.time_source import now as current_time

PRIORITY = (
    'Restart > OpsiCrossMonth > Commission > Tactical > Research > Dorm > Reward '
    '> OpsiExplore > Minigame > OpsiAshBeacon > OpsiDaily > OpsiShop > OpsiScheduling '
    '> OpsiAbyssal > Hard > Event > Main > Main2'
)


class Func:
    def __init__(self, command, next_run=None):
        self.command = command
        self.enable = True
        self.next_run = next_run


class DummyConfig:
    def __init__(self, oil=0, coin=0, record=None, **settings):
        self.config_name = 'rush-test'
        self.data = {
            'Dashboard': {
                'Oil': {'Value': oil, 'Limit': 19100, 'Record': record or datetime(2020, 1, 1)},
                'Coin': {'Value': coin, 'Limit': 0, 'Record': datetime(2020, 1, 1)},
            },
        }
        self.settings = {
            'Alas.CoinRush.AutoOnHighOil': True,
            'Alas.CoinRush.StartOil': 22000,
            'Alas.CoinRush.EmergencyOil': 23500,
            'Alas.CoinRush.HardLimit': 25000,
            'Alas.CoinRush.EmergencyFood': False,
            'Alas.CoinRush.EmergencyFoodOil': 1000,
            'Alas.CoinRush.Enable': False,
            'Alas.CoinRush.FarmingTask': 'auto',
            'Alas.CoinRush.TargetCoins': 0,
            'Alas.CoinRush.BalancerTargetCoins': 0,
            'Alas.CoinRush.MinOil': 300,
            'Alas.CoinRush.OpsiPolicy': 'suppress',
        }
        self.settings.update({f'Alas.{k.replace("_", ".", 1)}': v for k, v in settings.items()})
        self.modified = {}
        self.saved = False

    def cross_get(self, keys, default=None):
        return self.settings.get(keys, default)

    def cross_set(self, keys, value):
        self.settings[keys] = value

    def save(self):
        self.saved = True

    def set_oil(self, oil, record=None):
        self.data['Dashboard']['Oil']['Value'] = oil
        if record is not None:
            self.data['Dashboard']['Oil']['Record'] = record


def commands(funcs):
    return [f.command for f in funcs]


class RushTestCase(unittest.TestCase):
    def setUp(self):
        coin_rush.reset_state()
        self.addCleanup(coin_rush.reset_state)
        self.enterContext(patch('module.config.coin_rush.logger'))


class TestOilLevel(RushTestCase):
    def test_levels_by_absolute_oil_not_natural_cap(self):
        # 自然恢复上限 19100 不是溢出线：20000 石油不应触发
        for oil, expected in ((20000, LEVEL_NONE), (22000, LEVEL_HIGH), (23500, LEVEL_EMERGENCY)):
            with self.subTest(oil=oil):
                coin_rush.reset_state()
                self.assertEqual(oil_level(DummyConfig(oil=oil)), expected)

    def test_hysteresis_keeps_consuming_until_1000_below_start(self):
        config = DummyConfig(oil=22100)
        self.assertEqual(oil_level(config), LEVEL_HIGH)
        config.set_oil(21200)
        self.assertEqual(oil_level(config), LEVEL_HIGH)
        config.set_oil(20999)
        self.assertEqual(oil_level(config), LEVEL_NONE)
        config.set_oil(21500)
        self.assertEqual(oil_level(config), LEVEL_NONE)

    def test_lines_are_capped_by_hard_limit(self):
        config = DummyConfig(oil=20000, CoinRush_HardLimit=20000,
                             CoinRush_StartOil=30000, CoinRush_EmergencyOil=30000)
        self.assertEqual(oil_level(config), LEVEL_EMERGENCY)

    def test_disabled(self):
        self.assertEqual(oil_level(DummyConfig(oil=24000, CoinRush_AutoOnHighOil=False)), LEVEL_NONE)

    def test_commission_oil_maxed_is_emergency_until_dashboard_refreshes(self):
        config = DummyConfig(oil=15000, record=datetime(2020, 1, 1))
        mark_oil_maxed(config)
        self.assertEqual(oil_level(config), LEVEL_EMERGENCY)
        # 出击后顶栏重新识别，按实际数值判断
        config.set_oil(15000, record=datetime.now() + timedelta(seconds=5))
        self.assertEqual(oil_level(config), LEVEL_NONE)


class TestPlan(RushTestCase):
    def test_no_rush_when_oil_normal_and_coin_rush_off(self):
        self.assertIsNone(plan_coin_rush(DummyConfig(oil=10000)))

    def test_preferred_oil_task_goes_first(self):
        plan = plan_coin_rush(DummyConfig(oil=22500, CoinRush_FarmingTask='Event'))
        self.assertEqual(plan.tasks[:2], ['Event', 'Main'])

    def test_coin_rush_with_low_oil_keeps_opsi_policy_but_no_sortie_priority(self):
        plan = plan_coin_rush(DummyConfig(oil=100, CoinRush_Enable=True))
        self.assertTrue(plan.coin_rush)
        self.assertEqual(plan.tasks, [])

    def test_coin_rush_auto_disables_at_target(self):
        config = DummyConfig(oil=5000, coin=150000, CoinRush_Enable=True, CoinRush_TargetCoins=150000)
        self.assertIsNone(plan_coin_rush(config))
        self.assertFalse(config.settings['Alas.CoinRush.Enable'])
        self.assertTrue(config.saved)

    def test_coin_rush_and_oil_high_merge_candidates(self):
        plan = plan_coin_rush(DummyConfig(oil=22500, CoinRush_Enable=True))
        self.assertEqual(plan.tasks[0], 'Main2')
        self.assertEqual(len(plan.tasks), len(set(plan.tasks)))
        self.assertIn('Raid', plan.tasks)


class TestApplyCoinRushSchedule(RushTestCase):
    def test_waiting_task_is_never_pulled_forward(self):
        # 9/22 实机：Main 因作战委托占用关卡延后 30 分钟，旧逻辑把它提到队首，
        # 调度器在队首空等，Minigame / Hard / 大世界日常被堵 166 分钟。
        future = current_time() + timedelta(minutes=30)
        pending = [Func('Minigame'), Func('Hard'), Func('OpsiAshBeacon'), Func('OpsiDaily')]
        waiting = [Func('Main', future)]
        new_pending, new_waiting, _ = apply_coin_rush_schedule(
            DummyConfig(oil=22500), pending, waiting, PRIORITY)

        self.assertNotIn('Main', commands(new_pending))
        self.assertEqual(commands(new_waiting), ['Main'])
        self.assertEqual(commands(new_pending), ['Minigame', 'Hard'])

    def test_due_sortie_runs_after_light_dailies_and_before_opsi(self):
        order = parse_task_priority(boost_rush_priority(PRIORITY, ['Main', 'Event']))
        self.assertLess(order.index('Reward'), order.index('Main'))
        self.assertLess(order.index('Main'), order.index('Event'))
        self.assertLess(order.index('Event'), order.index('Research'))
        self.assertLess(order.index('Event'), order.index('OpsiExplore'))

    def test_only_enabled_candidates_are_boosted(self):
        _, _, priority = apply_coin_rush_schedule(
            DummyConfig(oil=22500), [Func('Event'), Func('Research')], [], PRIORITY)
        order = parse_task_priority(priority)
        self.assertLess(order.index('Event'), order.index('Research'))
        # 未启用的 Main 保持原来的位置
        self.assertGreater(order.index('Main'), order.index('Research'))

    def test_emergency_food_moves_dorm_first(self):
        order = parse_task_priority(boost_rush_priority(PRIORITY, ['Main'], emergency_food=True))
        self.assertEqual(order[:2], ['Restart', 'Dorm'])

    def test_normal_oil_leaves_queue_untouched(self):
        pending = [Func('OpsiExplore'), Func('Main')]
        result = apply_coin_rush_schedule(DummyConfig(oil=15000), pending, [], PRIORITY)
        self.assertEqual(commands(result[0]), ['OpsiExplore', 'Main'])
        self.assertEqual(result[2], PRIORITY)

    def test_logs_only_when_state_changes(self):
        config = DummyConfig(oil=22500)
        with patch('module.config.coin_rush.logger') as log:
            for _ in range(5):
                apply_coin_rush_schedule(config, [Func('Main')], [], PRIORITY)
            self.assertEqual(log.info.call_count, 1)
            config.set_oil(10000)
            apply_coin_rush_schedule(config, [Func('Main')], [], PRIORITY)
            apply_coin_rush_schedule(config, [Func('Main')], [], PRIORITY)
            self.assertEqual(log.info.call_count, 2)


class TestOpsiPolicy(RushTestCase):
    PENDING = ['OpsiCrossMonth', 'OpsiDaily', 'OpsiShop', 'OpsiVoucher',
               'OpsiExplore', 'OpsiScheduling', 'OpsiHazard1Leveling', 'Main']

    def filtered(self, policy):
        return commands(filter_opsi_tasks([Func(c) for c in self.PENDING], policy))

    def test_suppress_keeps_only_month_reset(self):
        self.assertEqual(self.filtered('suppress'), ['OpsiCrossMonth', 'Main'])

    def test_quick_only_keeps_short_tasks(self):
        self.assertEqual(self.filtered('quick_only'),
                         ['OpsiCrossMonth', 'OpsiDaily', 'OpsiShop', 'OpsiVoucher', 'Main'])

    def test_idle_only_keeps_all(self):
        self.assertEqual(self.filtered('idle_only'), self.PENDING)


class TestEmergencyFood(RushTestCase):
    def test_off_by_default(self):
        self.assertEqual(emergency_food_units(DummyConfig(oil=24500)), 0)

    def test_buys_food_worth_configured_oil_only_in_emergency(self):
        self.assertEqual(emergency_food_units(DummyConfig(oil=24500, CoinRush_EmergencyFood=True)), 20)
        coin_rush.reset_state()
        self.assertEqual(emergency_food_units(DummyConfig(oil=22500, CoinRush_EmergencyFood=True)), 0)

    def test_purchase_lowers_dashboard_oil(self):
        config = DummyConfig(oil=24500)
        with (
            patch('module.log_res.log_res.LogRes.groups', {'Oil': {}}),
            patch('module.log_res.log_res.LogRes._record_all_resource_snapshot'),
        ):
            record_food_purchase(config, 20)
        self.assertEqual(config.modified['Dashboard.Oil.Value'], 23500)
        self.assertIn('Dashboard.Oil.Record', config.modified)


class TestGetNextTaskIntegration(RushTestCase):
    """通过真实的 AzurLaneConfig.get_next_task 验证调度器不会在队首空等。"""

    def make_config(self, oil):
        now = current_time()
        config = DummyConfig(oil=oil)
        config.hoarding = timedelta(0)
        config.SCHEDULER_PRIORITY = PRIORITY
        config.pending_task = []
        config.waiting_task = []
        for command, next_run in (
                ('Minigame', now - timedelta(minutes=5)),
                ('OpsiExplore', now - timedelta(minutes=5)),
                ('Main', now + timedelta(minutes=30)),
                ('Restart', now + timedelta(hours=6)),
        ):
            config.data[command] = {'Scheduler': {'Enable': True, 'NextRun': next_run, 'Command': command}}
        config.settings['OpsiGeneral.OpsiGeneral.Enable'] = True
        config.get_next_task = lambda: AzurLaneConfig.get_next_task(config)
        return config

    def test_oil_high_runs_due_task_instead_of_waiting_for_sortie(self):
        config = self.make_config(oil=22500)
        task = AzurLaneConfig.get_next(config)
        self.assertEqual(task.command, 'Minigame')
        self.assertEqual(commands(config.pending_task), ['Minigame'])
        self.assertEqual(commands(config.waiting_task), ['Main', 'Restart'])


class TestTaskBalancerActivation(RushTestCase):
    def make_config(self, enabled=('Main', 'Event'), **settings):
        config = DummyConfig(**settings)
        config.called = []
        config.is_task_enabled = lambda task: task in enabled
        config.task_call = config.called.append
        return config

    def test_enables_rush_with_separate_exit_line_and_calls_main(self):
        config = self.make_config(CoinRush_TargetCoins=0)
        self.assertEqual(activate_from_task_balancer(config, 10000), 'Main')
        self.assertTrue(config.settings['Alas.CoinRush.Enable'])
        self.assertEqual(config.settings['Alas.CoinRush.BalancerTargetCoins'], 10000)
        # 不改用户设置的目标物资
        self.assertEqual(config.settings['Alas.CoinRush.TargetCoins'], 0)
        self.assertEqual(config.called, ['Main'])

    def test_manual_rush_keeps_its_own_target(self):
        config = self.make_config(CoinRush_Enable=True, CoinRush_TargetCoins=150000)
        activate_from_task_balancer(config, 10000)
        self.assertEqual(config.settings['Alas.CoinRush.BalancerTargetCoins'], 0)

    def test_manual_disable_clears_balancer_exit_line(self):
        config = self.make_config()
        activate_from_task_balancer(config, 10000)
        config.settings['Alas.CoinRush.Enable'] = False
        plan_coin_rush(config)
        self.assertEqual(config.settings['Alas.CoinRush.BalancerTargetCoins'], 0)

    def test_never_calls_event_back(self):
        # 物资过低是刷活动时触发的，指定了活动图也不能呼叫回去
        config = self.make_config(enabled=('Event', 'Event2'), CoinRush_FarmingTask='Event')
        self.assertIsNone(activate_from_task_balancer(config, 10000))
        self.assertEqual(config.called, [])
        self.assertTrue(config.settings['Alas.CoinRush.Enable'])

    def test_rush_turns_off_once_coins_reach_balancer_limit(self):
        config = self.make_config(oil=5000, coin=3000)
        activate_from_task_balancer(config, 10000)
        self.assertIsNotNone(plan_coin_rush(config))
        config.data['Dashboard']['Coin']['Value'] = 10000
        self.assertIsNone(plan_coin_rush(config))
        self.assertFalse(config.settings['Alas.CoinRush.Enable'])


class TestMigration(unittest.TestCase):
    def test_old_threshold(self):
        self.assertEqual(oil_start_redirect(0), 22000)
        self.assertEqual(oil_start_redirect(18000), 18000)
        self.assertEqual(oil_start_redirect('bad'), 22000)


if __name__ == '__main__':
    unittest.main()
