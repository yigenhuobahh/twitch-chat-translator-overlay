#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Content-level smoke: a successful render must actually contain chat pixels.

Guards against "pipeline exits 0 but the danmaku box is blank / frozen":
the overlay frames and the composed MP4 are opened with PIL and the chat
region must differ from a pure background by a sane amount of pixels, and
the fade-in window must animate (adjacent frames differ).

No GPU / no network: PIL + local FFmpeg only.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

pytestmark = pytest.mark.smoke

# Two text-only CJK messages at t=0 and t=1 (TwitchDownloaderGUI format).
MINIMAL_CHAT_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>overlay content smoke</title></head>
<body>
<pre class="comment-root">[<a href="https://www.twitch.tv/videos/1?t=0h0m0s">0:00:00</a>] <span class="comment-author" style="color: #FF0000">Alice</span><span class="comment-message">: 大家好 欢迎来到直播间</span></pre>
<pre class="comment-root">[<a href="https://www.twitch.tv/videos/1?t=0h0m1s">0:00:01</a>] <span class="comment-author" style="color: #00FF00">Bob</span><span class="comment-message">: 这个游戏真好玩</span></pre>
</body>
</html>
"""

BOX_X, BOX_Y, BOX_W, BOX_H = 8, 8, 240, 130
DURATION, FPS = 2.0, 10


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    import os

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            **{k: v for k, v in os.environ.items()},
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        },
    )


def _content_pixels(img, threshold: int = 32) -> int:
    """Count pixels that differ from a pure black background."""
    rgba = img.convert("RGBA")
    return sum(
        1
        for r, g, b, _a in rgba.getdata()
        if max(r, g, b) > threshold
    )


def _diff_pixels(img_a, img_b, threshold: int = 32) -> int:
    """Count pixel positions whose RGB differs between two same-size images."""
    a = img_a.convert("RGBA").getdata()
    b = img_b.convert("RGBA").getdata()
    return sum(
        1
        for (r1, g1, b1, _a1), (r2, g2, b2, _a2) in zip(a, b)
        if max(abs(r1 - r2), abs(g1 - g2), abs(b1 - b2)) > threshold
    )


def _load_frame(frames_dir: Path, index: int):
    from PIL import Image

    return Image.open(frames_dir / f"frame_{index:05d}.png")


def test_overlay_output_contains_chat_pixels_and_fade_animation(
    tmp_path: Path,
    make_test_video,
):
    import twitch_chat_burn as burn

    video = make_test_video(duration=DURATION, width=320, height=180, fps=FPS)
    html = tmp_path / "chat.html"
    html.write_text(MINIMAL_CHAT_HTML, encoding="utf-8")
    out_dir = tmp_path / "render"
    out_dir.mkdir()

    cmd = [
        sys.executable, str(SCRIPTS_DIR / "twitch_chat_burn.py"),
        str(video), str(html),
        "--x", str(BOX_X), "--y", str(BOX_Y),
        "--w", str(BOX_W), "--h", str(BOX_H),
        "--fps", str(FPS),
        "--preview-clip", str(int(DURATION)),
        "--out-dir", str(out_dir),
        "--job-dir", str(out_dir),
        "--keep-temp",
        "--offset", "0",
        "--overlay-codec", "png",
    ]
    proc = _run(cmd)
    assert proc.returncode == 0, (proc.stdout or "") + "\n" + (proc.stderr or "")

    final = out_dir / f"{video.stem}_chat.mp4"
    assert final.is_file(), f"missing output: {final}\n{proc.stdout}"

    # --- overlay frames: right amount, real content, animated fade-in ---
    frames_dir = out_dir / "overlay_frames"
    frames = sorted(frames_dir.glob("frame_*.png"))
    expected = burn.expected_overlay_frame_count(DURATION, FPS)
    assert len(frames) == expected, f"{len(frames)} != {expected}"

    # Message 1 is visible from t=0; fade-in completes after 0.3s (FADE_IN_SECONDS).
    frame_blank = _load_frame(frames_dir, 0)  # t=0.0: alpha 0 -> no text yet
    frame_fading = _load_frame(frames_dir, 1)  # t=0.1: mid fade-in
    frame_full = _load_frame(frames_dir, 5)  # t=0.5: fully opaque text
    box_area = BOX_W * BOX_H

    full_count = _content_pixels(frame_full)
    # Sane range: clearly more than an empty frame, clearly not a filled box.
    assert full_count >= 150, f"chat box looks blank: {full_count} content pixels"
    assert full_count <= box_area * 0.6, f"chat box looks flooded: {full_count} pixels"

    blank_count = _content_pixels(frame_blank)
    assert full_count - blank_count >= 100, (
        f"fade-in did not ramp content (blank={blank_count}, full={full_count})"
    )

    # The fade window must animate: frozen alpha (static-reuse regression) fails this.
    fade_diff = _diff_pixels(frame_fading, frame_full)
    assert fade_diff >= 50, f"frames inside fade-in window are frozen (diff={fade_diff})"

    # --- composed MP4: chat box area must differ from the black source video ---
    extract = tmp_path / "composed_frame.png"
    r = _run([
        "ffmpeg", "-y", "-ss", "1.0", "-i", str(final),
        "-frames:v", "1", str(extract),
    ])
    assert r.returncode == 0 and extract.is_file(), r.stderr

    from PIL import Image

    composed = Image.open(extract)
    chat_region = composed.convert("RGB").crop(
        (BOX_X, BOX_Y, BOX_X + BOX_W, BOX_Y + BOX_H)
    )
    region_count = _content_pixels(chat_region)
    assert 100 <= region_count <= box_area * 0.9, (
        f"composed frame chat region content={region_count} out of range"
    )
