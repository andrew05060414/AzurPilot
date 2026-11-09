"""指挥喵分拣规则（纯逻辑，无设备依赖，便于单测）。

回答「这只猫留不留、留来干什么」，供批量整理与收猫流程共用。分拣口径来自用户本人：

- 真正有用的彩天赋只有 **侵略如火**（最好）、**不动如山**，以及本舰种的精英天赋
  （航母「王牌机师」、战列「一发入魂」、潜艇「狼群之首」）；「既定的命运」勉强算半个；
  其疾如风、被期待的新星之类数值太小，当作占格子的废天赋。
- 普通天赋讲究「清一色」：全部是本舰种相关的天赋（同一种颜色），夹着别的舰种或
  观测、声呐这类天赋就是不纯。彩天赋最重要，纯度只是次要排序。
- 紫猫样本多，标准比金猫更严。
- 废金猫不全喂：每个品种留 4 只同名猫当「猫自身技能」升级料
  （技能升 1 级喂 1 只同名、升 2 级喂 3 只同名，共 4 只）。

本模块只给结论，不做页面操作；执行（上锁 / 喂养）由调用方负责。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from module.meowfficer.cat_data import CATS
from module.meowfficer.score import Talent, match_cat, resolve_talents

# 分拣结论
KEEP_EMBRYO = 'embryo'   # 胚子：上锁，以后喂经验、洗天赋都给它
KEEP_STOCK = 'stock'     # 同名库存：上锁，留给同名胚子升技能
KEEP_IN_USE = 'in_use'   # 已经养过（等级 > 1）或识别不全：不动
FEED = 'feed'            # 饲料：不上锁，喂给胚子

STOCK_PER_SPECIES = 4

# 彩天赋权重：火 > 山 = 本舰种精英 > 既定的命运；其余特殊天赋计 0（视作废格子）
GOOD_SPECIAL = {'侵略如火': 10, '不动如山': 6, '既定的命运': 2}
ELITE_BY_TYPE = {'航母': '王牌机师', '战列': '一发入魂', '潜艇': '狼群之首'}
ELITE_WEIGHT = 6

# 同色（本舰种相关）普通天赋：按天赋线后缀判断
TYPE_SUFFIX = {
    '航母': ('空母',),
    '战列': ('战列', '主力', '大型舰'),
    '潜艇': ('潜艇',),
    '驱逐': ('驱逐', '先锋', '小型舰'),
    '巡洋': ('巡洋', '先锋', '中型舰'),
}
# 后缀对得上也不算有用的天赋线（命中、反潜一类）
OFF_COLOR_LINES = ('新手观测士', '声纳兵')
# 每个舰种真正加攻击的普通天赋线（攻略「两条加攻击的普通天赋点满」）
ATTACK_LINES = {
    '航母': ('新晋指挥官·空母', '航空新兵·空母'),
    '战列': ('新晋指挥官·战列', '炮击新手·主力'),
    '潜艇': ('新晋指挥官·潜艇', '新人雷击士·潜艇', '装填新手·潜艇'),
    '驱逐': ('装填新手·驱逐', '炮击新手·驱逐', '新人雷击士·驱逐', '新晋指挥官·驱逐'),
    '巡洋': ('炮击新手·巡洋', '新人雷击士·巡洋', '新晋指挥官·巡洋', '装填新手·巡洋'),
}


@dataclass
class CatRecord:
    """一只猫的识别结果（输入）。"""

    name: str
    talents: list
    level: int | None = None
    locked: bool | None = None
    index: int = 0          # 在猫窝列表里的扫描序号，仅用于报告与回溯


@dataclass
class Verdict:
    """一只猫的分拣结论（输出）。"""

    record: CatRecord
    cat: str                 # 归一后的品种名
    rarity: str
    ship_type: str
    action: str
    score: float
    specials: list = field(default_factory=list)
    junk: list = field(default_factory=list)
    reason: str = ''

    @property
    def keep(self) -> bool:
        return self.action != FEED


def _line_is_on_color(line: str, ship_type: str) -> bool:
    if any(line.startswith(prefix) for prefix in OFF_COLOR_LINES):
        return False
    return any(line.endswith(suffix) for suffix in TYPE_SUFFIX.get(ship_type, ()))


def assess(record: CatRecord) -> Verdict:
    """只看这一只猫本身，给出胚子与否和分数；同名库存在 :func:`triage` 里统一分配。"""
    cat = record.name if record.name in CATS else (match_cat(record.name) or record.name)
    info = CATS.get(cat, {})
    rarity = info.get('rarity', '')
    ship_type = (info.get('types') or ('',))[0]
    talents: list[Talent] = resolve_talents(record.talents)
    elite = ELITE_BY_TYPE.get(ship_type)

    score = 0.0
    specials, junk = [], []
    for t in talents:
        if t.kind == 'special':
            weight = GOOD_SPECIAL.get(t.name, ELITE_WEIGHT if t.name == elite else 0)
            if weight:
                specials.append(t.name)
                score += weight
            else:
                junk.append(t.name)
        elif t.kind == 'normal':
            if t.line in ATTACK_LINES.get(ship_type, ()):
                score += 1.5 * t.level
            elif _line_is_on_color(t.line, ship_type):
                score += 0.5 * t.level
            else:
                junk.append(t.name)
        else:
            junk.append(t.name)
    score -= 1.0 * len(junk)

    verdict = Verdict(record=record, cat=cat, rarity=rarity, ship_type=ship_type,
                      action=FEED, score=round(score, 2), specials=specials, junk=junk)

    if record.level is not None and record.level > 1:
        verdict.action, verdict.reason = KEEP_IN_USE, f'已养到 Lv{record.level}，不动'
        return verdict
    if not talents or any(t.kind == 'unknown' for t in talents):
        verdict.action, verdict.reason = KEEP_IN_USE, '天赋没识别全，宁可不喂'
        return verdict
    if info.get('fixed'):
        verdict.action, verdict.reason = KEEP_IN_USE, '固定天赋猫'
        return verdict

    has_fire = '侵略如火' in specials
    core = [s for s in specials if s != '既定的命运']
    clean = not junk
    if rarity == 'SSR':
        if core:
            verdict.action = KEEP_EMBRYO
            verdict.reason = f'金猫自带{"、".join(core)}' + ('' if clean else f'（夹着 {len(junk)} 个废天赋）')
        else:
            verdict.reason = '金猫：没有火、山或本舰种精英'
    elif rarity == 'SR':
        if has_fire and clean:
            verdict.action, verdict.reason = KEEP_EMBRYO, '紫猫自带侵略如火且无废天赋'
        elif core and clean:
            verdict.action, verdict.reason = KEEP_EMBRYO, f'紫猫自带{core[0]}且清一色'
        else:
            verdict.reason = '紫猫：标准更严，要有火，或有山/精英且不夹废天赋'
    else:
        verdict.reason = '蓝猫一律当饲料'
    return verdict


def triage(records: list[CatRecord], stock_per_species: int = STOCK_PER_SPECIES) -> list[Verdict]:
    """分拣整个猫窝。

    规则：先逐只判定胚子；再给金猫每个品种、以及已有胚子的紫猫品种，
    从剩下的猫里挑分数最高的若干只当同名库存（总数含已有的同名胚子以外的库存）。

    Returns:
        与输入顺序一致的结论列表。
    """
    verdicts = [assess(r) for r in records]

    has_embryo = {v.cat for v in verdicts if v.action == KEEP_EMBRYO}
    pool = defaultdict(list)
    for v in verdicts:
        if v.action != FEED:
            continue
        if v.rarity == 'SSR' or (v.rarity == 'SR' and v.cat in has_embryo):
            pool[v.cat].append(v)
    for cat, items in pool.items():
        items.sort(key=lambda v: -v.score)
        for v in items[:stock_per_species]:
            v.action = KEEP_STOCK
            v.reason = f'{cat} 同名库存（留 {stock_per_species} 只给胚子升技能）'
    return verdicts


def summary(verdicts: list[Verdict]) -> dict:
    """按结论计数，便于日志与报告。"""
    counts = defaultdict(int)
    for v in verdicts:
        counts[v.action] += 1
    return dict(counts)
