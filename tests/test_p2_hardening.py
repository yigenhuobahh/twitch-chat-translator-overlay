#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 hardening: validate long/tiny, shared hex color, third-party emote parse."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from helpers import FIXTURES_DIR, load_module


def test_hex_to_rgb_soft_shared():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    cu = load_module("common_utils", "common_utils.py")
    assert burn.hex_to_rgb("#f00") == (255, 0, 0)
    assert burn.hex_to_rgb("#00FF00") == (0, 255, 0)
    # soft path: invalid -> white (must not raise)
    assert burn.hex_to_rgb("not-a-color") == (255, 255, 255)
    assert burn.hex_to_rgb("") == (255, 255, 255)
    # strict path still raises
    with pytest.raises(ValueError):
        cu.hex_to_rgb("not-a-color")
    assert cu.hex_to_rgb_soft("not-a-color") == (255, 255, 255)
    # shared soft == burn wrapper
    assert burn.hex_to_rgb("#abc") == cu.hex_to_rgb_soft("#abc")


def test_validate_rejects_too_long_output(make_test_video):
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    video = make_test_video(duration=3.0, fps=30)
    ok, summary, reason = burn.validate_rendered_output(
        str(video),
        expected_duration=1.0,
        require_audio=True,
        duration_tolerance=0.35,
        max_extra_seconds=0.5,
    )
    assert not ok
    assert "longer" in reason.lower()
    assert summary["duration"] > 1.0


def test_validate_rejects_tiny_dimensions(tmp_path: Path):
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    out = tmp_path / "tiny.mp4"
    # 2x2 is below min 16 default? we set min_width=16 in call
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=2x2:r=10:d=1",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(out),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok, summary, reason = burn.validate_rendered_output(
        str(out),
        expected_duration=1.0,
        require_audio=False,
        min_width=16,
        min_height=16,
    )
    assert not ok
    assert "dimensions" in reason.lower() or "small" in reason.lower()


def test_third_party_emotes_parsed_and_images_extracted(tmp_path: Path):
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    html = FIXTURES_DIR / "td_third_party_emotes.html"
    chat = burn.parse_chat_html(str(html), str(tmp_path / "tp"))
    msgs = chat["messages"]
    assert len(msgs) == 3
    titles = [
        f.get("title")
        for m in msgs
        for f in m["fragments"]
        if f.get("type") == "emote"
    ]
    classes = [
        f.get("class")
        for m in msgs
        for f in m["fragments"]
        if f.get("type") == "emote"
    ]
    assert "LUL" in titles
    assert "catJAM" in titles
    assert "xdx" in titles
    assert "FFZDemo" in titles
    assert any(c.startswith("third-5e9e") for c in classes)
    assert any(c.startswith("third-01F63") for c in classes)
    assert any(c.startswith("second-") for c in classes)
    emap = chat["emote_map"]
    # CSS extracted for first/third/second including single-quoted content:url
    assert any(k.startswith("first-") for k in emap)
    assert any(k.startswith("third-") for k in emap)
    assert any(k.startswith("second-") for k in emap)
    for path in emap.values():
        assert Path(path).is_file()
        assert Path(path).stat().st_size > 0


def test_emote_img_attribute_order_title_before_class(tmp_path: Path):
    """title before class on img must still parse (third-party real HTML varies)."""
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    html = FIXTURES_DIR / "td_third_party_emotes.html"
    chat = burn.parse_chat_html(str(html), str(tmp_path / "order"))
    bob = chat["messages"][1]
    emotes = [f for f in bob["fragments"] if f.get("type") == "emote"]
    assert len(emotes) == 1
    assert emotes[0]["title"] == "catJAM"
    texts = [f.get("text", "") for f in bob["fragments"] if f.get("type") == "text"]
    assert any("dance" in t for t in texts)
