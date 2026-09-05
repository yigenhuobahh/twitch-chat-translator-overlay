#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Concurrency-10 regression tests: media_health decode-check children must be
tracked in process_util's registry so cancel/atexit cleanup can kill them.

Both decode paths are exercised with a real long-running child (``sys
.executable`` sleeping), which keeps the tests independent of ffmpeg:

- ``decode_check_media`` non-progress path must route through
  ``process_util.run_tracked`` (child visible in ``_active`` while running,
  and terminated by ``kill_active_processes``).
- ``_decode_check_with_progress`` must spawn its Popen with
  ``process_util.popen_kwargs()`` and wrap it in
  ``process_util.tracked_process`` (child visible in ``_active`` while
  running, and terminated by ``kill_active_processes``).
- ``run_tracked(timeout=...)`` must kill the child and raise
  ``TimeoutExpired`` (``subprocess.run`` parity, no orphan left behind).
- ``tracked_process`` registers/unregisters an externally created Popen.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import process_util

SLEEP_CODE = "import time; time.sleep(120)"


def _registry_pids() -> set[int]:
    with process_util._lock:
        return {proc.pid for proc in process_util._active}


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    process_util.kill_active_processes(force=True)


def _sleep_substituting_popen(argv_marker: list[str]):
    """Return a subprocess.Popen spy that swaps the ffmpeg argv for a sleeper."""
    real_popen = subprocess.Popen

    def spying_popen(args, *pargs, **pkwargs):
        if isinstance(args, list) and args[: len(argv_marker)] == argv_marker:
            args = [sys.executable, "-c", SLEEP_CODE]
        return real_popen(args, *pargs, **pkwargs)

    return spying_popen


def test_decode_check_media_child_is_tracked_and_killable(tmp_path: Path, monkeypatch):
    """Non-progress decode check: child lands in _active, kill works."""
    import media_health as mh

    video = tmp_path / "vod.mp4"
    video.write_bytes(b"x")
    monkeypatch.delenv(mh.EVENT_FILE_ENV, raising=False)  # force non-progress path
    monkeypatch.setattr(mh, "safe_which", lambda _name: "ffmpeg")
    monkeypatch.setattr(mh, "require_executable", lambda _name: sys.executable)
    monkeypatch.setattr(
        subprocess, "Popen", _sleep_substituting_popen([sys.executable, "-v", "error"])
    )

    result: list[tuple[bool, str]] = []
    worker = threading.Thread(
        target=lambda: result.append(mh.decode_check_media(video)), daemon=True
    )
    worker.start()

    assert _wait_until(lambda: bool(_registry_pids())), (
        "decode_check_media child never appeared in process_util._active"
    )

    targeted = process_util.kill_active_processes(force=True)
    assert targeted >= 1
    worker.join(timeout=10)
    assert not worker.is_alive(), "decode_check_media did not return after kill"
    assert result
    ok, _reason = result[0]
    assert ok is False  # the killed child can never report success


def test_progress_decode_child_is_tracked_and_killable(tmp_path: Path, monkeypatch):
    """Progress decode check: Popen uses popen_kwargs() and is in _active."""
    import media_health as mh

    video = tmp_path / "vod.mp4"
    video.write_bytes(b"x")
    monkeypatch.setenv(mh.EVENT_FILE_ENV, str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(mh, "safe_which", lambda _name: "ffmpeg")
    monkeypatch.setattr(mh, "require_executable", lambda _name: sys.executable)

    real_popen = subprocess.Popen
    observed: dict[str, object] = {}

    def spying_popen(args, *pargs, **pkwargs):
        if isinstance(args, list) and "-progress" in args:
            # media_health must apply process_util.popen_kwargs() so the whole
            # child tree stays killable (creationflags on Windows /
            # start_new_session on POSIX).
            expected = process_util.popen_kwargs()
            observed["popen_kwargs_match"] = all(
                pkwargs.get(key) == value for key, value in expected.items()
            )
            args = [sys.executable, "-c", SLEEP_CODE]
        return real_popen(args, *pargs, **pkwargs)

    monkeypatch.setattr(subprocess, "Popen", spying_popen)

    result: list[tuple[bool, str]] = []
    worker = threading.Thread(
        target=lambda: result.append(mh._decode_check_with_progress(video, duration=10)),
        daemon=True,
    )
    worker.start()

    assert _wait_until(lambda: bool(_registry_pids())), (
        "progress-decode child never appeared in process_util._active"
    )
    assert observed.get("popen_kwargs_match") is True

    targeted = process_util.kill_active_processes(force=True)
    assert targeted >= 1
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert result
    ok, _reason = result[0]
    assert ok is False


def test_run_tracked_timeout_kills_child_and_raises():
    """run_tracked(timeout=...) mirrors subprocess.run: kill then TimeoutExpired."""
    cmd = [sys.executable, "-c", SLEEP_CODE]
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        process_util.run_tracked(cmd, timeout=0.5)
    elapsed = time.monotonic() - start
    assert elapsed < 30  # killed promptly, not after the full 120 s sleep
    assert _registry_pids() == set()  # nothing left registered


def test_tracked_process_registers_and_unregisters():
    """tracked_process exposes the child in _active and cleans up on exit."""
    proc = subprocess.Popen(
        [sys.executable, "-c", SLEEP_CODE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **process_util.popen_kwargs(),
    )
    try:
        with process_util.tracked_process(proc):
            assert proc.pid in _registry_pids()
        assert proc.pid not in _registry_pids()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
