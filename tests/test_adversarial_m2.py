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

from textual.widgets import Button, Checkbox, Input, OptionList, RichLog, Select, Static, TabbedContent
from tui_history import TuiHistoryStore
from tui_models import (
    MODE_AUTO,
    MODE_FULL_PRODUCTION,
    MODE_FULL_RENDER,
    MODE_ORIGINAL_PREVIEW,
    MODE_ORIGINAL_PRODUCTION,
    MODE_QUICK_PREVIEW_ORIGINAL,
    MODE_QUICK_PREVIEW_TRANSLATED,
    MODE_RENDER_ONLY,
    MODE_REUSE_RENDER,
    MODE_STEP_API_AND_REVIEW,
    MODE_STEP_EXPORT_MANUAL,
    MODE_STEP_RESUME_RENDER,
    MODE_TRANSLATE_ONLY,
    MODE_TRANSLATED_PREVIEW,
    TuiJobDraft,
)
from tui_run import (
    _ENCODER_OPTIONS,
    _LAYOUT_PRESET_OPTIONS,
    _RENDER_PRESET_OPTIONS,
    _TASK_MODE_OPTIONS,
    OverlayTui,
)


def test_tui_tab_switching_stress():
    """Stress test switching across all 6 tabs in multiple orders without crashing."""
    async def exercise():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            tabs = ["download", "new-task", "task", "jobs", "advanced", "history"]
            tabbed_content = app.query_one(TabbedContent)

            # Forward transition
            for tab_id in tabs:
                tabbed_content.active = tab_id
                await pilot.pause(0.02)
                assert tabbed_content.active == tab_id

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
            app._set_input("#video", str(video))
            app._set_input("#chat", str(chat))
            app._set_select("#task-mode", MODE_QUICK_PREVIEW_ORIGINAL)
            await pilot.pause(0.05)

            validation = app.query_one("#form-validation", Static)

            # Valid positive offset
            app._set_input("#offset", "12.5")
            await pilot.pause(0.02)
            assert app._draft().offset == "12.5"
            assert "待处理" not in str(validation.render())
            cmd = app._draft().build_command("python", "render_cn_chat.py")
            assert "--offset" in cmd and "12.5" in cmd

            # Valid negative offset
            app._set_input("#offset", "-8.25")
            await pilot.pause(0.02)
            assert app._draft().offset == "-8.25"
            assert "待处理" not in str(validation.render())
            cmd = app._draft().build_command("python", "render_cn_chat.py")
            assert "--offset" in cmd and "-8.25" in cmd

            # Valid zero and empty offset
            app._set_input("#offset", "0.0")
            await pilot.pause(0.02)
            assert app._draft().offset == "0.0"
            assert "待处理" not in str(validation.render())

            app._set_input("#offset", "")
            await pilot.pause(0.02)
            assert app._draft().offset == ""
            assert "待处理" not in str(validation.render())

            # Boundary extremes
            app._set_input("#offset", "604800.0")
            await pilot.pause(0.02)
            assert app._draft().offset == "604800.0"
            assert "待处理" not in str(validation.render())

            app._set_input("#offset", "-604800.0")
            await pilot.pause(0.02)
            assert app._draft().offset == "-604800.0"
            assert "待处理" not in str(validation.render())

            # Out-of-bounds offset
            app._set_input("#offset", "604801.0")
            await pilot.pause(0.02)
            assert "待处理" in str(validation.render())
            assert "时间偏移" in str(validation.render())

            app._set_input("#offset", "-604801.0")
            await pilot.pause(0.02)
            assert "待处理" in str(validation.render())

            # Malformed non-numeric values
            for bad_val in ["abc", "12.3.4", "--5", "NaN", "Infinity", "1e99999"]:
                app._set_input("#offset", bad_val)
                await pilot.pause(0.02)
                assert "待处理" in str(validation.render()), f"Expected validation failure for offset: {bad_val}"

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


def test_unhandled_yaml_parser_error_on_corrupt_job(tmp_path: Path):
    """Verifies that _load_job() catches malformed YAML syntax errors gracefully and updates status."""
    bad_syntax_file = tmp_path / "syntax_error.yaml"
    bad_syntax_file.write_text(":\n  - [\ninvalid: yaml: :", encoding="utf-8")

    async def exercise():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            app._set_input("#job-path", str(bad_syntax_file))
            app._load_job()
            await pilot.pause(0.02)
            assert "无法导入 YAML" in str(app.query_one("#status").render())

    asyncio.run(exercise())


def test_tui_api_config_save_and_probe_async(tmp_path: Path):
    """Stress test API configuration saving, feedback updates, and non-blocking live probe."""
    async def exercise():
        app = OverlayTui()
        async with app.run_test(size=(140, 50)) as pilot:
            app.query_one(TabbedContent).active = "advanced"
            await pilot.pause(0.02)

            # 1. Test Saving API Config (Success)
            with patch("tui_run.save_dotenv_api_config", return_value=(True, "OK")) as mock_save:
                app._set_input("#api-base-url", "https://api.openai.com/v1")
                app._set_input("#api-key", "sk-secret-key-1234")
                app._set_input("#api-model", "deepseek-chat")

                app._save_api_config()
                await pilot.pause(0.02)

                mock_save.assert_called_once_with(
                    "https://api.openai.com/v1", "sk-secret-key-1234", "deepseek-chat"
                )
                feedback = str(app.query_one("#api-status-feedback", Static).render())
                assert "配置已成功保存至本地 .env 文件" in feedback
                assert "deepseek-chat" in feedback

            # 2. Test Saving API Config (Error)
            with patch("tui_run.save_dotenv_api_config", return_value=(False, "权限不足")):
                app._save_api_config()
                await pilot.pause(0.02)
                feedback = str(app.query_one("#api-status-feedback", Static).render())
                assert "保存 .env 失败：权限不足" in feedback

            # 3. Test Probe Success
            with patch("tui_run.probe_translate_api", return_value=(True, "连通成功")):
                app._run_api_probe("https://api.openai.com/v1", "sk-secret", "gpt-4o-mini")
                await pilot.pause(0.1)
                feedback = str(app.query_one("#api-status-feedback", Static).render())
                assert "连通性测试成功" in feedback
                assert "gpt-4o-mini" in feedback

            # 4. Test Probe Failure
            with patch("tui_run.probe_translate_api", return_value=(False, "HTTP 401 Unauthorized")):
                app._run_api_probe("https://api.openai.com/v1", "sk-invalid", "gpt-4o-mini")
                await pilot.pause(0.1)
                feedback = str(app.query_one("#api-status-feedback", Static).render())
                assert "连通性测试失败" in feedback
                assert "401 Unauthorized" in feedback

            # 5. Non-blocking Async Probe Stress: UI remains responsive during long probe
            def slow_probe(*args, **kwargs):
                time.sleep(0.25)
                return True, "Slow OK"

            with patch("tui_run.probe_translate_api", side_effect=slow_probe):
                app._test_api_connectivity()
                # Immediately interact with other tabs while probe runs in thread
                for tab_id in ["download", "new-task", "task", "history", "advanced"]:
                    app.query_one(TabbedContent).active = tab_id
                    await pilot.pause(0.03)

                # Wait for background thread worker to complete
                for _ in range(20):
                    await pilot.pause(0.05)
                    feedback = str(app.query_one("#api-status-feedback", Static).render())
                    if "连通性测试成功" in feedback:
                        break
                assert "连通性测试成功" in feedback

    asyncio.run(exercise())


def test_tui_task_dispatch_and_manual_required_lifecycle(tmp_path: Path):
    """Stress test Task dispatch, manual_required state guidance, failure diagnostics, and cancel."""
    history_file = tmp_path / "history.json"
    app = OverlayTui()
    app.history = TuiHistoryStore(history_file)

    async def exercise():
        async with app.run_test(size=(140, 50)) as pilot:
            # 1. manual_required lifecycle
            manual_script = (
                "import json, os; "
                "p=os.environ['TWITCH_OVERLAY_RESULT_FILE']; "
                "open(p, 'w', encoding='utf-8').write(json.dumps({"
                "'schema_version':1,'state':'manual_required','mode':'full','returncode':0,'finished_at':1,'artifacts':[]"
                "}))"
            )
            app._start_command("translate", [sys.executable, "-c", manual_script])
            for _ in range(40):
                await pilot.pause(0.05)
                if app.session and not app.session.running and app._handled_session is app.session:
                    break

            assert app.history.list_records()[0]["state"] == "manual_required"
            status_text = str(app.query_one("#status").render())
            assert "人工复核已就绪" in status_text or "载入复核表并压制" in status_text

            # 2. Failed task diagnostics lifecycle
            fail_script = "import sys; print('Fatal error occurred'); raise SystemExit(42)"
            app._start_command("failing_task", [sys.executable, "-c", fail_script])
            for _ in range(40):
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
            for _ in range(40):
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
