#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for TwitchDownloaderCLI wrapper (no network)."""

from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_parse_vod_url_and_bare_id():
    from twitch_download import parse_twitch_source

    k, i = parse_twitch_source("https://www.twitch.tv/videos/612942303")
    assert k == "vod"
    assert i == "612942303"
    k2, i2 = parse_twitch_source("612942303")
    assert k2 == "vod" and i2 == "612942303"


def test_parse_clip_url():
    from twitch_download import parse_twitch_source

    k, i = parse_twitch_source("https://clips.twitch.tv/SomeClipSlug-abc")
    assert k == "clip"
    assert "SomeClipSlug" in i


def test_parse_kind_hint_forces_clip():
    from twitch_download import parse_twitch_source

    k, i = parse_twitch_source("MyClipName", kind_hint="clip")
    assert k == "clip"


def test_build_video_and_chat_cmds_include_embed():
    from twitch_download import build_chat_cmd, build_video_cmd

    cli = Path("TwitchDownloaderCLI.exe")
    out = Path("v.mp4")
    v = build_video_cmd(
        cli, kind="vod", source_id="123", output=out, quality="720p60", begin="10s", end="20s"
    )
    assert "videodownload" in v
    assert "--id" in v and "123" in v
    assert "-q" in v and "720p60" in v
    assert "-b" in v and "10s" in v
    c = build_chat_cmd(cli, source_id="123", output=Path("c.html"), begin="10s", end="20s")
    assert "chatdownload" in c
    assert "-E" in c
    assert any(x.startswith("--bttv=") for x in c)
    clip = build_video_cmd(cli, kind="clip", source_id="slug", output=out)
    assert "clipdownload" in clip
    assert "-b" not in clip


def test_parse_td_time_and_segment_line_smoke():
    from twitch_download import format_td_t_seconds, parse_segment_line, parse_td_time

    assert parse_td_time("0:01:40") == 100.0
    assert format_td_t_seconds(100) == ("0h1m40s", "0:01:40")
    seg = parse_segment_line("1m0s 2m0s")
    assert seg is not None
    assert seg.begin_s == 60 and seg.end_s == 120


def test_validate_chat_html_ok_and_cdn_fail(tmp_path: Path):
    from twitch_download import TwitchDownloadError, validate_chat_html

    good = tmp_path / "good.html"
    good.write_text(
        '<style>.first-1 { content:url("data:image/png;base64,aaa"); }</style>'
        '<pre class="comment-root">hi</pre>',
        encoding="utf-8",
    )
    validate_chat_html(good)

    bad = tmp_path / "bad.html"
    bad.write_text(
        '<pre class="comment-root"><img class="emote-image first-1" '
        'src="https://static-cdn.jtvnw.net/x.png"></pre>',
        encoding="utf-8",
    )
    with pytest.raises(TwitchDownloadError, match="embed"):
        validate_chat_html(bad)


def test_download_assets_missing_cli(monkeypatch, tmp_path: Path):
    import twitch_download as td

    monkeypatch.setattr(td, "find_twitchdownloader_cli", lambda root=None: None)
    with pytest.raises(td.TwitchDownloadError, match="未找到"):
        td.download_assets("612942303", out_dir=tmp_path)


def test_download_assets_mocked_success(monkeypatch, tmp_path: Path):
    import twitch_download as td

    fake_cli = tmp_path / "TwitchDownloaderCLI.exe"
    fake_cli.write_bytes(b"x")
    monkeypatch.setattr(td, "find_twitchdownloader_cli", lambda root=None: fake_cli)

    def fake_run(cmd, **kwargs):
        # Create outputs on chat/video commands
        out = None
        if "-o" in cmd:
            out = Path(cmd[cmd.index("-o") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            if str(out).endswith(".html"):
                out.write_text(
                    '<style>.first-1{content:url("data:image/png;base64,aa");}</style>'
                    '<pre class="comment-root">x</pre>',
                    encoding="utf-8",
                )
            else:
                out.write_bytes(b"\x00\x00")

        class C:
            returncode = 0

        return C()

    monkeypatch.setattr(td, "run_tracked", fake_run)
    monkeypatch.setattr(td, "safe_which", lambda n: None)
    res = td.download_assets("612942303", out_dir=tmp_path / "dl", quality="720p", media_check="off")
    assert res.video_path.is_file()
    assert res.chat_html_path.is_file()
    assert res.kind == "vod"


def test_readiness_includes_twitchdownloader_key():
    from env_bootstrap import collect_readiness

    items = collect_readiness()
    keys = {i.key for i in items}
    assert "twitchdownloader" in keys
    td = next(i for i in items if i.key == "twitchdownloader")
    assert td.required_for_render is False


def test_pick_td_cli_asset_windows_and_linux():
    import twitch_download as td

    assets = [
        {"name": "TwitchDownloaderCLI-1.56.4-Linux-x64.zip", "browser_download_url": "http://x/linux"},
        {"name": "TwitchDownloaderCLI-1.56.4-LinuxArm64.zip", "browser_download_url": "http://x/arm64"},
        {"name": "TwitchDownloaderCLI-1.56.4-Windows-x64.zip", "browser_download_url": "http://x/win"},
        {"name": "TwitchDownloaderGUI-1.56.4-Windows-x64.zip", "browser_download_url": "http://x/gui"},
        {"name": "TwitchDownloaderCLI-1.56.4-MacOSArm64.zip", "browser_download_url": "http://x/mac"},
    ]
    # Force token via monkeypatch of platform_td_asset_token
    old = td.platform_td_asset_token
    try:
        td.platform_td_asset_token = lambda: "Windows-x64"  # type: ignore
        picked = td.pick_td_cli_asset(assets)
        assert picked is not None
        assert "Windows-x64" in picked["name"]
        assert "GUI" not in picked["name"]

        td.platform_td_asset_token = lambda: "LinuxArm64"  # type: ignore
        picked = td.pick_td_cli_asset(assets)
        assert picked is not None
        assert "LinuxArm64" in picked["name"]

        td.platform_td_asset_token = lambda: "LinuxArm"  # type: ignore
        # No bare LinuxArm in list — should not match Arm64
        assets2 = assets + [
            {"name": "TwitchDownloaderCLI-1.56.4-LinuxArm.zip", "browser_download_url": "http://x/arm"}
        ]
        picked = td.pick_td_cli_asset(assets2)
        assert picked is not None
        assert "LinuxArm64" not in picked["name"]
        assert "LinuxArm" in picked["name"]
    finally:
        td.platform_td_asset_token = old  # type: ignore


def test_try_portable_td_cli_extracts_zip(tmp_path: Path, monkeypatch):
    """Offline: mock GitHub asset + zip contents, ensure exe is found after install."""
    import io
    import zipfile as zfmod

    import twitch_download as td

    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(
        td,
        "fetch_latest_td_cli_release_asset",
        lambda timeout=30.0: ("1.56.4", "TwitchDownloaderCLI-fake.zip", "http://example.test/cli.zip"),
    )

    buf = io.BytesIO()
    with zfmod.ZipFile(buf, "w") as zf:
        zf.writestr("TwitchDownloaderCLI-fake/TwitchDownloaderCLI.exe", b"MZ-fake")
    payload = buf.getvalue()

    class FakeResp:
        def __init__(self, data: bytes):
            self._data = data
            self._i = 0

        def read(self, n: int = -1):
            if self._i >= len(self._data):
                return b""
            if n < 0:
                chunk = self._data[self._i :]
                self._i = len(self._data)
                return chunk
            chunk = self._data[self._i : self._i + n]
            self._i += len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        return FakeResp(payload)

    import urllib.request as ur

    monkeypatch.setattr(ur, "urlopen", fake_urlopen)

    calls = {"n": 0}

    def find_side(root=None):
        if calls["n"] == 0:
            calls["n"] = 1
            return None
        base = Path(root or td._REPO_ROOT) / "tools" / "TwitchDownloaderCLI"
        if not base.is_dir():
            return None
        for name in td.td_exe_names():
            for hit in base.rglob(name):
                if hit.is_file():
                    return hit.resolve()
        return None

    monkeypatch.setattr(td, "find_twitchdownloader_cli", find_side)
    monkeypatch.setattr(td, "prepend_tools_td_to_path", lambda root=None: None)

    ok = td.try_portable_td_cli(assume_yes=True, root=root, timeout=10.0)
    assert ok is True
    found_any = list((root / "tools" / "TwitchDownloaderCLI").rglob("TwitchDownloaderCLI.exe"))
    assert found_any, "exe should exist under tools/TwitchDownloaderCLI"

def test_installed_defaults_ignore_untrusted_cwd_tools(tmp_path: Path, monkeypatch):
    import twitch_download as td

    cwd = tmp_path / "untrusted media"
    trusted = tmp_path / "trusted app data"
    fake_dir = cwd / "tools" / "TwitchDownloaderCLI"
    fake_dir.mkdir(parents=True)
    for name in td.td_exe_names():
        (fake_dir / name).write_bytes(b"not executable")
        (cwd / name).write_bytes(b"untrusted cwd executable")

    monkeypatch.chdir(cwd)
    monkeypatch.setattr(td, "_REPO_ROOT", cwd.resolve())
    monkeypatch.setattr(td, "_TOOLS_ROOT", trusted.resolve())
    monkeypatch.delenv("TWITCHDOWNLOADER_CLI", raising=False)
    monkeypatch.setenv("PATH", "")

    assert td.default_download_dir() == cwd.resolve() / "downloads"
    assert td.find_twitchdownloader_cli() is None
    assert all(cwd.resolve() not in path.parents for path in td.tools_td_bin_dirs())


def test_download_uses_trusted_tool_root_separate_from_output_root(
    tmp_path: Path, monkeypatch
):
    import twitch_download as td

    app_root = tmp_path / "output cwd"
    tools_root = tmp_path / "trusted tools"
    observed = []

    monkeypatch.setattr(td, "_REPO_ROOT", app_root)
    monkeypatch.setattr(td, "_TOOLS_ROOT", tools_root)
    monkeypatch.setattr(
        td,
        "find_twitchdownloader_cli",
        lambda root=None: observed.append(root) or None,
    )

    with pytest.raises(td.TwitchDownloadError, match="TwitchDownloaderCLI"):
        td.download_assets("2819850140", root=app_root / "explicit output")

    assert observed == [tools_root]
    assert observed[0] != app_root

def test_dotenv_only_loads_translation_keys_and_cannot_override_executable(
    tmp_path: Path, monkeypatch
):
    import common_utils
    import twitch_download as td

    payload = tmp_path / td.td_exe_names()[0]
    payload.write_bytes(b"untrusted")
    (tmp_path / ".env").write_text(
        "OPENAI_COMPAT_API_KEY=test-key\n"
        f"TWITCHDOWNLOADER_CLI={payload}\n"
        f"EDITOR={payload}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("_TWITCH_TRANSPARENT_TEST_MODE", raising=False)
    monkeypatch.delenv("OPENAI_COMPAT_API_KEY", raising=False)
    monkeypatch.delenv("TWITCHDOWNLOADER_CLI", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(common_utils, "_DOTENV_LOADED_KEYS", set())
    monkeypatch.setattr(td, "_TOOLS_ROOT", tmp_path / "trusted")

    common_utils.load_dotenv_if_present()

    assert os.environ["OPENAI_COMPAT_API_KEY"] == "test-key"
    assert "TWITCHDOWNLOADER_CLI" not in os.environ
    assert "EDITOR" not in os.environ
    assert td.find_twitchdownloader_cli() is None


def test_process_environment_cli_override_requires_absolute_path(
    tmp_path: Path, monkeypatch
):
    import common_utils
    import twitch_download as td

    payload = tmp_path / td.td_exe_names()[0]
    payload.write_bytes(b"explicit")
    monkeypatch.setattr(common_utils, "_DOTENV_LOADED_KEYS", set())
    monkeypatch.setenv("TWITCHDOWNLOADER_CLI", payload.name)
    monkeypatch.chdir(tmp_path)
    assert td.find_twitchdownloader_cli(tmp_path / "empty") is None

    monkeypatch.setenv("TWITCHDOWNLOADER_CLI", str(payload.resolve()))
    assert td.find_twitchdownloader_cli(tmp_path / "empty") == payload.resolve()
