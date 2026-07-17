#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for --init scaffold, doctor next-steps, offset diagnosis formatter, mode defaults."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from helpers import load_module

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def ux_mod():
    return load_module("ux_setup", "ux_setup.py")


@pytest.fixture(scope="module")
def cw_mod():
    return load_module("chat_window", "chat_window.py")


@pytest.fixture(scope="module")
def pipeline():
    return load_module("render_cn_chat", "render_cn_chat.py")


def test_ensure_dotenv_creates_from_example(tmp_path: Path, ux_mod, monkeypatch):
    example = tmp_path / ".env.example"
    example.write_text("OPENAI_COMPAT_API_KEY=\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # find_env_example also checks repo; write cwd example first
    path, status = ux_mod.ensure_dotenv(tmp_path)
    assert status == "created"
    assert path is not None and path.is_file()
    path2, status2 = ux_mod.ensure_dotenv(tmp_path)
    assert status2 == "exists"


def test_ensure_dotenv_uses_installed_share_template(tmp_path: Path, ux_mod, monkeypatch):
    cwd = tmp_path / "project"
    cwd.mkdir()
    share = tmp_path / "venv" / "share" / "twitch-chat-translator-overlay"
    share.mkdir(parents=True)
    template = share / ".env.example"
    template.write_text("OPENAI_COMPAT_MODEL=installed-model\n", encoding="utf-8")

    monkeypatch.chdir(cwd)
    monkeypatch.setattr(ux_mod, "_repo_root", lambda: tmp_path / "missing-source-root")
    monkeypatch.setattr(ux_mod, "distribution_share_dirs", lambda: [share])

    path, status = ux_mod.ensure_dotenv(cwd)

    assert status == "created"
    assert path == cwd / ".env"
    assert path.read_text(encoding="utf-8") == template.read_text(encoding="utf-8")


def test_preset_short_name_resolves_from_sysconfig_data_share(tmp_path: Path, monkeypatch):
    import common_utils

    data_root = tmp_path / "venv"
    profiles = data_root / "share" / "twitch-chat-translator-overlay" / "profiles"
    profiles.mkdir(parents=True)
    installed = profiles / "layout_installed_only.yaml"
    installed.write_text("layout: {width: 321}\n", encoding="utf-8")

    real_get_path = common_utils.sysconfig.get_path
    monkeypatch.setattr(
        common_utils.sysconfig,
        "get_path",
        lambda name, *args, **kwargs: str(data_root) if name == "data" else real_get_path(name, *args, **kwargs),
    )

    resolved = common_utils.resolve_profiles_preset("installed_only", prefix="layout")

    assert resolved == installed


def test_distribution_share_dirs_include_user_install_base(tmp_path: Path, monkeypatch):
    import common_utils

    user_base = tmp_path / "user-base"
    monkeypatch.setattr(common_utils.site, "getuserbase", lambda: str(user_base))

    expected = user_base / "share" / "twitch-chat-translator-overlay"
    assert expected in common_utils.distribution_share_dirs()


def test_distribution_share_dirs_include_console_entry_prefix(tmp_path: Path, monkeypatch):
    import common_utils

    prefix = tmp_path / "entry-venv"
    entry = prefix / "Scripts" / "twitch-chat-overlay.exe"
    installed_module = prefix / "Lib" / "site-packages" / "common_utils.py"
    monkeypatch.setattr(common_utils, "__file__", str(installed_module))
    monkeypatch.setattr(common_utils.sys, "argv", [str(entry)])

    expected = prefix / "share" / "twitch-chat-translator-overlay"
    assert expected in common_utils.distribution_share_dirs()


def test_runtime_app_root_uses_cwd_outside_source_checkout(tmp_path: Path, monkeypatch):
    import common_utils

    cwd = tmp_path / "project"
    cwd.mkdir()
    installed_module = tmp_path / "venv" / "Lib" / "common_utils.py"
    monkeypatch.chdir(cwd)

    assert common_utils.runtime_app_root(installed_module) == cwd.resolve()


def test_public_profile_and_rules_resolve_from_installed_share(tmp_path: Path, monkeypatch):
    import common_utils

    share = tmp_path / "prefix" / "share" / "twitch-chat-translator-overlay"
    profile = share / "profiles" / "installed_profile.yaml"
    rules = share / "configs" / "installed_rules.yaml"
    profile.parent.mkdir(parents=True)
    rules.parent.mkdir(parents=True)
    profile.write_text("label: installed\n", encoding="utf-8")
    rules.write_text("normalizations: []\n", encoding="utf-8")
    monkeypatch.setattr(common_utils, "distribution_share_dirs", lambda: [share])

    assert common_utils.resolve_public_resource(
        "profiles/installed_profile.yaml", subdir="profiles"
    ) == profile.resolve()
    assert common_utils.resolve_public_resource(
        "configs/installed_rules.yaml", subdir="configs"
    ) == rules.resolve()


def test_source_next_steps_use_python_and_repo_launchers(ux_mod, capsys):
    script = "scripts/render_cn_chat.py"
    command = ux_mod.format_cli_invocation(script)
    ux_mod.print_setup_next_steps(has_api=False, script=script)
    out = capsys.readouterr().out
    assert f"{command} --init" in out
    assert sys.executable in command
    assert "run.bat" in out
    assert "bash run.sh" in out


def test_installed_next_steps_use_console_entry_only(ux_mod, capsys):
    script = "C:/Program Files/A&B/twitch-chat-overlay.exe"
    command = ux_mod.format_cli_invocation(script)
    ux_mod.print_setup_next_steps(has_api=False, script=script)
    out = capsys.readouterr().out
    assert f"{command} --init" in out
    assert sys.executable not in command
    assert command[0] in ("'", '"') and command[-1] == command[0]
    assert "scripts/render_cn_chat.py" not in out
    assert "run.bat" not in out
    assert "bash run.sh" not in out


def test_installed_download_hints_use_console_entry(
    tmp_path: Path, pipeline, monkeypatch, capsys
):
    entry = tmp_path / "Program Files" / "A&B" / "twitch-chat-overlay.exe"
    monkeypatch.setattr(sys, "argv", [str(entry)])

    code = pipeline._post_download_next_steps(
        tmp_path / "video.mp4",
        tmp_path / "chat.html",
        download_only=True,
        yes=False,
    )

    out = capsys.readouterr().out
    command = pipeline.current_cli_invocation()
    assert code == 0
    assert command in out
    assert sys.executable not in command
    assert "render_cn_chat.py" not in out


def test_installed_burn_hint_uses_burn_console_entry(tmp_path: Path, monkeypatch):
    burn = load_module("twitch_chat_burn_hint", "twitch_chat_burn.py")
    entry = tmp_path / "Program Files" / "A&B" / "twitch-chat-burn.exe"
    monkeypatch.setattr(sys, "argv", [str(entry)])

    command = burn._format_import_translation_command(
        tmp_path / "video.mp4",
        tmp_path / "chat.html",
        tmp_path / "translation.json",
    )

    assert "twitch-chat-burn" in command
    assert sys.executable not in command
    assert "twitch_chat_burn.py" not in command


def test_ensure_example_job(tmp_path: Path, ux_mod):
    path, status = ux_mod.ensure_example_job(tmp_path)
    assert status == "created"
    assert path is not None and path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "mode:" in text
    assert "video:" in text


def test_format_offset_diagnosis_auto(cw_mod):
    messages = [{"timestamp": 3600.0}, {"timestamp": 3650.0}]
    diag = cw_mod.compute_time_offset(messages, video_duration=120.0, manual_offset=None)
    text = cw_mod.format_offset_diagnosis(diag)
    assert "Offset" in text or "时间轴" in text
    assert "auto" in text.lower() or "自动" in text
    assert "3600" in text or "3600.0" in text
    assert "--preview-clip" in text
    assert "--offset" in text


def test_format_offset_diagnosis_manual(cw_mod):
    messages = [{"timestamp": 10.0}, {"timestamp": 20.0}]
    diag = cw_mod.compute_time_offset(messages, video_duration=100.0, manual_offset=5.0)
    text = cw_mod.format_offset_diagnosis(diag)
    assert "手动" in text or "manual" in text.lower()
    assert "5" in text


def test_format_offset_diagnosis_empty(cw_mod):
    text = cw_mod.format_offset_diagnosis(None)
    assert "无数据" in text or "无" in text


def test_apply_mode_preview_defaults(pipeline):
    args = SimpleNamespace(
        mode="preview",
        preview_clip=None,
        preview_frame=None,
        overlay_codec="vp9",
        render_preset=None,
        render_original=False,
        reuse_translation=False,
    )
    applied = pipeline.apply_mode_defaults(args)
    assert args.preview_clip == 10.0
    assert args.overlay_codec == "png"
    assert any("preview_clip" in a for a in applied)


def test_apply_mode_preview_keeps_explicit_preview_clip(pipeline):
    args = SimpleNamespace(
        mode="preview",
        preview_clip=3.0,
        preview_frame=None,
        overlay_codec="vp9",
        render_preset=None,
        render_original=False,
        reuse_translation=False,
    )
    pipeline.apply_mode_defaults(args)
    assert args.preview_clip == 3.0


def test_apply_mode_render_requires_reuse_or_original(pipeline):
    args = SimpleNamespace(
        mode="render",
        preview_clip=None,
        preview_frame=None,
        overlay_codec="vp9",
        render_preset=None,
        render_original=False,
        reuse_translation=False,
    )
    with pytest.raises(pipeline.PipelineError):
        pipeline.apply_mode_defaults(args)


def test_apply_mode_render_ok_with_reuse(pipeline):
    args = SimpleNamespace(
        mode="render",
        preview_clip=None,
        preview_frame=None,
        overlay_codec="vp9",
        render_preset=None,
        render_original=False,
        reuse_translation=True,
    )
    applied = pipeline.apply_mode_defaults(args)
    assert "render_only_guard" in applied


def test_doctor_mentions_next_steps(pipeline, capsys, monkeypatch):
    # Avoid depending on real video; doctor without inputs still prints 推荐下一步
    monkeypatch.delenv("OPENAI_COMPAT_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_COMPAT_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_COMPAT_MODEL", raising=False)
    args = SimpleNamespace(
        video=None,
        chat_html=None,
        font_path="auto",
        font_bold_path="auto",
        offset=None,
    )
    code = pipeline.doctor(args)
    out = capsys.readouterr().out
    assert "推荐下一步" in out
    assert "--init" in out or "init" in out
    assert "render-original" in out or "--render-original" in out
    # API missing is WARN only; exit depends on ffmpeg/python packages
    assert code in (0, 1)


def test_doctor_source_still_imports_chat_parser_not_exec():
    text = (ROOT / "scripts" / "render_cn_chat.py").read_text(encoding="utf-8")
    assert "from chat_parser import parse_chat_html" in text
    assert 'exec(compile(code, str(burn_path), "exec"), glb)' not in text


def test_layout_short_name_compact_resolves():
    layout = load_module("layout_preset", "layout_preset.py")
    preset = layout.load_layout_preset("compact")
    assert "width" in preset or "x" in preset


def test_render_short_name_fast_resolves():
    render = load_module("render_preset", "render_preset.py")
    preset = render.load_render_preset("fast")
    assert preset.get("overlay_codec") == "png"

def test_active_console_share_precedes_stale_global_resource(tmp_path: Path, monkeypatch):
    import common_utils

    current = tmp_path / "current"
    global_root = tmp_path / "global"
    relative = Path("profiles") / "layout_collision.yaml"
    current_file = current / "share" / "twitch-chat-translator-overlay" / relative
    global_file = global_root / "share" / "twitch-chat-translator-overlay" / relative
    current_file.parent.mkdir(parents=True)
    global_file.parent.mkdir(parents=True)
    current_file.write_text("label: current\n", encoding="utf-8")
    global_file.write_text("label: stale-global\n", encoding="utf-8")

    installed_module = current / "Lib" / "site-packages" / "common_utils.py"
    entry = current / "Scripts" / "twitch-chat-overlay.exe"
    monkeypatch.setattr(common_utils, "__file__", str(installed_module))
    monkeypatch.setattr(common_utils.sys, "argv", [str(entry)])
    monkeypatch.setattr(
        common_utils.sysconfig,
        "get_path",
        lambda name, *args, **kwargs: str(global_root) if name == "data" else None,
    )
    monkeypatch.setattr(common_utils.sys, "prefix", str(global_root))
    monkeypatch.setattr(common_utils.site, "getuserbase", lambda: str(tmp_path / "user"))

    shares = common_utils.distribution_share_dirs()

    assert shares[0] == current / "share" / "twitch-chat-translator-overlay"
    assert common_utils.resolve_profiles_preset(
        "collision", prefix="layout"
    ) == current_file


def test_source_checkout_ignores_unrelated_console_argv(tmp_path: Path, monkeypatch):
    import common_utils

    fake_prefix = tmp_path / "fake"
    fake_entry = fake_prefix / "Scripts" / "twitch-chat-overlay.exe"
    monkeypatch.setattr(common_utils.sys, "argv", [str(fake_entry)])

    assert (
        fake_prefix / "share" / "twitch-chat-translator-overlay"
        not in common_utils.distribution_share_dirs()
    )


def test_trusted_tools_root_never_uses_installed_cwd(tmp_path: Path, monkeypatch):
    import common_utils

    cwd = tmp_path / "untrusted media"
    cwd.mkdir()
    installed_module = tmp_path / "venv" / "Lib" / "site-packages" / "common_utils.py"
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))

    root = common_utils.trusted_tools_root(installed_module)

    assert root != cwd.resolve()
    assert root.name == "twitch-chat-translator-overlay"
    assert common_utils.runtime_app_root(installed_module) == cwd.resolve()


def test_setup_next_steps_quotes_media_paths(ux_mod, tmp_path: Path, capsys):
    video = tmp_path / "media & clips" / "video $one.mp4"
    chat = tmp_path / "media & clips" / "chat file.html"

    ux_mod.print_setup_next_steps(
        has_api=True,
        video=video,
        chat=chat,
        script="scripts/render_cn_chat.py",
    )

    out = capsys.readouterr().out
    assert ux_mod.quote_cli_arg(video) in out
    assert ux_mod.quote_cli_arg(chat) in out


def test_run_init_quotes_example_job_path(ux_mod, tmp_path: Path, monkeypatch, capsys):
    cwd = tmp_path / "My Videos"
    job = cwd / "jobs" / "example_job.yaml"
    env_path = cwd / ".env"
    entry = tmp_path / "Program Files" / "twitch-chat-overlay.exe"
    monkeypatch.setattr(ux_mod, "ensure_dotenv", lambda: (env_path, "created"))
    monkeypatch.setattr(ux_mod, "ensure_example_job", lambda: (job, "created"))
    monkeypatch.setattr(ux_mod, "print_setup_next_steps", lambda **kwargs: None)
    monkeypatch.setattr(ux_mod, "current_cli_script", lambda: str(entry))
    monkeypatch.setattr(ux_mod, "_repo_root", lambda: tmp_path / "no-source")

    assert ux_mod.run_init(create_job=True) == 0

    out = capsys.readouterr().out
    assert f"--job {ux_mod.quote_cli_arg(job)}" in out

def test_safe_which_skips_cwd_even_when_path_lists_it(tmp_path: Path, monkeypatch):
    import common_utils

    cwd = tmp_path / "media"
    trusted_bin = tmp_path / "system-bin"
    cwd.mkdir()
    trusted_bin.mkdir()
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    untrusted = cwd / name
    trusted = trusted_bin / name
    untrusted.write_bytes(b"untrusted")
    trusted.write_bytes(b"trusted")
    if os.name != "nt":
        untrusted.chmod(0o755)
        trusted.chmod(0o755)

    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", os.pathsep.join([str(cwd), str(trusted_bin)]))

    assert common_utils.safe_which("ffmpeg") == str(trusted.resolve())
    assert common_utils.safe_which(str(untrusted)) is None

def test_safe_which_allows_absolute_path_directory_below_cwd(
    tmp_path: Path, monkeypatch
):
    import common_utils

    home = tmp_path / "home"
    user_bin = home / ".local" / "bin"
    user_bin.mkdir(parents=True)
    name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    executable = user_bin / name
    executable.write_bytes(b"trusted explicit PATH entry")
    if os.name != "nt":
        executable.chmod(0o755)

    monkeypatch.chdir(home)
    monkeypatch.setenv("PATH", str(user_bin))

    assert common_utils.safe_which("ffprobe") == str(executable.resolve())
