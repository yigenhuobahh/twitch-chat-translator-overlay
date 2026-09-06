#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""security-2 regression tests: bidi overrides / zero-width / control chars
must never survive the two text entry points that feed Pillow draw.text:

1. Chat HTML text path  -> chat_text_layout.build_message_frag_list
   (via normalize_text, which applies sanitize_render_text after NFKC).
2. LLM translation path -> translation_support.clean_translation_text
   (applies the same shared sanitize_render_text before returning).

Also proves normal CJK/English text (incl. fullwidth punctuation) is untouched,
so the NFKC behavior is not collateral damage.
"""
from __future__ import annotations

import pytest

from chat_text_layout import (
    build_message_frag_list,
    compute_message_header_width,
    normalize_text,
    sanitize_render_text,
)
from translation_support import clean_translation_text


def _frag_text(msg):
    frags = build_message_frag_list(
        msg,
        text_width_fn=lambda t: len(t) * 8.0,
        emote_width_fn=lambda c: 20.0,
        emote_available_fn=lambda c: False,
    )
    return "".join(f[1] for f in frags if f[0] == "text")


@pytest.mark.parametrize(
    "label,ch",
    [
        ("RLO", "\u202e"),          # bidi override
        ("LRO", "\u202d"),
        ("PDF", "\u202c"),          # bidi pop
        ("LRI", "\u2066"),          # isolate
        ("RLI", "\u2067"),
        ("FSI", "\u2068"),
        ("PDI", "\u2069"),
        ("ZWSP", "\u200b"),         # zero-width
        ("ZWNJ", "\u200c"),
        ("WJ", "\u2060"),           # word joiner (security-3)
        ("INVISIBLE_PLUS", "\u2064"),
        ("TAG_CHAR", "\U000e0030"),  # Tag block (security-3)
        ("BOM", "\ufeff"),
        ("NUL", "\u0000"),          # C0 controls (except \t \n \r)
        ("BEL", "\u0007"),
        ("ESC", "\u001b"),
        ("DEL", "\u007f"),
        ("C1-SCI", "\u009f"),       # C1 control
    ],
)
@pytest.mark.parametrize("path", ["html", "translation"])
def test_hostile_chars_removed(path, label, ch):
    """Five classes (bidi override/embedding, zero-width, C0/C1 controls) must
    not survive either entry point."""
    dirty = f"abc{ch}def"
    if path == "html":
        out = _frag_text({"author": "T", "badges": [], "fragments": [{"type": "text", "text": dirty}]})
        # also the direct normalize_text surface
        assert not any(c in normalize_text(dirty) for c in ch)
    else:
        out = clean_translation_text(dirty)
    for hostile in ch:
        assert hostile not in out, f"{label} ({ascii(ch)}) survived {path} path: {ascii(out)}"
    assert out == "abcdef"


def test_crlf_tab_normalized_to_space():
    """Render wraps on spaces only; \n \r \t must not survive as literals."""
    dirty = "line1\nline2\rline3\tline4"
    assert "\n" not in normalize_text(dirty)
    assert "\r" not in normalize_text(dirty)
    assert "\t" not in normalize_text(dirty)
    assert normalize_text(dirty) == "line1 line2 line3 line4"


@pytest.mark.parametrize(
    "path,text,expected",
    [
        # html path: NFKC folds fullwidth forms (pre-existing behavior).
        ("html", "太强了/真厉害", "太强了/真厉害"),
        ("html", "全角标点：；，！？（ＡＢＣ）", "全角标点:;,!?(ABC)"),
        ("html", "kawaii desu ne~ ^_^", "kawaii desu ne~ ^_^"),
        ("html", "Hello, 世界! Mixing 🎉 emoji", "Hello, 世界! Mixing 🎉 emoji"),
        ("html", "12:30", "12:30"),
        ("html", "Score: 5-0", "Score: 5-0"),
        # translation path: no NFKC, fullwidth punctuation kept verbatim.
        ("translation", "太强了/真厉害", "太强了"),  # pre-existing alt-pair fold
        ("translation", "全角标点：；，！？（ＡＢＣ）", "全角标点：；，！？（ＡＢＣ）"),
        ("translation", "kawaii desu ne~ ^_^", "kawaii desu ne~ ^_^"),
        ("translation", "Hello, 世界! Mixing 🎉 emoji", "Hello, 世界! Mixing 🎉 emoji"),
        ("translation", "12:30", "12:30"),
        ("translation", "Score: 5-0", "Score: 5-0"),
    ],
)
def test_normal_text_unchanged(path, text, expected):
    """Normal text passes through with only pre-existing documented folds
    (NFKC on the html path, alt-pair selection on the translation path);
    the sanitizer must not change any visible character."""
    if path == "html":
        out = _frag_text({"author": "T", "badges": [], "fragments": [{"type": "text", "text": text}]})
    else:
        out = clean_translation_text(text)
    assert out == expected


def test_nfkc_behavior_not_collateral_damaged():
    """NFKC still folds compatibility glyphs; sanitizer adds no extra folding."""
    assert normalize_text("ﬁ") == "fi"          # NFKC compatibility fold kept
    assert normalize_text("Ａ") == "A"          # fullwidth -> ASCII kept
    assert normalize_text("太强了") == "太强了"  # CJK untouched
    # Sanitizer itself must not NFKC-fold anything.
    s = "太强了／：！"
    assert clean_translation_text(s) == s
    # NFKC never produces bidi/zero-width/controls: order (after NFKC) is safe.
    assert normalize_text("\u202e") == ""
    assert normalize_text("ok") == "ok"


def test_zwj_emoji_sequence_kept():
    """U+200D (ZWJ) is deliberately NOT removed: it has no display-spoofing
    capability and removing it would break emoji ZWJ ligature sequences."""
    assert normalize_text("😀\u200d🚀") == "😀\u200d🚀"
    assert normalize_text("🎉") == "🎉"
    assert normalize_text("👍🏽") == "👍🏽"  # skin-tone modifier kept


# --- security-3: author name + emote title placeholder paths -----------------
#
# Both used to bypass sanitize_render_text end-to-end: the author string was
# stored raw by the parser and drawn verbatim (compute_message_header_width →
# header["author"] → draw.text), and the missing-emote `[title]` placeholder
# interpolated the raw HTML title attribute. Both are sanitized at their
# render-side consumers now; chat_data.json content stays unchanged.

_FAKE_FONT_W = 8.0


def _tw(t):
    return len(t) * _FAKE_FONT_W


class _FakeBBoxFont:
    """Minimal font double for compute_message_header_width."""

    def getbbox(self, s: str):
        return (0, 0, len(s or "") * 8, 10)


def test_author_sanitized_in_header_width():
    """compute_message_header_width returns a sanitized author (draw path)."""
    header = compute_message_header_width(
        {"author": "user\u202eevil\u202c", "badges": []},
        padding=5,
        badge_size=9,
        gap=3,
        font=_FakeBBoxFont(),
        font_bold=_FakeBBoxFont(),
    )
    assert header["author"] == "userevil"
    assert "\u202e" not in header["author"] and "\u202c" not in header["author"]


@pytest.mark.parametrize(
    "label,ch",
    [
        ("RLO", "\u202e"),
        ("WJ", "\u2060"),
        ("INVISIBLE_PLUS", "\u2064"),
        ("TAG_CHAR", "\U000e0030"),
    ],
)
def test_emote_title_placeholder_sanitized(label, ch):
    """The [title] placeholder for missing emotes must not carry hostile chars."""
    dirty = f"em{ch}ote"
    frags = build_message_frag_list(
        {
            "author": "T",
            "badges": [],
            "fragments": [{"type": "emote", "class": "nope", "title": dirty}],
        },
        text_width_fn=_tw,
        emote_width_fn=lambda c: 20.0,
        emote_available_fn=lambda c: False,  # force the [title] placeholder
    )
    placeholders = [f[1] for f in frags if f[0] == "text"]
    assert placeholders == ["[emote]"], f"{label}: {placeholders!r}"
    assert ch not in "".join(placeholders)


def test_author_and_title_stay_raw_in_msg_dict():
    """Sanitization happens at render consumers: the message dict itself is
    not mutated (chat_data.json content must remain unchanged)."""
    msg = {
        "author": "user\u202eevil",
        "badges": [],
        "fragments": [{"type": "emote", "class": "nope", "title": "em\u202eote\u2060x"}],
    }
    build_message_frag_list(
        msg,
        text_width_fn=_tw,
        emote_width_fn=lambda c: 20.0,
        emote_available_fn=lambda c: False,
    )
    assert msg["author"] == "user\u202eevil"
    assert msg["fragments"][0]["title"] == "em\u202eote\u2060x"


def test_sanitize_strips_u2060_u2064_and_tag_block_keeps_zwj():
    """Direct sanitizer coverage: U+2060-U+2064 and Tag block U+E0000-U+E007F
    are invisible/format characters and get stripped; U+200D (ZWJ) is kept."""
    out = sanitize_render_text("a\u2060b\u2061c\u2062d\u2063e\u2064f")
    assert out == "abcdef"
    out2 = sanitize_render_text("a\U000e0000b\U000e0030c\U000e007fd")
    assert out2 == "abcd"
    # ZWJ retention is unchanged by the coverage extension.
    assert sanitize_render_text("x\u200dy") == "x\u200dy"
