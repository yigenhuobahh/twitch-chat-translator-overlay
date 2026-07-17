#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regressions for 2026-07-11 deep audit silent-wrong fixes."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from helpers import load_module


def test_import_before_window_filter_in_main_source():
    """Guard: import-translation must run before filter_chat_for_time_window."""
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    src = inspect.getsource(burn.main)
    import_pos = src.find("if args.import_translation:")
    filter_pos = src.find("filter_chat_for_time_window")
    export_pos = src.find("if args.export_translation:")
    assert import_pos != -1 and filter_pos != -1 and export_pos != -1
    assert export_pos < filter_pos
    assert import_pos < filter_pos


def test_confirm_preview_operator_precedence():
    """Auto-offset tip should not fire for every preview without auto mode."""
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    src = inspect.getsource(burn.main)
    assert "(args.preview_frame is not None or args.preview_clip is not None)" in src
    assert 'and offset_info["mode"] == "auto"' in src


def test_negative_offset_allowed_by_runtime_validation():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    assert burn._validate_offset(-1.5) == -1.5
    with pytest.raises(ValueError, match="absolute value must be <= 7 days"):
        burn._validate_offset(-(7 * 24 * 3600 + 1))


def test_clean_translation_strips_username_prefix():
    tr = load_module("translate_chat_openai", "translate_chat_openai.py")
    assert tr.clean_translation_text("alice: 你好") == "你好"
    assert tr.clean_translation_text("<bob> 测试") == "测试"
    assert tr.clean_translation_text("[3] 内容") == "内容"


def test_chat_parser_accepts_single_quoted_time_href(tmp_path):
    parser = load_module("chat_parser", "chat_parser.py")
    html = (
        "<html><style>.first-1{content:url(\"data:image/png;base64,iVBORw0KGgo=\")}</style>"
        "<pre class=\"comment-root\">[<a href='https://twitch.tv/x?t=0h0m5s'>0:00:05</a>] "
        "<span class=\"comment-author\" style=\"color: #ff0000\">User</span>"
        "<span class=\"comment-message\">: hello world</span></pre></html>"
    )
    p = tmp_path / "chat.html"
    p.write_text(html, encoding="utf-8")
    data = parser.parse_chat_html(str(p), str(tmp_path / "out"))
    assert len(data["messages"]) == 1
    assert data["messages"][0]["timestamp"] == 5
    assert data["messages"][0]["author"] == "User"


def test_layout_preset_can_disable_reuse():
    lp = load_module("layout_preset", "layout_preset.py")
    args = SimpleNamespace(no_reuse_static_frames=False, no_skip_blank_frames=False, x=15)
    applied = lp.apply_layout_preset_to_namespace(
        args,
        {"reuse_static_frames": False, "skip_blank_frames": False},
        cli_defaults={"x": 15},
    )
    assert args.no_reuse_static_frames is True
    assert args.no_skip_blank_frames is True
    assert "no_reuse_static_frames" in applied
