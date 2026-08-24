#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Comprehensive unit and async UI tests for API configuration, .env sync, and live connectivity probe."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from env_bootstrap import (
    get_translate_api_config,
    probe_translate_api,
    save_dotenv_api_config,
    translate_api_config_ok,
)


@pytest.fixture(autouse=True)
def isolate_environment():
    """Ensure tests in this module do not leak os.environ mutations to subsequent test files."""
    old_env = os.environ.copy()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(old_env)


# ============================================================================
# Unit Tests: save_dotenv_api_config
# ============================================================================


def test_save_dotenv_api_config_creates_new_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify creating a new .env file with Base URL, API Key, and Model."""
    env_file = tmp_path / "new_env_dir" / ".env"
    monkeypatch.delenv("OPENAI_COMPAT_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_COMPAT_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_COMPAT_MODEL", raising=False)

    ok, msg = save_dotenv_api_config(
        base_url="https://api.openai.com/v1",
        api_key="sk-new-secret-12345",
        model="gpt-4o",
        env_path=env_file,
    )
    assert ok is True
    assert "保存成功" in msg
    assert env_file.is_file()

    content = env_file.read_text(encoding="utf-8")
    assert "OPENAI_COMPAT_BASE_URL=https://api.openai.com/v1" in content
    assert "OPENAI_COMPAT_API_KEY=sk-new-secret-12345" in content
    assert "OPENAI_COMPAT_MODEL=gpt-4o" in content

    # Check os.environ synchronization
    assert os.environ.get("OPENAI_COMPAT_BASE_URL") == "https://api.openai.com/v1"
    assert os.environ.get("OPENAI_COMPAT_API_KEY") == "sk-new-secret-12345"
    assert os.environ.get("OPENAI_COMPAT_MODEL") == "gpt-4o"


def test_save_dotenv_api_config_preserves_comments_and_other_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify that existing comments, whitespace, and unrelated environment variables are preserved."""
    env_file = tmp_path / ".env"
    initial_content = (
        "# Twitch Chat CN Overlay Configuration\n"
        "APP_DEBUG=true\n"
        "# Custom server port setting\n"
        "PORT=8080\n"
        "CUSTOM_KEY=custom_value_to_keep\n"
    )
    env_file.write_text(initial_content, encoding="utf-8")

    ok, msg = save_dotenv_api_config(
        base_url="https://api.custom.com/v1",
        api_key="sk-test-custom",
        model="custom-llm",
        env_path=env_file,
    )
    assert ok is True
    content = env_file.read_text(encoding="utf-8")
    assert "# Twitch Chat CN Overlay Configuration" in content
    assert "APP_DEBUG=true" in content
    assert "# Custom server port setting" in content
    assert "PORT=8080" in content
    assert "CUSTOM_KEY=custom_value_to_keep" in content
    assert "OPENAI_COMPAT_BASE_URL=https://api.custom.com/v1" in content
    assert "OPENAI_COMPAT_API_KEY=sk-test-custom" in content
    assert "OPENAI_COMPAT_MODEL=custom-llm" in content


def test_save_dotenv_api_config_updates_commented_and_existing_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify that commented template keys and existing active keys are updated in-place."""
    env_file = tmp_path / ".env"
    initial_content = (
        "# OpenAI Translation Config Template\n"
        "# OPENAI_COMPAT_BASE_URL=https://api.old.com/v1\n"
        "# OPENAI_COMPAT_API_KEY=sk-old-key\n"
        "OPENAI_COMPAT_MODEL=gpt-3.5-turbo\n"
        "OTHER_VAR=important\n"
    )
    env_file.write_text(initial_content, encoding="utf-8")

    ok, msg = save_dotenv_api_config(
        base_url="https://api.deepseek.com/v1",
        api_key="sk-deepseek-67890",
        model="deepseek-chat",
        env_path=env_file,
    )
    assert ok is True
    content = env_file.read_text(encoding="utf-8")
    assert "OPENAI_COMPAT_BASE_URL=https://api.deepseek.com/v1" in content
    assert "OPENAI_COMPAT_API_KEY=sk-deepseek-67890" in content
    assert "OPENAI_COMPAT_MODEL=deepseek-chat" in content
    assert "OTHER_VAR=important" in content
    # Ensure commented versions were replaced, not duplicated
    assert "# OPENAI_COMPAT_BASE_URL=" not in content
    assert "# OPENAI_COMPAT_API_KEY=" not in content
    assert "gpt-3.5-turbo" not in content


def test_save_dotenv_api_config_atomic_write_and_environ_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify atomic write behavior and full os.environ synchronization (including popping empty values)."""
    env_file = tmp_path / ".env"
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://pre-existing.com/v1")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "sk-pre-existing")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "pre-model")

    # Saving with empty API key should pop it from os.environ
    ok, msg = save_dotenv_api_config(
        base_url="https://api.updated.com/v1",
        api_key="",
        model="new-model",
        env_path=env_file,
    )
    assert ok is True
    assert os.environ.get("OPENAI_COMPAT_BASE_URL") == "https://api.updated.com/v1"
    assert "OPENAI_COMPAT_API_KEY" not in os.environ
    assert os.environ.get("OPENAI_COMPAT_MODEL") == "new-model"

    content = env_file.read_text(encoding="utf-8")
    assert "OPENAI_COMPAT_BASE_URL=https://api.updated.com/v1" in content
    assert "OPENAI_COMPAT_API_KEY=" in content
    assert "OPENAI_COMPAT_MODEL=new-model" in content


def test_save_dotenv_api_config_failure_handling(tmp_path: Path):
    """Verify graceful failure return when writing to an invalid or read-only destination."""
    invalid_path = tmp_path / "readonly_file"
    with patch("pathlib.Path.write_text", side_effect=PermissionError("Access denied")):
        ok, msg = save_dotenv_api_config(
            base_url="https://api.openai.com/v1",
            api_key="sk-key",
            model="gpt-4o",
            env_path=invalid_path,
        )
        assert ok is False
        assert "保存 .env 失败" in msg
        assert "Access denied" in msg


# ============================================================================
# Unit Tests: probe_translate_api
# ============================================================================


def test_probe_translate_api_success():
    """Verify success case when mock OpenAI client returns valid chat completion."""
    mock_client = MagicMock()
    mock_completion = MagicMock()
    mock_client.chat.completions.create.return_value = mock_completion

    with patch("openai.OpenAI", return_value=mock_client):
        ok, msg = probe_translate_api(
            base_url="https://api.openai.com/v1",
            api_key="sk-valid-key-12345",
            model="gpt-4o-mini",
            timeout=10.0,
        )
        assert ok is True
        assert "API 可达" in msg
        assert "https://api.openai.com/v1" in msg
        assert "gpt-4o-mini" in msg
        mock_client.chat.completions.create.assert_called_once_with(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )


def test_probe_translate_api_missing_config(monkeypatch: pytest.MonkeyPatch):
    """Verify probe returns (False, error_msg) when required configuration is missing."""
    monkeypatch.delenv("OPENAI_COMPAT_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_COMPAT_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_COMPAT_MODEL", raising=False)
    monkeypatch.delenv("AGNES_BASE_URL", raising=False)
    monkeypatch.delenv("AGNES_API_KEY", raising=False)
    monkeypatch.delenv("AGNES_MODEL", raising=False)

    # All fields missing
    ok, msg = probe_translate_api(base_url="", api_key="", model="")
    assert ok is False
    assert "未配置:" in msg
    assert "OPENAI_COMPAT_BASE_URL" in msg
    assert "OPENAI_COMPAT_API_KEY" in msg
    assert "OPENAI_COMPAT_MODEL" in msg

    # Only API Key provided, Base URL and Model missing
    ok, msg = probe_translate_api(base_url="", api_key="sk-123", model="")
    assert ok is False
    assert "OPENAI_COMPAT_BASE_URL" in msg
    assert "OPENAI_COMPAT_MODEL" in msg
    assert "OPENAI_COMPAT_API_KEY" not in msg


def test_probe_translate_api_uses_environ_defaults(monkeypatch: pytest.MonkeyPatch):
    """Verify probe picks up environment variables when explicit arguments are omitted."""
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://api.env-source.com/v1")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "sk-env-key-abcdef")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "claude-3-haiku")

    mock_client = MagicMock()
    with patch("openai.OpenAI", return_value=mock_client):
        ok, msg = probe_translate_api()
        assert ok is True
        assert "https://api.env-source.com/v1" in msg
        assert "claude-3-haiku" in msg
        mock_client.chat.completions.create.assert_called_once_with(
            model="claude-3-haiku",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )


def test_probe_translate_api_missing_openai_module(monkeypatch: pytest.MonkeyPatch):
    """Verify graceful handling when openai package is not installed."""
    with patch.dict("sys.modules", {"openai": None}):
        ok, msg = probe_translate_api(
            base_url="https://api.openai.com/v1",
            api_key="sk-key",
            model="gpt-4o",
        )
        assert ok is False
        assert "未安装 openai 库" in msg


def test_probe_translate_api_exception_handling():
    """Verify timeout, authentication failure, and connection error handling."""
    # 1. Timeout error
    mock_client_timeout = MagicMock()
    mock_client_timeout.chat.completions.create.side_effect = TimeoutError("Request timed out after 12.0s")
    with patch("openai.OpenAI", return_value=mock_client_timeout):
        ok, msg = probe_translate_api(
            base_url="https://api.openai.com/v1",
            api_key="sk-key",
            model="gpt-4o",
        )
        assert ok is False
        assert "API 不可用" in msg
        assert "Request timed out" in msg

    # 2. Authentication failure (e.g. 401 Unauthorized)
    mock_client_auth = MagicMock()
    mock_client_auth.chat.completions.create.side_effect = Exception("401 Unauthorized: Invalid API key")
    with patch("openai.OpenAI", return_value=mock_client_auth):
        ok, msg = probe_translate_api(
            base_url="https://api.openai.com/v1",
            api_key="sk-invalid",
            model="gpt-4o",
        )
        assert ok is False
        assert "API 不可用" in msg
        assert "401 Unauthorized" in msg

    # 3. Connection refused error
    mock_client_conn = MagicMock()
    mock_client_conn.chat.completions.create.side_effect = ConnectionRefusedError("Failed to connect to host")
    with patch("openai.OpenAI", return_value=mock_client_conn):
        ok, msg = probe_translate_api(
            base_url="https://invalid.host.example/v1",
            api_key="sk-test",
            model="gpt-4o",
        )
        assert ok is False
        assert "API 不可用" in msg


def test_translate_api_config_helpers(monkeypatch: pytest.MonkeyPatch):
    """Verify get_translate_api_config and translate_api_config_ok helper routines."""
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://api.test.com/v1")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "sk-test-key")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "model-x")

    cfg = get_translate_api_config()
    assert cfg["base_url"] == "https://api.test.com/v1"
    assert cfg["api_key"] == "sk-test-key"
    assert cfg["model"] == "model-x"
    assert translate_api_config_ok(cfg) is True

    monkeypatch.delenv("OPENAI_COMPAT_API_KEY", raising=False)
    monkeypatch.delenv("AGNES_API_KEY", raising=False)
    cfg2 = get_translate_api_config()
    assert translate_api_config_ok(cfg2) is False


# ============================================================================
# Async UI Tests: OverlayTui Tab 5 (Advanced Settings API Configuration)
# ============================================================================


def test_tui_api_tab_prefills_and_renders_ui(monkeypatch: pytest.MonkeyPatch):
    """Verify API configuration inputs in Tab 5 prefill accurately from environment variables."""
    pytest.importorskip("textual")
    from textual.widgets import Input, TabbedContent
    from tui_run import OverlayTui

    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://api.prefill.com/v1")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "sk-prefill-secret-999")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "gpt-4o-prefill")

    async def exercise():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            app.query_one(TabbedContent).active = "advanced"
            await pilot.pause(0.02)

            base_url_input = app.query_one("#api-base-url", Input)
            api_key_input = app.query_one("#api-key", Input)
            model_input = app.query_one("#api-model", Input)

            assert base_url_input.value == "https://api.prefill.com/v1"
            assert api_key_input.value == "sk-prefill-secret-999"
            assert model_input.value == "gpt-4o-prefill"

    asyncio.run(exercise())


def test_tui_api_tab_save_button_success():
    """Verify #btn-save-api saves configuration to .env and updates UI success feedback."""
    pytest.importorskip("textual")
    from textual.widgets import Button, Static, TabbedContent
    from tui_run import OverlayTui

    async def exercise():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            app.query_one(TabbedContent).active = "advanced"
            await pilot.pause(0.02)

            app._set_input("#api-base-url", "https://api.save-test.com/v1")
            app._set_input("#api-key", "sk-save-key-888")
            app._set_input("#api-model", "qwen-max")

            with patch("tui_run.save_dotenv_api_config", return_value=(True, "保存成功")) as mock_save:
                save_btn = app.query_one("#btn-save-api", Button)
                await pilot.click(save_btn)
                await pilot.pause(0.02)

                mock_save.assert_called_once_with(
                    "https://api.save-test.com/v1",
                    "sk-save-key-888",
                    "qwen-max",
                )
                feedback = str(app.query_one("#api-status-feedback", Static).render())
                assert "配置已成功保存至本地 .env 文件" in feedback
                assert "qwen-max" in feedback
                status = str(app.query_one("#status", Static).render())
                assert "API 配置已保存至 .env。" in status

    asyncio.run(exercise())


def test_tui_api_tab_save_button_failure():
    """Verify #btn-save-api handles save errors and updates UI error feedback."""
    pytest.importorskip("textual")
    from textual.widgets import Button, Static, TabbedContent
    from tui_run import OverlayTui

    async def exercise():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            app.query_one(TabbedContent).active = "advanced"
            await pilot.pause(0.02)

            app._set_input("#api-base-url", "https://api.save-test.com/v1")
            app._set_input("#api-key", "sk-save-key-888")
            app._set_input("#api-model", "qwen-max")

            with patch("tui_run.save_dotenv_api_config", return_value=(False, "写入磁盘失败：只读文件系统")):
                save_btn = app.query_one("#btn-save-api", Button)
                await pilot.click(save_btn)
                await pilot.pause(0.02)

                feedback = str(app.query_one("#api-status-feedback", Static).render())
                assert "保存 .env 失败：写入磁盘失败：只读文件系统" in feedback
                status = str(app.query_one("#status", Static).render())
                assert "写入磁盘失败：只读文件系统" in status

    asyncio.run(exercise())


def test_tui_api_tab_test_button_success():
    """Verify #btn-test-api handles successful API probe and renders live feedback."""
    pytest.importorskip("textual")
    from textual.widgets import Button, Static, TabbedContent
    from tui_run import OverlayTui

    async def exercise():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            app.query_one(TabbedContent).active = "advanced"
            await pilot.pause(0.02)

            app._set_input("#api-base-url", "https://api.probe-test.com/v1")
            app._set_input("#api-key", "sk-probe-key-111")
            app._set_input("#api-model", "deepseek-reasoner")

            with patch(
                "tui_run.probe_translate_api",
                return_value=(True, "API 可达 (https://api.probe-test.com/v1, model=deepseek-reasoner)"),
            ) as mock_probe:
                test_btn = app.query_one("#btn-test-api", Button)
                await pilot.click(test_btn)

                # Wait for background thread worker to complete and update feedback
                for _ in range(40):
                    await pilot.pause(0.05)
                    feedback = str(app.query_one("#api-status-feedback", Static).render())
                    if "连通性测试成功" in feedback:
                        break

                mock_probe.assert_called_once_with(
                    base_url="https://api.probe-test.com/v1",
                    api_key="sk-probe-key-111",
                    model="deepseek-reasoner",
                    timeout=12.0,
                )
                assert "API 连通性测试成功！" in feedback
                assert "deepseek-reasoner" in feedback
                status = str(app.query_one("#status", Static).render())
                assert "API 连通性测试成功" in status

    asyncio.run(exercise())


def test_tui_api_tab_test_button_failure():
    """Verify #btn-test-api handles failed API probe and renders error feedback."""
    pytest.importorskip("textual")
    from textual.widgets import Button, Static, TabbedContent
    from tui_run import OverlayTui

    async def exercise():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            app.query_one(TabbedContent).active = "advanced"
            await pilot.pause(0.02)

            app._set_input("#api-base-url", "https://api.probe-test.com/v1")
            app._set_input("#api-key", "sk-invalid-key")
            app._set_input("#api-model", "deepseek-reasoner")

            with patch(
                "tui_run.probe_translate_api",
                return_value=(False, "401 Unauthorized: Invalid API key"),
            ) as mock_probe:
                test_btn = app.query_one("#btn-test-api", Button)
                await pilot.click(test_btn)

                for _ in range(40):
                    await pilot.pause(0.05)
                    feedback = str(app.query_one("#api-status-feedback", Static).render())
                    if "连通性测试失败" in feedback:
                        break

                mock_probe.assert_called_once_with(
                    base_url="https://api.probe-test.com/v1",
                    api_key="sk-invalid-key",
                    model="deepseek-reasoner",
                    timeout=12.0,
                )
                assert "API 连通性测试失败：401 Unauthorized: Invalid API key" in feedback
                status = str(app.query_one("#status", Static).render())
                assert "API 测试失败：401 Unauthorized: Invalid API key" in status

    asyncio.run(exercise())


def test_tui_api_tab_nonblocking_ui_responsiveness():
    """Verify that during a long-running API probe, the UI remains responsive and tabs can be switched."""
    pytest.importorskip("textual")
    from textual.widgets import Button, Static, TabbedContent
    from tui_run import OverlayTui

    def slow_probe(*args, **kwargs):
        time.sleep(0.3)
        return True, "API 可达"

    async def exercise():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            app.query_one(TabbedContent).active = "advanced"
            await pilot.pause(0.02)

            with patch("tui_run.probe_translate_api", side_effect=slow_probe):
                test_btn = app.query_one("#btn-test-api", Button)
                await pilot.click(test_btn)
                await pilot.pause(0.02)

                # UI should immediately show connecting status
                initial_feedback = str(app.query_one("#api-status-feedback", Static).render())
                assert "正在连接翻译 API" in initial_feedback

                # Switch between tabs freely while probe is running in background thread
                for tab_name in ("new-task", "task", "history", "advanced"):
                    app.query_one(TabbedContent).active = tab_name
                    await pilot.pause(0.03)

                # Wait for probe worker completion
                for _ in range(40):
                    await pilot.pause(0.05)
                    feedback = str(app.query_one("#api-status-feedback", Static).render())
                    if "连通性测试成功" in feedback:
                        break

                assert "连通性测试成功" in feedback

    asyncio.run(exercise())
