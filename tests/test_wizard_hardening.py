#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hardening coverage for job_wizard: error redaction and preview constants.

Follows the monkeypatch pattern of test_job_wizard_menu.py: collaborators are
patched on the job_wizard module and assertions target observable behavior.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def jobs_dir(tmp_path: Path) -> Path:
    root = tmp_path / "jobs"
    root.mkdir()
    return root


def _patch_menu(jobs_dir: Path, monkeypatch, *, answers):
    """Hermetic wiring for _menu_download_and_continue (prompts + no real download)."""
    import job_wizard as wizard
    import twitch_download as download

    calls: dict[str, list] = {}

    def _prompt(msg: str, default: str | None = None, **_kwargs):
        return next(answers)

    monkeypatch.setattr(wizard, "_prompt", _prompt)
    monkeypatch.setattr(wizard, "_prompt_secret", lambda *_a, **_k: "")
    monkeypatch.setattr(wizard, "_stdin_is_interactive", lambda: False)
    monkeypatch.setattr(wizard, "default_jobs_dir", lambda: jobs_dir)
    monkeypatch.setattr(wizard, "_run_pipeline", lambda *a, **k: calls.setdefault("pipeline", []).append(a) or 0)
    monkeypatch.setattr(
        download, "find_twitchdownloader_cli", lambda: jobs_dir / "TwitchDownloaderCLI.exe"
    )
    monkeypatch.setattr(download, "tools_td_bin_dirs", lambda: [jobs_dir])
    return wizard, calls


# ---------------------------------------------------------------------------
# S-6-1: TwitchDownloadError text is redacted before printing
# ---------------------------------------------------------------------------


def test_menu_download_provider_error_is_redacted(jobs_dir, tmp_path, monkeypatch, capsys):
    """download_assets 抛出的错误文本若内嵌带凭据的 URL，打印前必须脱敏。"""
    import twitch_download as download

    wizard, _calls = _patch_menu(
        jobs_dir,
        monkeypatch,
        answers=iter(["2819850140", "auto", "1080p60", "1", "", "", "Safe", "fast", "audio", "", "1"]),
    )
    video = tmp_path / "v.mp4"
    chat = tmp_path / "c.html"
    video.write_bytes(b"v")
    chat.write_text("<html></html>", encoding="utf-8")

    def fail_download(source, **kwargs):
        raise download.TwitchDownloadError(
            "下载失败: https://user:secret-token@evil.example.com/p?oauth=leaked-value"
        )

    monkeypatch.setattr(download, "download_assets", fail_download)

    assert wizard._menu_download_and_continue() == 2
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "secret-token" not in out
    assert "leaked-value" not in out


def test_menu_download_segment_parse_error_is_redacted(jobs_dir, tmp_path, monkeypatch, capsys):
    """多段输入里 parse_segment_line 的报错文本同样过脱敏。"""
    import twitch_download as download

    wizard, _calls = _patch_menu(
        jobs_dir,
        monkeypatch,
        answers=iter(
            [
                "2819850140",       # URL
                "vod",              # 类型
                "1080p60",          # 画质
                "2",                # 多段裁切
                "bad?oauth=leaked-token",  # 第 1 段（触发 parse_segment_line 报错）
                "",                 # 结束输入
                "n",                # 确认下载 → 取消（空 pairs 抛错，走已取消/失败路径）
            ]
        ),
    )

    real_parse = download.parse_segment_line

    def parse_with_leaky_error(line):
        try:
            return real_parse(line)
        except download.TwitchDownloadError as e:
            raise download.TwitchDownloadError(f"{e} (context oauth=leaked-token)") from e

    monkeypatch.setattr(download, "parse_segment_line", parse_with_leaky_error)

    rc = wizard._menu_download_and_continue()
    out = capsys.readouterr().out
    assert "leaked-token" not in out
    assert "oauth=[redacted]" in out or rc != 0


# ---------------------------------------------------------------------------
# T-10: preview_clip magic 10 → named constant (no drift between the two sites)
# ---------------------------------------------------------------------------


def test_wizard_preview_task_uses_named_preview_clip_constant(jobs_dir, tmp_path, monkeypatch):
    """__preview__ 任务写入的 preview_clip == int(job_wizard._PREVIEW_CLIP_SECONDS)。

    __preview__ 分支只在「复用(用途3) + pin 路径」的组合里被触发
    （_prompt_translation_json 仅在 pin 时被询问），因此交互序列要覆盖
    高级选项 y → 写死 offset n → pin y → 视频/HTML 路径 → 输出路径。
    """
    import job_wizard as wizard

    video = tmp_path / "v.mp4"
    video.write_bytes(b"v")
    saved: dict[str, object] = {}
    monkeypatch.setattr(wizard, "write_job_file", lambda path, fields, **k: saved.update(fields=dict(fields)))
    monkeypatch.setattr(wizard, "save_last_job", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "_stdin_is_interactive", lambda: False)
    # 名称 / 用途3 / 布局 / 编码 / 高级y / 写死offset n / pin y / 输出路径 / 确认y / 保存后"2"
    # （pin y 后的视频/HTML 路径经 _prompt_path 桩取值，不消耗 _prompt 答案；
    #   输出路径用默认即可，但仍消耗一条 _prompt。）
    answers = iter(("reuse", "3", "1", "1", "n", "y", "", "y", "2"))
    monkeypatch.setattr(wizard, "_prompt", lambda *_a, **_k: next(answers))
    monkeypatch.setattr(wizard, "_prompt_path", lambda *_a, **_k: str(video))
    # pin 复用模式触发翻译路径询问；返回 __preview__ 信号走预览分支。
    monkeypatch.setattr(wizard, "_prompt_translation_json", lambda _video: "__preview__")
    monkeypatch.setattr(wizard, "discover_presets", lambda prefix: [])
    monkeypatch.setattr(wizard, "format_preset_menu_lines", lambda *_a, **_k: [])

    # 反证哨兵：字面量实现（preview_clip = 10）不会跟随常量，本测试会失败。
    sentinel = 7
    original = wizard._PREVIEW_CLIP_SECONDS
    wizard._PREVIEW_CLIP_SECONDS = sentinel
    try:
        path = wizard.run_job_wizard(jobs_dir=tmp_path)
    finally:
        wizard._PREVIEW_CLIP_SECONDS = original
    assert path is not None
    assert saved["fields"]["preview_clip"] == sentinel
    assert saved["fields"]["mode"] == "preview"
    # 复位后常量仍是约定值 10.0。
    assert wizard._PREVIEW_CLIP_SECONDS == 10.0


def test_chat_window_preview_clip_constant_matches_wizard_value():
    """chat_window._PREVIEW_CLIP_SECONDS 与 job_wizard 侧同源同值（防漂移）。

    apply_preview_first_defaults 必须引用模块常量而非字面量：把常量 monkeypatch
    成哨兵值后，apply 后的 preview_clip 应跟随常量（字面量实现会保持 10.0 而失败）。
    """
    import chat_window
    import job_wizard as wizard

    assert chat_window._PREVIEW_CLIP_SECONDS == wizard._PREVIEW_CLIP_SECONDS == 10.0

    sentinel = 123.456

    class Args:
        mode = "preview"
        preview_clip = None
        preview_frame = None
        overlay_codec = "vp9"
        render_preset = None

    args = Args()
    monkey_target = chat_window
    original = monkey_target._PREVIEW_CLIP_SECONDS
    monkey_target._PREVIEW_CLIP_SECONDS = sentinel
    try:
        applied = chat_window.apply_preview_first_defaults(args, cli_defaults={"overlay_codec": "vp9"})
        assert "preview_clip" in applied
        assert args.preview_clip == sentinel
    finally:
        monkey_target._PREVIEW_CLIP_SECONDS = original
    # 复位后再跑一遍确认恢复 10.0。
    args2 = Args()
    chat_window.apply_preview_first_defaults(args2, cli_defaults={"overlay_codec": "vp9"})
    assert args2.preview_clip == 10.0
