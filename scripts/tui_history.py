#!/usr/bin/env python3
"""Local, bounded history for TUI task lifecycle and artifact discovery."""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import shutil
import time
from typing import Any
import uuid

from run_meta import pid_is_alive
from task_results import read_task_result
from tui_models import TuiDownloadDraft, TuiJobDraft, sanitize_download_source_for_history

HISTORY_SCHEMA_VERSION = 1
DEFAULT_HISTORY_LIMIT = 100
_SENSITIVE_KEY_PARTS = ("apikey", "token", "password", "authorization", "secret", "oauth")


def _is_sensitive_key(key: object) -> bool:
    normalized = "".join(character for character in str(key).lower() if character.isalnum())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def default_history_path(root: str | Path | None = None) -> Path:
    base = Path(root) if root is not None else Path.cwd()
    return base / "outputs" / ".tui-history" / "history.json"


def _safe_value(value: Any, *, field_name: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _safe_value(item, field_name=str(key))
            for key, item in value.items()
            if not _is_sensitive_key(key)
        }
    if isinstance(value, list):
        return [_safe_value(item, field_name=field_name) for item in value]
    if field_name == "download" and isinstance(value, str):
        return sanitize_download_source_for_history(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class TuiHistoryStore:
    """Atomic JSON history; pipeline metadata remains authoritative for renders."""

    def __init__(self, path: str | Path | None = None, *, limit: int = DEFAULT_HISTORY_LIMIT) -> None:
        self.path = default_history_path() if path is None else Path(path)
        self.limit = max(1, int(limit))

    @contextmanager
    def _history_lock(self):
        """Serialize read-modify-write history updates across TUI processes."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

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
                authenticated_download = draft.get("_tui_task_type") == "download" and any(
                    _is_sensitive_key(key) and bool(value) for key, value in draft.items()
                )
                safe_draft = _safe_value(draft)
                if authenticated_download:
                    safe_draft["authentication_required"] = True
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
        for name, suffix in (("manifests", ".json"), ("diagnostics", ".txt"), ("jobs", ".yaml")):
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
        with self._history_lock():
            return list(reversed(self._load()))

    def get(self, record_id: str) -> dict[str, Any] | None:
        with self._history_lock():
            return next((record for record in self._load() if record.get("id") == record_id), None)

    def recover_interrupted(self) -> list[dict[str, Any]]:
        with self._history_lock():
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
        with self._history_lock():
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
            snapshot: Path | None = None
            try:
                if isinstance(draft, TuiJobDraft):
                    snapshot = draft.save_job(
                        self.job_path(record["id"]),
                        pin_paths=True,
                        overwrite=True,
                    )
                    record["job_path"] = str(snapshot)
                records.append(record)
                self._save(records)
            except (OSError, TypeError, ValueError):
                if snapshot is not None:
                    try:
                        snapshot.unlink()
                    except OSError:
                        pass
                raise
            return record

    def mark_running(self, record_id: str, *, pid: int | None, result_path: str | Path | None) -> None:
        with self._history_lock():
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

    def job_path(self, record_id: str) -> Path:
        return self.path.parent / "jobs" / f"{record_id}.yaml"

    def job_for(self, record: dict[str, Any]) -> Path | None:
        raw_path = record.get("job_path")
        if not isinstance(raw_path, str) or not raw_path:
            return None
        root = (self.path.parent / "jobs").resolve()
        candidate = Path(raw_path).resolve()
        if candidate.parent != root or candidate.suffix.lower() != ".yaml":
            return None
        return candidate if candidate.is_file() else None

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
        with self._history_lock():
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
        with self._history_lock():
            records = self._load()
            for record in records:
                if record.get("id") == record_id:
                    record["diagnostic_path"] = str(Path(path).resolve())
                    record["updated_at"] = time.time()
                    self._save(records)
                    return

    def clear(self) -> None:
        with self._history_lock():
            self._save([])
            root = self.path.parent.resolve()
            for name in ("manifests", "diagnostics", "jobs"):
                managed = (root / name).resolve()
                if managed.parent == root:
                    shutil.rmtree(managed, ignore_errors=True)

    def draft_for(self, record: dict[str, Any]) -> TuiJobDraft | None:
        draft = record.get("draft")
        if not isinstance(draft, dict):
            return None
        try:
            snapshot = self.job_for(record)
            return TuiJobDraft.from_fields(draft, source_job=str(snapshot) if snapshot else "")
        except (TypeError, ValueError):
            return None

    @staticmethod
    def download_for(record: dict[str, Any]) -> TuiDownloadDraft | None:
        draft = record.get("draft")
        return TuiDownloadDraft.from_history_fields(draft) if isinstance(draft, dict) else None
