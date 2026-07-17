#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression: chat overlay fps must not force final video fps."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS_DIR))

import twitch_chat_burn as burn  # noqa: E402


def _run(cmd):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def test_resolve_output_fps_explicit():
    assert burn.resolve_output_fps("missing.mp4", explicit=48, fallback=30) == 48


def test_resolve_output_fps_fallback():
    assert burn.resolve_output_fps("missing.mp4", explicit=None, fallback=24) == 24


@pytest.mark.smoke
def test_output_keeps_source_fps_when_overlay_lower(make_test_video, tmp_path: Path):
    # Source 30fps, overlay sampling 15fps -> published video should stay ~30.
    video = make_test_video(duration=2.0, fps=30)
    html = FIXTURES_DIR / "twitchdownloader_chat.html"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    cmd = [
        sys.executable, str(SCRIPTS_DIR / "twitch_chat_burn.py"),
        str(video), str(html),
        "--fps", "15",
        "--preview-clip", "2",
        "--out-dir", str(out_dir),
        "--job-dir", str(out_dir),
        "--keep-temp",
        "--offset", "0",
        "--x", "5", "--y", "20", "--w", "280", "--h", "180",
        "--overlay-codec", "png",
    ]
    r = _run(cmd)
    assert r.returncode == 0, r.stdout + "\n" + r.stderr
    # Console encoding on Windows may garble CJK logs; probe is authoritative.

    final = out_dir / f"{video.stem}_chat.mp4"
    assert final.is_file()
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate,avg_frame_rate",
            "-of", "json", str(final),
        ],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(probe.stdout)
    rate = info["streams"][0].get("avg_frame_rate") or info["streams"][0].get("r_frame_rate")
    if "/" in rate:
        num, den = rate.split("/", 1)
        fps = float(num) / max(float(den), 1.0)
    else:
        fps = float(rate)
    assert 29.0 <= fps <= 31.0, rate


@pytest.mark.smoke
def test_output_fps_flag_overrides_source(make_test_video, tmp_path: Path):
    video = make_test_video(duration=2.0, fps=30)
    html = FIXTURES_DIR / "twitchdownloader_chat.html"
    out_dir = tmp_path / "out2"
    out_dir.mkdir()
    cmd = [
        sys.executable, str(SCRIPTS_DIR / "twitch_chat_burn.py"),
        str(video), str(html),
        "--fps", "15",
        "--output-fps", "24",
        "--preview-clip", "1",
        "--out-dir", str(out_dir),
        "--job-dir", str(out_dir),
        "--keep-temp",
        "--offset", "0",
        "--x", "5", "--y", "20", "--w", "280", "--h", "180",
        "--overlay-codec", "png",
    ]
    r = _run(cmd)
    assert r.returncode == 0, r.stdout + "\n" + r.stderr
    final = out_dir / f"{video.stem}_chat.mp4"
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=avg_frame_rate,r_frame_rate",
            "-of", "json", str(final),
        ],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(probe.stdout)
    rate = info["streams"][0].get("avg_frame_rate") or info["streams"][0].get("r_frame_rate")
    if "/" in rate:
        num, den = rate.split("/", 1)
        fps = float(num) / max(float(den), 1.0)
    else:
        fps = float(rate)
    assert 23.0 <= fps <= 25.0, rate
