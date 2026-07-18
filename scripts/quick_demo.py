#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create a tiny offline overlay demo so first-time users can verify setup."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

from common_utils import require_executable

_DEMO_HTML = """<!DOCTYPE html>
<html><head><meta charset=\"utf-8\"><title>Offline demo</title></head><body>
<pre class=\"comment-root\">[<a href=\"?t=0h0m0s\">0:00</a>] <a><span class=\"comment-author\" style=\"color: #ffffff\">DemoUser</span></a><span class=\"comment-message\">: Welcome! This is an offline overlay demo.</span></pre>
<pre class=\"comment-root\">[<a href=\"?t=0h0m2s\">0:02</a>] <a><span class=\"comment-author\" style=\"color: #ffffff\">Chat</span></a><span class=\"comment-message\">: No translation API is needed for this preview.</span></pre>
<pre class=\"comment-root\">[<a href=\"?t=0h0m4s\">0:04</a>] <a><span class=\"comment-author\" style=\"color: #ffffff\">Ready</span></a><span class=\"comment-message\">: Drag your video and chat HTML onto run.bat next.</span></pre>
</body></html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate an offline 6-second overlay demo")
    parser.add_argument("--output-dir", default="outputs/quick_demo")
    args = parser.parse_args(argv)
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    video = output_dir / "demo_source.mp4"
    chat = output_dir / "demo_chat.html"
    output = output_dir / "demo_overlay.mp4"
    chat.write_text(_DEMO_HTML, encoding="utf-8")
    try:
        ffmpeg = require_executable("ffmpeg")
        subprocess.run(
            [
                ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=0x202938:s=1280x720:r=30:d=6",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-c:a", "aac", str(video),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"[FAIL] Could not generate demo source video: {exc}", file=sys.stderr)
        return 1
    pipeline = Path(__file__).with_name("render_cn_chat.py")
    result = subprocess.run(
        [sys.executable, str(pipeline), str(video), str(chat), "--render-original", "--preview-clip", "6", "--output", str(output), "--yes"],
        check=False,
    )
    if result.returncode == 0:
        print(f"\n[OK] Offline demo complete: {output}")
        print("Next: drag your own video and Twitch chat HTML onto run.bat.")
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
