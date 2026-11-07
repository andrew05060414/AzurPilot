"""VLM 未知页面恢复测试：全部 mock 模型接口，不发真实请求。"""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import numpy as np

from module import vlm_recover
from module.vlm_recover import MAX_CALLS_PER_FAULT, VlmRecover, choose_action, parse_action

FIELD_SCREENSHOT = r'D:/模拟器/AzurPilot/log/error/alas/1794064941638/2026-11-07_10-21-46-095748.png'


def make_config(enabled=True, api_key='sk-test', vlm_model='', llm_model='llm-default'):
    return SimpleNamespace(
        Error_VlmRecover=enabled,
        Error_LlmApiKey=api_key,
        Error_LlmApiBase='https://example.invalid/v1',
        Error_LlmModel=llm_model,
        Error_VlmModel=vlm_model,
        Emulator_ControlMethod='ADB',
    )


def make_image():
    return np.full((720, 1280, 3), 200, dtype=np.uint8)


def fake_openai(reply_text=None, exc=None):
    """返回一个替换 openai.OpenAI 的 mock，客户端对话返回 reply_text 或抛 exc。"""
    client = MagicMock()
    if exc is not None:
        client.chat.completions.create.side_effect = exc
    else:
        message = SimpleNamespace(content=reply_text)
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=message)])
    factory = MagicMock(return_value=client)
    return factory, client


def reply(action, x=None, y=None, reason='测试'):
    data = {'action': action, 'reason': reason}
    if x is not None:
        data['x'] = x
        data['y'] = y
    return json.dumps(data, ensure_ascii=False)


class VlmTestCase(unittest.TestCase):
    def setUp(self):
        vlm_recover.reset_global_limit()


class TestParseAction(VlmTestCase):
    def test_back_default_point(self):
        action = parse_action(reply('back'))
        self.assertEqual('back', action.name)
        self.assertEqual(vlm_recover.BACK_DEFAULT, action.point)

    def test_coordinates_scaled_from_sent_image(self):
        # 发给模型的是 640x360 缩略图，(25, 22) 对应原图 (50, 44)
        action = parse_action(reply('back', 25, 22), image_size=(640, 360))
        self.assertEqual((50, 44), action.point)

    def test_fenced_json_is_accepted(self):
        action = parse_action('```json\n' + reply('home', 1100, 40) + '\n```')
        self.assertEqual('home', action.name)

    def test_action_outside_whitelist_rejected(self):
        for name in ('confirm', 'buy', 'start', 'click', ''):
            self.assertEqual('none', parse_action(reply(name, 640, 30)).name, name)

    def test_out_of_image_rejected(self):
        self.assertEqual('none', parse_action(reply('back', 5000, 40)).name)
        self.assertEqual('none', parse_action(reply('back', -5, 40)).name)

    def test_outside_safe_region_rejected(self):
        # 在图内，但不在各动作的安全区域里（例如中下部的确认按钮位置）
        self.assertEqual('none', parse_action(reply('back', 640, 600)).name)
        self.assertEqual('none', parse_action(reply('home', 640, 600)).name)
        self.assertEqual('none', parse_action(reply('close', 640, 600)).name)
        self.assertEqual('none', parse_action(reply('tap_blank', 1100, 650)).name)

    def test_home_and_close_need_coordinates(self):
        self.assertEqual('none', parse_action(reply('home')).name)
        self.assertEqual('none', parse_action(reply('close')).name)

    def test_close_on_top_edge_accepted(self):
        action = parse_action(reply('close', 1200, 60))
        self.assertEqual(('close', (1200, 60)), (action.name, action.point))

    def test_garbage_rejected(self):
        for text in ('', 'back', '{not json', '[1, 2]', None):
            self.assertEqual('none', parse_action(text).name)
        self.assertEqual('none', parse_action('{"action": "back", "x": "abc", "y": 3}').name)
        self.assertEqual('none', parse_action('{"action": "back", "x": true, "y": 3}').name)


class TestChooseAction(VlmTestCase):
    def test_disabled_does_not_call_api(self):
        factory, client = fake_openai(reply('back'))
        with patch('openai.OpenAI', factory):
            action = choose_action(make_config(enabled=False), make_image())
        self.assertEqual('none', action.name)
        factory.assert_not_called()

    def test_missing_api_key_does_not_call_api(self):
        factory, client = fake_openai(reply('back'))
        with patch('openai.OpenAI', factory):
            self.assertEqual('none', choose_action(make_config(api_key=''), make_image()).name)
        factory.assert_not_called()

    def test_back_ok_and_request_shape(self):
        factory, client = fake_openai(reply('back', 25, 22, reason='左上角有返回箭头'))
        with patch('openai.OpenAI', factory):
            action = choose_action(make_config(), make_image())
        self.assertEqual(('back', (50, 44), '左上角有返回箭头'), (action.name, action.point, action.reason))
        self.assertEqual(vlm_recover.REQUEST_TIMEOUT, factory.call_args.kwargs['timeout'])
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertEqual('llm-default', kwargs['model'])
        image_part = kwargs['messages'][1]['content'][1]
        self.assertTrue(image_part['image_url']['url'].startswith('data:image/jpeg;base64,'))

    def test_vlm_model_overrides_llm_model(self):
        factory, client = fake_openai(reply('none'))
        with patch('openai.OpenAI', factory):
            choose_action(make_config(vlm_model='vision-x'), make_image())
        self.assertEqual('vision-x', client.chat.completions.create.call_args.kwargs['model'])

    def test_timeout_returns_none(self):
        import openai
        factory, client = fake_openai(exc=openai.APITimeoutError(request=Mock()))
        with patch('openai.OpenAI', factory):
            self.assertEqual('none', choose_action(make_config(), make_image()).name)

    def test_generic_error_returns_none(self):
        factory, client = fake_openai(exc=RuntimeError('boom'))
        with patch('openai.OpenAI', factory):
            self.assertEqual('none', choose_action(make_config(), make_image()).name)

    def test_empty_reply_returns_none(self):
        factory, client = fake_openai(reply_text=None)
        with patch('openai.OpenAI', factory):
            self.assertEqual('none', choose_action(make_config(), make_image()).name)

    def test_global_rate_limit(self):
        factory, client = fake_openai(reply('none'))
        with patch('openai.OpenAI', factory):
            for _ in range(vlm_recover.GLOBAL_LIMIT + 3):
                choose_action(make_config(), make_image())
        self.assertEqual(vlm_recover.GLOBAL_LIMIT, client.chat.completions.create.call_count)

    @unittest.skipUnless(os.path.exists(FIELD_SCREENSHOT), '现场截图不在本机')
    def test_field_screenshot_back(self):
        from PIL import Image
        image = np.array(Image.open(FIELD_SCREENSHOT).convert('RGB'))
        factory, client = fake_openai(reply('back', 25, 22, reason='选择宿舍页左上角返回'))
        with patch('openai.OpenAI', factory):
            action = choose_action(make_config(), image)
        self.assertEqual('back', action.name)


class TestVlmRecoverSession(VlmTestCase):
    def make_device(self):
        return SimpleNamespace(image=make_image(), click=Mock())

    def test_back_is_clicked(self):
        factory, client = fake_openai(reply('back', 25, 22))
        device = self.make_device()
        with patch('openai.OpenAI', factory):
            self.assertTrue(VlmRecover(make_config()).try_recover(device))
        device.click.assert_called_once()
        button = device.click.call_args.args[0]
        self.assertEqual('VLM_BACK', str(button))
        x1, y1, x2, y2 = button.button
        self.assertTrue(x1 <= 50 <= x2 and y1 <= 44 <= y2)

    def test_rejected_action_does_not_click(self):
        for text in (reply('confirm', 640, 600), reply('back', 640, 600), reply('none')):
            factory, client = fake_openai(text)
            device = self.make_device()
            with patch('openai.OpenAI', factory):
                self.assertFalse(VlmRecover(make_config()).try_recover(device))
            device.click.assert_not_called()

    def test_disabled_not_available(self):
        factory, client = fake_openai(reply('back'))
        session = VlmRecover(make_config(enabled=False))
        device = self.make_device()
        with patch('openai.OpenAI', factory):
            self.assertFalse(session.available)
            self.assertFalse(session.try_recover(device))
        factory.assert_not_called()

    def test_per_fault_limit(self):
        factory, client = fake_openai(reply('back'))
        session = VlmRecover(make_config())
        device = self.make_device()
        with patch('openai.OpenAI', factory):
            results = [session.try_recover(device) for _ in range(MAX_CALLS_PER_FAULT + 2)]
        self.assertEqual([True] * MAX_CALLS_PER_FAULT + [False] * 2, results)
        self.assertEqual(MAX_CALLS_PER_FAULT, client.chat.completions.create.call_count)
        self.assertFalse(session.available)


class TestUiIntegration(VlmTestCase):
    """ui_get_current_page 在页面始终认不出时，应先让 VLM 尝试，用完额度后才走原有出口。"""

    def build_ui(self, config):
        from module.exception import GamePageUnknownError
        from module.ui.ui import UI

        ui = UI.__new__(UI)
        ui.__dict__['config'] = config
        ui.device = SimpleNamespace(
            image=make_image(), has_cached_image=True, screenshot=Mock(),
            click=Mock(), app_is_running=Mock(return_value=True), get_orientation=Mock(),
        )
        return ui, GamePageUnknownError

    def run_unknown(self, config, reply_text):
        ui, error = self.build_ui(config)
        factory, client = fake_openai(reply_text)
        fake_timer = Mock()
        fake_timer.start.return_value = fake_timer
        fake_timer.reached.return_value = True
        with patch('openai.OpenAI', factory), \
                patch('module.ui.ui.Timer', return_value=fake_timer), \
                patch.object(type(ui), 'ui_page_appear', return_value=False), \
                patch.object(type(ui), 'appear_then_click', return_value=False), \
                patch.object(type(ui), 'ui_additional', return_value=False):
            with self.assertRaises(error):
                ui.ui_get_current_page(skip_first_screenshot=True, recover_unknown=False)
        return ui, client

    def test_vlm_runs_up_to_limit_then_falls_through(self):
        ui, client = self.run_unknown(make_config(), reply('back', 25, 22))
        self.assertEqual(MAX_CALLS_PER_FAULT, client.chat.completions.create.call_count)
        self.assertEqual(MAX_CALLS_PER_FAULT, ui.device.click.call_count)
        # 每次动作后都重新截图
        self.assertGreaterEqual(ui.device.screenshot.call_count, MAX_CALLS_PER_FAULT)

    def test_disabled_keeps_old_behavior(self):
        ui, client = self.run_unknown(make_config(enabled=False), reply('back', 25, 22))
        client.chat.completions.create.assert_not_called()
        ui.device.click.assert_not_called()

    def test_none_stops_immediately(self):
        ui, client = self.run_unknown(make_config(), reply('none'))
        self.assertEqual(1, client.chat.completions.create.call_count)
        ui.device.click.assert_not_called()


if __name__ == '__main__':
    unittest.main()
