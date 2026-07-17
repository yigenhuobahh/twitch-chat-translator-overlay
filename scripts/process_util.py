#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Subprocess helpers: tracked FFmpeg runs and Windows process-tree cleanup."""

from __future__ import annotations

import atexit
from collections.abc import Sequence
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
from typing import IO

from common_utils import require_executable

_lock = threading.Lock()
_active: list[subprocess.Popen] = []
_handlers_installed = False

# OS system-path denylist pieces for is_dangerous_publish_path (normalized / lowercased).
_WIN_DANGEROUS_ROOTS = (
    r"[a-z]:/windows",
    r"[a-z]:/program files",
    r"[a-z]:/program files \(x86\)",
    r"[a-z]:/programdata",
    r"[a-z]:/users/public",
    r"[a-z]:/system volume information",
    r"[a-z]:/\$recycle\.bin",
    # Bare drive\System32 (legacy preview denylist also matched this shape).
    r"[a-z]:/system32",
    r"[a-z]:/syswow64",
)
_UNIX_DANGEROUS_PREFIXES = (
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
    "/root",
    "/lib",
    "/lib64",
    "/lib32",
    "/run",
    "/var/run",
    "/var/lib",
    "/system",  # macOS
    "/library",  # macOS system Library
    "/private/etc",
    "/private/var/db",
)


def _is_windows() -> bool:
    return os.name == "nt" or sys.platform.startswith("win")


def _popen_kwargs() -> dict:
    """Start child in a new process group/session so the whole tree can be killed."""
    if _is_windows():
        # CREATE_NEW_PROCESS_GROUP lets us target the child tree via taskkill /T.
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return {"creationflags": flags}
    return {"start_new_session": True}


def _register(proc: subprocess.Popen) -> None:
    with _lock:
        _active.append(proc)


def _unregister(proc: subprocess.Popen) -> None:
    with _lock:
        try:
            _active.remove(proc)
        except ValueError:
            pass


def kill_process_tree(pid: int, force: bool = True) -> None:
    """Kill a process and its descendants when possible."""
    if pid <= 0:
        return
    try:
        if _is_windows():
            cmd = [require_executable("taskkill.exe"), "/T", "/PID", str(pid)]
            if force:
                cmd.insert(1, "/F")
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            try:
                pgid = os.getpgid(pid)
            except OSError:
                pgid = None
            sig = signal.SIGKILL if force else signal.SIGTERM
            if pgid is not None:
                try:
                    os.killpg(pgid, sig)
                    return
                except OSError:
                    pass
            try:
                os.kill(pid, sig)
            except OSError:
                pass
    except Exception:
        # Best-effort cleanup only.
        pass


def kill_active_processes(force: bool = True) -> int:
    """Kill all still-tracked child processes. Returns how many were targeted."""
    with _lock:
        procs = list(_active)
    count = 0
    for proc in procs:
        if proc.poll() is None:
            kill_process_tree(proc.pid, force=force)
            count += 1
        _unregister(proc)
    return count


def _signal_handler(signum, frame):
    kill_active_processes(force=True)
    # Re-raise default behavior after cleanup.
    signal.signal(signum, signal.SIG_DFL)
    try:
        os.kill(os.getpid(), signum)
    except OSError:
        raise SystemExit(128 + int(signum))


def install_process_cleanup_handlers() -> None:
    """Install once-per-process SIGINT/SIGTERM/atexit cleanup hooks."""
    global _handlers_installed
    if _handlers_installed:
        return
    _handlers_installed = True
    atexit.register(kill_active_processes)
    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _signal_handler)
        except (ValueError, OSError):
            # Not allowed in non-main threads or unsupported signals.
            pass


def run_tracked(
    cmd: Sequence[str],
    *,
    stdout: int | IO | None = subprocess.DEVNULL,
    stderr: int | IO | None = subprocess.DEVNULL,
    text: bool = False,
    cwd: str | Path | None = None,
    env: dict | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """
    Run a command while tracking the process for cancel/atexit cleanup.

    Prefer this for long-running FFmpeg/FFprobe-adjacent encode steps.
    """
    install_process_cleanup_handlers()
    kwargs = _popen_kwargs()
    proc = subprocess.Popen(
        list(cmd),
        stdout=stdout,
        stderr=stderr,
        text=text,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        **kwargs,
    )
    _register(proc)
    try:
        out, err = proc.communicate()
        completed = subprocess.CompletedProcess(list(cmd), proc.returncode, out, err)
        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, list(cmd), out, err)
        return completed
    finally:
        _unregister(proc)
        if proc.poll() is None:
            kill_process_tree(proc.pid, force=True)


JOB_DIR_MARKER = ".twitch_overlay_job"
_WINDOWS_REPARSE_POINT = 0x400


def _is_link_or_reparse_point(path: str | Path) -> bool:
    """Refuse filesystem indirections before scanning or deleting entries."""
    try:
        if os.path.islink(path) or Path(path).is_symlink():
            return True
        attrs = int(getattr(os.lstat(path), "st_file_attributes", 0) or 0)
        return bool(attrs & _WINDOWS_REPARSE_POINT)
    except OSError:
        return True


def make_job_dir(parent: str | Path, prefix: str = "job_") -> Path:
    """Create a unique per-run working directory under parent with a tool marker."""
    import time
    import uuid

    parent_path = Path(parent)
    parent_path.mkdir(parents=True, exist_ok=True)
    name = f"{prefix}{int(time.time())}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    job_dir = parent_path / name
    job_dir.mkdir(parents=True, exist_ok=False)
    try:
        (job_dir / JOB_DIR_MARKER).write_text("twitch-chat-cn-overlay\n", encoding="utf-8")
    except OSError:
        pass
    return job_dir


def is_tool_job_dir(path: str | Path, prefix: str = "job_") -> bool:
    """True if path is a make_job_dir product (requires tool marker or strict name).

    Strict legacy name: job_<digits>_<digits>_<hex8> (timestamp_pid_uuid-prefix).
    Loose names like job_backup_final are *not* treated as tool jobs.
    """
    p = Path(path)
    if _is_link_or_reparse_point(p):
        return False
    if not p.is_dir():
        return False
    if not p.name.startswith(prefix):
        return False
    marker = p / JOB_DIR_MARKER
    if marker.is_file():
        return True
    # Strict legacy: job_<timestamp>_<pid>_<8hex>
    rest = p.name[len(prefix):]
    import re as _re
    return bool(_re.fullmatch(r"\d+_\d+_[0-9a-fA-F]{6,16}", rest))


def path_is_under(child: str | Path, parent: str | Path) -> bool:
    """Return True if resolved child is under resolved parent (or equal)."""
    try:
        child_p = Path(child).resolve()
        parent_p = Path(parent).resolve()
        if hasattr(child_p, "is_relative_to"):
            return child_p == parent_p or child_p.is_relative_to(parent_p)
        return os.path.commonpath([str(child_p), str(parent_p)]) == str(parent_p)
    except (OSError, ValueError):
        return False


def _strip_win_extended_prefix(s: str) -> str:
    """Normalize ``\\\\?\\C:\\...`` / ``//?/C:/...`` to ``c:/...`` for denylist match."""
    t = str(s).replace("\\", "/")
    # \\?\C:\Windows → //?/C:/Windows ; also tolerate /?/C:/...
    t = re.sub(r"^//[?.]/", "", t, count=1, flags=re.I)
    t = re.sub(r"^/[?.]/", "", t, count=1, flags=re.I)
    return t.lower().rstrip("/")


def _normalize_path_for_policy(path: str | Path) -> str:
    """Absolute path with forward slashes, lowercased (for denylist matching)."""
    # Strip extended prefixes before resolve — resolve("\\\\?\\C:\\...") can
    # produce unusable forms on some Python/Windows combos.
    stripped = _strip_win_extended_prefix(str(path))
    try:
        hydrate = stripped
        if re.match(r"^[a-z]:/", hydrate):
            hydrate = hydrate[0:2] + "\\" + hydrate[3:].replace("/", "\\")
        resolved = Path(hydrate).resolve()
        return _strip_win_extended_prefix(str(resolved))
    except OSError:
        try:
            return _strip_win_extended_prefix(os.path.abspath(stripped))
        except OSError:
            return stripped


def is_dangerous_publish_path(path: str | Path) -> bool:
    """True if path is under an OS system directory the tool must not write/delete.

    Used for --preview-image publish copies and for refusing dangerous --out-dir /
    --job-dir roots. Cross-platform: Windows system roots + Unix/macOS system trees.
    Non-existent paths are still evaluated via abspath/resolve.

    On Windows, POSIX-looking absolute paths like ``/etc/passwd`` are checked
    against the *original* string as well: ``Path.resolve()`` would otherwise
    rewrite them to a drive-letter path and miss the denylist.
    """
    raw = _strip_win_extended_prefix(str(path))
    try:
        s = _normalize_path_for_policy(path)
    except Exception:
        return True  # fail closed

    candidates = {s, raw}
    # Drive-stripped form so /etc still matches after resolve() -> C:/etc on Windows.
    for c in list(candidates):
        m = re.match(r"^[a-z]:(/.*)$", c)
        if m:
            candidates.add(m.group(1))

    for cand in candidates:
        if not cand or cand == "/":
            return True
        if re.fullmatch(r"[a-z]:/?", cand):
            return True
        for root in _WIN_DANGEROUS_ROOTS:
            if re.match(root + r"(/|$)", cand):
                return True
        for pref in _UNIX_DANGEROUS_PREFIXES:
            if cand == pref or cand.startswith(pref + "/"):
                return True
    return False


def clean_companion_flags_error(args) -> str | None:
    """If --clean-all / --clean-progress used without --clean, return an error message."""
    if getattr(args, "clean", False):
        return None
    if getattr(args, "clean_all", False):
        return "错误: --clean-all 必须与 --clean 联用（例如 --clean --clean-all）"
    if getattr(args, "clean_progress", False):
        return "错误: --clean-progress 必须与 --clean 联用（例如 --clean --clean-progress）"
    return None


def _dir_size_bytes(path: str | Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _is_partial_artifact(name: str) -> bool:
    lower = name.lower()
    if lower.endswith(".partial.mp4") or lower.endswith(".mp4.partial"):
        return True
    # Exact temp suffix used by some paths; avoid bare ".partial" collateral.
    if lower.endswith(".partial") and (".mp4" in lower or lower.endswith(".webm.partial")):
        return True
    return False


def _is_live_tool_job(path: str | Path) -> bool:
    """True if run_meta.json reports this tool job as still running.

    Uses pid + updated_at staleness (see run_meta.is_live_run_meta).
    Fail closed: unreadable/corrupt meta is treated as live so clean will skip it.
    """
    from run_meta import is_live_run_meta, run_meta_path

    meta = run_meta_path(path)
    if not meta.is_file():
        return False
    try:
        data = json.loads(meta.read_text(encoding="utf-8")) or {}
    except Exception:
        return True
    return is_live_run_meta(data if isinstance(data, dict) else None)


def _marked_tool_dir_kind(name: str, path: str) -> str | None:
    """Return 'job' / 'batch' if path is a marked tool dir with that prefix, else None."""
    if name.startswith("job_") and is_tool_job_dir(path, prefix="job_"):
        return "job"
    if name.startswith("batch_") and is_tool_job_dir(path, prefix="batch_"):
        return "batch"
    return None


def clean_temp_artifacts(
    out_base: str | Path,
    *,
    clean_progress: bool = False,
    scan_one_level: bool = True,
    clean_all: bool = False,
    only_job_dir: str | Path | None = None,
) -> tuple[int, int]:
    """Remove tool temp dirs/files under out_base.

    Scope:
      - Always eligible: *.partial.mp4 / *.mp4.partial (and optional *.progress.json)
      - job_*/batch_* tool dirs:
          * only_job_dir set → only that one directory (must be under out_base)
          * clean_all=True → all finished marked tool job/batch dirs (legacy bulk clean)
          * otherwise → job dirs are left alone (safer default)

    Live jobs (run_meta status running/in_progress/started) are never removed.
    Symlinks/junctions are never rmtree'd.

    Returns (count, freed_bytes).
    """
    out_base = os.path.abspath(str(out_base))
    if not os.path.isdir(out_base):
        return 0, 0

    only_abs: str | None = None
    if only_job_dir is not None and str(only_job_dir).strip():
        only_abs = os.path.abspath(str(only_job_dir).strip())
        if not path_is_under(only_abs, out_base):
            print(
                f"  [clean] skip job-dir outside out-dir:\n"
                f"    job-dir: {only_abs}\n"
                f"    out-dir: {out_base}"
            )
            only_abs = None
        elif not os.path.isdir(only_abs):
            print(f"  [clean] job-dir 不存在: {only_abs}")
            only_abs = None

    # only_job_dir scopes to one dir even when clean_all=True (CLI may pass both).
    remove_job_dirs = bool(clean_all or only_abs)

    def _should_remove_dir(name: str, path: str) -> bool:
        if not remove_job_dirs:
            return False
        # Cheap name filter first (bulk mode); only_job_dir still needs exact path match.
        if only_abs is None and not (name.startswith("job_") or name.startswith("batch_")):
            return False
        # Refuse to rmtree symlinks/junctions (target may be outside out_base).
        if _is_link_or_reparse_point(path):
            return False
        path_abs = os.path.abspath(path)
        if only_abs is not None:
            if path_abs != only_abs:
                return False
            kind = _marked_tool_dir_kind(name, path)
            if kind is None:
                print(f"  [clean] skip non-tool job-dir: {name}")
                return False
            if _is_live_tool_job(path):
                print(f"  [clean] skip live {kind}: {name}")
                return False
            return True
        kind = _marked_tool_dir_kind(name, path)
        if kind is None:
            return False
        if _is_live_tool_job(path):
            print(f"  [clean] skip live {kind}: {name}")
            return False
        return True

    def _should_remove_file(name: str) -> bool:
        if _is_partial_artifact(name):
            return True
        if clean_progress and name.lower().endswith(".progress.json"):
            return True
        return False

    freed = 0
    count = 0
    skipped_jobs = 0

    def _clean_entry(entry_path: str, label: str) -> None:
        nonlocal freed, count, skipped_jobs
        name = os.path.basename(entry_path)
        try:
            if _is_link_or_reparse_point(entry_path):
                return
            if os.path.isdir(entry_path):
                # Default clean keeps jobs: count by name prefix only (no marker I/O).
                if not remove_job_dirs and (name.startswith("job_") or name.startswith("batch_")):
                    skipped_jobs += 1
                if _should_remove_dir(name, entry_path):
                    size = _dir_size_bytes(entry_path)
                    try:
                        shutil.rmtree(entry_path)
                    except OSError as e:
                        print(f"  [clean] 删除失败 {label}: {e}")
                        return
                    if os.path.exists(entry_path):
                        print(f"  [clean] 删除未完成 {label}（路径仍存在）")
                        return
                    freed += size
                    count += 1
                    print(f"  [clean] {label} ({size / (1024 * 1024):.1f} MB)")
            elif os.path.isfile(entry_path) and _should_remove_file(name):
                size = os.path.getsize(entry_path)
                os.remove(entry_path)
                freed += size
                count += 1
                print(f"  [clean] {label} ({size / (1024 * 1024):.1f} MB)")
        except OSError as e:
            print(f"  [clean] 跳过 {label}: {e}")

    # Single-job mode: delete that directory directly (may live one level down).
    if only_abs is not None and os.path.isdir(only_abs):
        try:
            label = os.path.relpath(only_abs, out_base)
        except ValueError:
            label = only_abs
        _clean_entry(only_abs, label)

    for entry in os.listdir(out_base):
        entry_path = os.path.join(out_base, entry)
        if only_abs is not None and os.path.abspath(entry_path) == only_abs:
            continue  # already handled
        _clean_entry(entry_path, entry)

    if scan_one_level and only_abs is None:
        for subdir in list(os.listdir(out_base)):
            subdir_path = os.path.join(out_base, subdir)
            if _is_link_or_reparse_point(subdir_path) or not os.path.isdir(subdir_path):
                continue
            # Do not recurse into leftover job/batch dirs already removed above.
            if subdir.startswith("job_") or subdir.startswith("batch_"):
                continue
            try:
                for entry in os.listdir(subdir_path):
                    entry_path = os.path.join(subdir_path, entry)
                    _clean_entry(entry_path, f"{subdir}/{entry}")
            except OSError:
                pass

    if skipped_jobs and not clean_all and only_abs is None:
        print(
            f"  [clean] 保留了 {skipped_jobs} 个 job_/batch_ 目录；"
            f"删除全部已结束任务请加 --clean-all，或指定 --job-dir 只清一个"
        )

    return count, freed
