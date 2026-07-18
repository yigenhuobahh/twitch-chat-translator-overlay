#!/usr/bin/env python3
"""Opt-in, privacy-safe terminal result manifests for interactive clients."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any

RESULT_FILE_ENV = "TWITCH_OVERLAY_RESULT_FILE"
RESULT_SCHEMA_VERSION = 1
TERMINAL_STATES = {"succeeded", "failed", "manual_required"}


def _existing_artifacts(candidates: list[tuple[str, str | Path | None]]) -> list[dict[str, str]]:
    seen: set[Path] = set()
    artifacts: list[dict[str, str]] = []
    for kind, raw_path in candidates:
        if raw_path is None:
            continue
        try:
            path = Path(raw_path).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        artifacts.append({"kind": str(kind), "path": str(path)})
    return artifacts


def write_task_result(
    *,
    state: str,
    mode: str = "unknown",
    returncode: int,
    artifacts: list[tuple[str, str | Path | None]] | None = None,
) -> bool:
    """Atomically write a terminal result only when a caller opts in via env."""
    raw_path = os.environ.get(RESULT_FILE_ENV, "").strip()
    if not raw_path:
        return False
    try:
        path = Path(raw_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "state": str(state),
            "mode": str(mode),
            "returncode": int(returncode),
            "finished_at": time.time(),
            "artifacts": _existing_artifacts(list(artifacts or [])),
        }
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temp, path)
        return True
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def read_task_result(path: str | Path) -> dict[str, Any] | None:
    """Read a valid result manifest without raising into the TUI."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != RESULT_SCHEMA_VERSION:
        return None
    if data.get("state") not in TERMINAL_STATES or not isinstance(data.get("artifacts"), list):
        return None
    return data
