# AzurPilot 本地部署与源码归属

本文档固定个人源码、云端上游、launcher 构建树和生产目录的边界，避免把运行产物误认为源码，或把不同版本的 UI 混在一起。

## 目录职责

| 路径 | 角色 | 规则 |
|---|---|---|
| `D:\Andrew\Code\AzurLane\AzurPilot` | 个人 AzurPilot 业务源码 | 个人业务修复、WebUI 修改和部署脚本的源码真相 |
| `D:\Andrew\Code\AzurLane\alas-launcher` | launcher 主工作树 | 个人 launcher 的长期开发区，可能有未提交的实验修改 |
| `D:\模拟器\alas-launcher-repair` | launcher 正式构建树 | 当前个人 branding 分支的构建来源，构建后复制 exe 到生产目录 |
| `D:\模拟器\AzurPilot` | 生产运行目录 | 只放运行时、配置、日志和构建产物，不在这里长期开发 |
| `wess09/AzurPilot` | 云端业务上游 | 提供主软件 upstream 基线和 PR 目标，不包含个人 UI 和 launcher 修改 |
| `andrew05060414/AzurPilot:master` | 生产业务源 | 基于 upstream 的 Fork 生产分支，保留必要功能补丁；经典主题不进入此分支 |
| `andrew05060414/AzurPilot:style/alas-ui` | 私人 UI 分支 | 保留经典主题和个人视觉实验，不作为生产自动更新源 |

## 当前生产链

当前生产目录使用个人 fork 的 `master` 分支，由 launcher 启动时自动更新。该分支从 `wess09/AzurPilot:master` 同步，并只保留必要的主软件功能补丁；经典主题留在 `style/alas-ui` 分支。启动更新会执行 Git 重置，因此生产目录中的未提交业务修改可能被覆盖。

launcher 本身不会从云端自动重新编译。修改 launcher 后必须在 `D:\模拟器\alas-launcher-repair` 执行：

```powershell
cargo test --locked
cargo build --release --locked
```

然后把 `target\release\alas-launcher.exe` 复制到 `D:\模拟器\AzurPilot\alas-launcher.exe`。替换前先退出 launcher，并保留一个带日期的回滚副本。

## UI 约束

正式 launcher 使用 ALAS Windows 风格的自定义标题栏：

- 不使用 Windows 原生标题栏。
- 不使用 Apple 风格的彩色圆点。
- 使用矩形按钮、灰色最小化/最大化图标和红色关闭悬停状态。

标题栏实现位于 launcher 的 `src/main.rs`，不是 AzurPilot 的 `webapp` 目录。

## WebView 生命周期约束

托盘最小化只能隐藏主窗口，不能销毁 WebView：

```rust
window.hide()
```

`window.destroy()` 会让 PyWebIO 页面 session 断开，恢复时重新创建 WebView，导致页面重载、白屏和 `SessionNotFoundException`。

## `webapp` 边界

根目录下以下静态文件仍由 Python WebUI 读取，必须保留：

```text
webapp/ap_chart.js
webapp/ap_chart_panel.html
webapp/copyable_device_id.html
webapp/muted_notice.html
webapp/recommendation_box.html
webapp/resource_chart.html
webapp/resource_chart.js
webapp/simple_table.html
webapp/title_block.html
```

旧 Electron 的 `packages`、构建脚本、`node_modules` 和 `app.asar` 不属于当前 Tauri launcher 运行链。清理它们前必须确认没有重新启用 Electron 客户端。

## 个人修复归档

个人修复必须提交到对应仓库，不能只留在生产目录：

| 修改 | 仓库 |
|---|---|
| WebUI、worker、配置和同步逻辑 | 个人 AzurPilot fork |
| launcher 标题栏、托盘、窗口生命周期 | 个人 launcher fork |
| `D:\模拟器\AzurPilot\config\deploy.yaml` 的本机运行偏好 | 仅生产目录，不提交公共默认配置 |

每次部署前检查两个仓库的状态、当前分支、HEAD 和正式产物 SHA256。
