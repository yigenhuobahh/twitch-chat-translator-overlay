#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shutdown-window race regressions for the Textual launcher.

Covers the empirically observed failure modes when widgets disappear while
timers / async messages / worker threads are still in flight:
- `_poll_session` and its widget refreshes must tolerate removed widgets;
- `_refresh_form_validation` must tolerate a removed `#form-validation`
  (programmatic `Input.value` assignment posts `Changed` asynchronously and the
  message can be dispatched during the shutdown window);
- the API probe worker must survive `call_from_thread` raising RuntimeError
  while the message loop is already stopped (textual 8.2.8 keeps `_loop`
  non-None during that window).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
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


def test_textual_api_probe_worker_survives_shutdown_runtime_error():
    """call_from_thread raising RuntimeError mid-shutdown must not kill the app."""
    pytest.importorskip("textual")

    from textual.widgets import TabbedContent

    from tui_run import OverlayTui

    def raise_shutdown(*_args, **_kwargs):
        raise RuntimeError("event loop is closed")

    async def exercise() -> None:
        app = OverlayTui()
        app.call_from_thread = raise_shutdown
        with patch("tui_run.probe_translate_api", return_value=(True, "API 可达")):
            async with app.run_test(size=(140, 50)) as pilot:
                app.query_one(TabbedContent).active = "advanced"
                await pilot.pause(0.05)
                await pilot.click("#btn-test-api")
                for _ in range(20):
                    await pilot.pause(0.05)

    asyncio.run(exercise())
