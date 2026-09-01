#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regressions for HTML parser silent-wrong audit (P0/P1)."""

from __future__ import annotations

from pathlib import Path

from helpers import FIXTURES_DIR, load_module

TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _parse(tmp_path: Path, html: str, name: str = "chat.html"):
    parser = load_module("chat_parser", "chat_parser.py")
    p = tmp_path / name
    if isinstance(html, bytes):
        p.write_bytes(html)
    else:
        p.write_text(html, encoding="utf-8")
    return parser.parse_chat_html(str(p), str(tmp_path / f"out_{name}"))


def test_author_class_single_quote_fixture(tmp_path: Path):
    parser = load_module("chat_parser", "chat_parser.py")
    html = FIXTURES_DIR / "td_author_class_single_quote.html"
    data = parser.parse_chat_html(str(html), str(tmp_path / "sq_author"))
    assert len(data["messages"]) == 1
    assert data["messages"][0]["author"] == "Alice"
    assert data["messages"][0]["timestamp"] == 1
    texts = [f["text"] for f in data["messages"][0]["fragments"] if f["type"] == "text"]
    assert any("hello" in t for t in texts)


def test_emote_attrs_single_quote_fixture(tmp_path: Path):
    parser = load_module("chat_parser", "chat_parser.py")
    html = FIXTURES_DIR / "td_emote_attrs_single_quote.html"
    data = parser.parse_chat_html(str(html), str(tmp_path / "sq_emote"))
    assert len(data["messages"]) == 1
    emotes = [f for f in data["messages"][0]["fragments"] if f["type"] == "emote"]
    assert len(emotes) == 1
    assert emotes[0]["class"] == "first-1"
    assert emotes[0]["title"] == "LUL"
    # Must not leak text-hide plain text as body text
    texts = [f.get("text", "") for f in data["messages"][0]["fragments"] if f["type"] == "text"]
    assert not any(t.strip() == "LUL" for t in texts)


def test_emote_class_extra_tokens_fixture(tmp_path: Path):
    parser = load_module("chat_parser", "chat_parser.py")
    html = FIXTURES_DIR / "td_emote_class_extra_tokens.html"
    data = parser.parse_chat_html(str(html), str(tmp_path / "extra_tok"))
    assert len(data["messages"]) == 1
    emotes = [f for f in data["messages"][0]["fragments"] if f["type"] == "emote"]
    assert len(emotes) == 1
    assert emotes[0]["class"] == "first-1"
    assert emotes[0]["title"] == "LUL"


def test_badge_mixed_quotes_fixture(tmp_path: Path):
    parser = load_module("chat_parser", "chat_parser.py")
    html = FIXTURES_DIR / "td_badge_mixed_quotes.html"
    data = parser.parse_chat_html(str(html), str(tmp_path / "badge_mix"))
    assert len(data["messages"]) == 1
    titles = [b["title"] for b in data["messages"][0]["badges"]]
    assert "Broadcaster" in titles
    assert "Subscriber" in titles


def test_comment_message_deleted_multi_class_fixture(tmp_path: Path):
    parser = load_module("chat_parser", "chat_parser.py")
    html = FIXTURES_DIR / "td_comment_message_deleted.html"
    data = parser.parse_chat_html(str(html), str(tmp_path / "deleted"))
    assert len(data["messages"]) == 2
    authors = [m["author"] for m in data["messages"]]
    assert authors == ["Alice", "System"]
    alice_texts = [
        f["text"]
        for f in data["messages"][0]["fragments"]
        if f["type"] == "text"
    ]
    assert any("message deleted" in t for t in alice_texts)


def test_css_class_far_from_url_lookback(tmp_path: Path):
    parser = load_module("chat_parser", "chat_parser.py")
    html = FIXTURES_DIR / "td_css_class_far_from_url.html"
    data = parser.parse_chat_html(str(html), str(tmp_path / "far"))
    assert len(data["messages"]) == 1
    emotes = [f for f in data["messages"][0]["fragments"] if f["type"] == "emote"]
    assert emotes and emotes[0]["class"] == "first-far"
    assert "first-far" in data["emote_map"]
    assert Path(data["emote_map"]["first-far"]).is_file()


def test_non_utf8_latin1_does_not_crash(tmp_path: Path):
    # latin-1 residual for José / café
    body = (
        b'<html><body><pre class="comment-root">[<a href="https://www.twitch.tv/videos/1?t=0h0m1s">0:00:01</a>] '
        b'<span class="comment-author">Jos\xe9</span>'
        b'<span class="comment-message">: caf\xe9</span></pre></body></html>'
    )
    data = _parse(tmp_path, body, "latin1.html")
    assert len(data["messages"]) == 1
    # With utf-8 errors=replace, non-utf8 bytes become U+FFFD; either way no crash.
    assert data["messages"][0]["author"]
    texts = [f["text"] for f in data["messages"][0]["fragments"] if f["type"] == "text"]
    assert texts


def test_inline_single_quote_author_and_emote(tmp_path: Path):
    html = (
        f"<html><style>.first-1{{content:url(\"data:image/png;base64,{TINY_PNG_B64}\")}}</style>"
        "<pre class=\"comment-root\">[<a href=\"https://twitch.tv/x?t=0h0m5s\">0:00:05</a>] "
        "<span class='comment-author' style='color: #0f0'>User</span>"
        "<span class='comment-message'>: hi "
        "<img class='emote-image first-1 animated' title='LUL'>"
        "<span class='text-hide'>LUL</span> there</span></pre></html>"
    )
    data = _parse(tmp_path, html, "combo.html")
    assert len(data["messages"]) == 1
    msg = data["messages"][0]
    assert msg["author"] == "User"
    assert msg["timestamp"] == 5
    assert msg["color"].strip() in ("#0f0", "#0f0 ")
    emotes = [f for f in msg["fragments"] if f["type"] == "emote"]
    assert len(emotes) == 1 and emotes[0]["title"] == "LUL"
    texts = " ".join(f["text"] for f in msg["fragments"] if f["type"] == "text")
    assert "hi" in texts and "there" in texts
