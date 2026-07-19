"""Deterministic coverage for high-risk interactive setup and wizard paths."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace


def test_job_wizard_creates_default_preview_job(tmp_path: Path, monkeypatch):
    import job_wizard as wizard

    answers = iter(("style", "1", "1", "1", "n", "y", "2"))
    saved: dict[str, object] = {}
    monkeypatch.setattr(wizard, "_prompt", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(wizard, "discover_presets", lambda prefix: [{"short": "fast" if prefix == "render" else "default"}])
    monkeypatch.setattr(wizard, "format_preset_menu_lines", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(wizard, "pick_preset_from_menu", lambda entries, *_args, **_kwargs: entries[0]["short"])
    monkeypatch.setattr(wizard, "_list_index_for", lambda *_args: 1)
    monkeypatch.setattr(wizard, "save_last_job", lambda *_args: None)
    monkeypatch.setattr(wizard, "_stdin_is_interactive", lambda: False)

    def write(path, fields, **kwargs):
        saved.update(path=path, fields=dict(fields), kwargs=kwargs)
        return path

    monkeypatch.setattr(wizard, "write_job_file", write)

    path = wizard.run_job_wizard(jobs_dir=tmp_path)

    assert path == tmp_path / "style.yaml"
    assert saved["fields"] == {
        "workdir": str((tmp_path / "style").resolve()),
        "mode": "preview",
        "render_original": True,
        "preview_clip": 10,
        "layout_preset": "default",
        "render_preset": "fast",
    }
    assert saved["kwargs"] == {"title": "style", "overwrite": False, "pin_paths": False}


def test_job_wizard_creates_pinned_reuse_translation_job(tmp_path: Path, monkeypatch):
    import job_wizard as wizard

    video = tmp_path / "video.mp4"
    chat = tmp_path / "chat.html"
    translation = tmp_path / "translation.json"
    for path in (video, chat, translation):
        path.write_text("x", encoding="utf-8")
    answers = iter(("reuse", "3", "1", "1", "y", "y", "1.5", "y", str(tmp_path / "output.mp4"), "y", "2"))
    paths = iter((str(video), str(chat)))
    saved: dict[str, object] = {}
    monkeypatch.setattr(wizard, "_prompt", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(wizard, "_prompt_path", lambda *_args, **_kwargs: next(paths))
    monkeypatch.setattr(wizard, "_prompt_translation_json", lambda _video: str(translation))
    monkeypatch.setattr(wizard, "discover_presets", lambda prefix: [{"short": "default"}])
    monkeypatch.setattr(wizard, "format_preset_menu_lines", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(wizard, "pick_preset_from_menu", lambda entries, *_args, **_kwargs: entries[0]["short"])
    monkeypatch.setattr(wizard, "_list_index_for", lambda *_args: None)
    monkeypatch.setattr(wizard, "save_last_job", lambda *_args: None)
    monkeypatch.setattr(wizard, "_stdin_is_interactive", lambda: False)
    monkeypatch.setattr(wizard, "write_job_file", lambda path, fields, **kwargs: saved.update(fields=dict(fields), kwargs=kwargs) or path)

    assert wizard.run_job_wizard(jobs_dir=tmp_path) == tmp_path / "reuse.yaml"
    assert saved["fields"]["reuse_translation"] is True
    assert saved["fields"]["offset"] == 1.5
    assert saved["fields"]["video"] == str(video)
    assert saved["fields"]["chat_html"] == str(chat)
    assert saved["fields"]["translation_json"] == str(translation)
    assert saved["kwargs"]["pin_paths"] is True


def test_download_menu_runs_original_preview_with_downloaded_paths(tmp_path: Path, monkeypatch):
    import job_wizard as wizard
    import twitch_download as download

    video = tmp_path / "downloaded.mp4"
    chat = tmp_path / "downloaded.html"
    video.write_bytes(b"video")
    chat.write_text("<html></html>", encoding="utf-8")
    seen: dict[str, object] = {}
    answers = iter(("2819850140", "auto", "1080p60", "1", "", "", "Safe", "fast", "audio", "", "token", "1"))
    monkeypatch.setattr(wizard, "_prompt", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(download, "find_twitchdownloader_cli", lambda: tmp_path / "TwitchDownloaderCLI.exe")
    monkeypatch.setattr(download, "tools_td_bin_dirs", lambda: [tmp_path])
    monkeypatch.setattr(
        download,
        "download_assets",
        lambda source, **kwargs: seen.update(source=source, kwargs=kwargs) or SimpleNamespace(video_path=video, chat_html_path=chat),
    )
    command: list[str] = []
    monkeypatch.setattr(wizard, "_run_pipeline", lambda *args: command.extend(args) or 0)

    assert wizard._menu_download_and_continue() == 0

    assert seen["source"] == "2819850140"
    assert seen["kwargs"]["oauth"] == "token"
    assert command == [
        str(video), str(chat), "--mode", "preview", "--render-original", "--preview-clip", "10", "--yes",
    ]


def test_multi_segment_prompt_retries_invalid_input_before_confirming(monkeypatch):
    import job_wizard as wizard

    answers = iter(("bad input", "0:00:00 0:00:05", "", "y"))
    monkeypatch.setattr(wizard, "_prompt", lambda *_args, **_kwargs: next(answers))

    assert wizard._prompt_multi_segments() == [("0:00:00", "0:00:05")]


def test_wizard_main_routes_known_commands(monkeypatch):
    import job_wizard as wizard

    seen: list[tuple[str, object]] = []
    monkeypatch.setattr(wizard, "run_menu", lambda: seen.append(("menu", None)) or 0)
    monkeypatch.setattr(wizard, "run_quick_start", lambda: seen.append(("quick", None)) or 0)
    monkeypatch.setattr(wizard, "run_drag_drop", lambda args: seen.append(("drop", list(args))) or 0)

    assert wizard.main(["menu"]) == 0
    assert wizard.main(["quick"]) == 0
    assert wizard.main(["drop", "video.mp4", "chat.html"]) == 0
    assert seen == [("menu", None), ("quick", None), ("drop", ["video.mp4", "chat.html"])]


def test_probe_translate_api_covers_success_and_provider_failure(monkeypatch):
    import env_bootstrap as env

    calls: list[dict[str, object]] = []

    class Completion:
        def create(self, **kwargs):
            calls.append(kwargs)

    class OpenAI:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.chat = SimpleNamespace(completions=Completion())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=OpenAI))
    config = {"base_url": "https://provider.invalid/v1", "api_key": "secret", "model": "model"}
    monkeypatch.setattr(env, "get_translate_api_config", lambda: config)
    assert env.probe_translate_api(timeout=3)[0] is True
    assert calls[0]["timeout"] == 3
    assert calls[1]["model"] == "model"

    class FailingCompletion:
        def create(self, **_kwargs):
            raise RuntimeError("provider offline")

    class FailingOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FailingCompletion())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FailingOpenAI))
    ok, message = env.probe_translate_api()
    assert ok is False
    assert "provider offline" in message


def test_try_fix_ffmpeg_uses_winget_and_refreshes_path(monkeypatch):
    import env_bootstrap as env

    state = {"installed": False}
    commands: list[list[str]] = []
    refreshed: list[bool] = []

    def which(name: str):
        if name == "winget":
            return "winget.exe"
        if name in {"ffmpeg", "ffprobe"} and state["installed"]:
            return name + ".exe"
        return None

    monkeypatch.setattr(env, "safe_which", which)
    monkeypatch.setattr(env, "_system", lambda: "Windows")
    monkeypatch.setattr(env, "_prompt_yes", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(env, "_refresh_windows_path_from_machine", lambda: refreshed.append(True))
    monkeypatch.setattr(env, "prepend_tools_ffmpeg_to_path", lambda *_args, **_kwargs: None)

    def run(command, **_kwargs):
        commands.append(command)
        state["installed"] = True
        return 0

    monkeypatch.setattr(env, "_run_cmd", run)

    assert env.try_fix_ffmpeg() is True
    assert commands and commands[0][:3] == ["winget.exe", "install", "--id"]
    assert refreshed == [True]


def test_run_cmd_does_not_enable_shell_interpretation(monkeypatch):
    import env_bootstrap as env

    captured: dict[str, object] = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(env.subprocess, "run", run)
    assert env._run_cmd(["sudo", "apt-get", "update"]) == 0
    assert captured == {"command": ["sudo", "apt-get", "update"], "kwargs": {}}


def test_try_fix_ffmpeg_linux_updates_before_installing(monkeypatch):
    import env_bootstrap as env

    state = {"installed": False}
    commands: list[list[str]] = []

    def which(name: str):
        if name in {"ffmpeg", "ffprobe"} and state["installed"]:
            return "/usr/bin/" + name
        return None

    def run(command: list[str]) -> int:
        commands.append(command)
        if command[:3] == ["sudo", "apt-get", "install"]:
            state["installed"] = True
        return 0

    monkeypatch.setattr(env, "safe_which", which)
    monkeypatch.setattr(env, "_system", lambda: "Linux")
    monkeypatch.setattr(env, "_prompt_yes", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(env, "prepend_tools_ffmpeg_to_path", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(env, "_run_cmd", run)

    assert env.try_fix_ffmpeg() is True
    assert commands == [
        ["sudo", "apt-get", "update"],
        ["sudo", "apt-get", "install", "-y", "ffmpeg", "fonts-noto-cjk", "fonts-wqy-zenhei"],
    ]


def test_td_cli_manual_guide_creates_local_readme(tmp_path: Path, monkeypatch):
    import webbrowser

    import env_bootstrap as env
    import twitch_download as download

    answers = iter((False, True))
    opened: list[str] = []
    monkeypatch.setattr(env, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(env, "can_prompt_interactive", lambda: True)
    monkeypatch.setattr(env, "_prompt_yes", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(download, "find_twitchdownloader_cli", lambda: None)
    monkeypatch.setattr(download, "td_install_hints", lambda: (["manual install"], ["https://example.invalid/td"]))
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    assert env.offer_td_cli_guide() is True
    readme = tmp_path / "tools" / "TwitchDownloaderCLI" / "README.txt"
    assert readme.is_file()
    assert opened == ["https://example.invalid/td"]
