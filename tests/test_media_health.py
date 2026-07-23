#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for bounded media-health checks."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _probe_payload(*, duration: float = 600.0, extras: list[str] | None = None) -> str:
    streams = [
        {
            "codec_type": "video",
            "width": 1920,
            "height": 1080,
            "start_time": "0",
            "r_frame_rate": "60/1",
            "avg_frame_rate": "60/1",
        },
        {"codec_type": "audio", "start_time": "0"},
    ]
    streams.extend({"codec_type": kind} for kind in extras or [])
    return json.dumps({"format": {"duration": str(duration)}, "streams": streams})


def test_data_stream_is_warning_not_failure(monkeypatch, tmp_path: Path):
    import media_health as mh

    source = tmp_path / "vod.mp4"
    source.write_bytes(b"x")
    monkeypatch.setattr(mh, "safe_which", lambda name: name)
    monkeypatch.setattr(mh, "require_executable", lambda name: name)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "-show_packets" in cmd:
            return SimpleNamespace(returncode=0, stdout="0.021333\n", stderr="")
        return SimpleNamespace(returncode=0, stdout=_probe_payload(extras=["data"]), stderr="")

    monkeypatch.setattr(mh.subprocess, "run", fake_run)
    health = mh.probe_media_health(source)

    assert health.ok
    assert health.extra_streams == ["data"]
    assert health.warnings == ["保留附加流: data"]
    assert any("-show_packets" in call for call in calls)


def test_packet_check_samples_first_and_last_minute(monkeypatch, tmp_path: Path):
    import media_health as mh

    source = tmp_path / "long.mp4"
    source.write_bytes(b"x")
    monkeypatch.setattr(mh, "safe_which", lambda name: name)
    monkeypatch.setattr(mh, "require_executable", lambda name: name)
    intervals = []

    def fake_run(cmd, **kwargs):
        if "-show_packets" in cmd:
            intervals.append(cmd[cmd.index("-read_intervals") + 1])
            return SimpleNamespace(returncode=0, stdout="0.021333\n", stderr="")
        return SimpleNamespace(returncode=0, stdout=_probe_payload(duration=600.0), stderr="")

    monkeypatch.setattr(mh.subprocess, "run", fake_run)
    health = mh.probe_media_health(source)

    assert health.ok
    assert intervals == ["0%+60", "540.000%+60"]


def test_decode_check_emits_percentage_events_for_tui(monkeypatch, tmp_path: Path):
    import media_health as mh

    source = tmp_path / "vod.mp4"
    source.write_bytes(b"x")
    monkeypatch.setenv(mh.EVENT_FILE_ENV, str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(mh, "safe_which", lambda _name: "ffmpeg")
    monkeypatch.setattr(mh, "require_executable", lambda name: name)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(mh, "emit_task_event", lambda kind, **fields: events.append((kind, fields)))

    class FakeStdout:
        def __init__(self):
            self.lines = iter(("out_time_ms=5000000\n", "progress=end\n"))

        def readline(self, _size=-1):
            return next(self.lines, "")

    class FakeProcess:
        def __init__(self):
            self.stdout = FakeStdout()
            self.returncode = 0

        def poll(self):
            return self.returncode

        def wait(self):
            return self.returncode

        def kill(self):
            self.returncode = 1

    fake = FakeProcess()
    monkeypatch.setattr(mh.subprocess, "Popen", lambda *args, **kwargs: fake)

    assert mh.decode_check_media(source, duration=10) == (True, "")
    assert ("stage_progress", {"stage": "media_decode", "completed": 50, "total": 100}) in events
    assert events[-1] == ("stage_completed", {"stage": "media_decode", "completed": 100, "total": 100})


def test_progress_decode_reports_nonzero_exit_and_bounded_error(monkeypatch, tmp_path: Path):
    import media_health as mh

    source = tmp_path / "bad.mp4"
    source.write_bytes(b"x")
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(mh, "require_executable", lambda name: name)
    monkeypatch.setattr(mh, "emit_task_event", lambda kind, **fields: events.append((kind, fields)))

    class FakeStdout:
        def __init__(self):
            self.lines = iter(f"decode error {index}\n" for index in range(40))

        def readline(self, _size=-1):
            return next(self.lines, "")

    class FakeProcess:
        stdout = FakeStdout()

        def poll(self):
            return 1

        def wait(self):
            return 1

    monkeypatch.setattr(mh.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    ok, reason = mh._decode_check_with_progress(source, duration=10)

    assert ok is False
    assert "decode error 39" in reason
    assert "decode error 0" not in reason
    assert events[-1][0] == "stage_failed"


def test_progress_decode_handles_spawn_failure(monkeypatch, tmp_path: Path):
    import media_health as mh

    monkeypatch.setattr(mh, "require_executable", lambda name: name)
    monkeypatch.setattr(mh.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("blocked")))

    assert mh._decode_check_with_progress(tmp_path / "video.mp4", duration=1) == (
        False,
        "Full decode check failed: blocked",
    )


def test_progress_decode_handles_executable_resolution_failure(monkeypatch, tmp_path: Path):
    import media_health as mh

    monkeypatch.setattr(mh, "require_executable", lambda _name: (_ for _ in ()).throw(FileNotFoundError("missing")))

    assert mh._decode_check_with_progress(tmp_path / "video.mp4", duration=1) == (
        False,
        "Full decode check failed: missing",
    )


def test_progress_decode_timeout_kills_and_waits_for_ffmpeg(monkeypatch, tmp_path: Path):
    import threading

    import media_health as mh

    released = threading.Event()
    monotonic = iter((0.0, 24 * 3600 + 1.0))
    monkeypatch.setattr(mh.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(mh, "require_executable", lambda name: name)
    monkeypatch.setattr(mh, "emit_task_event", lambda *args, **kwargs: True)

    class BlockingStdout:
        def readline(self, _size=-1):
            released.wait(timeout=2)
            return ""

    class FakeProcess:
        def __init__(self):
            self.stdout = BlockingStdout()
            self.returncode = None
            self.killed = False
            self.waited = False

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = 1
            released.set()

        def wait(self):
            self.waited = True
            return self.returncode

    process = FakeProcess()
    monkeypatch.setattr(mh.subprocess, "Popen", lambda *args, **kwargs: process)

    assert mh._decode_check_with_progress(tmp_path / "video.mp4", duration=1) == (
        False,
        "Full decode check timed out",
    )
    assert process.killed is True and process.waited is True


def test_decode_mode_marks_a_file_unhealthy_when_full_decode_fails(monkeypatch, tmp_path: Path):
    import media_health as mh

    source = tmp_path / "vod.mp4"
    source.write_bytes(b"x")
    monkeypatch.setattr(mh, "probe_media_health", lambda *args, **kwargs: mh.MediaHealth(path=source, ok=True))
    monkeypatch.setattr(mh, "decode_check_media", lambda _path, **_kwargs: (False, "corrupt packet"))

    health = mh.validate_media_health(source, mode="decode", require_audio=False)

    assert not health.ok
    assert any("完整解码失败" in issue for issue in health.issues)
