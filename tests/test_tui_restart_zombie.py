"""concurrency-4/5 回归:取消重启不留僵尸 running;历史锁超时降级不打崩 TUI。"""
import asyncio
from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import tui_history  # noqa: E402


def test_restart_after_cancel_finishes_stale_running_record(tmp_path: Path, monkeypatch):
    """取消后立刻 _start_command:旧 active 记录被 finish 为 cancelled,新记录成为 active。"""
    pytest.importorskip("textual")
    from tui_models import TuiJobDraft
    from tui_run import OverlayTui

    async def exercise() -> None:
        app = OverlayTui()
        app.history = tui_history.TuiHistoryStore(tmp_path / "history.json")
        async with app.run_test() as pilot:
            app._start_command("first", [sys.executable, "-c", "import time; time.sleep(30)"])
            assert app.session is not None and app.session.running
            old_id = app.active_history_id
            assert app.history.get(old_id)["state"] == "running"

            # 取消,并停掉 0.15s 轮询定时器——本用例钉的正是"终态分支还没收割
            # 旧会话时用户就重启"的窗口;慢机(CI)上等待循环可能跨过一个 tick,
            # 不停表会让窗口场景变成竞态。
            assert app.session.cancel()
            if app.poll_timer is not None:
                app.poll_timer.stop()
            deadline = time.monotonic() + 10.0
            while app.session.running and time.monotonic() < deadline:
                await pilot.pause(0.02)
            assert not app.session.running

            # 窗口内重启:旧会话尚未被 _poll_session_once 收尾。
            assert app._handled_session is not app.session
            draft = TuiJobDraft(video="v.mp4", chat_html="c.html")
            app._start_command(
                "second",
                [sys.executable, "-c", "import time; time.sleep(30)"],
                draft=draft,
            )
            assert app.history.get(old_id)["state"] == "cancelled"
            new_id = app.active_history_id
            assert new_id != old_id
            assert app.history.get(new_id)["state"] == "running"
            # 新会话存活(供 run_test 收尾);清理避免残留子进程。
            app.session.cancel()

    asyncio.run(exercise())


def test_clear_history_lock_timeout_degrades(tmp_path: Path, monkeypatch):
    """concurrency-5(clear 侧): 无在飞任务时 clear 裸抛被降级提示,TUI 存活。"""
    pytest.importorskip("textual")
    from tui_run import OverlayTui

    async def exercise() -> None:
        app = OverlayTui()
        app.history = tui_history.TuiHistoryStore(tmp_path / "history.json")
        monkeypatch.setattr(
            app.history,
            "clear",
            lambda: (_ for _ in ()).throw(tui_history.HistoryLockTimeoutError("held")),
        )
        async with app.run_test():
            app._history_clear_confirmation_until = time.monotonic() + 10.0
            app._clear_history()
            status = str(app.query_one("#status").render())
            assert "历史未清空" in status
            assert app.session is None  # TUI 未被异常打崩

    asyncio.run(exercise())


def test_history_lock_timeout_degrades_instead_of_crashing(tmp_path: Path, monkeypatch):
    """concurrency-5: mark_running/clear 遇 HistoryLockTimeoutError 降级提示,不沿消息链上抛。"""
    pytest.importorskip("textual")
    from tui_run import OverlayTui

    async def exercise() -> None:
        app = OverlayTui()
        app.history = tui_history.TuiHistoryStore(tmp_path / "history.json")
        monkeypatch.setattr(
            app.history,
            "mark_running",
            lambda *_a, **_k: (_ for _ in ()).throw(
                tui_history.HistoryLockTimeoutError("history lock held by another instance")
            ),
        )
        async with app.run_test() as pilot:
            # 启动任务:mark_running 抛出会被降级,任务照常启动且 TUI 存活。
            app._start_command("probe", [sys.executable, "-c", "import time; time.sleep(30)"])
            assert app.session is not None and app.session.running
            status = str(app.query_one("#status").render())
            assert "任务历史暂时无法更新" in status
            app.session.cancel()
            await pilot.pause()

    asyncio.run(exercise())
