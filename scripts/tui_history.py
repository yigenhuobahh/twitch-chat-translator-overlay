#!/usr/bin/env python3
"""Local, bounded history for TUI task lifecycle and artifact discovery."""

from __future__ import annotations

import json
import math
from pathlib import Path
import shutil
import time
from typing import Any
import uuid

from run_meta import pid_is_alive
from task_results import read_task_result
from tui_models import TuiDownloadDraft, TuiJobDraft

HISTORY_SCHEMA_VERSION = 1
DEFAULT_HISTORY_LIMIT = 100
_SENSITIVE_KEY_PARTS = ("apikey", "token", "password", "authorization", "secret", "oauth")


def _is_sensitive_key(key: object) -> bool:
    normalized = "".join(character for character in str(key).lower() if character.isalnum())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def default_history_path(root: str | Path | None = None) -> Path:
    base = Path(root) if root is not None else Path.cwd()
    return base / "outputs" / ".tui-history" / "history.json"


def _safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _safe_value(item)
            for key, item in value.items()
            if not _is_sensitive_key(key)
        }
    if isinstance(value, list):
        return [_safe_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class TuiHistoryStore:
    """Atomic JSON history; pipeline metadata remains authoritative for renders."""

    def __init__(self, path: str | Path | None = None, *, limit: int = DEFAULT_HISTORY_LIMIT) -> None:
        self.path = default_history_path() if path is None else Path(path)
        self.limit = max(1, int(limit))

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(data, dict) or data.get("schema_version") != HISTORY_SCHEMA_VERSION:
            return []
        records = data.get("records")
        if not isinstance(records, list):
            return []
        normalized: list[dict[str, Any]] = []
        sanitized_legacy_record = False
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                continue
            try:
                record["started_at"] = float(record.get("started_at", 0))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(record["started_at"]):
                continue
            if record.get("state") not in {
                "queued", "running", "succeeded", "failed", "cancelled", "interrupted", "manual_required",
            }:
                continue
            draft = record.get("draft")
            if draft is not None and not isinstance(draft, dict):
                record["draft"] = None
            elif isinstance(draft, dict):
                # Sanitize records written by older builds before OAuth was
                # treated as sensitive and migrate them on the next read.
                safe_draft = _safe_value(draft)
                if safe_draft != draft:
                    sanitized_legacy_record = True
                record["draft"] = safe_draft
            result_path = record.get("result_path")
            if result_path is not None and not isinstance(result_path, str):
                record["result_path"] = None
            # Older records embedded a result payload.  It is intentionally
            # ignored: current history references the durable manifest file.
            record.pop("result", None)
            normalized.append(record)
        if sanitized_legacy_record:
            self._save(normalized)
        return normalized

    def _save(self, records: list[dict[str, Any]]) -> None:
        records = records[-self.limit :]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": HISTORY_SCHEMA_VERSION, "records": records}
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)
        self._prune_artifacts({str(record["id"]) for record in records})

    def _prune_artifacts(self, active_ids: set[str]) -> None:
        """Keep managed manifests/diagnostics aligned with the record limit."""
        root = self.path.parent.resolve()
        for name, suffix in (("manifests", ".json"), ("diagnostics", ".txt")):
            directory = root / name
            if not directory.is_dir() or directory.resolve().parent != root:
                continue
            for candidate in directory.glob(f"*{suffix}"):
                if candidate.stem not in active_ids:
                    try:
                        candidate.unlink()
                    except OSError:
                        pass

    def list_records(self) -> list[dict[str, Any]]:
        return list(reversed(self._load()))

    def get(self, record_id: str) -> dict[str, Any] | None:
        return next((record for record in self._load() if record.get("id") == record_id), None)

    def recover_interrupted(self) -> list[dict[str, Any]]:
        records = self._load()
        changed: list[dict[str, Any]] = []
        for record in records:
            state = record.get("state")
            if state == "running" and pid_is_alive(record.get("pid")) is True:
                continue
            if state in {"queued", "running"}:
                record["state"] = "interrupted"
                record["finished_at"] = time.time()
                changed.append(record)
        if changed:
            self._save(records)
        return changed

    def start(self, draft: TuiJobDraft | TuiDownloadDraft | None, *, label: str) -> dict[str, Any]:
        records = self._load()
        now = time.time()
        record: dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "state": "queued",
            "label": str(label),
            "started_at": now,
            "updated_at": now,
            "pid": None,
            "draft": _safe_value(
                draft.to_history_fields() if isinstance(draft, TuiDownloadDraft) else draft.to_job_fields()
            ) if draft is not None else None,
            "result_path": None,
            "diagnostic_path": None,
        }
        records.append(record)
        self._save(records)
        return record

    def mark_running(self, record_id: str, *, pid: int | None, result_path: str | Path | None) -> None:
        records = self._load()
        for record in records:
            if record.get("id") == record_id:
                record["state"] = "running"
                record["pid"] = int(pid) if pid else None
                record["result_path"] = str(result_path) if result_path else None
                record["updated_at"] = time.time()
                self._save(records)
                return

    def manifest_path(self, record_id: str) -> Path:
        return self.path.parent / "manifests" / f"{record_id}.json"

    def result_for(self, record: dict[str, Any]) -> dict[str, Any] | None:
        raw_path = record.get("result_path")
        return read_task_result(raw_path) if isinstance(raw_path, str) and raw_path else None

    def finish(
        self,
        record_id: str,
        *,
        state: str,
        returncode: int | None,
        result_path: str | Path | None,
    ) -> dict[str, Any] | None:
        records = self._load()
        for record in records:
            if record.get("id") != record_id:
                continue
            record["state"] = state
            record["returncode"] = returncode
            record["result_path"] = str(Path(result_path).resolve()) if result_path else None
            record["finished_at"] = time.time()
            record["updated_at"] = record["finished_at"]
            self._save(records)
            return record
        return None

    def set_diagnostic(self, record_id: str, path: str | Path) -> None:
        records = self._load()
        for record in records:
            if record.get("id") == record_id:
                record["diagnostic_path"] = str(Path(path).resolve())
                record["updated_at"] = time.time()
                self._save(records)
                return

    def clear(self) -> None:
        self._save([])
        root = self.path.parent.resolve()
        for name in ("manifests", "diagnostics"):
            managed = (root / name).resolve()
            if managed.parent == root:
                shutil.rmtree(managed, ignore_errors=True)

    @staticmethod
    def draft_for(record: dict[str, Any]) -> TuiJobDraft | None:
        draft = record.get("draft")
        if not isinstance(draft, dict):
            return None
        try:
            return TuiJobDraft.from_fields(draft)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def download_for(record: dict[str, Any]) -> TuiDownloadDraft | None:
        draft = record.get("draft")
        return TuiDownloadDraft.from_history_fields(draft) if isinstance(draft, dict) else None
