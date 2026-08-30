#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Adversarial stress-test suite for Milestone 3 Acceptance Gate.

Testing:
- R1: Copywriting, technical boundary hints, safety warnings.
- R2: 3 core workflow paths and full legacy mode compatibility.
- R3: Select dropdowns, custom values, and robust --offset handling.
- R4: API configuration, dotenv atomic sync, and probe error handling.
- R5: Contract compatibility, YAML persistence, and CLI command building.
"""

from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path
import sys
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from env_bootstrap import (
    probe_translate_api,
    save_dotenv_api_config,
)
from tui_models import (
    CORE_MODES,
    MODE_AUTO,
    MODE_FULL_PRODUCTION,
    MODE_FULL_RENDER,
    MODE_ORIGINAL_PREVIEW,
    MODE_ORIGINAL_PRODUCTION,
    MODE_QUICK_PREVIEW_ORIGINAL,
    MODE_QUICK_PREVIEW_TRANSLATED,
    MODE_RENDER_ONLY,
    MODE_RENDER_ORIGINAL,
    MODE_REUSE_RENDER,
    MODE_STEP_API_AND_REVIEW,
    MODE_STEP_EXPORT_MANUAL,
    MODE_STEP_RESUME_RENDER,
    MODE_TRANSLATE_ONLY,
    MODE_TRANSLATED_PREVIEW,
    TuiJobDraft,
    _mode_from_fields,
)
from tui_run import (
    OverlayTui,
)


@pytest.fixture(autouse=True)
def isolate_environment():
    old_env = os.environ.copy()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(old_env)


# ============================================================================
# Adversarial Tests: Time Offset (--offset) Stress Tests
# ============================================================================

@pytest.mark.parametrize(
    "offset_input, expected_float",
    [
        ("0", 0.0),
        ("0.0", 0.0),
        ("-0.0", -0.0),
        ("+0", 0.0),
        ("12.5", 12.5),
        ("-12.5", -12.5),
        ("  3.14159  ", 3.14159),
        ("-3600", -3600.0),
        ("604800.0", 604800.0),
        ("-604800.0", -604800.0),
        ("604800", 604800.0),
        ("-604800", -604800.0),
        ("604800.000", 604800.0),
        ("-604800.000", -604800.0),
        ("0.000001", 0.000001),
        ("-0.000001", -0.000001),
        ("0.0001", 0.0001),
        ("-0.0001", -0.0001),
    ],
)
def test_adversarial_offset_valid_inputs(tmp_path: Path, offset_input: str, expected_float: float):
    video = tmp_path / "video.mp4"
    chat = tmp_path / "chat.html"
    video.write_bytes(b"dummy video")
    chat.write_bytes(b"dummy chat")
    draft = TuiJobDraft(
        video=str(video),
        chat_html=str(chat),
        mode=MODE_QUICK_PREVIEW_ORIGINAL,
        offset=offset_input,
    )
    problems = draft.validate(check_api=False, check_environment=False)
    assert not problems, f"Valid offset {offset_input!r} caused validation error: {problems}"
    fields = draft.to_job_fields()
    if offset_input.strip() in ("0", "0.0", "-0.0", "+0"):
        assert "offset" in fields
        assert fields["offset"] == 0.0
    else:
        assert "offset" in fields
        assert math.isclose(fields["offset"], expected_float, rel_tol=1e-5)
    restored = TuiJobDraft.from_fields(fields)
    assert str(restored.offset).strip() == str(fields["offset"])
    cmd = draft.build_command("python", "render_cn_chat.py")
    assert "--offset" in cmd
    idx = cmd.index("--offset")
    assert math.isclose(float(cmd[idx + 1]), expected_float, rel_tol=1e-5)


@pytest.mark.parametrize(
    "invalid_offset, expected_err_keyword",
    [
        ("not_a_number", "数字"),
        ("12.5.6", "数字"),
        (" --5 ", "数字"),
        ("NaN", "7 天"),
        ("Infinity", "7 天"),
        ("-Infinity", "7 天"),
        ("inf", "7 天"),
        ("-inf", "7 天"),
        ("604801.0", "7 天"),
        ("-604801.0", "7 天"),
        ("604800.1", "7 天"),
        ("-604800.1", "7 天"),
        ("1e10", "7 天"),
        ("-1e20", "7 天"),
    ],
)
def test_adversarial_offset_invalid_inputs(tmp_path: Path, invalid_offset: str, expected_err_keyword: str):
    video = tmp_path / "video.mp4"
    chat = tmp_path / "chat.html"
    video.write_bytes(b"dummy video")
    chat.write_bytes(b"dummy chat")
    draft = TuiJobDraft(
        video=str(video),
        chat_html=str(chat),
        mode=MODE_QUICK_PREVIEW_ORIGINAL,
        offset=invalid_offset,
    )
    problems = draft.validate(check_api=False, check_environment=False)
    assert any(expected_err_keyword in p for p in problems), f"Expected keyword {expected_err_keyword} in {problems}"


def test_adversarial_offset_empty_and_none(tmp_path: Path):
    video = tmp_path / "video.mp4"
    chat = tmp_path / "chat.html"
    video.write_bytes(b"dummy video")
    chat.write_bytes(b"dummy chat")
    for empty_val in ("", "   ", None):
        draft = TuiJobDraft(
            video=str(video),
            chat_html=str(chat),
            mode=MODE_QUICK_PREVIEW_ORIGINAL,
            offset=empty_val,
        )
        assert not draft.validate(check_api=False, check_environment=False)
        fields = draft.to_job_fields()
        assert "offset" not in fields or fields["offset"] == ""
        cmd = draft.build_command("python", "render_cn_chat.py")
        assert "--offset" not in cmd

# ============================================================================
# Adversarial Tests: 3 Core Workflow Paths & Legacy Compatibility
# ============================================================================

def test_adversarial_all_modes_projection_and_cli():
    test_matrix = [
        (MODE_QUICK_PREVIEW_ORIGINAL, {"mode": "preview", "render_original": True}, ["--mode", "preview", "--render-original"]),
        (MODE_QUICK_PREVIEW_TRANSLATED, {"mode": "preview"}, ["--mode", "preview"]),
        (MODE_FULL_PRODUCTION, {"mode": "full"}, ["--mode", "full"]),
        (MODE_ORIGINAL_PRODUCTION, {"mode": "render", "render_original": True}, ["--mode", "render", "--render-original"]),
        (MODE_STEP_EXPORT_MANUAL, {"mode": "translate", "manual_translation": True}, ["--manual-translation"]),
        (MODE_STEP_API_AND_REVIEW, {"mode": "full", "review": True}, ["--review"]),
        (MODE_STEP_RESUME_RENDER, {"mode": "render", "reuse_translation": True}, ["--mode", "render", "--reuse-translation"]),
        (MODE_ORIGINAL_PREVIEW, {"mode": "preview", "render_original": True}, ["--mode", "preview", "--render-original"]),
        (MODE_TRANSLATED_PREVIEW, {"mode": "preview"}, ["--mode", "preview"]),
        (MODE_FULL_RENDER, {"mode": "full"}, ["--mode", "full"]),
        (MODE_REUSE_RENDER, {"mode": "render", "reuse_translation": True}, ["--mode", "render", "--reuse-translation"]),
        (MODE_RENDER_ONLY, {"mode": "render"}, ["--mode", "render"]),
        (MODE_TRANSLATE_ONLY, {"mode": "translate"}, ["--mode", "translate"]),
        (MODE_AUTO, {"mode": "auto"}, ["--mode", "auto"]),
    ]
    for mode_name, expected_fields_subset, expected_cli_flags in test_matrix:
        draft = TuiJobDraft(
            video="test.mp4",
            chat_html="chat.html",
            mode=mode_name,
        )
        fields = draft.to_job_fields()
        for k, v in expected_fields_subset.items():
            assert fields.get(k) == v, f"Mode {mode_name} field {k} expected {v}, got {fields.get(k)}"
        cmd = draft.build_command("python", "render_cn_chat.py")
        for flag in expected_cli_flags:
            assert flag in cmd, f"Mode {mode_name} missing expected CLI flag {flag} in {cmd}"


def test_adversarial_mode_from_fields_exhaustive():
    for mode in CORE_MODES:
        assert _mode_from_fields({"mode": mode}) == mode
    assert _mode_from_fields({"mode": "preview", "render_original": True}) == MODE_ORIGINAL_PREVIEW
    assert _mode_from_fields({"mode": "preview", "render_original": False}) == MODE_TRANSLATED_PREVIEW
    assert _mode_from_fields({"mode": "translate", "manual_translation": True}) == MODE_STEP_EXPORT_MANUAL
    assert _mode_from_fields({"mode": "translate", "manual_translation": False}) == MODE_TRANSLATE_ONLY
    assert _mode_from_fields({"mode": "render", "reuse_translation": True}) == MODE_REUSE_RENDER
    assert _mode_from_fields({"mode": "render", "reuse_translation": False}) == MODE_RENDER_ONLY
    assert _mode_from_fields({"mode": "full"}) == MODE_FULL_RENDER
    assert _mode_from_fields({"mode": "auto"}) == MODE_AUTO
    assert _mode_from_fields({}) == MODE_FULL_RENDER


def test_adversarial_requires_translation_logic():
    for non_trans in (
        MODE_QUICK_PREVIEW_ORIGINAL,
        MODE_ORIGINAL_PREVIEW,
        MODE_ORIGINAL_PRODUCTION,
        MODE_RENDER_ORIGINAL,
        MODE_RENDER_ONLY,
        MODE_REUSE_RENDER,
        MODE_STEP_RESUME_RENDER,
        MODE_STEP_EXPORT_MANUAL,
    ):
        draft = TuiJobDraft(mode=non_trans)
        assert draft.requires_translation() is False, f"Mode {non_trans} unexpectedly required translation"

    for trans in (
        MODE_QUICK_PREVIEW_TRANSLATED,
        MODE_TRANSLATED_PREVIEW,
        MODE_FULL_PRODUCTION,
        MODE_FULL_RENDER,
        MODE_STEP_API_AND_REVIEW,
        MODE_TRANSLATE_ONLY,
        MODE_AUTO,
    ):
        draft = TuiJobDraft(mode=trans)
        assert draft.requires_translation() is True, f"Mode {trans} did not require translation"

    assert TuiJobDraft(mode=MODE_FULL_PRODUCTION, render_original=True).requires_translation() is False
    assert TuiJobDraft(mode=MODE_FULL_PRODUCTION, reuse_translation=True).requires_translation() is False
    assert TuiJobDraft(mode=MODE_FULL_PRODUCTION, manual_translation=True).requires_translation() is False

# ============================================================================
# Adversarial Tests: Dotenv Atomic Synchronization Engine
# ============================================================================

def test_adversarial_save_dotenv_special_characters(tmp_path: Path):
    env_file = tmp_path / "deep" / "nested" / "path" / ".env"
    special_key = "sk-proj-1234567890-test-key_value"
    special_url = "https://custom-proxy.internal.corp:8443/v1/api"
    special_model = "deepseek/deepseek-chat:v3.1-beta"
    ok, msg = save_dotenv_api_config(
        base_url=special_url,
        api_key=special_key,
        model=special_model,
        env_path=env_file,
    )
    assert ok is True
    assert env_file.is_file()
    content = env_file.read_text(encoding="utf-8")
    assert f"OPENAI_COMPAT_BASE_URL={special_url}" in content
    assert f"OPENAI_COMPAT_API_KEY={special_key}" in content
    assert f"OPENAI_COMPAT_MODEL={special_model}" in content
    assert os.environ.get("OPENAI_COMPAT_BASE_URL") == special_url
    assert os.environ.get("OPENAI_COMPAT_API_KEY") == special_key
    assert os.environ.get("OPENAI_COMPAT_MODEL") == special_model

    # Adversarial values in URL/key/model positions must survive verbatim
    # (absorbed from the former dedicated special-characters save test).
    adversarial_url = "https://api.example.com/v1/custom?query=test#frag"
    adversarial_key = "sk-special_!@#$%^&*()_+-=[]{}|;:,.<>?"
    adversarial_model = "deepseek/deepseek-chat-v3:latest"
    ok2, msg2 = save_dotenv_api_config(
        base_url=adversarial_url,
        api_key=adversarial_key,
        model=adversarial_model,
        env_path=env_file,
    )
    assert ok2 is True
    content = env_file.read_text(encoding="utf-8")
    assert f"OPENAI_COMPAT_BASE_URL={adversarial_url}" in content
    assert f"OPENAI_COMPAT_API_KEY={adversarial_key}" in content
    assert f"OPENAI_COMPAT_MODEL={adversarial_model}" in content


def test_adversarial_save_dotenv_multiline_and_comment_chaos(tmp_path: Path):
    env_file = tmp_path / ".env"
    initial = (
        "# Random comment line\n"
        "\n"
        "# OPENAI_COMPAT_BASE_URL=https://old1.com\n"
        "OPENAI_COMPAT_BASE_URL = https://old2.com\n"
        "# OPENAI_COMPAT_BASE_URL = https://old3.com\n"
        "# OPENAI_COMPAT_API_KEY=sk-old\n"
        "OPENAI_COMPAT_API_KEY=sk-old2\n"
        "OPENAI_COMPAT_MODEL=gpt-3.5\n"
        "# OPENAI_COMPAT_MODEL=gpt-4\n"
        "UNRELATED_KEY=keep_me\n"
    )
    env_file.write_text(initial, encoding="utf-8")
    ok, msg = save_dotenv_api_config(
        base_url="https://api.new.com/v1",
        api_key="sk-new-key",
        model="gpt-4o",
        env_path=env_file,
    )
    assert ok is True
    content = env_file.read_text(encoding="utf-8")
    lines = content.splitlines()
    base_url_lines = [line for line in lines if "OPENAI_COMPAT_BASE_URL=" in line]
    api_key_lines = [line for line in lines if "OPENAI_COMPAT_API_KEY=" in line]
    model_lines = [line for line in lines if "OPENAI_COMPAT_MODEL=" in line]
    assert len(base_url_lines) == 1
    assert base_url_lines[0] == "OPENAI_COMPAT_BASE_URL=https://api.new.com/v1"
    assert len(api_key_lines) == 1
    assert api_key_lines[0] == "OPENAI_COMPAT_API_KEY=sk-new-key"
    assert len(model_lines) == 1
    assert model_lines[0] == "OPENAI_COMPAT_MODEL=gpt-4o"
    assert "UNRELATED_KEY=keep_me" in content


# ============================================================================
# Adversarial Tests: API Live Probe Edge Cases
# ============================================================================

def test_adversarial_probe_api_error_truncation(monkeypatch: pytest.MonkeyPatch):
    mock_openai = MagicMock()
    very_long_error = "Server Error: " + ("A" * 500)
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = RuntimeError(very_long_error)
    mock_openai.OpenAI.return_value = mock_client
    monkeypatch.setitem(sys.modules, "openai", mock_openai)
    ok, msg = probe_translate_api(
        base_url="https://api.test.com/v1",
        api_key="sk-test",
        model="gpt-4o",
    )
    assert ok is False
    assert "API 不可用: Server Error:" in msg
    assert len(msg) <= 260
    assert msg.endswith("…")


def test_adversarial_probe_api_all_empty():
    ok, msg = probe_translate_api(base_url="", api_key="", model="")
    assert ok is False
    assert "未配置:" in msg
    assert "OPENAI_COMPAT_BASE_URL" in msg
    assert "OPENAI_COMPAT_API_KEY" in msg
    assert "OPENAI_COMPAT_MODEL" in msg


# ============================================================================
# Adversarial Tests: Textual UI Async App & Select Handling
# ============================================================================

def test_adversarial_tui_custom_select_options_and_offset():
    pytest.importorskip("textual")

    async def exercise():
        app = OverlayTui()
        async with app.run_test() as pilot:
            assert app.query_one("#layout-preset").value == "default"
            assert app.query_one("#render-preset").value == "default"
            assert app.query_one("#encoder").value == "auto"
            custom_draft = TuiJobDraft(
                video="input.mp4",
                chat_html="chat.html",
                layout_preset="my_ultra_custom_layout",
                render_preset="super_hq_custom",
                encoder="custom_nvenc_hevc",
                offset="-42.5",
            )
            app._apply_draft(custom_draft)
            await pilot.pause()
            assert app.query_one("#offset").value == "-42.5"
            assert app.query_one("#layout-preset").value == "my_ultra_custom_layout"
            assert app.query_one("#render-preset").value == "super_hq_custom"
            assert app.query_one("#encoder").value == "custom_nvenc_hevc"
            read_draft = app._draft()
            assert read_draft.offset == "-42.5"
            assert read_draft.layout_preset == "my_ultra_custom_layout"
            assert read_draft.render_preset == "super_hq_custom"
            assert read_draft.encoder == "custom_nvenc_hevc"

    asyncio.run(exercise())


def test_adversarial_tui_job_load_corrupt_file_handling(tmp_path: Path):
    pytest.importorskip("textual")
    from textual.widgets import Static
    corrupt_file = tmp_path / "corrupt_job.yaml"
    corrupt_file.write_bytes(b"\x00\xff\xfe\x00corrupt: [unclosed")

    async def exercise():
        app = OverlayTui()
        async with app.run_test() as pilot:
            app.query_one("#job-path").value = str(corrupt_file)
            app._load_job()
            await pilot.pause()
            status_text = str(app.query_one("#status", Static).render())
            # Binary garbage and malformed YAML syntax must both land on the
            # explicit import-failure message (absorbed from the former
            # dedicated malformed-YAML-syntax test).
            assert "无法导入 YAML" in status_text

    asyncio.run(exercise())
