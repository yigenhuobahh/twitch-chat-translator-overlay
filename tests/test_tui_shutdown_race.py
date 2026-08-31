#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shutdown-window race regressions for the Textual launcher.

Covers the empirically observed failure modes when widgets disappear while
timers / async messages / worker threads are still in flight:
- `_poll_session` and its widget refreshes must tolerate removed widgets;
- `_refresh_form_validation` must tolerate a removed `#form-validation`
  (programmatic `Input.value` assignment posts `Changed` asynchronously and the
  message can be dispatched during the shutdown window);
- the API probe worker must report through `post_message` (the official
  thread-safe channel) so a mid-shutdown worker neither blocks on
  `call_from_thread` nor crashes the app; late messages are dropped safely
  after the app closed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _fake_session() -> SimpleNamespace:
    """Stand-in for TaskSession shaped like the objects existing tests use."""
    return SimpleNamespace(
        poll=lambda: ([], []),
        drain_after_exit=lambda: ([], []),
        running=False,
        returncode=None,
        cancelled=False,
        dropped_output=0,
        result=None,
        cancel=lambda: False,
        close=lambda: None,
    )


def test_textual_poll_session_and_status_refreshes_tolerate_removed_widgets(tmp_path: Path):
    """Removing #status then polling must not raise; each refresh is covered once."""
    pytest.importorskip("textual")

    from tui_history import TuiHistoryStore
    from tui_run import OverlayTui

    async def exercise() -> None:
        app = OverlayTui()
        app.history = TuiHistoryStore(tmp_path / "history.json")
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(0.05)
            app.query_one("#status").remove()
            await pilot.pause(0.05)

            app.session = _fake_session()
            app._poll_session()
            app._set_status("关机窗口期状态写入")
            app._log("关机窗口期日志写入")
            app._refresh_history()
            app._set_history_clear_enabled(True)
            app.session = None

    asyncio.run(exercise())


def test_textual_refresh_form_validation_tolerates_removed_widget():
    """A removed #form-validation must not turn a late Changed message into a crash."""
    pytest.importorskip("textual")

    from tui_run import OverlayTui

    async def exercise() -> None:
        app = OverlayTui()
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(0.05)
            app.query_one("#form-validation").remove()
            await pilot.pause(0.05)
            app._refresh_form_validation()

    asyncio.run(exercise())


def test_textual_api_probe_worker_reports_via_post_message():
    """The probe worker must use post_message; both reply messages reach the UI."""
    pytest.importorskip("textual")

    from textual.widgets import Static, TabbedContent

    from tui_run import OverlayTui

    probe_started = threading.Event()
    probe_released = threading.Event()

    def staged_probe(*_args, **_kwargs):
        # 让 ApiProbeStarted 的"进行中"状态在 UI 上停留足够久以便断言。
        probe_started.set()
        assert probe_released.wait(timeout=10)
        return True, "API 可达"

    async def exercise() -> None:
        app = OverlayTui()
        with patch("tui_run.probe_translate_api", side_effect=staged_probe):
            async with app.run_test(size=(140, 50)) as pilot:
                app.query_one(TabbedContent).active = "advanced"
                await pilot.pause(0.05)
                await pilot.click("#btn-test-api")
                for _ in range(200):
                    if probe_started.is_set():
                        break
                    await pilot.pause(0.02)
                assert probe_started.is_set()

                feedback = app.query_one("#api-status-feedback", Static)
                for _ in range(100):
                    await pilot.pause(0.02)
                    if "正在连接翻译 API" in str(feedback.render()):
                        break
                assert "正在连接翻译 API" in str(feedback.render()), "ApiProbeStarted must render the waiting hint"
                assert "正在测试 API 连通性" in str(app.query_one("#status", Static).render())

                probe_released.set()
                for _ in range(100):
                    await pilot.pause(0.02)
                    if "连通性测试成功" in str(feedback.render()):
                        break
                assert "API 连通性测试成功" in str(feedback.render()), "ApiProbeFinished must render the success feedback"
                assert "API 连通性测试成功" in str(app.query_one("#status", Static).render())

    asyncio.run(exercise())


def test_textual_api_probe_worker_survives_shutdown_without_call_from_thread():
    """Unmount while a probe thread is running must neither block nor crash.

    Regression for the former call_from_thread-based feedback: textual 8.2.8
    can block or raise RuntimeError on a stopped message loop mid-shutdown.
    The worker now only posts messages, and a reply posted after the app
    closed is dropped safely (post_message returns False).
    """
    pytest.importorskip("textual")

    from textual.widgets import TabbedContent

    from tui_run import OverlayTui

    probe_started = threading.Event()
    probe_released = threading.Event()

    def blocked_probe(*_args, **_kwargs):
        probe_started.set()
        # Keep the thread alive across unmount; the reply is posted afterwards.
        assert probe_released.wait(timeout=10)
        return True, "API 可达"

    async def exercise() -> None:
        app = OverlayTui()
        posted_after_close: list[tuple[str, bool]] = []
        with patch("tui_run.probe_translate_api", side_effect=blocked_probe):
            async with app.run_test(size=(140, 50)) as pilot:
                app.query_one(TabbedContent).active = "advanced"
                await pilot.pause(0.05)
                await pilot.click("#btn-test-api")
                for _ in range(200):
                    if probe_started.is_set():
                        break
                    await pilot.pause(0.02)
                assert probe_started.is_set()
                # Spy on the worker's only feedback channel while it is still
                # blocked inside the probe; the late reply goes through here.
                original_post = app.post_message

                def spying_post(message):
                    result = original_post(message)
                    posted_after_close.append((type(message).__name__, result))
                    return result

                app.post_message = spying_post
        # App is closed here while the worker thread is still blocked.
        probe_released.set()
        for _ in range(200):
            if any(name == "ApiProbeFinished" for name, _result in posted_after_close):
                break
            time.sleep(0.05)
        late_replies = [result for name, result in posted_after_close if name == "ApiProbeFinished"]
        assert late_replies, f"worker never posted its late reply: {posted_after_close}"
        # The closed app dropped the message instead of queueing it.
        assert late_replies[-1] is False

    asyncio.run(exercise())
