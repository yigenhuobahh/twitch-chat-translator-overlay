#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared pytest fixtures and path setup."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture(scope="session", autouse=True)
def _isolate_translation_env():
    """Prevent local .env / live API keys from affecting the suite."""
    os.environ["_TWITCH_TRANSPARENT_TEST_MODE"] = "1"
    for key in (
        "OPENAI_COMPAT_API_KEY",
        "OPENAI_COMPAT_BASE_URL",
        "OPENAI_COMPAT_MODEL",
        "AGNES_API_KEY",
        "AGNES_BASE_URL",
        "AGNES_MODEL",
    ):
        os.environ.pop(key, None)
    yield


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def scripts_dir() -> Path:
    return SCRIPTS_DIR


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.fixture
def make_test_video(tmp_path: Path, ffmpeg_available: bool):
    """Factory: create a short H.264/AAC test video with ffmpeg."""

    if not ffmpeg_available:
        pytest.skip("ffmpeg/ffprobe not available")

    def _make(duration: float = 3.0, width: int = 640, height: int = 360, fps: int = 30) -> Path:
        out = tmp_path / f"src_{duration:g}s_{width}x{height}_{fps}fps.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}:d={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=880:duration={duration}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-shortest",
            str(out),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert out.is_file()
        return out

    return _make
