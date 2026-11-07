"""Button.match 的多尺度补救：同一页面两种渲染（原生 720p 与 1080p 缩放到 720p）文字相差 1%~3% 缩放。

用 2026-11-04（另一渲染）、2026-11-07（日常渲染）的宿舍页截图和主界面截图（负样本）验证：
原单帧模板能认出两种渲染，主界面仍不匹配，其余按钮没有新增误匹配。
"""

import importlib
import unittest
from pathlib import Path
from unittest.mock import patch

import module.base.button as button_module
from module.base.button import Button
from module.base.utils import load_image
from module.retire import assets as retire_assets
from module.ui import assets as ui_assets

FIXTURES = Path(__file__).parent / 'fixtures' / 'button_multiscale'
IMAGES = {
    'daily': FIXTURES / 'private_quarters_daily.png',
    'alt': FIXTURES / 'private_quarters_alt.png',
    'main': FIXTURES / 'main_page.png',
}
# 用于误匹配检查的资源模块；retire 含纯色模板，用来锁住“纯色模板不做补救”
CHECK_MODULES = ('module.ui.assets', 'module.retire.assets', 'module.handler.assets', 'module.combat.assets')


def load_images():
    return {name: load_image(str(path)) for name, path in IMAGES.items()}


def collect_buttons():
    buttons = []
    for module_name in CHECK_MODULES:
        module = importlib.import_module(module_name)
        for name, value in vars(module).items():
            if isinstance(value, Button) and Path(value.file).exists():
                buttons.append((f'{module_name}.{name}', value))
    return buttons


class ButtonMultiscaleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.images = load_images()

    def disable_multiscale(self):
        return patch.object(button_module, 'TEMPLATE_MULTISCALE_MIN_SIMILARITY', 2.0)

    def test_private_quarters_check_matches_both_renderings(self):
        button = ui_assets.PRIVATE_QUARTERS_CHECK
        self.assertFalse(button.is_gif, '模板应为单帧 png，由多尺度补救兼容两种渲染')
        self.assertTrue(button.match(self.images['daily']))
        self.assertTrue(button.match(self.images['alt']))
        self.assertFalse(button.match(self.images['main']))

    def test_alt_rendering_needs_multiscale(self):
        button = ui_assets.PRIVATE_QUARTERS_CHECK
        with self.disable_multiscale():
            self.assertTrue(button.match(self.images['daily']))
            self.assertFalse(button.match(self.images['alt']))

    def test_offset_stays_close_to_original_area(self):
        button = ui_assets.PRIVATE_QUARTERS_CHECK
        for name in ('daily', 'alt'):
            with self.subTest(name):
                self.assertTrue(button.match(self.images[name]))
                # 标题位置固定，按中心对齐折算后偏移应在几像素内
                for got, want in zip(button.button, button._button):
                    self.assertLessEqual(abs(got - want), 3)

    def test_no_new_false_positive(self):
        """多尺度补救开启前后，除宿舍标题在另一渲染上由假变真外，所有按钮的匹配结果一致。"""
        buttons = collect_buttons()
        self.assertGreater(len(buttons), 300)
        changed = []
        for key, button in buttons:
            for image_name, image in self.images.items():
                with self.disable_multiscale():
                    before = button.match(image)
                after = button.match(image)
                if before != after:
                    changed.append((key, image_name, before, after))
        self.assertEqual(changed, [('module.ui.assets.PRIVATE_QUARTERS_CHECK', 'alt', False, True)])

    def test_uniform_template_is_not_scaled(self):
        """纯色模板缩放后仍是纯色，TM_CCOEFF_NORMED 会退化成 1.0，必须跳过。"""
        button = retire_assets.ENHANCE_AREA_FULL
        for name in ('daily', 'alt'):
            with self.subTest(name):
                self.assertFalse(button.match(self.images[name]))

    def test_small_icon_is_not_scaled(self):
        """小图标缩放 1%~3% 不足 2 像素，补救会让相似形状（返回箭头）误中，必须不做。

        limit_challenge/menu.png 上原先会被多尺度误判的两个按钮：ASH_QUIT（24x30）与 GAME_ROOM_GOTO_MAIN（35x37）。
        """
        from module.os_ash import assets as os_ash_assets
        image = load_image(str(Path(__file__).parent / 'fixtures' / 'limit_challenge' / 'menu.png'))
        self.assertFalse(os_ash_assets.ASH_QUIT.match(image))
        self.assertFalse(ui_assets.GAME_ROOM_GOTO_MAIN.match(image))

    def test_scaled_template_cached_and_released(self):
        button = ui_assets.PRIVATE_QUARTERS_CHECK
        button.match(self.images['alt'])
        first = button._scaled_template(0, 1.03)
        self.assertIs(first, button._scaled_template(0, 1.03))
        button.resource_release()
        self.assertEqual(button._scaled_templates, {})

    def test_scaled_template_larger_than_search_area_is_skipped(self):
        """搜索区域刚好等于模板大小时，放大的模板放不下，只跳过不报错。"""
        button = ui_assets.PRIVATE_QUARTERS_CHECK
        button.match(self.images['alt'], offset=(0, 0))


if __name__ == '__main__':
    unittest.main()
