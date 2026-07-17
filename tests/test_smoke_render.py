#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Short FFmpeg smoke tests for the render/compose path."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS_DIR))


def _ffprobe(path: Path) -> dict:
    r = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,width,height",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(r.stdout)


@pytest.mark.smoke
def test_preview_clip_render_produces_valid_mp4(make_test_video, tmp_path: Path):
    import twitch_chat_burn as burn

    video = make_test_video(duration=3.0, fps=30)
    html = FIXTURES_DIR / "twitchdownloader_chat.html"
    out_dir = tmp_path / "render"
    out_dir.mkdir()

    cmd = [
        sys.executable, str(SCRIPTS_DIR / "twitch_chat_burn.py"),
        str(video), str(html),
        "--x", "10", "--y", "40", "--w", "300", "--h", "200",
        "--fps", "30",
        "--preview-clip", "3",
        "--out-dir", str(out_dir),
        "--job-dir", str(out_dir),
        "--keep-temp",
        "--offset", "0",
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**dict(**{k: v for k, v in __import__('os').environ.items()}), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    assert proc.returncode == 0, (proc.stdout or "") + "\n" + (proc.stderr or "")

    final = out_dir / f"{video.stem}_chat.mp4"
    assert final.is_file(), f"missing output: {final}\n{proc.stdout}"

    info = _ffprobe(final)
    duration = float(info["format"]["duration"])
    types = {s.get("codec_type") for s in info.get("streams", [])}
    assert "video" in types
    assert "audio" in types
    assert duration >= 2.7

    frames = sorted((out_dir / "overlay_frames").glob("frame_*.png"))
    expected = burn.expected_overlay_frame_count(3.0, 30)
    assert len(frames) == expected

    ok, summary, reason = burn.validate_rendered_output(
        str(final), expected_duration=3.0, require_audio=True
    )
    assert ok, f"{reason}; summary={summary}"


def _run_burn(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            **{k: v for k, v in __import__("os").environ.items()},
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        },
    )


@pytest.mark.smoke
def test_float_stack_preview_clip_render(make_test_video, tmp_path: Path):
    """Default lanes is legacy; float stack path must still compose a valid short MP4."""
    import twitch_chat_burn as burn

    video = make_test_video(duration=3.0, fps=30)
    html = FIXTURES_DIR / "twitchdownloader_chat.html"
    out_dir = tmp_path / "float_render"
    out_dir.mkdir()

    cmd = [
        sys.executable, str(SCRIPTS_DIR / "twitch_chat_burn.py"),
        str(video), str(html),
        "--x", "10", "--y", "40", "--w", "300", "--h", "200",
        "--fps", "15",
        "--stack-mode", "float",
        "--max-visible", "0",
        "--max-message-lines", "2",
        "--arrival-interval", "0.1",
        "--preview-clip", "3",
        "--out-dir", str(out_dir),
        "--job-dir", str(out_dir),
        "--keep-temp",
        "--offset", "0",
        "--overlay-codec", "png",
    ]
    proc = _run_burn(cmd)
    assert proc.returncode == 0, (proc.stdout or "") + "\n" + (proc.stderr or "")
    assert "stack_mode=float" in (proc.stdout or "")

    final = out_dir / f"{video.stem}_chat.mp4"
    assert final.is_file(), f"missing output: {final}\n{proc.stdout}"
    ok, summary, reason = burn.validate_rendered_output(
        str(final), expected_duration=3.0, require_audio=True
    )
    assert ok, f"{reason}; summary={summary}"
    frames = sorted((out_dir / "overlay_frames").glob("frame_*.png"))
    assert len(frames) == burn.expected_overlay_frame_count(3.0, 15)


@pytest.mark.smoke
def test_layout_mobile_preset_preview_clip(make_test_video, tmp_path: Path, repo_root: Path):
    """Public mobile preset must load and render through the burn CLI."""
    import twitch_chat_burn as burn

    video = make_test_video(duration=3.0, fps=30)
    html = FIXTURES_DIR / "twitchdownloader_chat.html"
    out_dir = tmp_path / "mobile_render"
    out_dir.mkdir()
    preset = repo_root / "profiles" / "layout_mobile.yaml"
    assert preset.is_file(), preset

    cmd = [
        sys.executable, str(SCRIPTS_DIR / "twitch_chat_burn.py"),
        str(video), str(html),
        "--layout-preset", str(preset),
        "--preview-clip", "3",
        "--out-dir", str(out_dir),
        "--job-dir", str(out_dir),
        "--keep-temp",
        "--offset", "0",
        "--overlay-codec", "png",
    ]
    proc = _run_burn(cmd)
    assert proc.returncode == 0, (proc.stdout or "") + "\n" + (proc.stderr or "")
    assert "stack_mode=float" in (proc.stdout or "")
    assert "max_visible=auto" in (proc.stdout or "")

    final = out_dir / f"{video.stem}_chat.mp4"
    assert final.is_file(), f"missing output: {final}\n{proc.stdout}"
    ok, summary, reason = burn.validate_rendered_output(
        str(final), expected_duration=3.0, require_audio=True
    )
    assert ok, f"{reason}; summary={summary}"


@pytest.mark.smoke
def test_preview_dense_seeks_mid_video(make_test_video, tmp_path: Path):
    """--preview-dense must pick a mid-window and still produce a valid short MP4."""
    import twitch_chat_burn as burn

    # Fixture chat is at 1-3s; densest on a 12s video with clip=3 should leave head.
    video = make_test_video(duration=12.0, fps=30)
    html = FIXTURES_DIR / "twitchdownloader_chat.html"
    out_dir = tmp_path / "dense_render"
    out_dir.mkdir()

    cmd = [
        sys.executable, str(SCRIPTS_DIR / "twitch_chat_burn.py"),
        str(video), str(html),
        "--x", "10", "--y", "40", "--w", "300", "--h", "200",
        "--fps", "15",
        "--stack-mode", "lanes",
        "--msg-lifetime", "14",
        "--preview-clip", "3",
        "--preview-dense",
        "--out-dir", str(out_dir),
        "--job-dir", str(out_dir),
        "--keep-temp",
        "--offset", "0",
        "--overlay-codec", "png",
    ]
    proc = _run_burn(cmd)
    assert proc.returncode == 0, (proc.stdout or "") + "\n" + (proc.stderr or "")
    out = proc.stdout or ""
    assert "预览最密段" in out or "dense" in out.lower()
    assert "start=" in out

    final = out_dir / f"{video.stem}_chat.mp4"
    assert final.is_file(), f"missing output: {final}\n{out}"
    info = _ffprobe(final)
    duration = float(info["format"]["duration"])
    assert duration >= 2.5
    ok, summary, reason = burn.validate_rendered_output(
        str(final), expected_duration=3.0, require_audio=True
    )
    assert ok, f"{reason}; summary={summary}"
    frames = sorted((out_dir / "overlay_frames").glob("frame_*.png"))
    assert len(frames) == burn.expected_overlay_frame_count(3.0, 15)


def test_export_translation_without_video_file(tmp_path: Path, ffmpeg_available: bool):
    """Export should work with a manual offset even if video path is unreadable."""
    # This test does not need a real video file for export-only + manual offset.
    html = FIXTURES_DIR / "twitchdownloader_chat.html"
    export_json = tmp_path / "export.json"
    missing_video = tmp_path / "does_not_exist.mp4"

    cmd = [
        sys.executable, str(SCRIPTS_DIR / "twitch_chat_burn.py"),
        str(missing_video), str(html),
        "--export-translation", str(export_json),
        "--offset", "0",
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**dict(**{k: v for k, v in __import__('os').environ.items()}), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    assert proc.returncode == 0, (proc.stdout or "") + "\n" + (proc.stderr or "")
    assert export_json.is_file()

    data = json.loads(export_json.read_text(encoding="utf-8"))
    assert len(data["messages"]) == 3
    # Schema v2: stream-absolute timestamps for stable import identity.
    assert data.get("time_base") == "stream"
    assert "export_offset" in data
    # Export flattens fragments into original text; emotesv2 must still appear as [Hey].
    originals = [msg.get("original", "") for msg in data["messages"]]
    assert any("[Hey]" in text for text in originals), originals
    assert any("[LUL]" in text for text in originals), originals
    assert any("[xdx]" in text for text in originals), originals
    for msg in data["messages"]:
        assert "index" in msg and "translation" in msg
        assert "stream_timestamp" in msg or "timestamp" in msg
