"""Result-driven TUI history and post-download routing coverage."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ("1", ("--mode", "preview", "--render-original")),
        ("2", ("--manual-translation", "--yes")),
        ("3", ("--mode", "full", "--yes")),
    ],
)
def test_post_download_routes_interactive_choice(monkeypatch, tmp_path: Path, choice: str, expected: tuple[str, ...]):
    import render_cn_chat as pipeline

    video = tmp_path / "video.mp4"
    chat = tmp_path / "chat.html"
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(pipeline, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_args: choice)
    monkeypatch.setattr(pipeline, "_run_pipeline_with_media", lambda *args: calls.append(args) or 7)

    assert pipeline._post_download_next_steps(video, chat, download_only=False, yes=False) == 7
    assert calls and calls[0][0:2] == (video, chat)
    assert tuple(calls[0][2:]) == expected or tuple(calls[0][2:]) == (*expected, "--preview-clip", "10", "--yes")


def test_post_download_noninteractive_stops_without_starting_pipeline(monkeypatch, tmp_path: Path):
    import render_cn_chat as pipeline

    monkeypatch.setattr(pipeline, "_run_pipeline_with_media", lambda *_args: pytest.fail("must not re-enter"))
    assert pipeline._post_download_next_steps(tmp_path / "video.mp4", tmp_path / "chat.html", download_only=True, yes=False) == 0


def test_textual_history_uses_manifest_for_artifacts_rerun_and_diagnostics(tmp_path: Path, monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import Input

    from tui_history import TuiHistoryStore
    from tui_models import MODE_ORIGINAL_PREVIEW, TuiJobDraft
    from tui_run import OverlayTui

    video = tmp_path / "video.mp4"
    chat = tmp_path / "chat.html"
    artifact = tmp_path / "rendered.mp4"
    diagnostic = tmp_path / "diagnostic.txt"
    for path in (video, chat, artifact):
        path.write_bytes(b"x")
    diagnostic.write_text("$ python private.py\nOPENAI_COMPAT_API_KEY missing\n", encoding="utf-8")
    manifest = tmp_path / "result.json"
    manifest.write_text(
        json.dumps({"schema_version": 1, "state": "succeeded", "mode": "preview", "returncode": 0, "finished_at": 1, "artifacts": [{"kind": "video", "path": str(artifact)}]}),
        encoding="utf-8",
    )
    draft = TuiJobDraft(video=str(video), chat_html=str(chat), mode=MODE_ORIGINAL_PREVIEW)

    async def exercise() -> None:
        app = OverlayTui()
        app.history = TuiHistoryStore(tmp_path / "history.json")
        record = app.history.start(draft, label="preview")
        app.history.finish(record["id"], state="succeeded", returncode=0, result_path=manifest)
        app.history.set_diagnostic(record["id"], diagnostic)
        opened: list[Path] = []
        rerun: list[str] = []
        monkeypatch.setattr(app, "_open_result_dir", lambda: opened.append(app.result_directory))
        monkeypatch.setattr(app, "_start_draft", lambda mode: rerun.append(mode))
        async with app.run_test():
            app.query_one("#history-id", Input).value = record["id"]
            app._open_history_artifacts()
            app._export_history_diagnostic()
            app._rerun_history()
        assert opened == [tmp_path.resolve(), tmp_path.resolve()]
        assert rerun == [MODE_ORIGINAL_PREVIEW]

    import asyncio

    asyncio.run(exercise())
    text = diagnostic.read_text(encoding="utf-8")
    assert "$ python" not in text
    assert "OPENAI_COMPAT_API_KEY" not in text


def test_textual_history_rerun_starts_from_a_new_advanced_job_snapshot(tmp_path: Path, monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import Input

    from tui_history import TuiHistoryStore
    from tui_models import MODE_ORIGINAL_PREVIEW, TuiJobDraft
    import tui_run
    from tui_run import OverlayTui

    video = tmp_path / "video.mp4"
    chat = tmp_path / "chat.html"
    video.write_bytes(b"video")
    chat.write_text("<html></html>", encoding="utf-8")
    draft = TuiJobDraft(
        video=str(video),
        chat_html=str(chat),
        mode=MODE_ORIGINAL_PREVIEW,
        extra_fields={"offset": 12.5, "max_visible": 7},
    )
    commands: list[list[str]] = []

    class FakeSession:
        def __init__(self, command: list[str], **_kwargs):
            commands.append(command)
            self.process = SimpleNamespace(pid=123)
            self.running = False
            self.returncode = None
            self.dropped_output = 0

        def start(self) -> None:
            pass

        def poll(self):
            return [], []

        def close(self) -> None:
            pass

    monkeypatch.setattr(tui_run, "TaskSession", FakeSession)
    monkeypatch.setattr(TuiJobDraft, "validate", lambda *_args, **_kwargs: [])

    async def exercise() -> None:
        app = OverlayTui()
        app.history = TuiHistoryStore(tmp_path / "history.json")
        original = app.history.start(draft, label="advanced")
        async with app.run_test():
            app.query_one("#history-id", Input).value = original["id"]
            app._rerun_history()
            assert commands
            command = commands[0]
            snapshot = Path(command[command.index("--job") + 1])
            assert snapshot.is_file() and snapshot != Path(original["job_path"])
            text = snapshot.read_text(encoding="utf-8")
            assert "offset: 12.5" in text and "max_visible: 7" in text

    import asyncio

    asyncio.run(exercise())


def test_textual_download_result_populates_new_task_fields(tmp_path: Path):
    pytest.importorskip("textual")
    from textual.widgets import Input, TabbedContent

    from tui_run import OverlayTui

    video = tmp_path / "downloaded.mp4"
    chat = tmp_path / "downloaded.html"
    video.write_bytes(b"x")
    chat.write_text("<html></html>", encoding="utf-8")

    async def exercise() -> None:
        app = OverlayTui()
        app.session = SimpleNamespace(
            running=False,
            close=lambda: None,
            poll=lambda: ([], []),
            dropped_output=0,
            returncode=None,
            result={"artifacts": [{"kind": "video", "path": str(video)}, {"kind": "chat_html", "path": str(chat)}]},
        )
        async with app.run_test():
            assert app._apply_download_result() is True
            assert app.query_one("#video", Input).value == str(video)
            assert app.query_one("#chat", Input).value == str(chat)
            assert app.query_one(TabbedContent).active == "new-task"

    import asyncio

    asyncio.run(exercise())


def test_textual_download_without_result_manifest_is_not_reported_as_success(tmp_path: Path):
    pytest.importorskip("textual")

    from tui_history import TuiHistoryStore
    from tui_models import TuiDownloadDraft
    from tui_run import OverlayTui

    async def exercise() -> None:
        app = OverlayTui()
        app.history = TuiHistoryStore(tmp_path / "history.json")
        async with app.run_test():
            record = app.history.start(
                TuiDownloadDraft(source="2819850140", segments_text="0:00:00-0:00:08"),
                label="download",
            )
            app.active_history_id = record["id"]
            app.current_task_kind = "download"
            app.require_result_manifest = True
            app.completion_message = "download reported complete"
            app.session = SimpleNamespace(
                running=False,
                close=lambda: None,
                poll=lambda: ([], []),
                drain_after_exit=lambda: ([], []),
                retain_result=lambda _path: None,
                export_diagnostics=lambda path: Path(path),
                cleanup=lambda **_kwargs: None,
                dropped_output=0,
                returncode=0,
                cancelled=False,
                result=None,
            )
            app._poll_session()
            assert app.history.get(record["id"])["state"] == "failed"
            assert "download reported complete" not in str(app.query_one("#status").render())

    import asyncio

    asyncio.run(exercise())


def test_textual_oauth_download_history_rerun_waits_for_new_credential(tmp_path: Path, monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import Input, TabbedContent

    from tui_history import TuiHistoryStore
    from tui_models import TuiDownloadDraft
    from tui_run import OverlayTui

    async def exercise() -> None:
        app = OverlayTui()
        app.history = TuiHistoryStore(tmp_path / "history.json")
        record = app.history.start(
            TuiDownloadDraft(
                source="2819850140",
                segments_text="1:00:00-1:00:08",
                oauth="private-token",
            ),
            label="protected download",
        )
        monkeypatch.setattr(app, "_start_download", lambda _draft=None: pytest.fail("must wait for OAuth"))
        async with app.run_test():
            app.query_one("#history-id", Input).value = record["id"]
            app._rerun_history()
            assert app.query_one(TabbedContent).active == "download"
            assert app.query_one("#download-oauth", Input).value == ""
            assert "OAuth" in str(app.query_one("#status").render())

    import asyncio

    asyncio.run(exercise())
