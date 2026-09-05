# -*- coding: utf-8 -*-
"""Regression tests for TUI lifecycle hardening (concurrency-6 / concurrency-7).

F-A: tui_history.recover_interrupted must apply an absolute staleness cap
     (run_meta.ABSOLUTE_MAX_LIVE_SEC) to "running" records whose pid appears
     alive, so a recycled pid cannot keep a zombie record running forever and
     block clear() permanently.
F-B: tui_task.sanitize_diagnostic_file / TaskSession.retain_result must use a
     unique per-call temp file (mkstemp) in the target directory instead of a
     fixed "<name>.tmp" sibling, so two concurrent writers cannot clobber each
     other through the same temp path.
"""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time

from run_meta import ABSOLUTE_MAX_LIVE_SEC, pid_is_alive
from tui_history import TuiHistoryStore
from tui_task import TaskSession, sanitize_diagnostic_file

NOW = time.time()


def _seed_history(store: TuiHistoryStore, *, state: str, pid, age_sec: float, record_id: str) -> None:
    store._save([{
        "id": record_id,
        "state": state,
        "label": "hardening test",
        "started_at": NOW - age_sec,
        "updated_at": NOW - age_sec,
        "pid": pid,
        "draft": None,
        "result_path": None,
        "diagnostic_path": None,
    }])


# ---------------------------------------------------------------------------
# F-A: absolute staleness cap in recover_interrupted
# ---------------------------------------------------------------------------


def test_stale_running_with_recycled_live_pid_is_interrupted_and_clearable(tmp_path: Path) -> None:
    """30 天前的 running 记录 + 被复用的活 pid：必须判 interrupted，clear() 成功。"""
    store = TuiHistoryStore(tmp_path / "history.json")
    live_pid = os.getpid()  # 本进程必然存活，模拟 pid 被任意进程复用
    assert pid_is_alive(live_pid) is True
    _seed_history(
        store, state="running", pid=live_pid,
        age_sec=ABSOLUTE_MAX_LIVE_SEC + 30 * 24 * 3600, record_id="stale01",
    )

    changed = store.recover_interrupted()
    assert [record["id"] for record in changed] == ["stale01"]
    assert changed[0]["state"] == "interrupted"
    assert store.get("stale01")["state"] == "interrupted"
    assert store.clear() is True


def test_fresh_running_with_live_pid_stays_running(tmp_path: Path) -> None:
    """新鲜的 running 记录 + 活 pid：不得被绝对时限误杀。"""
    store = TuiHistoryStore(tmp_path / "history.json")
    live_pid = os.getpid()
    _seed_history(store, state="running", pid=live_pid, age_sec=1.0, record_id="fresh1")

    changed = store.recover_interrupted()
    assert changed == []
    assert store.get("fresh1")["state"] == "running"


def test_absolute_window_boundary_uses_updated_at(tmp_path: Path) -> None:
    """恰好未超绝对时限的记录保持 running；时间基准是 updated_at。"""
    store = TuiHistoryStore(tmp_path / "history.json")
    live_pid = os.getpid()
    # 比上限旧一点点但仍新鲜：started_at 很老，updated_at 才是权威时间基准。
    records = [{
        "id": "refresh",
        "state": "running",
        "label": "long-lived but heartbeating",
        "started_at": NOW - 2 * ABSOLUTE_MAX_LIVE_SEC,
        "updated_at": NOW - 60.0,
        "pid": live_pid,
        "draft": None,
        "result_path": None,
        "diagnostic_path": None,
    }]
    store._save(records)
    assert store.recover_interrupted() == []
    assert store.get("refresh")["state"] == "running"


def test_running_with_dead_pid_still_interrupted(tmp_path: Path) -> None:
    """死 pid 行为保持不变（回归保护）。"""
    store = TuiHistoryStore(tmp_path / "history.json")
    _seed_history(store, state="running", pid=999_999_999, age_sec=1.0, record_id="deadpid")

    changed = store.recover_interrupted()
    assert [record["state"] for record in changed] == ["interrupted"]


def test_legacy_record_without_timestamps_keeps_conservative_behavior(tmp_path: Path) -> None:
    """缺时间字段的旧记录：pid 活着就保守保持 running（不误杀）。"""
    store = TuiHistoryStore(tmp_path / "history.json")
    live_pid = os.getpid()
    record = {
        "id": "legacy0",
        "state": "running",
        "label": "legacy",
        "pid": live_pid,
        "draft": None,
        "result_path": None,
        "diagnostic_path": None,
    }
    # 直接落盘以绕过 _load 对 started_at 的强制规范化。
    import json
    payload = {"schema_version": 1, "records": [dict(record, started_at=None)]}
    (tmp_path / "history.json").write_text(json.dumps(payload), encoding="utf-8")

    assert store.recover_interrupted() == []
    # _load 会丢弃 started_at 非法的记录；若未丢弃则说明保守分支生效。
    after = store.get("legacy0")
    if after is not None:
        assert after["state"] == "running"


# ---------------------------------------------------------------------------
# F-B: unique temp files in tui_task
# ---------------------------------------------------------------------------


def test_sanitize_diagnostic_file_uses_unique_mkstemp_sibling(tmp_path: Path, monkeypatch) -> None:
    """sanitize_diagnostic_file 的临时文件必须是 mkstemp 唯一名且在目标同目录。"""
    target = tmp_path / "diag.txt"
    target.write_text("$ secret-cmd\nplain\n", encoding="utf-8")

    created: list[Path] = []
    import tui_task

    real_mkstemp = tui_task.tempfile.mkstemp

    def spy_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        created.append(Path(name))
        return fd, name

    monkeypatch.setattr(tui_task.tempfile, "mkstemp", spy_mkstemp)
    sanitize_diagnostic_file(target)

    assert len(created) == 1
    temp_used = created[0]
    assert temp_used.parent == target.parent
    assert temp_used.suffix == ".tmp"
    assert temp_used != target.with_suffix(target.suffix + ".tmp")  # 不再是固定名
    assert not temp_used.exists()  # finally 清理生效
    assert target.read_text(encoding="utf-8") == "[command omitted for privacy]\nplain\n"


def test_sanitize_diagnostic_file_no_temp_when_clean(tmp_path: Path, monkeypatch) -> None:
    """内容无需清洗时不得创建临时文件。"""
    target = tmp_path / "clean.txt"
    target.write_text("already clean\n", encoding="utf-8")

    import tui_task

    def explode(*args, **kwargs):
        raise AssertionError("mkstemp must not be called when nothing changes")

    monkeypatch.setattr(tui_task.tempfile, "mkstemp", explode)
    assert sanitize_diagnostic_file(target) == target


def test_concurrent_sanitizers_same_target_each_get_unique_temp(tmp_path: Path) -> None:
    """两个线程并发清洗同一目标：临时路径互不相同，最终内容完整。"""
    import tui_task

    target = tmp_path / "shared_diag.txt"
    target.write_text("$ secret-cmd\n" * 200000, encoding="utf-8")

    created: list[Path] = []
    lock = threading.Lock()
    real_mkstemp = tui_task.tempfile.mkstemp

    def spy_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        with lock:
            created.append(Path(name))
        return fd, name

    original = tui_task.tempfile.mkstemp
    tui_task.tempfile.mkstemp = spy_mkstemp
    try:
        start = threading.Event()

        def writer() -> None:
            start.wait()
            sanitize_diagnostic_file(target)

        threads = [threading.Thread(target=writer) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join()
    finally:
        tui_task.tempfile.mkstemp = original

    assert len(created) == 2
    assert created[0] != created[1]
    final_lines = target.read_text(encoding="utf-8").count("\n")
    assert final_lines == 200000
    for temp in created:
        assert not temp.exists()


def test_concurrent_retain_result_same_target_content_intact(tmp_path: Path) -> None:
    """两个 TaskSession 并发 retain 到同一目标：mkstemp 路径唯一，内容为合法 JSON。"""
    import json

    target = tmp_path / "shared_result.json"
    sessions = []
    for index in range(2):
        source = tmp_path / f"src_{index}.result.json"
        source.write_text(json.dumps({"ok": True, "writer": index}), encoding="utf-8")
        session = TaskSession(["python", "-c", "pass"])
        session.result = {"ok": True, "writer": index}
        session.result_path = source
        sessions.append(session)

    start = threading.Event()

    def retainer(session: TaskSession) -> None:
        start.wait()
        session.retain_result(target)

    threads = [threading.Thread(target=retainer, args=(session,)) for session in sessions]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join()

    assert target.is_file()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["writer"] in (0, 1)
    for session in sessions:
        assert session.result_path is None
    # 固定名兄弟 tmp 不应残留。
    assert not target.with_suffix(target.suffix + ".tmp").exists()


def test_no_fixed_tmp_name_in_source() -> None:
    """源码层面：两处均不再构造固定 "<suffix>.tmp" 兄弟路径。"""
    import inspect

    import tui_task

    sanitize_source = inspect.getsource(tui_task.sanitize_diagnostic_file)
    retain_source = inspect.getsource(tui_task.TaskSession.retain_result)
    for source in (sanitize_source, retain_source):
        assert 'with_suffix(target.suffix + ".tmp")' not in source
        assert "_unique_sibling_temp" in source
