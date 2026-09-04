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

import glob  # noqa: E402 - Fix 10 glob.escape 验证用

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


# ---------------------------------------------------------------------------
# PIPE-O1: --dry-run 不得产生真实写副作用
# ---------------------------------------------------------------------------


def test_export_review_tables_dry_run_skips_write(monkeypatch, tmp_path):
    """写侧守卫：DRY_RUN 下复核表导出函数不落盘。"""
    trans = _write_trans_json(tmp_path / "trans.json", fail=False)
    tsv = tmp_path / "review.tsv"
    xlsx = tmp_path / "review.xlsx"
    monkeypatch.setattr(pipe, "DRY_RUN", True)
    try:
        pipe.export_review_tsv(trans, tsv)
        pipe.export_review_xlsx(trans, xlsx)
    finally:
        monkeypatch.setattr(pipe, "DRY_RUN", False)
    assert not tsv.exists(), "dry-run 不得写出复核 TSV"
    assert not xlsx.exists(), "dry-run 不得写出复核 XLSX"


def test_dry_run_reuse_review_does_not_write_review_tables(monkeypatch, tmp_path, pipeline_mocks):
    """--dry-run --reuse-translation --review：此前会真实写出复核 TSV/XLSX。"""
    video, html = _make_inputs(tmp_path)
    trans = _write_trans_json(tmp_path / "trans.json", fail=False)
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
            "--review",
            "--dry-run",
        ],
    )
    try:
        pipe.main()
        tsv = video.with_name(video.stem + "_translation_review.tsv")
        xlsx = tsv.with_suffix(".xlsx")
        assert not tsv.exists(), "dry-run 不得写出复核 TSV"
        assert not xlsx.exists(), "dry-run 不得写出复核 XLSX"
    finally:
        pipe.DRY_RUN = False  # main() 会真实改写模块全局，防止泄漏给后续用例


def test_dry_run_download_flow_skips_real_download(monkeypatch):
    """--download 在 DRY_RUN 全局赋值前执行，必须在入口拦截，不能真实下载。"""
    import twitch_download as td

    def _boom(*a, **k):
        raise AssertionError("dry-run 下不得调用真实下载")

    monkeypatch.setattr(td, "download_assets", _boom)
    monkeypatch.setattr(td, "download_assets_multi", _boom)
    args = SimpleNamespace(download="https://www.twitch.tv/videos/123", dry_run=True)
    assert pipe._run_download_flow(args) == 0


def test_dry_run_job_does_not_touch_last_job(monkeypatch, tmp_path, pipeline_mocks):
    """--dry-run --job 不得写出 jobs/.last_job。"""
    video, html = _make_inputs(tmp_path)
    job = tmp_path / "style.yaml"
    job.write_text("mode: auto\n", encoding="utf-8")
    calls: list = []
    monkeypatch.setattr(pipe, "save_last_job", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(
        sys,
        "argv",
        ["render_cn_chat.py", str(video), str(html), "--job", str(job), "--dry-run"],
    )
    try:
        pipe.main()
        assert calls == [], "dry-run 不得记录 .last_job"
    finally:
        pipe.DRY_RUN = False  # main() 会真实改写模块全局，防止泄漏给后续用例


def test_dry_run_lint_report_not_written(monkeypatch, tmp_path, pipeline_mocks):
    """--dry-run --lint-translation --lint-report：lint 照常执行，但报告不落盘。"""
    trans = _write_trans_json(tmp_path / "clean.json", fail=False)
    report = tmp_path / "lint_report.tsv"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "render_cn_chat.py",
            "--lint-translation",
            str(trans),
            "--lint-report",
            str(report),
            "--dry-run",
        ],
    )
    try:
        with pytest.raises(SystemExit) as excinfo:
            pipe.main()
        assert excinfo.value.code == 0
        assert not report.exists(), "dry-run 不得写出质检报告"
    finally:
        pipe.DRY_RUN = False  # main() 会真实改写模块全局，防止泄漏给后续用例


# ---------------------------------------------------------------------------
# PIPE-O4: preview 模式不应为 10 秒预览完整解码全片
# ---------------------------------------------------------------------------


def _preview_args(source_media_check: str) -> SimpleNamespace:
    return SimpleNamespace(
        mode="preview",
        preview_clip=None,
        preview_frame=None,
        overlay_codec="vp9",
        render_preset=None,
        source_media_check=source_media_check,
    )


def test_preview_mode_downgrades_decode_media_check(monkeypatch):
    # 模拟 TUI/其他入口把 decode 作为默认值传入、用户未显式写 --source-media-check
    monkeypatch.setattr(sys, "argv", ["render_cn_chat.py"])
    args = _preview_args("decode")
    applied = pipe.apply_mode_defaults(args)
    assert args.source_media_check == "fast"
    assert "source_media_check=fast" in applied


def test_preview_mode_respects_explicit_decode_flag(monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["render_cn_chat.py", "--source-media-check", "decode"]
    )
    args = _preview_args("decode")
    applied = pipe.apply_mode_defaults(args)
    assert args.source_media_check == "decode"
    assert "source_media_check=fast" not in applied


def test_preview_mode_keeps_fast_without_flag(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["render_cn_chat.py"])
    args = _preview_args("fast")
    pipe.apply_mode_defaults(args)
    assert args.source_media_check == "fast"


# ---------------------------------------------------------------------------
# PIPE-O5: 复核表导出只解析一次 JSON、只跑一次 lint
# ---------------------------------------------------------------------------


def test_review_export_pair_lints_once(monkeypatch, tmp_path):
    trans = _write_trans_json(tmp_path / "trans.json", fail=False)
    calls: list = []
    orig_lint = pipe.lint_translation

    def spy(*a, **k):
        calls.append(a)
        return orig_lint(*a, **k)

    monkeypatch.setattr(pipe, "lint_translation", spy)
    monkeypatch.setattr(pipe, "DRY_RUN", False)  # 防止前面的 main() 级用例遗留 True
    data, issue_map = pipe._prepare_review_export(trans)
    pipe.export_review_tsv(trans, tmp_path / "review.tsv", data=data, issue_map=issue_map)
    pipe.export_review_xlsx(trans, tmp_path / "review.xlsx", data=data, issue_map=issue_map)
    assert len(calls) == 1, f"lint 应只跑一次，实际 {len(calls)} 次"
    assert (tmp_path / "review.tsv").is_file()
    assert (tmp_path / "review.xlsx").is_file()


def test_lint_translation_accepts_preparsed_data(tmp_path):
    trans = _write_trans_json(tmp_path / "trans.json", fail=True)  # 缺 translation → FAIL
    data = json.loads(trans.read_text(encoding="utf-8"))
    issues = pipe.lint_translation(trans, data=data)
    assert any(i["severity"] == "FAIL" for i in issues)


# ---------------------------------------------------------------------------
# Fix 10: 管线面修复
# ---------------------------------------------------------------------------

def test_pause_prompt_eof_stops_instead_of_continue(monkeypatch, tmp_path, capsys):
    """EOF（stdin 关闭）时返回 "stop"：无人监督自动续渲染数小时不安全。"""
    trans = _write_trans_json(tmp_path / "t.json", fail=False)
    monkeypatch.setattr(pipe, "export_review_tsv", lambda *a, **k: None)
    monkeypatch.setattr(pipe, "export_review_xlsx", lambda *a, **k: None)
    monkeypatch.setattr(pipe, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(pipe, "DRY_RUN", False)

    def raise_eof(*_a, **_k):
        raise EOFError("stdin closed")

    monkeypatch.setattr("builtins.input", raise_eof)

    action = pipe.pause_after_translation_for_review(
        trans_json=trans,
        review_xlsx=tmp_path / "r.xlsx",
        review_tsv=tmp_path / "r.tsv",
    )
    assert action == "stop"
    assert "已暂停" in capsys.readouterr().out


def test_render_preview_clip_glob_escapes_special_stem(monkeypatch, tmp_path):
    """stem 含 glob 元字符（[ ]）时按字面匹配，不匹配到错误候选。"""
    preview_dir = tmp_path / "out"
    preview_dir.mkdir()
    real = preview_dir / "clip [x]_chat.mp4"
    real.write_bytes(b"real")
    # 未转义 glob 会把 "clip [x]" 当字符集，命中 "clip x_chat.mp4" 之类的候选；
    # 放一个只被坏 glob 命中的文件验证转义生效。
    bad_glob_match = preview_dir / "clip x_chat.mp4"
    bad_glob_match.write_bytes(b"wrong")

    monkeypatch.setattr(pipe, "run", lambda *a, **k: None)

    from test_cli_flag_forward import _representative_namespace

    args = _representative_namespace()
    args.offset = None
    args.x = 10
    args.y = 20
    args.width = 100
    args.height = 200
    args.font_size = 16
    args.font_path = "auto"
    args.font_bold_path = "auto"
    args.bg_alpha = 200

    video = tmp_path / "clip [x].mp4"
    video.write_bytes(b"v")
    html = tmp_path / "chat.html"
    html.write_text("<html></html>", encoding="utf-8")
    trans = tmp_path / "t.json"
    trans.write_text("{}", encoding="utf-8")
    burn = tmp_path / "burn.py"
    burn.write_text("# stub", encoding="utf-8")

    monkeypatch.setattr(pipe.os, "startfile", lambda *_a: None, raising=False)
    out = pipe._render_preview_clip(
        video=video,
        chat_html=html,
        trans_json=trans,
        args=args,
        workdir=preview_dir,  # 函数内实际写到 workdir/temp
        seconds=5.0,
        burn=burn,
    )
    assert out is None, "workdir/temp 下没有产物，应返回 None（只验证命令）"
    # 直接验证转义后的 glob 行为：workdir/temp 现在存在
    temp_dir = preview_dir / "temp"
    moved_real = temp_dir / "clip [x]_chat.mp4"
    moved_real.write_bytes(b"real")
    candidates = list(temp_dir.glob(f"{glob.escape(video.stem)}_chat.mp4"))
    assert candidates == [moved_real], "glob.escape 后应精确命中带 [] 的文件名"
    # 未转义 glob 会漏掉真实文件（字符集语义）——证明转义是必要的
    assert list(temp_dir.glob(f"{video.stem}_chat.mp4")) == []


def test_render_preset_failure_message_and_exit_code(tmp_path, monkeypatch):
    """--render-preset 加载失败：SystemExit("错误: ...")，exit code 1，与
    layout 分支一致（原 bare SystemExit(2) 已改）。"""
    video, html = _make_inputs(tmp_path)
    bad_preset = tmp_path / "bad_preset.yaml"
    bad_preset.write_text("{ not: valid: yaml ]", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "render_cn_chat.py",
            str(video),
            str(html),
            "--render-preset",
            str(bad_preset),
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        pipe.main()
    # SystemExit(f"...") → code 是错误消息字符串；str 形式 exit code 为 1。
    code = excinfo.value.code
    assert not isinstance(code, int) or code == 1
    assert isinstance(code, str)
    assert code.startswith("错误:")


def test_sentinel_lint_with_video_but_no_chat_is_parser_error(tmp_path, monkeypatch):
    """--lint-translation 不带值 + video 但缺 chat_html：parser.error（exit 2），
    不再走 _lint_only_exit 把 video 当翻译 JSON。"""
    video, _html = _make_inputs(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "render_cn_chat.py",
            str(video),
            "--lint-translation",
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        pipe.main()
    assert excinfo.value.code == 2
