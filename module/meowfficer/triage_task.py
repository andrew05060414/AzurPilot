"""指挥喵批量整理：扫描猫窝 → 按 :mod:`module.meowfficer.triage` 分拣 → 按结论上锁 / 解锁。

上锁的猫不会出现在强化（喂养）材料里，所以「留谁」完全由锁决定：
胚子与同名库存上锁，饲料解锁，之后由强化流程把饲料喂给胚子。

两步走：
1. ``plan``：只读扫描并写出分拣清单（``./log/meowfficer_triage.md`` / ``.json``），不改任何状态。
2. ``apply``：重新按同样的顺序遍历猫窝，核对每个位置上的猫与清单一致后才改锁；
   任何一个位置对不上（猫名、等级不一致），立刻停止，不在错位的情况下继续改锁。

页面操作沿用 :class:`~module.meowfficer.scan.MeowfficerScanner` 的猫窝遍历。
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from module.logger import logger
from module.meowfficer.assets import MEOWFFICER_CAT_LOCKED, MEOWFFICER_CAT_UNLOCKED
from module.meowfficer.scan import MeowfficerScanner
from module.meowfficer.scan_utils import MEOWFFICER_CATTERY_GRID
from module.meowfficer.triage import (FEED, KEEP_EMBRYO, KEEP_IN_USE, KEEP_STOCK, CatRecord, Verdict,
                                      summary, triage)

ACTION_TEXT = {KEEP_EMBRYO: '胚子（上锁）', KEEP_STOCK: '同名库存（上锁）',
               KEEP_IN_USE: '不动', FEED: '饲料（解锁）'}
REPORT_PATH = './log/meowfficer_triage'


def desired_lock(verdict: Verdict) -> bool | None:
    """分拣结论对应的目标锁状态；``None`` 表示保持原样。"""
    if verdict.action in (KEEP_EMBRYO, KEEP_STOCK):
        return True
    if verdict.action == FEED:
        return False
    return None


def render_report(verdicts: list[Verdict], generated_at: str) -> str:
    """把分拣结果写成给人看的 Markdown 清单。"""
    counts = summary(verdicts)
    lines = ['# 指挥喵分拣清单', '', f'生成时间：{generated_at}', '',
             '| 结论 | 只数 |', '| --- | --- |']
    for action in (KEEP_EMBRYO, KEEP_STOCK, KEEP_IN_USE, FEED):
        lines.append(f'| {ACTION_TEXT[action]} | {counts.get(action, 0)} |')
    changes = [v for v in verdicts if desired_lock(v) is not None and v.record.locked is not None
               and desired_lock(v) != v.record.locked]
    lines += ['', f'需要改锁：{len(changes)} 只（上锁 '
              f'{sum(1 for v in changes if desired_lock(v))}，解锁 {sum(1 for v in changes if not desired_lock(v))}）', '']
    for action in (KEEP_EMBRYO, KEEP_STOCK, KEEP_IN_USE, FEED):
        group = sorted((v for v in verdicts if v.action == action), key=lambda v: (v.cat, -v.score))
        if not group:
            continue
        lines += [f'## {ACTION_TEXT[action]}（{len(group)}）', '',
                  '| # | 猫 | Lv | 原锁 | 分 | 天赋 | 理由 |', '| --- | --- | --- | --- | --- | --- | --- |']
        for v in group:
            talents = '、'.join(str(t.name if hasattr(t, 'name') else t) for t in v.record.talents)
            lock = {True: '锁', False: '', None: '?'}[v.record.locked]
            lines.append(f'| {v.record.index} | {v.cat} | {v.record.level or "?"} | {lock} | {v.score} '
                         f'| {talents} | {v.reason} |')
        lines.append('')
    return '\n'.join(lines)


def to_payload(verdicts: list[Verdict], generated_at: str) -> dict:
    return {
        'generatedAt': generated_at,
        'summary': summary(verdicts),
        'cats': [{
            'index': v.record.index, 'name': v.record.name, 'cat': v.cat, 'level': v.record.level,
            'locked': v.record.locked, 'action': v.action, 'score': v.score, 'reason': v.reason,
            'specials': v.specials, 'junk': v.junk,
            'talents': [t.name if hasattr(t, 'name') else str(t) for t in v.record.talents],
        } for v in verdicts],
    }


class MeowfficerTriage(MeowfficerScanner):
    """批量整理猫窝：扫描、分拣、按清单改锁。"""

    def plan(self) -> list[Verdict]:
        """只读扫描并生成分拣清单。

        Pages:
            in: 任意（会自行进入指挥喵猫窝）
            out: 指挥喵猫窝
        """
        logger.hr('指挥喵分拣：扫描', level=1)
        self.scan_all(passes=20)
        records = [CatRecord(name=r['name'], talents=r['talents'], level=r['level'], locked=r['locked'],
                             index=i + 1) for i, r in enumerate(self.records)]
        verdicts = triage(records)
        self._plan_slots = [(r['page'], r['slot']) for r in self.records]
        self._save(verdicts)
        logger.attr('[指挥喵-分拣] 结论', summary(verdicts))
        if self.incomplete:
            logger.error('[指挥喵-分拣] 扫描中途离开了猫窝，清单不完整，不会按它改锁')
        return verdicts

    def _save(self, verdicts: list[Verdict]) -> None:
        stamp = f'{datetime.now():%Y-%m-%d %H:%M:%S}'
        os.makedirs(os.path.dirname(os.path.abspath(REPORT_PATH)), exist_ok=True)
        with open(REPORT_PATH + '.md', 'w', encoding='utf-8') as f:
            f.write(render_report(verdicts, stamp))
        with open(REPORT_PATH + '.json', 'w', encoding='utf-8') as f:
            json.dump(to_payload(verdicts, stamp), f, ensure_ascii=False, indent=1)
        logger.info(f'[指挥喵-分拣] 清单已写入 {REPORT_PATH}.md')

    # ------------------------------------------------------------------
    # 改锁
    # ------------------------------------------------------------------

    def _set_lock(self, lock: bool) -> bool:
        """把当前选中的猫切到指定锁状态，返回是否成功。

        Pages:
            in: 指挥喵猫窝（已选中目标猫）
            out: 指挥喵猫窝
        """
        target = MEOWFFICER_CAT_LOCKED if lock else MEOWFFICER_CAT_UNLOCKED
        source = MEOWFFICER_CAT_UNLOCKED if lock else MEOWFFICER_CAT_LOCKED
        for _ in range(3):
            self.device.screenshot()
            if self.appear(target, offset=(10, 10), similarity=0.9):
                return True
            if self.appear(source, offset=(10, 10), similarity=0.9):
                self.device.click(source)
                self._wait_stable(MEOWFFICER_CAT_LOCKED.area, timeout=2)
                self.device.stuck_record_clear()
                continue
            # 锁按钮被弹窗挡住：沿用扫描里的结算弹窗处理
            if not self._dismiss_play_popup():
                break
        self.device.screenshot()
        return self.appear(target, offset=(10, 10), similarity=0.9)

    def _talents_match(self, verdict: Verdict, ocr) -> bool:
        """打开天赋页重读当前猫的天赋，与清单比对（按天赋名集合）。

        Pages:
            in: 指挥喵猫窝（已选中目标猫）
            out: 指挥喵猫窝
        """
        if not self._open_talent():
            return False
        talents = self._read_talents(ocr)
        if not self._back_to_cattery():
            self._ensure_cattery()
            return False
        seen = {t.name if hasattr(t, 'name') else str(t) for t in talents}
        expected = {t.name if hasattr(t, 'name') else str(t) for t in verdict.record.talents}
        return bool(seen) and seen == expected

    def apply(self, verdicts: list[Verdict]) -> dict:
        """按清单逐只改锁。顺序必须与 :meth:`plan` 的扫描顺序一致（同一次运行内、猫窝未变动）。

        Returns:
            dict: ``{'locked': n, 'unlocked': n, 'skipped': n}``。

        Pages:
            in: 指挥喵猫窝
            out: 指挥喵猫窝
        """
        logger.hr('指挥喵分拣：改锁', level=1)
        if self.incomplete:
            logger.error('[指挥喵-分拣] 清单不完整，拒绝改锁')
            return {'locked': 0, 'unlocked': 0, 'skipped': 0}
        stats = {'locked': 0, 'unlocked': 0, 'skipped': 0}
        mismatch = 0
        by_slot = dict(zip(self._plan_slots, verdicts))
        last_page = max((p for p, _ in self._plan_slots), default=0)
        ocr = self._load_ocr()
        self._ensure_cattery()
        self._reset_cattery_scroll()

        for page in range(1, last_page + 1):
            previous = ''
            for index, button in enumerate(MEOWFFICER_CATTERY_GRID.buttons, 1):
                verdict = by_slot.get((page, index))
                if verdict is None:
                    continue
                want = desired_lock(verdict)
                if want is None or want == verdict.record.locked:
                    continue
                cat = self._select_card(button, ocr, previous=previous)
                previous = cat or previous
                level, locked = self._read_card_state()
                if cat != verdict.record.name or (verdict.record.level and level != verdict.record.level):
                    logger.error(f'[指挥喵-分拣] 第 {page} 屏第 {index} 张对不上清单：'
                                 f'看到 {cat} Lv{level}，清单是 {verdict.record.name} Lv{verdict.record.level}。'
                                 '停止改锁，请重新生成清单')
                    return stats
                if locked == want:
                    stats['skipped'] += 1
                    continue
                # 同名猫常常连着十几只（例如 16 只庞德喵），翻屏有惯性误差时只核对猫名会错位；
                # 改锁前再读一次天赋，和清单逐条对上才动手
                if not self._talents_match(verdict, ocr):
                    mismatch += 1
                    stats['skipped'] += 1
                    logger.warning(f'[指挥喵-分拣] #{verdict.record.index} {cat} 天赋与清单不一致，这只不动')
                    if mismatch >= 3:
                        logger.error('[指挥喵-分拣] 连续 3 只对不上，多半是翻屏错位。停止改锁，请重新生成清单')
                        return stats
                    continue
                mismatch = 0
                if self._set_lock(want):
                    stats['locked' if want else 'unlocked'] += 1
                    logger.info(f'[指挥喵-分拣] #{verdict.record.index} {cat} -> {"上锁" if want else "解锁"}')
                else:
                    logger.warning(f'[指挥喵-分拣] #{verdict.record.index} {cat} 改锁失败，跳过')
                    stats['skipped'] += 1
            if page < last_page and self._advance_cattery_screen() < 40:
                logger.warning('[指挥喵-分拣] 列表没有继续滚动，提前结束')
                break
        logger.attr('[指挥喵-分拣] 改锁结果', stats)
        return stats


def run_meowfficer_triage(config, device=None, apply: bool = False):
    """工具入口：默认只出清单；``apply=True`` 时出清单后立即按清单改锁。"""
    task = MeowfficerTriage(config, device=device)
    verdicts = task.plan()
    if apply:
        task.apply(verdicts)
    return verdicts


class MeowfficerSweep(MeowfficerTriage):
    """一遍过的猫窝整理：只看「1 级、没上锁」的猫，读完天赋当场决定上不上锁。

    用户口径：满级 / 养过的猫不用管，已经上锁的是用户自己挑的，也不用管；
    只处理新开出来的 1 级猫。所以这里：

    - 猫窝按等级**升序**排，编队中的猫置顶，之后就是 1 级猫，遇到等级 > 1 的就结束；
    - 已上锁的 1 级猫只记品种（算进同名库存），不读天赋；
    - 没上锁的 1 级猫读天赋：胚子当场上锁，其余不动；同名库存不上锁，由喂猫步骤负责每个品种留 4 只；
    - 永远不解锁任何猫。

    不再「先扫一遍出清单、再扫一遍改锁」，也就不存在两遍之间对不上位置的问题。
    滚动位置按实测位移累加，并用滚动条消除同名猫成排时的周期歧义，
    每张卡按绝对行号只处理一次。
    """

    def _cattery_capacity(self) -> int:
        from module.meowfficer.enhance import MEOWFFICER_ENHANCE_CAPACITY
        current, _, total = MEOWFFICER_ENHANCE_CAPACITY.ocr(self.device.image)
        return current or total or 160

    def _thumb(self) -> float:
        from module.meowfficer.enhance import MEOWFFICER_CATTERY_SCROLL
        return MEOWFFICER_CATTERY_SCROLL.cal_position(main=self)

    def _scroll_once(self, scroll_range: float) -> int:
        """向下翻约一行多，返回消歧后的实际内容位移。"""
        from module.meowfficer.scan_utils import (CATTERY_PANEL_AREA, CATTERY_SWIPE_STEP, _crop,
                                                  resolve_scroll, scroll_offset)
        self.device.screenshot()
        before = _crop(self.device.image, CATTERY_PANEL_AREA).copy()
        thumb_before = self._thumb()
        self._swipe_panel(CATTERY_PANEL_AREA, distance=CATTERY_SWIPE_STEP, duration=0.7)
        self._wait_stable(CATTERY_PANEL_AREA, timeout=3)
        self.device.screenshot()
        after = _crop(self.device.image, CATTERY_PANEL_AREA).copy()
        image_dy = scroll_offset(before, after)
        thumb_dy = (self._thumb() - thumb_before) * scroll_range
        moved = resolve_scroll(image_dy, thumb_dy)
        logger.info(f'[指挥喵-整理] 滚动：画面 {image_dy}px，滚动条约 {thumb_dy:.0f}px，取 {moved}px')
        return moved

    def _card_button(self, column: int, center_y: int):
        from module.base.button import Button
        from module.meowfficer.scan_utils import CATTERY_COLUMN_X
        x = CATTERY_COLUMN_X[column]
        area = (x - 25, center_y - 25, x + 25, center_y + 25)
        return Button(area=area, color=(), button=area, name=f'CATTERY_CARD_{column}_{center_y}')

    def sweep(self, stock_per_species: int = 4) -> dict:
        """执行一遍整理，返回统计。

        Pages:
            in: 任意（会自行进入指挥喵猫窝）
            out: 指挥喵猫窝
        """
        from collections import Counter

        from module.meowfficer.scan_utils import (CATTERY_CLICK_Y_RANGE, CATTERY_FIRST_ROW_Y,
                                                  CATTERY_ROW_PITCH)
        from module.meowfficer.triage import assess

        logger.hr('指挥喵整理：只看 1 级未上锁的猫', level=1)
        ocr = self._load_ocr()
        self._ensure_cattery()
        self._reset_cattery_scroll()
        self.device.screenshot()
        capacity = self._cattery_capacity()
        rows = (capacity + 3) // 4
        scroll_range = max(1, rows * CATTERY_ROW_PITCH - 438)
        logger.attr('[指挥喵-整理] 猫窝', f'{capacity} 只，约 {rows} 行')

        stock = Counter()          # 品种 -> 已上锁的 1 级猫数（含用户自己锁的）
        embryo_species = set()
        stats = Counter()
        records = []
        content_y = 0
        next_row = 0               # 下一个要处理的绝对行号
        seen_level1 = False
        previous = ''
        stalls = 0

        while next_row < rows:
            progressed = False
            while next_row < rows:
                center_y = CATTERY_FIRST_ROW_Y + next_row * CATTERY_ROW_PITCH - content_y
                if center_y > CATTERY_CLICK_Y_RANGE[1]:
                    break
                if center_y < CATTERY_CLICK_Y_RANGE[0]:
                    logger.warning(f'[指挥喵-整理] 第 {next_row + 1} 行已滚出可点范围，跳过（这一行不改锁）')
                    next_row += 1
                    continue
                for column in range(4):
                    index = next_row * 4 + column + 1
                    if index > capacity:
                        break
                    self.device.screenshot()
                    if not self._in_cattery() and not self._dismiss_play_popup():
                        logger.error('[指挥喵-整理] 已不在猫窝列表上，停止')
                        return dict(stats)
                    cat = self._select_card(self._card_button(column, center_y), ocr, previous=previous)
                    previous = cat or previous
                    level, locked = self._read_card_state()
                    if level is None or locked is None:
                        stats['unreadable'] += 1
                        continue
                    if level > 1:
                        if seen_level1:
                            logger.info(f'[指挥喵-整理] 遇到 Lv{level} 的 {cat}，1 级猫已经看完')
                            return self._finish(stats, records)
                        stats['leveled'] += 1
                        continue
                    seen_level1 = True
                    if locked:
                        stock[cat] += 1
                        stats['already_locked'] += 1
                        continue
                    if not self._open_talent():
                        stats['unreadable'] += 1
                        continue
                    talents = self._read_talents(ocr)
                    if not self._back_to_cattery():
                        logger.error('[指挥喵-整理] 回不到猫窝列表，停止')
                        return self._finish(stats, records)
                    verdict = assess(CatRecord(name=cat, talents=talents, level=level, locked=False, index=index))
                    action = verdict.action
                    # 同名库存不上锁（用户口径：喂猫时记得每个品种留 4 只即可），这里只锁胚子
                    records.append(verdict)
                    if action == KEEP_EMBRYO:
                        if self._set_lock(True):
                            stock[verdict.cat] += 1
                            stats['locked_' + action] += 1
                            logger.info(f'[指挥喵-整理] #{index} {cat} 上锁：{verdict.reason}')
                        else:
                            stats['lock_failed'] += 1
                    else:
                        stats[action] += 1
                        logger.info(f'[指挥喵-整理] #{index} {cat} 不上锁：{verdict.reason}')
                next_row += 1
                progressed = True
            if next_row >= rows:
                break
            moved = self._scroll_once(scroll_range)
            content_y += moved
            if moved < 10 and not progressed:
                stalls += 1
                if stalls >= 2:
                    logger.info('[指挥喵-整理] 列表滚不动了，结束')
                    break
            else:
                stalls = 0
        return self._finish(stats, records)

    def _finish(self, stats, records) -> dict:
        self._save(records)
        logger.attr('[指挥喵-整理] 结果', dict(stats))
        return dict(stats)


def run_meowfficer_sweep(config, device=None):
    return MeowfficerSweep(config, device=device).sweep()
