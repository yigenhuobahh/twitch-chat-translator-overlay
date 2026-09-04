#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""In-process coverage for job_wizard interactive menu loops and command routing.

Follows the monkeypatch pattern proven in test_job_wizard_interactive.py:
answer iterators are fed through ``wizard._prompt`` (and friends), every
collaborator is patched on the job_wizard module, and assertions target the
interaction sequence / return codes rather than implementation details.
"""

from __future__ import annotations

import builtins
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def jobs_dir(tmp_path: Path) -> Path:
    """Isolated jobs/ root so default_jobs_dir() never sees the repo's jobs/."""
    root = tmp_path / "jobs"
    root.mkdir()
    return root


def make_job(jobs_dir: Path, name: str = "style") -> Path:
    path = jobs_dir / f"{name}.yaml"
    path.write_text("mode: preview\nrender_original: true\n", encoding="utf-8")
    return path


def patch_wizard(jobs_dir: Path, monkeypatch, *, answers, prompt_recorder=None):
    """Common hermetic wiring: prompts, jobs root, and a never-real pipeline."""
    import job_wizard as wizard

    calls: dict[str, list] = {}

    def record(key: str):
        def _spy(*args, **kwargs):
            calls.setdefault(key, []).append(args)
            return 0

        return _spy

    def _prompt(msg: str, default: str | None = None, **_kwargs):
        if prompt_recorder is not None:
            prompt_recorder.append((msg, default))
        return next(answers)

    monkeypatch.setattr(wizard, "_prompt", _prompt)
    monkeypatch.setattr(wizard, "_prompt_secret", lambda *_a, **_k: "")
    monkeypatch.setattr(wizard, "_stdin_is_interactive", lambda: False)
    monkeypatch.setattr(wizard, "default_jobs_dir", lambda: jobs_dir)
    monkeypatch.setattr(wizard, "save_last_job", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "_run_pipeline", record("pipeline"))
    return wizard, calls


# ---------------------------------------------------------------------------
# run_menu: top-level loop dispatch
# ---------------------------------------------------------------------------


def test_run_menu_exit_choice_returns_zero(jobs_dir, monkeypatch, capsys):
    wizard, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["0"]))

    assert wizard.run_menu() == 0
    assert "再见。" in capsys.readouterr().out


def test_run_menu_dispatches_quick_start_then_exits(jobs_dir, monkeypatch):
    import job_wizard as wizard

    _wizard, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["1", "0"]))
    entered: list[bool] = []
    monkeypatch.setattr(wizard, "run_quick_start", lambda: entered.append(True) or 0)

    assert wizard.run_menu() == 0
    assert entered == [True]


def test_run_menu_continue_last_job_confirms_with_pinned_path(jobs_dir, monkeypatch):
    import job_wizard as wizard

    job = make_job(jobs_dir)
    (jobs_dir / ".last_job").write_text(f"{job}\n", encoding="utf-8")
    _wizard, calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["2", "0"]))
    monkeypatch.setattr(
        wizard, "_confirm_and_run_job", lambda path, **k: calls.setdefault("ran", []).append(path) or 0
    )

    assert wizard.run_menu() == 0
    assert calls["ran"] == [job.resolve()]


def test_run_menu_continue_last_job_failure_reports_fail(jobs_dir, monkeypatch, capsys):
    import job_wizard as wizard

    job = make_job(jobs_dir)
    (jobs_dir / ".last_job").write_text(f"{job}\n", encoding="utf-8")
    _wizard, calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["2", "0"]))

    def raise_eof(path, **k):
        raise EOFError("stdin closed")

    monkeypatch.setattr(wizard, "_confirm_and_run_job", raise_eof)

    assert wizard.run_menu() == 0
    assert "[FAIL]" in capsys.readouterr().out


def test_run_menu_continue_without_last_job_prints_hint(jobs_dir, monkeypatch, capsys):
    import job_wizard as wizard

    _wizard, calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["2", "0"]))
    monkeypatch.setattr(wizard, "_confirm_and_run_job", lambda *a, **k: calls.setdefault("ran", []).append(a))

    assert wizard.run_menu() == 0
    assert "ran" not in calls
    assert "还没有上次任务" in capsys.readouterr().out


def test_run_menu_choose_existing_job_receives_jobs_root(jobs_dir, monkeypatch):
    import job_wizard as wizard

    _wizard, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["3", "0"]))
    seen_roots: list[Path] = []
    monkeypatch.setattr(wizard, "_choose_existing_job", lambda root: seen_roots.append(root) or 0)

    assert wizard.run_menu() == 0
    assert seen_roots == [jobs_dir]


def test_run_menu_offline_demo_and_tools_dispatch(jobs_dir, monkeypatch):
    import job_wizard as wizard

    _wizard, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["4", "5", "0"]))
    demo: list[bool] = []
    tools_roots: list[Path] = []
    monkeypatch.setattr(wizard, "_run_offline_demo", lambda: demo.append(True) or 0)
    monkeypatch.setattr(wizard, "_run_tools_menu", lambda root: tools_roots.append(root) or None)

    assert wizard.run_menu() == 0
    assert demo == [True]
    assert tools_roots == [jobs_dir]


def test_run_menu_invalid_choice_reprompts_before_exit(jobs_dir, monkeypatch, capsys):
    wizard, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["9", "0"]))

    assert wizard.run_menu() == 0
    assert "无效选择" in capsys.readouterr().out


@pytest.mark.parametrize("has_last,expected_default", [(False, "1"), (True, "2")])
def test_run_menu_default_follows_last_job_state(
    jobs_dir, monkeypatch, has_last: bool, expected_default: str
):
    if has_last:
        job = make_job(jobs_dir)
        (jobs_dir / ".last_job").write_text(f"{job}\n", encoding="utf-8")
    prompts: list[tuple[str, str | None]] = []
    wizard, _calls = patch_wizard(
        jobs_dir, monkeypatch, answers=iter(["0"]), prompt_recorder=prompts
    )

    assert wizard.run_menu() == 0
    assert prompts[0] == ("请选择", expected_default)


# ---------------------------------------------------------------------------
# _run_legacy_menu: full Chinese launcher branches
# ---------------------------------------------------------------------------


def test_legacy_menu_exit_returns_zero(jobs_dir, monkeypatch, capsys):
    wizard, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["0"]))

    assert wizard._run_legacy_menu() == 0
    assert "再见。" in capsys.readouterr().out


def test_legacy_menu_new_config_runs_wizard_and_announces_index(jobs_dir, monkeypatch, capsys):
    import job_wizard as wizard

    created = jobs_dir / "new_style.yaml"
    _wizard, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["1", "0"]))
    wizard_roots: list[Path] = []
    monkeypatch.setattr(
        wizard, "run_job_wizard", lambda *, jobs_dir=None, **k: wizard_roots.append(jobs_dir) or created
    )
    monkeypatch.setattr(wizard, "_list_index_for", lambda *_a: 2)

    assert wizard._run_legacy_menu() == 0
    assert wizard_roots == [jobs_dir]
    assert "配置在列表第 [2] 项" in capsys.readouterr().out


def test_legacy_menu_pick_existing_job_by_number_runs_it(jobs_dir, monkeypatch):
    import job_wizard as wizard

    job = make_job(jobs_dir, "pickme")
    _wizard, calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["2", "1", "", "0"]))
    monkeypatch.setattr(
        wizard, "_confirm_and_run_job", lambda path, **k: calls.setdefault("ran", []).append(path) or 0
    )

    assert wizard._run_legacy_menu() == 0
    assert calls["ran"] == [job]


def test_legacy_menu_pick_existing_job_by_name_uses_resolver(jobs_dir, monkeypatch):
    import job_wizard as wizard

    job = make_job(jobs_dir, "named")
    _wizard, calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["2", "named", "", "0"]))
    monkeypatch.setattr(
        wizard, "_confirm_and_run_job", lambda path, **k: calls.setdefault("ran", []).append(path) or 0
    )

    assert wizard._run_legacy_menu() == 0
    assert calls["ran"] == [job.resolve()]


def test_legacy_menu_pick_existing_job_failure_reports_fail(jobs_dir, monkeypatch, capsys):
    import job_wizard as wizard

    make_job(jobs_dir, "pickme")
    _wizard, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["2", "1", "", "0"]))

    def raise_eof(path, **k):
        raise EOFError("stdin closed")

    monkeypatch.setattr(wizard, "_confirm_and_run_job", raise_eof)

    assert wizard._run_legacy_menu() == 0
    assert "[FAIL]" in capsys.readouterr().out


def test_legacy_menu_empty_job_list_skips_selection(jobs_dir, monkeypatch, capsys):
    import job_wizard as wizard

    _wizard, calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["2", "", "0"]))
    monkeypatch.setattr(wizard, "_confirm_and_run_job", lambda *a, **k: calls.setdefault("ran", []).append(a))

    assert wizard._run_legacy_menu() == 0
    assert "ran" not in calls


def test_legacy_menu_out_of_range_number_returns_to_menu(jobs_dir, monkeypatch, capsys):
    import job_wizard as wizard

    make_job(jobs_dir)
    _wizard, calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["2", "99", "", "0"]))
    monkeypatch.setattr(wizard, "_confirm_and_run_job", lambda *a, **k: calls.setdefault("ran", []).append(a))

    assert wizard._run_legacy_menu() == 0
    assert "ran" not in calls
    # _pick_job_from_list：越界编号打印"无效编号"；非编号走 resolve_job_arg
    # 的"找不到 job 配置"文案。两种无效输入都不再静默。
    assert "无效编号" in capsys.readouterr().out


def test_legacy_menu_reuse_last_job_dispatches_confirm(jobs_dir, monkeypatch):
    import job_wizard as wizard

    job = make_job(jobs_dir, "last_one")
    (jobs_dir / ".last_job").write_text(f"{job}\n", encoding="utf-8")
    _wizard, calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["5", "", "0"]))
    monkeypatch.setattr(
        wizard, "_confirm_and_run_job", lambda path, **k: calls.setdefault("ran", []).append(path) or 0
    )

    assert wizard._run_legacy_menu() == 0
    assert calls["ran"] == [job.resolve()]


def test_legacy_menu_reuse_last_job_failure_reports_fail(jobs_dir, monkeypatch, capsys):
    import job_wizard as wizard

    job = make_job(jobs_dir, "last_one")
    (jobs_dir / ".last_job").write_text(f"{job}\n", encoding="utf-8")
    _wizard, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["5", "", "0"]))

    def raise_eof(path, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(wizard, "_confirm_and_run_job", raise_eof)

    assert wizard._run_legacy_menu() == 0
    assert "[FAIL]" in capsys.readouterr().out


def test_legacy_menu_reuse_without_last_job_prints_hint(jobs_dir, monkeypatch, capsys):
    wizard, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["5", "", "0"]))

    assert wizard._run_legacy_menu() == 0
    assert "没有上次配置" in capsys.readouterr().out


@pytest.mark.parametrize("download_rc,expect_rc_line", [(2, True), (0, False)])
def test_legacy_menu_download_reports_nonzero_rc_then_returns(
    jobs_dir, monkeypatch, capsys, download_rc: int, expect_rc_line: bool
):
    import job_wizard as wizard

    _wizard, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["3", "", "0"]))
    monkeypatch.setattr(wizard, "_menu_download_and_continue", lambda: download_rc)

    assert wizard._run_legacy_menu() == 0
    out = capsys.readouterr().out
    if expect_rc_line:
        assert "(下载/后续步骤退出码 2)" in out
    else:
        assert "退出码" not in out


def test_legacy_menu_download_eof_cancelled_prints_message(jobs_dir, monkeypatch, capsys):
    import job_wizard as wizard

    _wizard, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["3", "", "0"]))

    def raise_eof():
        raise EOFError

    monkeypatch.setattr(wizard, "_menu_download_and_continue", raise_eof)

    assert wizard._run_legacy_menu() == 0
    assert "已取消" in capsys.readouterr().out


def test_legacy_menu_list_and_doctor_dispatch_pipeline_with_doctor_flag(
    jobs_dir, monkeypatch, capsys
):
    import job_wizard as wizard

    make_job(jobs_dir)
    _wizard, calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["4", "", "6", "", "0"]))
    monkeypatch.setattr(
        wizard, "_run_pipeline", lambda *args: calls.setdefault("pipeline", []).append(args) or 0
    )

    assert wizard._run_legacy_menu() == 0
    assert ("--doctor",) in calls["pipeline"]
    assert "doctor 退出码" not in capsys.readouterr().out


def test_legacy_menu_doctor_failure_reports_exit_code(jobs_dir, monkeypatch, capsys):
    import job_wizard as wizard

    _wizard, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["6", "", "0"]))
    monkeypatch.setattr(wizard, "_run_pipeline", lambda *args: 3)

    assert wizard._run_legacy_menu() == 0
    assert "(doctor 退出码 3)" in capsys.readouterr().out


def test_legacy_menu_invalid_choice_reprompts(jobs_dir, monkeypatch, capsys):
    wizard, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["7", "0"]))

    assert wizard._run_legacy_menu() == 0
    assert "无效选择" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _run_tools_menu: tools submenu dispatch
# ---------------------------------------------------------------------------


def test_tools_menu_zero_returns_to_main_menu_without_legacy(jobs_dir, monkeypatch):

    wizard_obj, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["0"]))
    visited: list[bool] = []
    monkeypatch.setattr(wizard_obj, "_run_legacy_menu", lambda: visited.append(True) or 0)

    wizard_obj._run_tools_menu(jobs_dir)
    assert visited == []


def test_tools_menu_dispatches_list_doctor_download_and_legacy(jobs_dir, monkeypatch):

    wizard_obj, calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["1", "2", "3", "4", "0"]))
    downloads: list[bool] = []
    legacy: list[bool] = []
    monkeypatch.setattr(wizard_obj, "_menu_download_and_continue", lambda: downloads.append(True) or 0)
    monkeypatch.setattr(wizard_obj, "_run_legacy_menu", lambda: legacy.append(True) or 0)

    wizard_obj._run_tools_menu(jobs_dir)

    assert ("--doctor",) in calls["pipeline"]
    assert downloads == [True]
    assert legacy == [True]


def test_tools_menu_download_eof_cancelled(jobs_dir, monkeypatch, capsys):

    wizard_obj, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["3", "0"]))

    def raise_cancel():
        raise KeyboardInterrupt

    monkeypatch.setattr(wizard_obj, "_menu_download_and_continue", raise_cancel)

    wizard_obj._run_tools_menu(jobs_dir)
    assert "已取消。" in capsys.readouterr().out


def test_tools_menu_invalid_choice_reprompts(jobs_dir, monkeypatch, capsys):
    wizard_obj, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["x", "0"]))

    wizard_obj._run_tools_menu(jobs_dir)
    assert "无效选择。" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _choose_existing_job: pick + run an existing job
# ---------------------------------------------------------------------------


def test_choose_existing_job_without_history_returns_zero(jobs_dir, monkeypatch):

    prompts: list[tuple[str, str | None]] = []
    wizard_obj, _calls = patch_wizard(
        jobs_dir, monkeypatch, answers=iter([]), prompt_recorder=prompts
    )

    assert wizard_obj._choose_existing_job(jobs_dir) == 0
    assert prompts == []


def test_choose_existing_job_by_number_runs_selected_job(jobs_dir, monkeypatch):

    job = make_job(jobs_dir, "chosen")
    wizard_obj, calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["1"]))
    monkeypatch.setattr(
        wizard_obj, "_confirm_and_run_job", lambda path, **k: calls.setdefault("ran", []).append(path) or 7
    )

    assert wizard_obj._choose_existing_job(jobs_dir) == 7
    assert calls["ran"] == [job]


def test_choose_existing_job_by_name_resolves_job_file(jobs_dir, monkeypatch):

    job = make_job(jobs_dir, "byname")
    wizard_obj, calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["byname"]))
    monkeypatch.setattr(
        wizard_obj, "_confirm_and_run_job", lambda path, **k: calls.setdefault("ran", []).append(path) or 0
    )

    assert wizard_obj._choose_existing_job(jobs_dir) == 0
    assert calls["ran"] == [job.resolve()]


def test_choose_existing_job_unknown_name_returns_one(jobs_dir, monkeypatch, capsys):

    make_job(jobs_dir, "other")
    wizard_obj, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["ghost"]))

    assert wizard_obj._choose_existing_job(jobs_dir) == 1
    assert "无效选择。" in capsys.readouterr().out


def test_choose_existing_job_aborted_run_reports_fail_and_returns_one(
    jobs_dir, monkeypatch, capsys
):

    make_job(jobs_dir, "boom")
    wizard_obj, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["1"]))

    def raise_eof(path, **k):
        raise EOFError("stdin closed")

    monkeypatch.setattr(wizard_obj, "_confirm_and_run_job", raise_eof)

    assert wizard_obj._choose_existing_job(jobs_dir) == 1
    assert "[FAIL]" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main(): command routing
# ---------------------------------------------------------------------------


def test_main_without_args_or_help_prints_usage(capsys):
    import job_wizard as wizard

    assert wizard.main([]) == 0
    assert wizard.main(["-h"]) == 0
    assert wizard.main(["help"]) == 0
    out = capsys.readouterr().out
    assert out.count("用法:") == 3
    assert "resolve <名称>" in out


def test_main_unknown_command_returns_two_with_stderr_hint(capsys):
    import job_wizard as wizard

    assert wizard.main(["bogus"]) == 2
    assert "未知命令: bogus" in capsys.readouterr().err


def test_main_run_without_name_falls_through_to_unknown(capsys):
    import job_wizard as wizard

    assert wizard.main(["run"]) == 2
    assert "未知命令: run" in capsys.readouterr().err


def test_main_routes_menu_new_quick_list_and_drop(monkeypatch):
    import job_wizard as wizard

    seen: list[str] = []
    monkeypatch.setattr(wizard, "run_menu", lambda: seen.append("menu") or 0)
    monkeypatch.setattr(wizard, "run_quick_start", lambda: seen.append("quick") or 0)
    monkeypatch.setattr(wizard, "run_list_jobs", lambda: seen.append("list") or 0)
    monkeypatch.setattr(wizard, "run_drag_drop", lambda args: seen.append(f"drop:{list(args)}") or 0)
    monkeypatch.setattr(
        wizard, "run_job_wizard", lambda *, name=None, **k: seen.append(f"new:{name}") or Path("x.yaml")
    )

    assert wizard.main(["menu"]) == 0
    assert wizard.main(["new", "style"]) == 0
    assert wizard.main(["init"]) == 0
    assert wizard.main(["quick"]) == 0
    assert wizard.main(["list"]) == 0
    assert wizard.main(["drop", "a.mp4"]) == 0
    assert seen == ["menu", "new:style", "new:None", "quick", "list", "drop:['a.mp4']"]


def test_main_new_returns_one_when_wizard_cancelled(monkeypatch):
    import job_wizard as wizard

    monkeypatch.setattr(wizard, "run_job_wizard", lambda *, name=None, **k: None)
    assert wizard.main(["new"]) == 1


def test_main_run_forwards_extra_cli_to_confirm_and_run(monkeypatch):
    import job_wizard as wizard

    resolved = Path("jobs/ok.yaml")
    monkeypatch.setattr(wizard, "resolve_job_arg", lambda name: resolved)
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        wizard,
        "_confirm_and_run_job",
        lambda path, extra_cli=None: seen.update(path=path, extra_cli=extra_cli) or 5,
    )

    assert wizard.main(["run", "ok.yaml", "--mode", "preview"]) == 5
    assert seen == {"path": resolved, "extra_cli": ["--mode", "preview"]}


def test_main_run_with_unresolvable_name_returns_one(monkeypatch, capsys):
    import job_wizard as wizard

    def raise_value_error(name):
        raise ValueError(f"找不到 job 配置: {name}")

    monkeypatch.setattr(wizard, "resolve_job_arg", raise_value_error)

    assert wizard.main(["run", "ghost"]) == 1
    assert "找不到 job 配置" in capsys.readouterr().err


def test_main_resolve_prints_resolved_path_and_returns_zero(monkeypatch, capsys):
    import job_wizard as wizard

    resolved = Path("jobs/ok.yaml")
    monkeypatch.setattr(wizard, "resolve_job_arg", lambda name: resolved)

    assert wizard.main(["resolve", "ok.yaml"]) == 0
    assert str(resolved) in capsys.readouterr().out


def test_main_resolve_failure_returns_one(monkeypatch, capsys):
    import job_wizard as wizard

    def raise_value_error(name):
        raise ValueError("找不到 job 配置")

    monkeypatch.setattr(wizard, "resolve_job_arg", raise_value_error)

    assert wizard.main(["resolve", "ghost"]) == 1
    assert "找不到 job 配置" in capsys.readouterr().err


def test_main_without_argv_reads_sys_argv(monkeypatch):
    """main() 无参时读 sys.argv[1:] —— 进程入口的默认路由。"""
    import sys

    import job_wizard as wizard

    seen: list[str] = []
    monkeypatch.setattr(wizard, "run_menu", lambda: seen.append("menu") or 0)
    monkeypatch.setattr(sys, "argv", ["job_wizard.py", "menu"])

    assert wizard.main() == 0
    assert seen == ["menu"]


# ---------------------------------------------------------------------------
# run_drag_drop: dropped-file routing next to the menu entries
# ---------------------------------------------------------------------------


def test_run_drag_drop_job_yaml_confirms_with_remaining_args(jobs_dir, monkeypatch):
    import job_wizard as wizard

    job = make_job(jobs_dir, "dropped")
    monkeypatch.chdir(jobs_dir)
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        wizard,
        "_confirm_and_run_job",
        lambda path, extra_cli=None: seen.update(path=path, extra_cli=extra_cli) or 0,
    )

    assert wizard.run_drag_drop([str(job), "--mode", "preview"]) == 0
    assert seen == {"path": job, "extra_cli": ["--mode", "preview"]}


def test_run_drag_drop_video_and_html_previews_original(jobs_dir, monkeypatch):
    import job_wizard as wizard

    video = jobs_dir / "clip.mp4"
    chat = jobs_dir / "chat.html"
    video.write_bytes(b"video")
    chat.write_text("<html></html>", encoding="utf-8")
    monkeypatch.chdir(jobs_dir)
    forwarded: list[tuple[str, ...]] = []
    monkeypatch.setattr(wizard, "_run_pipeline", lambda *args: forwarded.append(args) or 0)

    assert wizard.run_drag_drop([str(video), str(chat)]) == 0
    assert forwarded == [
        (str(video), str(chat), "--mode", "preview", "--render-original", "--preview-clip", "10", "--yes")
    ]


def test_run_drag_drop_unresolvable_argument_fails(jobs_dir, monkeypatch, capsys):
    import job_wizard as wizard

    monkeypatch.chdir(jobs_dir)

    assert wizard.run_drag_drop(["ghost.yaml"]) == 1
    assert "[FAIL]" in capsys.readouterr().out


def test_run_drag_drop_job_name_argument_runs_resolved_job(jobs_dir, monkeypatch):
    import job_wizard as wizard

    job = make_job(jobs_dir, "byref")
    monkeypatch.chdir(jobs_dir.parent)  # default_jobs_dir() -> <cwd>/jobs == jobs_dir
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        wizard,
        "_confirm_and_run_job",
        lambda path, extra_cli=None: seen.update(path=path, extra_cli=extra_cli) or 0,
    )

    assert wizard.run_drag_drop(["byref", "--flag"]) == 0
    assert seen == {"path": job.resolve(), "extra_cli": ["--flag"]}


def test_run_drag_drop_video_only_prompts_for_chat_then_previews(jobs_dir, monkeypatch):
    import job_wizard as wizard

    video = jobs_dir / "solo.mp4"
    video.write_bytes(b"v")
    chat = jobs_dir / "other.html"
    chat.write_text("<html></html>", encoding="utf-8")
    monkeypatch.chdir(jobs_dir)
    _wizard, calls = patch_wizard(jobs_dir, monkeypatch, answers=iter([]))
    monkeypatch.setattr(wizard, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(wizard, "_prompt_path", lambda *a, **k: str(chat))

    assert wizard.run_drag_drop([str(video)]) == 0
    assert calls["pipeline"] == [
        (str(video), str(chat), "--mode", "preview", "--render-original", "--preview-clip", "10", "--yes")
    ]


def test_run_drag_drop_video_only_aborted_chat_prompt_fails(jobs_dir, monkeypatch, capsys):
    import job_wizard as wizard

    video = jobs_dir / "solo.mp4"
    video.write_bytes(b"v")
    monkeypatch.chdir(jobs_dir)
    _wizard, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter([]))
    monkeypatch.setattr(wizard, "_stdin_is_interactive", lambda: True)

    def raise_not_found(*_a, **_k):
        raise FileNotFoundError("文件不存在")

    monkeypatch.setattr(wizard, "_prompt_path", raise_not_found)

    assert wizard.run_drag_drop([str(video)]) == 1
    assert "[FAIL]" in capsys.readouterr().out


def test_run_drag_drop_no_arguments_falls_back_to_menu(jobs_dir, monkeypatch):
    import job_wizard as wizard

    entered: list[bool] = []
    monkeypatch.setattr(wizard, "run_menu", lambda: entered.append(True) or 0)

    assert wizard.run_drag_drop([]) == 0
    assert entered == [True]


# ---------------------------------------------------------------------------
# small helpers used by the menus (prompt primitives, session plumbing)
# ---------------------------------------------------------------------------


def test_prompt_secret_falls_back_to_input_without_tty(monkeypatch):
    """getpass 失败（无 TTY）时回退 input()，保证非交互环境可测试。"""
    import job_wizard as wizard

    def raise_os_error(*_a, **_k):
        raise OSError("no tty")

    monkeypatch.setattr(wizard.getpass, "getpass", raise_os_error)
    monkeypatch.setattr(builtins, "input", lambda *_a, **_k: "typed-secret")

    assert wizard._prompt_secret("API Key") == "typed-secret"


def test_prompt_returns_default_on_eof(monkeypatch):
    """_prompt 在有默认值时吞掉 EOF（非交互安全），无默认值时上抛。"""
    import job_wizard as wizard

    def raise_eof(*_a, **_k):
        raise EOFError

    monkeypatch.setattr(builtins, "input", raise_eof)

    assert wizard._prompt("问题", "fallback") == "fallback"
    with pytest.raises(EOFError):
        wizard._prompt("问题", None)


def test_confirm_and_run_cancelled_non_interactive_maps_to_failure(jobs_dir, monkeypatch, capsys):
    """取消/缺文件在非交互环境下必须按失败退出（自动化不得把取消当成功）。"""

    job = make_job(jobs_dir, "needsmedia")
    wizard_obj, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter([]))
    monkeypatch.setattr(wizard_obj, "summarize_job", lambda *_a: "needsmedia — 预览")
    monkeypatch.setattr(wizard_obj, "load_job_file", lambda *_a: {"mode": "preview"})

    def no_media(job, **k):
        return None

    monkeypatch.setattr(wizard_obj, "_prompt_session_media", no_media)

    assert wizard_obj._confirm_and_run_job(job) == 1
    assert "已取消（非交互：按失败退出）" in capsys.readouterr().out


def test_split_bare_media_paths_extracts_video_and_chat_pairs(tmp_path):
    import job_wizard as wizard

    video = tmp_path / "video.mp4"
    chat = tmp_path / "chat.html"
    video.write_bytes(b"v")
    chat.write_text("<html></html>", encoding="utf-8")

    overrides, remaining = wizard._split_bare_media_paths(
        ["--output", "out.mp4", str(video), str(chat), "--workdir=work", "flag"]
    )
    assert overrides == {"video": str(video), "chat_html": str(chat)}
    assert remaining == ["--output", "out.mp4", "--workdir=work", "flag"]


def test_apply_extra_cli_overrides_empty_session_is_safe():
    import job_wizard as wizard

    assert wizard._apply_extra_cli_path_overrides(None, None) == {}
    assert wizard._apply_extra_cli_path_overrides({}, []) == {}
    assert wizard._apply_extra_cli_path_overrides(None, ["--other"]) == {}


def test_resolve_clean_root_prefers_workdir_temp_over_video_parent(tmp_path):
    import job_wizard as wizard

    workdir = tmp_path / "wd"
    (workdir / "temp").mkdir(parents=True)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")

    assert wizard._resolve_clean_root({"workdir": str(workdir)}, {}) == workdir / "temp"
    assert wizard._resolve_clean_root({}, {"video": str(video)}) == tmp_path
    assert wizard._resolve_clean_root({}, {}) is None


def test_menu_download_without_cli_binary_reports_missing_tool(jobs_dir, monkeypatch, capsys):
    """未安装 TwitchDownloaderCLI 时给出安装引导并返回失败码。"""
    import twitch_download as download

    wizard_obj, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter([]))
    monkeypatch.setattr(download, "find_twitchdownloader_cli", lambda: None)

    assert wizard_obj._menu_download_and_continue() == 1
    out = capsys.readouterr().out
    assert "[FAIL] 未找到 TwitchDownloaderCLI" in out
    assert "--offer-td-cli" in out


def test_menu_download_eof_before_questions_is_cancelled(jobs_dir, monkeypatch, capsys):
    import twitch_download as download

    wizard_obj, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter([]))
    monkeypatch.setattr(
        download, "find_twitchdownloader_cli", lambda: SimpleNamespace(name="td.exe")
    )

    def raise_eof(*_a, **_k):
        raise EOFError

    monkeypatch.setattr(wizard_obj, "_prompt", raise_eof)

    assert wizard_obj._menu_download_and_continue() == 0
    assert "已取消" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# quick start / offline demo / pipeline launcher / download next-step dispatch
# ---------------------------------------------------------------------------


def test_run_quick_start_confirms_created_job(jobs_dir, monkeypatch):
    import job_wizard as wizard
    import ux_setup

    created = jobs_dir / "quick.yaml"
    _wizard, calls = patch_wizard(jobs_dir, monkeypatch, answers=iter([]))
    monkeypatch.setattr(ux_setup, "run_init", lambda *a, **k: 0)
    monkeypatch.setattr(wizard, "run_job_wizard", lambda *a, **k: created)
    monkeypatch.setattr(
        wizard, "_confirm_and_run_job", lambda path, **k: calls.setdefault("ran", []).append(path) or 0
    )

    assert wizard.run_quick_start() == 0
    assert calls["ran"] == [created]


def test_run_quick_start_returns_failure_when_init_fails(jobs_dir, monkeypatch):
    import job_wizard as wizard
    import ux_setup

    patch_wizard(jobs_dir, monkeypatch, answers=iter([]))
    monkeypatch.setattr(ux_setup, "run_init", lambda *a, **k: 1)

    assert wizard.run_quick_start() == 1


def test_run_quick_start_wizard_cancelled_returns_zero(jobs_dir, monkeypatch):
    """run_job_wizard 取消/仅保存（返回 None）时 quick start 也按成功收尾。"""
    import job_wizard as wizard
    import ux_setup

    patch_wizard(jobs_dir, monkeypatch, answers=iter([]))
    monkeypatch.setattr(ux_setup, "run_init", lambda *a, **k: 0)
    monkeypatch.setattr(wizard, "run_job_wizard", lambda *a, **k: None)

    assert wizard.run_quick_start() == 0


def test_run_offline_demo_launches_quick_demo_subprocess(monkeypatch):
    import job_wizard as wizard

    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=4)

    monkeypatch.setattr(wizard.subprocess, "run", fake_run)

    assert wizard._run_offline_demo() == 4
    assert seen["cmd"][1].endswith("quick_demo.py")


def test_run_pipeline_reports_launch_failure(monkeypatch, capsys):
    import job_wizard as wizard

    def raise_os(cmd):
        raise OSError("spawn blocked")

    monkeypatch.setattr(wizard.subprocess, "run", raise_os)

    assert wizard._run_pipeline("--doctor") == 1
    assert "[FAIL]" in capsys.readouterr().out


def _download_answers(next_step: str) -> list[str]:
    """Answers for the single-segment download flow up to the next-step choice."""
    return ["2819850140", "auto", "1080p60", "1", "", "", "Weird", "weird", "weird", "", next_step]


def _patch_download(monkeypatch, jobs_dir: Path, *, download_assets=None):
    import twitch_download as download

    video = jobs_dir / "downloaded.mp4"
    chat = jobs_dir / "downloaded.html"
    video.write_bytes(b"video")
    chat.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(
        download, "find_twitchdownloader_cli", lambda: jobs_dir / "TwitchDownloaderCLI.exe"
    )
    if download_assets is None:
        def download_assets(source, **kwargs):
            return SimpleNamespace(video_path=video, chat_html_path=chat)

    monkeypatch.setattr(download, "download_assets", download_assets)
    return video, chat


def test_menu_download_invalid_options_fall_back_to_defaults_then_end(jobs_dir, monkeypatch):

    wizard_obj, calls = patch_wizard(
        jobs_dir, monkeypatch, answers=iter(_download_answers("0"))
    )
    _patch_download(monkeypatch, jobs_dir)

    assert wizard_obj._menu_download_and_continue() == 0
    assert calls.get("pipeline") is None  # choice 0 ends without running the pipeline


def test_menu_download_next_step_exports_manual_translation_table(jobs_dir, monkeypatch):

    wizard_obj, calls = patch_wizard(
        jobs_dir, monkeypatch, answers=iter(_download_answers("2"))
    )
    video, chat = _patch_download(monkeypatch, jobs_dir)

    assert wizard_obj._menu_download_and_continue() == 0
    assert calls["pipeline"] == [(str(video), str(chat), "--manual-translation", "--yes")]


def test_menu_download_next_step_runs_existing_job_with_downloaded_media(jobs_dir, monkeypatch):

    wizard_obj, calls = patch_wizard(
        jobs_dir, monkeypatch, answers=iter(_download_answers("4") + ["1"])
    )
    video, chat = _patch_download(monkeypatch, jobs_dir)
    job = make_job(jobs_dir, "restyle")
    monkeypatch.setattr(
        wizard_obj,
        "_confirm_and_run_job",
        lambda path, extra_cli=None: calls.setdefault("ran", []).append((path, extra_cli)) or 0,
    )
    # print_job_list 以 stub 替代：现实现用 root= 关键字调用它（形参是 jobs_dir），
    # 选 [4] 必然 TypeError —— 已作为实现 bug 上报，未改实现；这里只验证分发行为。
    monkeypatch.setattr(wizard_obj, "print_job_list", lambda *a, **k: [job])

    assert wizard_obj._menu_download_and_continue() == 0
    assert calls["ran"] == [(job, [str(video), str(chat)])]


def test_menu_download_next_step_four_lists_jobs_then_prompts(jobs_dir, monkeypatch):
    """下载后续 [4] 应列出 jobs 目录并进入选择，而不是 TypeError(曾用 root= 调 jobs_dir 形参)。"""

    wizard_obj, _calls = patch_wizard(
        jobs_dir, monkeypatch, answers=iter(_download_answers("4"))
    )
    _patch_download(monkeypatch, jobs_dir)

    import job_wizard as wizard

    seen: dict[str, object] = {}

    def fake_print_job_list(jobs_dir_arg, *args, **kwargs):
        seen["dir"] = jobs_dir_arg
        seen["kwargs"] = kwargs
        return []

    monkeypatch.setattr(wizard, "print_job_list", fake_print_job_list)

    assert wizard_obj._menu_download_and_continue() == 0
    assert seen["dir"] == jobs_dir
    assert "root" not in seen["kwargs"]


def test_menu_download_provider_error_returns_two(jobs_dir, monkeypatch, capsys):
    import twitch_download as download

    wizard_obj, _calls = patch_wizard(
        jobs_dir, monkeypatch, answers=iter(_download_answers("0"))
    )

    def fail_download(source, **kwargs):
        raise download.TwitchDownloadError("provider offline")

    _patch_download(monkeypatch, jobs_dir, download_assets=fail_download)

    assert wizard_obj._menu_download_and_continue() == 2
    assert "[FAIL] provider offline" in capsys.readouterr().out


def test_menu_download_unexpected_error_returns_one(jobs_dir, monkeypatch, capsys):

    wizard_obj, _calls = patch_wizard(
        jobs_dir, monkeypatch, answers=iter(_download_answers("0"))
    )

    def fail_download(source, **kwargs):
        raise RuntimeError("disk exploded")

    _patch_download(monkeypatch, jobs_dir, download_assets=fail_download)

    assert wizard_obj._menu_download_and_continue() == 1
    assert "[FAIL] 下载异常: disk exploded" in capsys.readouterr().out


def test_menu_download_multi_segment_dispatches_multi_download(jobs_dir, monkeypatch):
    import twitch_download as download

    wizard_obj, calls = patch_wizard(
        jobs_dir,
        monkeypatch,
        answers=iter(
            [
                "2819850140",        # URL
                "vod",               # 类型
                "1080p60",           # 画质
                "2",                 # 裁切模式: 多段
                "0:00:00 0:00:05",   # 第 1 段
                "",                  # 结束输入
                "y",                 # 确认开始下载
                "",                  # 切除时间段（跳过）
                "60",                # 合并帧率
                "Exact",             # 裁切模式
                "fast",              # 媒体检查
                "audio",             # 修复策略
                "",                  # 下载目录
                "0",                 # 下一步: 结束
            ]
        ),
    )
    video, chat = _patch_download(monkeypatch, jobs_dir)
    multi_seen: dict[str, object] = {}

    def fake_multi(source, segments, **kwargs):
        multi_seen.update(source=source, segments=segments, kwargs=kwargs)
        return SimpleNamespace(video_path=video, chat_html_path=chat)

    monkeypatch.setattr(download, "download_assets_multi", fake_multi)

    assert wizard_obj._menu_download_and_continue() == 0
    assert multi_seen["source"] == "2819850140"
    assert multi_seen["segments"] == [("0:00:00", "0:00:05")]
    assert multi_seen["kwargs"]["output_fps"] == 60.0
    assert multi_seen["kwargs"]["trim_mode"] == "Exact"
    assert calls.get("pipeline") is None


# ---------------------------------------------------------------------------
# _confirm_and_run_job: the run flow every menu item lands on
# ---------------------------------------------------------------------------


def test_confirm_and_run_job_builds_pipeline_command_from_session(jobs_dir, monkeypatch):

    job = make_job(jobs_dir, "runme")
    video = jobs_dir / "v.mp4"
    video.write_bytes(b"v")
    chat = jobs_dir / "c.html"
    chat.write_text("<html></html>", encoding="utf-8")
    wizard_obj, calls = patch_wizard(jobs_dir, monkeypatch, answers=iter([""]))
    monkeypatch.setattr(
        wizard_obj,
        "_prompt_session_media",
        lambda job_fields, **k: {"video": str(video), "chat_html": str(chat)},
    )

    assert wizard_obj._confirm_and_run_job(job) == 0
    assert calls["pipeline"] == [("--job", str(job), str(video), str(chat))]


def test_confirm_and_run_job_edit_branch_reasks_session(jobs_dir, monkeypatch):

    job = make_job(jobs_dir, "editable")
    video = jobs_dir / "v.mp4"
    video.write_bytes(b"v")
    chat = jobs_dir / "c.html"
    chat.write_text("<html></html>", encoding="utf-8")
    wizard_obj, calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["e", ""]))
    monkeypatch.setattr(
        wizard_obj,
        "_prompt_session_media",
        lambda job_fields, **k: {"video": str(video), "chat_html": str(chat)},
    )
    opened: list[Path] = []
    monkeypatch.setattr(wizard_obj, "_open_editor", lambda path: opened.append(path))

    assert wizard_obj._confirm_and_run_job(job) == 0
    assert opened == [job]
    assert calls["pipeline"] == [("--job", str(job), str(video), str(chat))]


def test_report_run_success_lists_output_and_skips_opening(jobs_dir, monkeypatch, capsys):

    wizard_obj, _calls = patch_wizard(jobs_dir, monkeypatch, answers=iter(["n"]))
    out_file = jobs_dir / "v_chat.mp4"
    out_file.write_bytes(b"x")
    job_path = jobs_dir / "j.yaml"
    job_path.write_text("mode: preview\n", encoding="utf-8")

    wizard_obj._report_run_success(job_path, session={"output": str(out_file)})

    out = capsys.readouterr().out
    assert "[OK] 任务结束。" in out
    assert "v_chat.mp4" in out
