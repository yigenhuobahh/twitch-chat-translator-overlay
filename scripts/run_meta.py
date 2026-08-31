#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Write small run metadata next to job artifacts for failure diagnosis."""

from __future__ import annotations

import calendar
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any

# Jobs stuck in running without a live pid or heartbeat longer than this are not "live".
DEFAULT_STALE_RUNNING_SEC = 6 * 3600


def run_meta_path(job_dir: str | Path) -> Path:
    return Path(job_dir) / "run_meta.json"


def _local_fixed_utc_offset_sec() -> int:
    """本地时区当前 UTC 偏移（秒）。进程内固定采样，不做历史 DST 推导。"""
    return int(datetime.now().astimezone().utcoffset().total_seconds())


def _parse_meta_time(value: Any) -> float | None:
    """Parse run_meta timestamps to epoch seconds; None if unknown.

    写入端（write_run_meta）是无时区的本地挂钟字符串。旧实现用 time.mktime 按
    "该日期的 DST 规则"回推 epoch：DST 回退日同一挂钟出现两次，stale 判定可能
    偏差 1 小时。这里改为固定无 DST 语义：统一用本地当前 UTC 偏移换算
    （calendar.timegm 按 UTC 计秒再减偏移）。字符串格式保持不变，旧记录仍可
    解析；对小时级的 stale 窗口，跨 DST 边界的残差可忽略。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    offset = _local_fixed_utc_offset_sec()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            naive = datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
        return float(calendar.timegm(naive.timetuple()) - offset)
    return None


def pid_is_alive(pid: Any) -> bool | None:
    """Return True/False if pid liveness is known; None if pid missing/unusable."""
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return None
    if pid_i <= 0:
        return None
    if os.name == "nt":
        # Windows: OpenProcess is more reliable than signal 0.
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            process_query_limited_information = 0x1000
            handle = kernel32.OpenProcess(process_query_limited_information, 0, pid_i)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            # 5 = ACCESS_DENIED (process exists); 87 = invalid parameter (gone)
            err = int(kernel32.GetLastError() or 0)
            if err == 5:
                return True
            return False
        except Exception:
            return None
    try:
        os.kill(pid_i, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but not owned by us — treat as alive (fail closed for clean).
        return True
    except OSError:
        return None
    return True


def is_live_run_meta(
    data: dict[str, Any] | None,
    *,
    stale_after_sec: float = DEFAULT_STALE_RUNNING_SEC,
    now: float | None = None,
) -> bool:
    """Whether a tool job should be treated as still running for --clean-all safety.

    Rules (first match wins for "not live"):
      - missing/empty status → not live
      - status not in running/in_progress/started → not live
      - pid present and dead → not live (crashed / killed)
      - pid present and alive → live, regardless of metadata age
      - without a known-live pid, stale metadata → not live
      - otherwise live (fail closed when meta is ambiguous)
    """
    if not isinstance(data, dict):
        return False
    status = str(data.get("status") or "").strip().lower()
    if status not in ("running", "in_progress", "started"):
        return False

    alive = pid_is_alive(data.get("pid"))
    if alive is False:
        return False
    if alive is True:
        return True

    now_ts = time.time() if now is None else float(now)
    stamp = _parse_meta_time(data.get("updated_at")) or _parse_meta_time(data.get("started_at"))
    if stamp is not None and stale_after_sec > 0 and (now_ts - stamp) > float(stale_after_sec):
        return False

    # No pid and fresh timestamp (or unparsable time): treat as live.
    return True


def write_run_meta(job_dir: str | Path, payload: dict[str, Any]) -> Path:
    path = run_meta_path(job_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dict(payload)
    data.setdefault("pid", os.getpid())
    data.setdefault("started_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    # Unique tmp name + os.replace (mirrors common_utils.atomic_write_json):
    # a fixed ".json.tmp" name let concurrent writers clobber each other.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return path


def mark_run_status(job_dir: str | Path, status: str, **extra: Any) -> Path | None:
    path = run_meta_path(job_dir)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["status"] = status
    data.setdefault("pid", os.getpid())
    data.update(extra)
    return write_run_meta(job_dir, data)
