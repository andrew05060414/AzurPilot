"""只读回放 ALAS JSON 状态或本地日志尾部，输出脱敏巡检事件。

JSON 样本可含 timestamp（ISO 8601）、instance、status、current_task、
expected_progress、desired_running、error、ordinary_task_success、success_task 和 success_id。
只有 running 状态下 ordinary_task_success=true、显式非空 success_task（且不是 Restart）
和新的 success_id 同时成立才算普通任务进展；
首个 success_id 只建立基线。显式错误优先于成功字段，不改变基线；明确 stopped/updating
且 desired_running=false 会取消监测并清除基线，不宣称恢复。日志目前没有可靠的普通任务
成功行，因此日志适配器只识别 ERROR/CRITICAL 并保持恢复状态未知。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LOG_PATTERN = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)"
    r" \| (?P<level>INFO|WARNING|ERROR|CRITICAL) \| (?P<message>.*)$"
)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", nargs="?", type=Path, help="JSON 样本文件（对象或数组）")
    parser.add_argument("--log-path", type=Path, help="直接指定本地 ALAS 日志文件，只读尾部")
    parser.add_argument("--instance", help="日志所属实例名（日志模式必填）")
    parser.add_argument("--timezone-offset", default="+00:00", help="日志本地时区偏移，例如 -04:00")
    parser.add_argument("--lines", type=int, default=500)
    parser.add_argument("--threshold-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    try:
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
            raise ValueError("须提供 JSON 样本文件或 --log-path")
        result = evaluate(samples, args.threshold_seconds)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"输入错误: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
