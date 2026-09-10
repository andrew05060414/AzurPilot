import unittest
from datetime import datetime, timedelta

from module.config.config import AzurLaneConfig, Function
from module.config.time_source import now as current_time


class DummyTaskConfig:
    def __init__(self, master_enable=True, opsi_sub_enable=True, main_enable=True):
        now = current_time()
        past = now - timedelta(minutes=10)
        self.hoarding = timedelta(0)
        self.SCHEDULER_PRIORITY = 'Restart > OpsiDaily > OpsiExplore > Main > Main2'
        self.pending_task = []
        self.waiting_task = []
        self._master_enable = master_enable

        self.data = {
            'OpsiGeneral': {
                'OpsiGeneral': {
                    'Enable': master_enable,
                }
            },
            'OpsiDaily': {
                'Scheduler': {
                    'Enable': opsi_sub_enable,
                    'NextRun': past,
                    'Command': 'OpsiDaily',
                }
            },
            'OpsiExplore': {
                'Scheduler': {
                    'Enable': opsi_sub_enable,
                    'NextRun': past,
                    'Command': 'OpsiExplore',
                }
            },
            'Main': {
                'Scheduler': {
                    'Enable': main_enable,
                    'NextRun': past,
                    'Command': 'Main',
                }
            },
        }

    def cross_get(self, keys, default=None):
        if keys == 'OpsiGeneral.OpsiGeneral.Enable':
            return self._master_enable
        if keys.startswith('Alas.OilOverflow.'):
            return False
        return default


class TestOpsiMasterSwitch(unittest.TestCase):
    def test_opsi_master_switch_enabled(self):
        cfg = DummyTaskConfig(master_enable=True, opsi_sub_enable=True, main_enable=True)
        AzurLaneConfig.get_next_task(cfg)
        commands = [f.command for f in cfg.pending_task]
        self.assertIn('OpsiDaily', commands)
        self.assertIn('OpsiExplore', commands)
        self.assertIn('Main', commands)

    def test_opsi_master_switch_disabled_filters_all_opsi(self):
        cfg = DummyTaskConfig(master_enable=False, opsi_sub_enable=True, main_enable=True)
        AzurLaneConfig.get_next_task(cfg)
        commands = [f.command for f in cfg.pending_task]
        # 所有 Opsi 任务被全局跳过
        self.assertNotIn('OpsiDaily', commands)
        self.assertNotIn('OpsiExplore', commands)
        # 主线任务不受影响正常调度
        self.assertIn('Main', commands)
        # 子开关数据未被触碰，仍然是 True
        self.assertTrue(cfg.data['OpsiDaily']['Scheduler']['Enable'])
        self.assertTrue(cfg.data['OpsiExplore']['Scheduler']['Enable'])

    def test_opsi_master_switch_toggle_recovery(self):
        cfg = DummyTaskConfig(master_enable=False, opsi_sub_enable=True, main_enable=True)
        AzurLaneConfig.get_next_task(cfg)
        self.assertNotIn('OpsiDaily', [f.command for f in cfg.pending_task])

        # 恢复开启总开关
        cfg._master_enable = True
        AzurLaneConfig.get_next_task(cfg)
        commands = [f.command for f in cfg.pending_task]
        self.assertIn('OpsiDaily', commands)
        self.assertIn('OpsiExplore', commands)


if __name__ == '__main__':
    unittest.main()
