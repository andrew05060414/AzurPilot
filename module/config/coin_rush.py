"""物资冲刺模式调度。

在大型活动前夕或急需物资时，将刷钱任务（如 Main2 / 13-4、12-4）
提权至仅次于轻量日常（委托、后宅、领奖、战术学院、大舰队）的顶格顺位，
只要石油达到最低出击门槛即专注出击刷钱，大世界与海岛任务主动延后或跳过，
以最高效率将石油转化为抽卡物资。

支持特性：
- 目标物资自动达成退出（达到 TargetCoins 后自动关闭冲刺并还原调度）
- 最低石油门槛保护（石油低于 MinOil 时不强行提权出击，让位给委托与食堂回油）
- 大世界策略选择（完全跳过 suppress / 仅没油闲置挂机 idle_only）
- 自动挑选刷钱任务（auto 首选 Main2，次选 Main/Event 等）
"""

from module.config.deep import deep_get
from module.config.task_priority import format_task_priority, parse_task_priority
from module.logger import logger

# 可用于刷取物资的出击任务，按刷钱效率与常见配置大致排序
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

# 物资冲刺期间，仍应排在刷钱出击之前的轻量日常/维护任务
FAST_ROUTINES_AHEAD = (
    'Restart',
    'OpsiCrossMonth',
    'Commission',
    'Tactical',
    'Dorm',
    'Reward',
    'Guild',
)

OPSI_KEEP_AHEAD = frozenset({
    'OpsiCrossMonth',
})


def _to_int(value, default=0):
    """将配置值转为 int，无法转换时返回 default。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_dashboard_coin(data):
    """读取仪表盘物资当前值。

    Args:
        data (dict): 用户配置 JSON。

    Returns:
        int: 当前物资数量，缺失时为 0。
    """
    return max(0, _to_int(deep_get(data, 'Dashboard.Coin.Value', 0), 0))


def get_dashboard_oil(data):
    """读取仪表盘石油当前值与上限。

    Args:
        data (dict): 用户配置 JSON。

    Returns:
        tuple[int, int]: (value, limit)，缺失时为 0。
    """
    value = _to_int(deep_get(data, 'Dashboard.Oil.Value', 0), 0)
    limit = _to_int(deep_get(data, 'Dashboard.Oil.Limit', 0), 0)
    return max(value, 0), max(limit, 0)


def is_coin_rush_enabled(config):
    """是否启用物资冲刺模式。"""
    return bool(config.cross_get(keys='Alas.CoinRush.Enable', default=False))


def get_coin_rush_target_coins(config):
    """读取目标物资数量。0 表示不限上限。"""
    return max(0, _to_int(
        config.cross_get(keys='Alas.CoinRush.TargetCoins', default=0),
        0,
    ))


def get_coin_rush_min_oil(config):
    """读取最低出击石油门槛。默认 300。"""
    return max(0, _to_int(
        config.cross_get(keys='Alas.CoinRush.MinOil', default=300),
        300,
    ))


def get_coin_rush_farming_task(config):
    """读取指定的刷钱任务，``auto`` 表示自动挑选。"""
    task = config.cross_get(keys='Alas.CoinRush.FarmingTask', default='auto')
    if not task:
        return 'auto'
    return str(task)


def get_coin_rush_opsi_policy(config):
    """读取大世界处理策略，默认为 ``suppress``。"""
    policy = config.cross_get(keys='Alas.CoinRush.OpsiPolicy', default='suppress')
    if policy not in ('suppress', 'idle_only'):
        return 'suppress'
    return str(policy)


def should_exit_coin_rush(target_coins, current_coin):
    """判断是否达到目标物资，应退出冲刺模式。"""
    target = max(0, _to_int(target_coins, 0))
    if target <= 0:
        return False
    coin = max(0, _to_int(current_coin, 0))
    return coin >= target


def is_oil_sufficient(oil, limit, min_oil):
    """判断当前石油是否达到出击最低门槛。

    若仪表盘尚未获取石油数据（oil 与 limit 皆为 0），放行出击以促使进游戏更新。
    """
    oil = max(0, _to_int(oil, 0))
    limit = max(0, _to_int(limit, 0))
    min_oil = max(0, _to_int(min_oil, 0))
    if oil == 0 and limit == 0:
        return True
    return oil >= min_oil


def resolve_farming_task(task_call, enabled_tasks, candidates=COIN_FARMING_TASKS):
    """挑选用于物资冲刺的出击任务。

    Args:
        task_call (str): 用户指定任务，或 ``auto``。
        enabled_tasks (Iterable[str]): 当前已启用的任务名。
        candidates (Iterable[str]): 候选任务顺序。

    Returns:
        str | None: 选中的任务名，无可用任务时返回 None。
    """
    enabled = set(enabled_tasks or ())
    preferred = str(task_call or 'auto')
    if preferred != 'auto':
        if preferred in enabled:
            return preferred
        logger.warning(
            f'[调度-物资冲刺] 指定的刷钱任务 `{preferred}` 未启用，改为自动选择'
        )

    for task in candidates:
        if task in enabled:
            return task
    return None


def boost_coin_rush_priority(priority, farming_task):
    """重构调度优先级，将刷钱任务置于轻量日常之后、重度任务之前。

    轻量日常（FAST_ROUTINES_AHEAD）保持在最前，刷钱任务紧随其后，
    其余所有任务（科研、大世界、演习、海岛等）后移并保留原有相对顺序。

    Args:
        priority (str): 当前调度优先级文本。
        farming_task (str): 刷钱任务名称。

    Returns:
        str: 调整后的调度优先级文本。
    """
    if not farming_task:
        return priority

    tasks = parse_task_priority(priority)
    ahead = [t for t in tasks if t in FAST_ROUTINES_AHEAD]
    rest = [t for t in tasks if t not in FAST_ROUTINES_AHEAD and t != farming_task]

    ordered = ahead + [farming_task] + rest
    return format_task_priority(ordered)


def _function_command(func):
    return getattr(func, 'command', None)


def promote_farming_task(pending, waiting, farming_task):
    """若刷钱任务还在等待队列，将其提升至待运行队列。"""
    if not farming_task:
        return pending, waiting

    pending = list(pending)
    waiting = list(waiting)
    if any(_function_command(func) == farming_task for func in pending):
        return pending, waiting

    for index, func in enumerate(waiting):
        if _function_command(func) == farming_task:
            waiting.pop(index)
            pending.append(func)
            logger.info(f'[调度-物资冲刺] 提升等待中的刷钱任务 `{farming_task}` 为待运行')
            break
    return pending, waiting


def demote_farming_task(pending, waiting, farming_task):
    """若石油不足且刷钱任务在待运行队列，将其移至等待队列。"""
    if not farming_task:
        return pending, waiting

    pending = list(pending)
    waiting = list(waiting)
    for index, func in enumerate(pending):
        if _function_command(func) == farming_task:
            pending.pop(index)
            waiting.append(func)
            logger.info(f'[调度-物资冲刺] 石油不足，刷钱任务 `{farming_task}` 转为等待')
            break
    return pending, waiting


def filter_opsi_tasks(pending, opsi_policy):
    """根据大世界策略过滤待运行队列中的大世界任务。"""
    if opsi_policy != 'suppress':
        return pending

    new_pending = []
    for func in pending:
        cmd = _function_command(func)
        if cmd and cmd.startswith('Opsi') and cmd not in OPSI_KEEP_AHEAD:
            continue
        new_pending.append(func)
    return new_pending


def apply_coin_rush_schedule(config, pending, waiting, priority):
    """按物资冲刺状态调整待运行队列与优先级。

    Args:
        config: AzurLaneConfig 实例。
        pending (list): 已到期待运行任务。
        waiting (list): 未到期等待任务。
        priority (str): 当前调度优先级。

    Returns:
        tuple[list, list, str]: (pending, waiting, priority)
    """
    if not is_coin_rush_enabled(config):
        return pending, waiting, priority

    data = getattr(config, 'data', {}) or {}
    coin = get_dashboard_coin(data)
    oil, limit = get_dashboard_oil(data)
    target_coins = get_coin_rush_target_coins(config)
    min_oil = get_coin_rush_min_oil(config)
    opsi_policy = get_coin_rush_opsi_policy(config)

    if should_exit_coin_rush(target_coins, coin):
        logger.info(
            f'[调度-物资冲刺] 物资已达标 ({coin}/{target_coins})，自动退出物资冲刺模式'
        )
        config.cross_set('Alas.CoinRush.Enable', False)
        if hasattr(config, 'save') and callable(config.save):
            try:
                config.save()
            except Exception:
                pass
        return pending, waiting, priority

    enabled_tasks = []
    for func in list(pending) + list(waiting):
        cmd = _function_command(func)
        if cmd:
            enabled_tasks.append(cmd)

    farming_task = resolve_farming_task(
        get_coin_rush_farming_task(config),
        enabled_tasks,
    )

    if not farming_task:
        logger.warning(
            '[调度-物资冲刺] 物资冲刺模式已启用，但未找到已启用的刷钱任务 (如 Main2/Main/Event 等)'
        )
        if opsi_policy == 'suppress':
            pending = filter_opsi_tasks(pending, opsi_policy)
        return pending, waiting, priority

    sufficient = is_oil_sufficient(oil, limit, min_oil)
    if sufficient:
        logger.info(
            f'[调度-物资冲刺] 物资冲刺中 (物资: {coin}{f"/{target_coins}" if target_coins > 0 else ""}, 石油: {oil}/{limit})，'
            f'优先执行刷钱任务 `{farming_task}`'
        )
        pending, waiting = promote_farming_task(pending, waiting, farming_task)
        priority = boost_coin_rush_priority(priority, farming_task)
    else:
        logger.info(
            f'[调度-物资冲刺] 当前石油 {oil} 低于最低门槛 {min_oil}，刷钱任务 `{farming_task}` 等待回油'
        )
        pending, waiting = demote_farming_task(pending, waiting, farming_task)

    if opsi_policy == 'suppress':
        pending = filter_opsi_tasks(pending, opsi_policy)

    return pending, waiting, priority
