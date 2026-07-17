#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Performance helpers and encode option unit tests."""

from __future__ import annotations

from pathlib import Path
import sys

from PIL import Image
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def test_write_or_reuse_frame_hardlink_or_copy(tmp_path: Path):
    from render_perf import frame_path, write_or_reuse_frame

    frames = tmp_path / "frames"
    img = Image.new("RGBA", (8, 8), (255, 0, 0, 128))
    assert write_or_reuse_frame(frames, 0, img) == "write"
    action = write_or_reuse_frame(frames, 1, img, reuse_from=0)
    assert action in ("hardlink", "copy")
    assert frame_path(frames, 0).is_file()
    assert frame_path(frames, 1).is_file()
    assert frame_path(frames, 0).stat().st_size == frame_path(frames, 1).stat().st_size


def test_blank_gap_and_expand(tmp_path: Path):
    from PIL import Image

    from render_perf import blank_gap_frame_indexes, expand_frame_sequence_for_ffmpeg, write_or_reuse_frame

    idxs = blank_gap_frame_indexes(0, 10, hold_stride=4)
    assert idxs[0] == 0
    assert idxs[-1] == 9
    assert len(idxs) < 10

    frames = tmp_path / "f"
    img = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    for i in idxs:
        write_or_reuse_frame(frames, i, img)
    stats = expand_frame_sequence_for_ffmpeg(frames, 10, idxs)
    assert stats["filled"] >= 1
    assert len(list(frames.glob("frame_*.png"))) == 10


def test_resolve_encode_options_x264_defaults():
    from encode_options import build_video_encode_args, resolve_encode_options, summarize_encode_options

    opts = resolve_encode_options(encoder="x264", crf=20, video_preset="veryfast")
    assert opts.resolved_encoder == "x264"
    assert opts.video_codec == "libx264"
    args = build_video_encode_args(opts)
    assert "-c:v" in args and "libx264" in args
    assert "-crf" in args and "20" in args
    assert "-preset" in args and "veryfast" in args
    assert "encoder=x264" in summarize_encode_options(opts)


def test_resolve_encode_options_bitrate_mode():
    from encode_options import build_video_encode_args, resolve_encode_options

    opts = resolve_encode_options(encoder="x264", video_bitrate="6M", maxrate="8M", bufsize="12M")
    assert opts.video_bitrate == "6m" or opts.video_bitrate == "6M" or opts.video_bitrate.endswith("m")
    args = build_video_encode_args(opts)
    assert "-b:v" in args
    assert "-crf" not in args


def test_nvenc_args_shape_even_if_encoder_missing():
    from encode_options import build_video_encode_args, resolve_encode_options

    # Force nvenc logical family; concrete codec may or may not exist on this machine.
    opts = resolve_encode_options(encoder="nvenc", crf=19, video_preset="p4")
    assert opts.resolved_encoder == "nvenc"
    args = build_video_encode_args(opts)
    assert args[0:2] == ["-c:v", opts.video_codec]
    assert "-cq" in args or "-b:v" in args


def test_webm_args_include_cpu_used():
    from encode_options import build_webm_encode_args, resolve_encode_options

    opts = resolve_encode_options(webm_cpu_used=6, webm_crf=32)
    args = build_webm_encode_args(opts)
    assert "-cpu-used" in args and "6" in args
    assert "-crf" in args and "32" in args


@pytest.mark.smoke
def test_smoke_with_static_reuse_and_png_overlay(make_test_video, tmp_path: Path):
    """End-to-end short clip using new perf/encode flags (CPU path)."""
    import os
    import subprocess

    video = make_test_video(duration=2.0, fps=15)
    html = Path(__file__).resolve().parent / "fixtures" / "twitchdownloader_chat.html"
    out_dir = tmp_path / "perf"
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
        "--encoder", "x264",
        "--video-preset", "ultrafast",
        "--crf", "28",
        "--overlay-codec", "png",
        "--webm-cpu-used", "8",
    ]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    assert proc.returncode == 0, (proc.stdout or "") + "\n" + (proc.stderr or "")
    final = out_dir / f"{video.stem}_chat.mp4"
    assert final.is_file()
    frames = list((out_dir / "overlay_frames").glob("frame_*.png"))
    assert len(frames) == 30  # 2s * 15fps
