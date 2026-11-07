"""出击菜单横幅导航总超时测试。

横幅导航的局部计时器会在每次点击／滑动后被重置，原先只靠它判断超时，
横幅一直能「成功」点击时循环永远不会结束。现在增加从入口起算、不随点击重置的总超时。
"""
import unittest
from unittest import mock

import module.campaign.campaign_event as campaign_event_module
from module.campaign.campaign_event import CAMPAIGN_BANNER_GOTO_GLOBAL_TIMEOUT, CampaignEvent
from module.ui.assets import CAMPAIGN_MENU_GOTO_EVENT
from module.ui.page import page_campaign_menu, page_event

# 防止实现回退时测试真的死循环：超过这个截图次数直接失败。
MAX_SCREENSHOTS = 1000


class _NeverReachedTimer:
    """替代局部 Timer：永远不会自行超时，只能靠总超时退出。"""

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        return self

    def reset(self):
        return self

    def reached(self):
        return False


class _FakeClock:
    """替代 monotonic：每次读取前进 1 秒。"""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 1.0
        return self.now


def _make_campaign_event():
    event = object.__new__(CampaignEvent)
    event.device = mock.Mock()
    screenshot_count = {'n': 0}

    def screenshot():
        screenshot_count['n'] += 1
        if screenshot_count['n'] > MAX_SCREENSHOTS:
            raise RuntimeError('横幅导航循环超过截图上限，总超时没有生效')

    event.device.screenshot = mock.Mock(side_effect=screenshot)
    event.device.swipe_vector = mock.Mock()
    event._screenshot_count = screenshot_count
    # 出击菜单可见；横幅点击总是成功（模拟每次点击都重置局部计时器）
    event.ui_page_appear = mock.Mock(return_value=False)
    event.appear = mock.Mock(side_effect=lambda button, offset=(0, 0), **kwargs: button is page_campaign_menu.check_button)
    event.appear_then_click = mock.Mock(return_value=True)
    event.ui_additional = mock.Mock(return_value=False)
    return event


class CampaignBannerGlobalTimeoutTest(unittest.TestCase):
    def test_successful_clicks_do_not_bypass_global_timeout(self):
        event = _make_campaign_event()
        clock = _FakeClock()
        with mock.patch.object(campaign_event_module, 'Timer', _NeverReachedTimer), \
                mock.patch.object(campaign_event_module, 'monotonic', clock):
            result = event._ui_goto_campaign_banner_page(page_event)

        # 超时应返回失败，而不是无限循环
        self.assertFalse(result)
        # 循环确实一直在「成功」点击横幅，退出只能来自总超时
        event.appear_then_click.assert_called_with(CAMPAIGN_MENU_GOTO_EVENT, offset=(30, 30), interval=3)
        self.assertGreater(event.appear_then_click.call_count, 0)
        # 截图次数应与总超时时长同量级，而不是接近上限
        self.assertLessEqual(event._screenshot_count['n'], CAMPAIGN_BANNER_GOTO_GLOBAL_TIMEOUT + 5)

    def test_reaching_destination_before_deadline_returns_true(self):
        event = _make_campaign_event()
        # 第一轮两个成功页都未命中，第二轮 page_event 命中
        event.ui_page_appear = mock.Mock(side_effect=[False, False, True])
        clock = _FakeClock()
        with mock.patch.object(campaign_event_module, 'Timer', _NeverReachedTimer), \
                mock.patch.object(campaign_event_module, 'monotonic', clock):
            result = event._ui_goto_campaign_banner_page(page_event)
        self.assertTrue(result)


if __name__ == '__main__':
    unittest.main()
