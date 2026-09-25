---
description: AzurPilot & ALAS 实机运行高频故障复盘、根因分析与代码修复路线图
alwaysApply: true
---

# 实机运行故障分析与修复路线图 (Runtime Fault Analysis & Fixes)

**文档版本**: 1.0.0  
**更新日期**: 2026-09-25  
**数据来源**: `D:\模拟器\AzurPilot\log` (2026-09 实测) + `D:\模拟器\AzurLaneAutoScript\log` (1,189 次异常转储历史复盘)

---

## 一、 故障全景与频次统计

通过对本地运行日志及错误快照（截图 + 堆栈）的自动化聚合挖掘，真实发生的运行时异常高度集中在以下 5 大类：

| 异常类型 | 统计次数 | 典型触发场景 | 严重级别 |
|---|---|---|---|
| **GameStuckError** | **560+ 次** | 加载过长、过场动画、战斗结算白屏、弱网重连弹窗未捕获 | 🔴 严重（导致重启循环） |
| **GameTooManyClickError** | **511+ 次** | 大世界选舰队 (`FLEET_CHOOSE`)、登录弹窗 (`LOGIN_CHECK`)、领奖结算 | 🔴 严重（触发保护性退出） |
| **NemuIpcError / WinError 10053** | **1,400+ 行** | MuMu 模拟器未就绪、句柄失效、ADB 管道被系统安全软件重置 | 🔴 严重（连接层阻断） |
| **MapDetectionError** | **53 次** | 海域边缘、新型活动图网格透视变换匹配失败，画面偏离地图 | 🟡 中等（战役中断） |
| **MinitouchNotInstalledError** | **15 次** | minitouch socket 管道崩溃或返回空数据 | 🟡 中等（触控降级） |

---

## 二、 核心故障详细复盘与修复方案

### 1. NemuIpc 模拟器通信断连与 `WinError 10053`

#### 1.1 错误日志特征
```text
ERROR | [WinError 10053] 你的主机中的软件中止了一个已建立的连接。
ERROR | 连接失败，请检查 nemu_folder 是否正确以及模拟器是否正在运行
ERROR | [设备-NemuIpc] 模拟器信息不正确
CRITICAL | [设备-NemuIpc] 重试 connect_with_retry() 失败
ERROR | nemu_capture_display failed during get_resolution()
```

#### 1.2 根因定位
- **文件位置**: `module/device/method/nemu_ipc.py` (`connect_with_retry` L294, `get_resolution` L358)
- **触发机制**:
  1. AzurPilot 默认尝试启用 MuMu 共享内存 IPC 接口获取超低延迟截图。当模拟器尚未完全就绪、渲染进程重新初始化、或多开实例对应端口/路径变动时，`nemu_connect` 返回空句柄。
  2. 即使初次连接成功，在游戏分辨率切换、窗口最小化或后台休眠时，`nemu_capture_display` 调用失败抛出 `NemuIpcError`，但上层缺乏自动降级机制，直接导致调度器崩溃。
  3. ADB socket 管道由于 Windows Defender / 防火墙扫描瞬时重置抛出 `WinError 10053`。

#### 1.3 修复方案与代码设计
- **自动降级回退机制 (Graceful Fallback)**:
  当 `nemu_ipc` 重试 3 次仍然失败时，不抛出不可逆异常，而是动态降级至 `aScreenCap` 或 `ADB_nc`，保证调度任务不中断。
- **重试容错保护**:
  在 `get_resolution` 中增加重试延迟，避免在窗口创建瞬间读取导致断连。
```python
# 建议修改 module/device/method/nemu_ipc.py 中的截图调用
def screenshot_with_fallback(self):
    try:
        return self.screenshot_ipc()
    except (NemuIpcError, OSError) as e:
        logger.warning(f'[NemuIpc] IPC 截图失败 ({e})，临时降级至 ADB 截图')
        return self.screenshot_adb()
```

---

### 2. `GameTooManyClickError`: 大世界舰队选择 (`FLEET_CHOOSE`) 连点风暴

#### 2.1 错误日志特征
```text
GameTooManyClickError: [设备-点击] 按钮点击次数过多: FLEET_CHOOSE
```

#### 2.2 根因定位
- **文件位置**: `module/os/map_fleet_selector.py` (`open` L117-L135, `bar_opened` L60-L68)
- **触发机制**:
  ```python
  def open(self):
      click_timer = Timer(3, count=6)
      for _ in main.loop():
          if self.bar_opened():
              break
          if click_timer.reached():
              main.device.click(self._choose)
              click_timer.reset()
  ```
  `self.bar_opened()` 依赖硬编码的像素颜色统计：
  `image_color_count(area, color=(239, 243, 247), threshold=221, count=400)`
  当游戏更新了 UI 阴影、滤镜、或者夜间模式色彩微调时，此颜色判定条件永久为 `False`。因此循环中会不断点击 `self._choose` (`FLEET_CHOOSE`)，直到 `device.click_record` 累计达到 12 次抛出崩溃。

#### 2.3 修复方案与代码设计
1. **增加退出与后退重试上限**:
   在 `open()` 循环中增加最大尝试次数（如最多点击 5 次）。若仍未检测到展开，主动点击空白处取消并重新进入大世界地图。
2. **多特征容错判定**:
   结合模板匹配或边缘检测替代纯单一色值统计。
```python
# 建议修改 module/os/map_fleet_selector.py
def open(self):
    click_timer = Timer(3, count=6)
    attempt_count = 0
    for _ in main.loop():
        if main.handle_map_event():
            click_timer.reset()
            continue
        if self.bar_opened():
            break
        if click_timer.reached():
            attempt_count += 1
            if attempt_count > 5:
                logger.warning('[大世界-舰队选择] 下拉栏展开检测连续失败，尝试点击安全区刷新')
                main.device.click(CLICK_SAFE_AREA)
                main.ensure_edge_insight()
                attempt_count = 0
            main.device.click(self._choose)
            click_timer.reset()
```

---

### 3. `GameStuckError`: 等待超时与静态画面假死

#### 3.1 错误日志特征
```text
GameStuckError: [设备-卡死] 等待时间过长
GameStuckError: [设备-卡死] 截图未变化
```

#### 3.2 根因定位
- **文件位置**: `module/device/device.py` (`_check_image_stuck` L405, `stuck_record_check` L430)
- **触发机制**:
  - `_stuck_image_timer` 默认 30~60 秒，通过 16x16 缩略图指纹对比判断截图未变化。
  - 在大世界地图漫游、远距离自律寻敌或活动副本结算动画较长时，画面变化细微，极易误触 `截图未变化`。
  - 遇到断线重连对话框或游戏热更弹窗时，由于未在 `stuck_long_wait_list` 中登记，直接判为卡死并触发模拟器整机重启。

#### 3.3 修复方案与代码设计
1. **唤醒兜底（Click-to-Wake）**:
   在抛出 `GameStuckError` 之前，执行一次“轻量唤醒动作”（点击屏幕边缘安全区 `CLICK_SAFE_AREA` 或发送 Back 返回键），尝试消除阻塞性对话框。
2. **长等待白名单动态扩充**:
   将剧情动画（`STORY_CHECK`）、大型活动结算、加载界面（`LOADING_CHECK`）加入动态免死计时器。

---

### 4. 登录认证与领奖连点风暴 (`LOGIN_CHECK` / `REWARD_1_WHITE`)

#### 4.1 错误日志特征
```text
GameTooManyClickError: Too many click for a button: LOGIN_CHECK (102 次)
GameTooManyClickError: Too many click for a button: REWARD_1_WHITE (49 次)
GameTooManyClickError: Too many click for a button: POPUP_CONFIRM_LOGIN (48 次)
```

#### 4.2 根因定位
- **文件位置**: `module/handler/login.py` 与 `module/reward/reward.py`
- **触发机制**:
  - 港区网络拥堵或服务器排队时，登录界面处于不可交互状态，但 `LOGIN_CHECK` 依然处于 `appear` 状态，Alas 以 2 秒间隔高频重试，超过 15 次滑动窗口上限。
  - 领奖界面的“白色高亮框”在动画未播放完毕前重复判定为可领奖。

#### 4.3 修复方案与代码设计
- **指数退避重试 (Exponential Backoff)**:
  对于登录、网络连接类按钮，`interval` 随重试次数动态延长（2s → 4s → 8s），防止快速耗尽点击配额。
- **状态转移硬核校验**:
  点击后强制检查是否有后续动作变化（如加载转圈或弹出新提示），无变化时主动清除点击计数并触发微休眠。

---

### 5. `MapDetectionError`: 地图网格透视变换丢失

#### 5.1 错误日志特征
```text
MapDetectionError: Image to detect is not in_map
```

#### 5.2 根因定位
- **文件位置**: `module/map_detection/homography.py` / `module/map/map_camera.py`
- **触发机制**:
  - 舰队移动到地图极角边缘时，画面中陆地与海域网格特征不足，单应性矩阵（Homography Matrix）拟合残差过大，直接抛出 `MapDetectionError`。

#### 5.3 修复方案与代码设计
- 捕获 `MapDetectionError` 后，执行相机居中校准（Camera Reset）：向屏幕中心点双向轻微滑动或点击小地图边缘，恢复全局视野后再重新计算网格。

---

## 三、 推进实施清单

- [ ] **P0 - NemuIpc 稳定性提升**: 为 `module/device/method/nemu_ipc.py` 增加异常捕获与 ADB 截图无缝回退。
- [ ] **P0 - FleetSelector 连点防护**: 优化 `module/os/map_fleet_selector.py`，限制最大点击次数并增加安全区刷新。
- [ ] **P1 - 唤醒机制 (Click-to-Wake)**: 在 `device.py` 触发 `GameStuckError` 前执行自救点击，减少无效重启。
- [ ] **P1 - 登录与领奖退避机制**: 优化 `module/handler/login.py` 中的重连间隔计算。
- [ ] **P2 - 地图透视迷航自愈**: 在海域地图检测中引入归中校准。

