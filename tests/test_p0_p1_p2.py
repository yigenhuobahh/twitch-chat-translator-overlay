#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Strict tests for P0/P1/P2 features: window filter, offset, cache, errors, lint review."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def test_preview_window_and_filter_keeps_only_relevant_messages():
    from chat_window import filter_chat_for_time_window, preview_window

    chat = {
        "messages": [
            {"timestamp": 0.0, "fragments": [{"type": "text", "text": "early"}], "author": "a"},
            {
                "timestamp": 50.0,
                "fragments": [
                    {"type": "text", "text": "mid"},
                    {"type": "emote", "class": "first-1", "title": "LUL"},
                ],
                "author": "b",
            },
            {
                "timestamp": 200.0,
                "fragments": [
                    {"type": "text", "text": "late"},
                    {"type": "emote", "class": "first-2", "title": "Pog"},
                ],
                "author": "c",
            },
        ],
        "emote_map": {
            "first-1": "e1.png",
            "first-2": "e2.png",
            "first-unused": "e3.png",
        },
    }
    start, end = preview_window(preview_frame=55.0, preview_clip=None, msg_lifetime=14.0)
    assert start == pytest.approx(41.0)
    assert end == pytest.approx(55.05)
    filtered = filter_chat_for_time_window(chat, start, end, msg_lifetime=14.0)
    assert len(filtered["messages"]) == 1
    assert filtered["messages"][0]["timestamp"] == 50.0
    assert set(filtered["emote_map"]) == {"first-1"}
    assert filtered["_window"]["source_messages"] == 3


def test_preview_clip_window_includes_alive_messages_into_clip():
    from chat_window import filter_chat_for_time_window, preview_window

    chat = {
        "messages": [
            {"timestamp": -1.0, "fragments": [{"type": "text", "text": "before"}], "author": "a"},
            {"timestamp": 1.0, "fragments": [{"type": "text", "text": "in"}], "author": "b"},
            {"timestamp": 20.0, "fragments": [{"type": "text", "text": "after"}], "author": "c"},
        ],
        "emote_map": {},
    }
    # timestamps are non-negative in real data; still test overlap logic
    chat["messages"][0]["timestamp"] = 0.0
    start, end = preview_window(None, preview_clip=3.0, msg_lifetime=14.0)
    filtered = filter_chat_for_time_window(chat, start, end, 14.0)
    # message at 0 and 1 can appear in [0,3]; 20 cannot
    assert [m["timestamp"] for m in filtered["messages"]] == [0.0, 1.0]


def test_compute_time_offset_auto_and_manual():
    from chat_window import apply_time_offset, compute_time_offset

    messages = [
        {"timestamp": 1000.0},
        {"timestamp": 1100.0},
    ]
    auto = compute_time_offset(messages, video_duration=120.0, manual_offset=None)
    assert auto["mode"] == "auto"
    assert auto["offset"] == 1000.0
    assert auto["confirm_with_preview"] is True

    manual = compute_time_offset(messages, video_duration=120.0, manual_offset=12.5)
    assert manual["mode"] == "manual"
    assert manual["offset"] == 12.5

    apply_time_offset(messages, 1000.0)
    assert messages[0]["timestamp"] == 0.0
    assert messages[1]["timestamp"] == 100.0


def test_translation_error_classification_and_backoff():
    from translation_support import TranslationErrorKind, backoff_seconds, classify_api_error

    class FakeHTTPError(Exception):
        def __init__(self, status_code, msg="err"):
            super().__init__(msg)
            self.status_code = status_code
            self.response = type("R", (), {"headers": {"Retry-After": "7"}, "status_code": status_code})()

    assert classify_api_error(FakeHTTPError(429, "rate limit")) == TranslationErrorKind.RATE_LIMIT
    assert classify_api_error(FakeHTTPError(401, "unauthorized")) == TranslationErrorKind.AUTH
    assert classify_api_error(TimeoutError("timed out")) == TranslationErrorKind.TIMEOUT
    assert classify_api_error(ValueError("json decode boom")) == TranslationErrorKind.BAD_JSON
    assert backoff_seconds(TranslationErrorKind.RATE_LIMIT, 0, FakeHTTPError(429)) == 7.0
    assert backoff_seconds(TranslationErrorKind.AUTH, 0) == 0.0
    assert backoff_seconds(TranslationErrorKind.SERVER, 1) == 20.0


def test_translation_cache_roundtrip(tmp_path: Path):
    from translation_support import TranslationCache

    cache = TranslationCache(tmp_path / "cache")
    assert cache.get("hello", "zh", "m1", "ctx") is None
    cache.put("hello", "zh", "m1", "ctx", "你好")
    assert cache.get("hello", "zh", "m1", "ctx") == "你好"
    assert cache.get("hello", "ja", "m1", "ctx") is None


def test_overlay_config_dataclass():
    from overlay_config import OverlayConfig

    cfg = OverlayConfig(fps=24, width=100, height=200)
    d = cfg.to_dict()
    assert d["fps"] == 24
    assert d["width"] == 100
    assert cfg.emote_h == 22


def test_run_meta_write(tmp_path: Path):
    from run_meta import mark_run_status, write_run_meta

    write_run_meta(tmp_path, {"status": "running", "fps": 30})
    mark_run_status(tmp_path, "failed", stage="compose")
    data = json.loads((tmp_path / "run_meta.json").read_text(encoding="utf-8"))
    assert data["status"] == "failed"
    assert data["stage"] == "compose"
    assert data["fps"] == 30


def test_clean_translation_preserves_tokens_and_urls():
    import translate_chat_openai as tr

    cases = {
        "https://example.com/a/b": "https://example.com/a/b",
        "看这里 https://twitch.tv/foo": "看这里 https://twitch.tv/foo",
        "and/or": "and/or",
        "A/B测试": "A/B测试",
        "太强了/真厉害": "太强了",
        "@User123 hello": "@User123 hello",
        "[LUL] nice": "[LUL] nice",
        "C:\\Users\\foo/bar": "C:\\Users\\foo/bar",
    }
    for src, expected in cases.items():
        assert tr.clean_translation_text(src) == expected, src


def test_review_export_includes_lint_columns(tmp_path: Path):
    import render_cn_chat as pipeline

    src = {
        "messages": [
            {
                "index": 0,
                "timestamp": 1.0,
                "author": "alice",
                "original": "hello @bob https://example.com/x [LUL]",
                "translation": "你好",  # missing mention/url/bracket intentionally
            },
            {
                "index": 1,
                "timestamp": 2.0,
                "author": "bob",
                "original": "[Hey]",
                "translation": "[Hey]",
            },
        ]
    }
    json_path = tmp_path / "t.json"
    json_path.write_text(json.dumps(src, ensure_ascii=False, indent=2), encoding="utf-8")
    tsv = tmp_path / "r.tsv"
    xlsx = tmp_path / "r.xlsx"
    pipeline.export_review_tsv(json_path, tsv)
    header = tsv.read_text(encoding="utf-8-sig").splitlines()[0].split("\t")
    assert header[:5] == ["index", "timestamp", "author", "original", "translation"]
    assert "lint_severity" in header
    assert "lint_codes" in header

    pipeline.export_review_xlsx(json_path, xlsx)
    assert xlsx.is_file()
    # round-trip import still works with extra columns
    src["messages"][0]["translation"] = "旧"
    json_path.write_text(json.dumps(src, ensure_ascii=False, indent=2), encoding="utf-8")
    # rewrite translation via tsv
    lines = tsv.read_text(encoding="utf-8-sig").splitlines()
    parts = lines[1].split("\t")
    parts[4] = "新译文"
    lines[1] = "\t".join(parts)
    tsv.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    pipeline.import_review_tsv(json_path, tsv)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["messages"][0]["translation"] == "新译文"


def test_lint_detects_token_loss():
    import contextlib
    import io

    import render_cn_chat as pipeline

    # Use fixtures dirty or inline
    payload = {
        "messages": [
            {
                "index": 0,
                "original": "hi @User https://a.com [Pog]",
                "translation": "嗨",
            }
        ]
    }
    p = Path(__file__).resolve().parent / "fixtures" / "_tmp_lint.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            issues = pipeline.lint_translation(p)
        codes = {i["code"] for i in issues}
        assert "mention_lost" in codes
        assert "url_lost" in codes
        assert "bracket_token_lost" in codes
    finally:
        try:
            p.unlink()
        except OSError:
            pass
