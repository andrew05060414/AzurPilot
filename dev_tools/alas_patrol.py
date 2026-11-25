#!/usr/bin/env python3
"""ALAS / MAA 只读定时巡检（PX-2039）。

对 ALAS / MAA 只读：运行 alas_maintenance_probe.py --live（WebUI /healthz、adb devices -l、
SSH 端口连通、读日志），读取 ALAS / MAA 日志尾部和 tasklist。绝不启停或点击，不改配置。
巡检会写入自己的状态和日志，并通过 Multica 建告警票。

行为：
- 正常时不输出、不发消息。
- 发现异常 -> 计算指纹 -> 新指纹才用 multica CLI 建一张票；同一指纹在冷却期内不重复建票。
- 指纹连续 2 轮不再出现 -> 视为已恢复，清掉状态，之后再出现会重新告警。

卸载： schtasks /delete /tn "AI-ALAS-Patrol" /f
状态： D:\\Data\\alas-patrol\\state.json    日志： D:\\Logs\\alas-patrol\\patrol.log
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ALAS_ROOT = Path(os.environ.get("ALAS_PATROL_ALAS_ROOT", r"D:\模拟器\AzurPilot"))
MAA_ROOT = Path(os.environ.get("ALAS_PATROL_MAA_ROOT", r"D:\模拟器\MAA-win-x64"))
STATE_PATH = Path(os.environ.get("ALAS_PATROL_STATE", r"D:\Data\alas-patrol\state.json"))
LOG_PATH = Path(os.environ.get("ALAS_PATROL_LOG", r"D:\Logs\alas-patrol\patrol.log"))
COOLDOWN_HOURS = 24
RECOVER_AFTER_CLEAN_RUNS = 2
STUCK_WINDOW_MIN = 60
STUCK_MIN_COUNT = 3
LOG_RETENTION_BYTES = 1_000_000

SECRET_RE = re.compile(
    r"""(?i)\b(token|password|passwd|secret|api[_-]?key|authorization)["']?\s*[=:]\s*"""
    r"""(?:bearer\s+\S+|"[^"]*"|'[^']*'|\S+)"""
)
WAIT_RE = re.compile(r"等待直到 (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
LINE_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
MAA_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
SCROLL_ERROR = "GameStuckError: Scroll drag page error."
STALLED_REASONS = {
    "emulator_offline", "webui_unavailable", "ssh_unreachable", "task_error",
    "heartbeat_expired", "heartbeat_absent",
}


class PatrolCommandError(RuntimeError):
    pass


def sanitize(text: str) -> str:
    return SECRET_RE.sub(lambda m: m.group(1) + "=<redacted>", text)


def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    try:
        if LOG_PATH.stat().st_size > LOG_RETENTION_BYTES:
            data = LOG_PATH.read_bytes()[-LOG_RETENTION_BYTES // 2:]
            LOG_PATH.write_bytes(data)
    except OSError:
        pass


def tail_text(path: Path, max_bytes: int = 2_000_000) -> list[str]:
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    return data.decode("utf-8", errors="replace").splitlines()


def finding(key: str, severity: str, title: str, detail: str, actions: list[str]) -> dict[str, Any]:
    fp = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return {"fingerprint": fp, "key": key, "severity": severity, "title": title,
            "detail": sanitize(detail), "actions": actions}


def log_group(message: str) -> str:
    return hashlib.sha256(sanitize(message).encode("utf-8")).hexdigest()[:16]


def parse_log_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


# ---------------------------------------------------------------- ALAS
def scheduler_is_sleeping(log_lines: list[str], now: datetime) -> bool:
    """最后一条「等待直到 T」的 T 还没到：调度器在合法等待，日志静默不是卡死。"""
    for line in reversed(log_lines[-200:]):
        m = WAIT_RE.search(line)
        if m:
            target = parse_log_time(m.group(1))
            if target is not None:
                return target + timedelta(minutes=5) > now
    return False


def stuck_findings(log_lines: list[str], now: datetime) -> list[dict[str, Any]]:
    """窗口内重复出现「游戏状态无法推进」，按异常文本归类。"""
    cutoff = now - timedelta(minutes=STUCK_WINDOW_MIN)
    groups: dict[str, tuple[str, int]] = {}
    pending = False
    for line in log_lines:
        m = LINE_TS_RE.match(line)
        if m:
            cur_ts = parse_log_time(m.group(1))
            pending = (cur_ts is not None and cutoff <= cur_ts <= now
                       and "游戏状态无法推进" in line and "ERROR" in line)
            continue
        if pending and line.startswith("异常："):
            exc = line.removeprefix("异常：").strip()
            group = log_group(exc)
            if exc == SCROLL_ERROR:
                label = SCROLL_ERROR
            else:
                label = "GameTooManyClickError" if exc.startswith("GameTooManyClickError:") else "GameStuckError"
            groups[group] = (label, groups.get(group, (label, 0))[1] + 1)
            pending = False
    out = []
    for group, (label, n) in groups.items():
        if n >= STUCK_MIN_COUNT:
            out.append(finding(
                f"alas:stuck:{group}", "WARNING",
                f"[AzurPilot] ALAS 重复卡住：{label}",
                f"最近 {STUCK_WINDOW_MIN} 分钟「游戏状态无法推进」出现 {n} 次，异常：{label}。"
                "详细信息见本地 ALAS 日志。",
                ["对照 PX-2039 里的归因，判断是不是脚本 bug；是脚本 bug 就开修复票并在独立 worktree 修。",
                 "不要重启 ALAS 或模拟器（第一阶段不做自动恢复）。"]))
    return out


def alas_findings(summary: dict[str, Any], alas_log: Path | None, now: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    lines: list[str] = []
    if alas_log and alas_log.exists():
        lines = tail_text(alas_log)
    state = summary.get("state")
    reasons = sorted({reason for reason in (summary.get("stalled_reasons") or [])
                      if isinstance(reason, str) and reason in STALLED_REASONS})
    webui_ok = (summary.get("webui") or {}).get("available", True)
    emu_ok = (summary.get("emulator") or {}).get("online", True)

    if not webui_ok:
        out.append(finding("alas:webui_down", "WARNING", "[AzurPilot] ALAS WebUI 健康探针失败",
                           "WebUI /healthz 不可用，详细信息见本地探针输出。",
                           ["确认 alas-launcher 进程是否还在；只读检查，不要重启。"]))
    if not emu_ok:
        out.append(finding("alas:emulator_offline", "WARNING", "[AzurPilot] 模拟器 ADB 不在线",
                           "adb 未检测到在线设备，详细信息见本地探针输出。",
                           ["确认 MuMu 是否被手动关闭或崩溃；需要 Andrew 在场时再处理。"]))
    if state == "stalled" and reasons:
        sleeping = reasons == ["heartbeat_expired"] and scheduler_is_sleeping(lines, now)
        if sleeping:
            log("ALAS 心跳过期，但调度器在合法等待，不告警")
        else:
            out.append(finding(
                "alas:stalled:" + ",".join(reasons), "WARNING",
                "[AzurPilot] ALAS 实例运行受阻：" + ",".join(reasons),
                f"state={state}，原因={','.join(reasons)}。详细信息见本地探针输出。",
                ["在 WebUI 和日志里对照状态；若调度器死锁，需 Andrew 手动退出并重开 Launcher。"]))
    out.extend(stuck_findings(lines, now))
    return out


def run_probe(timezone_offset: str) -> dict[str, Any]:
    py = ALAS_ROOT / ".venv" / "Scripts" / "python.exe"
    cmd = [str(py if py.exists() else sys.executable), "-B",
           str(ALAS_ROOT / "dev_tools" / "alas_maintenance_probe.py"),
           "--live", "--alas-root", str(ALAS_ROOT), "--instance", "alas",
           f"--timezone-offset={timezone_offset}"]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, encoding="utf-8", timeout=60,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        raise PatrolCommandError(f"probe exit {proc.returncode}")
    return json.loads(proc.stdout)["summary"]


# ---------------------------------------------------------------- MAA
def maa_findings(maa_log: Path | None, now: datetime, window_min: int = 30) -> list[dict[str, Any]]:
    """MAA：运行状态（进程在跑但日志停更）和近期 [ERR] 行。MAA 没开不算故障。"""
    if not maa_log or not maa_log.exists():
        return []
    lines = tail_text(maa_log, 500_000)
    cutoff = now - timedelta(minutes=window_min)
    errs: dict[str, int] = {}
    for line in lines:
        m = MAA_TS_RE.match(line)
        if not m or "[ERR]" not in line:
            continue
        event_time = parse_log_time(m.group(1))
        if event_time is None or not cutoff <= event_time <= now:
            continue
        msg = re.sub(r"\[P\w+\]\[T\w+\]\s*", "", line.split("[ERR]", 1)[1])
        group = log_group(re.sub(r"\d+", "N", sanitize(msg)))
        errs[group] = errs.get(group, 0) + 1
    out = []
    for group, n in errs.items():
        out.append(finding(f"maa:err:{group}", "WARNING", "[AzurPilot] MAA 日志报错",
                           f"最近 {window_min} 分钟 asst.log 同类 ERR 出现 {n} 次，详细信息见本地 MAA 日志。",
                           ["对照 MAA 当前任务；只读排查，不要重启 MAA。"]))
    if maa_process_running() and maa_log.exists():
        age = now - datetime.fromtimestamp(maa_log.stat().st_mtime)
        # 仅在日志停更很久且 gui.log 也静默时才算异常；MAA 空闲时日志本来就安静，所以阈值放宽到 6 小时
        if age > timedelta(hours=6):
            out.append(finding("maa:log_stale", "INFO", "[AzurPilot] MAA 进程在运行但日志 6 小时无更新",
                               f"asst.log 距今 {age}。", ["确认 MAA 是否卡在某个任务上。"]))
    return out


def maa_process_running() -> bool:
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq MAA.exe", "/NH"], capture_output=True,
                           text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "MAA.exe" in r.stdout
    except Exception:
        return False


# ---------------------------------------------------------------- state / dedup
def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"open": {}}


def decide(findings: list[dict[str, Any]], state: dict[str, Any], now: datetime,
           deliver: Callable[[dict[str, Any]], None] | None = None) -> list[dict[str, Any]]:
    """返回本轮要新发的告警；就地更新 state。"""
    to_post = []
    seen = {f["fingerprint"] for f in findings}
    open_ = state.setdefault("open", {})
    for f in findings:
        rec = open_.get(f["fingerprint"])
        if rec:
            rec["last_seen"] = now.isoformat(timespec="seconds")
            rec["clean_runs"] = 0
            last_alert = datetime.fromisoformat(rec["alerted_at"])
            if now - last_alert < timedelta(hours=COOLDOWN_HOURS):
                continue
            if deliver:
                deliver(f)
            rec["alerted_at"] = now.isoformat(timespec="seconds")
            to_post.append(f)
        else:
            if deliver:
                deliver(f)
            open_[f["fingerprint"]] = {"key": f["key"], "first_seen": now.isoformat(timespec="seconds"),
                                       "last_seen": now.isoformat(timespec="seconds"),
                                       "alerted_at": now.isoformat(timespec="seconds"), "clean_runs": 0}
            to_post.append(f)
    for fp in list(open_):
        if fp not in seen:
            open_[fp]["clean_runs"] = open_[fp].get("clean_runs", 0) + 1
            if open_[fp]["clean_runs"] >= RECOVER_AFTER_CLEAN_RUNS:
                del open_[fp]
    return to_post


def render(f: dict[str, Any]) -> str:
    acts = "\n".join(f"{i}. {a}" for i, a in enumerate(f["actions"], 1))
    return (f"## 现象\n{f['detail']}\n\n## 建议动作（只读，不自动恢复）\n{acts}\n\n"
            f"## 元数据\n- 指纹：`{f['fingerprint']}`（同一指纹 {COOLDOWN_HOURS} 小时内不重复建票）\n"
            f"- 来源：巡检任务 AI-ALAS-Patrol，来自 PX-2039\n- key：`{f['key']}`\n")


def post(f: dict[str, Any], dry_run: bool, state_dir: Path) -> None:
    body = render(f)
    if dry_run:
        print(f"[DRY-RUN] 将建票：{f['title']}\n{body}")
        return
    cli = os.environ.get("MULTICA_CLI_PATH") or r"C:\Users\Andrew\AppData\Local\Programs\@multicadesktop\resources\app.asar.unpacked\resources\bin\multica.exe"
    with TemporaryDirectory(dir=state_dir) as directory:
        desc = Path(directory) / "alert.md"
        desc.write_text(body, encoding="utf-8")
        r = subprocess.run([cli, "issue", "create", "--title", f["title"], "--description-file", str(desc),
                            "--allow-external-file", "--assignee", "领航员-TB", "--output", "json"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise PatrolCommandError(f"multica issue create exit {r.returncode}")
    log(f"已建票 {f['fingerprint']} {f['title']}")


# ---------------------------------------------------------------- main
def local_offset() -> str:
    off = datetime.now().astimezone().utcoffset() or timedelta(0)
    mins = int(off.total_seconds() // 60)
    sign = "+" if mins >= 0 else "-"
    return f"{sign}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="不建票，只打印")
    ap.add_argument("--fixture", type=Path, help='夹具 JSON：{"alas_summary":{...}}，跳过真实探针')
    ap.add_argument("--alas-log", type=Path, help="覆盖 ALAS 日志路径（默认当天 log\\YYYY-MM-DD_alas.txt）")
    ap.add_argument("--maa-log", type=Path, help="覆盖 MAA asst.log 路径")
    ap.add_argument("--state", type=Path, help="覆盖状态文件路径")
    ap.add_argument("--now", help="覆盖当前时间 YYYY-MM-DD HH:MM:SS（测试用）")
    args = ap.parse_args(argv)

    now = datetime.strptime(args.now, "%Y-%m-%d %H:%M:%S") if args.now else datetime.now()
    state_path = args.state or STATE_PATH
    try:
        alas_log = args.alas_log or ALAS_ROOT / "log" / f"{now:%Y-%m-%d}_alas.txt"
        maa_log = args.maa_log or MAA_ROOT / "debug" / "asst.log"
        if args.fixture:
            summary = json.loads(args.fixture.read_text(encoding="utf-8"))["alas_summary"]
        else:
            summary = run_probe(local_offset())
        findings = alas_findings(summary, alas_log, now) + maa_findings(maa_log, now)
        state = load_state(state_path)
        save = not args.dry_run or args.state is not None
        if save:
            state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            to_post = decide(findings, state, now, lambda f: post(f, args.dry_run, state_path.parent))
        finally:
            if save:
                with TemporaryDirectory(dir=state_path.parent) as directory:
                    pending_state = Path(directory) / "state.json"
                    pending_state.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
                    pending_state.replace(state_path)
        log(f"ok findings={len(findings)} posted={len(to_post)} dry_run={args.dry_run}")
        return 0
    except Exception as exc:  # 巡检自己坏了也要留痕，退出码非 0 让计划任务显示失败
        error = str(exc) if isinstance(exc, PatrolCommandError) else type(exc).__name__
        print(f"patrol error: {error}", file=sys.stderr)
        log(f"ERROR {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
