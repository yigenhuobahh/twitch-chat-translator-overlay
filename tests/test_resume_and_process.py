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


# ---------------------------------------------------------------------------
# Fix 6: run_meta 绝对时限兜底 Windows pid 复用
# ---------------------------------------------------------------------------

def test_is_live_run_meta_absolute_cap_beats_live_pid(monkeypatch):
    """updated_at 远超 7 天：即使 pid_is_alive 恒 True（pid 复用），也非 live。"""
    import time

    import run_meta as rm

    monkeypatch.setattr(rm, "pid_is_alive", lambda _pid: True)
    now = time.time()
    assert rm.is_live_run_meta(
        {"status": "running", "pid": 123, "updated_at": "2020-01-01T00:00:00"},
        now=now,
    ) is False
    # 新鲜 meta + 活 pid 保持 live（原语义未破坏）。
    fresh = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
    assert rm.is_live_run_meta(
        {"status": "running", "pid": 123, "updated_at": fresh},
        now=now,
    ) is True


def test_is_live_run_meta_absolute_cap_boundary():
    """6 天前、无 pid：未超绝对时限（7 天），绝对时限不提前误伤——
    stale_after_sec 设为覆盖整个 6 天窗口时仍算 live。"""
    import time

    import run_meta as rm

    now = time.time()
    six_days_ago = time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.localtime(now - 6 * 24 * 3600)
    )
    assert rm.is_live_run_meta(
        {"status": "running", "updated_at": six_days_ago},
        stale_after_sec=7 * 24 * 3600,
        now=now,
    ) is True


# ---------------------------------------------------------------------------
# Fix 7: 孤儿 .download- 分段暂存文件识别 + clean 实际删除
# ---------------------------------------------------------------------------

def test_is_partial_artifact_recognizes_orphan_download_staging():
    from process_util import _is_partial_artifact

    # twitch_download._new_download_staging_path 形态: ".<name>.download-<hex32>.<ext>"
    assert _is_partial_artifact(".video.mp4.download-abc12345.mp4")
    assert _is_partial_artifact(".video.mp4.download-" + "a" * 32 + ".mp4")
    assert _is_partial_artifact(".chat.html.download-0123abcd.html")
    assert _is_partial_artifact(".clip.mkv.download-deadbeef.mkv")
    # 普通隐藏文件不误伤
    assert not _is_partial_artifact(".gitignore")
    assert not _is_partial_artifact(".env")
    assert not _is_partial_artifact(".video.mp4.download-nothex.mp4")
    assert not _is_partial_artifact(".video.mp4.download-abc12345.txt")


def test_clean_temp_artifacts_removes_orphan_download_staging(tmp_path, capsys):
    from process_util import clean_temp_artifacts

    orphan = tmp_path / ".video.mp4.download-abc12345.mp4"
    orphan.write_bytes(b"x" * 1024)
    keep = tmp_path / "video.mp4"
    keep.write_bytes(b"y")
    hidden = tmp_path / ".gitignore"
    hidden.write_text("*.tmp\n", encoding="utf-8")

    count, freed = clean_temp_artifacts(tmp_path)
    assert count == 1
    assert freed == 1024
    assert not orphan.exists()
    assert keep.exists()
    assert hidden.exists()
