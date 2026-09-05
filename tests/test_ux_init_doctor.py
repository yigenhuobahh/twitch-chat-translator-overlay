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


def monkeypatch_platform(ux_mod, name: str) -> None:
    """Force ux_setup's os.name branch regardless of the host OS.

    Uses SimpleNamespace on the module binding (load_module gives ux_setup its
    own module namespace), so the global stdlib os module is untouched.
    """
    ux_mod.os = SimpleNamespace(name=name, chmod=lambda p, m: os.chmod(p, m))


@pytest.fixture(scope="module")
def cw_mod():
    return load_module("chat_window", "chat_window.py")


@pytest.fixture(scope="module")
def pipeline():
    return load_module("render_cn_chat", "render_cn_chat.py")


def test_tighten_env_permissions_skipped_on_windows(ux_mod, tmp_path: Path):
    """Windows 分支:跳过收紧、不抛错、返回 False。

    平台判定经 monkeypatch 强制,两个平台都能跑完整断言矩阵
    (CI 上真实 os.name 是 posix,直接断言会与 chmods_on_posix 重复)。
    """
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_COMPAT_API_KEY=x\n", encoding="utf-8")
    monkeypatch_platform(ux_mod, "nt")

    assert ux_mod._tighten_env_permissions(env_file) is False


def test_tighten_env_permissions_chmods_on_posix(ux_mod, tmp_path: Path):
    """POSIX 分支:收紧到仅属主读写并返回 True。

    平台判定经 monkeypatch_platform 强制(Windows 上对文件 chmod 0o600
    合法,MSVCRT 只处理只读位,不会抛错;POSIX 上真实 chmod 也成立)。
    """
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_COMPAT_API_KEY=x\n", encoding="utf-8")
    monkeypatch_platform(ux_mod, "posix")

    assert ux_mod._tighten_env_permissions(env_file) is True


def test_ensure_dotenv_tightens_created_env(
    tmp_path: Path, ux_mod, monkeypatch
):
    """ensure_dotenv 创建 .env 后必须走权限收紧助手(接缝 spy)。"""
    example = tmp_path / ".env.example"
    example.write_text("OPENAI_COMPAT_API_KEY=\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    tightened: list[Path] = []
    monkeypatch.setattr(
        ux_mod, "_tighten_env_permissions", lambda p: (tightened.append(p), True)[1]
    )

    path, status = ux_mod.ensure_dotenv(tmp_path)

    assert status == "created"
    assert tightened == [tmp_path / ".env"]
    assert path is not None and path.read_text(encoding="utf-8") == example.read_text(
        encoding="utf-8"
    )


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


def test_doctor_early_exit_runs_before_job_loading(pipeline, monkeypatch, tmp_path, capsys):
    """--doctor 必须先于 --job 加载与交互提问早退。

    回归门（9f99463 修复）：`--doctor --job style.yaml`（job 缺 video/chat_html）
    在修复前会先进入 job 媒体路径解析/非交互报错，诊断根本跑不到。
    这里让 doctor 返回哨兵退出码 7，并用一个缺 video/chat_html 的 job YAML：
    doctor 早退时 pipeline.main() 必须以 7 退出且 job 加载不发生。
    """
    calls = {"doctor": 0}

    def fake_doctor(args):
        calls["doctor"] += 1
        return 7

    monkeypatch.setattr(pipeline, "doctor", fake_doctor)
    job = tmp_path / "style.yaml"
    job.write_text("x: 5\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["render_cn_chat.py", "--doctor", "--job", str(job)],
    )

    with pytest.raises(SystemExit) as excinfo:
        pipeline.main()

    assert calls["doctor"] == 1
    assert excinfo.value.code == 7
    # job 未被加载：既无 [job] 加载日志，也无缺媒体的非交互报错
    out = capsys.readouterr().out
    assert "[job]" not in out
    assert "video/chat_html" not in out


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


# ---------------------------------------------------------------------------
# doctor_check.doctor() 全路径：视频探针 + HTML 时间轴对齐诊断 + 退出码语义
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def doctor_mod():
    import doctor_check

    return doctor_check


def _doctor_args(video: Path | None = None, chat_html: Path | None = None, offset: float | None = None):
    return SimpleNamespace(
        video=video,
        chat_html=chat_html,
        offset=offset,
        font_path="auto",
        font_bold_path="auto",
        offer_fix=False,
        yes=False,
        fix_yes=False,
    )


def _write_td_chat(tmp_path: Path, name: str, seconds: list[int]) -> Path:
    """TwitchDownloader 形态的最小聊天 HTML（时间戳从 ?t=0h0mNs 链接解析）。"""
    rows = []
    for i, total in enumerate(seconds):
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        rows.append(
            f'<pre class="comment-root">[<a href="https://www.twitch.tv/videos/1?t={h}h{m}m{s}s">'
            f"{h}:{m:02d}:{s:02d}</a>] <span class=\"comment-author\">User{i}</span>"
            f'<span class="comment-message">: msg {i}</span></pre>'
        )
    html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
        + "".join(rows)
        + "</body></html>"
    )
    path = tmp_path / name
    path.write_text(html, encoding="utf-8")
    return path


def _patch_doctor_probe(monkeypatch, doctor_mod, duration: str) -> None:
    """FFmpeg 面 stub：safe_which/require_executable/探针输出/字体/就绪清单可控。

    patch 全部落在 doctor_check 模块属性上，不触碰实现文件；
    prepend_tools_ffmpeg_to_path 一并短路，避免测试进程的 os.environ PATH 被改写。
    """
    monkeypatch.setattr(doctor_mod, "prepend_tools_ffmpeg_to_path", lambda: None)
    monkeypatch.setattr(doctor_mod, "safe_which", lambda name: f"C:/trusted-tools/{name}.exe")
    monkeypatch.setattr(doctor_mod, "require_executable", lambda name: f"{name}.exe")
    monkeypatch.setattr(
        doctor_mod, "detect_cjk_font", lambda: (r"C:\fonts\cjk.ttf", r"C:\fonts\cjk-bold.ttf")
    )
    monkeypatch.setattr(doctor_mod, "print_readiness_report", lambda items=None: (True, True))
    monkeypatch.setattr(
        doctor_mod.subprocess,
        "run",
        lambda cmd, **kwargs: SimpleNamespace(returncode=0, stdout=f"{duration}\n", stderr=""),
    )


def test_doctor_missing_video_file_fails_with_hint(doctor_mod, tmp_path, capsys):
    code = doctor_mod.doctor(_doctor_args(video=tmp_path / "nope.mp4"))

    out = capsys.readouterr().out
    assert "[FAIL] 输入视频" in out
    assert "诊断结果: 存在问题" in out
    assert code == 1


def test_doctor_missing_chat_html_file_fails(doctor_mod, tmp_path, capsys):
    code = doctor_mod.doctor(_doctor_args(chat_html=tmp_path / "nope.html"))

    out = capsys.readouterr().out
    assert "[FAIL] 聊天 HTML" in out
    assert "诊断结果: 存在问题" in out
    assert code == 1


def test_doctor_skips_probe_and_alignment_without_ffprobe(
    doctor_mod, tmp_path, monkeypatch, capsys
):
    video = tmp_path / "real.mp4"
    video.write_bytes(b"video")
    chat = _write_td_chat(tmp_path, "chat.html", [1, 2, 3])
    monkeypatch.setattr(doctor_mod, "prepend_tools_ffmpeg_to_path", lambda: None)
    monkeypatch.setattr(doctor_mod, "safe_which", lambda name: None)

    code = doctor_mod.doctor(_doctor_args(video=video, chat_html=chat))

    out = capsys.readouterr().out
    assert "[FAIL] ffprobe" in out
    assert "视频可读取" not in out  # ffprobe 缺失：探针门禁直接短路
    assert "时间轴对齐" not in out
    assert code == 1


def test_doctor_probe_and_alignment_ok_with_stubbed_ffprobe(
    doctor_mod, tmp_path, monkeypatch, capsys, fixtures_dir
):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    chat = fixtures_dir / "twitchdownloader_chat.html"
    _patch_doctor_probe(monkeypatch, doctor_mod, "12.5")

    code = doctor_mod.doctor(_doctor_args(video=video, chat_html=chat))

    out = capsys.readouterr().out
    assert "[OK] 视频可读取: 12.5" in out
    assert "[OK] 时间轴对齐" in out  # 首条 1s < 视频 12.5s，无警告
    assert "诊断结果: 通过" in out
    assert code == 0


def test_doctor_detects_auto_offset_from_first_message_beyond_video(
    doctor_mod, tmp_path, monkeypatch, capsys
):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    chat = _write_td_chat(tmp_path, "chat.html", [100, 101, 103])
    _patch_doctor_probe(monkeypatch, doctor_mod, "10.0")

    code = doctor_mod.doctor(_doctor_args(video=video, chat_html=chat))

    out = capsys.readouterr().out
    assert "将自动 offset=100s" in out
    assert "# doctor 检测到自动 offset≈100s" in out
    assert code == 0


def test_doctor_flags_misaligned_chat_that_auto_offset_cannot_fix(
    doctor_mod, tmp_path, monkeypatch, capsys
):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    chat = _write_td_chat(tmp_path, "chat.html", [100, 150, 200])
    _patch_doctor_probe(monkeypatch, doctor_mod, "10.0")

    code = doctor_mod.doctor(_doctor_args(video=video, chat_html=chat))

    out = capsys.readouterr().out
    assert "[WARN] 时间轴对齐" in out
    assert "自动检测未触发" in out
    assert "时间轴有警告" in out
    assert code == 0  # WARN 不计入失败，诊断整体仍算通过


def test_doctor_reports_chat_html_without_messages(
    doctor_mod, tmp_path, monkeypatch, capsys
):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    chat = tmp_path / "empty.html"
    chat.write_text("<html><body></body></html>", encoding="utf-8")
    _patch_doctor_probe(monkeypatch, doctor_mod, "10.0")

    code = doctor_mod.doctor(_doctor_args(video=video, chat_html=chat))

    out = capsys.readouterr().out
    assert "解析到 0 条消息" in out
    assert code == 0


def test_doctor_alignment_failure_is_non_fatal(doctor_mod, tmp_path, monkeypatch, capsys):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    chat = _write_td_chat(tmp_path, "chat.html", [1, 2, 3])
    _patch_doctor_probe(monkeypatch, doctor_mod, "10.0")

    def raise_runtime(*_a, **_k):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr("chat_parser.parse_chat_html", raise_runtime)

    code = doctor_mod.doctor(_doctor_args(video=video, chat_html=chat))

    out = capsys.readouterr().out
    assert "跳过详细诊断 (RuntimeError)" in out
    assert code == 0  # doctor 不应因诊断失败而整体失败


def test_doctor_manual_offset_bypasses_auto_detection(
    doctor_mod, tmp_path, monkeypatch, capsys
):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    chat = _write_td_chat(tmp_path, "chat.html", [1, 2, 3])
    _patch_doctor_probe(monkeypatch, doctor_mod, "10.0")

    code = doctor_mod.doctor(_doctor_args(video=video, chat_html=chat, offset=5.0))

    out = capsys.readouterr().out
    assert "[OK] 时间轴对齐" in out
    assert "自动 offset" not in out  # manual 模式不做自动检测推荐
    assert code == 0


@pytest.mark.smoke
def test_doctor_real_ffprobe_reports_video_and_alignment(
    doctor_mod, tmp_path, monkeypatch, capsys, make_test_video, fixtures_dir
):
    video = make_test_video(duration=3.0)
    chat = fixtures_dir / "twitchdownloader_chat.html"
    monkeypatch.setattr(doctor_mod, "prepend_tools_ffmpeg_to_path", lambda: None)

    code = doctor_mod.doctor(_doctor_args(video=video, chat_html=chat))

    out = capsys.readouterr().out
    assert "视频可读取" in out
    assert "时间轴对齐" in out
    assert "诊断结果:" in out
    assert code in (0, 1)  # 退出码取决于本机字体等环境项；探针段本身必须存在


@pytest.mark.smoke
def test_doctor_real_ffprobe_detects_auto_offset(
    doctor_mod, tmp_path, monkeypatch, capsys, make_test_video
):
    video = make_test_video(duration=2.0)
    chat = _write_td_chat(tmp_path, "chat.html", [100, 101, 103])
    monkeypatch.setattr(doctor_mod, "prepend_tools_ffmpeg_to_path", lambda: None)

    code = doctor_mod.doctor(_doctor_args(video=video, chat_html=chat))

    out = capsys.readouterr().out
    assert "将自动 offset=100s" in out
    assert code in (0, 1)


# ---------------------------------------------------------------------------
# doctor 分支补齐：字体显式路径 / 探针失败 / 警告分支 / 自动修复复检 / 平台文案
# ---------------------------------------------------------------------------


def test_doctor_explicit_font_paths_check_file_existence(
    doctor_mod, tmp_path, monkeypatch, capsys
):
    good_font = tmp_path / "cjk.ttf"
    good_font.write_bytes(b"font")
    args = _doctor_args()
    args.font_path = str(good_font)
    args.font_bold_path = str(tmp_path / "missing-bold.ttf")
    _patch_doctor_probe(monkeypatch, doctor_mod, "10.0")

    code = doctor_mod.doctor(args)

    out = capsys.readouterr().out
    assert f"[OK] 常规字体: {good_font}" in out
    assert "[FAIL] 粗体字体" in out
    assert code == 1


def test_doctor_alignment_warns_when_message_span_much_shorter_than_video(
    doctor_mod, tmp_path, monkeypatch, capsys
):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    chat = _write_td_chat(tmp_path, "chat.html", [70, 75, 80])
    _patch_doctor_probe(monkeypatch, doctor_mod, "1000.0")

    code = doctor_mod.doctor(_doctor_args(video=video, chat_html=chat))

    out = capsys.readouterr().out
    assert "[WARN] 时间轴对齐" in out
    assert "消息跨度" in out
    assert "时间轴有警告" in out
    assert code == 0


def test_doctor_reports_probe_timeout_as_video_failure(
    doctor_mod, tmp_path, monkeypatch, capsys
):
    import subprocess

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(doctor_mod, "prepend_tools_ffmpeg_to_path", lambda: None)
    monkeypatch.setattr(doctor_mod, "safe_which", lambda name: f"C:/t/{name}.exe")
    monkeypatch.setattr(doctor_mod, "require_executable", lambda name: f"{name}.exe")
    monkeypatch.setattr(
        doctor_mod, "detect_cjk_font", lambda: (r"C:\fonts\cjk.ttf", r"C:\fonts\cjk-bold.ttf")
    )
    monkeypatch.setattr(doctor_mod, "print_readiness_report", lambda items=None: (True, True))

    def raise_timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=45)

    monkeypatch.setattr(doctor_mod.subprocess, "run", raise_timeout)

    code = doctor_mod.doctor(_doctor_args(video=video))

    out = capsys.readouterr().out
    assert "[FAIL] 视频可读取" in out
    assert code == 1


def test_doctor_unparseable_duration_keeps_alignment_silent(
    doctor_mod, tmp_path, monkeypatch, capsys
):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    chat = _write_td_chat(tmp_path, "chat.html", [1, 2, 3])
    _patch_doctor_probe(monkeypatch, doctor_mod, "N/A")

    code = doctor_mod.doctor(_doctor_args(video=video, chat_html=chat))

    out = capsys.readouterr().out
    assert "[OK] 视频可读取" in out  # 探针退出码 0，但时长无法解析 → 不做对齐诊断
    assert "时间轴对齐" not in out
    assert code == 0


@pytest.mark.parametrize("system,expected_hint", [("Darwin", "brew install ffmpeg"), ("Linux", "apt install ffmpeg")])
def test_doctor_platform_specific_ffmpeg_fix_hints(
    doctor_mod, monkeypatch, capsys, system, expected_hint
):
    monkeypatch.setattr(doctor_mod, "prepend_tools_ffmpeg_to_path", lambda: None)
    monkeypatch.setattr(doctor_mod, "safe_which", lambda name: None)
    monkeypatch.setattr(doctor_mod, "detect_cjk_font", lambda: (None, None))
    monkeypatch.setattr(doctor_mod, "print_readiness_report", lambda items=None: (False, False))
    monkeypatch.setattr(doctor_mod, "maybe_prompt_offer_fixes", lambda **k: False)
    monkeypatch.setattr(doctor_mod.platform, "system", lambda: system)

    code = doctor_mod.doctor(_doctor_args())

    out = capsys.readouterr().out
    assert expected_hint in out
    assert code == 1


def test_doctor_stub_module_without_spec_falls_back_to_sys_modules(
    doctor_mod, monkeypatch, capsys
):
    import sys
    from types import ModuleType

    _patch_doctor_probe(monkeypatch, doctor_mod, "10.0")
    stub = ModuleType("yaml")
    stub.__spec__ = None  # 某些测试桩模块会把 __spec__ 置空
    monkeypatch.setitem(sys.modules, "yaml", stub)

    code = doctor_mod.doctor(_doctor_args())

    out = capsys.readouterr().out
    assert "[OK] PyYAML" in out  # find_spec 抛 ValueError 时按 sys.modules 兜底
    assert code == 0


def test_doctor_offer_fix_runs_then_rechecks(doctor_mod, tmp_path, monkeypatch, capsys):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(doctor_mod, "prepend_tools_ffmpeg_to_path", lambda: None)
    monkeypatch.setattr(doctor_mod, "safe_which", lambda name: None)
    monkeypatch.setattr(
        doctor_mod, "detect_cjk_font", lambda: (r"C:\fonts\cjk.ttf", r"C:\fonts\cjk-bold.ttf")
    )
    offered: list[bool] = []
    monkeypatch.setattr(doctor_mod, "offer_fixes", lambda **k: offered.append(True))
    monkeypatch.setattr(doctor_mod, "maybe_prompt_offer_fixes", lambda **k: True)
    reports = iter([(False, True), (True, True)])
    monkeypatch.setattr(
        doctor_mod, "print_readiness_report", lambda items=None: next(reports)
    )

    args = _doctor_args(video=video)
    args.offer_fix = True
    code = doctor_mod.doctor(args)

    out = capsys.readouterr().out
    assert offered == [True]
    assert "--- 修复后复检 ---" in out
    assert code == 1  # ffprobe 仍缺失：复检后依旧失败
