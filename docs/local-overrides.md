# 本地 fork 与直接上游的边界

创建日期：2026-10-23 ｜ 最后更新：2026-10-23 ｜ 版本：1.0

本仓库以 `wess09/AzurPilot` 为直接上游；原始 ALAS 只作为间接来源。本地维护目标是上游代码加已明确需要的个人补丁，不维护平行的同功能实现。本轮核对基线为上游 `5eb8ecab77b1fc1b8a9ad826dec59f6de7e03f35`，后续同步以实际远端 HEAD 为准。

## 保留的本地差异

| 类别 | 内容与来源 | 主要入口 |
|---|---|---|
| 个人调度 | 物资冲刺、石油偏高自动消耗、大世界总开关；属于个人功能，不声称上游同义支持 | `module/config/coin_rush.py`、配置定义与生成产物、`module/dorm/dorm.py` |
| 页面识别 | 活动横幅避让、红脸弹窗正文 OCR、大世界 O.A.G.S 提示模板与拦截 | `module/campaign/campaign_event.py`、`module/handler/info_handler.py`、`module/os_handler/` |
| 运行保护 | 同配置跨进程排他锁；启停失败保留恢复层级；委托滚动无进展有界恢复；Boss 战败回海图立即交回恢复 | `alas.py`、`module/runtime/scheduler_lock.py`、设备/委托/战斗模块 |
| 界面恢复 | 创建路由前恢复上次页面，避免托盘重建先显示首页；保持 WebView 销毁设置 | `frontend/src/main.tsx`、`frontend/src/app/App.tsx` 与 `topbarPrefs.ts` |
| 开发证据 | 上游只读同步检查、AI 只读巡检原型、测试/截图、实机故障记录 | `dev_tools/`、`tests/`、`.agent/`、受影响模块文档 |

## 已消解的差异

- 猫强化的第二段相同循环来自上游 `d0b32e478`（2026-09-28，注释重写）；上游在 `fedac81ee` / #1216 中已修复，并随 #1291 进入 master。旧运行版只同步到 `c50cd8bac` / #1200，因此仍携带旧代码。本地 `74fdbee83` 的去重与上游重叠，本轮采用上游完整选猫/等级校验实现，`module/meowfficer/enhance.py` 已无个人差异；保留不消耗游戏资源的回归测试。
- 小游戏旧客户端故障临时跳过补丁 `cfe929f4b` 已移除，上游后续 `c9e1f27b3` 合入 ALAS #6051 的列表识别与滚动修复；`module/minigame/` 采用上游实现。尚未执行真实小游戏，清理不等于实机验收。
- 同异常延后、旧内联战败弹窗点击与旧树状菜单补丁，在此前合并中已被上游流程吸收/替代，不重复移植。`module/combat/auto_search_combat.py` 现在也与上游一致，其中上游现有 `_restart_resume_guard` 等状态不得仅凭名字当作本地残留删除。

## 核对方式与边界

同步先固定直接上游提交，保留旧本地提交，用隔离 worktree 合并，再比较 `git diff upstream/master HEAD`；`git log upstream/master..HEAD` 会显示历史补丁和 merge，不能用提交数量代表仍有多少代码差异。不要重写已推送历史或为压缩 diff 删除仍需的个人功能。

修改配置定义或资源后运行项目生成器，保留生成结果与源文件一致。合并后优先验证个人补丁与上游新接口交界；自动检查不替代真实托盘恢复、真实账号任务与长期挂机验收。开发源目录与实际运行目录分离，合入开发分支不会自动替换运行版。
