"""测试指挥喵强化流程只执行单次循环，避免重复强化。"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from module.meowfficer.enhance import MeowfficerEnhance


class DummyMeowEnhance(MeowfficerEnhance):
    def __init__(self):
        self.config = SimpleNamespace(
            MeowfficerTrain_EnhanceIndex=1,
            MeowfficerTrain_Enable=True,
        )


class TestMeowEnhanceOnce(unittest.TestCase):
    def test_non_max_result_calls_only_once(self):
        for status in ['success', 'coin_limit', 'invalid', 'in_battle']:
            with self.subTest(status=status):
                handler = DummyMeowEnhance()
                handler._meow_enhance = Mock(return_value=status)

                handler.meow_enhance()

                self.assertEqual(handler._meow_enhance.call_count, 1)
                self.assertEqual(handler.config.MeowfficerTrain_EnhanceIndex, 1)
                self.assertTrue(handler.config.MeowfficerTrain_Enable)

    def test_level_max_advances_index_until_non_max(self):
        handler = DummyMeowEnhance()
        handler.config.MeowfficerTrain_EnhanceIndex = 1
        handler._meow_enhance = Mock(side_effect=['leveled_max', 'leveled_max', 'success'])

        handler.meow_enhance()

        self.assertEqual(handler._meow_enhance.call_count, 3)
        self.assertEqual(handler.config.MeowfficerTrain_EnhanceIndex, 3)
        self.assertTrue(handler.config.MeowfficerTrain_Enable)

    def test_index_12_max_disables_training_without_second_run(self):
        handler = DummyMeowEnhance()
        handler.config.MeowfficerTrain_EnhanceIndex = 12
        handler._meow_enhance = Mock(return_value='leveled_max')

        handler.meow_enhance()

        self.assertEqual(handler._meow_enhance.call_count, 1)
        self.assertEqual(handler.config.MeowfficerTrain_EnhanceIndex, 12)
        self.assertFalse(handler.config.MeowfficerTrain_Enable)


if __name__ == '__main__':
    unittest.main()
