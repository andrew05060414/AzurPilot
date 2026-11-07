"""VLM 未知页面恢复模块。

页面识别不出、现有兜底（点主页、点返回、处理弹窗）也无效时，把当前截图交给能看图的
大模型（VLM），让它从一份白名单动作里挑一个，尝试脱困。复用 llm.py 的 OpenAI 兼容配置。

配置项：
- Error_VlmRecover: 总开关，默认关闭
- Error_VlmModel: 视觉模型名称，为空则使用 Error_LlmModel
- Error_LlmApiKey / Error_LlmApiBase: 与 LLM 错误分析共用

白名单动作（模型只能选这些，其余一律视为 none）：
- back: 点左上返回，坐标必须落在 BACK_REGION 内，缺省用 BACK_DEFAULT
- home: 点右上主页，坐标必须落在 HOME_REGION 内，模型不给坐标就放弃
- close: 点关闭/X，坐标必须落在 CLOSE_REGIONS（屏幕顶部条带、右侧列）内
- tap_blank: 点屏幕顶部中间的空白处，坐标必须落在 BLANK_REGION 内，缺省用 BLANK_DEFAULT
- none: 什么都不做

不存在「确认/购买/开始」类动作；游戏里这类按钮多在屏幕中下部，白名单区域一律避开。
任何异常（开关关闭、没填 Key、超时、解析失败、坐标越界、动作不在白名单）都返回 none，不抛异常，
也不吞 GameStuckError 等上层恢复机制：本模块只在 ui_get_current_page 超时前多试几次，
失败后仍交给原有的重启游戏流程。

限制：
- 同一次故障（一次 ui_get_current_page 调用）最多请求 MAX_CALLS_PER_FAULT 次
- 全局 GLOBAL_LIMIT 次 / GLOBAL_WINDOW 秒，防止反复故障时持续烧费用
- 截图会发给用户配置的第三方模型服务，只在用户主动开启时才会发送
"""

import json
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass

from module.base.button import Button
from module.logger import logger

ACTIONS = ('back', 'close', 'home', 'tap_blank', 'none')

# 游戏画面固定 1280x720；以下区域均为该坐标系 (x1, y1, x2, y2)
SCREEN_SIZE = (1280, 720)
BACK_REGION = (0, 0, 200, 110)
BACK_DEFAULT = (45, 45)
HOME_REGION = (950, 0, 1280, 110)
CLOSE_REGIONS = (
    (0, 0, 1280, 110),  # 顶部条带
    (1150, 0, 1280, 400),  # 右侧列上半
)
BLANK_REGION = (300, 0, 900, 60)
BLANK_DEFAULT = (640, 30)

REQUEST_TIMEOUT = 20
MAX_CALLS_PER_FAULT = 3
GLOBAL_LIMIT = 6
GLOBAL_WINDOW = 600
SEND_WIDTH = 640
JPEG_QUALITY = 70

_global_calls = deque()
_global_lock = threading.Lock()

SYSTEM_PROMPT = '你是手游《碧蓝航线》自动脚本的页面恢复助手，只输出 JSON，不输出其他文字。'

USER_PROMPT = """脚本无法识别当前页面，请看截图，从下面的白名单动作里选一个最安全的，帮脚本回到主界面。
截图宽 {w} 像素、高 {h} 像素，坐标用这张截图的像素坐标，原点在左上角。

可选动作：
- back: 点击左上角的返回箭头。可附 x、y。
- home: 点击右上角的「主页/回港」房子图标。必须附 x、y。
- close: 点击弹窗或页面的关闭按钮（X）。必须附 x、y，且只能在画面顶部或右侧边缘。
- tap_blank: 点击画面顶部中间的安全空白处（用于「点击继续」之类）。可附 x、y。
- none: 没有把握，什么都不做。

严禁选择任何确认、购买、开始、出击、领取、使用、消耗资源的操作。拿不准就选 none。

只返回一行 JSON：{{"action": "back|home|close|tap_blank|none", "x": 整数, "y": 整数, "reason": "一句话理由"}}"""


@dataclass
class VlmAction:
    """VLM 给出并通过校验的动作。

    Attributes:
        name (str): 白名单动作名。
        point (tuple[int, int] | None): 1280x720 坐标系下的点击点；none 为 None。
        reason (str): 模型给出的理由，仅用于日志。
    """
    name: str = 'none'
    point: tuple = None
    reason: str = ''


NONE_ACTION = VlmAction()


def _in_region(x, y, region):
    return region[0] <= x <= region[2] and region[1] <= y <= region[3]


def _extract_json(text):
    """从模型回复中提取第一个 JSON 对象，失败返回 None。"""
    if not text:
        return None
    text = text.strip()
    candidates = [text]
    match = re.search(r'\{.*\}', text, re.S)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _to_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def parse_action(text, image_size=SCREEN_SIZE):
    """解析并校验模型回复，不满足白名单或安全区域的一律返回 none。

    Args:
        text (str): 模型原始回复。
        image_size (tuple[int, int]): 发给模型的截图尺寸 (w, h)，用于把坐标换算回 1280x720。

    Returns:
        VlmAction: 通过校验的动作；任何不合规情况返回 name='none'。
    """
    data = _extract_json(text)
    if data is None:
        logger.warning('[VLM] 无法解析模型回复，视为 none')
        return NONE_ACTION

    reason = str(data.get('reason', ''))[:200]
    name = str(data.get('action', '')).strip().lower()
    if name not in ACTIONS:
        logger.warning(f'[VLM] 模型给出白名单外动作 {name!r}，已拒绝。理由: {reason}')
        return NONE_ACTION
    if name == 'none':
        return VlmAction('none', None, reason)

    x, y = _to_number(data.get('x')), _to_number(data.get('y'))
    has_point = x is not None and y is not None
    if has_point:
        width, height = image_size
        if not (0 <= x <= width and 0 <= y <= height):
            logger.warning(f'[VLM] 坐标 ({x}, {y}) 超出截图范围 {image_size}，已拒绝。理由: {reason}')
            return NONE_ACTION
        x = int(round(x * SCREEN_SIZE[0] / width))
        y = int(round(y * SCREEN_SIZE[1] / height))
    elif data.get('x') is not None or data.get('y') is not None:
        logger.warning('[VLM] 坐标格式非法，已拒绝')
        return NONE_ACTION

    if name == 'back':
        point = (x, y) if has_point else BACK_DEFAULT
        regions = (BACK_REGION,)
    elif name == 'tap_blank':
        point = (x, y) if has_point else BLANK_DEFAULT
        regions = (BLANK_REGION,)
    elif name == 'home':
        point = (x, y) if has_point else None
        regions = (HOME_REGION,)
    else:  # close
        point = (x, y) if has_point else None
        regions = CLOSE_REGIONS
    if point is None:
        logger.warning(f'[VLM] 动作 {name} 缺少坐标，已拒绝')
        return NONE_ACTION
    if not any(_in_region(point[0], point[1], region) for region in regions):
        logger.warning(f'[VLM] 动作 {name} 的坐标 {point} 不在安全区域，已拒绝。理由: {reason}')
        return NONE_ACTION
    return VlmAction(name, point, reason)


def encode_image(image, width=SEND_WIDTH):
    """把 RGB 截图缩小并编码为 JPEG base64。

    Returns:
        tuple[str, tuple[int, int]]: (base64 字符串, 缩小后尺寸 (w, h))。
    """
    import base64

    import cv2

    height, orig_width = image.shape[:2]
    new_height = max(1, int(round(height * width / orig_width)))
    small = cv2.resize(image, (width, new_height), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode('.jpg', cv2.cvtColor(small, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise ValueError('JPEG 编码失败')
    return base64.b64encode(buf.tobytes()).decode('ascii'), (width, new_height)


def _request_model(config, b64, size):
    """调用 OpenAI 兼容接口，返回模型回复文本。异常由调用方处理。"""
    from openai import OpenAI

    api_key = getattr(config, 'Error_LlmApiKey', '')
    api_base = getattr(config, 'Error_LlmApiBase', 'https://api.openai.com/v1')
    model = getattr(config, 'Error_VlmModel', '') or getattr(config, 'Error_LlmModel', 'gpt-4o-mini')
    client = OpenAI(api_key=api_key, base_url=api_base, timeout=REQUEST_TIMEOUT, max_retries=0)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': [
                {'type': 'text', 'text': USER_PROMPT.format(w=size[0], h=size[1])},
                {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}},
            ]},
        ],
        timeout=REQUEST_TIMEOUT,
        temperature=0,
        max_tokens=200,
    )
    from module.llm import _get_analysis_from_response
    return _get_analysis_from_response(response)


def _global_allow():
    """全局限频：窗口内请求数未超限则登记并放行。"""
    now = time.monotonic()
    with _global_lock:
        while _global_calls and now - _global_calls[0] > GLOBAL_WINDOW:
            _global_calls.popleft()
        if len(_global_calls) >= GLOBAL_LIMIT:
            return False
        _global_calls.append(now)
        return True


def reset_global_limit():
    """清空全局限频记录（测试用）。"""
    with _global_lock:
        _global_calls.clear()


def choose_action(config, image):
    """请求 VLM 并返回通过校验的动作。任何失败都返回 none，不抛异常。"""
    try:
        if not getattr(config, 'Error_VlmRecover', False):
            return NONE_ACTION
        if not getattr(config, 'Error_LlmApiKey', ''):
            logger.warning('[VLM] 已启用但未配置 LLM API Key，跳过')
            return NONE_ACTION
        if image is None:
            return NONE_ACTION
        if not _global_allow():
            logger.warning('[VLM] 触发全局限频，跳过')
            return NONE_ACTION
        b64, size = encode_image(image)
        text = _request_model(config, b64, size)
        action = parse_action(text, size)
        logger.attr('VLM 动作', action.name)
        logger.info(f'[VLM] 模型理由: {action.reason}')
        return action
    except Exception as e:
        logger.warning(f'[VLM] 请求失败，视为 none: {type(e).__name__}: {e}')
        return NONE_ACTION


class VlmRecover:
    """一次页面故障内的 VLM 恢复会话：最多请求 MAX_CALLS_PER_FAULT 次。

    Pages:
        in: 任意无法识别的页面
        out: 同左（动作执行后由调用方重新截图识别）
    """

    def __init__(self, config):
        self.config = config
        self.calls = 0

    @property
    def available(self):
        """开关开启且本次故障的请求额度未用完。"""
        return bool(getattr(self.config, 'Error_VlmRecover', False)) and self.calls < MAX_CALLS_PER_FAULT

    def try_recover(self, device):
        """请求 VLM 并执行动作。

        Args:
            device: 带 image 属性和 click(button) 方法的设备对象。

        Returns:
            bool: True 表示已点击，调用方应重新截图识别；False 表示未操作。
        """
        if not self.available:
            return False
        self.calls += 1
        logger.hr('VLM 未知页面恢复', level=2)
        logger.attr('VLM 调用', f'{self.calls}/{MAX_CALLS_PER_FAULT}')
        action = choose_action(self.config, getattr(device, 'image', None))
        if action.name == 'none' or action.point is None:
            return False
        x, y = action.point
        button = Button(area=(x - 3, y - 3, x + 4, y + 4), color=(), button=(x - 3, y - 3, x + 4, y + 4),
                        name=f'VLM_{action.name.upper()}')
        device.click(button)
        return True
