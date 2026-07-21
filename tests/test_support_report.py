from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import support_report
from task_results import RESULT_FILE_ENV, read_task_result
from tui_history import TuiHistoryStore


def test_support_summary_removes_credentials_and_common_private_paths(monkeypatch):
    monkeypatch.setattr(support_report, "installed_version", lambda: "0.2.4.dev0")
    summary = support_report.build_summary(
        doctor_returncode=1,
        doctor_output=(
            "[FAIL] ffmpeg: C:\\Users\\Alice\\tools\\ffmpeg.exe\n"
            "API_KEY=secret-value\n"
            "cache: /home/alice/.cache/twitch\n"
            "[OK] 翻译 Base URL: https://user:password@internal.example/v1?api_key=query-secret\n"
            "OPENAI_COMPAT_BASE_URL=https://another-user:another-password@example.invalid/v1\n"
        ),
        generated_at=datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc),
    )

    assert "0.2.4.dev0" in summary
    assert "doctor exit code: 1" in summary
    assert all(secret not in summary for secret in ("secret-value", "password", "query-secret", "another-password"))
    assert "Alice" not in summary and "alice" not in summary
    assert "[local path]" in summary
    assert "Before sharing" in summary


def test_main_writes_reviewable_summary(tmp_path: Path, monkeypatch):
    output = tmp_path / "nested" / "summary.txt"
    result = tmp_path / "result.json"
    monkeypatch.setattr(support_report, "run_doctor", lambda **_kwargs: (0, "[OK] Python: 3.12"))
    monkeypatch.setattr(support_report, "installed_version", lambda: "test-version")
    monkeypatch.setenv(RESULT_FILE_ENV, str(result))

    assert support_report.main(["--output", str(output)]) == 0

    text = output.read_text(encoding="utf-8")
    assert "project version: test-version" in text
    assert "[OK] Python: 3.12" in text
    manifest = read_task_result(result)
    assert manifest is not None
    assert manifest["artifacts"] == [{"kind": "support_summary", "path": str(output.resolve())}]


def test_doctor_summary_disables_interactive_fix_prompts(monkeypatch):
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen.update(kwargs)
        return type("Result", (), {"returncode": 0, "stdout": "[OK]"})()

    monkeypatch.setattr(support_report.subprocess, "run", fake_run)

    assert support_report.run_doctor(python="python", pipeline="pipeline.py", timeout=12) == (0, "[OK]")
    assert seen["command"] == ["python", "pipeline.py", "--doctor"]
    assert seen["stdin"] is subprocess.DEVNULL


def test_tui_history_opens_a_persisted_support_summary(tmp_path: Path, monkeypatch):
    pytest.importorskip("textual")
    from tui_run import OverlayTui

    report = tmp_path / "support-reports" / "issue-summary.txt"
    report.parent.mkdir()
    report.write_text("summary", encoding="utf-8")
    result = tmp_path / "result.json"
    monkeypatch.setenv(RESULT_FILE_ENV, str(result))
    assert support_report.write_task_result(
        state="succeeded",
        mode="support-summary",
        returncode=0,
        artifacts=[("support_summary", report)],
    )
    history = TuiHistoryStore(tmp_path / "history.json")
    record = history.start(None, label="Issue summary")
    history.mark_running(record["id"], pid=None, result_path=result)
    history.finish(record["id"], state="succeeded", returncode=0, result_path=result)

    async def exercise() -> None:
        app = OverlayTui()
        app.history = history
        opened: list[Path] = []
        monkeypatch.setattr(app, "_open_result_dir", lambda: opened.append(app.result_directory))
        async with app.run_test():
            app._set_input("#history-id", record["id"])
            app._open_history_artifacts()
            assert opened == [report.parent.resolve()]

    import asyncio

    asyncio.run(exercise())


def test_tui_support_summary_starts_a_report_task(monkeypatch):
    import tui_run
    from tui_run import OverlayTui

    app = OverlayTui()
    captured: dict[str, object] = {}

    def start_command(label, command, **kwargs):
        captured.update(label=label, command=command, **kwargs)

    monkeypatch.setattr(app, "_start_command", start_command)
    monkeypatch.setattr("tui_run.time.time_ns", lambda: 123456)

    app._start_support_summary()

    assert captured["label"] == "生成 Issue 自检摘要"
    assert captured["task_kind"] == "support-summary"
    command = captured["command"]
    root = Path(tui_run.__file__).resolve().parent.parent
    assert command[:2] == [sys.executable, str(root / "scripts" / "support_report.py")]
    assert command[-2:] == ["--output", str(root / "outputs" / "support-reports" / "issue-summary-123456.txt")]
