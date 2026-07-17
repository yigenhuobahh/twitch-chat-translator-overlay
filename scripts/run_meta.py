#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Write small run metadata next to job artifacts for failure diagnosis."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any

# Jobs stuck in running without a live pid or heartbeat longer than this are not "live".
DEFAULT_STALE_RUNNING_SEC = 6 * 3600


def run_meta_path(job_dir: str | Path) -> Path:
    return Path(job_dir) / "run_meta.json"


def _parse_meta_time(value: Any) -> float | None:
    """Parse run_meta timestamps to epoch seconds; None if unknown."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return time.mktime(time.strptime(text[:19], fmt))
        except (ValueError, OverflowError, OSError):
            continue
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
      - updated_at/started_at older than stale_after_sec → not live
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
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
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
