"""作战档案「消耗档案密钥」弹窗：识别、勾选今日不再提示、确认。

出击后弹出的数据密钥弹窗与红脸弹窗共用 POPUP_CANCEL / POPUP_CONFIRM，
只按这两个通用按钮判定会把出击被弹窗拦住误判成心情异常：取消弹窗、
清零心情并把任务延后到次日。这里固定住三条行为：弹窗被正确确认、
弹窗渲染完之前不误判、不是数据密钥弹窗时不越权处理。
"""

import unittest
from types import SimpleNamespace

import numpy as np

from module.exception import ScriptEnd
from module.handler.assets import (POPUP_CANCEL, POPUP_CONFIRM, USE_DATA_KEY,
                                   USE_DATA_KEY_NOTIFIED)
from module.handler.info_handler import InfoHandler, split_text_lines


class FakeInfoHandler:
    """只挂弹窗处理方法的桩，记录点击并模拟弹窗渲染进度。"""

    _popup_offset = InfoHandler._popup_offset
    use_data_key_notified_enabled = InfoHandler.use_data_key_notified_enabled
    use_data_key_appear = InfoHandler.use_data_key_appear
    handle_use_data_key = InfoHandler.handle_use_data_key
    handle_combat_low_emotion = InfoHandler.handle_combat_low_emotion
    is_low_emotion_popup = InfoHandler.is_low_emotion_popup

    def __init__(self, text_template=False, checkbox=False, popup=True, confirm_result=True,
                 popup_text='第一舰队中爱宕处于低心情状态，强制出击将降低好感且获得经验减半'):
        """
        Args:
            text_template: 黄色「档案密钥」模板是否命中。
            checkbox: 「今日不再提示」复选框是否已渲染出来。
            popup: 双按钮弹窗是否在画面上。
            confirm_result: handle_popup_confirm 是否点到确定。
            popup_text: OCR 读到的弹窗正文，默认是红脸弹窗原文。
        """
        self.popup_text_value = popup_text
        self.text_template = text_template
        self.checkbox_visible = checkbox
        self.checkbox_enabled = False
        self.popup = popup
        self.confirm_result = confirm_result
        self.loop_ticks = 3
        self.clicked = []
        self.confirmed = []
        self.config = SimpleNamespace(USE_DATA_KEY=True, is_os=False, task_delay=lambda **kwargs: None)
        self.device = SimpleNamespace(click=self.click)
        self.emotion = SimpleNamespace(is_calculate=False, is_ignore=True)
        self.emotion.emergency_reset = lambda: None

    def loop(self, skip_first=True, timeout=None):
        # 每次迭代当作一次截图，迭代若干次后超时退出
        for _ in range(self.loop_ticks):
            yield None

    def appear(self, button, offset=0, interval=0, similarity=0.85, threshold=10):
        if button is USE_DATA_KEY:
            return self.text_template
        if button is POPUP_CONFIRM or button is POPUP_CANCEL:
            return self.popup
        raise AssertionError(f'未预期的按钮: {button}')

    def image_color_count(self, button, color, threshold=30, count=50):
        if button is not USE_DATA_KEY_NOTIFIED:
            raise AssertionError(f'未预期的按钮: {button}')
        if color == (140, 207, 66):
            return self.checkbox_enabled
        # 深色方块：未勾选时才在
        return self.checkbox_visible and not self.checkbox_enabled

    def click(self, button, *args, **kwargs):
        if button is USE_DATA_KEY_NOTIFIED:
            self.clicked.append('今日不再提示')
            self.checkbox_enabled = True
        else:
            self.clicked.append(str(button))

    def handle_popup_confirm(self, name='', offset=None, interval=2):
        if self.confirm_result:
            self.clicked.append('确定')
            self.confirmed.append(name)
        return self.confirm_result

    def popup_text(self):
        return self.popup_text_value

    def handle_popup_cancel(self, name='', offset=None, interval=2):
        self.clicked.append('取消')
        return True

    def _emotion_emergency_exit(self):
        pass


class TestUseDataKeyPopup(unittest.TestCase):
    def test_text_template_confirmed(self):
        """模板命中：先勾选今日不再提示，再点确定。"""
        fake = FakeInfoHandler(text_template=True, checkbox=True)

        self.assertTrue(fake.handle_use_data_key())

        self.assertEqual(fake.clicked, ['今日不再提示', '确定'])
        self.assertEqual(fake.confirmed, ['USE_DATA_KEY'])
        self.assertFalse(fake.config.USE_DATA_KEY)

    def test_checkbox_identifies_popup_when_template_missed(self):
        """弹窗文字没渲染出来（模板失配）时，靠复选框仍能认出这个弹窗。"""
        fake = FakeInfoHandler(text_template=False, checkbox=True)

        self.assertTrue(fake.handle_use_data_key())

        self.assertIn('今日不再提示', fake.clicked)
        self.assertIn('确定', fake.clicked)

    def test_waits_until_popup_rendered(self):
        """刚弹出的第一帧什么都没渲染：等一次截图后再处理，而不是放弃。"""
        fake = FakeInfoHandler(text_template=False, checkbox=False)
        raw_appear = fake.use_data_key_appear
        judged = []

        def appear():
            judged.append(None)
            if len(judged) > 1:
                fake.checkbox_visible = True
            return raw_appear()

        fake.use_data_key_appear = appear

        self.assertTrue(fake.handle_use_data_key())

        self.assertGreater(len(judged), 1)
        self.assertEqual(fake.clicked, ['今日不再提示', '确定'])

    def test_other_popup_is_left_alone(self):
        """不是数据密钥弹窗（如红脸弹窗）：不点确定、不清除预期状态。"""
        fake = FakeInfoHandler(text_template=False, checkbox=False)

        self.assertFalse(fake.handle_use_data_key())

        self.assertEqual(fake.clicked, [])
        self.assertEqual(fake.confirmed, [])
        self.assertTrue(fake.config.USE_DATA_KEY)

    def test_unverified_popup_keeps_expectation(self):
        """确定没点成时保留预期状态，下次循环还能再处理这个弹窗。"""
        fake = FakeInfoHandler(text_template=True, checkbox=True, confirm_result=False)

        self.assertFalse(fake.handle_use_data_key())

        self.assertTrue(fake.config.USE_DATA_KEY)


class TestLowEmotionWithDataKey(unittest.TestCase):
    def test_data_key_popup_is_not_red_face_popup(self):
        """作战档案的双按钮弹窗按数据密钥弹窗处理，不触发心情保底。"""
        fake = FakeInfoHandler(text_template=True, checkbox=True)
        fake.emotion = SimpleNamespace(is_calculate=True, is_ignore=False)

        self.assertTrue(fake.handle_combat_low_emotion())

        self.assertNotIn('取消', fake.clicked)
        self.assertIn('确定', fake.clicked)

    def test_red_face_popup_still_guarded(self):
        """不是数据密钥弹窗时，红脸弹窗保底逻辑保持原样。"""
        fake = FakeInfoHandler(text_template=False, checkbox=False)
        fake.emotion = SimpleNamespace(is_calculate=True, is_ignore=False)
        reset = []
        fake.emotion.emergency_reset = lambda: reset.append(None)

        with self.assertRaises(ScriptEnd):
            fake.handle_combat_low_emotion()

        self.assertEqual(fake.clicked, ['取消'])
        self.assertEqual(len(reset), 1)

    def test_other_two_button_popup_is_not_red_face_popup(self):
        """正文不是低心情警告的双按钮弹窗：不取消、不确认、不清零心情。

        16 图进图后的潜艇支援剧情，跳过未读剧情会弹确认框，曾被当成红脸
        弹窗取消，导致心情清零、任务延后到下次刷新。
        """
        for is_calculate, is_ignore in ((True, False), (False, True), (True, True)):
            for text in ('潜艇支援舰队出击消耗10点石油，是否继续?', '真的要撤退吗?', ''):
                with self.subTest(is_calculate=is_calculate, is_ignore=is_ignore, text=text):
                    fake = FakeInfoHandler(text_template=False, checkbox=False, popup_text=text)
                    fake.emotion = SimpleNamespace(is_calculate=is_calculate, is_ignore=is_ignore)
                    reset = []
                    fake.emotion.emergency_reset = lambda: reset.append(None)

                    self.assertFalse(fake.handle_combat_low_emotion())

                    self.assertEqual(fake.clicked, [])
                    self.assertEqual(reset, [])

    def test_red_face_popup_confirmed_in_ignore_mode(self):
        """ignore 模式下，正文确认是红脸弹窗才点确定。"""
        fake = FakeInfoHandler(text_template=False, checkbox=False)
        fake.emotion = SimpleNamespace(is_calculate=False, is_ignore=True)
        fake.interval_reset = lambda button: None

        self.assertTrue(fake.handle_combat_low_emotion())

        self.assertEqual(fake.clicked, ['确定'])

    def test_no_popup_skips_ocr(self):
        """画面上没有双按钮弹窗时不做 OCR。"""
        fake = FakeInfoHandler(popup=False)
        fake.popup_text = lambda: self.fail('不应读取弹窗正文')

        self.assertFalse(fake.is_low_emotion_popup())


class TestSplitTextLines(unittest.TestCase):
    def test_two_lines_and_noise(self):
        """两行文字切成两段，零星噪点行被丢弃。"""
        letters = np.full((120, 200), 255, dtype=np.uint8)
        letters[20:42, 30:170] = 0
        letters[54:76, 50:150] = 0
        letters[100:102, 10:14] = 0

        self.assertEqual(split_text_lines(letters), [(20, 41), (54, 75)])

    def test_blank(self):
        self.assertEqual(split_text_lines(np.full((50, 50), 255, dtype=np.uint8)), [])


if __name__ == '__main__':
    unittest.main()
