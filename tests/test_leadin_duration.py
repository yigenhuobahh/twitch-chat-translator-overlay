#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression: lead-in must not false-fail ~source-length encodes."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from helpers import load_module


def _make_start_offset_video(out_path: Path, content_s: float = 3.0, lead_in: float = 1.0, fps: int = 30) -> Path:
    """
    Build MP4 similar to real VODs: audio from 0, video stream start_time≈lead_in.

    Uses -itsoffset on the video input so ffprobe reports video start_time > 0
    while format duration stays ~ content_s + lead_in or ~content depending on mux.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Generate plain A/V then remux with video delay via itsoffset.
    # Simpler reliable approach used by many tools:
    #   ffmpeg -i video -i audio -itsoffset LEAD -i video -map delayed_v -map a
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={content_s + lead_in}",
        "-f", "lavfi", "-itsoffset", str(lead_in),
        "-i", f"color=c=black:s=320x180:r={fps}:d={content_s}",
        "-map", "1:v:0", "-map", "0:a:0",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-t", str(content_s + lead_in),
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert out_path.is_file()
    return out_path


def test_expected_compose_duration_ignores_lead_in_padding():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    assert burn.expected_compose_duration(374.03, 1.0) == pytest.approx(374.03)
    assert burn.expected_compose_duration(15.0, 0.0) == pytest.approx(15.0)
    assert burn.expected_compose_duration(10.0, 2.5) == pytest.approx(10.0)


def test_validate_accepts_source_length_when_expected_is_source_not_source_plus_leadin(
    make_test_video,
):
    """The Fontinalia false-fail: actual≈374, bad expected was 375.03."""
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    video = make_test_video(duration=3.0, fps=30)
    # Correct policy: expected = source/render length
    ok, summary, reason = burn.validate_rendered_output(
        str(video),
        expected_duration=3.0,
        require_audio=True,
        duration_tolerance=0.35,
        min_duration=3.0 - 1.0,  # allow losing at most lead-in
    )
    assert ok, reason
    # Old wrong policy would demand 4.0 and fail a complete 3.0s file
    ok_bad, _, reason_bad = burn.validate_rendered_output(
        str(video),
        expected_duration=4.0,
        require_audio=True,
        duration_tolerance=0.35,
    )
    assert not ok_bad
    assert "shorter" in reason_bad


def test_validate_min_duration_still_rejects_truncated_tail(make_test_video):
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    video = make_test_video(duration=2.0, fps=30)
    ok, _, reason = burn.validate_rendered_output(
        str(video),
        expected_duration=2.0,
        require_audio=True,
        duration_tolerance=0.35,
        min_duration=5.0,  # floor higher than actual
    )
    assert not ok
    assert "shorter" in reason


def test_resolve_source_av_timing_fields(make_test_video):
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    video = make_test_video(duration=2.0, fps=30)
    timing = burn.resolve_source_av_timing(str(video))
    assert timing["has_audio"] is True
    assert timing["source_duration"] > 0
    assert timing["video_lead_in"] >= 0.0
    assert "summary" in timing


def test_compose_math_matches_fontinalia_case():
    """Document the exact numbers from the failed full render."""
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    source_duration = 374.033667
    video_lead_in = 1.0
    # render_duration from probe_video_duration ≈ format duration
    expected = burn.expected_compose_duration(source_duration, video_lead_in)
    assert expected == pytest.approx(374.033667)
    # actual partial was ~374.067 — must pass with default 0.35 tolerance
    # simulate by checking the comparison logic via a real short file is enough;
    # here just assert the inequality that previously failed is now OK:
    actual = 374.066667
    bad_expected = source_duration + video_lead_in  # old formula
    assert actual + 0.35 < bad_expected  # old code would FAIL
    assert actual + 0.35 >= expected  # new code PASS
