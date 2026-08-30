#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""--mode render 守卫、lint 短路、翻译 JSON 原子写与 pause-S 终态契约测试。"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import render_cn_chat as pipe


def _render_args(**overrides) -> SimpleNamespace:
    base = dict(
        mode="render",
        render_original=False,
        reuse_translation=False,
        skip_translate=False,
        manual_translation=False,
        review=False,
        review_done=False,
        lint_translation=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_inputs(tmp_path: Path) -> tuple[Path, Path]:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00\x00")
    html = tmp_path / "chat.html"
    html.write_text("<html></html>", encoding="utf-8")
    return video, html


def _write_trans_json(path: Path, *, fail: bool) -> Path:
    if fail:
        # 缺 translation 字段 -> lint FAIL
        messages: list[dict] = [{"index": 0, "original": "hi"}]
    else:
        messages = [{"index": 0, "original": "[LUL]", "translation": "[LUL]"}]
    path.write_text(json.dumps({"messages": messages}, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture()
def pipeline_mocks(monkeypatch):
    """拦截子进程与媒体检查，记录 run() 调用，保证测试不会触发真实渲染/翻译。"""
    calls: list[list[str]] = []

    def _record_run(cmd, *args, **kwargs):
        calls.append([str(c) for c in cmd])

    monkeypatch.setattr(pipe, "validate_source_media", lambda *a, **k: None)
    monkeypatch.setattr(pipe, "resolve_font_paths", lambda *a, **k: ("font.ttf", "font-bold.ttf"))
    monkeypatch.setattr(pipe, "run", _record_run)
    return calls


# ---------------------------------------------------------------------------
# C2: LINT_PURE_EMOTE_RE 语义 + ReDoS 回归
# ---------------------------------------------------------------------------


def test_lint_pure_emote_re_matches_pure_emote_sequences():
    for text in ("[LUL]", "  [LUL]  ", "[a] [b] [c]", "[a]   [b]", "\t[x]\n[y] ", "[Hey]"):
        assert pipe.LINT_PURE_EMOTE_RE.fullmatch(text), text


def test_lint_pure_emote_re_rejects_plain_words_and_numbers():
    for text in ("", "   ", "hello [a]", "[a] world", "123", "[a] 123", "翻译 [a]", "[a]x", "x[a]"):
        assert pipe.LINT_PURE_EMOTE_RE.fullmatch(text) is None, text


def test_lint_pure_emote_re_no_catastrophic_backtracking():
    # 旧正则 (?:\s*\[[^\]]+\]\s*)+ 对"尾部未闭合 token"输入呈指数回溯：
    # n=26 约 5 秒、n=30 约 81 秒；消歧空白后应为微秒级。
    payload = "[a] " * 26 + "[b"
    t0 = time.perf_counter()
    assert pipe.LINT_PURE_EMOTE_RE.fullmatch(payload) is None
    elapsed = time.perf_counter() - t0
    assert elapsed < 2.0, f"疑似灾难性回溯: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# PIPE-R1: --mode render 守卫不得绕过翻译 API
# ---------------------------------------------------------------------------


def test_render_mode_guard_rejects_review_flag():
    # --review 在不带 --reuse-translation 时会走"导出 JSON -> API probe -> LLM 翻译"
    with pytest.raises(pipe.PipelineError):
        pipe.apply_mode_defaults(_render_args(review=True))


def test_render_mode_guard_rejects_pipeline_lint_sentinel():
    # --lint-translation 不带值（sentinel）在守卫层面同样不能豁免调 API
    with pytest.raises(pipe.PipelineError):
        pipe.apply_mode_defaults(_render_args(lint_translation="__PIPELINE__"))


def test_render_mode_guard_still_allows_reuse_and_review_done():
    applied = pipe.apply_mode_defaults(_render_args(reuse_translation=True, review_done=True))
    assert "render_only_guard" in applied


def test_render_mode_with_explicit_lint_path_short_circuits_to_lint_only():
    args = _render_args(lint_translation="given/translation.json")
    applied = pipe.apply_mode_defaults(args)
    assert "render_lint_only" in applied
    assert getattr(args, "_mode_render_lint_only", False) is True


def test_render_mode_lint_path_only_lints_given_file(monkeypatch, tmp_path, pipeline_mocks):
    """--mode render --lint-translation <路径>：只质检指定文件，退出码=质检结果。"""
    video, html = _make_inputs(tmp_path)
    user_json = _write_trans_json(tmp_path / "given.json", fail=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "render_cn_chat.py",
            str(video),
            str(html),
            "--mode",
            "render",
            "--lint-translation",
            str(user_json),
            "--translation-json",
            str(tmp_path / "trans.json"),
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        pipe.main()
    assert excinfo.value.code == 0
    assert pipeline_mocks == [], "lint 短路不应导出/翻译/渲染"


def test_render_mode_lint_path_fails_with_exit_1(monkeypatch, tmp_path, pipeline_mocks):
    video, html = _make_inputs(tmp_path)
    user_json = _write_trans_json(tmp_path / "given.json", fail=True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "render_cn_chat.py",
            str(video),
            str(html),
            "--mode",
            "render",
            "--lint-translation",
            str(user_json),
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        pipe.main()
    assert excinfo.value.code == 1
    assert pipeline_mocks == []


def test_pipeline_lint_uses_user_specified_json_not_fresh_export(monkeypatch, tmp_path, pipeline_mocks):
    """pipeline 中 --lint-translation 带路径时必须检查用户指定文件，而不是新导出的 trans_json。"""
    video, html = _make_inputs(tmp_path)
    trans = _write_trans_json(tmp_path / "trans.json", fail=False)  # 新导出的 JSON 干净
    user_json = _write_trans_json(tmp_path / "given.json", fail=True)  # 用户指定的有 FAIL
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "render_cn_chat.py",
            str(video),
            str(html),
            "--reuse-translation",
            "--translation-json",
            str(trans),
            "--lint-translation",
            str(user_json),
        ],
    )
    with pytest.raises(pipe.PipelineError, match="翻译质检存在 FAIL"):
        pipe.main()
    assert pipeline_mocks == [], "质检 FAIL 后不应继续渲染"


def test_render_mode_sentinel_lint_checks_pipeline_trans_json(monkeypatch, tmp_path, pipeline_mocks):
    """--lint-translation 不带值（sentinel）时仍检查本次流水线的 trans_json。"""
    video, html = _make_inputs(tmp_path)
    trans = _write_trans_json(tmp_path / "trans.json", fail=True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "render_cn_chat.py",
            str(video),
            str(html),
            "--mode",
            "render",
            "--reuse-translation",
            "--translation-json",
            str(trans),
            "--lint-translation",
        ],
    )
    with pytest.raises(pipe.PipelineError, match="翻译质检存在 FAIL"):
        pipe.main()
    assert pipeline_mocks == []


# ---------------------------------------------------------------------------
# PIPE-R2: 翻译 JSON 原子写
# ---------------------------------------------------------------------------


def test_atomic_write_json_roundtrip_and_no_tmp_left(tmp_path):
    target = tmp_path / "sub" / "trans.json"
    data = {"messages": [{"index": 0, "original": "[LUL]", "translation": "[LUL]"}]}
    pipe.atomic_write_json(target, data)
    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8")) == data
    # 与原 write_text(json.dumps(..., ensure_ascii=False, indent=2)) 格式一致
    assert target.read_text(encoding="utf-8") == json.dumps(data, ensure_ascii=False, indent=2)
    leftovers = [p.name for p in target.parent.iterdir() if p.name != target.name]
    assert leftovers == [], f"残留临时文件: {leftovers}"


def test_atomic_write_json_keeps_original_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "trans.json"
    target.write_text("original-content", encoding="utf-8")

    def _boom(*args, **kwargs):
        raise OSError("simulated crash before replace")

    monkeypatch.setattr(pipe.os, "replace", _boom)
    with pytest.raises(OSError):
        pipe.atomic_write_json(target, {"messages": []})
    assert target.read_text(encoding="utf-8") == "original-content"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != target.name]
    assert leftovers == [], f"残留临时文件: {leftovers}"


# ---------------------------------------------------------------------------
# PIPE-R3: 翻译后交互确认选择 S 停止 -> 终态 manual_required
# ---------------------------------------------------------------------------


def test_pause_stop_publishes_manual_required(monkeypatch, tmp_path, pipeline_mocks):
    video, html = _make_inputs(tmp_path)
    trans = tmp_path / "trans.json"  # 导出被 mock，无需真实存在
    manifest_path = tmp_path / "pause_stop.result.json"

    monkeypatch.setattr(pipe, "_export_translation_json", lambda **kwargs: None)
    monkeypatch.setattr(pipe, "ensure_translate_api_or_fallback", lambda **kwargs: "api")
    monkeypatch.setattr(pipe, "export_review_tsv", lambda *a, **k: None)
    monkeypatch.setattr(pipe, "export_review_xlsx", lambda *a, **k: None)
    monkeypatch.setattr(pipe, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "s")
    monkeypatch.setenv("TWITCH_OVERLAY_RESULT_FILE", str(manifest_path))
    monkeypatch.setattr(
        sys,
        "argv",
        ["render_cn_chat.py", str(video), str(html), "--translation-json", str(trans)],
    )

    pipe.main()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["state"] == "manual_required", manifest
    # 只允许发生翻译器调用，渲染（burn）绝不能启动
    assert len(pipeline_mocks) == 1, pipeline_mocks
