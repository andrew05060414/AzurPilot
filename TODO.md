# AzurPilot Personal Fork TODO

## Current Delivery

- [ ] 在独立部署副本中运行 personal `master` 的 portable EXE，验收启动、托盘、四个窗口按钮和 classic 主题。
- [x] 验收通过后确认运行目录的 `deploy.yaml` 跟踪个人 fork 的 `master`。

## Maintenance

- [x] 每 3 小时检查并合并 `wess09/AzurPilot:master`。
- [x] 合并时保留个人 Electron/UI 文件；其他 AzurPilot 代码继续跟随上游。
- [x] 只检查 `LmeSzinc/AzurLaneAutoScript:master` 是否有未被 AzurPilot 表示的提交，不自动合并第二个上游。
- [ ] 如果 ALAS 有重要活动更新而 AzurPilot 尚未同步，手动审查后再合并。
- [x] 根据实际截图核对并恢复 ALAS 头像；保留 AzurPilot 当前页面所需的 fork 专属资源。
- [ ] 单独处理 UV 启动失败的 issue/PR，不与 UI 资源恢复混在一起。
