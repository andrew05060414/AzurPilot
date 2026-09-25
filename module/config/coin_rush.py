"""物资冲刺：优先执行快速出击任务，用来快速积累物资或快速消耗石油。

两种进入方式共用同一套调度动作：
- 手动开启：急需物资时开启，物资达到目标数量后自动关闭。
- 石油偏高自动进入：仪表盘石油达到开始消耗线（默认 22000），或委托领奖时弹出
  石油溢出。游戏石油硬上限默认 25000，满仓后委托和收菜的石油无法入库，因此要
  提前消耗。

调度动作：
- 只调整已到期任务的先后顺序：轻量日常 > 出击任务 > 其他任务。
- 按大型作战策略暂缓大世界任务（跳过 / 只做快速任务 / 仅空闲时运行）。
- 不把尚未到期的任务提前。任务因心情、作战委托占用关卡或次数用尽而自行
  延后时沿用任务设定的时间；若强行放到队首，调度器会在队首空等，堵住
  其他已到期任务。
- 石油达到紧急线且开启了紧急购买食物时，后宅任务运行时先买食物消耗石油。

石油消耗的滞后区间、委托溢出标记和日志去重保存在当前进程内：调度器在每个
任务结束后都会重建配置对象，挂在配置对象上的状态会丢失。
"""

import math
from dataclasses import dataclass
from datetime import datetime

from module.config.deep import deep_get
from module.config.task_priority import format_task_priority, parse_task_priority
from module.logger import logger

# 可用于消耗石油的出击任务，按常见耗油速度大致排序
OIL_DUMP_TASKS = (
    'Main',
    'Main2',
    'Main3',
    'Event',
    'Event2',
    'Event3',
    'Raid',
    'GemsFarming',
    'ThreeOilLowCost',
    'Hospital',
    'HospitalEvent',
    'Coalition',
)

# 可用于刷取物资的出击任务，按刷物资效率与常见配置大致排序
COIN_FARMING_TASKS = (
    'Main2',
    'Main',
    'Main3',
    'Event2',
    'Event',
    'Event3',
    'GemsFarming',
    'ThreeOilLowCost',
)

# 物资冲刺期间仍排在出击任务之前的轻量日常与维护任务
FAST_ROUTINES_AHEAD = (
    'Restart',
    'OpsiCrossMonth',
    'Commission',
    'Tactical',
    'Dorm',
    'Reward',
    'Guild',
)

# 任何策略下都保留的大世界任务：跨月重置不能错过
OPSI_ALWAYS_ALLOWED = frozenset({
    'OpsiCrossMonth',
})

# 耗时短、收益固定的大世界任务：每日任务、商店（含月末清库存）、补给凭证
OPSI_QUICK_TASKS = OPSI_ALWAYS_ALLOWED | frozenset({
    'OpsiDaily',
    'OpsiShop',
    'OpsiVoucher',
})

OPSI_SUPPRESS = 'suppress'
OPSI_QUICK_ONLY = 'quick_only'
OPSI_IDLE_ONLY = 'idle_only'
OPSI_POLICIES = (OPSI_SUPPRESS, OPSI_QUICK_ONLY, OPSI_IDLE_ONLY)

LEVEL_NONE = 'none'
LEVEL_HIGH = 'high'
LEVEL_EMERGENCY = 'emergency'

# 进入石油消耗后，降到开始线以下这么多才退出，避免在开始线附近反复切换
OIL_HYSTERESIS = 1000

# 后宅购买界面默认选中的食物（喂食量 5000）每份的石油价格。
# 依据识别素材 DORM_BUY_FOOD_CHECK（单价 50）与 FOOD_BUY_COST（11 份 550）推定。
FOOD_OIL_COST = 50

DEFAULT_HARD_LIMIT = 25000
DEFAULT_START_OIL = 22000
DEFAULT_EMERGENCY_OIL = 23500
DEFAULT_EMERGENCY_FOOD_OIL = 1000


def _to_int(value, default=0):
    """将配置值转为 int，无法转换时返回 default。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class OilSettings:
    enable: bool
    hard_limit: int
    start: int
    emergency: int
    task_call: str
    emergency_food: bool
    emergency_food_oil: int


def read_oil_settings(config):
    """读取石油偏高自动冲刺的设置，并保证 开始线 <= 紧急线 <= 硬上限。"""
    def get(key, default):
        return config.cross_get(keys=f'Alas.CoinRush.{key}', default=default)

    hard_limit = _to_int(get('HardLimit', DEFAULT_HARD_LIMIT), DEFAULT_HARD_LIMIT)
    if hard_limit <= 0:
        hard_limit = DEFAULT_HARD_LIMIT
    emergency = min(max(0, _to_int(get('EmergencyOil', DEFAULT_EMERGENCY_OIL), DEFAULT_EMERGENCY_OIL)), hard_limit)
    start = min(max(0, _to_int(get('StartOil', DEFAULT_START_OIL), DEFAULT_START_OIL)), emergency)
    return OilSettings(
        enable=bool(get('AutoOnHighOil', True)),
        hard_limit=hard_limit,
        start=start,
        emergency=emergency,
        # 与手动冲刺共用同一个出击任务设置
        task_call=str(get('FarmingTask', 'auto') or 'auto'),
        emergency_food=bool(get('EmergencyFood', False)),
        emergency_food_oil=max(0, _to_int(
            get('EmergencyFoodOil', DEFAULT_EMERGENCY_FOOD_OIL), DEFAULT_EMERGENCY_FOOD_OIL)),
    )


@dataclass
class CoinRushSettings:
    enable: bool
    farming_task: str
    target_coins: int
    min_oil: int


def read_coin_rush_settings(config):
    def get(key, default):
        return config.cross_get(keys=f'Alas.CoinRush.{key}', default=default)

    return CoinRushSettings(
        enable=bool(get('Enable', False)),
        farming_task=str(get('FarmingTask', 'auto') or 'auto'),
        target_coins=max(0, _to_int(get('TargetCoins', 0), 0)),
        min_oil=max(0, _to_int(get('MinOil', 300), 300)),
    )


def read_opsi_policy(config):
    policy = config.cross_get(keys='Alas.CoinRush.OpsiPolicy', default=OPSI_SUPPRESS)
    return policy if policy in OPSI_POLICIES else OPSI_SUPPRESS


@dataclass
class _RushState:
    # 石油消耗是否已开始（滞后区间内保持）
    oil_active: bool = False
    # 委托领奖弹出石油溢出的时间；仪表盘石油在此之后更新前，按紧急处理
    maxed_at: datetime | None = None
    # 上一次输出的状态摘要，只在变化时记录日志
    last_summary: tuple | None = None


_states = {}


def _state(config):
    return _states.setdefault(getattr(config, 'config_name', None), _RushState())


def reset_state(config_name=None):
    """清除进程内状态；None 表示全部清除。供测试和重新启动使用。"""
    if config_name is None:
        _states.clear()
    else:
        _states.pop(config_name, None)


def _dashboard_oil(config):
    """返回 (石油, 记录时间)。仪表盘由出击时的顶栏识别更新，可能滞后。"""
    data = getattr(config, 'data', {}) or {}
    oil = max(0, _to_int(deep_get(data, 'Dashboard.Oil.Value', 0), 0))
    record = deep_get(data, 'Dashboard.Oil.Record', None)
    return oil, record if isinstance(record, datetime) else None


def _dashboard_coin(config):
    data = getattr(config, 'data', {}) or {}
    return max(0, _to_int(deep_get(data, 'Dashboard.Coin.Value', 0), 0))


def is_oil_rush_enabled(config):
    return read_oil_settings(config).enable


def mark_oil_maxed(config):
    """委托领奖弹出石油溢出时调用；在仪表盘石油更新前按紧急处理。"""
    _state(config).maxed_at = datetime.now().replace(microsecond=0)


def oil_level(config, settings=None):
    """判断石油档位，并更新滞后状态。

    Returns:
        str: ``none`` / ``high`` / ``emergency``。
    """
    settings = settings or read_oil_settings(config)
    state = _state(config)
    if not settings.enable:
        state.oil_active = False
        state.maxed_at = None
        return LEVEL_NONE

    oil, record = _dashboard_oil(config)
    if state.maxed_at is not None and record is not None and record > state.maxed_at:
        # 弹窗之后仪表盘已重新识别，改用实际数值
        state.maxed_at = None

    if state.maxed_at is not None or oil >= settings.emergency:
        level = LEVEL_EMERGENCY
    elif oil >= settings.start:
        level = LEVEL_HIGH
    elif state.oil_active and oil >= settings.start - OIL_HYSTERESIS:
        level = LEVEL_HIGH
    else:
        level = LEVEL_NONE
    state.oil_active = level != LEVEL_NONE
    return level


def _coin_rush_reached(config, settings):
    """达到目标物资时自动关闭手动物资冲刺。"""
    coin = _dashboard_coin(config)
    target = settings.target_coins
    if target <= 0 or coin < target:
        return False
    logger.info(f'[调度-物资冲刺] 物资已达标 ({coin}/{target})，自动关闭物资冲刺')
    config.cross_set('Alas.CoinRush.Enable', False)
    save = getattr(config, 'save', None)
    if callable(save):
        try:
            save()
        except Exception as e:
            logger.warning(f'[调度-物资冲刺] 保存物资冲刺开关失败: {e}')
    return True


def _ordered_candidates(preferred, candidates):
    ordered = []
    if preferred and preferred != 'auto':
        ordered.append(preferred)
    for task in candidates:
        if task not in ordered:
            ordered.append(task)
    return ordered


@dataclass
class RushPlan:
    oil_level: str
    coin_rush: bool
    # 需要优先执行的出击任务，按优先顺序排列
    tasks: list
    opsi_policy: str
    emergency_food: bool


def plan_coin_rush(config):
    """根据石油和物资冲刺状态生成物资冲刺计划；不需要冲刺时返回 None。"""
    oil_settings = read_oil_settings(config)
    level = oil_level(config, oil_settings)

    coin_settings = read_coin_rush_settings(config)
    coin_rush = coin_settings.enable and not _coin_rush_reached(config, coin_settings)

    if level == LEVEL_NONE and not coin_rush:
        return None

    tasks = []
    if coin_rush:
        oil, _ = _dashboard_oil(config)
        # 仪表盘还没有石油数据时放行，出击后会刷新
        if oil == 0 or oil >= coin_settings.min_oil:
            tasks += _ordered_candidates(coin_settings.farming_task, COIN_FARMING_TASKS)
    if level != LEVEL_NONE:
        for task in _ordered_candidates(oil_settings.task_call, OIL_DUMP_TASKS):
            if task not in tasks:
                tasks.append(task)

    return RushPlan(
        oil_level=level,
        coin_rush=coin_rush,
        tasks=tasks,
        opsi_policy=read_opsi_policy(config),
        emergency_food=level == LEVEL_EMERGENCY and oil_settings.emergency_food,
    )


def boost_rush_priority(priority, tasks, emergency_food=False):
    """轻量日常保持在前，出击任务紧随其后，其余任务保持原相对顺序后移。

    紧急购买食物时把后宅提到轻量日常的最前面（重启之后）。
    """
    if not tasks:
        return priority
    order = parse_task_priority(priority)
    ahead = [task for task in order if task in FAST_ROUTINES_AHEAD]
    if emergency_food and 'Dorm' in ahead:
        ahead.remove('Dorm')
        ahead.insert(1 if ahead and ahead[0] == 'Restart' else 0, 'Dorm')
    rest = [task for task in order if task not in FAST_ROUTINES_AHEAD and task not in tasks]
    return format_task_priority(ahead + list(tasks) + rest)


def filter_opsi_tasks(pending, opsi_policy):
    """按大型作战策略移除待运行队列中的大世界任务。

    被移除的任务不进入等待队列：它们的运行时间已到，放进等待队列会被当作
    最早到期的任务再次选中。冲刺结束后会按原计划重新出现。
    """
    if opsi_policy == OPSI_SUPPRESS:
        allowed = OPSI_ALWAYS_ALLOWED
    elif opsi_policy == OPSI_QUICK_ONLY:
        allowed = OPSI_QUICK_TASKS
    else:
        return pending
    return [
        func for func in pending
        if not str(getattr(func, 'command', '')).startswith('Opsi')
        or func.command in allowed
    ]


def _log_plan(config, plan, enabled_tasks):
    """只在冲刺状态变化时记录日志，避免每轮调度（以及 WebUI 刷新）重复输出。"""
    state = _state(config)
    if plan is None:
        summary = None
    else:
        summary = (plan.oil_level, plan.coin_rush, tuple(enabled_tasks), plan.opsi_policy, plan.emergency_food)
    if summary == state.last_summary:
        return
    state.last_summary = summary

    if plan is None:
        logger.info('[调度-物资冲刺] 退出物资冲刺，恢复正常调度')
        return

    oil, _ = _dashboard_oil(config)
    settings = read_oil_settings(config)
    reasons = []
    if plan.oil_level == LEVEL_EMERGENCY:
        reasons.append(f'石油紧急 ({oil}，紧急线 {settings.emergency}，硬上限 {settings.hard_limit})')
    elif plan.oil_level == LEVEL_HIGH:
        reasons.append(f'石油偏高 ({oil}，开始线 {settings.start})')
    if plan.coin_rush:
        reasons.append(f'物资冲刺 (物资 {_dashboard_coin(config)})')
    logger.info(
        f'[调度-物资冲刺] 进入物资冲刺：{"，".join(reasons)}；'
        f'优先出击 {enabled_tasks or "无"}；大型作战策略 {plan.opsi_policy}'
        f'{"；后宅将紧急购买食物" if plan.emergency_food else ""}'
    )
    if not enabled_tasks:
        logger.warning('[调度-物资冲刺] 没有已启用的出击任务可以优先执行，只暂缓大型作战任务')


def apply_coin_rush_schedule(config, pending, waiting, priority):
    """按物资冲刺计划调整待运行队列与优先级；等待队列保持不变。

    Returns:
        tuple[list, list, str]: (pending, waiting, priority)
    """
    plan = plan_coin_rush(config)
    if plan is None:
        _log_plan(config, None, [])
        return pending, waiting, priority

    enabled = {getattr(func, 'command', None) for func in list(pending) + list(waiting)}
    tasks = [task for task in plan.tasks if task in enabled]
    _log_plan(config, plan, tasks)

    priority = boost_rush_priority(priority, tasks, emergency_food=plan.emergency_food)
    pending = filter_opsi_tasks(pending, plan.opsi_policy)
    return pending, waiting, priority


def emergency_food_units(config):
    """后宅任务运行时应购买的食物份数；不需要紧急消耗时返回 0。"""
    settings = read_oil_settings(config)
    if not settings.emergency_food or settings.emergency_food_oil <= 0:
        return 0
    if oil_level(config, settings) != LEVEL_EMERGENCY:
        return 0
    return math.ceil(settings.emergency_food_oil / FOOD_OIL_COST)


def record_food_purchase(config, units):
    """买完食物后按单价扣减仪表盘石油，避免旧数值让下一轮继续判定为紧急。"""
    from module.log_res import LogRes

    oil, _ = _dashboard_oil(config)
    spent = units * FOOD_OIL_COST
    LogRes(config).Oil = {'Value': max(0, oil - spent)}
    logger.info(f'[调度-物资冲刺] 后宅购买食物 {units} 份，约消耗石油 {spent}')

