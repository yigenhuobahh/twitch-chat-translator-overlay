#!/usr/bin/env python3
"""Local, bounded history for TUI task lifecycle and artifact discovery."""

from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import math
import os
from pathlib import Path
import shutil
import time
from typing import Any
import uuid

from run_meta import ABSOLUTE_MAX_LIVE_SEC, pid_is_alive
from task_results import read_task_result
from tui_models import (
    TuiDownloadDraft,
    TuiJobDraft,
    _is_sensitive_field,
    sanitize_download_source_for_history,
)
from tui_task import redact_text

HISTORY_SCHEMA_VERSION = 1
DEFAULT_HISTORY_LIMIT = 100
# Lock acquisition is non-blocking with a bounded retry window so a wedged
# TUI instance cannot freeze this instance's UI thread forever.
LOCK_TIMEOUT_S = 5.0
LOCK_RETRY_INTERVAL_S = 0.05

_LOGGER = logging.getLogger(__name__)


class HistoryLockTimeoutError(OSError):
    """另一个 TUI 实例持有历史锁超过重试窗口时抛出。

    仍继承 OSError：既有 ``except OSError`` 调用方（tui_run 的写路径收尾）
    行为不变。读路径会把本异常归一为安全返回（[]/None/False），只有
    start/clear 等写路径继续把它抛给调用方，避免一个僵死实例让本实例
    启动或点击即崩。
    """


def _degrade_locked_read(exc: HistoryLockTimeoutError) -> None:
    """读路径锁超时的统一降级说明（模块级 debug 级日志）。"""
    _LOGGER.debug("任务历史锁被其他实例长期占用，读操作降级为安全返回: %s", exc)

# Sensitive-key filtering shares one vocabulary with tui_models.
_is_sensitive_key = _is_sensitive_field


def _try_lock_history(handle) -> bool:
    """Attempt a non-blocking exclusive lock; return False when contended."""
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock_history(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
    if isinstance(value, str):
        # 自由文本字段同样过一遍 UI 日志脱敏规则，凭据形态的字符串不能落盘。
        return redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
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
        # 参照 process_util.exclusive_file_lock：锁文件被符号链接替换时拒绝跟随。
        if lock_path.is_symlink():
            raise OSError(f"任务历史锁文件不能是符号链接: {lock_path}")
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            opened = os.fstat(handle.fileno())
            on_disk = lock_path.lstat()
            if (opened.st_dev, opened.st_ino) != (on_disk.st_dev, on_disk.st_ino):
                raise OSError(f"任务历史锁文件在打开后被替换: {lock_path}")
            locked = False
            try:
                deadline = time.monotonic() + LOCK_TIMEOUT_S
                while not _try_lock_history(handle):
                    if time.monotonic() >= deadline:
                        raise HistoryLockTimeoutError(
                            f"任务历史被其他实例长期占用（等待约 {LOCK_TIMEOUT_S:.0f} 秒）: {lock_path}"
                        )
                    time.sleep(LOCK_RETRY_INTERVAL_S)
                locked = True
                yield
            finally:
                if locked:
                    try:
                        _unlock_history(handle)
                    except OSError:
                        pass

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
        try:
            with self._history_lock():
                return list(reversed(self._load()))
        except HistoryLockTimeoutError as exc:
            # 读路径：锁被另一实例长期占用时降级为空历史，而不是让 UI 崩溃。
            _degrade_locked_read(exc)
            return []

    def get(self, record_id: str) -> dict[str, Any] | None:
        try:
            with self._history_lock():
                return next((record for record in self._load() if record.get("id") == record_id), None)
        except HistoryLockTimeoutError as exc:
            _degrade_locked_read(exc)
            return None

    def recover_interrupted(self) -> list[dict[str, Any]]:
        # 含状态改写，但入口在 on_mount；这里按读语义降级：锁被占时本实例
        # 先正常启动，中断恢复留给下次启动重试。
        try:
            with self._history_lock():
                records = self._load()
                changed: list[dict[str, Any]] = []
                now = time.time()
                for record in records:
                    state = record.get("state")
                    if state == "running" and pid_is_alive(record.get("pid")) is True:
                        # Windows 会复用 pid：仅凭 pid 探活会让僵尸 running 记录
                        # 永远“看似存活”并永久阻塞 clear()。与 run_meta 的
                        # is_live_run_meta 一致，加一个绝对时限兜底。
                        if self._record_within_absolute_live_window(record, now=now):
                            continue
                    if state in {"queued", "running"}:
                        record["state"] = "interrupted"
                        record["finished_at"] = time.time()
                        changed.append(record)
                if changed:
                    self._save(records)
                return changed
        except HistoryLockTimeoutError as exc:
            _degrade_locked_read(exc)
            return []

    @staticmethod
    def _record_within_absolute_live_window(record: dict[str, Any], *, now: float) -> bool:
        """Absolute-age check for running records whose pid appears alive.

        以 updated_at（缺省回退 started_at）为时间基准；缺时间字段的极老记录
        保守处理：只要 pid 活着就按存活保持现状（不误杀），注释即此约定的
        说明。updated_at 在 mark_running / set_diagnostic / finish 时都会刷新。
        """
        stamp = record.get("updated_at")
        if not isinstance(stamp, (int, float)):
            stamp = record.get("started_at")
        try:
            stamp_f = float(stamp)
        except (TypeError, ValueError):
            # 旧记录缺时间字段：保守起见沿用旧行为（pid 活 ⇒ 保持 running）。
            return True
        if not math.isfinite(stamp_f):
            return True
        return (now - stamp_f) <= float(ABSOLUTE_MAX_LIVE_SEC)

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
        try:
            with self._history_lock():
                records = self._load()
                for record in records:
                    if record.get("id") == record_id:
                        record["diagnostic_path"] = str(Path(path).resolve())
                        record["updated_at"] = time.time()
                        self._save(records)
                        return
        except HistoryLockTimeoutError as exc:
            # 诊断记录可以在下次导出时重写；锁被占时静默降级即可。
            _degrade_locked_read(exc)

    def has_unfinished_records(self) -> bool:
        """Report whether another task still owns durable history state."""
        try:
            with self._history_lock():
                return any(record.get("state") in {"queued", "running"} for record in self._load())
        except HistoryLockTimeoutError as exc:
            # 读路径：无法确认时按 False 返回（UI 层另有会话级运行判定兜底）。
            _degrade_locked_read(exc)
            return False

    def clear(self) -> bool:
        """Delete managed history only when no queued or running record exists."""
        with self._history_lock():
            records = self._load()
            if any(record.get("state") in {"queued", "running"} for record in records):
                return False
            self._save([])
            root = self.path.parent.resolve()
            for name in ("manifests", "diagnostics", "jobs"):
                managed = (root / name).resolve()
                if managed.parent == root:
                    shutil.rmtree(managed, ignore_errors=True)
            return True

    def draft_for(self, record: dict[str, Any]) -> TuiJobDraft | None:
        # 纯内存/文件读取，不取历史锁，天然不会受锁超时影响。
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
        # 纯内存读取，不取历史锁，天然不会受锁超时影响。
        draft = record.get("draft")
        return TuiDownloadDraft.from_history_fields(draft) if isinstance(draft, dict) else None
