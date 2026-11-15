"""指挥喵分拣规则测试（纯逻辑）。样例取自 2026-11-09 实机扫描的真实天赋组合。"""

import unittest

from module.meowfficer.triage import (CatRecord, FEED, KEEP_EMBRYO, KEEP_IN_USE, KEEP_STOCK,
                                      assess, triage)


def rec(name, *talents, level=None):
    return CatRecord(name=name, talents=list(talents), level=level)


class TestAssess(unittest.TestCase):
    """胚子 = 清一色 / 火或山 / 本舰种精英 三条满足两条。"""

    def test_fire_plus_elite_is_ideal_embryo(self):
        v = assess(rec('毗沙丸', '侵略如火', '王牌机师', '精锐指挥官·重樱'))
        self.assertEqual(v.action, KEEP_EMBRYO)

    def test_fire_with_clean_normals_is_embryo(self):
        self.assertEqual(assess(rec('毗沙丸', '侵略如火', '轮机手·空母', '精锐指挥官·空母')).action, KEEP_EMBRYO)

    def test_single_special_with_junk_is_not_enough(self):
        # 实机：火 + 重樱阵营天赋 —— 只满足一条
        self.assertEqual(assess(rec('毗沙丸', '轮机手·空母', '侵略如火', '精锐指挥官·重樱')).action, FEED)
        self.assertEqual(assess(rec('约翰喵', '新晋指挥官·战列', '新晋指挥官·皇家', '不动如山')).action, FEED)
        self.assertEqual(assess(rec('克雷喵', '新手观测士·潜艇', '新晋指挥官·铁血', '狼群之首')).action, FEED)

    def test_clean_alone_is_not_enough(self):
        self.assertEqual(assess(rec('伯克喵', '熟练炮手·驱逐', '精锐指挥官·驱逐')).action, FEED)

    def test_elite_of_other_ship_type_is_junk(self):
        v = assess(rec('约翰喵', '王牌机师', '侵略如火', '轮机手·战列'))
        self.assertIn('王牌机师', v.junk)
        self.assertEqual(v.action, FEED)

    def test_purple_same_rule(self):
        self.assertEqual(assess(rec('莫里喵', '不动如山', '航空新兵·空母')).action, KEEP_EMBRYO)
        self.assertEqual(assess(rec('莫里喵', '不动如山', '声纳兵·先锋')).action, FEED)

    def test_blue_is_always_feed(self):
        self.assertEqual(assess(rec('谢尔喵', '侵略如火', '航空新兵·空母')).action, FEED)

    def test_leveled_and_unreadable_cats_are_left_alone(self):
        self.assertEqual(assess(rec('奥古喵', '炮击新手·主力', level=12)).action, KEEP_IN_USE)
        self.assertEqual(assess(rec('奥古喵', '完全不认识的天赋')).action, KEEP_IN_USE)

    def test_fire_scores_above_mountain(self):
        fire = assess(rec('约翰喵', '侵略如火', '轮机手·战列'))
        mountain = assess(rec('约翰喵', '不动如山', '轮机手·战列'))
        self.assertGreater(fire.score, mountain.score)


class TestTriage(unittest.TestCase):
    def test_gold_stock_keeps_best_four_per_species(self):
        cats = [rec('伯克喵', '熟练炮手·驱逐', '精锐指挥官·驱逐')] * 3 + \
               [rec('伯克喵', '新晋指挥官·白鹰', '新手观测士·先锋', '熟练雷击士·驱逐')] * 3
        verdicts = triage(cats)
        actions = [v.action for v in verdicts]
        self.assertEqual(actions.count(KEEP_STOCK), 4)
        self.assertEqual(actions.count(FEED), 2)
        # 好的三只全留，差的只留一只
        self.assertEqual(actions[:3], [KEEP_STOCK] * 3)

    def test_purple_stock_only_when_species_has_embryo(self):
        cats = [rec('汉克喵', '装填新手·驱逐', '声纳兵·先锋')] * 2
        self.assertEqual([v.action for v in triage(cats)], [FEED, FEED])
        cats.append(rec('汉克喵', '侵略如火', '装填新手·驱逐', '轮机手·驱逐'))
        self.assertEqual([v.action for v in triage(cats)], [KEEP_STOCK, KEEP_STOCK, KEEP_EMBRYO])


if __name__ == '__main__':
    unittest.main()
