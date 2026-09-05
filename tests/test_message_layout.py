#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for shared message layout helpers (schedule/render must not drift)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load_burn():
    path = SCRIPTS / "twitch_chat_burn.py"
    spec = importlib.util.spec_from_file_location("twitch_chat_burn_layout", str(path))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class _FakeFont:
    """Monospace-ish metric: 1 unit per character for deterministic wrap tests."""

    def getbbox(self, s: str):
        w = len(s or "")
        return (0, 0, w, 10)


@pytest.fixture(scope="module")
def burn():
    return _load_burn()


@pytest.fixture
def font():
    return _FakeFont()


def test_build_message_frag_list_strips_leading_colon_and_placeholder_emotes(burn, font):
    msg = {
        "author": "u",
        "badges": [],
        "fragments": [
            {"type": "text", "text": ": hello"},
            {"type": "emote", "class": "first-lul", "title": "LUL"},
            {"type": "text", "text": ":"},
            {"type": "text", "text": "  "},
        ],
    }
    frags = burn.build_message_frag_list(
        msg,
        text_width_fn=lambda s: font.getbbox(s)[2],
        emote_width_fn=lambda _c: 12,
        emote_available_fn=lambda _c: False,  # force [title] placeholder
    )
    assert frags[0] == ("text", "hello", 5)
    assert frags[1][0] == "text"
    assert frags[1][1] == "[LUL]"
    assert len(frags) == 2


def test_build_message_frag_list_keeps_available_emote(burn, font):
    msg = {
        "author": "u",
        "badges": [],
        "fragments": [
            {"type": "emote", "class": "first-lul", "title": "LUL"},
            {"type": "text", "text": " hi"},
        ],
    }
    frags = burn.build_message_frag_list(
        msg,
        text_width_fn=lambda s: font.getbbox(s)[2],
        emote_width_fn=lambda _c: 12,
        emote_available_fn=lambda c: c == "first-lul",
    )
    assert frags[0] == ("emote", "first-lul", 12)
    assert frags[1][0] == "text"
    assert "hi" in frags[1][1]


def test_layout_linecount_and_render_share_header_and_body(burn, font):
    """Prepass (no ellipsis) and render (ellipsis) must agree on short messages."""
    msg = {
        "author": "alice",
        "badges": [{"title": "moderator"}],
        "fragments": [{"type": "text", "text": ": short"}],
    }

    def tw(s):
        return font.getbbox(s)[2]

    lines_a, header_a, n_a = burn.layout_message_lines(
        msg,
        max_w=80,
        font=font,
        font_bold=font,
        text_width_fn=tw,
        emote_width_fn=lambda _c: 10,
        emote_available_fn=lambda _c: False,
        max_message_lines=0,
        truncate_with_ellipsis=False,
    )
    lines_b, header_b, n_b = burn.layout_message_lines(
        msg,
        max_w=80,
        font=font,
        font_bold=font,
        text_width_fn=tw,
        emote_width_fn=lambda _c: 10,
        emote_available_fn=lambda _c: False,
        max_message_lines=0,
        truncate_with_ellipsis=True,
    )
    assert header_a["author"] == header_b["author"] == "alice"
    assert header_a["header_w"] == header_b["header_w"]
    assert n_a == n_b == 1
    assert lines_a[0][0][1] == "short"
    assert lines_b[0][0][1] == "short"


def test_layout_truncation_adds_visible_ellipsis(burn, font):
    long_body = "字" * 80
    msg = {
        "author": "u",
        "badges": [],
        "fragments": [{"type": "text", "text": f": {long_body}"}],
    }

    def tw(s):
        return font.getbbox(s)[2]

    _lines, _h, n_cap = burn.layout_message_lines(
        msg,
        max_w=40,
        font=font,
        font_bold=font,
        text_width_fn=tw,
        emote_width_fn=lambda _c: 10,
        emote_available_fn=lambda _c: False,
        max_message_lines=2,
        truncate_with_ellipsis=False,
    )
    lines_t, _h2, n_t = burn.layout_message_lines(
        msg,
        max_w=40,
        font=font,
        font_bold=font,
        text_width_fn=tw,
        emote_width_fn=lambda _c: 10,
        emote_available_fn=lambda _c: False,
        max_message_lines=2,
        truncate_with_ellipsis=True,
    )
    assert n_cap == 2
    assert n_t == 2
    assert len(lines_t) == 2
    # Last fragment on last line should be ellipsis marker
    last_items = lines_t[-1]
    assert any(it[0] == "text" and it[1] == "..." for it in last_items)


def test_apply_import_pure_emote_keeps_image_fragments(burn):
    chat = {
        "messages": [
            {
                "author": "u",
                "timestamp": 1.0,
                "fragments": [
                    {"type": "emote", "class": "first-lul", "title": "LUL"},
                ],
            }
        ]
    }
    trans = {
        "messages": [
            {
                "index": 0,
                "author": "u",
                "timestamp": 1.0,
                "original": "[LUL]",
                "translation": "[LUL]",
            }
        ]
    }
    replaced, stripped, warnings = burn.apply_imported_translations(chat, trans)
    assert replaced == 1
    assert stripped >= 1
    frags = chat["messages"][0]["fragments"]
    assert len(frags) == 1
    assert frags[0]["type"] == "emote"
    assert frags[0]["class"] == "first-lul"


# --- S-3: hard cap on per-message line count (default-behavior change) ---

def test_layout_hard_caps_unbounded_50k_message_with_ellipsis(burn, font):
    """50k-char message with no configured limit: <=200 lines, last line ends '...'.

    Default behavior change: max_message_lines=0 used to mean unlimited lines
    (516 lines / tens of MB bitmaps for a 50k-char message); it now falls back
    to the module hard cap of 200 lines with a visible ellipsis.
    """
    long_body = "字" * 50000
    msg = {
        "author": "u",
        "badges": [],
        "fragments": [{"type": "text", "text": f": {long_body}"}],
    }

    def tw(s):
        return font.getbbox(s)[2]

    # Prepass (line-count prepass must agree with the capped render height)
    _lines, _h, n_prepass = burn.layout_message_lines(
        msg, max_w=96, font=font, font_bold=font, text_width_fn=tw,
        emote_width_fn=lambda _c: 10, emote_available_fn=lambda _c: False,
        max_message_lines=0, truncate_with_ellipsis=False,
    )
    assert n_prepass <= 200

    lines, _h2, n_render = burn.layout_message_lines(
        msg, max_w=96, font=font, font_bold=font, text_width_fn=tw,
        emote_width_fn=lambda _c: 10, emote_available_fn=lambda _c: False,
        max_message_lines=0, truncate_with_ellipsis=True,
    )
    assert len(lines) == n_render <= 200
    assert any(it[0] == "text" and it[1] == "..." for it in lines[-1])


def test_layout_explicit_limit_above_hard_cap_is_honored(burn, font):
    """An explicit max_message_lines=300 (> 200 hard cap) is a user choice: not compressed."""
    long_body = "字" * 50000
    msg = {
        "author": "u",
        "badges": [],
        "fragments": [{"type": "text", "text": f": {long_body}"}],
    }

    def tw(s):
        return font.getbbox(s)[2]

    lines, _h, n = burn.layout_message_lines(
        msg, max_w=96, font=font, font_bold=font, text_width_fn=tw,
        emote_width_fn=lambda _c: 10, emote_available_fn=lambda _c: False,
        max_message_lines=300, truncate_with_ellipsis=True,
    )
    assert len(lines) == n == 300
    assert any(it[0] == "text" and it[1] == "..." for it in lines[-1])


def test_truncate_hard_cap_fallback_matches_explicit_semantics(burn, font):
    """Direct truncate call with max_message_lines=0 caps at 200 via the same ellipsis path."""

    def tw(s):
        return font.getbbox(s)[2]

    lines_in = [[("text", "x", 1)]] * 250
    out = burn.truncate_wrapped_lines_with_ellipsis(
        lines_in, max_message_lines=0, max_w=96, padding=5, indent=12, gap=3,
        text_width_fn=tw,
    )
    assert len(out) == 200
    assert any(it[0] == "text" and it[1] == "..." for it in out[-1])
