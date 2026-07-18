from __future__ import annotations

import os
from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tui_models import (
    MODE_FULL_RENDER,
    MODE_ORIGINAL_PREVIEW,
    MODE_REUSE_RENDER,
    MODE_TRANSLATED_PREVIEW,
    TuiDownloadDraft,
    TuiJobDraft,
)
from tui_task import TaskSession, format_event, redact_command, redact_text, sanitize_diagnostic_file


def test_draft_maps_to_existing_pipeline_flags(tmp_path: Path, monkeypatch):
    video = tmp_path / "source.mp4"
    chat = tmp_path / "chat.html"
    translation = tmp_path / "translation.json"
    for path in (video, chat, translation):
        path.write_text("x", encoding="utf-8")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "test")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "test-model")
    draft = TuiJobDraft(
        video=str(video),
        chat_html=str(chat),
        translation_json=str(translation),
        output=str(tmp_path / "out.mp4"),
        mode=MODE_FULL_RENDER,
        layout_preset="compact",
        render_preset="fast",
        workers="3",
    )

    assert draft.validate(check_environment=False) == []
    command = draft.build_command("python", "render_cn_chat.py")
    assert command[:5] == ["python", "render_cn_chat.py", str(video), str(chat), "--yes"]
    assert "--mode" in command and "full" in command
    assert "--layout-preset" in command and "compact" in command
    assert "--workers" in command and "3" in command
    assert "--source-media-check" in command and "decode" in command

    fields = draft.to_job_fields()
    assert fields["mode"] == "full"
    assert fields["video"] == str(video)


def test_reuse_mode_requires_existing_translation(tmp_path: Path):
    video = tmp_path / "source.mp4"
    chat = tmp_path / "chat.html"
    video.write_text("x", encoding="utf-8")
    chat.write_text("x", encoding="utf-8")
    draft = TuiJobDraft(video=str(video), chat_html=str(chat), mode=MODE_REUSE_RENDER)

    assert "复用翻译渲染需要选择已存在的翻译 JSON。" in draft.validate(check_api=False)


def test_save_and_load_pinned_yaml_round_trip(tmp_path: Path):
    video = tmp_path / "source.mp4"
    chat = tmp_path / "chat.html"
    video.write_text("x", encoding="utf-8")
    chat.write_text("x", encoding="utf-8")
    draft = TuiJobDraft(video=str(video), chat_html=str(chat), mode=MODE_ORIGINAL_PREVIEW)
    path = draft.save_job(tmp_path / "saved.yaml")

    loaded = TuiJobDraft.from_job_file(path)
    assert loaded.video == str(video)
    assert loaded.chat_html == str(chat)
    assert loaded.mode == MODE_ORIGINAL_PREVIEW


def test_imported_yaml_preserves_unexposed_advanced_fields(tmp_path: Path):
    video = tmp_path / "source.mp4"
    chat = tmp_path / "chat.html"
    video.write_text("x", encoding="utf-8")
    chat.write_text("x", encoding="utf-8")
    original = tmp_path / "advanced.yaml"
    original.write_text(
        f"video: {video}\nchat_html: {chat}\nmode: preview\nrender_original: true\nbg_alpha: 170\n",
        encoding="utf-8",
    )

    loaded = TuiJobDraft.from_job_file(original)
    saved = loaded.save_job(tmp_path / "copy.yaml")
    command = loaded.build_command("python", "render_cn_chat.py")

    assert "bg_alpha: 170" in saved.read_text(encoding="utf-8")
    assert command[:5] == ["python", "render_cn_chat.py", "--job", str(original), str(video)]
    assert str(chat) in command


def test_imported_yaml_oauth_is_not_serialized_into_tui_fields_or_command(tmp_path: Path):
    video = tmp_path / "source.mp4"
    chat = tmp_path / "chat.html"
    video.write_text("x", encoding="utf-8")
    chat.write_text("x", encoding="utf-8")
    original = tmp_path / "legacy.yaml"
    original.write_text(
        f"video: {video}\nchat_html: {chat}\nmode: preview\nrender_original: true\noauth: secret-token\n",
        encoding="utf-8",
    )

    loaded = TuiJobDraft.from_job_file(original)

    assert "oauth" not in loaded.to_job_fields()
    assert "secret-token" not in loaded.build_command("python", "render_cn_chat.py")


def test_event_format_and_redaction_are_safe():
    assert format_event({"event": "stage_progress", "stage": "translate", "completed": 2, "total": 5}) == "stage progress: translate (2/5)"
    for value in (
        "API_KEY=secret-value",
        "Authorization: Bearer secret-value",
        '"api_key": "secret-value"',
        "oauth=secret-value",
        '"oauth": "secret-value"',
        "Authorization: OAuth secret-value",
        "--oauth secret-value",
        "--oauth=secret-value",
        "OPENAI_COMPAT_API_KEY is missing",
        "AGNES_BASE_URL is missing",
    ):
        redacted = redact_text(value)
        assert "secret-value" not in redacted
        if "secret-value" in value:
            assert "[redacted]" in redacted
        assert "OPENAI_COMPAT_API_KEY" not in redacted
        assert "AGNES_BASE_URL" not in redacted


def test_manual_translation_and_legacy_api_do_not_fail_api_preflight(tmp_path: Path, monkeypatch):
    video = tmp_path / "source.mp4"
    chat = tmp_path / "chat.html"
    video.write_text("x", encoding="utf-8")
    chat.write_text("x", encoding="utf-8")
    monkeypatch.delenv("OPENAI_COMPAT_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_COMPAT_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_COMPAT_MODEL", raising=False)
    monkeypatch.delenv("AGNES_BASE_URL", raising=False)
    monkeypatch.delenv("AGNES_API_KEY", raising=False)
    monkeypatch.delenv("AGNES_MODEL", raising=False)
    manual = TuiJobDraft(video=str(video), chat_html=str(chat), mode=MODE_FULL_RENDER, manual_translation=True)
    assert not any("翻译服务" in problem for problem in manual.validate(check_environment=False))

    monkeypatch.setenv("AGNES_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("AGNES_API_KEY", "test")
    monkeypatch.setenv("AGNES_MODEL", "model")
    legacy = TuiJobDraft(video=str(video), chat_html=str(chat), mode=MODE_FULL_RENDER)
    assert not any("翻译服务" in problem for problem in legacy.validate(check_environment=False))


def test_malformed_preview_clip_yaml_is_a_clear_validation_error():
    with pytest.raises(ValueError, match="preview_clip"):
        TuiJobDraft.from_fields({"preview_clip": []})


def test_download_draft_requires_bounded_segments_and_builds_multi_segment_command(tmp_path: Path):
    draft = TuiDownloadDraft(
        source="2819850140",
        download_dir=str(tmp_path / "download"),
        quality="1080p60",
        segments_text="1:00:00-1:00:08; 1:00:20-1:00:28",
    )
    assert draft.validate() == []
    command = draft.build_command("python", "render_cn_chat.py")
    assert command[:5] == ["python", "render_cn_chat.py", "--download", "2819850140", "--download-only"]
    assert command.count("--segment") == 2
    assert "1:00:20-1:00:28" in command
    assert "--media-check" in command and "decode" in command
    assert TuiDownloadDraft(source="2819850140").validate()
    assert TuiDownloadDraft(source="https://clips.twitch.tv/ExampleClip").validate() == []
    protected = TuiDownloadDraft(source="2819850140", segments_text="1:00:00-1:00:08", oauth="secret-token")
    assert "secret-token" in protected.build_command("python", "render_cn_chat.py")
    assert "oauth" not in protected.to_history_fields()
    query_token = TuiDownloadDraft(
        source="https://www.twitch.tv/videos/2819850140?oauth=secret-token#fragment",
        segments_text="1:00:00-1:00:08",
    )
    assert query_token.to_history_fields()["download"] == "2819850140"


def test_task_session_drains_output_and_events(tmp_path: Path):
    child = (
        "import json, os; "
        "p=os.environ['TWITCH_OVERLAY_EVENT_FILE']; "
        "open(p, 'a', encoding='utf-8').write(json.dumps({'event':'stage_started','stage':'render'})+'\\n'); "
        "print('API_KEY=not-for-ui')"
    )
    session = TaskSession([sys.executable, "-c", child], cwd=tmp_path)
    session.start()
    deadline = time.monotonic() + 10
    lines: list[str] = []
    events: list[str] = []
    while time.monotonic() < deadline and (session.running or not lines):
        got_lines, got_events = session.poll()
        lines.extend(got_lines)
        events.extend(got_events)
        time.sleep(0.03)

    assert session.returncode == 0
    assert any("[redacted]" in line for line in lines)
    assert events == ["stage started: render"]
    session.cleanup()
    assert session.event_path is not None and not session.event_path.exists()


def test_task_session_keeps_a_partial_event_until_the_jsonl_line_is_complete(tmp_path: Path):
    session = TaskSession([sys.executable, "-c", "pass"], cwd=tmp_path)
    session.event_path = tmp_path / "events.jsonl"
    session.event_path.write_text('{"event":"stage_started"', encoding="utf-8")

    assert session.poll()[1] == []
    with session.event_path.open("a", encoding="utf-8") as handle:
        handle.write(',"stage":"download"}\n')

    assert session.poll()[1] == ["stage started: download"]


def test_command_redaction_hides_oauth_before_ui_logging():
    assert redact_command(["python", "render.py", "--oauth", "secret-token", "--download", "123"]) == [
        "python", "render.py", "--oauth", "[redacted]", "--download", "123"
    ]
    assert redact_command(["python", "render.py", "--oauth=secret-token"]) == [
        "python", "render.py", "--oauth=[redacted]"
    ]


def test_task_session_redacts_oauth_from_child_output(tmp_path: Path):
    child = "print('--oauth secret-token'); print('Authorization: OAuth secret-token')"
    session = TaskSession([sys.executable, "-c", child], cwd=tmp_path)
    session.start()
    deadline = time.monotonic() + 10
    lines: list[str] = []
    while time.monotonic() < deadline and (session.running or not lines):
        got_lines, _ = session.poll()
        lines.extend(got_lines)
        time.sleep(0.03)

    assert session.returncode == 0
    assert "secret-token" not in "\n".join(lines)
    assert "[redacted]" in "\n".join(lines)
    session.cleanup()


def test_task_session_drains_final_child_output_after_exit(tmp_path: Path):
    session = TaskSession([sys.executable, "-c", "print('late terminal detail'); raise SystemExit(2)"], cwd=tmp_path)
    session.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and session.running:
        time.sleep(0.01)

    lines, _ = session.drain_after_exit()

    assert session.returncode == 2
    assert "late terminal detail" in lines
    session.cleanup(keep_failure=False)


def test_diagnostic_exports_omit_commands_and_environment_variable_names(tmp_path: Path):
    session = TaskSession([sys.executable, "-c", "pass"], cwd=tmp_path)
    session._log_lines.extend((
        "$ python render.py --oauth [redacted] --output private.mp4",
        "missing OPENAI_COMPAT_API_KEY",
        "Authorization: Bearer secret-token",
    ))
    diagnostic = session.export_diagnostics(tmp_path / "diagnostic.txt")
    text = diagnostic.read_text(encoding="utf-8")

    assert "$ python" not in text
    assert "private.mp4" not in text
    assert "OPENAI_COMPAT_API_KEY" not in text
    assert "secret-token" not in text
    assert "[command omitted for privacy]" in text
    assert "[environment variable]" in text

    legacy = tmp_path / "legacy.txt"
    legacy.write_text("$ python render.py --output private.mp4\nAGNES_API_KEY missing\n", encoding="utf-8")
    sanitize_diagnostic_file(legacy)
    migrated = legacy.read_text(encoding="utf-8")
    assert "$ python" not in migrated
    assert "private.mp4" not in migrated
    assert "AGNES_API_KEY" not in migrated


def test_task_session_start_failure_cleans_transient_files(tmp_path: Path):
    session = TaskSession([str(tmp_path / "does-not-exist.exe")], cwd=tmp_path)
    with pytest.raises(OSError):
        session.start()
    assert session.event_path is not None and not session.event_path.exists()
    assert session.result_path is not None and not session.result_path.exists()


@pytest.mark.smoke
def test_offline_demo_runs_through_task_session(tmp_path: Path):
    session = TaskSession(
        [sys.executable, str(SCRIPTS / "quick_demo.py"), "--output-dir", str(tmp_path / "demo")],
        cwd=ROOT,
    )
    session.start()
    deadline = time.monotonic() + 90
    logs: list[str] = []
    events: list[str] = []
    while time.monotonic() < deadline and session.running:
        got_logs, got_events = session.poll()
        logs.extend(got_logs)
        events.extend(got_events)
        time.sleep(0.05)
    got_logs, got_events = session.poll()
    logs.extend(got_logs)
    events.extend(got_events)

    assert session.returncode == 0
    assert (tmp_path / "demo" / "demo_overlay.mp4").is_file()
    assert any("stage completed: render" in line for line in events)
    assert all("OPENAI_COMPAT_API_KEY" not in line for line in logs)
    session.cleanup()


def test_textual_invalid_form_stays_open():
    pytest.importorskip("textual")
    from tui_run import OverlayTui

    async def exercise() -> None:
        app = OverlayTui()
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.click("#original-preview")
            assert "无法开始" in str(app.query_one("#status").render())

    import asyncio

    asyncio.run(exercise())


def test_textual_download_button_builds_existing_cli_command(monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import TabbedContent

    from tui_run import OverlayTui

    captured: dict[str, object] = {}

    async def exercise() -> None:
        app = OverlayTui()
        monkeypatch.setattr(app, "_start_command", lambda *args, **kwargs: captured.update(args=args, kwargs=kwargs))
        async with app.run_test(size=(140, 45)) as pilot:
            app.query_one(TabbedContent).active = "download"
            await pilot.pause(0.05)
            app.query_one("#download-url").value = "2819850140"
            app.query_one("#download-segments").value = "1:00:00-1:00:08; 1:00:20-1:00:28"
            app.query_one("#download-oauth").value = "secret-token"
            await pilot.click("#download-start")
        command = captured["args"][1]
        assert "--download-only" in command and command.count("--segment") == 2
        assert "secret-token" in command
        assert captured["kwargs"]["task_kind"] == "download"

    import asyncio

    asyncio.run(exercise())


def test_textual_import_then_save_preserves_mode_and_extra_fields(tmp_path: Path):
    pytest.importorskip("textual")
    from tui_run import OverlayTui

    video = tmp_path / "source.mp4"
    chat = tmp_path / "chat.html"
    video.write_text("x", encoding="utf-8")
    chat.write_text("x", encoding="utf-8")
    source = tmp_path / "source.yaml"
    target = tmp_path / "saved.yaml"
    source.write_text(
        f"video: {video}\nchat_html: {chat}\nmode: render\nreuse_translation: true\nbg_alpha: 170\n",
        encoding="utf-8",
    )

    async def exercise() -> None:
        app = OverlayTui()
        async with app.run_test() as pilot:
            app.query_one("#job-path").value = str(source)
            app._load_job()
            app.query_one("#job-path").value = str(target)
            app._save_job()
            await pilot.pause(0.05)

    import asyncio

    asyncio.run(exercise())
    saved = target.read_text(encoding="utf-8")
    assert "mode: render" in saved
    assert "reuse_translation: true" in saved
    assert "bg_alpha: 170" in saved


def test_tui_result_context_is_specific_to_task_mode(tmp_path: Path):
    from tui_run import OverlayTui

    video = tmp_path / "source.mp4"
    chat = tmp_path / "chat.html"
    video.write_text("x", encoding="utf-8")
    chat.write_text("x", encoding="utf-8")
    for mode, expected in (
        (MODE_ORIGINAL_PREVIEW, "预览任务完成"),
        (MODE_TRANSLATED_PREVIEW, "预览任务完成"),
        (MODE_REUSE_RENDER, "复用翻译渲染完成"),
        (MODE_FULL_RENDER, "正式翻译渲染完成"),
    ):
        directory, message = OverlayTui._result_context(TuiJobDraft(video=str(video), chat_html=str(chat), mode=mode))
        assert directory == tmp_path
        assert expected in message

    manual_directory, manual_message = OverlayTui._result_context(
        TuiJobDraft(video=str(video), chat_html=str(chat), mode=MODE_FULL_RENDER, manual_translation=True)
    )
    assert manual_directory == tmp_path
    assert "复核目录" in manual_message


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher behavior")
def test_textual_open_result_directory_uses_active_task_context(tmp_path: Path, monkeypatch):
    pytest.importorskip("textual")
    from tui_run import OverlayTui

    opened: list[str] = []
    monkeypatch.setattr(os, "startfile", lambda path: opened.append(str(path)))

    async def exercise() -> None:
        app = OverlayTui()
        async with app.run_test():
            app.result_directory = tmp_path
            app._open_result_dir()
            assert "已打开" in str(app.query_one("#status").render())

    import asyncio

    asyncio.run(exercise())
    assert opened == [str(tmp_path.resolve())]
