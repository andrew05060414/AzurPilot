"""石油溢出调度。

石油超过用户设定额度，或接近仓库上限时，优先插入已启用的主线图等
快速耗油任务，避免调度器先跑耗时很长的大型作战。

检测来源：
- 仪表盘 ``Dashboard.Oil.Value / Limit``
- 委托领取弹出石油溢出时的强制标记

两档触发：
- 额度：石油达到设定值后提高耗油任务优先级并提前执行
- 警戒：离上限剩余容量过低时立即拉起耗油任务
"""

from module.config.deep import deep_get
from module.config.task_priority import format_task_priority, parse_task_priority
from module.logger import logger

# 默认可用于消耗石油的出击任务，按常见耗油速度大致排序。
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

# 即使石油溢出，仍应排在耗油任务之前的大世界任务。
OPSI_KEEP_AHEAD = frozenset({
    'OpsiCrossMonth',
})

LEVEL_NONE = 'none'
LEVEL_THRESHOLD = 'threshold'
LEVEL_ALERT = 'alert'

ATTR_FORCED = '_oil_overflow_forced'
ATTR_ACTIVE = '_oil_overflow_active'


def _to_int(value, default=0):
    """将配置值转为 int，无法转换时返回 default。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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


def is_oil_overflow_enabled(config):
    """是否启用石油溢出优先消耗。"""
    value = config.cross_get(keys='Alas.OilOverflow.Enable', default=True)
    return bool(value)


def get_oil_overflow_threshold(config):
    """读取石油额度。0 表示不按绝对数量触发。"""
    return max(0, _to_int(
        config.cross_get(keys='Alas.OilOverflow.Threshold', default=0),
        0,
    ))


def get_oil_overflow_alert(config):
    """读取警戒额度，表示离上限还剩多少时立即消耗。"""
    return max(0, _to_int(
        config.cross_get(keys='Alas.OilOverflow.Alert', default=300),
        300,
    ))


def get_oil_overflow_reserve(config):
    """读取消耗石油时要保留的剩余容量。"""
    reserve = _to_int(
        config.cross_get(keys='Alas.OilOverflow.Reserve', default=2000),
        2000,
    )
    return max(0, reserve)


def get_oil_overflow_task_call(config):
    """读取用户指定的耗油任务，``auto`` 表示自动挑选已启用任务。"""
    task = config.cross_get(keys='Alas.OilOverflow.TaskCall', default='auto')
    if not task:
        return 'auto'
    return str(task)


def mark_oil_overflow(config):
    """标记当前石油已溢出。

    委托领取弹窗无法提供准确数值时使用，调度器据此插入耗油任务。
    """
    setattr(config, ATTR_FORCED, True)
    setattr(config, ATTR_ACTIVE, True)


def clear_oil_overflow(config):
    """清除石油溢出标记。"""
    setattr(config, ATTR_FORCED, False)
    setattr(config, ATTR_ACTIVE, False)


def oil_overflow_level(
        oil,
        limit,
        threshold=0,
        alert=300,
        reserve=2000,
        active=False,
        forced=False,
):
    """判断石油调度档位。

    Args:
        oil (int): 当前石油。
        limit (int): 石油上限。
        threshold (int): 额度。石油达到此值后开始消耗，0 表示不按绝对数量触发。
        alert (int): 警戒额度。离上限剩余容量低于此值时立即消耗。
        reserve (int): 从警戒消耗到该剩余容量后停止。
        active (bool): 上一轮是否已处于消耗状态（滞后）。
        forced (bool): 是否由石油溢出弹窗强制触发。

    Returns:
        str: ``none`` / ``threshold`` / ``alert``。
    """
    oil = max(0, _to_int(oil, 0))
    limit = max(0, _to_int(limit, 0))
    threshold = max(0, _to_int(threshold, 0))
    alert = max(0, _to_int(alert, 0))
    reserve = max(0, _to_int(reserve, 0))
    if alert > reserve:
        alert = reserve

    if forced:
        return LEVEL_ALERT

    remaining = (limit - oil) if limit > 0 else None
    if remaining is not None and remaining <= alert:
        return LEVEL_ALERT
    if threshold > 0 and oil >= threshold:
        return LEVEL_THRESHOLD

    if active:
        if remaining is None:
            return LEVEL_ALERT
        if remaining < reserve:
            return LEVEL_ALERT
        if threshold > 0 and oil >= threshold:
            return LEVEL_THRESHOLD
        return LEVEL_NONE

    return LEVEL_NONE


def should_dump_oil(
        oil,
        limit,
        reserve,
        threshold=0,
        alert=300,
        active=False,
        forced=False,
):
    """判断是否应优先消耗石油。"""
    return oil_overflow_level(
        oil=oil,
        limit=limit,
        threshold=threshold,
        alert=alert,
        reserve=reserve,
        active=active,
        forced=forced,
    ) != LEVEL_NONE


def resolve_oil_dump_task(task_call, enabled_tasks, dump_tasks=OIL_DUMP_TASKS):
    """选择用于消耗石油的任务。

    Args:
        task_call (str): 用户指定任务，或 ``auto``。
        enabled_tasks (Iterable[str]): 当前已启用的任务名。
        dump_tasks (Iterable[str]): 可作为耗油任务的候选。

    Returns:
        str | None: 选中的任务名。没有可用任务时返回 None。
    """
    enabled = set(enabled_tasks or ())
    candidates = tuple(dump_tasks)
    preferred = str(task_call or 'auto')
    if preferred != 'auto':
        if preferred in enabled:
            return preferred
        logger.warning(
            f'[调度-石油] 指定的耗油任务 `{preferred}` 未启用，改为自动选择'
        )

    for task in candidates:
        if task in enabled:
            return task
    return None


def boost_oil_dump_priority(priority, dump_task):
    """把耗油任务插入到大型作战之前。

    ``OpsiCrossMonth`` 仍保持最高优先级之一，不被后移。

    Args:
        priority (str): 当前调度优先级文本。
        dump_task (str): 要提前的耗油任务。

    Returns:
        str: 调整后的优先级文本。
    """
    if not dump_task:
        return priority

    tasks = parse_task_priority(priority)
    rest = [task for task in tasks if task != dump_task]
    insert_at = len(rest)
    for index, task in enumerate(rest):
        if task.startswith('Opsi') and task not in OPSI_KEEP_AHEAD:
            insert_at = index
            break
    rest.insert(insert_at, dump_task)
    return format_task_priority(rest)


def _function_command(func):
    return getattr(func, 'command', None)


def promote_dump_task(pending, waiting, dump_task):
    """若耗油任务还在等待队列，将其提升为待运行。

    Args:
        pending (list): 已到期任务。
        waiting (list): 未到期任务。
        dump_task (str): 耗油任务名。

    Returns:
        tuple[list, list]: 调整后的 (pending, waiting)。
    """
    if not dump_task:
        return pending, waiting

    pending = list(pending)
    waiting = list(waiting)
    if any(_function_command(func) == dump_task for func in pending):
        return pending, waiting

    for index, func in enumerate(waiting):
        if _function_command(func) == dump_task:
            waiting.pop(index)
            pending.append(func)
            logger.info(f'[调度-石油] 提升等待中的耗油任务 `{dump_task}` 为待运行')
            break
    return pending, waiting


def apply_oil_overflow_schedule(config, pending, waiting, priority):
    """按石油溢出状态调整待运行队列与优先级。

    Args:
        config: AzurLaneConfig。
        pending (list): 已到期任务。
        waiting (list): 未到期任务。
        priority (str): 当前调度优先级。

    Returns:
        tuple[list, list, str]: (pending, waiting, priority)
    """
    if not is_oil_overflow_enabled(config):
        clear_oil_overflow(config)
        return pending, waiting, priority

    data = getattr(config, 'data', {}) or {}
    oil, limit = get_dashboard_oil(data)
    threshold = get_oil_overflow_threshold(config)
    alert = get_oil_overflow_alert(config)
    reserve = get_oil_overflow_reserve(config)
    forced = bool(getattr(config, ATTR_FORCED, False))
    active = bool(getattr(config, ATTR_ACTIVE, False))
    level = oil_overflow_level(
        oil=oil,
        limit=limit,
        threshold=threshold,
        alert=alert,
        reserve=reserve,
        active=active,
        forced=forced,
    )
    if level == LEVEL_NONE:
        if active or forced:
            logger.info(
                f'[调度-石油] 石油已回到安全区间，恢复正常调度 '
                f'({oil}/{limit}, 额度 {threshold}, 警戒剩余 {alert}, 保留 {reserve})'
            )
        clear_oil_overflow(config)
        return pending, waiting, priority

    enabled_tasks = []
    for func in list(pending) + list(waiting):
        command = _function_command(func)
        if command:
            enabled_tasks.append(command)
    dump_task = resolve_oil_dump_task(
        get_oil_overflow_task_call(config),
        enabled_tasks,
    )
    if not dump_task:
        logger.warning(
            '[调度-石油] 石油需要消耗，但未启用主线图等耗油任务，无法自动消耗'
        )
        return pending, waiting, priority

    setattr(config, ATTR_ACTIVE, True)
    setattr(config, ATTR_FORCED, False)
    remaining = (limit - oil) if limit > 0 else None
    remaining_text = f'剩余 {remaining}' if remaining is not None else '上限未知'
    if level == LEVEL_ALERT:
        logger.info(
            f'[调度-石油] 石油警戒 ({oil}/{limit}, {remaining_text})，'
            f'立即消耗石油: {dump_task}'
        )
    else:
        logger.info(
            f'[调度-石油] 石油超过额度 {threshold} ({oil}/{limit})，'
            f'优先消耗石油: {dump_task}'
        )
    pending, waiting = promote_dump_task(pending, waiting, dump_task)
    priority = boost_oil_dump_priority(priority, dump_task)
    return pending, waiting, priority


def try_handle_oil_maxed(config):
    """委托石油溢出后，尝试改为调度耗油任务。

    Args:
        config: AzurLaneConfig。

    Returns:
        bool: 已标记溢出并存在可用耗油任务时返回 True。
    """
    if not is_oil_overflow_enabled(config):
        return False

    mark_oil_overflow(config)
    dump_task = resolve_oil_dump_task(
        get_oil_overflow_task_call(config),
        [
            task
            for task in OIL_DUMP_TASKS
            if config.is_task_enabled(task)
        ],
    )
    if not dump_task:
        logger.warning(
            '[委托-石油] 石油溢出，但未启用主线图等耗油任务，无法自动消耗'
        )
        return False

    logger.info(f'[委托-石油] 石油溢出，改为优先运行耗油任务 `{dump_task}`')
    return True
