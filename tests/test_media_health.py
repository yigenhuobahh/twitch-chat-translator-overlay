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
