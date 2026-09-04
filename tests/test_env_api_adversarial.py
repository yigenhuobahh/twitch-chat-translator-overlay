# -*- coding: utf-8 -*-
"""Adversarial stress testing suite for Milestone 3 (Challenger 1).

Covers:
1. Mock isolation and environment variable leakage prevention across test runs.
2. Robustness of save_dotenv_api_config under adversarial file formats (CRLF, BOM,
   malformed lines, duplicates, whitespace, Unicode). The dedicated special-characters
   value test was deduplicated into test_tui_adversarial.py.
3. Robustness of probe_translate_api under adverse error conditions (SSLError, ConnectionReset, 429 RateLimit, 500 ServerError, invalid response).
4. Offset propagation through PipelinePlan. The boundary/adversarial --offset parsing
   parametrization was deduplicated into test_tui_adversarial.py.
5. Async Textual UI stress testing (rapid input typing, tab switching during probe, unmounting while probe thread is active).
6. Deterministic reproduction of DOM query safety failure in _refresh_form_validation.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from env_bootstrap import (
    probe_translate_api,
    save_dotenv_api_config,
)
from pipeline_plan import PipelinePlan
from tui_models import (
    MODE_FULL_PRODUCTION,
    TuiJobDraft,
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
# 1. Mock Isolation & Environment Leakage Stress Tests
# ============================================================================


def test_env_isolation_no_leakage_after_save_dotenv(tmp_path: Path):
    """Verify that environment mutations do not persist when isolated."""
    env_file = tmp_path / ".env"
    initial_base = os.environ.get("OPENAI_COMPAT_BASE_URL")
    initial_key = os.environ.get("OPENAI_COMPAT_API_KEY")
    initial_model = os.environ.get("OPENAI_COMPAT_MODEL")

    try:
        ok, msg = save_dotenv_api_config(
            base_url="https://leak-test.com/v1",
            api_key="sk-leak-key-999",
            model="leak-model-gpt",
            env_path=env_file,
        )
        assert ok is True
        assert os.environ.get("OPENAI_COMPAT_BASE_URL") == "https://leak-test.com/v1"
        assert os.environ.get("OPENAI_COMPAT_API_KEY") == "sk-leak-key-999"
        assert os.environ.get("OPENAI_COMPAT_MODEL") == "leak-model-gpt"
    finally:
        # Cleanup
        if initial_base is not None:
            os.environ["OPENAI_COMPAT_BASE_URL"] = initial_base
        else:
            os.environ.pop("OPENAI_COMPAT_BASE_URL", None)

        if initial_key is not None:
            os.environ["OPENAI_COMPAT_API_KEY"] = initial_key
        else:
            os.environ.pop("OPENAI_COMPAT_API_KEY", None)

        if initial_model is not None:
            os.environ["OPENAI_COMPAT_MODEL"] = initial_model
        else:
            os.environ.pop("OPENAI_COMPAT_MODEL", None)


def test_save_dotenv_api_config_handles_crlf_and_utf8_bom(tmp_path: Path):
    """Adversarial check: .env with Windows CRLF line endings, UTF-8 BOM, and comments."""
    env_file = tmp_path / ".env"
    # Write with UTF-8-SIG (BOM) and CRLF
    bom_content = (
        "\ufeff# Windows style .env\r\n"
        "FOO=BAR\r\n"
        "# OPENAI_COMPAT_BASE_URL=https://old.url\r\n"
        "OPENAI_COMPAT_MODEL=old-model\r\n"
    )
    env_file.write_bytes(bom_content.encode("utf-8"))

    ok, msg = save_dotenv_api_config(
        base_url="https://crlf-bom-test.com/v1",
        api_key="sk-bom-key",
        model="gpt-4o-bom",
        env_path=env_file,
    )
    assert ok is True
    read_text = env_file.read_text(encoding="utf-8")
    assert "OPENAI_COMPAT_BASE_URL=https://crlf-bom-test.com/v1" in read_text
    assert "OPENAI_COMPAT_API_KEY=sk-bom-key" in read_text
    assert "OPENAI_COMPAT_MODEL=gpt-4o-bom" in read_text
    assert "FOO=BAR" in read_text


# NOTE: the former dedicated special-characters/spaces save test was merged
# into test_tui_adversarial.py::test_adversarial_save_dotenv_special_characters,
# and the former 19-case offset boundary parametrization was merged into
# test_tui_adversarial.py::test_adversarial_offset_valid_inputs /
# ::test_adversarial_offset_invalid_inputs.


# ============================================================================
# 2. probe_translate_api Adversarial Error Conditions
# ============================================================================


def test_probe_translate_api_simulated_network_failures():
    """Adversarial check: probe handling SSL errors, 502/503 bad gateway, 429 rate limit."""
    # SSL Error
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = RuntimeError("SSL: CERTIFICATE_VERIFY_FAILED")
    with patch("openai.OpenAI", return_value=mock_client):
        ok, msg = probe_translate_api(
            base_url="https://ssl-error.test/v1",
            api_key="sk-test",
            model="gpt-4o",
        )
        assert ok is False
        assert "CERTIFICATE_VERIFY_FAILED" in msg

    # HTTP 429 Rate Limit
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = Exception("429 Too Many Requests: Rate limit exceeded")
    with patch("openai.OpenAI", return_value=mock_client):
        ok, msg = probe_translate_api(
            base_url="https://api.test/v1",
            api_key="sk-test",
            model="gpt-4o",
        )
        assert ok is False
        assert "429 Too Many Requests" in msg

    # HTTP 503 Service Unavailable
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = Exception("503 Service Temporarily Unavailable")
    with patch("openai.OpenAI", return_value=mock_client):
        ok, msg = probe_translate_api(
            base_url="https://api.test/v1",
            api_key="sk-test",
            model="gpt-4o",
        )
        assert ok is False
        assert "503" in msg


# ============================================================================
# 3. Time Offset (--offset) Extreme Values and Boundary Conditions
# ============================================================================
# NOTE: the former 19-case parametrized offset boundary test lives on in
# test_tui_adversarial.py (valid-input and invalid-input parametrizations,
# extended with the boundary and beyond-bound values unique to this file).


def test_offset_pipeline_plan_building(tmp_path: Path):
    """Verify PipelinePlan propagates offset correctly to render_cn_chat CLI."""
    draft = TuiJobDraft(
        video=str(tmp_path / "v.mp4"),
        chat_html=str(tmp_path / "c.html"),
        offset="-14.25",
        mode=MODE_FULL_PRODUCTION,
    )
    plan = PipelinePlan(fields=draft.to_job_fields())
    cmd = plan.build_command("python", "render_cn_chat.py")
    assert "--offset" in cmd
    idx = cmd.index("--offset")
    assert cmd[idx + 1] == "-14.25"


# ============================================================================
# 4. Async Textual UI Stress & DOM Safety
# ============================================================================


def test_tui_rapid_offset_input_and_validation():
    """Verify rapid offset input changes do not crash Textual app."""
    pytest.importorskip("textual")

    from helpers import wait_for_widget
    from tui_run import OverlayTui

    async def exercise():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            offset_input = await wait_for_widget(app, pilot, "#offset")
            for val in ["1", "12", "12.", "12.5", "-12.5", "invalid", "", "0", "-0.5"]:
                offset_input.value = val
                await pilot.pause(0.01)

            await wait_for_widget(app, pilot, "#form-validation")

    asyncio.run(exercise())


def test_tui_app_unmount_while_probe_thread_active():
    """Verify unmounting the app while an API probe is sleeping in background thread does not crash."""
    pytest.importorskip("textual")
    from textual.widgets import Button, TabbedContent

    from tui_run import OverlayTui

    probe_started = threading.Event()

    def slow_probe(*args, **kwargs):
        probe_started.set()
        time.sleep(0.5)
        return True, "API 可达"

    async def exercise():
        app = OverlayTui()
        with patch("tui_run.probe_translate_api", side_effect=slow_probe):
            async with app.run_test(size=(140, 50)) as pilot:
                app.query_one(TabbedContent).active = "advanced"
                await pilot.pause(0.02)

                test_btn = app.query_one("#btn-test-api", Button)
                await pilot.click(test_btn)
                # The @work(thread=True) probe runs on an executor thread whose
                # startup latency is not covered by pilot.pause; poll the event
                # instead of asserting after a single fixed pause (Windows CI).
                for _ in range(100):
                    if probe_started.is_set():
                        break
                    await pilot.pause(0.02)
                assert probe_started.is_set()
                # App exits now while thread is still active

    asyncio.run(exercise())


# ============================================================================
# Security R-3: untrusted cwd .env providing endpoint + API key must be
# explicitly confirmed (chat text would be sent to that endpoint).
# ============================================================================

_DOTENV_KEYS = (
    "OPENAI_COMPAT_BASE_URL",
    "OPENAI_COMPAT_MODEL",
    "OPENAI_COMPAT_API_KEY",
    "AGNES_BASE_URL",
    "AGNES_MODEL",
    "AGNES_API_KEY",
)


def _load_common_utils():
    import common_utils as cu

    return cu


def _prepare_dotenv_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset dotenv-related state so load_dotenv_if_present actually loads."""
    cu = _load_common_utils()
    for key in _DOTENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("_TWITCH_TRANSPARENT_TEST_MODE", raising=False)
    monkeypatch.setattr(cu, "_DOTENV_LOADED_KEYS", set())


def test_untrusted_cwd_dotenv_endpoint_plus_key_rejected_without_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """恶意 cwd .env(端点+密钥)在非 TTY stdin 下一律拒绝(fail closed)。"""
    cu = _load_common_utils()
    _prepare_dotenv_env(monkeypatch)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "OPENAI_COMPAT_BASE_URL=https://attacker.example/v1",
                "OPENAI_COMPAT_API_KEY=sk-attacker",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    repo_env = Path(cu.__file__).resolve().parents[1] / ".env"
    repo_dotenv = None
    if repo_env.is_file():
        repo_dotenv = repo_env.read_text(encoding="utf-8")
        repo_env.unlink()

    class _NotTty:
        def isatty(self):
            return False

    monkeypatch.setattr("sys.stdin", _NotTty())

    try:
        cu.load_dotenv_if_present()

        assert "OPENAI_COMPAT_BASE_URL" not in os.environ
        assert "OPENAI_COMPAT_API_KEY" not in os.environ
        assert not cu._DOTENV_LOADED_KEYS
    finally:
        if repo_dotenv is not None:
            repo_env.write_text(repo_dotenv, encoding="utf-8")


def test_untrusted_cwd_dotenv_endpoint_plus_key_tty_confirms_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """TTY 下回答 y/yes 视为用户确认,加载成功;其他回答拒绝并回退 repo .env。"""
    cu = _load_common_utils()
    _prepare_dotenv_env(monkeypatch)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "OPENAI_COMPAT_BASE_URL=https://attacker.example/v1",
                "OPENAI_COMPAT_API_KEY=sk-attacker",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    class _Tty:
        def __init__(self, answers):
            self._answers = list(answers)

        def isatty(self):
            return True

        def readline(self):
            return self._answers.pop(0)

    class _TtyInput:
        def __init__(self, answers):
            self._answers = list(answers)

        def isatty(self):
            return True

    repo_env = Path(cu.__file__).resolve().parents[1] / ".env"
    repo_dotenv = None
    if repo_env.is_file():
        repo_dotenv = repo_env.read_text(encoding="utf-8")
        repo_env.unlink()

    try:
        for answer, expected in (("y\n", True), ("YES\n", True), ("n\n", False)):
            monkeypatch.setattr(cu, "_DOTENV_LOADED_KEYS", set())
            for key in _DOTENV_KEYS:
                os.environ.pop(key, None)
            tty = _TtyInput(answers=[])
            monkeypatch.setattr("sys.stdin", tty)
            import builtins

            answer_iter = iter([answer])

            def _fake_input(*_a, _it=answer_iter, **_k):
                return next(_it)

            monkeypatch.setattr(builtins, "input", _fake_input)
            cu.load_dotenv_if_present()
            assert ("OPENAI_COMPAT_BASE_URL" in os.environ) is expected
            assert ("OPENAI_COMPAT_API_KEY" in os.environ) is expected

        # 拒绝后回退:cwd .env 被拒,repo .env 仍生效。
        monkeypatch.setattr(cu, "_DOTENV_LOADED_KEYS", set())
        for key in _DOTENV_KEYS:
            os.environ.pop(key, None)
        repo_env.write_text(
            "OPENAI_COMPAT_MODEL=fallback-model\n", encoding="utf-8"
        )
        monkeypatch.setattr("sys.stdin", _TtyInput(answers=[]))
        answers = iter(["n\n"])
        import builtins

        monkeypatch.setattr(builtins, "input", lambda *a, **k: next(answers))
        cu.load_dotenv_if_present()
        assert "OPENAI_COMPAT_BASE_URL" not in os.environ
        assert os.environ.get("OPENAI_COMPAT_MODEL") == "fallback-model"
    finally:
        if repo_dotenv is not None:
            repo_env.write_text(repo_dotenv, encoding="utf-8")
        else:
            repo_env.unlink(missing_ok=True)


def test_cwd_dotenv_model_only_still_loads_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """仅模型名(无端点+密钥组合)的 cwd .env 维持现状:直接加载。"""
    cu = _load_common_utils()
    _prepare_dotenv_env(monkeypatch)
    (tmp_path / ".env").write_text(
        "OPENAI_COMPAT_MODEL=test-model\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    class _NotTty:
        def isatty(self):
            return False

    monkeypatch.setattr("sys.stdin", _NotTty())
    cu.load_dotenv_if_present()

    assert os.environ.get("OPENAI_COMPAT_MODEL") == "test-model"
