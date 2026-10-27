"""ALAS 只读维护巡检、真实状态（WebUI/API/SSH/ADB）接线与可行动告警生成工具。

提供两类模式：
1. 回放模式（离线）：回放 JSON 状态样本或日志尾部，评估故障 episode 与脱敏指纹。
2. 实况模式（只读）：只读探测 WebUI 健康、ADB 模拟器在线状态、SSH 远程隧道联通性，
   并结合日志尾部提取心跳与最近任务成功/失败信号；在检测到卡死或离线时产生可行动告警。

安全边界：
- 全程只读：不点击游戏、不改写配置、不重启模拟器、不发起非幂等控制。
- 有限自动恢复：默认采用干运行（dry-run），自动恢复开关严格默认关闭（auto_recovery_enabled=False），
  且具备重试上限与冷却时间保护。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LOG_PATTERN = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)"
    r" \| (?P<level>INFO|WARNING|ERROR|CRITICAL) \| (?P<message>.*)$"
)
TASK_START_PATTERN = re.compile(r"<<<\s*(?P<task>[^\s>]+)\s*>>>")
TASK_SCHEDULER_START = re.compile(r"调度器:\s*开始任务\s*[`'‘“](?P<task>[^`'’”]+)[`'’”]")
TASK_SUCCESS_PATTERNS = [
    re.compile(r"\[(?P<module>[^\]]+)\]\s*(?P<task>\S+)?\s*登录成功"),
    re.compile(r"调度器:\s*任务\s*[`'‘“](?P<task>[^`'’”]+)[`'’”]\s*成功"),
    re.compile(r"调度器:\s*结束任务\s*[`'‘“](?P<task>[^`'’”]+)[`'’”]\s*\(success\)", re.IGNORECASE),
    re.compile(r"ordinary_task_success\s*=\s*true", re.IGNORECASE),
]
TASK_ERROR_PATTERNS = [
    re.compile(r"\[错误\]\s*(?P<msg>.*)"),
    re.compile(r"GameStuckError"),
]


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _fingerprint(instance: str, status: str, error: str | None, current_task: str | None) -> str:
    reason = re.sub(r"\b\d+\b", "#", (error or status).strip().lower())
    raw = "|".join((instance, status, current_task or "", reason))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


SENSITIVE_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # 凭据、私钥、Token、密码等敏感键值或独立复合标识（如 demo-token-DO-NOT-SHARE）
    (
        re.compile(
            r"""(?i)\b[a-z0-9._%+-]*?(?:token|secret|password|passwd|api_key|apikey|credential|session_id|bearer|auth|private)[a-z0-9._%+-]*\b"""
            r"""(?:[=:\s]+[^\s,;'"<>\[\]{}()]+)?"""
        ),
        "[REDACTED]",
    ),
    # 标准 Bearer 格式
    (
        re.compile(r"""(?i)\b(bearer)\s+[^\s,;'"<>\[\]{}()]+"""),
        r"\1 [REDACTED]",
    ),
    # 包含账号密码的 URL (http://user:pass@host)
    (
        re.compile(r"""(?i)(https?://)[^\s:@]+:[^\s:@]+@"""),
        r"\1***:***@",
    ),
    # Query String 凭据 (?token=xxx, &key=xxx)
    (
        re.compile(r"""(?i)([?&](?:key|token|auth|secret|password|api_key)=)[^&\s"']+"""),
        r"\1***",
    ),
    # 本地包含用户名的用户目录路径脱敏
    (
        re.compile(r"""(?i)([a-z]:\\users\\)[^\\]+(\\)""", re.IGNORECASE),
        r"\1<user>\2",
    ),
    (
        re.compile(r"""(?i)(/home/)[^/]+(/)"""),
        r"\1<user>\2",
    ),
)


def sanitize_error(text: str | None) -> str | None:
    """对原始日志中的错误信息做深度脱敏与清洗，严防凭据或原始私密日志回显。"""
    if not text:
        return text
    clean = str(text).strip()
    for pattern, replacement in SENSITIVE_PATTERNS:
        clean = pattern.sub(replacement, clean)
    clean = re.sub(r"(\[REDACTED\]\s*)+", "[REDACTED] ", clean).strip()
    if len(clean) > 200:
        clean = clean[:197] + "..."
    return clean


def evaluate(samples: list[dict[str, Any]], threshold_seconds: int = 300) -> list[dict[str, Any]]:
    """按实例各自的时间线评估故障 episode；不执行恢复操作。"""
    if threshold_seconds <= 0:
        raise ValueError("threshold_seconds must be positive")
    events: list[dict[str, Any]] = []
    previous_time: dict[str, datetime] = {}
    baseline_success: dict[str, str] = {}
    episodes: dict[str, dict[str, Any]] = {}

    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            events.append({"index": index, "state": "unknown", "reason": "sample_not_object"})
            continue
        raw_instance = sample.get("instance")
        if not isinstance(raw_instance, str) or not raw_instance.strip():
            events.append({"index": index, "state": "unknown", "reason": "missing_instance"})
            continue
        instance = raw_instance.strip()
        timestamp = parse_time(sample.get("timestamp"))
        if timestamp is None or (instance in previous_time and timestamp < previous_time[instance]):
            events.append({"index": index, "instance": instance, "state": "unknown",
                           "reason": "invalid_or_out_of_order_timestamp"})
            continue
        previous_time[instance] = timestamp

        status = _text(sample.get("status"))
        task = _text(sample.get("current_task"))
        error_value = sample.get("error")
        error = _text(error_value) if error_value is None or isinstance(error_value, str) else None
        if not isinstance(sample.get("status"), str) or not isinstance(sample.get("current_task"), (str, type(None))) \
                or not isinstance(error_value, (str, type(None))):
            events.append({"index": index, "instance": instance, "state": "unknown",
                           "reason": "invalid_field_type"})
            continue

        if status in {"stopped", "updating"} and sample.get("desired_running") is False:
            episodes.pop(instance, None)
            baseline_success.pop(instance, None)
            events.append({"index": index, "instance": instance, "state": "cancelled",
                           "reason": "explicit_not_desired_running"})
            continue

        explicit_error = status == "error" or error is not None or \
            (status in {"stopped", "updating"} and sample.get("desired_running") is True)
        success_id = sample.get("success_id")
        success_task = _text(sample.get("success_task"))
        success_event = sample.get("ordinary_task_success") is True and isinstance(success_id, str) \
            and bool(success_id.strip()) and success_task is not None and success_task.casefold() != "restart" \
            and status == "running" and not explicit_error
        new_success = success_event and baseline_success.get(instance) is not None \
            and baseline_success[instance] != success_id.strip()
        if success_event:
            baseline_success[instance] = success_id.strip()

        episode = episodes.get(instance)
        if episode and new_success:
            events.append({"index": index, "instance": instance, "state": "recovered",
                           "fingerprint": episode["fingerprint"], "evidence": "new_ordinary_task_success"})
            episodes.pop(instance, None)
            continue
        if new_success:
            events.append({"index": index, "instance": instance, "state": "healthy",
                           "evidence": "new_ordinary_task_success"})
            continue

        if status in {"stopped", "updating"} and sample.get("desired_running") is not True:
            events.append({"index": index, "instance": instance, "state": "normal", "reason": "not_desired_running"})
            continue

        expected = sample.get("expected_progress") is True
        if not explicit_error and not expected and status in {"running", "stopped", "updating"}:
            state = "healthy" if new_success else "unknown"
            reason = None if new_success else "no_progress_expectation_or_success_evidence"
            item = {"index": index, "instance": instance, "state": state}
            if reason:
                item["reason"] = reason
            events.append(item)
            continue
        if not explicit_error and not expected:
            events.append({"index": index, "instance": instance, "state": "unknown",
                           "reason": "unrecognized_status"})
            continue

        fault_status = "error" if explicit_error else "expected_progress_stalled"
        key = _fingerprint(instance, fault_status, error, task)
        if episode is None:
            episode = {"fingerprint": key, "first_seen": timestamp, "reported": False}
            episodes[instance] = episode
        elapsed = (timestamp - episode["first_seen"]).total_seconds()
        stalled = elapsed >= threshold_seconds
        should_report = stalled and not episode["reported"]
        if should_report:
            episode["reported"] = True
        events.append({"index": index, "instance": instance,
                       "state": "stalled" if stalled else "retry", "fingerprint": episode["fingerprint"],
                       "duration_seconds": int(elapsed), "report": should_report})
    return events


def read_log_tail(path: Path, instance: str, lines: int = 500, timezone_offset: str = "+00:00") -> list[dict[str, Any]]:
    """只读指定日志尾部，提取格式明确的 ERROR/CRITICAL 行，不回显原文。"""
    if lines < 1:
        raise ValueError("lines must be positive")
    offset_match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", timezone_offset)
    if not offset_match:
        raise ValueError("timezone-offset must use ±HH:MM")
    hours, minutes = int(offset_match[2]), int(offset_match[3])
    if hours > 23 or minutes > 59:
        raise ValueError("timezone-offset is out of range")
    delta = timedelta(hours=hours, minutes=minutes)
    if offset_match[1] == "-":
        delta = -delta
    tz = timezone(delta)
    with path.open(encoding="utf-8", errors="replace") as stream:
        tail = deque(stream, maxlen=lines)
    samples = []
    for line in tail:
        match = LOG_PATTERN.match(line.rstrip("\r\n"))
        if not match or match["level"] not in {"ERROR", "CRITICAL"}:
            continue
        try:
            try:
                parsed_time = datetime.strptime(match["time"], "%Y-%m-%d %H:%M:%S.%f")
            except ValueError:
                parsed_time = datetime.strptime(match["time"], "%Y-%m-%d %H:%M:%S")
            timestamp = parsed_time.replace(tzinfo=tz)
        except ValueError:
            continue
        samples.append({"timestamp": timestamp.isoformat(), "instance": instance,
                        "status": "error", "error": match["message"]})
    return samples


# ============================================================================
# 只读实况探测（WebUI / API / SSH / ADB / 心跳与任务状态）
# ============================================================================

def probe_webui(webui_url: str = "http://127.0.0.1:25548", timeout: float = 2.0) -> dict[str, Any]:
    """只读检测 WebUI /healthz 健康探针。"""
    url = webui_url.rstrip("/") + "/healthz"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ALAS-Maintenance-Probe/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status_code = response.getcode()
            return {
                "available": status_code == 200,
                "url": url,
                "status_code": status_code,
                "detail": "ok" if status_code == 200 else f"unexpected_status_{status_code}",
            }
    except Exception as exc:
        return {
            "available": False,
            "url": url,
            "status_code": None,
            "detail": f"connection_failed: {type(exc).__name__}",
        }


def probe_adb_emulator(
    adb_executable: Path | str | None = None,
    serial: str = "auto",
    timeout: float = 3.0,
) -> dict[str, Any]:
    """只读检测模拟器 ADB 在线状态与设备列表。"""
    adb_cmd: str | None = None
    if adb_executable and Path(adb_executable).exists():
        adb_cmd = str(adb_executable)
    else:
        candidates = [
            Path("D:/模拟器/AzurPilot/.venv/Scripts/adb.exe"),
            Path("./.venv/Scripts/adb.exe"),
        ]
        for cand in candidates:
            if cand.exists():
                adb_cmd = str(cand)
                break
        if not adb_cmd:
            adb_cmd = "adb"

    try:
        proc = subprocess.run(
            [adb_cmd, "devices", "-l"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        return {
            "online": False,
            "adb_available": False,
            "serial": serial,
            "device_state": "adb_not_found_or_timeout",
            "devices_found": [],
            "detail": f"adb_error: {type(exc).__name__}",
        }

    devices: list[dict[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] in {"device", "offline", "unauthorized", "recovery"}:
            devices.append({"serial": parts[0], "state": parts[1], "raw": line.strip()})

    if serial == "auto":
        online_dev = next((d for d in devices if d["state"] == "device"), None)
        if online_dev:
            return {
                "online": True,
                "adb_available": True,
                "serial": online_dev["serial"],
                "device_state": "device",
                "devices_found": devices,
                "detail": f"device_online ({online_dev['serial']})",
            }
        if devices:
            return {
                "online": False,
                "adb_available": True,
                "serial": devices[0]["serial"],
                "device_state": devices[0]["state"],
                "devices_found": devices,
                "detail": f"device_not_ready ({devices[0]['state']})",
            }
        return {
            "online": False,
            "adb_available": True,
            "serial": "auto",
            "device_state": "no_devices",
            "devices_found": [],
            "detail": "no_devices_attached",
        }

    matched = next((d for d in devices if d["serial"] == serial), None)
    if matched:
        return {
            "online": matched["state"] == "device",
            "adb_available": True,
            "serial": serial,
            "device_state": matched["state"],
            "devices_found": devices,
            "detail": f"matched_device_{matched['state']}",
        }
    return {
        "online": False,
        "adb_available": True,
        "serial": serial,
        "device_state": "not_found",
        "devices_found": devices,
        "detail": f"serial_{serial}_not_found_in_devices",
    }


def probe_ssh_tunnel(deploy_config_path: Path | None = None, timeout: float = 3.0) -> dict[str, Any]:
    """只读核查 RemoteAccess SSH 配置与通道连通性（轻量 TCP 连接探测，不执行命令）。"""
    path = deploy_config_path or Path("config/deploy.yaml")
    if not path.exists():
        cand = Path("D:/模拟器/AzurPilot/config/deploy.yaml")
        if cand.exists():
            path = cand

    if not path.exists():
        return {"enabled": False, "server": None, "user": None, "reachable": None, "detail": "deploy_yaml_missing"}

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"enabled": False, "server": None, "user": None, "reachable": None, "detail": f"read_error: {exc}"}

    enable_match = re.search(r"^\s*EnableRemoteAccess:\s*(true|false)", content, re.MULTILINE | re.IGNORECASE)
    server_match = re.search(r"^\s*SSHServer:\s*(?P<srv>[^\r\n#]+)", content, re.MULTILINE)
    user_match = re.search(r"^\s*SSHUser:\s*(?P<usr>[^\r\n#]+)", content, re.MULTILINE)

    enabled = bool(enable_match and enable_match.group(1).lower() == "true")
    server = server_match.group("srv").strip().strip("'\"") if server_match else None
    user = user_match.group("usr").strip().strip("'\"") if user_match else None

    if not enabled or not server or server.lower() in {"null", "none", ""}:
        return {"enabled": False, "server": server, "user": user, "reachable": None, "detail": "remote_access_disabled"}

    host, port_str = server.split(":", 1) if ":" in server else (server, "22")
    try:
        port = int(port_str)
        with socket.create_connection((host, port), timeout=timeout):
            reachable = True
            detail = "ssh_port_reachable"
    except Exception as exc:
        reachable = False
        detail = f"ssh_port_unreachable: {type(exc).__name__}"

    return {"enabled": True, "server": server, "user": user, "reachable": reachable, "detail": detail}


def probe_heartbeat_and_tasks(
    log_path: Path | None,
    instance: str = "alas",
    lines: int = 200,
    timezone_offset: str = "+00:00",
) -> dict[str, Any]:
    """只读提取最近日志心跳、当前执行任务与最近成功/失败信号。"""
    if not log_path or not log_path.exists():
        return {
            "heartbeat_time": None,
            "elapsed_seconds": None,
            "alive": False,
            "current_task": None,
            "latest_task_status": "unknown",
            "latest_error": None,
            "latest_error_time": None,
            "detail": "log_not_found",
        }

    offset_match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", timezone_offset)
    delta = timedelta(hours=int(offset_match[2]), minutes=int(offset_match[3])) if offset_match else timedelta()
    if offset_match and offset_match[1] == "-":
        delta = -delta
    tz = timezone(delta)

    with log_path.open(encoding="utf-8", errors="replace") as stream:
        tail = list(deque(stream, maxlen=lines))

    last_timestamp: datetime | None = None
    current_task: str | None = None
    latest_task_status = "idle"
    latest_error: str | None = None
    latest_error_time: str | None = None

    for line in tail:
        line_clean = line.rstrip("\r\n")
        match = LOG_PATTERN.match(line_clean)
        if match:
            try:
                t_str = match["time"]
                try:
                    pt = datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S.%f")
                except ValueError:
                    pt = datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S")
                last_timestamp = pt.replace(tzinfo=tz)
            except ValueError:
                pass

        task_m = TASK_START_PATTERN.search(line_clean) or TASK_SCHEDULER_START.search(line_clean)
        if task_m:
            current_task = task_m.group("task").strip()
            latest_task_status = "running"

        for pat in TASK_SUCCESS_PATTERNS:
            if pat.search(line_clean):
                latest_task_status = "success"
                break

        for pat in TASK_ERROR_PATTERNS:
            err_m = pat.search(line_clean)
            if err_m:
                latest_task_status = "error"
                latest_error = sanitize_error(err_m.group(0).strip())
                if last_timestamp:
                    latest_error_time = last_timestamp.isoformat()
                break

        if match and match["level"] in {"ERROR", "CRITICAL"}:
            latest_task_status = "error"
            latest_error = sanitize_error(match["message"].strip())
            if last_timestamp:
                latest_error_time = last_timestamp.isoformat()

    elapsed: int | None = None
    alive = False
    if last_timestamp:
        now_tz = datetime.now(tz)
        elapsed = max(0, int((now_tz - last_timestamp).total_seconds()))
        alive = elapsed <= 300

    return {
        "heartbeat_time": last_timestamp.isoformat() if last_timestamp else None,
        "elapsed_seconds": elapsed,
        "alive": alive,
        "current_task": current_task,
        "latest_task_status": latest_task_status,
        "latest_error": latest_error,
        "latest_error_time": latest_error_time,
        "detail": "ok" if last_timestamp else "no_log_lines_parsed",
    }


def read_live_summary(
    alas_root: Path | str | None = None,
    instance: str = "alas",
    webui_url: str = "http://127.0.0.1:25548",
    threshold_seconds: int = 300,
    timezone_offset: str = "+00:00",
    check_ssh: bool = True,
    desired_running: bool | None = None,
) -> dict[str, Any]:
    """汇总 WebUI / ADB / SSH / 心跳状态为统一只读摘要。"""
    if alas_root:
        root = Path(alas_root)
    else:
        root = Path(".")
        if not (root / "config").exists() and Path("D:/模拟器/AzurPilot").exists():
            root = Path("D:/模拟器/AzurPilot")

    now_iso = datetime.now(timezone.utc).isoformat()
    webui = probe_webui(webui_url)
    adb = probe_adb_emulator(root / ".venv/Scripts/adb.exe")
    ssh = probe_ssh_tunnel(root / "config/deploy.yaml") if check_ssh else {"enabled": False, "reachable": None}

    log_file: Path | None = None
    log_dir = root / "log"
    if log_dir.exists():
        today_prefix = datetime.now().strftime("%Y-%m-%d")
        cand = log_dir / f"{today_prefix}_{instance}.txt"
        if cand.exists():
            log_file = cand
        else:
            logs = sorted(log_dir.glob(f"*{instance}*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
            if logs:
                log_file = logs[0]

    heartbeat = probe_heartbeat_and_tasks(log_file, instance=instance, timezone_offset=timezone_offset)

    stalled = False
    stalled_reasons: list[str] = []

    if desired_running is False:
        overall_state = "stopped"
        fingerprint = _fingerprint(
            instance,
            overall_state,
            "explicit_not_desired_running",
            heartbeat.get("current_task"),
        )
        return {
            "timestamp": now_iso,
            "instance": instance,
            "state": overall_state,
            "fingerprint": fingerprint,
            "stalled": False,
            "stalled_reasons": [],
            "webui": webui,
            "emulator": adb,
            "ssh": ssh,
            "heartbeat": heartbeat,
        }

    if not adb["online"]:
        stalled = True
        stalled_reasons.append("emulator_offline")
    if not webui["available"]:
        stalled = True
        stalled_reasons.append("webui_unavailable")
    if ssh.get("enabled") and ssh.get("reachable") is False:
        stalled = True
        stalled_reasons.append("ssh_unreachable")
    if heartbeat["latest_task_status"] == "error":
        stalled = True
        stalled_reasons.append("task_error")
    if heartbeat["elapsed_seconds"] is not None and heartbeat["elapsed_seconds"] > threshold_seconds:
        stalled = True
        stalled_reasons.append("heartbeat_expired")
    elif desired_running is True and heartbeat["elapsed_seconds"] is None:
        stalled = True
        stalled_reasons.append("heartbeat_absent")

    if stalled:
        if "webui_unavailable" in stalled_reasons and adb["online"] and heartbeat["latest_task_status"] != "error":
            overall_state = "degraded"
        elif "ssh_unreachable" in stalled_reasons and len(stalled_reasons) == 1:
            overall_state = "degraded"
        else:
            overall_state = "stalled"
    elif heartbeat["latest_task_status"] == "idle" and heartbeat["elapsed_seconds"] is None:
        overall_state = "idle"
    else:
        overall_state = "healthy"
    fingerprint = _fingerprint(
        instance,
        overall_state,
        heartbeat.get("latest_error") or ("|".join(stalled_reasons) if stalled_reasons else None),
        heartbeat.get("current_task"),
    )

    return {
        "timestamp": now_iso,
        "instance": instance,
        "state": overall_state,
        "fingerprint": fingerprint,
        "stalled": stalled,
        "stalled_reasons": stalled_reasons,
        "webui": webui,
        "emulator": adb,
        "ssh": ssh,
        "heartbeat": heartbeat,
    }


# ============================================================================
# 可行动告警与自动恢复设计（开关默认关闭）
# ============================================================================

def generate_actionable_alert(summary: dict[str, Any], threshold_seconds: int = 300) -> dict[str, Any] | None:
    """当检测到模拟器恢复失败、离线或任务卡死时，产生面向人类（Andrew）的可行动告警。"""
    if not summary.get("stalled"):
        return None

    instance = summary.get("instance", "alas")
    reasons = summary.get("stalled_reasons", [])
    hb = summary.get("heartbeat", {})
    emu = summary.get("emulator", {})
    webui = summary.get("webui", {})
    ssh = summary.get("ssh", {})
    fingerprint = summary.get("fingerprint", "unknown")

    detected_at = summary.get("timestamp", datetime.now(timezone.utc).isoformat())
    duration = hb.get("elapsed_seconds") or threshold_seconds

    phenomenon_items: list[str] = []
    recommended_actions: list[str] = []
    suggested_auto_action: str | None = None
    severity = "WARNING"

    if "emulator_offline" in reasons or not emu.get("online"):
        severity = "CRITICAL"
        dev_state = emu.get("device_state", "unknown")
        serial = emu.get("serial", "auto")
        phenomenon_items.append(f"模拟器连接中断/掉线：ADB 状态为 `{dev_state}`（目标序列号: `{serial}`），未能检测到在线设备。")
        recommended_actions.append("检查 MuMu 等模拟器窗口是否冻结或已崩溃退出；若已退出请手动重新启动模拟器。")
        recommended_actions.append(f"在终端运行 `adb connect {serial}` 或通过托盘重新连接模拟器。")
        suggested_auto_action = "restart_adb"

    if "webui_unavailable" in reasons or not webui.get("available"):
        severity = "CRITICAL"
        status_code = webui.get("status_code")
        detail = webui.get("detail", "unknown")
        phenomenon_items.append(
            f"WebUI 服务无响应：无法访问健康探针（{webui.get('url', 'http://127.0.0.1:25548/healthz')}，"
            f"状态码: {status_code or '连接失败'}，详情: {detail}）。"
        )
        recommended_actions.append("检查 ALAS 后端 Python 进程或 Launcher 是否存活，确认端口 25548 是否处于监听状态。")
        recommended_actions.append("若 WebUI 进程已崩溃退出，通过托盘右键重新启动 AzurPilot / Alas Launcher。")
        if not suggested_auto_action:
            suggested_auto_action = "restart_instance"

    if "task_error" in reasons:
        current_task = hb.get("current_task") or "未知任务"
        err_msg = sanitize_error(hb.get("latest_error")) or "游戏状态无法推进"
        phenomenon_items.append(f"任务卡死/执行异常：任务 `{current_task}` 持续受阻，报错信息：`{err_msg}`。")
        recommended_actions.append(f"打开 ALAS WebUI ({webui.get('url', 'http://127.0.0.1:25548')}) 确认当前画面是否卡在弹窗或未识别界面。")
        recommended_actions.append("若画面正常但无法点击，在 WebUI 中手动点击「停止」，等待 5 秒后再次点击「启动」。")
        if not suggested_auto_action:
            suggested_auto_action = "restart_instance"

    if "heartbeat_expired" in reasons:
        elapsed = hb.get("elapsed_seconds", 0)
        phenomenon_items.append(f"心跳停止：实例在预期运行状态下，最近 {elapsed} 秒未产生任何新日志（超时阈值: {threshold_seconds}s）。")
        recommended_actions.append(f"检查 WebUI 服务是否存活（当前健康探针状态: {'正常' if webui.get('available') else '无响应'}）。")
        recommended_actions.append("若调度器进程死锁，通过托盘右键退出 AzurPilot 并重新打开 Launcher。")
        if not suggested_auto_action:
            suggested_auto_action = "restart_instance"

    if "ssh_unreachable" in reasons or (ssh.get("enabled") and ssh.get("reachable") is False):
        if severity != "CRITICAL":
            severity = "WARNING"
        server = ssh.get("server", "unknown")
        phenomenon_items.append(f"RemoteAccess SSH 隧道不可达：远程服务器 `{server}` 端口连接超时或拒绝。")
        recommended_actions.append(f"检查远程服务器 `{server}` 的网络连通性与 SSH 服务状态。")
        recommended_actions.append("若使用了内网穿透（如 frp/cloudflared/quick-tunnel），检查对应穿透客户端是否在线。")

    if "heartbeat_absent" in reasons:
        if severity != "CRITICAL":
            severity = "WARNING"
        phenomenon_items.append("心跳日志缺失：实例预期运行，但未检索到今日运行日志或有效心跳记录。")
        recommended_actions.append("确认 ALAS 日志目录与实例名配置，检查调度器是否已成功拉起。")

    if not phenomenon_items:
        phenomenon_items.append(f"检测到未明确归类的持续停滞状态（原因: {reasons}）。")
        recommended_actions.append("检查 ALAS WebUI 运行日志与模拟器画面。")

    phenomenon = "；".join(phenomenon_items)

    recovery_manager = AutoRecoveryManager(AutoRecoveryConfig(auto_recovery_enabled=False))
    plan = recovery_manager.plan_recovery(summary)
    exec_result = recovery_manager.execute_recovery(plan, dry_run=True) if plan else None

    return {
        "alert_id": f"ALERT-{fingerprint[:8]}",
        "fingerprint": fingerprint,
        "severity": severity,
        "instance": instance,
        "title": f"[{severity} 告警] ALAS 实例 {instance} 运行受阻",
        "phenomenon": phenomenon,
        "detected_at": detected_at,
        "duration_seconds": duration,
        "recommended_human_actions": recommended_actions,
        "suggested_recovery_action": suggested_auto_action,
        "recovery_status": exec_result.get("status") if exec_result else "no_plan",
        "recovery_message": exec_result.get("message") if exec_result else None,
    }


def format_actionable_alert_markdown(alert: dict[str, Any]) -> str:
    """格式化为易于阅读的 Markdown 告警卡片。"""
    actions_lines = [f"{i+1}. {act}" for i, act in enumerate(alert.get("recommended_human_actions", []))]
    actions_md = "\n".join(actions_lines)
    recovery_msg = alert.get("recovery_message") or "自动恢复未启用"
    title = alert.get("title", "")
    aid = alert.get("alert_id", "")
    fp = alert.get("fingerprint", "")
    sev = alert.get("severity", "")
    det = alert.get("detected_at", "")
    dur = alert.get("duration_seconds", 0)
    phen = alert.get("phenomenon", "")
    sug = alert.get("suggested_recovery_action", "")
    header = f"### ⚠️ {title}\n\n"
    meta1 = f"- **告警编号**：`{aid}`（指纹: `{fp}`）\n"
    meta2 = f"- **严重级别**：`{sev}`\n"
    meta3 = f"- **发生时间**：`{det}`（持续约 {dur} 秒）\n"
    meta4 = f"- **异常现象**：{phen}\n\n"
    act_sec = f"#### 🛠️ 建议人工动作（请 Andrew 按序排查）：\n{actions_md}\n\n"
    rec_sec = f"#### 🛡️ 自动自愈状态：\n- **自愈开关**：`auto_recovery_enabled=False`（默认安全关闭）\n- **自愈评估**：建议动作 `{sug}`，处理结果：{recovery_msg}\n"
    return header + meta1 + meta2 + meta3 + meta4 + act_sec + rec_sec


class AutoRecoveryConfig:
    """自动恢复策略配置。默认关闭以确保只读安全。"""
    def __init__(
        self,
        auto_recovery_enabled: bool = False,
        max_attempts: int = 2,
        cooldown_seconds: int = 600,
        allowed_actions: tuple[str, ...] = ("restart_adb", "restart_instance"),
    ):
        self.auto_recovery_enabled = auto_recovery_enabled
        self.max_attempts = max_attempts
        self.cooldown_seconds = cooldown_seconds
        self.allowed_actions = allowed_actions


class AutoRecoveryManager:
    """有限自动恢复管理器。

    遵守本票约束：
    - 本票只做到「设计 + 默认关闭的开关」，不默认启用。
    - 动作范围严格限制在 allowed_actions 内部，且具备冷却与最大尝试次数。
    """
    def __init__(self, config: AutoRecoveryConfig | None = None):
        self.config = config or AutoRecoveryConfig()
        self.attempts: dict[str, int] = {}
        self.last_action_time: dict[str, datetime] = {}

    def plan_recovery(self, summary: dict[str, Any]) -> dict[str, Any] | None:
        if not summary.get("stalled"):
            return None
        instance = summary.get("instance", "alas")
        reasons = summary.get("stalled_reasons", [])
        emu = summary.get("emulator", {})

        if not emu.get("online") or "emulator_offline" in reasons:
            return {
                "action": "restart_adb",
                "instance": instance,
                "reason": "emulator_offline",
                "detail": "重启 ADB 服务以尝试重新恢复模拟器连接",
            }
        if "webui_unavailable" in reasons or "task_error" in reasons or "heartbeat_expired" in reasons or "heartbeat_absent" in reasons:
            return {
                "action": "restart_instance",
                "instance": instance,
                "reason": "webui_unavailable" if "webui_unavailable" in reasons else "task_stalled_or_heartbeat_dead",
                "detail": "停止并重启当前异常或卡死的 ALAS 调度实例",
            }
        return None

    def execute_recovery(self, plan: dict[str, Any], dry_run: bool = True) -> dict[str, Any]:
        action = plan.get("action", "none")
        instance = plan.get("instance", "alas")

        if not self.config.auto_recovery_enabled or dry_run:
            return {
                "executed": False,
                "dry_run": True,
                "action": action,
                "instance": instance,
                "status": "dry_run_disabled",
                "message": "自动恢复开关处于默认关闭状态 (auto_recovery_enabled=False)，未执行任何物理变更操作。",
            }

        if action not in self.config.allowed_actions:
            return {
                "executed": False,
                "dry_run": False,
                "action": action,
                "status": "action_not_allowed",
                "message": f"操作 {action} 超出已授权恢复动作范围 {self.config.allowed_actions}",
            }

        now = datetime.now(timezone.utc)
        if instance in self.last_action_time:
            elapsed = (now - self.last_action_time[instance]).total_seconds()
            if elapsed < self.config.cooldown_seconds:
                return {
                    "executed": False,
                    "dry_run": False,
                    "action": action,
                    "status": "in_cooldown",
                    "message": f"仍在恢复冷却期内（已过去 {int(elapsed)}s < {self.config.cooldown_seconds}s），跳过重复执行",
                }

        current_attempts = self.attempts.get(instance, 0)
        if current_attempts >= self.config.max_attempts:
            return {
                "executed": False,
                "dry_run": False,
                "action": action,
                "status": "max_attempts_exceeded",
                "message": f"已达到最大恢复重试次数 ({self.config.max_attempts})，升级至人工干预",
            }

        self.attempts[instance] = current_attempts + 1
        self.last_action_time[instance] = now
        return {
            "executed": True,
            "dry_run": False,
            "action": action,
            "instance": instance,
            "attempt": current_attempts + 1,
            "status": "executed",
            "message": f"已触发恢复动作 {action}（第 {current_attempts + 1} 次尝试）",
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", nargs="?", type=Path, help="JSON 样本文件（对象或数组）")
    parser.add_argument("--log-path", type=Path, help="直接指定本地 ALAS 日志文件，只读尾部")
    parser.add_argument("--instance", help="日志所属实例名（日志模式必填）")
    parser.add_argument("--timezone-offset", default="+00:00", help="日志本地时区偏移，例如 -04:00")
    parser.add_argument("--lines", type=int, default=500)
    parser.add_argument("--threshold-seconds", type=int, default=300)
    parser.add_argument("--live", action="store_true", help="启用实况只读状态巡检（探测 WebUI/ADB/SSH/日志心跳）")
    parser.add_argument("--alas-root", type=Path, help="ALAS 安装/运行根目录")
    parser.add_argument("--webui-url", default="http://127.0.0.1:25548", help="WebUI 基础 URL")
    parser.add_argument("--format-alert", action="store_true", help="若存在告警则输出格式化 Markdown 告警文本")
    parser.add_argument("--desired-running", dest="desired_running", default=None, type=lambda v: str(v).lower() in ("true", "1", "yes") if str(v).lower() not in ("none", "null") else None, help="指示实例是否预期处于运行状态（默认自动探测）")

    effective_argv = list(sys.argv[1:] if argv is None else argv)
    for i, arg in enumerate(effective_argv):
        if arg == "--timezone-offset" and i + 1 < len(effective_argv) and effective_argv[i + 1].startswith("-"):
            effective_argv[i] = f"--timezone-offset={effective_argv[i + 1]}"
            del effective_argv[i + 1]
            break

    args = parser.parse_args(effective_argv)
    try:
        if args.live:
            inst = args.instance or "alas"
            summary = read_live_summary(
                alas_root=args.alas_root,
                instance=inst,
                webui_url=args.webui_url,
                threshold_seconds=args.threshold_seconds,
                timezone_offset=args.timezone_offset,
                desired_running=args.desired_running,
            )
            alert = generate_actionable_alert(summary, threshold_seconds=args.threshold_seconds)
            if args.format_alert:
                if alert:
                    print(format_actionable_alert_markdown(alert))
                elif summary.get("state") == "stopped":
                    print(f"【实例已停止】实例 `{inst}` 已主动停止或非预期运行状态，无需告警。")
                elif summary.get("state") == "idle":
                    print(f"【实例就绪】实例 `{inst}` 当前空闲就绪，无执行中任务。")
                else:
                    print(f"【状态正常】实例 `{inst}` 无活跃告警，心跳与模拟器状态正常。")
                return 0
            result: dict[str, Any] = {"summary": summary, "alert": alert}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.log_path:
            if not args.instance or args.sample:
                raise ValueError("日志模式须提供 --instance 且不能同时提供 JSON 文件")
            samples = read_log_tail(args.log_path, args.instance, args.lines, args.timezone_offset)
        elif args.sample:
            data = json.loads(args.sample.read_text(encoding="utf-8"))
            samples = data if isinstance(data, list) else [data]
            if not all(isinstance(item, dict) for item in samples):
                raise ValueError("样本必须是 JSON 对象或对象数组")
        else:
            raise ValueError("须提供 JSON 样本文件、--log-path 或 --live 选项")
        result = evaluate(samples, args.threshold_seconds)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"输入错误: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
