from __future__ import annotations

import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from textual.widgets import Button, Checkbox, Input, OptionList, RichLog, Select, TabbedContent

from helpers import wait_for_validation_state, wait_for_widget
from tui_history import TuiHistoryStore
from tui_models import (
    MODE_FULL_PRODUCTION,
    MODE_ORIGINAL_PRODUCTION,
    MODE_QUICK_PREVIEW_ORIGINAL,
    MODE_QUICK_PREVIEW_TRANSLATED,
    MODE_STEP_API_AND_REVIEW,
    MODE_STEP_EXPORT_MANUAL,
    MODE_STEP_RESUME_RENDER,
    TuiJobDraft,
)
from tui_run import (
    _ENCODER_OPTIONS,
    _LAYOUT_PRESET_OPTIONS,
    _RENDER_PRESET_OPTIONS,
    OverlayTui,
)


def test_tui_tab_switching_stress():
    """Stress test switching across all 6 tabs in multiple orders without crashing.

    Also verifies each tab's critical widgets (absorbed from the former
    single-pass tab-presence inventory test).
    """
    async def exercise():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            tabs = ["download", "new-task", "task", "jobs", "advanced", "history"]
            tabbed_content = app.query_one(TabbedContent)

            tab_widgets = {
                "download": [
                    ("#download-url", Input),
                    ("#download-quality", Input),
                    ("#download-media-check", Select),
                    ("#download-start", Button),
                ],
                "new-task": [
                    ("#task-mode", Select),
                    ("#video", Input),
                    ("#chat", Input),
                    ("#offset", Input),
                    ("#run-mode", Button),
                ],
                "task": [
                    ("#log", RichLog),
                    ("#doctor", Button),
                    ("#cancel", Button),
                    ("#export-diagnostics", Button),
                ],
                "jobs": [
                    ("#job-path", Input),
                    ("#load-job", Button),
                    ("#save-job", Button),
                    ("#pin-paths", Checkbox),
                ],
                "advanced": [
                    ("#api-base-url", Input),
                    ("#api-key", Input),
                    ("#api-model", Input),
                    ("#btn-test-api", Button),
                    ("#btn-save-api", Button),
                    ("#layout-preset", Select),
                    ("#render-preset", Select),
                    ("#encoder", Select),
                    ("#source-media-check", Select),
                ],
                "history": [
                    ("#history-list", OptionList),
                    ("#history-refresh", Button),
                    ("#history-rerun", Button),
                    ("#history-clear", Button),
                ],
            }

            # Forward transition, checking every critical widget of each tab
            for tab_id in tabs:
                tabbed_content.active = tab_id
                await pilot.pause(0.02)
                assert tabbed_content.active == tab_id
                for selector, widget_type in tab_widgets[tab_id]:
                    assert app.query_one(selector, widget_type) is not None, selector

            # Reverse transition
            for tab_id in reversed(tabs):
                tabbed_content.active = tab_id
                await pilot.pause(0.02)
                assert tabbed_content.active == tab_id

            # Random-access jump transitions
            for tab_id in ["advanced", "download", "history", "new-task", "jobs", "task"]:
                tabbed_content.active = tab_id
                await pilot.pause(0.02)
                assert tabbed_content.active == tab_id

            # Verify key widgets across all tabs are present and healthy
            assert app.query_one("#download-url", Input) is not None
            assert app.query_one("#task-mode", Select) is not None
            assert app.query_one("#log", RichLog) is not None
            assert app.query_one("#job-path", Input) is not None
            assert app.query_one("#api-base-url", Input) is not None
            assert app.query_one("#history-list", OptionList) is not None

    asyncio.run(exercise())


def test_tui_select_controls_and_custom_options():
    """Stress test standard Select widgets and dynamic custom options injection."""
    async def exercise():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            # 1. Standard options for layout preset
            layout_select = app.query_one("#layout-preset", Select)
            assert layout_select.value == "default"
            for _, val in _LAYOUT_PRESET_OPTIONS:
                layout_select.value = val
                await pilot.pause(0.01)
                assert app.query_one("#layout-preset", Select).value == val

            # 2. Dynamic custom layout preset
            app._set_select_with_custom("#layout-preset", _LAYOUT_PRESET_OPTIONS, "my_custom_layout.yaml")
            await pilot.pause(0.01)
            assert app.query_one("#layout-preset", Select).value == "my_custom_layout.yaml"
            read_draft = app._draft()
            assert read_draft.layout_preset == "my_custom_layout.yaml"

            # 3. Dynamic custom encoder
            encoder_select = app.query_one("#encoder", Select)
            assert encoder_select.value == "auto"
            app._set_select_with_custom("#encoder", _ENCODER_OPTIONS, "hevc_nvenc")
            await pilot.pause(0.01)
            assert app.query_one("#encoder", Select).value == "hevc_nvenc"
            assert app._draft().encoder == "hevc_nvenc"

            # 4. Dynamic custom render preset
            app._set_select_with_custom("#render-preset", _RENDER_PRESET_OPTIONS, "ultra_fast_draft")
            await pilot.pause(0.01)
            assert app.query_one("#render-preset", Select).value == "ultra_fast_draft"
            assert app._draft().render_preset == "ultra_fast_draft"

            # 5. Empty or whitespace custom fallback
            app._set_select_with_custom("#layout-preset", _LAYOUT_PRESET_OPTIONS, "   ")
            await pilot.pause(0.01)
            assert app.query_one("#layout-preset", Select).value == "default"

    asyncio.run(exercise())


def test_tui_offset_inputs_and_validation(tmp_path: Path):
    """Stress test offset inputs (positive, negative, zero, boundary, invalid) and form validation."""
    video = tmp_path / "v.mp4"
    chat = tmp_path / "c.html"
    video.write_text("x", encoding="utf-8")
    chat.write_text("x", encoding="utf-8")

    async def exercise():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            await wait_for_widget(app, pilot, "#form-validation")
            app._set_input("#video", str(video))
            app._set_input("#chat", str(chat))
            app._set_select("#task-mode", MODE_QUICK_PREVIEW_ORIGINAL)

            # Valid positive offset
            app._set_input("#offset", "12.5")
            assert app._draft().offset == "12.5"
            await wait_for_validation_state(app, pilot, absent="待处理")
            cmd = app._draft().build_command("python", "render_cn_chat.py")
            assert "--offset" in cmd and "12.5" in cmd

            # Valid negative offset
            app._set_input("#offset", "-8.25")
            assert app._draft().offset == "-8.25"
            await wait_for_validation_state(app, pilot, absent="待处理")
            cmd = app._draft().build_command("python", "render_cn_chat.py")
            assert "--offset" in cmd and "-8.25" in cmd

            # Valid zero and empty offset
            app._set_input("#offset", "0.0")
            assert app._draft().offset == "0.0"
            await wait_for_validation_state(app, pilot, absent="待处理")

            app._set_input("#offset", "")
            assert app._draft().offset == ""
            await wait_for_validation_state(app, pilot, absent="待处理")

            # Boundary extremes
            app._set_input("#offset", "604800.0")
            assert app._draft().offset == "604800.0"
            await wait_for_validation_state(app, pilot, absent="待处理")

            app._set_input("#offset", "-604800.0")
            assert app._draft().offset == "-604800.0"
            await wait_for_validation_state(app, pilot, absent="待处理")

            # Out-of-bounds offset
            app._set_input("#offset", "604801.0")
            validation = await wait_for_validation_state(app, pilot, present="时间偏移")
            assert "待处理" in str(validation.render())

            app._set_input("#offset", "-604801.0")
            validation = await wait_for_validation_state(app, pilot, present="时间偏移")
            assert "待处理" in str(validation.render())

            # Malformed non-numeric values
            for bad_val in ["abc", "12.3.4", "--5", "NaN", "Infinity", "1e99999"]:
                app._set_input("#offset", bad_val)
                validation = await wait_for_validation_state(app, pilot, present="时间偏移")
                assert "待处理" in str(validation.render()), f"Expected validation failure for offset: {bad_val}"

            # Command propagation for representative values (absorbed from the
            # former parametrized propagation test). build_command serializes
            # the offset as a float, so compare numerically.
            for value, expected in (("0.0", 0.0), ("120.0", 120.0), ("  +5.2  ", 5.2), ("-0.0", 0.0)):
                app._set_input("#offset", value)
                assert app._draft().offset == value.strip()
                cmd = app._draft().build_command("python", "render_cn_chat.py")
                assert "--offset" in cmd
                assert float(cmd[cmd.index("--offset") + 1]) == expected

            # Blank offset must omit the flag from the generated command entirely.
            app._set_input("#offset", "")
            assert app._draft().offset == ""
            assert "--offset" not in app._draft().build_command("python", "render_cn_chat.py")

    asyncio.run(exercise())


def test_tui_all_workflow_modes_command_generation(tmp_path: Path):
    """Test all 3 core workflow paths and legacy modes in TUI draft building."""
    video = tmp_path / "video.mp4"
    chat = tmp_path / "chat.html"
    trans = tmp_path / "trans.json"
    video.write_text("x", encoding="utf-8")
    chat.write_text("x", encoding="utf-8")
    trans.write_text("{}", encoding="utf-8")

    async def exercise():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            app._set_input("#video", str(video))
            app._set_input("#chat", str(chat))
            app._set_input("#translation-json", str(trans))
            app._set_input("#offset", "-3.0")

            # 1. Core Path: Quick Preview Original
            app._set_task_mode_options(value=MODE_QUICK_PREVIEW_ORIGINAL)
            await pilot.pause(0.02)
            cmd = app._draft().build_command("python", "render_cn_chat.py")
            assert "--mode" in cmd and "preview" in cmd
            assert "--render-original" in cmd
            assert "--preview-clip" in cmd and "10.0" in cmd
            assert "--offset" in cmd and "-3.0" in cmd

            # 2. Core Path: Quick Preview Translated
            app._set_task_mode_options(value=MODE_QUICK_PREVIEW_TRANSLATED)
            await pilot.pause(0.02)
            cmd = app._draft().build_command("python", "render_cn_chat.py")
            assert "--mode" in cmd and "preview" in cmd
            assert "--render-original" not in cmd
            assert "--preview-clip" in cmd and "10.0" in cmd

            # 3. Core Path: Full Production (One-Click)
            app._set_task_mode_options(value=MODE_FULL_PRODUCTION)
            await pilot.pause(0.02)
            cmd = app._draft().build_command("python", "render_cn_chat.py")
            assert "--mode" in cmd and "full" in cmd

            # 4. Core Path: Original Production
            app._set_task_mode_options(value=MODE_ORIGINAL_PRODUCTION)
            await pilot.pause(0.02)
            cmd = app._draft(render_original=True).build_command("python", "render_cn_chat.py")
            assert "--mode" in cmd and "render" in cmd
            assert "--render-original" in cmd

            # 5. Core Path: Step Export Manual
            app._set_task_mode_options(value=MODE_STEP_EXPORT_MANUAL)
            await pilot.pause(0.02)
            cmd = app._draft().build_command("python", "render_cn_chat.py")
            assert "--manual-translation" in cmd

            # 6. Core Path: Step API and Review
            app._set_task_mode_options(value=MODE_STEP_API_AND_REVIEW)
            await pilot.pause(0.02)
            cmd = app._draft().build_command("python", "render_cn_chat.py")
            assert "--review" in cmd

            # 7. Core Path: Step Resume Render
            app._set_task_mode_options(value=MODE_STEP_RESUME_RENDER)
            await pilot.pause(0.02)
            cmd = app._draft().build_command("python", "render_cn_chat.py")
            assert "--mode" in cmd and "render" in cmd
            assert "--reuse-translation" in cmd

    asyncio.run(exercise())


def test_tui_draft_save_load_roundtrip_and_corruption_handling(tmp_path: Path):
    """Stress test Draft Save / Load through GUI buttons and corrupted file handling."""
    job_file = tmp_path / "saved_job.yaml"
    video = tmp_path / "v_source.mp4"
    chat = tmp_path / "c_source.html"
    trans = tmp_path / "t_source.json"
    video.write_text("x", encoding="utf-8")
    chat.write_text("x", encoding="utf-8")
    trans.write_text("{}", encoding="utf-8")

    async def exercise():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            # Populate rich form inputs
            app.query_one(TabbedContent).active = "new-task"
            app._set_input("#video", str(video))
            app._set_input("#chat", str(chat))
            app._set_input("#output", str(tmp_path / "out_custom.mp4"))
            app._set_input("#preview-clip", "15.5")
            app._set_input("#offset", "-12.75")
            app._set_task_mode_options(value=MODE_FULL_PRODUCTION)

            app.query_one(TabbedContent).active = "jobs"
            app._set_input("#job-path", str(job_file))
            app._set_input("#translation-json", str(trans))

            app.query_one(TabbedContent).active = "advanced"
            app._set_input("#target-language", "ja")
            app._set_select_with_custom("#layout-preset", _LAYOUT_PRESET_OPTIONS, "mobile")
            app._set_select_with_custom("#render-preset", _RENDER_PRESET_OPTIONS, "hq")
            app._set_select_with_custom("#encoder", _ENCODER_OPTIONS, "nvenc")
            app._set_select("#source-media-check", "fast")
            app._set_input("#crf", "20")
            app._set_input("#workers", "4")
            app.query_one("#keep-temp", Checkbox).value = True
            app.query_one("#review", Checkbox).value = True
            app._set_input("#profile", "prof.yaml")
            app._set_input("#rules", "rules.yaml")

            # Save via _save_job()
            app._save_job()
            await pilot.pause(0.05)
            assert job_file.is_file()
            assert "已保存 YAML" in str(app.query_one("#status").render())

            # Wipe out GUI fields
            app._apply_draft(TuiJobDraft())
            assert app._draft().offset == ""
            assert app._draft().video == ""

            # Load back via _load_job()
            app._set_input("#job-path", str(job_file))
            app._load_job()
            await pilot.pause(0.05)
            assert "已导入 YAML" in str(app.query_one("#status").render())

            # Verify all restored fields
            restored = app._draft()
            assert restored.video == str(video)
            assert restored.chat_html == str(chat)
            assert restored.output == str(tmp_path / "out_custom.mp4")
            assert restored.offset == "-12.75"
            assert restored.layout_preset == "mobile"
            assert restored.render_preset == "hq"
            assert restored.encoder == "nvenc"
            assert restored.target_language == "ja"
            assert restored.crf == "20"
            assert restored.workers == "4"
            assert restored.keep_temp is True
            assert restored.review is True
            assert restored.manual_translation is False
            assert restored.translation_json == str(trans)
            assert restored.profile == "prof.yaml"
            assert restored.rules == "rules.yaml"
            assert restored.source_job == str(job_file.resolve())

            # Test Corrupted YAML handling: non-existent file
            app._set_input("#job-path", str(tmp_path / "non_existent.yaml"))
            app._load_job()
            await pilot.pause(0.02)
            assert "无法导入 YAML" in str(app.query_one("#status").render())

            # Test Corrupted YAML handling: top-level list
            list_yaml_file = tmp_path / "list.yaml"
            list_yaml_file.write_text("- item1\n- item2\n", encoding="utf-8")
            app._set_input("#job-path", str(list_yaml_file))
            app._load_job()
            await pilot.pause(0.02)
            assert "无法导入 YAML" in str(app.query_one("#status").render())

    asyncio.run(exercise())


def test_tui_task_dispatch_and_manual_required_lifecycle(tmp_path: Path):
    """Stress test Task dispatch, manual_required state guidance, failure diagnostics, and cancel."""
    history_file = tmp_path / "history.json"
    app = OverlayTui()
    app.history = TuiHistoryStore(history_file)

    async def exercise():
        async with app.run_test(size=(140, 50)) as pilot:
            # Readiness barrier: poll until the validation widget exists instead
            # of trusting a fixed pause on loaded CI runners.
            await wait_for_widget(app, pilot, "#form-validation")
            # 1. manual_required lifecycle
            manual_script = (
                "import json, os; "
                "p=os.environ['TWITCH_OVERLAY_RESULT_FILE']; "
                "open(p, 'w', encoding='utf-8').write(json.dumps({"
                "'schema_version':1,'state':'manual_required','mode':'full','returncode':0,'finished_at':1,'artifacts':[]"
                "}))"
            )
            app._start_command("translate", [sys.executable, "-c", manual_script])
            # 12s ceiling for cold CI runners; breaks as soon as the session is handled.
            for _ in range(240):
                await pilot.pause(0.05)
                if app.session and not app.session.running and app._handled_session is app.session:
                    break

            assert app.history.list_records()[0]["state"] == "manual_required"
            status_text = str(app.query_one("#status").render())
            # Both guidance fragments must be present (absorbed from the former
            # standalone manual_required guidance test).
            assert "人工复核已就绪" in status_text
            assert "载入复核表并压制" in status_text

            # 2. Failed task diagnostics lifecycle
            fail_script = "import sys; print('Fatal error occurred'); raise SystemExit(42)"
            app._start_command("failing_task", [sys.executable, "-c", fail_script])
            # 12s ceiling for cold CI runners; breaks as soon as the session is handled.
            for _ in range(240):
                await pilot.pause(0.05)
                if app.session and not app.session.running and app._handled_session is app.session:
                    break

            records = app.history.list_records()
            assert records[0]["state"] == "failed"
            assert "任务失败（退出码 42）" in str(app.query_one("#status").render())

            # 3. Concurrent launch prevention
            slow_script = "import time; time.sleep(1.0)"
            app._start_command("slow_task", [sys.executable, "-c", slow_script])
            await pilot.pause(0.05)
            assert app.session.running is True

            # Try to launch second task
            app._start_command("second_task", [sys.executable, "-c", "print('second')"])
            assert "已有任务正在运行" in str(app.query_one("#status").render())

            # 4. Cancel running task
            app._cancel_task()
            for _ in range(240):
                await pilot.pause(0.05)
                if app.session and not app.session.running:
                    break
            assert app.session.cancelled is True

    asyncio.run(exercise())


def test_tui_history_clear_safety_and_record_loading(tmp_path: Path):
    """Stress test History interactions: double-click clear confirmation, draft loading."""
    history_file = tmp_path / "history.json"
    store = TuiHistoryStore(history_file)
    draft1 = TuiJobDraft(video="v1.mp4", chat_html="c1.html", offset="5.0", mode=MODE_FULL_PRODUCTION)
    rec1 = store.start(draft1, label="task_1")
    store.finish(rec1["id"], state="succeeded", returncode=0, result_path=None)

    app = OverlayTui()
    app.history = store

    async def exercise():
        async with app.run_test(size=(140, 50)) as pilot:
            app.query_one(TabbedContent).active = "history"
            app._refresh_history()
            await pilot.pause(0.05)

            # Select record
            app._select_history(rec1["id"])
            await pilot.pause(0.02)
            assert app.selected_history_id == rec1["id"]

            # Load draft from history
            app._load_history_draft()
            await pilot.pause(0.02)
            assert app._draft().video == "v1.mp4"
            assert app._draft().offset == "5.0"

            # Test Clear History first click (confirmation required)
            app._clear_history()
            assert "再次点击确认" in str(app.query_one("#status").render())
            assert len(app.history.list_records()) == 1

            # Second click within timeout -> cleared
            app._clear_history()
            assert "本机任务历史已清空" in str(app.query_one("#status").render())
            assert len(app.history.list_records()) == 0

    asyncio.run(exercise())
