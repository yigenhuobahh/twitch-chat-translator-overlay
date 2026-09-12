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


def _make_html(t_param: str) -> str:
    """Minimal single-message TD-format chat export with the given ?t= query."""
    return (
        '<html><body><pre class="comment-root">'
        f'[<a href="https://www.twitch.tv/videos/1{t_param}">link</a>] '
        '<span class="comment-author">User</span>'
        '<span class="comment-message">: hello</span></pre></body></html>'
    )


def test_timestamp_hour_minute_multipliers_nonzero(tmp_path: Path):
    """tests-1 (a): h/m 倍率 pin —— ?t=1h2m3s 必须解析为 3723.0。

    Mutation evidence (verifier-confirmed): h*3600→h*60 and m*60→m*61 both
    survive the full 94-test parser subset because every fixture uses
    t=0h0mNs. This nonzero case pins both multipliers simultaneously
    (1*3600 + 2*60 + 3 = 3723; under the mutations it would be 125/3724).
    """
    data = _parse(tmp_path, _make_html("?t=1h2m3s"), "hm_mult.html")
    assert len(data["messages"]) == 1
    assert data["messages"][0]["timestamp"] == 3723.0


def test_timestamp_extra_query_and_hash_after_t(tmp_path: Path):
    """tests-1 (b): t= 之后的额外 query/hash 按正则文档化后缀规则允许。

    ?t=0h2m3s&foo=1#chat → 2m3s = 192... 即 0*3600 + 2*60 + 3 = 123 秒。
    (The regex's documented suffix allowance is [&#'\"] or end-of-href; the
    href here continues with &foo=1#chat so both separators are exercised.)
    """
    data = _parse(tmp_path, _make_html("?t=0h2m3s&foo=1#chat"), "suffix.html")
    assert len(data["messages"]) == 1
    assert data["messages"][0]["timestamp"] == 123.0


def test_timestamp_invalid_unit_suffix_drops_message(tmp_path: Path):
    """tests-1 (c): ?t=0h0m5x 不是合法时间链接 → 整条消息按缺时间戳语义丢弃。

    `_extract_comment_root_fields` returns None when time_link_pattern does
    not match, so `5x` (x is not in the allowed terminator set) must leave
    zero messages — pinning that the pattern does not loosen to accept
    malformed suffixes.
    """
    data = _parse(tmp_path, _make_html("?t=0h0m5x"), "bad_suffix.html")
    assert data["messages"] == []


def test_emote_class_substring_is_not_the_emote_image_token(tmp_path: Path):
    """tests-7: `emote-image-2` 含 `emote-image` 子串但不是精确 token。

    `_class_has_token` 按 whitespace 分词后做精确匹配:class="emote-image-2
    first-1" 的 token 列表里没有 `emote-image`,因此该 <img> 不得产出
    emote fragment(即便 "first-1" 本身命中 _EMOTE_PREFIXES 也不行)。
    """
    html = (
        "<html><body><pre class=\"comment-root\">"
        "[<a href=\"https://www.twitch.tv/videos/1?t=0h0m5s\">0:00:05</a>] "
        "<span class=\"comment-author\">User</span>"
        "<span class=\"comment-message\">: hi "
        '<img class="emote-image-2 first-1" title="X">'
        "</span></pre></body></html>"
    )
    data = _parse(tmp_path, html, "substring_class.html")
    assert len(data["messages"]) == 1
    msg = data["messages"][0]
    emotes = [f for f in msg["fragments"] if f["type"] == "emote"]
    assert emotes == []
    # img 标签被剥离后正文文本原样保留。
    texts = [f["text"] for f in msg["fragments"] if f["type"] == "text"]
    assert texts == ["hi"]


def test_parse_progress_without_total_precount(tmp_path, capsys):
    """perf-6:进度不再为总数预扫 body;收尾报最终块数(无百分比模式)。"""
    parser = load_module("chat_parser", "chat_parser.py")
    html = FIXTURES_DIR / "td_author_class_single_quote.html"
    data = parser.parse_chat_html(str(html), str(tmp_path / "perf6_progress"))
    assert len(data["messages"]) == 1
    out = capsys.readouterr().out
    assert "消息块约" not in out, "总数预计数扫描已删,不应再报约数"
    assert "切分 comment-root" in out
    assert "解析消息块: 1 " in out, "收尾 force tick 报最终块数(无百分比模式)"
