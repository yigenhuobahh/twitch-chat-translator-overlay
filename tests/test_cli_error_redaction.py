#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI 错误输出脱敏回归（0905 e7f4b86 修复只覆盖了 TUI/wizard 分支的补集）。

两条 CLI 路径曾把含凭据的错误原文打到终端：
  1. download_flow.run_download_flow 的 TwitchDownloadError / 通用 Exception
     分支直接 print 异常文本——源 URL（userinfo@host / ?oauth=）会被
     parse_twitch_source 等原样回显。
  2. render_cn_chat.ensure_translate_api_or_fallback 打印
     probe_translate_api 的失败 reason，并把它传进
     _fallback_manual_after_export——服务端错误文本可能回显请求 URL/头。

断言口径：凭据标记（user:secret / oauth=leak）不得出现在任何捕获输出里，
且输出含 [redacted]。不测单引号 dict-repr 形态（另一条修复线的范围）。
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from types import SimpleNamespace

import pytest


@pytest.fixture()
def pipeline():
    import render_cn_chat as pipeline
    return pipeline


@pytest.fixture()
def download_flow_module():
    import download_flow as download_flow_module
    return download_flow_module


def _captured(fn):
    """Run fn() capturing stdout+stderr; return (result, combined_text)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        result = fn()
    return result, out.getvalue() + err.getvalue()


def _assert_redacted(combined: str) -> None:
    assert "user:secret" not in combined
    assert "oauth=leak" not in combined
    assert "[redacted]" in combined


# ---------------------------------------------------------------- download_flow

def test_download_flow_twitch_download_error_redacted(download_flow_module, monkeypatch):
    """TwitchDownloadError 分支：含凭据的源 URL 进 stderr 前必须脱敏。"""
    import twitch_download

    leaky = "下载失败: https://user:secret@evil.example/p?oauth=leak"

    def _boom(*_args, **_kwargs):
        raise twitch_download.TwitchDownloadError(leaky)

    monkeypatch.setattr(twitch_download, "download_assets", _boom)
    events: list[str] = []
    args = SimpleNamespace(
        download="https://www.twitch.tv/videos/123456789",
        download_dir=None,
        segment=[],
        cut=[],
        begin=None,
        end=None,
        kind="auto",
        quality=None,
        oauth=None,
        download_output_fps=None,
        download_encoder="auto",
        download_trim_mode="Safe",
        media_check="fast",
        media_repair="audio",
        dry_run=False,
        download_only=False,
        yes=False,
    )

    code, combined = _captured(
        lambda: download_flow_module.run_download_flow(
            args, emit=lambda kind, **_kw: events.append(kind), next_steps=lambda *_a, **_kw: 0,
        )
    )

    assert code == 2
    assert events == ["stage_started", "stage_failed"]
    _assert_redacted(combined)


def test_download_flow_generic_exception_redacted(download_flow_module, monkeypatch):
    """通用 Exception 分支（错误: 下载失败: …）同样必须脱敏。"""
    import twitch_download

    def _boom(*_args, **_kwargs):
        raise RuntimeError("https://user:secret@evil.example/p?oauth=leak")

    monkeypatch.setattr(twitch_download, "download_assets", _boom)
    events: list[str] = []
    args = SimpleNamespace(
        download="https://www.twitch.tv/videos/123456789",
        download_dir=None,
        segment=[],
        cut=[],
        begin=None,
        end=None,
        kind="auto",
        quality=None,
        oauth=None,
        download_output_fps=None,
        download_encoder="auto",
        download_trim_mode="Safe",
        media_check="fast",
        media_repair="audio",
        dry_run=False,
        download_only=False,
        yes=False,
    )

    code, combined = _captured(
        lambda: download_flow_module.run_download_flow(
            args, emit=lambda kind, **_kw: events.append(kind), next_steps=lambda *_a, **_kw: 0,
        )
    )

    assert code == 1
    assert events == ["stage_started", "stage_failed"]
    assert "错误: 下载失败:" in combined
    _assert_redacted(combined)


# ------------------------------------------------------- render_cn_chat probe

_PROBE_MSG = "API 不可用: Error code: 401 - auth failed for https://user:secret@mirror.example/v1?oauth=leak"


def test_cli_probe_failure_print_and_fallback_reason_redacted(pipeline, monkeypatch, tmp_path):
    """CLI 探测失败：print 与 _fallback_manual_after_export 的 reason 都要脱敏。

    收口修复：ensure_translate_api_or_fallback 在拿到失败 msg 后统一
    redact_text 一次，print 与 fallback 分支共用同一变量。
    """
    monkeypatch.setattr(
        pipeline, "probe_translate_api", lambda *_a, **_kw: (False, _PROBE_MSG)
    )
    monkeypatch.setattr(pipeline, "export_review_tsv", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "export_review_xlsx", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_stdin_is_interactive", lambda: False)

    trans_json = tmp_path / "trans.json"
    trans_json.write_text('{"messages": []}', encoding="utf-8")

    code, combined = _captured(
        lambda: pipeline.ensure_translate_api_or_fallback(
            video=tmp_path / "v.mp4",
            chat_html=tmp_path / "c.html",
            trans_json=trans_json,
            review_tsv=tmp_path / "review.tsv",
            review_xlsx=tmp_path / "review.xlsx",
            workdir=tmp_path,
            final_output=tmp_path / "out.mp4",
            yes=True,
        )
    )

    assert code == "manual"
    assert "[!] 翻译 API 不可用:" in combined
    # fallback reason（log 出来的 "[翻译 API] …" 行）同样来自脱敏后的 msg
    assert "[翻译 API]" in combined
    _assert_redacted(combined)


def test_cli_probe_failure_interactive_fallback_reason_redacted(pipeline, monkeypatch, tmp_path):
    """交互分支选 [C] 继续时，fallback reason 也走同一条脱敏后的 msg。"""
    monkeypatch.setattr(
        pipeline, "probe_translate_api", lambda *_a, **_kw: (False, _PROBE_MSG)
    )
    monkeypatch.setattr(pipeline, "export_review_tsv", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "export_review_xlsx", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a: "c")

    trans_json = tmp_path / "trans.json"
    trans_json.write_text('{"messages": []}', encoding="utf-8")

    code, combined = _captured(
        lambda: pipeline.ensure_translate_api_or_fallback(
            video=tmp_path / "v.mp4",
            chat_html=tmp_path / "c.html",
            trans_json=trans_json,
            review_tsv=tmp_path / "review.tsv",
            review_xlsx=tmp_path / "review.xlsx",
            workdir=tmp_path,
            final_output=tmp_path / "out.mp4",
            yes=False,
        )
    )

    assert code == "manual"
    _assert_redacted(combined)


def test_cli_probe_retry_after_redaction_still_redacts(pipeline, monkeypatch, tmp_path):
    """R 重试后再失败（新一次探测返回原始文本）仍要脱敏——脱敏在循环体内。"""
    calls = {"n": 0}

    def _probe(*_a, **_kw):
        calls["n"] += 1
        return (False, _PROBE_MSG)

    monkeypatch.setattr(pipeline, "probe_translate_api", _probe)
    monkeypatch.setattr(pipeline, "export_review_tsv", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "export_review_xlsx", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a: "r" if calls["n"] == 1 else "c")

    trans_json = tmp_path / "trans.json"
    trans_json.write_text('{"messages": []}', encoding="utf-8")

    code, combined = _captured(
        lambda: pipeline.ensure_translate_api_or_fallback(
            video=tmp_path / "v.mp4",
            chat_html=tmp_path / "c.html",
            trans_json=trans_json,
            review_tsv=tmp_path / "review.tsv",
            review_xlsx=tmp_path / "review.xlsx",
            workdir=tmp_path,
            final_output=tmp_path / "out.mp4",
            yes=False,
        )
    )

    assert calls["n"] == 2
    assert code == "manual"
    _assert_redacted(combined)


# ------------------------------------------------------- success path sanity

def test_cli_probe_success_path_untouched(pipeline, monkeypatch, capsys):
    """探测成功分支不应被脱敏逻辑影响（msg 原样 log）。"""
    monkeypatch.setattr(
        pipeline, "probe_translate_api", lambda *_a, **_kw: (True, "API 可达 (https://ok.example, model=m)")
    )

    result = pipeline.ensure_translate_api_or_fallback(
        video=__import__("pathlib").Path("v.mp4"),
        chat_html=__import__("pathlib").Path("c.html"),
        trans_json=__import__("pathlib").Path("nope.json"),
        review_tsv=__import__("pathlib").Path("nope.tsv"),
        review_xlsx=__import__("pathlib").Path("nope.xlsx"),
        workdir=None,
        final_output=__import__("pathlib").Path("out.mp4"),
        yes=True,
    )

    assert result == "api"
    assert "API 可达" in capsys.readouterr().out


def test_redact_text_idempotent_on_redacted_text():
    """收口处已 redact 的文本再过一次 redact_text 不得二次破坏（调用方可能再脱敏）。"""
    from tui_task import redact_text

    once = redact_text("https://user:secret@evil.example/p?oauth=leak")
    twice = redact_text(once)
    assert twice == once
    assert "[redacted]" in twice
    assert "user:secret" not in twice
