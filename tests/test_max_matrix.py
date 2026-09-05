#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Max-suite coverage for long-term development.

Default `run_tests.py` excludes `@pytest.mark.max` / `slow`.
Run: python scripts/run_tests.py --max
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

pytestmark = pytest.mark.max


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SCRIPTS)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["_TWITCH_TRANSPARENT_TEST_MODE"] = "1"
    for k in (
        "OPENAI_COMPAT_API_KEY",
        "OPENAI_COMPAT_BASE_URL",
        "OPENAI_COMPAT_MODEL",
        "AGNES_API_KEY",
        "AGNES_BASE_URL",
        "AGNES_MODEL",
    ):
        env.pop(k, None)
    return env


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_env(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Packaging / module surface
# ---------------------------------------------------------------------------


def test_all_scripts_py_compile():
    files = sorted(SCRIPTS.glob("*.py"))
    assert files, "no scripts"
    r = _run([sys.executable, "-m", "py_compile", *[str(p) for p in files]])
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def test_pyproject_py_modules_match_scripts():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    # crude extract of py-modules list
    m = re.search(r"py-modules\s*=\s*\[(.*?)\]", text, re.S)
    assert m, "py-modules missing"
    mods = re.findall(r'"([^"]+)"', m.group(1))
    assert mods
    for name in mods:
        assert (SCRIPTS / f"{name}.py").is_file(), f"py-module {name} missing on disk"
    assert {p.stem for p in SCRIPTS.glob("*.py")} == set(mods), (
        "scripts/ 下存在未注册 py-modules 的新模块"
    )

    runner_tree = ast.parse((SCRIPTS / "run_tests.py").read_text(encoding="utf-8"))
    compile_node = next(
        node.value
        for node in runner_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "COMPILE_SCRIPTS"
            for target in node.targets
        )
    )
    compile_modules = [Path(name).stem for name in ast.literal_eval(compile_node)]
    assert set(compile_modules) == set(mods), (
        "compile/import smoke list must match packaged py-modules"
    )
    # critical UX modules must be packaged
    for must in ("job_config", "job_wizard", "ux_setup", "render_cn_chat", "twitch_chat_burn", "twitch_download"):
        assert must in mods


def test_wheel_data_files_include_env_template():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    data_files = text.split("[tool.setuptools.data-files]", 1)[1].split("\n[", 1)[0]
    assert '"share/twitch-chat-translator-overlay"' in data_files
    assert '".env.example"' in data_files


def test_public_console_scripts_declared():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for entry in ("twitch-chat-overlay", "twitch-chat-burn", "twitch-chat-translate"):
        assert entry in text


def test_run_bat_is_ascii_crlf():
    for name in ("run.bat", "run_cli.bat", "run_tui.bat", "install.bat", "update.bat", "doctor.bat"):
        p = ROOT / name
        if not p.is_file():
            continue
        data = p.read_bytes()
        assert all(b < 128 for b in data), f"{name} must be ASCII-only for GBK cmd safety"
        assert b"\r\n" in data, f"{name} should use CRLF"


def test_launchers_require_runnable_python_310():
    for name in ("run.bat", "run_cli.bat", "run_tui.bat", "install.bat", "update.bat", "doctor.bat", "run.sh", "install.sh", "update.sh", "doctor.sh"):
        text = (ROOT / name).read_text(encoding="ascii" if name.endswith(".bat") else "utf-8")
        assert "sys.version_info>=(3,10)" in text, f"{name} must reject stale or broken Python"
        if name.endswith(".bat"):
            assert "Cannot enter repository directory" in text


def test_run_bat_preserves_original_argument_vector():
    text = (ROOT / "run.bat").read_text(encoding="ascii")
    assert 'run_cli.bat" %*' in text
    cli = (ROOT / "run_cli.bat").read_text(encoding="ascii")
    assert r"scripts\job_wizard.py drop %*" in cli
    assert r"scripts\job_wizard.py quick" in cli
    assert r"scripts\quick_demo.py" in cli
    assert 'set "EXTRA="' not in text
    # C3: run_cli 拦截 cmd 元字符，但 %* 原样转发保持不变。
    assert "findstr /R /C:" in cli
    assert r"echo(%*| findstr" in cli


def test_bat_launchers_reject_cmd_metacharacters():
    """C3: run_cli.bat 与 doctor.bat 都在路由/执行前拦截 [&|^<>()!] 并 exit 1。"""
    for name, anchor in (
        ("run_cli.bat", 'if /I "%~1"=="" goto MENU'),
        ("doctor.bat", r'"%PY%" scripts\render_cn_chat.py --doctor %*'),
    ):
        text = (ROOT / name).read_text(encoding="ascii")
        assert "findstr /R /C:" in text, f"{name} must probe for cmd metacharacters"
        assert 'echo(%*| findstr /R /C:"[&|^<>()!]"' in text
        assert "Arguments contain cmd metacharacters" in text
        assert "exit /b 1" in text
        if name == "run_cli.bat":
            assert text.index("findstr /R /C:") < text.index(anchor), (
                "run_cli.bat must reject metacharacters before argument routing"
            )
        else:
            assert text.index("findstr /R /C:") < text.index(anchor), (
                "doctor.bat must reject metacharacters before invoking --doctor"
            )
        # ASCII+CRLF 仍由 test_run_bat_is_ascii_crlf 覆盖；这里再硬校验一次。
        data = (ROOT / name).read_bytes()
        assert all(b < 128 for b in data), f"{name} must stay ASCII-only"
        assert b"\r\n" in data, f"{name} should use CRLF"


def test_install_and_update_launchers_stop_after_failures():
    install_bat = (ROOT / "install.bat").read_text(encoding="ascii")
    update_bat = (ROOT / "update.bat").read_text(encoding="ascii")
    update_sh = (ROOT / "update.sh").read_text(encoding="utf-8")

    pip_pos = install_bat.index('"%PY%" -m pip install -U pip')
    pull_pos = update_bat.index("git pull --ff-only")
    update_pip_pos = update_bat.index('"%PY%" -m pip install -U pip')
    assert "if errorlevel 1 (" in install_bat[pip_pos : pip_pos + 200]
    assert "if %ERRORLEVEL%" not in install_bat
    assert "if errorlevel 1 (" in update_bat[pull_pos : pull_pos + 300]
    assert "if errorlevel 1 (" in update_bat[update_pip_pos : update_pip_pos + 200]
    assert "if [[ -e .git ]]; then" in update_sh
    assert "if ! git pull --ff-only; then" in update_sh
    assert 'if ! "$PY" -m pip install -U pip; then' in update_sh


# ---------------------------------------------------------------------------
# CLI surface / UX
# ---------------------------------------------------------------------------

def test_install_progress_and_unix_invocations_are_copy_pasteable():
    for name in ("run.sh", "install.sh", "update.sh", "doctor.sh"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "./install.sh" not in text
        assert "./run.sh" not in text
    for name in ("install.bat", "install.sh"):
        text = (ROOT / name).read_text(encoding="ascii" if name.endswith(".bat") else "utf-8")
        for step in range(1, 6):
            assert f"[{step}/5]" in text


def test_pipeline_help_lists_ux_flags():
    r = _run([sys.executable, str(SCRIPTS / "render_cn_chat.py"), "--help"])
    assert r.returncode == 0
    text = (r.stdout or "") + (r.stderr or "")
    for flag in (
        "--init",
        "--init-job",
        "--list-jobs",
        "--job",
        "--mode",
        "--doctor",
        "--clean",
        "--clean-all",
        "--clean-progress",
    ):
        assert flag in text, f"missing {flag}"


def test_burn_help_mentions_no_job_dir_parallel_risk():
    r = _run([sys.executable, str(SCRIPTS / "twitch_chat_burn.py"), "--help"])
    assert r.returncode == 0
    text = (r.stdout or "") + (r.stderr or "")
    assert "--no-job-dir" in text
    assert "--job-dir" in text


def test_job_wizard_menu_chinese_and_exit():
    r = _run(
        [sys.executable, str(SCRIPTS / "job_wizard.py"), "menu"],
        input="0\n",
    )
    assert r.returncode == 0
    out = (r.stdout or "") + (r.stderr or "")
    assert "Twitch" in out
    assert "[0]" in out


def test_wizard_helpers_guess_translation_and_report_paths(tmp_path: Path):
    """Unit checks for wizard UX helpers (purpose-3 / post-run feedback)."""
    import job_wizard as jw

    vid = tmp_path / "stream.mp4"
    vid.write_bytes(b"x")
    tj = tmp_path / "stream_translation.json"
    tj.write_text("{}", encoding="utf-8")
    found = jw._guess_translation_json(vid)
    assert any(p.resolve() == tj.resolve() for p in found)

    # list index after write
    from job_config import write_job_file

    jobs = tmp_path / "jobs"
    jobs.mkdir()
    p = write_job_file(
        jobs / "demo.yaml",
        {
            "video": str(vid),
            "chat_html": str(tmp_path / "c.html"),
            "mode": "preview",
            "render_original": True,
        },
        title="demo",
        overwrite=True,
    )
    (tmp_path / "c.html").write_text("<html></html>", encoding="utf-8")
    idx = jw._list_index_for(p, jobs)
    assert idx == 1


def test_wizard_session_media_soft_cancel_on_bad_path(tmp_path: Path, monkeypatch):
    """Missing media must cancel session, not crash the menu with traceback."""
    import job_wizard as jw

    monkeypatch.setattr(jw, "_stdin_is_interactive", lambda: False)
    answers = iter(["does_not_exist.mp4"])
    monkeypatch.setattr(jw, "_prompt", lambda msg, default=None: next(answers))
    session = jw._prompt_session_media({"mode": "preview", "render_original": True})
    assert session is None


def test_wizard_resolve_clean_root_prefers_workdir_temp(tmp_path: Path):
    import job_wizard as jw

    wd = tmp_path / "work"
    temp = wd / "temp"
    temp.mkdir(parents=True)
    root = jw._resolve_clean_root({"workdir": str(wd)}, {})
    assert root == temp

    # No temp/ → workdir itself
    wd2 = tmp_path / "work2"
    wd2.mkdir()
    root2 = jw._resolve_clean_root({"workdir": str(wd2)}, {})
    assert root2 == wd2


def test_wizard_apply_extra_cli_path_overrides(tmp_path: Path):
    """extra_cli --output/--workdir must win for post-run report/clean paths."""
    import job_wizard as jw

    out_cli = tmp_path / "cli_out.mp4"
    wd_cli = tmp_path / "cli_work"
    wd_cli.mkdir()
    (wd_cli / "temp").mkdir()
    tj_cli = tmp_path / "cli_tj.json"

    session = {
        "output": str(tmp_path / "job_out.mp4"),
        "workdir": str(tmp_path / "job_work"),
        "video": str(tmp_path / "v.mp4"),
    }
    merged = jw._apply_extra_cli_path_overrides(
        session,
        [
            "--preview-clip",
            "10",
            "--output",
            str(out_cli),
            "--workdir",
            str(wd_cli),
            f"--translation-json={tj_cli}",
        ],
    )
    assert merged["output"] == str(out_cli)
    assert merged["workdir"] == str(wd_cli)
    assert merged["translation_json"] == str(tj_cli)
    assert merged["video"] == session["video"]

    # Clean root follows CLI workdir/temp
    root = jw._resolve_clean_root(merged, {"workdir": str(tmp_path / "job_work")})
    assert root == wd_cli / "temp"

    # No extra_cli → session unchanged (copy)
    same = jw._apply_extra_cli_path_overrides(session, None)
    assert same["output"] == session["output"]
    assert same is not session


# ---------------------------------------------------------------------------
# PIPE-O3: 拖放 job.yaml + 裸媒体路径不得导致 argparse "unrecognized arguments"
# ---------------------------------------------------------------------------


def test_wizard_split_bare_media_paths_strips_dropped_media(tmp_path: Path):
    """裸媒体路径按现有后缀约定剥离；--flag 取值与不存在的路径保持原样。"""
    import job_wizard as jw

    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    html = tmp_path / "c.html"
    html.write_text("x", encoding="utf-8")
    out_flag = tmp_path / "out.mp4"
    out_flag.write_bytes(b"x")

    overrides, rest = jw._split_bare_media_paths(
        ["--output", str(out_flag), str(video), str(html), "--yes"]
    )
    assert overrides == {"video": str(video), "chat_html": str(html)}
    assert rest == ["--output", str(out_flag), "--yes"]

    # 不存在的裸媒体路径不劫持（留给管线给出明确报错）
    missing = str(tmp_path / "missing.mp4")
    overrides2, rest2 = jw._split_bare_media_paths([missing])
    assert overrides2 == {}
    assert rest2 == [missing]

    # 空入参
    assert jw._split_bare_media_paths(None) == ({}, [])


def test_wizard_drag_drop_job_with_media_builds_clean_cli(tmp_path: Path, monkeypatch):
    """拖放 job.yaml + 视频 + HTML：媒体进 session，CLI 只含一份位置参数。"""
    import job_wizard as jw

    job = tmp_path / "style.yaml"
    job.write_text("mode: preview\nrender_original: true\n", encoding="utf-8")
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    chat = tmp_path / "chat.html"
    chat.write_text("<html></html>", encoding="utf-8")

    captured: list[tuple[str, ...]] = []
    monkeypatch.setattr(jw, "_run_pipeline", lambda *args: captured.append(args) or 0)
    monkeypatch.setattr(jw, "save_last_job", lambda _path, *_a, **_k: None)
    monkeypatch.setattr(jw, "_prompt", lambda _msg, _default=None: "")

    rc = jw.run_drag_drop([str(job), str(video), str(chat)])
    assert rc == 0
    assert captured, "管线未被调用"
    cmd = captured[0]
    # 位置参数 video/chat 只出现一次（多出来会被 argparse 当 unrecognized arguments）
    assert cmd.count(str(video)) == 1, cmd
    assert cmd.count(str(chat)) == 1, cmd
    assert cmd[:3] == ("--job", str(job), str(video)), cmd
    assert str(chat) in cmd


def test_wizard_maybe_clean_temp_after_run(tmp_path: Path, monkeypatch):
    import job_wizard as jw
    from process_util import make_job_dir

    wd = tmp_path / "wd"
    temp = wd / "temp"
    temp.mkdir(parents=True)
    job = make_job_dir(temp, prefix="job_")
    (job / "frame.bin").write_bytes(b"x" * 50)
    partial = temp / "x.partial.mp4"
    partial.write_bytes(b"zz")

    monkeypatch.setattr(jw, "_prompt", lambda msg, default=None: "y")
    jw._maybe_clean_temp_after_run({"workdir": str(wd)}, {})
    # Safer default: partials only (not every finished job_*).
    assert job.is_dir(), "wizard post-run clean must not wipe job dirs by default"
    assert not partial.exists()

    # No workdir → refuse bulk clean next to the video.
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x")
    sibling = make_job_dir(tmp_path, prefix="job_")
    (sibling / "keep.bin").write_bytes(b"y")
    jw._maybe_clean_temp_after_run({"video": str(vid)}, {})
    assert sibling.is_dir()


def test_discover_presets_scans_profiles_dir():
    from common_utils import (
        discover_presets,
        format_preset_menu_lines,
        pick_preset_from_menu,
    )

    layouts = discover_presets("layout")
    renders = discover_presets("render")
    assert layouts, "expected layout_* presets under profiles/"
    assert renders, "expected render_* presets under profiles/"
    shorts_l = {e["short"] for e in layouts}
    shorts_r = {e["short"] for e in renders}
    assert "default" in shorts_l or "compact" in shorts_l
    assert "fast" in shorts_r or "default" in shorts_r
    # menu lines numbered
    lines = format_preset_menu_lines(layouts)
    assert lines[0].startswith("   [1]")
    assert any("不写" in x for x in lines)
    # pick by index / short name / 0
    assert pick_preset_from_menu(layouts, "1") == layouts[0]["short"]
    if "compact" in shorts_l:
        assert pick_preset_from_menu(layouts, "compact") == "compact"
    assert pick_preset_from_menu(layouts, "0") is None


def test_discover_presets_reads_custom_yaml(tmp_path: Path, monkeypatch):
    """Dropping a new layout_*.yaml into profiles/ should appear in the menu."""
    from common_utils import discover_presets

    # Use a fake profiles dir via monkeypatch of profiles_search_dirs
    prof = tmp_path / "profiles"
    prof.mkdir()
    (prof / "layout_wide.yaml").write_text(
        "# 宽屏弹幕 — 适合 21:9\n"
        "name: layout_wide\n"
        "label: Wide\n"
        "description: Wide chat box\n"
        "layout:\n  x: 10\n  y: 10\n  width: 800\n  height: 300\n",
        encoding="utf-8",
    )
    (prof / "render_turbo.yaml").write_text(
        "# 极速编码 — 仅自测\n"
        "name: render_turbo\n"
        "render:\n  encoder: x264\n  crf: 28\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "common_utils.profiles_search_dirs",
        lambda: [prof],
    )
    layouts = discover_presets("layout")
    renders = discover_presets("render")
    assert any(e["short"] == "wide" for e in layouts)
    wide = next(e for e in layouts if e["short"] == "wide")
    assert "宽屏" in wide["menu_text"] or "Wide" in wide["menu_text"]
    assert any(e["short"] == "turbo" for e in renders)


def test_list_jobs_and_example_job_comments():
    r = _run([sys.executable, str(SCRIPTS / "render_cn_chat.py"), "--list-jobs"])
    assert r.returncode == 0
    example = ROOT / "jobs" / "example_job.yaml"
    assert example.is_file()
    text = example.read_text(encoding="utf-8")
    assert "video:" in text and "chat_html:" in text
    assert "占位" in text or "path/to" in text
    # every active key-ish line near comments: require substantial Chinese comments
    assert text.count("#") >= 20


def test_reusable_job_without_paths_needs_cli_or_tty():
    """example_job has commented video/chat — non-TTY must get clear error."""
    r = _run([sys.executable, str(SCRIPTS / "render_cn_chat.py"), "--job", "example_job"])
    assert r.returncode != 0
    joined = (r.stdout or "") + (r.stderr or "")
    assert (
        "未包含 video" in joined
        or "缺少 video" in joined
        or "非交互" in joined
        or "chat_html" in joined
        or "取消注释" in joined
    )


def test_write_job_default_comments_out_paths(tmp_path: Path):
    from job_config import load_job_file, write_job_file

    p = write_job_file(
        tmp_path / "style.yaml",
        {
            "mode": "preview",
            "render_original": True,
            "preview_clip": 10,
            "layout_preset": "compact",
            "video": "D:/a.mp4",
            "chat_html": "D:/a.html",
        },
        title="style",
        overwrite=True,
        pin_paths=False,
    )
    text = p.read_text(encoding="utf-8")
    # Active keys should not pin media
    data = load_job_file(p)
    assert "video" not in data or data.get("video") is None
    assert data.get("mode") == "preview"
    assert data.get("layout_preset") == "compact"
    assert "# video:" in text or "video:" in text and text.find("#") < text.find("mode")


# ---------------------------------------------------------------------------
# Job config / presets
# ---------------------------------------------------------------------------


def test_job_roundtrip_and_cli_wins(tmp_path: Path):
    from types import SimpleNamespace

    from job_config import apply_job_to_namespace, load_job_file, write_job_file

    vid = tmp_path / "v.mp4"
    html = tmp_path / "c.html"
    vid.write_bytes(b"x")
    html.write_text("<html></html>", encoding="utf-8")
    job_path = write_job_file(
        tmp_path / "j.yaml",
        {
            "video": str(vid),
            "chat_html": str(html),
            "mode": "preview",
            "render_original": True,
            "preview_clip": 7,
            "layout_preset": "compact",
            "overlay_codec": "png",
        },
        title="j",
        overwrite=True,
    )
    data = load_job_file(job_path)
    assert data["mode"] == "preview"
    assert data["preview_clip"] == 7
    args = SimpleNamespace(
        video=None,
        chat_html=None,
        mode="auto",
        render_original=False,
        preview_clip=None,
        layout_preset=None,
        overlay_codec="vp9",
    )
    applied = apply_job_to_namespace(
        args,
        data,
        cli_defaults={
            "video": None,
            "chat_html": None,
            "mode": "auto",
            "render_original": False,
            "preview_clip": None,
            "layout_preset": None,
            "overlay_codec": "vp9",
        },
    )
    assert args.mode == "preview"
    assert args.preview_clip == 7
    assert "mode" in applied
    # CLI wins
    args2 = SimpleNamespace(
        video=None,
        chat_html=None,
        mode="full",
        render_original=False,
        preview_clip=None,
        layout_preset=None,
        overlay_codec="vp9",
    )
    apply_job_to_namespace(
        args2,
        data,
        cli_defaults={
            "video": None,
            "chat_html": None,
            "mode": "auto",
            "render_original": False,
            "preview_clip": None,
            "layout_preset": None,
            "overlay_codec": "vp9",
        },
    )
    assert args2.mode == "full"


@pytest.mark.parametrize(
    "short,prefix",
    [
        ("compact", "layout"),
        ("mobile", "layout"),
        ("default", "layout"),
        ("fast", "render"),
        ("hq", "render"),
        ("default", "render"),
    ],
)
def test_short_preset_names_resolve(short: str, prefix: str):
    from common_utils import resolve_profiles_preset

    p = resolve_profiles_preset(short, prefix=prefix)
    assert p.is_file(), f"{prefix} {short} -> {p}"


# ---------------------------------------------------------------------------
# Mode matrix (no ffmpeg required)
# ---------------------------------------------------------------------------


def test_mode_defaults_matrix():
    from types import SimpleNamespace

    from render_cn_chat import PipelineError, apply_mode_defaults

    # preview
    a = SimpleNamespace(
        mode="preview",
        preview_clip=None,
        preview_frame=None,
        overlay_codec="vp9",
        render_preset=None,
        render_original=False,
        reuse_translation=False,
        skip_translate=False,
        manual_translation=False,
        review=False,
        review_done=False,
        lint_translation=None,
    )
    applied = apply_mode_defaults(a)
    assert a.preview_clip == 10.0
    assert a.overlay_codec == "png"
    assert any("preview" in x for x in applied)

    # render guard
    b = SimpleNamespace(
        mode="render",
        render_original=False,
        reuse_translation=False,
        skip_translate=False,
        manual_translation=False,
        review=False,
        review_done=False,
        lint_translation=None,
    )
    with pytest.raises(PipelineError):
        apply_mode_defaults(b)

    c = SimpleNamespace(
        mode="render",
        render_original=False,
        reuse_translation=True,
        skip_translate=False,
        manual_translation=False,
        review=False,
        review_done=False,
        lint_translation=None,
    )
    assert "render_only_guard" in apply_mode_defaults(c)

    # --review 不再豁免 --mode render 守卫（否则会走完整翻译路径、产生 API 费用）
    e = SimpleNamespace(
        mode="render",
        render_original=False,
        reuse_translation=False,
        skip_translate=False,
        manual_translation=False,
        review=True,
        review_done=False,
        lint_translation=None,
    )
    with pytest.raises(PipelineError):
        apply_mode_defaults(e)

    # lint sentinel（--lint-translation 不带值）同样不能豁免守卫
    f = SimpleNamespace(
        mode="render",
        render_original=False,
        reuse_translation=False,
        skip_translate=False,
        manual_translation=False,
        review=False,
        review_done=False,
        lint_translation="__PIPELINE__",
    )
    with pytest.raises(PipelineError):
        apply_mode_defaults(f)

    # 显式 lint 路径：短路为"仅质检"，不触发守卫错误
    g = SimpleNamespace(
        mode="render",
        render_original=False,
        reuse_translation=False,
        skip_translate=False,
        manual_translation=False,
        review=False,
        review_done=False,
        lint_translation="given/translation.json",
    )
    assert "render_lint_only" in apply_mode_defaults(g)

    # --review-done 被强制要求搭配 --reuse-translation，安全豁免保留
    h = SimpleNamespace(
        mode="render",
        render_original=False,
        reuse_translation=True,
        skip_translate=False,
        manual_translation=False,
        review=False,
        review_done=True,
        lint_translation=None,
    )
    assert "render_only_guard" in apply_mode_defaults(h)

    d = SimpleNamespace(mode="translate")
    assert "stop_after_translate" in apply_mode_defaults(d)


# ---------------------------------------------------------------------------
# Process / clean / concurrent helpers
# ---------------------------------------------------------------------------


def test_clean_skips_running_job(tmp_path: Path):
    import json

    from process_util import clean_temp_artifacts, make_job_dir

    job = make_job_dir(tmp_path, prefix="job_")
    (job / "run_meta.json").write_text(json.dumps({"status": "running"}), encoding="utf-8")
    (job / "keep.bin").write_bytes(b"x")
    # finished job
    job2 = make_job_dir(tmp_path, prefix="job_")
    (job2 / "run_meta.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    (job2 / "gone.bin").write_bytes(b"y")

    # Without clean_all, both jobs stay.
    count0, _ = clean_temp_artifacts(tmp_path, clean_progress=False, clean_all=False)
    assert job.is_dir() and job2.is_dir()
    assert count0 == 0

    count, _freed = clean_temp_artifacts(tmp_path, clean_progress=False, clean_all=True)
    assert job.is_dir(), "running job must remain"
    assert (job / "keep.bin").is_file()
    assert not job2.is_dir(), "finished job should be cleaned with clean_all"
    assert count >= 1


def test_gitignore_protects_user_jobs():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "jobs/*" in gi
    assert "!jobs/example_job.yaml" in gi
    assert "jobs/.last_job" in gi or ".last_job" in gi


# ---------------------------------------------------------------------------
# Parser / AST hygiene for long-term
# ---------------------------------------------------------------------------
# （"doctor 不得 exec 源码"契约断言已收敛至 tests/test_doctor_import.py 正典，
# 原本在此的 test_no_exec_burn_in_doctor_source 为重复断言，已删除。）


def test_test_tree_has_no_syntax_errors():
    errors = []
    for p in sorted((ROOT / "tests").glob("test_*.py")):
        try:
            ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        except SyntaxError as e:
            errors.append(f"{p.name}: {e}")
    assert not errors, errors


# ---------------------------------------------------------------------------
# FFmpeg max paths (skip without ffmpeg)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_max_render_matrix_core_paths(make_test_video, tmp_path: Path):
    """Broader than smoke: several layouts/codecs/modes in one module."""
    html = ROOT / "tests" / "fixtures" / "twitchdownloader_chat.html"
    if not html.is_file():
        pytest.skip("fixture missing")
    video = make_test_video(duration=3.0, fps=30)

    cases = [
        ["--render-original", "--preview-clip", "2", "--overlay-codec", "png", "--offset", "0"],
        ["--render-original", "--layout-preset", "compact", "--preview-clip", "2", "--overlay-codec", "png", "--offset", "0"],
        ["--render-original", "--layout-preset", "mobile", "--preview-clip", "2", "--overlay-codec", "png", "--offset", "0"],
        ["--mode", "preview", "--render-original", "--overlay-codec", "png", "--offset", "0", "--preview-clip", "2"],
        ["--render-original", "--preview-dense", "--preview-clip", "2", "--overlay-codec", "png", "--offset", "0"],
    ]
    for i, extra in enumerate(cases):
        out = tmp_path / f"case_{i}.mp4"
        work = tmp_path / f"w_{i}"
        cmd = [
            sys.executable,
            str(SCRIPTS / "render_cn_chat.py"),
            str(video),
            str(html),
            *extra,
            "--output",
            str(out),
            "--workdir",
            str(work),
            "--fps",
            "15",
        ]
        r = _run(cmd)
        assert r.returncode == 0, (extra, (r.stdout or "")[-500:], (r.stderr or "")[-500:])
        assert out.is_file() and out.stat().st_size > 1000


@pytest.mark.slow
def test_max_concurrent_two_jobs(make_test_video, tmp_path: Path):
    html = ROOT / "tests" / "fixtures" / "twitchdownloader_chat.html"
    if not html.is_file():
        pytest.skip("fixture missing")
    video = make_test_video(duration=3.0, fps=30)
    shared = tmp_path / "shared"
    shared.mkdir()

    def one(tag: str) -> subprocess.CompletedProcess:
        return _run(
            [
                sys.executable,
                str(SCRIPTS / "twitch_chat_burn.py"),
                str(video),
                str(html),
                "--preview-clip",
                "2",
                "--overlay-codec",
                "png",
                "--offset",
                "0",
                "--fps",
                "15",
                "--out-dir",
                str(shared),
                "--keep-temp",
            ]
        )

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(one, "a"), ex.submit(one, "b")]
        results = [f.result() for f in futs]
    assert all(r.returncode == 0 for r in results), [((r.stdout or "") + (r.stderr or ""))[-400:] for r in results]
    jobs = [p for p in shared.iterdir() if p.is_dir() and p.name.startswith("job_")]
    assert len(jobs) >= 2
    # promote unique names if collision
    root_mp4 = list(shared.glob("*_chat*.mp4"))
    assert len(root_mp4) >= 1
