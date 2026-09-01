from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from task_results import RESULT_FILE_ENV, read_task_result, write_task_result
import tui_history as tui_history_module
from tui_history import TuiHistoryStore
from tui_models import MODE_ORIGINAL_PREVIEW, TuiDownloadDraft, TuiJobDraft
from tui_task import TaskSession


def test_result_manifest_is_opt_in_and_exposes_only_existing_artifacts(tmp_path: Path, monkeypatch):
    output = tmp_path / "result.json"
    artifact = tmp_path / "output.mp4"
    artifact.write_bytes(b"video")
    monkeypatch.delenv(RESULT_FILE_ENV, raising=False)
    assert write_task_result(state="succeeded", returncode=0, artifacts=[("video", artifact)]) is False

    monkeypatch.setenv(RESULT_FILE_ENV, str(output))
    assert write_task_result(
        state="succeeded",
        mode="full",
        returncode=0,
        artifacts=[("video", artifact), ("missing", tmp_path / "missing.mp4")],
    )
    manifest = read_task_result(output)
    assert manifest is not None
    assert manifest["state"] == "succeeded"
    assert manifest["artifacts"] == [{"kind": "video", "path": str(artifact.resolve())}]
    assert "command" not in manifest and "environment" not in manifest


def test_history_retains_bounded_records_and_recovers_running(tmp_path: Path):
    store = TuiHistoryStore(tmp_path / "history.json", limit=3)
    draft = TuiJobDraft(video="video.mp4", chat_html="chat.html", mode=MODE_ORIGINAL_PREVIEW)
    first = store.start(draft, label="one")
    store.mark_running(first["id"], pid=1, result_path=tmp_path / "one.result.json")
    second = store.start(draft, label="two")
    store.mark_running(second["id"], pid=2, result_path=None)
    store.finish(second["id"], state="succeeded", returncode=0, result_path=None)
    third = store.start(draft, label="three")
    store.mark_running(third["id"], pid=3, result_path=None)

    original_liveness = tui_history_module.pid_is_alive
    tui_history_module.pid_is_alive = lambda _pid: False
    try:
        recovered = store.recover_interrupted()
    finally:
        tui_history_module.pid_is_alive = original_liveness
    assert {record["id"] for record in recovered} == {first["id"], third["id"]}
    records = store.list_records()
    assert len(records) == 3
    assert records[0]["state"] == "interrupted"
    assert all("command" not in json.dumps(record) for record in records)
    limited = TuiHistoryStore(tmp_path / "limited.json", limit=2)
    for label in ("one", "two", "three"):
        limited.start(draft, label=label)
    assert len(limited.list_records()) == 2


def test_history_recovery_keeps_a_live_task_running(tmp_path: Path, monkeypatch):
    store = TuiHistoryStore(tmp_path / "history.json")
    running = store.start(None, label="live")
    store.mark_running(running["id"], pid=1234, result_path=None)
    queued = store.start(None, label="queued")
    monkeypatch.setattr(tui_history_module, "pid_is_alive", lambda pid: int(pid) == 1234)

    recovered = store.recover_interrupted()

    assert [record["id"] for record in recovered] == [queued["id"]]
    assert store.get(running["id"])["state"] == "running"


def test_history_strips_sensitive_draft_values(tmp_path: Path):
    store = TuiHistoryStore(tmp_path / "history.json")
    draft = TuiJobDraft(
        video="video.mp4",
        chat_html="chat.html",
        extra_fields={"api_key": "secret", "apiKey": "secret", "api-key": "secret", "oauth": "secret", "offset": 1},
    )
    record = store.start(draft, label="safe")
    assert all(key not in record["draft"] for key in ("api_key", "apiKey", "api-key", "oauth"))
    assert record["draft"]["offset"] == 1


def test_history_migrates_legacy_oauth_out_of_saved_draft(tmp_path: Path):
    path = tmp_path / "history.json"
    path.write_text(
        json.dumps({
            "schema_version": 1,
            "records": [{"id": "legacy", "state": "succeeded", "started_at": 1, "draft": {"oauth": "secret", "offset": 1}}],
        }),
        encoding="utf-8",
    )

    records = TuiHistoryStore(path).list_records()

    assert records[0]["draft"] == {"offset": 1}
    assert "secret" not in path.read_text(encoding="utf-8")


def test_history_migrates_download_url_query_credentials(tmp_path: Path):
    path = tmp_path / "history.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "records": [
                    {
                        "id": "legacy-url",
                        "state": "succeeded",
                        "started_at": 1,
                        "draft": {
                            "_tui_task_type": "download",
                            "download": "https://www.twitch.tv/videos/2819850140?oauth=secret-token",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    records = TuiHistoryStore(path).list_records()

    assert records[0]["draft"]["download"] == "2819850140"
    assert "secret-token" not in path.read_text(encoding="utf-8")


def test_history_migrates_legacy_download_oauth_to_authentication_marker(tmp_path: Path):
    path = tmp_path / "history.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "records": [
                    {
                        "id": "legacy-download",
                        "state": "failed",
                        "started_at": 1,
                        "draft": {
                            "_tui_task_type": "download",
                            "download": "2819850140",
                            "oauth": "secret-token",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = TuiHistoryStore(path)
    restored = store.download_for(store.list_records()[0])

    assert restored is not None and restored.authentication_required is True
    assert "secret-token" not in path.read_text(encoding="utf-8")


def test_history_round_trips_download_draft(tmp_path: Path):
    store = TuiHistoryStore(tmp_path / "history.json")
    draft = TuiDownloadDraft(source="2819850140", quality="720p60", segments_text="1:00:00-1:00:08", oauth="secret-token")
    record = store.start(draft, label="download")
    restored = store.download_for(record)
    assert restored is not None
    assert restored.source == "2819850140"
    assert restored.segments() == ["1:00:00-1:00:08"]
    assert restored.oauth == ""
    assert restored.authentication_required is True
    assert "secret-token" not in json.dumps(record)


def test_history_job_snapshot_preserves_advanced_fields_for_rerun(tmp_path: Path):
    store = TuiHistoryStore(tmp_path / "history.json")
    draft = TuiJobDraft(
        video="video.mp4",
        chat_html="chat.html",
        mode=MODE_ORIGINAL_PREVIEW,
        extra_fields={"offset": 12.5, "overlay_codec": "qtrle", "max_visible": 7},
    )

    record = store.start(draft, label="advanced")
    restored = store.draft_for(record)

    assert restored is not None
    assert restored.offset == "12.5"
    assert restored.extra_fields == {"overlay_codec": "qtrle", "max_visible": 7}
    assert "max_visible" in restored.extra_fields
    assert restored.source_job
    snapshot = Path(restored.source_job)
    assert snapshot.is_file()
    command = restored.build_command("python", "render_cn_chat.py")
    assert command[2:4] == ["--job", str(snapshot)]
    snapshot_text = snapshot.read_text(encoding="utf-8")
    assert "offset: 12.5" in snapshot_text
    assert "overlay_codec: qtrle" in snapshot_text
    assert "max_visible: 7" in snapshot_text


def test_history_removes_job_snapshot_when_history_write_fails(tmp_path: Path, monkeypatch):
    store = TuiHistoryStore(tmp_path / "history.json")
    draft = TuiJobDraft(video="video.mp4", chat_html="chat.html", mode=MODE_ORIGINAL_PREVIEW)
    monkeypatch.setattr(store, "_save", lambda _records: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        store.start(draft, label="write failure")

    assert not list((tmp_path / "jobs").glob("*.yaml"))


def test_history_write_waits_for_another_instance_lock(tmp_path: Path):
    path = tmp_path / "history.json"
    first = TuiHistoryStore(path)
    second = TuiHistoryStore(path)
    first.start(None, label="first")
    locked = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def hold_lock() -> None:
        with first._history_lock():
            locked.set()
            assert release.wait(timeout=5)

    def write_second() -> None:
        second.start(None, label="second")
        finished.set()

    holder = threading.Thread(target=hold_lock)
    writer = threading.Thread(target=write_second)
    holder.start()
    assert locked.wait(timeout=5)
    writer.start()
    assert not finished.wait(timeout=0.1)
    release.set()
    holder.join(timeout=5)
    writer.join(timeout=5)

    assert finished.is_set()
    assert {record["label"] for record in first.list_records()} == {"first", "second"}


def test_history_read_paths_return_safely_after_bounded_retry_window(tmp_path: Path, monkeypatch):
    """A wedged holder must not crash read entry points: reads degrade to []/None/False."""
    path = tmp_path / "history.json"
    first = TuiHistoryStore(path)
    second = TuiHistoryStore(path)
    record = first.start(None, label="first")
    locked = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with first._history_lock():
            locked.set()
            assert release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert locked.wait(timeout=5)
    monkeypatch.setattr(tui_history_module, "LOCK_RETRY_INTERVAL_S", 0.01)
    monkeypatch.setattr(tui_history_module, "LOCK_TIMEOUT_S", 0.2)
    try:
        # 读入口全部安全降级，绝不让另一个僵死实例打崩本实例的启动/点击。
        assert second.list_records() == []
        assert second.get(record["id"]) is None
        assert second.has_unfinished_records() is False
        assert second.recover_interrupted() == []
        second.set_diagnostic(record["id"], tmp_path / "diag.txt")  # 不抛即通过
    finally:
        release.set()
        holder.join(timeout=5)
    # 锁释放后读写恢复正常，且没有任何数据被破坏。
    assert [item["label"] for item in second.list_records()] == ["first"]
    assert second.get(record["id"]) is not None
    assert second.get(record["id"])["label"] == "first"


def test_history_write_still_raises_after_bounded_retry_window(tmp_path: Path, monkeypatch):
    """对照用例：写路径（start/clear）在锁超时时必须继续抛 OSError。"""
    path = tmp_path / "history.json"
    first = TuiHistoryStore(path)
    second = TuiHistoryStore(path)
    first.start(None, label="first")
    locked = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with first._history_lock():
            locked.set()
            assert release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert locked.wait(timeout=5)
    monkeypatch.setattr(tui_history_module, "LOCK_RETRY_INTERVAL_S", 0.01)
    monkeypatch.setattr(tui_history_module, "LOCK_TIMEOUT_S", 0.2)
    try:
        with pytest.raises(OSError, match="任务历史"):
            second.start(None, label="blocked")
        with pytest.raises(OSError, match="任务历史"):
            second.clear()
    finally:
        release.set()
        holder.join(timeout=5)
    # 写路径失败没有留下半成品数据。
    assert [record["label"] for record in second.list_records()] == ["first"]


def test_history_skips_malformed_records_and_handles_bad_draft(tmp_path: Path):
    path = tmp_path / "history.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "records": [
                    {"id": "bad-time", "state": "succeeded", "started_at": "never"},
                    {"id": "not-a-number", "state": "succeeded", "started_at": float("nan")},
                    {"id": "bad-state", "state": "unknown", "started_at": 1},
                    {"id": "bad-draft", "state": "failed", "started_at": 2, "draft": {"preview_clip": [1]}},
                ],
            }
        ),
        encoding="utf-8",
    )
    store = TuiHistoryStore(path)
    records = store.list_records()
    assert [record["id"] for record in records] == ["bad-draft"]
    assert store.draft_for(records[0]) is None


def test_history_clear_removes_managed_artifacts(tmp_path: Path):
    store = TuiHistoryStore(tmp_path / "history.json")
    manifest = store.manifest_path("task")
    diagnostic = store.path.parent / "diagnostics" / "task.txt"
    job = store.path.parent / "jobs" / "task.yaml"
    manifest.parent.mkdir(parents=True)
    diagnostic.parent.mkdir(parents=True)
    job.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    diagnostic.write_text("diagnostic", encoding="utf-8")
    job.write_text("mode: preview", encoding="utf-8")
    assert store.clear() is True
    assert not manifest.exists() and not diagnostic.exists() and not job.exists()


@pytest.mark.parametrize("state", ["queued", "running"])
def test_history_clear_refuses_to_delete_another_active_task(tmp_path: Path, state: str):
    first = TuiHistoryStore(tmp_path / "history.json")
    second = TuiHistoryStore(tmp_path / "history.json")
    record = first.start(None, label="active")
    if state == "running":
        first.mark_running(record["id"], pid=None, result_path=None)

    assert second.has_unfinished_records() is True
    assert second.clear() is False
    assert first.get(record["id"]) is not None

    first.finish(record["id"], state="succeeded", returncode=0, result_path=None)
    assert second.has_unfinished_records() is False
    assert second.clear() is True


def test_textual_history_write_failure_does_not_start_task(tmp_path: Path, monkeypatch):
    pytest.importorskip("textual")
    from tui_run import OverlayTui

    async def exercise() -> None:
        app = OverlayTui()
        app.history = TuiHistoryStore(tmp_path / "history.json")
        monkeypatch.setattr(app.history, "start", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
        async with app.run_test():
            app._start_command("probe", [sys.executable, "-c", "print('must not run')"])
            assert app.session is None
            assert "任务未启动" in str(app.query_one("#status").render())

    import asyncio

    asyncio.run(exercise())


def test_history_limit_prunes_expired_managed_artifacts(tmp_path: Path):
    store = TuiHistoryStore(tmp_path / "history.json", limit=1)
    first = store.start(None, label="first")
    manifest = store.manifest_path(first["id"])
    diagnostic = store.path.parent / "diagnostics" / f"{first['id']}.txt"
    manifest.parent.mkdir(parents=True)
    diagnostic.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    diagnostic.write_text("diagnostic", encoding="utf-8")
    store.start(None, label="second")
    assert not manifest.exists() and not diagnostic.exists()


def test_task_session_reads_result_manifest(tmp_path: Path):
    child = (
        "import json, os; "
        "p=os.environ['TWITCH_OVERLAY_RESULT_FILE']; "
        "open(p, 'w', encoding='utf-8').write(json.dumps({'schema_version':1,'state':'succeeded','mode':'full','returncode':0,'finished_at':1,'artifacts':[]}))"
    )
    session = TaskSession([sys.executable, "-c", child], cwd=tmp_path)
    session.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and session.running:
        session.poll()
        time.sleep(0.02)
    session.poll()
    assert session.result is not None
    assert session.result["mode"] == "full"
    session.cleanup()


def test_pipeline_wrapper_writes_terminal_result(tmp_path: Path, monkeypatch):
    import render_cn_chat as pipeline

    artifact = tmp_path / "out.mp4"
    artifact.write_bytes(b"video")
    manifest_path = tmp_path / "result.json"
    monkeypatch.setenv(RESULT_FILE_ENV, str(manifest_path))

    def fake_main():
        runner = pipeline.active_runner()
        assert runner is not None
        runner.configure(mode="full", artifacts=[("video", artifact)])
        return 0

    monkeypatch.setattr(pipeline, "_main", fake_main)
    assert pipeline.main() == 0
    result = read_task_result(manifest_path)
    assert result is not None
    assert result["state"] == "succeeded"
    assert result["artifacts"][0]["path"] == str(artifact.resolve())


def test_pipeline_wrapper_preserves_manual_required_terminal_state(tmp_path: Path, monkeypatch):
    import render_cn_chat as pipeline

    manifest_path = tmp_path / "result.json"
    monkeypatch.setenv(RESULT_FILE_ENV, str(manifest_path))

    def fake_main():
        runner = pipeline.active_runner()
        assert runner is not None
        runner.configure(mode="full", artifacts=[])
        runner.mark_manual_required()
        return 0

    monkeypatch.setattr(pipeline, "_main", fake_main)
    assert pipeline.main() == 0
    result = read_task_result(manifest_path)
    assert result is not None and result["state"] == "manual_required"


def test_download_flow_publishes_video_and_chat_result_manifest(tmp_path: Path, monkeypatch):
    import render_cn_chat as pipeline
    import twitch_download

    video = tmp_path / "video.mp4"
    chat = tmp_path / "chat.html"
    video.write_bytes(b"video")
    chat.write_text('<div class="comment-root"></div>', encoding="utf-8")
    monkeypatch.setattr(
        twitch_download,
        "download_assets",
        lambda *args, **kwargs: SimpleNamespace(video_path=video, chat_html_path=chat),
    )
    monkeypatch.setattr(pipeline, "_post_download_next_steps", lambda *args, **kwargs: 0)
    runner = pipeline.PipelineRunner()
    args = SimpleNamespace(
        download="2819850140",
        download_dir=str(tmp_path),
        segment=[],
        cut=[],
        kind="vod",
        quality="720p60",
        oauth=None,
        begin=None,
        end=None,
        download_output_fps=None,
        download_encoder="auto",
        download_trim_mode="Safe",
        media_check="off",
        media_repair="off",
        download_only=True,
        yes=True,
    )
    assert pipeline._run_download_flow(args, runner=runner) == 0
    assert runner.mode == "download"
    assert ("video", video) in runner.artifacts
    assert ("chat_html", chat) in runner.artifacts


def test_textual_history_loads_saved_draft(tmp_path: Path):
    pytest.importorskip("textual")
    from textual.widgets import Input

    from tui_run import OverlayTui

    draft = TuiJobDraft(video="saved.mp4", chat_html="saved.html", mode=MODE_ORIGINAL_PREVIEW)

    async def exercise() -> None:
        app = OverlayTui()
        app.history = TuiHistoryStore(tmp_path / "history.json")
        record = app.history.start(draft, label="saved")
        async with app.run_test():
            app._refresh_history()
            app._select_history(record["id"])
            app._load_history_draft()
            assert app.query_one("#video", Input).value == "saved.mp4"
            assert "已载入" in str(app.query_one("#status").render())

    import asyncio

    asyncio.run(exercise())


def test_textual_history_clear_requires_confirmation_and_preserves_running_task(tmp_path: Path):
    pytest.importorskip("textual")

    from helpers import wait_for_widget
    from tui_history import TuiHistoryStore
    from tui_run import OverlayTui

    async def exercise() -> None:
        app = OverlayTui()
        app.history = TuiHistoryStore(tmp_path / "history.json")
        record = app.history.start(None, label="running")
        async with app.run_test() as pilot:
            await wait_for_widget(app, pilot, "#history-clear")
            app.session = type("Session", (), {"running": True})()
            app._set_history_clear_enabled(False)
            assert app.query_one("#history-clear").disabled is True
            app._clear_history()
            assert app.history.get(record["id"]) is not None
            assert "不能清空历史" in str(app.query_one("#status").render())

            app.session = type("Session", (), {"running": False, "returncode": 0})()
            app._handled_session = None
            app._clear_history()
            assert app.history.get(record["id"]) is not None

            app.session = None
            app._handled_session = None
            app.history.finish(record["id"], state="succeeded", returncode=0, result_path=None)
            app._set_history_clear_enabled(True)
            app._clear_history()
            assert app.history.get(record["id"]) is not None
            assert "再次点击" in str(app.query_one("#status").render())
            app._clear_history()
            assert app.history.list_records() == []

    import asyncio

    asyncio.run(exercise())


def test_textual_history_actions_require_explicit_selection(tmp_path: Path):
    pytest.importorskip("textual")

    from tui_history import TuiHistoryStore
    from tui_run import OverlayTui

    async def exercise() -> None:
        app = OverlayTui()
        app.history = TuiHistoryStore(tmp_path / "history.json")
        record = app.history.start(None, label="active")
        app.active_history_id = record["id"]
        async with app.run_test():
            assert app._history_record() is None
            assert "选择一个任务" in str(app.query_one("#status").render())

    import asyncio

    asyncio.run(exercise())


def test_textual_session_records_terminal_history(tmp_path: Path):
    pytest.importorskip("textual")
    from tui_run import OverlayTui

    draft = TuiJobDraft(video="saved.mp4", chat_html="saved.html", mode=MODE_ORIGINAL_PREVIEW)
    child = (
        "import json, os; "
        "p=os.environ['TWITCH_OVERLAY_RESULT_FILE']; "
        "open(p, 'w', encoding='utf-8').write(json.dumps({'schema_version':1,'state':'succeeded','mode':'preview','returncode':0,'finished_at':1,'artifacts':[]}))"
    )

    async def exercise() -> None:
        app = OverlayTui()
        app.history = TuiHistoryStore(tmp_path / "history.json")
        async with app.run_test() as pilot:
            app._start_command("history probe", [sys.executable, "-c", child], draft=draft)
            for _ in range(80):
                await pilot.pause(0.05)
                if app.session and not app.session.running and app._handled_session is app.session:
                    break
            records = app.history.list_records()
            assert records[0]["state"] == "succeeded"
            result = app.history.result_for(records[0])
            assert result is not None and result["mode"] == "preview"
            assert Path(records[0]["result_path"]).is_file()

    import asyncio

    asyncio.run(exercise())


def test_textual_manual_required_is_not_reported_as_render_success(tmp_path: Path):
    pytest.importorskip("textual")
    from tui_run import OverlayTui

    child = (
        "import json, os; "
        "p=os.environ['TWITCH_OVERLAY_RESULT_FILE']; "
        "open(p, 'w', encoding='utf-8').write(json.dumps({'schema_version':1,'state':'manual_required','mode':'full','returncode':0,'finished_at':1,'artifacts':[]}))"
    )

    async def exercise() -> None:
        app = OverlayTui()
        app.history = TuiHistoryStore(tmp_path / "history.json")
        async with app.run_test() as pilot:
            app._start_command("translate", [sys.executable, "-c", child])
            for _ in range(80):
                await pilot.pause(0.05)
                if app.session and not app.session.running and app._handled_session is app.session:
                    break
            assert app.history.list_records()[0]["state"] == "manual_required"
            status_text = str(app.query_one("#status").render())
            assert "【人工复核已就绪】" in status_text or "载入复核表并压制" in status_text

    import asyncio

    asyncio.run(exercise())


def test_textual_failures_persist_independent_redacted_diagnostics(tmp_path: Path):
    pytest.importorskip("textual")
    from tui_run import OverlayTui

    first = "import sys; print('Authorization: Bearer first-secret'); raise SystemExit(2)"
    second = "import sys; print('Authorization: Bearer second-secret'); raise SystemExit(3)"

    async def exercise() -> None:
        app = OverlayTui()
        app.history = TuiHistoryStore(tmp_path / "history.json")
        async with app.run_test() as pilot:
            for label, child in (("first failure", first), ("second failure", second)):
                app._start_command(label, [sys.executable, "-c", child])
                for _ in range(80):
                    await pilot.pause(0.05)
                    if app.session and not app.session.running and app._handled_session is app.session:
                        break
            records = app.history.list_records()
            paths = [Path(record["diagnostic_path"]) for record in records]
            assert [record["state"] for record in records] == ["failed", "failed"]
            assert len(set(paths)) == 2 and all(path.is_file() for path in paths)
            active_path = next(
                Path(record["diagnostic_path"])
                for record in records
                if record["id"] == app.active_history_id
            )
            app._export_diagnostics()
            refreshed = app.history.get(str(app.active_history_id))
            assert refreshed is not None and Path(refreshed["diagnostic_path"]) == active_path
            assert app.session is not None
            assert app.session.event_path is not None and not app.session.event_path.exists()
            assert app.session.result_path is not None and not app.session.result_path.exists()
        # A fresh store simulates reopening the TUI after both failures.
        records = TuiHistoryStore(tmp_path / "history.json").list_records()
        text = "\n".join(Path(record["diagnostic_path"]).read_text(encoding="utf-8") for record in records)
        assert "first-secret" not in text and "second-secret" not in text

    import asyncio

    asyncio.run(exercise())


def test_textual_unmount_marks_running_task_interrupted(tmp_path: Path):
    pytest.importorskip("textual")
    from tui_run import OverlayTui

    async def exercise() -> None:
        app = OverlayTui()
        app.history = TuiHistoryStore(tmp_path / "history.json")
        async with app.run_test() as pilot:
            # The 30s child sleep is never waited for: it only guarantees the
            # task outlives this sub-second test window on slow runners.
            # Leaving the run_test context unmounts the app, whose cleanup calls
            # session.cancel() -> kill_process_tree, reaping the child at once
            # and marking the history record "interrupted".
            app._start_command("interrupt probe", [sys.executable, "-c", "import time; time.sleep(30)"])
            await pilot.pause(0.1)
            assert app.session and app.session.running
        assert app.history.list_records()[0]["state"] == "interrupted"

    import asyncio

    asyncio.run(exercise())


@pytest.mark.slow
def test_textual_demo_records_manifest_artifacts(tmp_path: Path):
    pytest.importorskip("textual")
    from textual.widgets import TabbedContent

    from tui_run import OverlayTui

    async def exercise() -> None:
        app = OverlayTui()
        app.history = TuiHistoryStore(tmp_path / "history.json")
        async with app.run_test(size=(140, 45)) as pilot:
            app.query_one(TabbedContent).active = "task"
            await pilot.pause(0.1)
            await pilot.click("#demo")
            for _ in range(720):
                await pilot.pause(0.1)
                if app.session and not app.session.running and app._handled_session is app.session:
                    break
            records = app.history.list_records()
            assert records[0]["state"] == "succeeded"
            result = app.history.result_for(records[0])
            assert result is not None
            artifacts = result["artifacts"]
            assert any(artifact["kind"] == "video" and artifact["path"].endswith("demo_overlay.mp4") for artifact in artifacts)

    import asyncio

    asyncio.run(exercise())


def test_textual_download_surfaces_hls_boundary_expansion(tmp_path: Path, monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import TabbedContent

    from tui_run import OverlayTui
    import twitch_download

    video = tmp_path / "video.mp4"
    chat = tmp_path / "chat.html"
    video.write_bytes(b"video")
    chat.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(twitch_download, "probe_media_duration", lambda _path: 20.0)

    async def exercise() -> None:
        app = OverlayTui()
        app.history = TuiHistoryStore(tmp_path / "history.json")
        async with app.run_test(size=(140, 45)):
            app.query_one(TabbedContent).active = "download"
            app.download_requested_duration_s = 8.0
            app.session = type(
                "Session",
                (),
                {
                    "result": {"artifacts": [{"kind": "video", "path": str(video)}, {"kind": "chat_html", "path": str(chat)}]},
                    "running": False,
                },
            )()
            assert app._apply_download_result() is True
            assert "实际下载视频为 20.0 秒" in app.download_duration_note
            monkeypatch.setattr(twitch_download, "probe_media_duration", lambda _path: 9.0)
            assert app._download_duration_note(str(video)) == ""
            app.session = None

    import asyncio

    asyncio.run(exercise())
