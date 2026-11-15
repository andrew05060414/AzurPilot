# 指挥喵（Meowfficer）分拣与整理

> 把指挥喵按用户本人的口径分成「胚子 / 同名库存 / 在用 / 饲料」，并只对 1 级、未锁的猫执行上锁。

## 1. 模块概述

指挥喵的天赋有好有坏，猫窝容量有限。本模块回答「这只猫留不留、留来干什么」，并把结论落到游戏里的「锁定」状态上。目前只做到**识别 + 上锁**，喂猫、洗猫、开箱、批量加速都还没有实现，也**不会被调度器自动调用**（配置里没有对应任务）。

## 2. 入口

| 入口 | 位置 | 说明 |
| --- | --- | --- |
| 分拣规则 | `module/meowfficer/triage.py` | 纯逻辑，无设备依赖，可单测 |
| 页面操作 | `module/meowfficer/triage_task.py` | `MeowfficerSweep` 翻页扫描并上锁；`run_meowfficer_triage` 为手工入口 |
| 扫描与识别 | `module/meowfficer/scan.py`、`scan_utils.py` | 读天赋、等级、锁状态；`MeowfficerScore` 任务也使用 |

没有配置项，也没有调度任务：只能由手工脚本调用。

## 3. 分拣口径（用户 2026-11-09 定）

- 有用的彩天赋：侵略如火（最好）、不动如山，以及本舰种精英天赋（航母「王牌机师」、战列「一发入魂」、潜艇「狼群之首」）。
- 普通天赋讲究「清一色」，全是本舰种相关才算纯。紫猫样本多，标准比金猫严。
- 每个品种留 4 只同名猫当技能升级料（升 1 级喂 1 只、升 2 级喂 3 只）。
- 只处理 1 级猫；满级和用户自己锁的猫不动；不反复全量重扫。

## 4. 安全边界

- `MeowfficerSweep.sweep()` 只**上锁**，从不解锁、不喂、不洗。
- 旧的 `plan/apply` 路径还在，`apply` 内含解锁，默认 `apply=False`，不应再使用。
- 喂猫不可逆，后续实现前必须先出清单让用户过目。

## 5. 依赖

依赖 `module/meowfficer/score.py`（天赋评分）、`cat_data.py`（品种数据）、`assets.py`（锁 / 未锁图标）。被 `MeowfficerScore` 任务共用 `scan.py`。

## 6. 修改注意事项

- 锁 / 未锁图标互相很像（素材互比约 0.65）；非原生 720p 下阈值会被放宽，改动前先确认不会把两者互相认错。
- 测试：`tests/test_meowfficer_triage.py`、`tests/test_meowfficer_scan.py`、`tests/test_meowfficer_score.py`。
- 目标与待办见仓库外的 `meowfficer-autopilot-TASK.md`。
