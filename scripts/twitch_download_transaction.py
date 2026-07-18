"""Process-crash recovery for paired Twitch video and chat publication.

The transaction serializes cooperating callers on one local filesystem and
recovers a consistent old or new pair after process termination. It is not a
power-loss filesystem transaction or a security boundary against hostile
concurrent writers.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

from process_util import FileLockTimeoutError, exclusive_file_lock
from twitch_download_types import TwitchDownloadError

_DOWNLOAD_TRANSACTION_VERSION = 1
_DOWNLOAD_TRANSACTION_JOURNAL = ".twitch-download-publish.json"
_DOWNLOAD_TRANSACTION_MAX_BYTES = 128 * 1024
_DOWNLOAD_TRANSACTION_GUARD = ".twitch-download-publish.guard"
_DOWNLOAD_TRANSACTION_GUARD_WAIT_SECONDS = 30.0
_DOWNLOAD_SIGNATURE_CHUNK_BYTES = 1024 * 1024
_DOWNLOAD_TRANSACTION_CLAIM = ".twitch-download-publish.lock"
_DOWNLOAD_TRANSACTION_CLAIM_MAX_BYTES = 4096
_ACTIVE_DOWNLOAD_TRANSACTIONS: set[str] = set()
_ACTIVE_DOWNLOAD_TRANSACTION_LOCK = threading.Lock()


@dataclass(frozen=True)
class _DownloadTransactionEntry:
    role: str
    destination: Path
    staged: Path
    backup: Path
    old_signature: dict[str, object] | None
    staged_signature: dict[str, object]


def _download_transaction_journal_path(root: Path) -> Path:
    return root / _DOWNLOAD_TRANSACTION_JOURNAL


def _sync_directory(path: Path) -> None:
    """Best-effort directory sync for durable rename/unlink metadata."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _download_transaction_claim_path(root: Path) -> Path:
    return root / _DOWNLOAD_TRANSACTION_CLAIM


def _download_transaction_guard_path(root: Path) -> Path:
    return root / _DOWNLOAD_TRANSACTION_GUARD


def _is_download_transaction_metadata_path(root: Path, path: Path) -> bool:
    return path in (
        _download_transaction_journal_path(root),
        _download_transaction_claim_path(root),
        _download_transaction_guard_path(root),
    )


@contextlib.contextmanager
def _download_transaction_guard(root: Path) -> Iterator[None]:
    """Serialize publication and recovery, with automatic release on process exit."""
    guard = _download_transaction_guard_path(root)
    try:
        with exclusive_file_lock(guard, timeout=_DOWNLOAD_TRANSACTION_GUARD_WAIT_SECONDS):
            yield
    except FileLockTimeoutError as exc:
        raise TwitchDownloadError(
            f"另一个进程仍在发布或恢复下载结果，等待 "
            f"{_DOWNLOAD_TRANSACTION_GUARD_WAIT_SECONDS:g} 秒后超时: {root}"
        ) from exc
    except OSError as exc:
        raise TwitchDownloadError(f"无法使用下载事务互斥锁 {guard}: {exc}") from exc


def _register_active_download_transaction(transaction_id: str) -> None:
    with _ACTIVE_DOWNLOAD_TRANSACTION_LOCK:
        _ACTIVE_DOWNLOAD_TRANSACTIONS.add(transaction_id)


def _unregister_active_download_transaction(transaction_id: str) -> None:
    with _ACTIVE_DOWNLOAD_TRANSACTION_LOCK:
        _ACTIVE_DOWNLOAD_TRANSACTIONS.discard(transaction_id)


def _download_transaction_is_active(transaction_id: str) -> bool:
    with _ACTIVE_DOWNLOAD_TRANSACTION_LOCK:
        return transaction_id in _ACTIVE_DOWNLOAD_TRANSACTIONS


def _transaction_evidence_error(root: Path, message: str) -> TwitchDownloadError:
    journal = _download_transaction_journal_path(root)
    claim = _download_transaction_claim_path(root)
    return TwitchDownloadError(f"{message}；事务现场已保留: {journal} / {claim}")


def _canonical_transaction_root(root: Path) -> Path:
    try:
        resolved = Path(root).resolve(strict=True)
    except OSError as exc:
        raise TwitchDownloadError(f"下载事务目录不可用: {root}: {exc}") from exc
    if not resolved.is_dir():
        raise TwitchDownloadError(f"下载事务目录不是文件夹: {resolved}")
    return resolved


def _canonical_transaction_member(
    root: Path,
    path: Path,
    *,
    label: str,
) -> tuple[str, Path]:
    raw_path = Path(path)
    if raw_path.is_symlink():
        raise TwitchDownloadError(f"{label}不能是符号链接: {raw_path}")
    try:
        resolved = raw_path.resolve(strict=False)
        relative = resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise TwitchDownloadError(f"{label}必须位于下载目录内: {raw_path}") from exc
    value = relative.as_posix()
    if not value or value == "." or "\\" in value or ":" in value:
        raise TwitchDownloadError(f"{label}不是安全的事务相对路径: {raw_path}")
    if _is_download_transaction_metadata_path(root, resolved):
        raise TwitchDownloadError(f"{label}与下载事务元数据冲突: {raw_path}")
    return value, resolved


def _resolve_transaction_relative_path(
    root: Path,
    value: object,
    *,
    label: str,
) -> Path:
    if type(value) is not str or not value:
        raise _transaction_evidence_error(root, f"下载事务日志中的 {label} 必须是非空相对路径")
    if "\\" in value or ":" in value or "\x00" in value:
        raise _transaction_evidence_error(root, f"下载事务日志中的 {label} 含不安全路径字符")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise _transaction_evidence_error(root, f"下载事务日志中的 {label} 发生路径逃逸")
    lexical = root.joinpath(*parts)
    if lexical.is_symlink():
        raise _transaction_evidence_error(root, f"下载事务日志中的 {label} 不能是符号链接")
    try:
        resolved = lexical.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise _transaction_evidence_error(root, f"下载事务日志中的 {label} 发生路径逃逸") from exc
    if _is_download_transaction_metadata_path(root, resolved):
        raise _transaction_evidence_error(root, f"下载事务日志中的 {label} 指向事务元数据")
    return resolved


def _file_transaction_signature(path: Path) -> dict[str, object] | None:
    """Return a rename-stable, bounded-I/O signature for a regular file.

    The sample detects ordinary replacement/corruption without hashing a
    potentially multi-gigabyte VOD; it is not an adversarial integrity proof.
    """
    if path.is_symlink():
        raise TwitchDownloadError(f"事务文件不能是符号链接: {path}")
    try:
        initial = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TwitchDownloadError(f"无法读取事务文件状态: {path}: {exc}") from exc
    if not stat.S_ISREG(initial.st_mode):
        raise TwitchDownloadError(f"事务路径不是普通文件: {path}")

    digest = hashlib.sha256()
    digest.update(b"twitch-download-signature-v1\x00")
    digest.update(str(initial.st_size).encode("ascii"))
    digest.update(b"\x00")
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if initial.st_size <= 2 * _DOWNLOAD_SIGNATURE_CHUNK_BYTES:
                while block := handle.read(_DOWNLOAD_SIGNATURE_CHUNK_BYTES):
                    digest.update(block)
            else:
                digest.update(handle.read(_DOWNLOAD_SIGNATURE_CHUNK_BYTES))
                digest.update(b"\x00tail\x00")
                handle.seek(initial.st_size - _DOWNLOAD_SIGNATURE_CHUNK_BYTES)
                digest.update(handle.read(_DOWNLOAD_SIGNATURE_CHUNK_BYTES))
            final = os.fstat(handle.fileno())
    except OSError as exc:
        raise TwitchDownloadError(f"无法读取事务文件签名: {path}: {exc}") from exc

    initial_identity = (
        initial.st_dev,
        initial.st_ino,
        initial.st_size,
        initial.st_mtime_ns,
    )
    opened_identity = (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
    )
    final_identity = (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
    )
    if initial_identity != opened_identity or opened_identity != final_identity:
        raise TwitchDownloadError(f"计算签名时事务文件发生变化: {path}")
    return {
        "kind": "file",
        "device": int(final.st_dev),
        "inode": int(final.st_ino),
        "size": int(final.st_size),
        "mtime_ns": int(final.st_mtime_ns),
        "sample_sha256": digest.hexdigest(),
    }


def _validate_transaction_signature(
    root: Path,
    value: object,
    *,
    label: str,
    optional: bool,
) -> dict[str, object] | None:
    if value is None and optional:
        return None
    required = {"kind", "device", "inode", "size", "mtime_ns", "sample_sha256"}
    if type(value) is not dict or set(value) != required:
        raise _transaction_evidence_error(root, f"下载事务日志中的 {label} 签名结构无效")
    if value.get("kind") != "file":
        raise _transaction_evidence_error(root, f"下载事务日志中的 {label} 文件类型无效")
    for field in ("device", "inode", "size", "mtime_ns"):
        number = value.get(field)
        if type(number) is not int or (field != "mtime_ns" and number < 0):
            raise _transaction_evidence_error(root, f"下载事务日志中的 {label}.{field} 无效")
    fingerprint = value.get("sample_sha256")
    if type(fingerprint) is not str or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise _transaction_evidence_error(root, f"下载事务日志中的 {label} 摘要无效")
    return dict(value)


def _validate_transaction_identity(
    root: Path,
    transaction_id: object,
    owner_pid: object,
    created_ns: object,
    *,
    label: str,
) -> tuple[str, int, int]:
    if type(transaction_id) is not str or re.fullmatch(r"[0-9a-f]{32}", transaction_id) is None:
        raise _transaction_evidence_error(root, f"{label}事务 ID 无效")
    if type(owner_pid) is not int or owner_pid <= 0:
        raise _transaction_evidence_error(root, f"{label}owner_pid 无效")
    if type(created_ns) is not int or created_ns <= 0:
        raise _transaction_evidence_error(root, f"{label}created_ns 无效")
    return transaction_id, owner_pid, created_ns


def _validate_download_transaction_claim(
    root: Path,
    payload: object,
) -> tuple[str, int, int]:
    if type(payload) is not dict:
        raise _transaction_evidence_error(root, "下载事务 claim 根节点必须是对象")
    required = {"version", "transaction_id", "owner_pid", "created_ns"}
    if set(payload) != required:
        raise _transaction_evidence_error(root, "下载事务 claim 字段集合无效")
    if type(payload.get("version")) is not int or payload["version"] != _DOWNLOAD_TRANSACTION_VERSION:
        raise _transaction_evidence_error(root, "下载事务 claim 版本不受支持")
    return _validate_transaction_identity(
        root,
        payload.get("transaction_id"),
        payload.get("owner_pid"),
        payload.get("created_ns"),
        label="下载事务 claim ",
    )


def _load_download_transaction_claim(root: Path) -> tuple[str, int, int]:
    claim = _download_transaction_claim_path(root)
    if claim.is_symlink() or not claim.is_file():
        raise _transaction_evidence_error(root, "下载事务 claim 不是普通文件")
    try:
        with claim.open("rb") as handle:
            encoded = handle.read(_DOWNLOAD_TRANSACTION_CLAIM_MAX_BYTES + 1)
    except OSError as exc:
        raise _transaction_evidence_error(root, f"无法读取下载事务 claim: {exc}") from exc
    if len(encoded) > _DOWNLOAD_TRANSACTION_CLAIM_MAX_BYTES:
        raise _transaction_evidence_error(root, "下载事务 claim 超过大小上限")
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _transaction_evidence_error(root, f"下载事务 claim 不是有效 JSON: {exc}") from exc
    return _validate_download_transaction_claim(root, payload)


def _claim_download_transaction(
    root: Path,
    transaction_id: str,
    owner_pid: int,
    created_ns: int,
) -> None:
    payload: dict[str, object] = {
        "version": _DOWNLOAD_TRANSACTION_VERSION,
        "transaction_id": transaction_id,
        "owner_pid": owner_pid,
        "created_ns": created_ns,
    }
    _validate_download_transaction_claim(root, payload)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    claim = _download_transaction_claim_path(root)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    _register_active_download_transaction(transaction_id)
    try:
        descriptor = os.open(claim, flags, 0o600)
    except FileExistsError as exc:
        _unregister_active_download_transaction(transaction_id)
        raise _transaction_evidence_error(root, "下载目录已被另一个发布事务独占") from exc
    except OSError as exc:
        _unregister_active_download_transaction(transaction_id)
        raise TwitchDownloadError(f"无法独占下载发布事务: {claim}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        _sync_directory(root)
    except Exception as exc:
        _unregister_active_download_transaction(transaction_id)
        try:
            claim.unlink(missing_ok=True)
            _sync_directory(root)
        except OSError:
            pass
        raise TwitchDownloadError(f"无法持久化下载事务 claim: {claim}: {exc}") from exc
    except BaseException:
        _unregister_active_download_transaction(transaction_id)
        try:
            claim.unlink(missing_ok=True)
            _sync_directory(root)
        except OSError:
            pass
        raise


def _validate_download_transaction_document(
    root: Path,
    payload: object,
) -> tuple[str, str, int, int, list[_DownloadTransactionEntry]]:
    if type(payload) is not dict:
        raise _transaction_evidence_error(root, "下载事务日志根节点必须是对象")
    required = {"version", "transaction_id", "owner_pid", "created_ns", "state", "entries"}
    if set(payload) != required:
        raise _transaction_evidence_error(root, "下载事务日志字段集合无效")
    if type(payload.get("version")) is not int or payload["version"] != _DOWNLOAD_TRANSACTION_VERSION:
        raise _transaction_evidence_error(root, "下载事务日志版本不受支持")
    transaction_id, owner_pid, created_ns = _validate_transaction_identity(
        root,
        payload.get("transaction_id"),
        payload.get("owner_pid"),
        payload.get("created_ns"),
        label="下载事务日志 ",
    )
    state = payload.get("state")
    if state not in ("prepared", "committed"):
        raise _transaction_evidence_error(root, "下载事务状态无效")
    raw_entries = payload.get("entries")
    if type(raw_entries) is not list or len(raw_entries) != 2:
        raise _transaction_evidence_error(root, "下载事务必须恰好包含视频和聊天两项")

    entries: list[_DownloadTransactionEntry] = []
    roles: set[str] = set()
    all_paths: set[Path] = set()
    entry_fields = {
        "role",
        "destination",
        "staged",
        "backup",
        "old_signature",
        "staged_signature",
    }
    for index, raw_entry in enumerate(raw_entries):
        if type(raw_entry) is not dict or set(raw_entry) != entry_fields:
            raise _transaction_evidence_error(root, f"下载事务第 {index + 1} 项结构无效")
        role = raw_entry.get("role")
        if role not in ("video", "chat") or role in roles:
            raise _transaction_evidence_error(root, f"下载事务第 {index + 1} 项角色无效")
        roles.add(role)
        destination = _resolve_transaction_relative_path(
            root, raw_entry.get("destination"), label=f"{role}.destination"
        )
        staged = _resolve_transaction_relative_path(root, raw_entry.get("staged"), label=f"{role}.staged")
        backup = _resolve_transaction_relative_path(root, raw_entry.get("backup"), label=f"{role}.backup")
        old_signature = _validate_transaction_signature(
            root, raw_entry.get("old_signature"), label=f"{role}.old_signature", optional=True
        )
        staged_signature = _validate_transaction_signature(
            root, raw_entry.get("staged_signature"), label=f"{role}.staged_signature", optional=False
        )
        assert staged_signature is not None
        if old_signature == staged_signature:
            raise _transaction_evidence_error(root, f"下载事务 {role} 的新旧签名不可区分")
        backup_prefix = f".{destination.name}.backup-"
        backup_nonce = backup.name.removeprefix(backup_prefix)
        if (
            backup.parent != destination.parent
            or not backup.name.startswith(backup_prefix)
            or re.fullmatch(r"[0-9a-f]{32}", backup_nonce) is None
        ):
            raise _transaction_evidence_error(root, f"下载事务 {role} 的备份路径无效")
        for candidate in (destination, staged, backup):
            if candidate in all_paths:
                raise _transaction_evidence_error(root, "下载事务包含重复或歧义路径")
            all_paths.add(candidate)
        entries.append(
            _DownloadTransactionEntry(
                role=role,
                destination=destination,
                staged=staged,
                backup=backup,
                old_signature=old_signature,
                staged_signature=staged_signature,
            )
        )
    if roles != {"video", "chat"}:
        raise _transaction_evidence_error(root, "下载事务缺少视频或聊天项")
    return str(state), transaction_id, owner_pid, created_ns, entries


def _atomic_write_download_transaction(root: Path, payload: dict[str, object]) -> None:
    _state, transaction_id, owner_pid, created_ns, _entries = _validate_download_transaction_document(root, payload)
    claim_identity = _load_download_transaction_claim(root)
    if claim_identity != (transaction_id, owner_pid, created_ns):
        raise _transaction_evidence_error(root, "下载事务日志与独占 claim 身份不一致")
    journal = _download_transaction_journal_path(root)
    journal_present = journal.exists() or journal.is_symlink()
    if _state == "prepared" and journal_present:
        raise _transaction_evidence_error(root, "prepared 下载事务拒绝覆盖已有日志")
    if _state == "committed":
        if not journal_present:
            raise _transaction_evidence_error(root, "提交下载事务时 prepared 日志缺失")
        (
            current_state,
            current_transaction_id,
            current_owner_pid,
            current_created_ns,
            _current_entries,
        ) = _load_download_transaction(root)
        if current_state != "prepared" or (current_transaction_id, current_owner_pid, current_created_ns) != (
            transaction_id,
            owner_pid,
            created_ns,
        ):
            raise _transaction_evidence_error(root, "提交标记与 prepared 日志身份不一致")
    temporary = journal.with_name(f".{journal.name}.tmp-{uuid.uuid4().hex}")
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > _DOWNLOAD_TRANSACTION_MAX_BYTES:
        raise TwitchDownloadError("下载事务日志超过大小上限")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, journal)
        _sync_directory(root)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise TwitchDownloadError(f"无法持久化下载事务日志: {journal}: {exc}") from exc


def _load_download_transaction(
    root: Path,
) -> tuple[str, str, int, int, list[_DownloadTransactionEntry]]:
    journal = _download_transaction_journal_path(root)
    if journal.is_symlink() or not journal.is_file():
        raise _transaction_evidence_error(root, "下载事务日志不是普通文件")
    try:
        with journal.open("rb") as handle:
            encoded = handle.read(_DOWNLOAD_TRANSACTION_MAX_BYTES + 1)
    except OSError as exc:
        raise _transaction_evidence_error(root, f"无法读取下载事务日志: {exc}") from exc
    if len(encoded) > _DOWNLOAD_TRANSACTION_MAX_BYTES:
        raise _transaction_evidence_error(root, "下载事务日志超过大小上限")
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _transaction_evidence_error(root, f"下载事务日志不是有效 JSON: {exc}") from exc
    return _validate_download_transaction_document(root, payload)


def recover_download_transaction(
    root: Path,
    *,
    expected_transaction_id: str | None = None,
) -> str | None:
    transaction_root = _canonical_transaction_root(root)
    with _download_transaction_guard(transaction_root):
        return _recover_download_transaction_locked(
            transaction_root,
            expected_transaction_id=expected_transaction_id,
        )


def _recover_download_transaction_locked(
    root: Path,
    *,
    expected_transaction_id: str | None = None,
) -> str | None:
    """Recover a prior pair publication without guessing about disk state."""
    transaction_root = _canonical_transaction_root(root)
    journal = _download_transaction_journal_path(transaction_root)
    claim = _download_transaction_claim_path(transaction_root)
    journal_present = journal.exists() or journal.is_symlink()
    claim_present = claim.exists() or claim.is_symlink()
    if not journal_present and not claim_present:
        return None
    if not journal_present:
        claim_transaction_id: str | None = None
        try:
            claim_transaction_id, _claim_owner_pid, _claim_created_ns = _load_download_transaction_claim(
                transaction_root
            )
        except TwitchDownloadError:
            pass
        if (
            claim_transaction_id is not None
            and _download_transaction_is_active(claim_transaction_id)
            and expected_transaction_id != claim_transaction_id
        ):
            raise _transaction_evidence_error(transaction_root, "无日志的下载事务 claim 仍处于活动状态")

        cleanup_errors: list[str] = []
        try:
            claim.unlink()
        except OSError as exc:
            cleanup_errors.append(f"{claim}: {exc}")
        temporary_prefix = f".{_DOWNLOAD_TRANSACTION_JOURNAL}.tmp-"
        for candidate in transaction_root.iterdir():
            if re.fullmatch(re.escape(temporary_prefix) + r"[0-9a-f]{32}", candidate.name) is None:
                continue
            try:
                if candidate.is_dir() and not candidate.is_symlink():
                    raise OSError("事务日志临时路径不是普通文件")
                candidate.unlink()
            except OSError as exc:
                cleanup_errors.append(f"{candidate}: {exc}")
        try:
            _sync_directory(transaction_root)
        except OSError as exc:
            cleanup_errors.append(f"{transaction_root}: {exc}")
        if cleanup_errors:
            raise TwitchDownloadError("无法完整清理未开始的下载事务: " + "; ".join(cleanup_errors))
        return None

    if not claim_present:
        raise _transaction_evidence_error(transaction_root, "下载事务日志存在，但独占 claim 缺失")

    claim_transaction_id, claim_owner_pid, claim_created_ns = _load_download_transaction_claim(transaction_root)
    active = _download_transaction_is_active(claim_transaction_id)
    if expected_transaction_id is not None and expected_transaction_id != claim_transaction_id:
        raise _transaction_evidence_error(transaction_root, "恢复请求与下载事务 claim 身份不一致")
    if active and expected_transaction_id != claim_transaction_id:
        raise _transaction_evidence_error(transaction_root, "下载事务仍由当前进程中的发布者占用")

    state, transaction_id, owner_pid, created_ns, entries = _load_download_transaction(transaction_root)
    if (transaction_id, owner_pid, created_ns) != (
        claim_transaction_id,
        claim_owner_pid,
        claim_created_ns,
    ):
        raise _transaction_evidence_error(transaction_root, "下载事务日志与独占 claim 身份不一致")

    snapshots: list[
        tuple[
            _DownloadTransactionEntry,
            dict[str, object] | None,
            dict[str, object] | None,
            dict[str, object] | None,
        ]
    ] = []
    try:
        for entry in entries:
            snapshots.append(
                (
                    entry,
                    _file_transaction_signature(entry.destination),
                    _file_transaction_signature(entry.staged),
                    _file_transaction_signature(entry.backup),
                )
            )
    except TwitchDownloadError as exc:
        raise _transaction_evidence_error(transaction_root, str(exc)) from exc

    if state == "prepared":
        for entry, destination_sig, staged_sig, backup_sig in snapshots:
            old_sig = entry.old_signature
            new_sig = entry.staged_signature
            allowed_destination = (None, new_sig) if old_sig is None else (None, old_sig, new_sig)
            if destination_sig not in allowed_destination:
                raise _transaction_evidence_error(transaction_root, f"prepared 事务的 {entry.role} 目标签名不匹配")
            if staged_sig not in (None, new_sig):
                raise _transaction_evidence_error(transaction_root, f"prepared 事务的 {entry.role} 暂存签名不匹配")
            if old_sig is None:
                if backup_sig is not None:
                    raise _transaction_evidence_error(transaction_root, f"prepared 事务的 {entry.role} 出现意外备份")
            else:
                if backup_sig not in (None, old_sig):
                    raise _transaction_evidence_error(transaction_root, f"prepared 事务的 {entry.role} 备份签名不匹配")
                old_locations = int(destination_sig == old_sig) + int(backup_sig == old_sig)
                if old_locations != 1:
                    raise _transaction_evidence_error(
                        transaction_root, f"prepared 事务的 {entry.role} 旧文件状态存在歧义"
                    )
            new_locations = int(destination_sig == new_sig) + int(staged_sig == new_sig)
            if new_locations > 1:
                raise _transaction_evidence_error(transaction_root, f"prepared 事务的 {entry.role} 新文件状态存在歧义")
    else:
        for entry, destination_sig, staged_sig, backup_sig in snapshots:
            if destination_sig != entry.staged_signature or staged_sig is not None:
                raise _transaction_evidence_error(transaction_root, f"committed 事务的 {entry.role} 已发布文件不匹配")
            if entry.old_signature is None:
                if backup_sig is not None:
                    raise _transaction_evidence_error(transaction_root, f"committed 事务的 {entry.role} 出现意外备份")
            elif backup_sig not in (None, entry.old_signature):
                raise _transaction_evidence_error(transaction_root, f"committed 事务的 {entry.role} 备份签名不匹配")

    try:
        if state == "prepared":
            for entry, destination_sig, _staged_sig, _backup_sig in snapshots:
                if destination_sig == entry.staged_signature:
                    entry.destination.unlink()
            for entry, _destination_sig, _staged_sig, backup_sig in snapshots:
                if entry.old_signature is not None and backup_sig == entry.old_signature:
                    os.replace(entry.backup, entry.destination)
            for entry, _destination_sig, staged_sig, _backup_sig in snapshots:
                if staged_sig == entry.staged_signature:
                    entry.staged.unlink()
        else:
            for entry, _destination_sig, _staged_sig, backup_sig in snapshots:
                if entry.old_signature is not None and backup_sig == entry.old_signature:
                    entry.backup.unlink()
        journal.unlink()
        _sync_directory(transaction_root)
        claim.unlink()
        _sync_directory(transaction_root)
    except OSError as exc:
        raise _transaction_evidence_error(transaction_root, f"下载事务恢复未完成，可在下次运行重试: {exc}") from exc
    return state


def _publish_claimed_download_pair(
    root: Path,
    payload: dict[str, object],
    runtime_entries: list[_DownloadTransactionEntry],
    transaction_id: str,
) -> None:
    try:
        # Inputs were sampled under the guard, but recheck all of them before
        # persisting intent so manual writers cannot poison a prepared journal.
        for entry in runtime_entries:
            if _file_transaction_signature(entry.destination) != entry.old_signature:
                raise TwitchDownloadError(f"{entry.role} 目标文件在取得发布独占权后发生变化")
            if _file_transaction_signature(entry.staged) != entry.staged_signature:
                raise TwitchDownloadError(f"{entry.role} 暂存文件在取得发布独占权后发生变化")
            if _file_transaction_signature(entry.backup) is not None:
                raise TwitchDownloadError(f"{entry.role} 备份路径在取得发布独占权后已被占用")
        _atomic_write_download_transaction(root, payload)
        for entry in runtime_entries:
            if entry.old_signature is not None:
                if _file_transaction_signature(entry.destination) != entry.old_signature:
                    raise TwitchDownloadError(f"{entry.role} 目标文件在发布前发生变化")
                if _file_transaction_signature(entry.backup) is not None:
                    raise TwitchDownloadError(f"{entry.role} 备份路径在发布前已被占用")
                os.replace(entry.destination, entry.backup)
        for entry in runtime_entries:
            if _file_transaction_signature(entry.staged) != entry.staged_signature:
                raise TwitchDownloadError(f"{entry.role} 暂存文件在发布前发生变化")
            if _file_transaction_signature(entry.destination) is not None:
                raise TwitchDownloadError(f"{entry.role} 目标路径在发布前未腾空")
            os.replace(entry.staged, entry.destination)
        for entry in runtime_entries:
            if _file_transaction_signature(entry.destination) != entry.staged_signature:
                raise TwitchDownloadError(f"{entry.role} 发布后签名验证失败")
            if _file_transaction_signature(entry.staged) is not None:
                raise TwitchDownloadError(f"{entry.role} 发布后暂存文件仍然存在")
        committed = dict(payload)
        committed["state"] = "committed"
        _atomic_write_download_transaction(root, committed)
    except Exception as exc:
        try:
            recovered_state = _recover_download_transaction_locked(root, expected_transaction_id=transaction_id)
        except TwitchDownloadError as recovery_exc:
            raise _transaction_evidence_error(
                root, f"发布下载结果失败，且无法安全自动恢复 ({exc}): {recovery_exc}"
            ) from recovery_exc
        if recovered_state == "committed":
            return
        raise TwitchDownloadError(f"发布下载结果失败，旧文件已恢复: {exc}") from exc

    recovered_state = _recover_download_transaction_locked(root, expected_transaction_id=transaction_id)
    if recovered_state != "committed":
        raise _transaction_evidence_error(root, "下载事务提交后状态异常")


def publish_download_pair(
    staged_video: Path,
    video_path: Path,
    staged_chat: Path,
    chat_path: Path,
    *,
    transaction_root: Path | None = None,
) -> None:
    if transaction_root is None:
        try:
            transaction_root = Path(
                os.path.commonpath(
                    [
                        str(Path(video_path).absolute().parent),
                        str(Path(chat_path).absolute().parent),
                    ]
                )
            )
        except ValueError as exc:
            raise TwitchDownloadError("视频和聊天目标必须位于同一下载目录树") from exc
    root = _canonical_transaction_root(transaction_root)
    with _download_transaction_guard(root):
        _publish_download_pair_locked(
            staged_video,
            video_path,
            staged_chat,
            chat_path,
            transaction_root=root,
        )


def _publish_download_pair_locked(
    staged_video: Path,
    video_path: Path,
    staged_chat: Path,
    chat_path: Path,
    *,
    transaction_root: Path | None = None,
) -> None:
    """Atomically publish a validated pair with crash-recoverable intent."""
    if transaction_root is None:
        try:
            common_parent = Path(
                os.path.commonpath(
                    [
                        str(Path(video_path).absolute().parent),
                        str(Path(chat_path).absolute().parent),
                    ]
                )
            )
        except ValueError as exc:
            raise TwitchDownloadError("视频和聊天目标必须位于同一下载目录树") from exc
        transaction_root = common_parent
    root = _canonical_transaction_root(transaction_root)
    journal = _download_transaction_journal_path(root)
    if journal.exists() or journal.is_symlink():
        raise _transaction_evidence_error(root, "存在尚未恢复的下载事务，拒绝覆盖")

    raw_pairs = (
        ("video", Path(staged_video), Path(video_path)),
        ("chat", Path(staged_chat), Path(chat_path)),
    )
    canonical_pairs: list[tuple[str, str, Path, str, Path]] = []
    unique_paths: set[Path] = set()
    for role, raw_staged, raw_destination in raw_pairs:
        staged_relative, staged = _canonical_transaction_member(root, raw_staged, label=f"{role} 暂存文件")
        destination_relative, destination = _canonical_transaction_member(
            root, raw_destination, label=f"{role} 目标文件"
        )
        for candidate in (staged, destination):
            if candidate in unique_paths:
                raise TwitchDownloadError("下载事务的暂存和目标路径不能重复")
            unique_paths.add(candidate)
        canonical_pairs.append((role, staged_relative, staged, destination_relative, destination))

    payload_entries: list[dict[str, object]] = []
    runtime_entries: list[_DownloadTransactionEntry] = []
    for role, staged_relative, staged, destination_relative, destination in canonical_pairs:
        staged_signature = _file_transaction_signature(staged)
        if staged_signature is None:
            raise TwitchDownloadError(f"待发布的 {role} 暂存文件不存在: {staged}")
        old_signature = _file_transaction_signature(destination)
        if old_signature == staged_signature:
            raise TwitchDownloadError(f"{role} 的新旧文件签名不可区分，拒绝发布")
        while True:
            backup = destination.with_name(f".{destination.name}.backup-{uuid.uuid4().hex}")
            if not backup.exists() and not backup.is_symlink():
                break
        backup_relative, backup = _canonical_transaction_member(root, backup, label=f"{role} 备份文件")
        entry = _DownloadTransactionEntry(
            role=role,
            destination=destination,
            staged=staged,
            backup=backup,
            old_signature=old_signature,
            staged_signature=staged_signature,
        )
        runtime_entries.append(entry)
        payload_entries.append(
            {
                "role": role,
                "destination": destination_relative,
                "staged": staged_relative,
                "backup": backup_relative,
                "old_signature": old_signature,
                "staged_signature": staged_signature,
            }
        )

    transaction_id = uuid.uuid4().hex
    owner_pid = os.getpid()
    created_ns = time.time_ns()
    payload: dict[str, object] = {
        "version": _DOWNLOAD_TRANSACTION_VERSION,
        "transaction_id": transaction_id,
        "owner_pid": owner_pid,
        "created_ns": created_ns,
        "state": "prepared",
        "entries": payload_entries,
    }
    _claim_download_transaction(root, transaction_id, owner_pid, created_ns)
    try:
        _publish_claimed_download_pair(
            root,
            payload,
            runtime_entries,
            transaction_id,
        )
    finally:
        _unregister_active_download_transaction(transaction_id)


def resolve_download_targets(
    root: Path,
    video_path: Path,
    chat_path: Path,
) -> tuple[Path, Path, Path]:
    """Validate and canonicalize the transaction root and output pair."""
    transaction_root = _canonical_transaction_root(root)
    _video_relative, resolved_video = _canonical_transaction_member(transaction_root, video_path, label="视频目标文件")
    _chat_relative, resolved_chat = _canonical_transaction_member(transaction_root, chat_path, label="聊天目标文件")
    if resolved_video == resolved_chat:
        raise TwitchDownloadError("视频和聊天下载目标不能是同一个文件")
    return transaction_root, resolved_video, resolved_chat


def preserved_staged_paths(root: Path) -> set[Path] | None:
    """Return staged files referenced by valid evidence; None means preserve all."""
    try:
        transaction_root = _canonical_transaction_root(root)
        journal = _download_transaction_journal_path(transaction_root)
        if not (journal.exists() or journal.is_symlink()):
            return set()
        with _download_transaction_guard(transaction_root):
            if not (journal.exists() or journal.is_symlink()):
                return set()
            _state, _txid, _pid, _created, entries = _load_download_transaction(transaction_root)
            return {entry.staged for entry in entries}
    except TwitchDownloadError:
        return None


# Private compatibility aliases for focused fault-injection tests.
_recover_download_transaction = recover_download_transaction
_publish_download_pair = publish_download_pair


__all__ = [
    "preserved_staged_paths",
    "publish_download_pair",
    "recover_download_transaction",
    "resolve_download_targets",
]
