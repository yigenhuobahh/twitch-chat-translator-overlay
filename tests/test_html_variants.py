#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parameterized TwitchDownloader / web HTML parse-variant tests (Batch A2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import FIXTURES_DIR, load_module


def _summarize(chat: dict) -> dict:
    msgs = chat.get("messages") or []
    authors = [m.get("author") for m in msgs]
    timestamps = [float(m.get("timestamp", 0)) for m in msgs]
    emote_titles = [
        frag.get("title")
        for m in msgs
        for frag in (m.get("fragments") or [])
        if frag.get("type") == "emote"
    ]
    emote_classes = [
        frag.get("class")
        for m in msgs
        for frag in (m.get("fragments") or [])
        if frag.get("type") == "emote"
    ]
    return {
        "count": len(msgs),
        "authors": authors,
        "timestamps": timestamps,
        "emote_titles": emote_titles,
        "emote_classes": emote_classes,
        "emote_map_keys": sorted((chat.get("emote_map") or {}).keys()),
    }


@pytest.mark.parametrize(
    "fixture_name",
    [
        "twitchdownloader_chat.html",
        "td_minified.html",
        "td_attr_reordered.html",
        "td_nested_span.html",
        "td_html_entities.html",
    ],
)
def test_twitchdownloader_core_variants_extract_same_core(fixture_name: str, tmp_path: Path):
    """Core 3-message TD fixture variants should keep authors/emotes/timestamps."""
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    html = FIXTURES_DIR / fixture_name
    assert html.is_file(), fixture_name
    chat = burn.parse_chat_html(str(html), str(tmp_path / fixture_name))
    summary = _summarize(chat)

    assert summary["count"] == 3, summary
    assert summary["timestamps"] == [1.0, 2.0, 3.0]
    # Authors: entities fixture decodes Al&ice -> Al&ice
    if fixture_name == "td_html_entities.html":
        assert summary["authors"][0] == "Al&ice"
    else:
        assert summary["authors"][0] == "Alice"
    assert summary["authors"][1] == "Bob"
    assert summary["authors"][2] == "Carol"
    assert "LUL" in summary["emote_titles"]
    assert "Hey" in summary["emote_titles"]
    assert "xdx" in summary["emote_titles"]
    assert any(str(c).startswith("first-emotesv2_") for c in summary["emote_classes"])
    assert any(str(c).startswith("third-") for c in summary["emote_classes"])
    # CSS base64 emotes extracted for first-/third- classes
    assert len(summary["emote_map_keys"]) >= 3

    # Nested span should not drop surrounding text.
    if fixture_name == "td_nested_span.html":
        texts = [
            frag.get("text", "")
            for frag in chat["messages"][0]["fragments"]
            if frag.get("type") == "text"
        ]
        joined = " ".join(texts)
        assert "hello" in joined
        assert "nested" in joined
        assert "world" in joined


def test_missing_timestamp_message_is_skipped(tmp_path: Path):
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    html = FIXTURES_DIR / "td_missing_timestamp.html"
    chat = burn.parse_chat_html(str(html), str(tmp_path / "miss_ts"))
    summary = _summarize(chat)
    assert summary["count"] == 1
    assert summary["authors"] == ["Dave"]
    assert summary["timestamps"] == [5.0]
    assert "LUL" in summary["emote_titles"]
    assert "SkipMe" not in summary["authors"]


def test_legacy_twitch_web_html_format(tmp_path: Path):
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    html = FIXTURES_DIR / "twitch_web_chat.html"
    chat = burn.parse_chat_html(str(html), str(tmp_path / "web"))
    summary = _summarize(chat)
    assert summary["count"] == 1
    assert summary["authors"] == ["WebUser"]
    assert summary["timestamps"] == [1.5]  # data-timestamp ms -> seconds
    assert any(t == "Kappa" for t in summary["emote_titles"]) or any(
        "first-999" in str(c) for c in summary["emote_classes"]
    )
