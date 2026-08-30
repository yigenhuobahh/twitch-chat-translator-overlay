#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for translation resume helpers and process/job utilities."""

from __future__ import annotations

from pathlib import Path
import sys

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def test_make_job_dir_unique(tmp_path: Path):
    from process_util import make_job_dir

    a = make_job_dir(tmp_path, prefix="job_")
    b = make_job_dir(tmp_path, prefix="job_")
    assert a.is_dir() and b.is_dir()
    assert a != b
    assert a.parent == tmp_path
    assert a.name.startswith("job_")


def test_progress_helpers_roundtrip(tmp_path: Path):
    import translate_chat_openai as tr

    progress_file = tmp_path / "t.json.progress.json"
    payload = {
        "schema_version": tr.PROGRESS_SCHEMA_VERSION,
        "translations": {"1": "你好", "2": "世界"},
        "failed": [3],
    }
    tr.save_progress(progress_file, payload)
    loaded = tr.load_progress(progress_file)
    assert loaded["translations"]["1"] == "你好"
    assert loaded["translations"]["2"] == "世界"
    assert 3 in loaded["failed"] or "3" in map(str, loaded["failed"])


def test_param_validation_helpers():
    import pytest

    from common_utils import validate_non_negative_float, validate_positive_int

    assert validate_positive_int("fps", 30, 1, 240) == 30
    with pytest.raises(ValueError):
        validate_positive_int("fps", 0, 1, 240)
    with pytest.raises(ValueError):
        validate_non_negative_float("offset", -1)


def test_run_tracked_echo():
    import sys

    from process_util import run_tracked

    r = run_tracked(
        [sys.executable, "-c", "print('ok')"],
        stdout=__import__("subprocess").PIPE,
        stderr=__import__("subprocess").PIPE,
        text=True,
    )
    assert r.returncode == 0
    assert "ok" in (r.stdout or "")


def test_cli_rejects_bad_fps(tmp_path: Path):
    import os
    import subprocess

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    video = tmp_path / "v.mp4"
    html = tmp_path / "c.html"
    video.write_bytes(b"x")
    html.write_text("<html></html>", encoding="utf-8")
    cmd = [
        sys.executable,
        str(SCRIPTS_DIR / "twitch_chat_burn.py"),
        str(video),
        str(html),
        "--fps", "0",
        "--export-translation", str(tmp_path / "e.json"),
        "--offset", "0",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    assert r.returncode != 0
    joined = (r.stdout or "") + (r.stderr or "")
    assert "fps" in joined.lower() or "FPS" in joined or "error" in joined.lower() or "错误" in joined
