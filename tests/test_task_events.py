from __future__ import annotations

import importlib
import json
import subprocess


def test_event_writer_is_opt_in(monkeypatch):
    import task_events

    monkeypatch.delenv(task_events.EVENT_FILE_ENV, raising=False)
    assert task_events.emit_task_event("command_started", program="render") is False


def test_event_writer_appends_jsonl(tmp_path, monkeypatch):
    import task_events

    path = tmp_path / "events" / "task.jsonl"
    monkeypatch.setenv(task_events.EVENT_FILE_ENV, str(path))

    assert task_events.emit_task_event("command_started", program="render_cn_chat.py") is True
    event = json.loads(path.read_text(encoding="utf-8"))
    assert event["event"] == "command_started"
    assert event["schema_version"] == task_events.EVENT_SCHEMA_VERSION
    assert event["program"] == "render_cn_chat.py"
    assert isinstance(event["timestamp"], float)


def test_pipeline_command_events_do_not_include_arguments(monkeypatch):
    import render_cn_chat as pipeline

    pipeline = importlib.reload(pipeline)

    events: list[dict] = []

    def capture(kind: str, **fields) -> bool:
        events.append({"event": kind, **fields})
        return True

    monkeypatch.setattr(pipeline, "DRY_RUN", False)
    monkeypatch.setattr(pipeline, "emit_task_event", capture)
    monkeypatch.setattr(
        pipeline,
        "run_tracked",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 0),
    )

    pipeline.run(["python", "twitch_chat_burn.py", "--secret-like-value", "hidden"])

    assert [event["event"] for event in events] == [
        "command_started",
        "stage_started",
        "command_exited",
        "stage_completed",
    ]
    assert events[0]["program"] == "twitch_chat_burn.py"
    assert events[1]["stage"] == "render"
    assert all("hidden" not in event.values() for event in events)
