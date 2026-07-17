#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Silent-wrong edge regressions for validate / offset / window (Batch A3)."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from helpers import load_module


def test_validate_rejects_missing_audio_when_required(make_test_video, tmp_path: Path):
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    # Video-only file
    out = tmp_path / "video_only.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=320x180:r=30:d=2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(out),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok, summary, reason = burn.validate_rendered_output(
        str(out), expected_duration=1.5, require_audio=True
    )
    assert not ok
    assert "audio" in reason.lower()
    assert summary.get("has_video") is True
    assert summary.get("has_audio") is False


def test_validate_accepts_duration_within_tolerance(make_test_video):
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    video = make_test_video(duration=2.0, fps=30)
    # expected slightly longer than actual but within default 0.35s tolerance
    ok, summary, reason = burn.validate_rendered_output(
        str(video), expected_duration=2.2, require_audio=True, duration_tolerance=0.35
    )
    assert ok, reason
    assert summary["has_video"] and summary["has_audio"]


def test_validate_rejects_corrupt_or_missing_file(tmp_path: Path):
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    missing = tmp_path / "nope.mp4"
    ok, summary, reason = burn.validate_rendered_output(
        str(missing), expected_duration=1.0, require_audio=False
    )
    assert not ok
    assert reason


def test_offset_auto_not_triggered_when_span_mismatches_video():
    """first_ts beyond duration but span does not match -> no silent auto offset."""
    cw = load_module("chat_window", "chat_window.py")
    messages = [
        {"timestamp": 5000.0},
        {"timestamp": 5200.0},  # span 200s, video 100s -> should NOT auto
    ]
    diag = cw.compute_time_offset(messages, video_duration=100.0, manual_offset=None)
    assert diag["mode"] != "auto"
    assert diag["offset"] == 0.0
    assert diag["warnings"]  # should warn user instead of silently shifting


def test_offset_auto_triggered_when_vod_absolute_timestamps():
    cw = load_module("chat_window", "chat_window.py")
    messages = [
        {"timestamp": 3600.0},
        {"timestamp": 3650.0},
        {"timestamp": 3690.0},  # span 90s ~ video 100s
    ]
    diag = cw.compute_time_offset(messages, video_duration=100.0, manual_offset=None)
    assert diag["mode"] == "auto"
    assert diag["offset"] == pytest.approx(3600.0)
    assert diag["confirm_with_preview"] is True


def test_manual_offset_overrides_auto():
    cw = load_module("chat_window", "chat_window.py")
    messages = [{"timestamp": 3600.0}, {"timestamp": 3650.0}]
    diag = cw.compute_time_offset(messages, video_duration=100.0, manual_offset=12.5)
    assert diag["mode"] == "manual"
    assert diag["offset"] == pytest.approx(12.5)
    assert diag["auto_detected"] is False


def test_apply_time_offset_clamps_negative():
    cw = load_module("chat_window", "chat_window.py")
    messages = [{"timestamp": 5.0}, {"timestamp": 20.0}]
    out = cw.apply_time_offset(messages, offset=10.0)
    assert out[0]["timestamp"] == 0.0
    assert out[1]["timestamp"] == 10.0


def test_filter_full_window_none_is_passthrough():
    cw = load_module("chat_window", "chat_window.py")
    chat = {
        "messages": [{"timestamp": 1.0, "fragments": []}],
        "emote_map": {"first-1": "a.png"},
        "extra": 1,
    }
    filtered = cw.filter_chat_for_time_window(chat, None, None, 14.0)
    assert filtered is chat  # no copy / no filter when window open
