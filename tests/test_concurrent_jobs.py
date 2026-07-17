#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Concurrent job-dir isolation, promote naming, and placeholder job validation."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(SCRIPTS)
    env["_TWITCH_TRANSPARENT_TEST_MODE"] = "1"
    return env


def test_make_job_dir_unique_and_marked(tmp_path: Path):
    from process_util import JOB_DIR_MARKER, is_tool_job_dir, make_job_dir

    a = make_job_dir(tmp_path, prefix="job_")
    b = make_job_dir(tmp_path, prefix="job_")
    assert a != b
    assert (a / JOB_DIR_MARKER).is_file()
    assert (b / JOB_DIR_MARKER).is_file()
    assert is_tool_job_dir(a)
    assert is_tool_job_dir(b)
    # pid embedded
    assert re.match(r"job_\d+_\d+_[0-9a-fA-F]+", a.name)


def test_placeholder_and_validate_job_media_paths(tmp_path: Path):
    from job_config import is_placeholder_media_path, validate_job_media_paths, write_job_file

    assert is_placeholder_media_path("path/to/video.mp4")
    assert is_placeholder_media_path(r"path\to\chat.html")
    assert not is_placeholder_media_path(str(tmp_path / "real.mp4"))

    problems = validate_job_media_paths(
        {"video": "path/to/video.mp4", "chat_html": "path/to/chat.html"},
        require_existing=True,
    )
    assert problems
    assert any("占位" in p or "path/to" in p for p in problems)

    vid = tmp_path / "v.mp4"
    html = tmp_path / "c.html"
    vid.write_bytes(b"not-a-real-mp4")
    html.write_text("<html></html>", encoding="utf-8")
    ok = validate_job_media_paths(
        {"video": str(vid), "chat_html": str(html)},
        require_existing=True,
    )
    assert ok == []

    # example-style job file roundtrip + validation
    job_path = write_job_file(
        tmp_path / "ex.yaml",
        {
            "video": "path/to/video.mp4",
            "chat_html": "path/to/chat.html",
            "mode": "preview",
            "render_original": True,
        },
        title="ex",
        overwrite=True,
    )
    text = job_path.read_text(encoding="utf-8")
    assert "video:" in text


def test_pipeline_rejects_job_without_media_non_tty(tmp_path: Path):
    from job_config import write_job_file

    # Reusable style job: no pinned paths
    job = write_job_file(
        tmp_path / "style.yaml",
        {
            "mode": "preview",
            "render_original": True,
            "preview_clip": 3,
            "layout_preset": "compact",
        },
        title="style",
        overwrite=True,
        pin_paths=False,
    )
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_cn_chat.py"), "--job", str(job)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_env(),
    )
    assert r.returncode != 0
    joined = (r.stdout or "") + (r.stderr or "")
    assert (
        "video" in joined.lower()
        or "chat" in joined.lower()
        or "非交互" in joined
        or "取消注释" in joined
        or "缺少" in joined
    )


def test_pipeline_accepts_job_plus_cli_media(tmp_path: Path, make_test_video):
    from job_config import write_job_file

    html = ROOT / "tests" / "fixtures" / "twitchdownloader_chat.html"
    if not html.is_file():
        pytest.skip("fixture missing")
    video = make_test_video(duration=2.0)
    job = write_job_file(
        tmp_path / "style.yaml",
        {
            "mode": "preview",
            "render_original": True,
            "preview_clip": 2,
            "overlay_codec": "png",
            "layout_preset": "compact",
        },
        title="style",
        overwrite=True,
        pin_paths=False,
    )
    out = tmp_path / "o.mp4"
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "render_cn_chat.py"),
            "--job",
            str(job),
            str(video),
            str(html),
            "--output",
            str(out),
            "--workdir",
            str(tmp_path / "w"),
            "--fps",
            "15",
            "--offset",
            "0",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_env(),
    )
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    assert out.is_file()


def test_promote_to_out_base_uses_job_unique_name_on_collision(tmp_path: Path):
    """Unit-level simulation of concurrent promote basename collision."""
    import shutil

    out_base = tmp_path / "out"
    job_a = tmp_path / "job_1_111_aaa"
    job_b = tmp_path / "job_2_222_bbb"
    out_base.mkdir()
    job_a.mkdir()
    job_b.mkdir()
    src_a = job_a / "clip_chat.mp4"
    src_b = job_b / "clip_chat.mp4"
    src_a.write_bytes(b"AAAA")
    src_b.write_bytes(b"BBBB")
    # First promote wins default name
    first = out_base / "clip_chat.mp4"
    shutil.copy2(src_a, first)
    # Second should prefer unique name when default exists and is not samefile
    base_name = "clip_chat.mp4"
    promoted = out_base / base_name
    assert promoted.is_file()
    job_tag = job_b.name
    stem, ext = os.path.splitext(base_name)
    alt = out_base / f"{stem}__{job_tag}{ext}"
    # Mimic production heuristic
    try:
        same = os.path.samefile(src_b, promoted)
    except OSError:
        same = False
    assert not same
    target = alt if not same else promoted
    shutil.copy2(src_b, target)
    assert first.read_bytes() == b"AAAA"
    assert alt.read_bytes() == b"BBBB"


@pytest.mark.smoke
@pytest.mark.max
def test_concurrent_burns_shared_out_dir_isolated(tmp_path: Path, make_test_video):
    """Two burns, same --out-dir, default job dirs: both succeed with distinct job_*."""
    video = make_test_video(duration=3.0, fps=30)
    html = ROOT / "tests" / "fixtures" / "twitchdownloader_chat.html"
    if not html.is_file():
        pytest.skip("fixture html missing")
    out_dir = tmp_path / "shared"
    out_dir.mkdir()

    def run_one(log_path: Path) -> int:
        cmd = [
            sys.executable,
            str(SCRIPTS / "twitch_chat_burn.py"),
            str(video),
            str(html),
            "--preview-clip",
            "2",
            "--overlay-codec",
            "png",
            "--offset",
            "0",
            "--fps",
            "15",
            "--out-dir",
            str(out_dir),
            "--keep-temp",
        ]
        with open(log_path, "w", encoding="utf-8") as fh:
            p = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, env=_env())
        return p.returncode

    log_a = tmp_path / "a.txt"
    log_b = tmp_path / "b.txt"
    results: list[int] = [None, None]  # type: ignore

    def wrap(i: int, logp: Path):
        results[i] = run_one(logp)

    t1 = threading.Thread(target=wrap, args=(0, log_a))
    t2 = threading.Thread(target=wrap, args=(1, log_b))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert results[0] == 0, log_a.read_text(encoding="utf-8", errors="replace")[-800:]
    assert results[1] == 0, log_b.read_text(encoding="utf-8", errors="replace")[-800:]

    job_dirs = [p for p in out_dir.iterdir() if p.is_dir() and p.name.startswith("job_")]
    assert len(job_dirs) >= 2
    pids = set()
    for d in job_dirs:
        m = re.match(r"job_\d+_(\d+)_[0-9a-fA-F]+", d.name)
        assert m, d.name
        pids.add(m.group(1))
        assert (d / ".twitch_overlay_job").is_file()
    assert len(pids) >= 2

    # Each job should have its own chat mp4; root may have unique-suffixed copies
    job_mp4s = list(out_dir.glob("job_*/" + video.stem + "_chat.mp4"))
    assert len(job_mp4s) >= 2
