# -*- coding: utf-8 -*-
"""Empirical test suite for M2 Textual GUI implementation (Challenger 2).

Tests:
1. _read_form_draft() & _populate_form_from_draft() edge cases (custom codecs, custom layout yaml, non-standard modes, None/empty handling, roundtrips).
2. Select dropdown value changes and reactivity (validation updates, mode transitions).
3. Time offset (--offset) propagation into generated pipeline command and YAML serialization.
4. Non-blocking nature of @work(thread=True) API probe ensuring UI event loop responsiveness.
5. Tab navigation across all 6 tabs and widget presence.
6. Select custom option hygiene (no unbounded duplication).
7. Offset boundary & extreme values testing.
8. Core 3-path workflow mode command generation contracts.
9. Structured guidance on manual_required state.
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

from textual.widgets import Button, Checkbox, Input, OptionList, RichLog, Select, Static, TabbedContent

from helpers import wait_for_validation_state, wait_for_widget
from tui_history import TuiHistoryStore
from tui_models import (
    MODE_FULL_PRODUCTION,
    MODE_ORIGINAL_PRODUCTION,
    MODE_QUICK_PREVIEW_ORIGINAL,
    MODE_QUICK_PREVIEW_TRANSLATED,
    MODE_RENDER_ONLY,
    MODE_STEP_API_AND_REVIEW,
    MODE_STEP_EXPORT_MANUAL,
    MODE_STEP_RESUME_RENDER,
    TuiJobDraft,
)
from tui_run import (
    _ENCODER_OPTIONS,
    _UI_MODE_RENDER_ORIGINAL,
    OverlayTui,
)

# =============================================================================
# Area 1: _read_form_draft() & _populate_form_from_draft() Edge Cases
# =============================================================================


def test_form_draft_roundtrip_standard_presets():
    """Test full form round-trip with standard preset values."""
    async def run():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            original_draft = TuiJobDraft(
                video="test_video.mp4",
                chat_html="test_chat.html",
                output="test_out.mp4",
                translation_json="trans.json",
                target_language="ja",
                mode=MODE_STEP_API_AND_REVIEW,
                layout_preset="compact",
                render_preset="hq",
                preview_clip=15.5,
                profile="prof.yaml",
                rules="rules.yaml",
                encoder="nvenc",
                source_media_check="fast",
                crf="19",
                workers="4",
                keep_temp=True,
                review=True,
                manual_translation=False,
                offset="-3.5",
                source_job="job.yaml",
            )

            app._populate_form_from_draft(original_draft)
            await pilot.pause(0.05)

            read_draft = app._read_form_draft()

            assert read_draft.video == "test_video.mp4"
            assert read_draft.chat_html == "test_chat.html"
            assert read_draft.output == "test_out.mp4"
            assert read_draft.translation_json == "trans.json"
            assert read_draft.target_language == "ja"
            assert read_draft.mode == MODE_STEP_API_AND_REVIEW
            assert read_draft.layout_preset == "compact"
            assert read_draft.render_preset == "hq"
            assert read_draft.preview_clip == 15.5
            assert read_draft.profile == "prof.yaml"
            assert read_draft.rules == "rules.yaml"
            assert read_draft.encoder == "nvenc"
            assert read_draft.source_media_check == "fast"
            assert read_draft.crf == "19"
            assert read_draft.workers == "4"
            assert read_draft.keep_temp is True
            assert read_draft.review is True
            assert read_draft.manual_translation is False
            assert read_draft.offset == "-3.5"
            assert read_draft.source_job == "job.yaml"

    asyncio.run(run())


def test_form_draft_custom_codec():
    """Test custom video codecs not in the default Select list (e.g. hevc_nvenc, libvpx-vp9)."""
    async def run():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            # 1. Custom codec: hevc_nvenc
            draft_hevc = TuiJobDraft(
                video="v.mp4",
                chat_html="c.html",
                encoder="hevc_nvenc",
                mode=MODE_FULL_PRODUCTION,
            )
            app._populate_form_from_draft(draft_hevc)
            await pilot.pause(0.05)

            encoder_select = app.query_one("#encoder", Select)
            assert encoder_select.value == "hevc_nvenc"
            # Verify custom option exists in options
            option_values = [val for _lbl, val in encoder_select._options]
            assert "hevc_nvenc" in option_values
            assert app._read_form_draft().encoder == "hevc_nvenc"

            # 2. Another custom codec: libvpx-vp9
            draft_vp9 = TuiJobDraft(
                video="v.mp4",
                chat_html="c.html",
                encoder="libvpx-vp9",
                mode=MODE_FULL_PRODUCTION,
            )
            app._populate_form_from_draft(draft_vp9)
            await pilot.pause(0.05)

            assert encoder_select.value == "libvpx-vp9"
            assert app._read_form_draft().encoder == "libvpx-vp9"

    asyncio.run(run())


def test_form_draft_custom_layout_yaml_path():
    """Test custom layout yaml paths loaded from external YAML jobs."""
    async def run():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            custom_layout = "configs/layouts/super_wide_4k.yaml"
            draft = TuiJobDraft(
                video="v.mp4",
                chat_html="c.html",
                layout_preset=custom_layout,
                mode=MODE_FULL_PRODUCTION,
            )
            app._populate_form_from_draft(draft)
            await pilot.pause(0.05)

            layout_select = app.query_one("#layout-preset", Select)
            assert layout_select.value == custom_layout
            option_values = [val for _lbl, val in layout_select._options]
            assert custom_layout in option_values
            assert app._read_form_draft().layout_preset == custom_layout

    asyncio.run(run())


def test_form_draft_custom_render_preset():
    """Test custom render presets (e.g. custom ultrafast lossless profile)."""
    async def run():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            custom_preset = "lossless_ultrafast_cqp0"
            draft = TuiJobDraft(
                video="v.mp4",
                chat_html="c.html",
                render_preset=custom_preset,
                mode=MODE_FULL_PRODUCTION,
            )
            app._populate_form_from_draft(draft)
            await pilot.pause(0.05)

            # Startup window: pane widgets mount asynchronously after run_test.
            preset_select = await wait_for_widget(app, pilot, "#render-preset")
            assert preset_select.value == custom_preset
            assert app._read_form_draft().render_preset == custom_preset

    asyncio.run(run())


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

            # Fill translation-json -> should immediately become ready
            app.query_one("#translation-json", Input).value = str(trans)
            await pilot.pause(0.1)

            assert "ready" in val_widget.classes
            assert "表单检查通过" in str(val_widget.render())

            # Change layout preset, render preset, encoder, source media check
            app.query_one("#layout-preset", Select).value = "mobile"
            app.query_one("#render-preset", Select).value = "fast"
            app.query_one("#encoder", Select).value = "qsv"
            app.query_one("#source-media-check", Select).value = "fast"
            await pilot.pause(0.1)

            draft = app._read_form_draft()
            assert draft.layout_preset == "mobile"
            assert draft.render_preset == "fast"
            assert draft.encoder == "qsv"
            assert draft.source_media_check == "fast"
            assert "ready" in val_widget.classes

    asyncio.run(run())


def test_select_repeated_custom_values_no_duplication():
    """Verify _set_select_with_custom does not accumulate duplicate custom options."""
    async def run():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            encoder_select = app.query_one("#encoder", Select)

            # Standard options length
            std_count = len(_ENCODER_OPTIONS)

            # Set custom 1
            app._set_select_with_custom("#encoder", _ENCODER_OPTIONS, "hevc_nvenc")
            await pilot.pause(0.02)
            assert len(encoder_select._options) == std_count + 1

            # Set custom 2
            app._set_select_with_custom("#encoder", _ENCODER_OPTIONS, "libvpx-vp9")
            await pilot.pause(0.02)
            assert len(encoder_select._options) == std_count + 1

            # Set back to standard
            app._set_select_with_custom("#encoder", _ENCODER_OPTIONS, "nvenc")
            await pilot.pause(0.02)
            assert len(encoder_select._options) == std_count
            assert encoder_select.value == "nvenc"

    asyncio.run(run())


# =============================================================================
# Area 3: Time Offset Parameter Propagation & Validation
# =============================================================================


@pytest.mark.parametrize(
    ("offset_input", "expected_flag_value"),
    [
        ("12.5", "12.5"),
        ("-3.0", "-3.0"),
        ("0.0", "0.0"),
        ("120.0", "120.0"),
        ("-0.5", "-0.5"),
        (" +5.2 ", "5.2"),
        ("-0.0", "0.0"),
    ],
)
def test_offset_parameter_propagation_to_command(offset_input: str, expected_flag_value: str, tmp_path: Path):
    """Test offset parameter is passed from UI input into draft and built command."""
    video = tmp_path / "vid.mp4"
    chat = tmp_path / "chat.html"
    video.write_text("x", encoding="utf-8")
    chat.write_text("x", encoding="utf-8")

    async def run():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            app.query_one("#video", Input).value = str(video)
            app.query_one("#chat", Input).value = str(chat)
            app.query_one("#offset", Input).value = offset_input
            app.query_one("#task-mode", Select).value = MODE_QUICK_PREVIEW_ORIGINAL
            await pilot.pause(0.1)

            draft = app._read_form_draft()
            assert draft.offset == offset_input.strip()

            cmd = draft.build_command("python", "render_cn_chat.py")
            assert "--offset" in cmd
            offset_idx = cmd.index("--offset")
            assert float(cmd[offset_idx + 1]) == float(expected_flag_value)

    asyncio.run(run())


def test_blank_offset_omitted_from_command(tmp_path: Path):
    """Test blank offset leaves --offset out of generated command."""
    video = tmp_path / "vid.mp4"
    chat = tmp_path / "chat.html"
    video.write_text("x", encoding="utf-8")
    chat.write_text("x", encoding="utf-8")

    async def run():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            app.query_one("#video", Input).value = str(video)
            app.query_one("#chat", Input).value = str(chat)
            app.query_one("#offset", Input).value = "   "
            await pilot.pause(0.1)

            draft = app._read_form_draft()
            assert draft.offset == ""
            cmd = draft.build_command("python", "render_cn_chat.py")
            assert "--offset" not in cmd

    asyncio.run(run())


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
            await pilot.pause(0.1)

            val_widget = await wait_for_validation_state(app, pilot, present="时间偏移")
            assert "invalid" in val_widget.classes

    asyncio.run(run())


# =============================================================================
# Area 4: Non-Blocking Nature of @work(thread=True) API Probe
# =============================================================================


def test_api_probe_non_blocking_event_loop_responsiveness():
    """Verify that network probe execution runs asynchronously in a thread

    without blocking the Textual UI event loop.
    """
    async def run():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            app.query_one(TabbedContent).active = "advanced"
            await pilot.pause(0.1)

            app.query_one("#api-base-url", Input).value = "https://api.openai.com/v1"
            app.query_one("#api-key", Input).value = "sk-test-key-12345"
            app.query_one("#api-model", Input).value = "gpt-4o-mini"
            await pilot.pause(0.05)

            def slow_mock_probe(*args, **kwargs):
                # Simulate a 1000ms network round-trip in worker thread
                time.sleep(1.0)
                return (True, "API 连通成功")

            with patch("tui_run.probe_translate_api", side_effect=slow_mock_probe):
                # Click Test API button
                await pilot.click("#btn-test-api")
                # Give tiny pause for thread worker to start and send initial feedback
                await pilot.pause(0.05)

                feedback_widget = app.query_one("#api-status-feedback", Static)
                rendered_initial = str(feedback_widget.render())
                assert "正在连接" in rendered_initial or "稍候" in rendered_initial

                # While probe is sleeping in background thread for 1.0s, verify UI event loop responds rapidly
                for i in range(4):
                    start_turn = time.perf_counter()
                    app.query_one("#offset", Input).value = f"{i}.5"
                    await pilot.pause(0.05)
                    elapsed_turn = time.perf_counter() - start_turn
                    # If UI were blocked by the 1000ms probe, this would take >= 1.0s.
                    # 750ms tolerates loaded CI runners while still proving non-blocking.
                    assert elapsed_turn < 0.75, f"UI event loop was blocked! Turn elapsed: {elapsed_turn:.3f}s"

                # Wait for the background thread worker to deliver its result
                for _ in range(60):
                    await pilot.pause(0.05)
                    if "连通性测试成功" in str(feedback_widget.render()):
                        break

                # Check final feedback
                rendered_final = str(feedback_widget.render())
                assert "连通性测试成功" in rendered_final
                assert "gpt-4o-mini" in rendered_final
                assert "ms" in rendered_final

    asyncio.run(run())


def test_api_probe_failure_reporting():
    """Verify failed API probe gracefully reports failure diagnostics."""
    async def run():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            app.query_one(TabbedContent).active = "advanced"
            await pilot.pause(0.1)

            app.query_one("#api-base-url", Input).value = "https://api.broken.test/v1"
            app.query_one("#api-key", Input).value = "sk-bad-key"
            app.query_one("#api-model", Input).value = "deepseek-chat"

            with patch("tui_run.probe_translate_api", return_value=(False, "401 Unauthorized: Invalid API key")):
                await pilot.click("#btn-test-api")
                await pilot.pause(0.1)

                feedback = app.query_one("#api-status-feedback", Static)
                assert "连通性测试失败" in str(feedback.render())
                assert "401 Unauthorized" in str(feedback.render())
                assert "401 Unauthorized" in str(app.query_one("#status", Static).render())

    asyncio.run(run())


def test_api_config_save_feedback():
    """Verify saving API config writes to .env and updates UI feedback."""
    async def run():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            app.query_one(TabbedContent).active = "advanced"
            await pilot.pause(0.1)

            app.query_one("#api-base-url", Input).value = "https://api.custom.ai/v1"
            app.query_one("#api-key", Input).value = "sk-custom-secret"
            app.query_one("#api-model", Input).value = "deepseek-v3"

            with patch("tui_run.save_dotenv_api_config", return_value=(True, "OK")) as mock_save:
                await pilot.click("#btn-save-api")
                await pilot.pause(0.05)

                mock_save.assert_called_once_with("https://api.custom.ai/v1", "sk-custom-secret", "deepseek-v3")
                feedback = app.query_one("#api-status-feedback", Static)
                assert "配置已成功保存至本地 .env 文件" in str(feedback.render())

    asyncio.run(run())


# =============================================================================
# Area 5: All 6 Tabs Presence and Navigation
# =============================================================================


def test_all_six_tabs_presence_and_navigation():
    """Verify that all 6 tabs exist, can be navigated to, and critical widgets are found."""
    async def run():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            tabbed = app.query_one(TabbedContent)

            # 1. download tab
            tabbed.active = "download"
            await pilot.pause(0.05)
            assert app.query_one("#download-url", Input)
            assert app.query_one("#download-quality", Input)
            assert app.query_one("#download-media-check", Select)
            assert app.query_one("#download-start", Button)

            # 2. new-task tab
            tabbed.active = "new-task"
            await pilot.pause(0.05)
            assert app.query_one("#task-mode", Select)
            assert app.query_one("#video", Input)
            assert app.query_one("#chat", Input)
            assert app.query_one("#offset", Input)
            assert app.query_one("#run-mode", Button)

            # 3. task tab
            tabbed.active = "task"
            await pilot.pause(0.05)
            assert app.query_one("#log", RichLog)
            assert app.query_one("#doctor", Button)
            assert app.query_one("#cancel", Button)
            assert app.query_one("#export-diagnostics", Button)

            # 4. jobs tab
            tabbed.active = "jobs"
            await pilot.pause(0.05)
            assert app.query_one("#job-path", Input)
            assert app.query_one("#load-job", Button)
            assert app.query_one("#save-job", Button)
            assert app.query_one("#pin-paths", Checkbox)

            # 5. advanced tab
            tabbed.active = "advanced"
            await pilot.pause(0.05)
            assert app.query_one("#api-base-url", Input)
            assert app.query_one("#api-key", Input)
            assert app.query_one("#api-model", Input)
            assert app.query_one("#btn-test-api", Button)
            assert app.query_one("#btn-save-api", Button)
            assert app.query_one("#layout-preset", Select)
            assert app.query_one("#render-preset", Select)
            assert app.query_one("#encoder", Select)
            assert app.query_one("#source-media-check", Select)

            # 6. history tab
            tabbed.active = "history"
            await pilot.pause(0.05)
            assert app.query_one("#history-list", OptionList)
            assert app.query_one("#history-refresh", Button)
            assert app.query_one("#history-rerun", Button)
            assert app.query_one("#history-clear", Button)

    asyncio.run(run())


# =============================================================================
# Area 6: Core 3-Path Workflow Mode CLI Contracts
# =============================================================================


@pytest.mark.parametrize(
    ("mode_key", "expected_flags"),
    [
        (MODE_QUICK_PREVIEW_ORIGINAL, ["--mode", "preview", "--render-original", "--preview-clip", "10.0"]),
        (MODE_QUICK_PREVIEW_TRANSLATED, ["--mode", "preview", "--preview-clip", "10.0"]),
        (MODE_FULL_PRODUCTION, ["--mode", "full"]),
        (MODE_ORIGINAL_PRODUCTION, ["--mode", "render", "--render-original"]),
        (MODE_STEP_EXPORT_MANUAL, ["--manual-translation"]),
        (MODE_STEP_API_AND_REVIEW, ["--review"]),
        (MODE_STEP_RESUME_RENDER, ["--mode", "render", "--reuse-translation"]),
    ],
)
def test_core_three_paths_command_generation(mode_key: str, expected_flags: list[str], tmp_path: Path):
    """Verify CLI flags generated for all 7 core workflow modes."""
    video = tmp_path / "vid.mp4"
    chat = tmp_path / "chat.html"
    video.write_text("x", encoding="utf-8")
    chat.write_text("x", encoding="utf-8")

    draft = TuiJobDraft(
        video=str(video),
        chat_html=str(chat),
        translation_json=str(tmp_path / "trans.json") if "resume" in mode_key else "",
        mode=mode_key,
    )
    cmd = draft.build_command("python", "render_cn_chat.py")

    for i in range(len(expected_flags)):
        flag = expected_flags[i]
        assert flag in cmd, f"Expected flag {flag} not in command {cmd}"
        if i + 1 < len(expected_flags) and not expected_flags[i + 1].startswith("--"):
            val = expected_flags[i + 1]
            idx = cmd.index(flag)
            assert cmd[idx + 1] == val


# =============================================================================
# Area 7: Structured Guidance on manual_required Exit
# =============================================================================


def test_manual_required_state_guidance_display(tmp_path: Path):
    """Verify structured guidance status message when a task halts on manual_required."""
    child_script = (
        "import json, os; "
        "p=os.environ['TWITCH_OVERLAY_RESULT_FILE']; "
        "open(p, 'w', encoding='utf-8').write(json.dumps({'schema_version':1,'state':'manual_required','mode':'full','returncode':0,'finished_at':1,'artifacts':[]}))"
    )

    async def run():
        app = OverlayTui()
        app.history = TuiHistoryStore(tmp_path / "history.json")
        async with app.run_test(size=(140, 50)) as pilot:
            app._start_command("translate", [sys.executable, "-c", child_script])
            for _ in range(80):
                await pilot.pause(0.05)
                if app.session and not app.session.running and app._handled_session is app.session:
                    break

            assert app.history.list_records()[0]["state"] == "manual_required"
            status_text = str(app.query_one("#status", Static).render())
            assert "人工复核已就绪" in status_text
            assert "载入复核表并压制" in status_text

    asyncio.run(run())
