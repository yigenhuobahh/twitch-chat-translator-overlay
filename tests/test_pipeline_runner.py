from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from pipeline_runner import PipelineRunner, activate_runner, active_runner, emit_task_event
from task_events import EVENT_FILE_ENV
from task_results import RESULT_FILE_ENV, read_task_result


def test_runner_scopes_events_and_preserves_manual_result(tmp_path: Path, monkeypatch):
    artifact = tmp_path / "review.xlsx"
    artifact.write_bytes(b"review")
    event_path = tmp_path / "events.jsonl"
    result_path = tmp_path / "result.json"
    monkeypatch.setenv(EVENT_FILE_ENV, str(event_path))
    monkeypatch.setenv(RESULT_FILE_ENV, str(result_path))
    runner = PipelineRunner()

    assert active_runner() is None
    with activate_runner(runner):
        assert active_runner() is runner
        runner.configure(mode="full", artifacts=[("review_xlsx", artifact)])
        assert emit_task_event("stage_started", stage="translate") is True
        runner.mark_manual_required()
        assert runner.publish_terminal_result("succeeded", 0) is True
    assert active_runner() is None

    assert json.loads(event_path.read_text(encoding="utf-8"))["event"] == "stage_started"
    result = read_task_result(result_path)
    assert result is not None
    assert result["state"] == "manual_required"
    assert result["mode"] == "full"
    assert result["artifacts"] == [{"kind": "review_xlsx", "path": str(artifact.resolve())}]


@pytest.mark.parametrize(
    ("raised", "expected_returncode"),
    [(SystemExit(3), 3), (RuntimeError("unexpected"), 1)],
)
def test_pipeline_main_publishes_failure_and_restores_context(tmp_path: Path, monkeypatch, raised, expected_returncode):
    import render_cn_chat as pipeline

    result_path = tmp_path / "result.json"
    monkeypatch.setenv(RESULT_FILE_ENV, str(result_path))
    parent = PipelineRunner(mode="parent")

    def fake_main():
        assert pipeline.active_runner() is not None
        raise raised

    monkeypatch.setattr(pipeline, "_main", fake_main)
    with activate_runner(parent):
        if isinstance(raised, SystemExit):
            with pytest.raises(SystemExit):
                pipeline.main()
        else:
            with pytest.raises(RuntimeError, match="unexpected"):
                pipeline.main()
        assert active_runner() is parent
    assert active_runner() is None

    result = read_task_result(result_path)
    assert result is not None
    assert result["state"] == "failed"
    assert result["returncode"] == expected_returncode
