# -*- coding: utf-8 -*-
"""Empirical test suite for M2 Textual GUI implementation (Challenger 2).

Coverage kept in this file after deduplication against the shared suites
(test_adversarial_m3.py, test_tui_api_config.py, test_adversarial_m2.py):
1. Non-standard and legacy mode handling in form populate and read.
2. Draft edge cases (extra_fields offset fallback, invalid preview clip).
3. Select dropdown reactivity driving the validation widget.
4. Select custom option hygiene (no unbounded duplication, public API only).
5. TUI-level invalid offset values surfacing in the validation widget.
6. Non-blocking nature of @work(thread=True) API probe with wall-clock checks.

Deduplicated cases now covered elsewhere:
- custom preset populate -> test_adversarial_m3.py::test_adversarial_tui_custom_select_options_and_offset
  and test_adversarial_m2.py::test_tui_select_controls_and_custom_options
- API save/test/probe UI flows -> test_tui_api_config.py (tab save/test buttons)
- 6-tab widget inventory and navigation -> test_adversarial_m2.py::test_tui_tab_switching_stress
- mode -> CLI contracts -> test_adversarial_m3.py::test_adversarial_all_modes_projection_and_cli
- offset propagation / blank omission -> test_adversarial_m2.py::test_tui_offset_inputs_and_validation
- manual_required guidance -> test_adversarial_m2.py::test_tui_task_dispatch_and_manual_required_lifecycle
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import time
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from textual.widgets import Input, Select, Static, TabbedContent
from textual.widgets._select import InvalidSelectValueError

from helpers import wait_for_validation_state, wait_for_widget
from tui_models import (
    MODE_QUICK_PREVIEW_ORIGINAL,
    MODE_RENDER_ONLY,
    MODE_STEP_RESUME_RENDER,
    TuiJobDraft,
)
from tui_run import (
    _ENCODER_OPTIONS,
    _UI_MODE_RENDER_ORIGINAL,
    OverlayTui,
)

# =============================================================================
# Area 1: Non-Standard Modes & Draft Edge Cases
# =============================================================================


def test_form_draft_non_standard_modes():
    """Test non-standard and legacy modes handling in form populate and read."""
    async def run():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            # 1. MODE_RENDER_ONLY with render_original=True -> UI mode is "render_original"
            draft_render_orig = TuiJobDraft(
                video="v.mp4",
                chat_html="c.html",
                mode=MODE_RENDER_ONLY,
                render_original=True,
            )
            app._populate_form_from_draft(draft_render_orig)
            await pilot.pause(0.05)
            assert app.query_one("#task-mode", Select).value == _UI_MODE_RENDER_ORIGINAL

            # 2. MODE_RENDER_ONLY without render_original -> includes advanced render option
            draft_render_adv = TuiJobDraft(
                video="v.mp4",
                chat_html="c.html",
                mode=MODE_RENDER_ONLY,
                render_original=False,
            )
            app._populate_form_from_draft(draft_render_adv)
            await pilot.pause(0.05)
            assert app.query_one("#task-mode", Select).value == MODE_RENDER_ONLY

            # 3. Custom unregistered mode
            custom_mode = "experimental_streaming_mode"
            draft_custom = TuiJobDraft(
                video="v.mp4",
                chat_html="c.html",
                mode=custom_mode,
            )
            app._populate_form_from_draft(draft_custom)
            await pilot.pause(0.05)
            assert app.query_one("#task-mode", Select).value == custom_mode
            assert app._read_form_draft().mode == custom_mode

    asyncio.run(run())


def test_form_draft_missing_and_none_edge_cases():
    """Test edge cases with empty strings, None, and extra_fields fallback."""
    async def run():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            # Offset in extra_fields fallback when draft.offset is None/empty
            draft_extra = TuiJobDraft(
                video="v.mp4",
                chat_html="c.html",
                offset="",
                extra_fields={"offset": "42.5"},
            )
            app._populate_form_from_draft(draft_extra)
            await pilot.pause(0.05)
            assert app.query_one("#offset", Input).value == "42.5"
            assert app._read_form_draft().offset == "42.5"

            # Invalid preview clip in input falls back safely
            app.query_one("#preview-clip", Input).value = "not-a-number"
            await pilot.pause(0.05)
            assert app._read_form_draft().preview_clip == 0.0

            # A valid preview clip round-trips through the form read (absorbed
            # from the former 20-field populate/read roundtrip test).
            app.query_one("#preview-clip", Input).value = "15.5"
            await pilot.pause(0.05)
            assert app._read_form_draft().preview_clip == 15.5

    asyncio.run(run())


# =============================================================================
# Area 2: Select Dropdown Value Changes and Reactivity
# =============================================================================


def test_select_dropdown_reactivity_and_validation(tmp_path: Path):
    """Test that changing Select dropdowns triggers reactivity and updates validation."""
    video = tmp_path / "video.mp4"
    chat = tmp_path / "chat.html"
    trans = tmp_path / "trans.json"
    video.write_text("dummy", encoding="utf-8")
    chat.write_text("dummy", encoding="utf-8")
    trans.write_text("{}", encoding="utf-8")

    async def run():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            await wait_for_widget(app, pilot, "#form-validation")
            # Populate basic valid draft
            app.query_one("#video", Input).value = str(video)
            app.query_one("#chat", Input).value = str(chat)
            app.query_one("#task-mode", Select).value = MODE_QUICK_PREVIEW_ORIGINAL

            val_widget = await wait_for_validation_state(app, pilot, present="表单检查通过")
            assert "ready" in val_widget.classes

            # Switch mode to step_resume_render (which requires translation_json)
            app.query_one("#task-mode", Select).value = MODE_STEP_RESUME_RENDER
            val_widget = await wait_for_validation_state(
                app, pilot, present="复用翻译渲染需要选择已存在的翻译 JSON"
            )
            assert "invalid" in val_widget.classes

            # Fill translation-json -> should flip back to ready (polled, not a fixed pause)
            app.query_one("#translation-json", Input).value = str(trans)
            val_widget = await wait_for_validation_state(app, pilot, present="表单检查通过")
            assert "ready" in val_widget.classes
            assert "表单检查通过" in str(val_widget.render())

            # Change layout preset, render preset, encoder, source media check
            app.query_one("#layout-preset", Select).value = "mobile"
            app.query_one("#render-preset", Select).value = "fast"
            app.query_one("#encoder", Select).value = "qsv"
            app.query_one("#source-media-check", Select).value = "fast"
            # Select.value assignment is synchronous; the poll below just makes
            # sure the reactivity pass has settled before the widget is read.
            val_widget = await wait_for_validation_state(app, pilot, present="表单检查通过")

            draft = app._read_form_draft()
            assert draft.layout_preset == "mobile"
            assert draft.render_preset == "fast"
            assert draft.encoder == "qsv"
            assert draft.source_media_check == "fast"
            assert "ready" in val_widget.classes

    asyncio.run(run())


def test_select_repeated_custom_values_no_duplication():
    """Verify _set_select_with_custom does not accumulate duplicate custom options.

    Public-interface check only: if a previous custom entry were kept (or the
    option list grew unboundedly), selecting it after returning to the standard
    options would still succeed instead of raising InvalidSelectValueError.
    """
    async def run():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            encoder_select = app.query_one("#encoder", Select)

            # Custom 1 registers and selects fine.
            app._set_select_with_custom("#encoder", _ENCODER_OPTIONS, "hevc_nvenc")
            await pilot.pause(0.02)
            assert encoder_select.value == "hevc_nvenc"

            # Custom 2 replaces (does not stack on) the previous custom entry.
            app._set_select_with_custom("#encoder", _ENCODER_OPTIONS, "libvpx-vp9")
            await pilot.pause(0.02)
            assert encoder_select.value == "libvpx-vp9"

            # Back to a standard value: the custom option must be gone again.
            app._set_select_with_custom("#encoder", _ENCODER_OPTIONS, "nvenc")
            await pilot.pause(0.02)
            assert encoder_select.value == "nvenc"
            with pytest.raises(InvalidSelectValueError):
                encoder_select.value = "hevc_nvenc"

    asyncio.run(run())


# =============================================================================
# Area 3: TUI-Level Invalid Offset Validation
# =============================================================================


@pytest.mark.parametrize(
    "invalid_input",
    ["invalid_text", "1e10", "-1e10", "nan", "inf", "++3"],
)
def test_invalid_and_out_of_range_offset_validation(invalid_input: str, tmp_path: Path):
    """Test that non-numeric or out-of-range offsets trigger form validation errors."""
    video = tmp_path / "vid.mp4"
    chat = tmp_path / "chat.html"
    video.write_text("x", encoding="utf-8")
    chat.write_text("x", encoding="utf-8")

    async def run():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            app.query_one("#video", Input).value = str(video)
            app.query_one("#chat", Input).value = str(chat)
            app.query_one("#offset", Input).value = invalid_input

            val_widget = await wait_for_validation_state(app, pilot, present="时间偏移")
            assert "invalid" in val_widget.classes

    asyncio.run(run())


# =============================================================================
# Area 4: Non-Blocking Nature of @work(thread=True) API Probe
# =============================================================================


async def _wait_for_visible(app, pilot, selector: str, *, attempts: int = 120, interval: float = 0.05):
    """Poll until the widget exists AND has been laid out with a non-zero region.

    Tab panes are mounted eagerly in textual 8.2.8, so DOM presence is
    immediate after switching tabs; only a non-zero region proves the pane is
    actually shown. A hidden pane yields a zero-size region and a silent no-op
    pilot.click, which a DOM-presence poll would miss.
    """
    for _ in range(attempts):
        matches = app.query(selector)
        if matches:
            widget = matches.first()
            if widget.region.width > 0 and widget.region.height > 0:
                return widget
        await pilot.pause(interval)
    raise AssertionError(f"{selector!r} never became visible within {attempts} polls")


def test_api_probe_non_blocking_event_loop_responsiveness():
    """Verify that network probe execution runs asynchronously in a thread

    without blocking the Textual UI event loop.
    """
    async def run():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            app.query_one(TabbedContent).active = "advanced"
            # Wait until the advanced pane is shown, not just mounted, so the
            # button click below actually lands.
            await _wait_for_visible(app, pilot, "#btn-test-api")

            app.query_one("#api-base-url", Input).value = "https://api.openai.com/v1"
            app.query_one("#api-key", Input).value = "sk-test-key-12345"
            app.query_one("#api-model", Input).value = "gpt-4o-mini"

            def slow_mock_probe(*args, **kwargs):
                # Simulate a 2000ms network round-trip in worker thread
                time.sleep(2.0)
                return (True, "API 连通成功")

            with patch("tui_run.probe_translate_api", side_effect=slow_mock_probe):
                # Click Test API button
                await pilot.click("#btn-test-api")

                feedback_widget = app.query_one("#api-status-feedback", Static)
                # Poll for the transient "connecting" state with relaxed OR
                # semantics; the mock probe needs 2s so success cannot show yet.
                initial = ""
                for _ in range(40):
                    await pilot.pause(0.05)
                    initial = str(feedback_widget.render())
                    if "正在连接" in initial or "稍候" in initial or "连通性测试成功" in initial:
                        break
                assert (
                    "正在连接" in initial or "稍候" in initial
                ), f"probe did not report the transient connecting state: {initial!r}"

                # While the probe sleeps 2.0s in the background thread, verify the
                # UI event loop responds rapidly.
                for i in range(4):
                    start_turn = time.perf_counter()
                    app.query_one("#offset", Input).value = f"{i}.5"
                    await pilot.pause(0.05)
                    elapsed_turn = time.perf_counter() - start_turn
                    # If the UI were blocked by the 2000ms probe, every turn
                    # would take >= 2.0s; the 1.0s bound tolerates loaded CI
                    # runners while still failing a blocked loop by a factor of 2.
                    assert elapsed_turn < 1.0, f"UI event loop was blocked! Turn elapsed: {elapsed_turn:.3f}s"

                # Wait for the background thread worker to deliver its result
                for _ in range(120):
                    await pilot.pause(0.05)
                    if "连通性测试成功" in str(feedback_widget.render()):
                        break

                # Check final feedback
                rendered_final = str(feedback_widget.render())
                assert "连通性测试成功" in rendered_final
                assert "gpt-4o-mini" in rendered_final
                assert "ms" in rendered_final

    asyncio.run(run())
