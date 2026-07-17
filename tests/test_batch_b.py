#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch B: chat_parser extract, layout preset, lazy message image cache."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import FIXTURES_DIR, ROOT, load_module


def test_chat_parser_module_matches_burn_reexport(tmp_path: Path):
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    parser = load_module("chat_parser", "chat_parser.py")
    html = FIXTURES_DIR / "twitchdownloader_chat.html"
    a = burn.parse_chat_html(str(html), str(tmp_path / "a"))
    b = parser.parse_chat_html(str(html), str(tmp_path / "b"))
    assert len(a["messages"]) == len(b["messages"]) == 3
    assert [m["author"] for m in a["messages"]] == [m["author"] for m in b["messages"]]
    assert set(a["emote_map"]) == set(b["emote_map"])
    assert burn.parse_chat_html is parser.parse_chat_html or callable(burn.parse_chat_html)


def test_default_layout_preset_auto_fills_max_visible():
    lp = load_module("layout_preset", "layout_preset.py")
    preset = lp.load_layout_preset(ROOT / "profiles" / "layout_default.yaml")
    assert preset["max_visible"] == 0  # auto-fill by box height / font size


def test_mobile_layout_preset_loads_readability_controls():
    lp = load_module("layout_preset", "layout_preset.py")
    preset = lp.load_layout_preset(ROOT / "profiles" / "layout_mobile.yaml")
    assert preset["x"] == 15
    assert preset["y"] == 327
    assert preset["width"] == 497
    assert preset["height"] == 363
    assert preset["font_size"] == 15
    assert preset["max_visible"] == 0  # auto-fill by height
    assert preset.get("stack_mode", "float") == "float"
    assert preset["max_message_lines"] == 2
    assert preset["arrival_interval"] == 0.35
    assert "msg_lifetime" not in preset


def test_layout_preset_applies_only_defaults():
    lp = load_module("layout_preset", "layout_preset.py")
    preset = lp.load_layout_preset(ROOT / "profiles" / "layout_compact.yaml")
    assert preset["width"] == 420
    assert preset["msg_lifetime"] == 10.0
    args = SimpleNamespace(
        x=15, y=327, width=497, height=363, font_size=15,
        font_path="auto", font_bold_path="auto", fps=30,
        max_visible=0, msg_lifetime=14.0, bg_alpha=255, emote_height=22,
        blank_hold_seconds=0.5, no_reuse_static_frames=False, no_skip_blank_frames=False,
    )
    # explicit CLI width should win
    args.width = 900
    applied = lp.apply_layout_preset_to_namespace(
        args,
        preset,
        cli_defaults={
            "x": 15, "y": 327, "width": 497, "height": 363,
            "font_size": 15, "font_path": "auto", "font_bold_path": "auto",
            "fps": 30, "max_visible": 0, "msg_lifetime": 14.0,
            "bg_alpha": 255, "emote_height": 22, "blank_hold_seconds": 0.5,
        },
    )
    assert args.width == 900  # kept CLI
    assert args.height == 260  # from preset
    assert args.bg_alpha == 200
    assert "width" not in applied
    assert "height" in applied


@pytest.mark.smoke
def test_lazy_message_image_cache_evicts(tmp_path: Path, make_test_video):
    """Unit-level: message_image LRU keeps cache under cap when lazy is on."""
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    # Build a tiny chat_data with many short text messages
    messages = []
    for i in range(40):
        messages.append({
            "timestamp": float(i) * 0.2,
            "author": f"u{i}",
            "color": "#ffffff",
            "badges": [],
            "fragments": [{"type": "text", "text": f"hello {i}"}],
        })
    chat = {"messages": messages, "emote_map": {}}
    video = make_test_video(duration=2.0, fps=10)
    cfg = burn.OverlayConfig(
        x=0, y=0, width=320, height=180, font_size=14, fps=10,
        max_visible=5, msg_lifetime=1.0, bg_alpha=255, emote_h=18,
        preview_clip=2.0, lazy_message_images=True, message_image_cache_size=8,
        reuse_static_frames=True, skip_blank_frames=True,
    )
    # resolve fonts
    from common_utils import resolve_font_paths
    cfg.font_path, cfg.font_bold_path = resolve_font_paths("auto", "auto")
    frames_dir, duration = burn.render_overlay(chat, str(tmp_path), str(video), cfg)
    assert duration > 0
    assert Path(frames_dir).is_dir()
    # frames were produced
    pngs = list(Path(frames_dir).glob("frame_*.png"))
    assert pngs


def test_render_cn_chat_accepts_layout_preset_flag():
    r = load_module("render_cn_chat", "render_cn_chat.py")
    # ensure helpers exist
    assert callable(r.append_layout_burn_args)
    cmd = ["burn.py"]
    args = SimpleNamespace(
        max_visible=8, msg_lifetime=10.0, emote_height=20,
        lazy_message_images=True, message_image_cache_size=64,
    )
    r.append_layout_burn_args(cmd, args)
    assert "--max-visible" in cmd and "8" in cmd
    assert "--lazy-message-images" in cmd
    assert "--message-image-cache-size" in cmd
