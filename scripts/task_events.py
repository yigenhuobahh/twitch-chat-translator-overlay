#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Optional, privacy-safe task events for future interactive clients.

多进程并发追加同一事件文件时行可能交错（append 非原子跨进程）；消费方
需容忍坏行（跳过无法 json.loads 的行），不要因单行损坏丢弃整个事件流。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any

EVENT_FILE_ENV = "TWITCH_OVERLAY_EVENT_FILE"
EVENT_SCHEMA_VERSION = 1


def emit_task_event(kind: str, **fields: Any) -> bool:
    """Append one JSONL event when a caller explicitly configured an event file.

    Event writers are observational: an unavailable file must never change a
    render, download, or translation result. Callers should only include coarse
    state, not command arguments, environment values, or user content.
    """
    raw_path = os.environ.get(EVENT_FILE_ENV, "").strip()
    if not raw_path:
        return False
    try:
        path = Path(raw_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event": str(kind),
            "timestamp": time.time(),
            **fields,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded + "\n")
        return True
    except (OSError, TypeError, ValueError):
        return False
