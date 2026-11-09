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
