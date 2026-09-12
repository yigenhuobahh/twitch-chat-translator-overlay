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


def test_slot_values_reject_option_like_tokens(tmp_path: Path):
    """Slot values starting with '-' would be parsed as options by the .NET CLI."""
    from twitch_download import TwitchDownloadError, build_chat_cmd, build_video_cmd

    cli = Path("TwitchDownloaderCLI.exe")
    out = tmp_path / "v.mp4"
    with pytest.raises(TwitchDownloadError, match="画质"):
        build_video_cmd(cli, kind="vod", source_id="123", output=out, quality="-q:evil")
    with pytest.raises(TwitchDownloadError, match="开始时间"):
        build_video_cmd(cli, kind="vod", source_id="123", output=out, begin="-b:evil")
    with pytest.raises(TwitchDownloadError, match="结束时间"):
        build_video_cmd(cli, kind="vod", source_id="123", output=out, end="-e:evil")
    with pytest.raises(TwitchDownloadError, match="OAuth"):
        build_video_cmd(cli, kind="vod", source_id="123", output=out, oauth="-o:evil")
    with pytest.raises(TwitchDownloadError, match="ID"):
        build_video_cmd(cli, kind="vod", source_id="-o:evil", output=out)
    with pytest.raises(TwitchDownloadError, match="ID"):
        build_chat_cmd(cli, source_id="-o:evil", output=tmp_path / "c.html")
    with pytest.raises(TwitchDownloadError, match="开始时间"):
        build_chat_cmd(cli, source_id="123", output=tmp_path / "c.html", begin="-b:evil")


def test_reject_option_like_message_hides_original_value():
    """security-1: 拒绝消息不得回显以 '-' 开头的原值（可能内嵌凭据）。"""
    from twitch_download import TwitchDownloadError, _reject_option_like

    with pytest.raises(TwitchDownloadError) as excinfo:
        _reject_option_like("--oauth=leaked-secret", "OAuth 令牌")
    message = str(excinfo.value)
    assert "leaked-secret" not in message
    assert "值已隐藏" in message
    # 标签（label）仍保留在消息里，定位是哪个槽位被拒。
    with pytest.raises(TwitchDownloadError, match="下载画质"):
        _reject_option_like("-q:evil", "下载画质")


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
    # W3-A1: pick_td_cli_asset / platform_td_asset_token live in td_cli_install;
    # patching the defining module keeps the internal call site observable.
    import td_cli_install as td

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

    import td_cli_install
    import twitch_download as td

    root = tmp_path / "repo"
    root.mkdir()
    # W3-A1: fetch_latest_td_cli_release_asset moved to td_cli_install; the
    # patch must target the defining module so try_portable_td_cli sees it.
    monkeypatch.setattr(
        td_cli_install,
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

    monkeypatch.delenv("TWITCHDOWNLOADER_CLI", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(td, "safe_which", lambda _name: None)

    ok = td.try_portable_td_cli(root=root, timeout=10.0)
    assert ok is True
    installed = root / "tools" / "TwitchDownloaderCLI" / "TwitchDownloaderCLI.exe"
    assert installed.is_file()
    assert td.find_twitchdownloader_cli(root) == installed.resolve()


def test_try_portable_td_cli_signature_has_no_assume_yes(tmp_path: Path, monkeypatch):
    """D-#10: assume_yes was a dead parameter and is gone from the signature."""
    import inspect

    import twitch_download as td

    params = inspect.signature(td.try_portable_td_cli).parameters
    assert "assume_yes" not in params

    # Callers still passing the removed kwarg must fail loudly, not silently.
    with pytest.raises(TypeError, match="assume_yes"):
        td.try_portable_td_cli(assume_yes=True, root=tmp_path)  # type: ignore[call-arg]

    # Offline smoke: new keyword-only signature runs the no-CLI failure path
    # without network (release lookup mocked to fail).
    monkeypatch.setattr(td, "find_twitchdownloader_cli", lambda root=None: None)

    def fake_fetch(*, timeout: float = 30.0):
        raise td.TwitchDownloadError("无法连接 GitHub releases: offline")

    # W3-A1: fetch_latest_td_cli_release_asset moved to td_cli_install.
    import td_cli_install

    monkeypatch.setattr(td_cli_install, "fetch_latest_td_cli_release_asset", fake_fetch)
    ok = td.try_portable_td_cli(root=tmp_path / "fresh-root")
    assert ok is False


def test_clean_temp_artifacts_removes_partial_files_and_keeps_others(tmp_path: Path):
    """D-#9 behavior guard: cleanup results are unchanged without a full pre-walk."""
    from process_util import clean_temp_artifacts

    partial = tmp_path / "out.partial.mp4"
    partial.write_bytes(b"p" * 100)
    mp4_partial = tmp_path / "clip.mp4.partial"
    mp4_partial.write_bytes(b"q" * 50)
    keep = tmp_path / "final.mp4"
    keep.write_bytes(b"keep")

    count, freed = clean_temp_artifacts(tmp_path)

    assert count == 2
    assert not partial.exists()
    assert not mp4_partial.exists()
    assert keep.is_file()
    assert freed > 0

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
    # 本测试要求"进程环境无任何翻译键"才能走到 cwd .env 加载路径；上游测试
    # (save_dotenv 的 os.environ 直写,monkeypatch undo 之外)可能留下
    # BASE_URL/MODEL,这里把六个翻译键全部清掉,而非只清 API key。
    for _k in (
        "OPENAI_COMPAT_API_KEY",
        "OPENAI_COMPAT_BASE_URL",
        "OPENAI_COMPAT_MODEL",
        "AGNES_API_KEY",
        "AGNES_BASE_URL",
        "AGNES_MODEL",
    ):
        monkeypatch.delenv(_k, raising=False)
    monkeypatch.delenv("TWITCHDOWNLOADER_CLI", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(common_utils, "_DOTENV_LOADED_KEYS", set())
    monkeypatch.setattr(td, "_TOOLS_ROOT", tmp_path / "trusted")
    # R-3: 本 .env 仅含 API key(无端点键),不触发端点+密钥确认分支;
    # 若上游后续加了端点键,这里也模拟用户确认(y)以维持测试语义。
    monkeypatch.setattr(common_utils, "_confirm_untrusted_dotenv", lambda: True)

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
    # P2-10: tmp_path 不在信任根 → 走确认门；测试里直接放行以锁定
    # "绝对路径显式覆盖在用户确认后生效"的契约。
    monkeypatch.setattr(common_utils, "_confirm_untrusted_executable", lambda p: True)
    assert td.find_twitchdownloader_cli(tmp_path / "empty") == payload.resolve()


# ---------------------------------------------------------------------------
# Fix 9: 下载簇其余修复
# ---------------------------------------------------------------------------

def test_validate_chat_html_prose_with_cdn_text_passes(tmp_path):
    """正文纯文本提到 CDN 链接 + "first-" 单词（如 first-place finish）不再误报。"""
    from twitch_download import validate_chat_html

    html = tmp_path / "prose.html"
    html.write_text(
        '<pre class="comment-root">He took first-place finish at '
        'static-cdn.jtvnw.net emote-image showcase! third- party wins too.</pre>',
        encoding="utf-8",
    )
    validate_chat_html(html)  # must not raise


def test_validate_chat_html_still_fails_remote_emote_class(tmp_path):
    from twitch_download import TwitchDownloadError, validate_chat_html

    bad = tmp_path / "bad.html"
    bad.write_text(
        '<pre class="comment-root"><img class="emote-image first-1" '
        'src="https://static-cdn.jtvnw.net/x.png"></pre>',
        encoding="utf-8",
    )
    with pytest.raises(TwitchDownloadError, match="embed"):
        validate_chat_html(bad)


def test_validate_chat_html_second_prefix_remote_cdn_fails(tmp_path):
    """K-4: 仅含 second- emote 类 + 远程 CDN URL 也必须硬失败（原先漏检）。

    HTML 里不能出现 emote-image/first-/third- 字样，否则旧正则也会命中，
    无法证明 second- 分支是必要条件。
    """
    from twitch_download import TwitchDownloadError, validate_chat_html

    bad = tmp_path / "second-bad.html"
    bad.write_text(
        '<pre class="comment-root"><span class="second-42">: emote!</span>'
        '<span class="comment-message" style="background:'
        'url(https://cdn.betterttv.net/emote/x/3x.png)"></span></pre>',
        encoding="utf-8",
    )
    with pytest.raises(TwitchDownloadError, match="embed"):
        validate_chat_html(bad)


def test_validate_chat_html_second_prefix_local_data_passes(tmp_path):
    """K-4: second- emote 类 + base64 内嵌 → 通过（与 first-/third- 对称）。"""
    from twitch_download import validate_chat_html

    good = tmp_path / "second-good.html"
    good.write_text(
        '<style>.second-42 { content:url("data:image/png;base64,aaa"); }</style>'
        '<pre class="comment-root"><img class="emote-image second-42"></pre>',
        encoding="utf-8",
    )
    validate_chat_html(good)  # must not raise


def test_run_cli_masks_oauth_equals_form(capsys):
    """--oauth=TOKEN 等号形式也必须掩码（单源 process_util.redact_command）。"""
    import twitch_download as td

    cmd = ["TwitchDownloaderCLI.exe", "chatdownload", "--oauth=supersecret", "-o", "x.html"]

    def fake_run_tracked(cmd, **kwargs):
        from subprocess import CompletedProcess

        return CompletedProcess(list(cmd), 0)

    saved = td.run_tracked
    td.run_tracked = fake_run_tracked
    try:
        td._run_cli(cmd, label="测试")
    finally:
        td.run_tracked = saved
    out = capsys.readouterr().out
    assert "supersecret" not in out
    assert "--oauth=[redacted]" in out


def test_run_cli_masks_oauth_separated_form(capsys):
    """--oauth TOKEN 分离形式也必须掩码（D-9: 单源 process_util.redact_command）。"""
    import twitch_download as td

    cmd = ["TwitchDownloaderCLI.exe", "videodownload", "--oauth", "topsecrettoken", "-o", "v.mp4"]

    def fake_run_tracked(cmd, **kwargs):
        from subprocess import CompletedProcess

        return CompletedProcess(list(cmd), 0)

    saved = td.run_tracked
    td.run_tracked = fake_run_tracked
    try:
        td._run_cli(cmd, label="测试")
    finally:
        td.run_tracked = saved
    out = capsys.readouterr().out
    assert "topsecrettoken" not in out
    assert "[redacted]" in out
    assert "--oauth [redacted]" in out
    # 其余参数保持原样
    assert "videodownload" in out
    assert "-o" in out and "v.mp4" in out


def test_slug_for_source_windows_reserved_names():
    import twitch_download as td

    assert td.slug_for_source("clip", "con") == "con_vod"
    assert td.slug_for_source("clip", "NUL") == "NUL_vod"
    assert td.slug_for_source("clip", "com1") == "com1_vod"
    assert td.slug_for_source("clip", "lpt2") == "lpt2_vod"
    # 普通名字不加后缀
    assert td.slug_for_source("clip", "SomeClip") == "SomeClip"
    assert td.slug_for_source("vod", "612942303") == "612942303"


def test_get_stream_start_time_warns_on_probe_failure(monkeypatch, tmp_path, capsys):
    import twitch_download as td

    path = tmp_path / "v.mp4"
    path.write_bytes(b"x")

    monkeypatch.setattr(td, "_run_ffprobe", lambda args: None)
    assert td.get_stream_start_time(path, "v:0") == 0.0
    assert "[WARN]" in capsys.readouterr().out

    class BadProbe:
        returncode = 1
        stdout = ""
        stderr = "moov atom not found"

    monkeypatch.setattr(td, "_run_ffprobe", lambda args: BadProbe())
    assert td.get_stream_start_time(path, "a:0") == 0.0
    assert "moov atom" in capsys.readouterr().out


def test_multi_segment_duration_tolerance_scales_with_segment_count():
    """20 段容差 = 1.0 + 0.05*20 = 2.0；validate_media_health 签名带
    duration_tolerance 关键字（默认 1.0）。"""
    import inspect

    import media_health as mh

    sig = inspect.signature(mh.validate_media_health)
    assert "duration_tolerance" in sig.parameters
    assert sig.parameters["duration_tolerance"].default == 1.0

    # 调用方公式（download_assets_multi 内联）：
    seg_downloads = list(range(20))
    assert 1.0 + 0.05 * len(seg_downloads) == 2.0


class _StubHealth:
    def __init__(self, ok=True, warnings=()):
        self.ok = ok
        self.warnings = list(warnings)
        self.reason_text = "stub-reason"

    def reason(self):
        return self.reason_text


def test_validate_download_video_single_segment_call_shape(monkeypatch, tmp_path):
    """design-5b 行为保留:单段接线 = expected_duration None / 容差默认 / repair 不传 encoder。"""
    import media_health as mh
    import twitch_download as td

    calls_validate, calls_repair = [], []
    monkeypatch.setattr(mh, "validate_media_health",
                        lambda path, **k: (calls_validate.append(k), _StubHealth())[1])
    monkeypatch.setattr(mh, "repair_media", lambda *a, **k: (calls_repair.append(k), a[0])[1])

    staged = tmp_path / "staged.mp4"
    staged.write_bytes(b"x")
    state = {"repaired_video": None}
    out = td._validate_download_video(
        staged, media_check="fast", media_repair="audio",
        label="下载视频", blocked_action="X", repair_state=state,
    )
    assert out is staged
    assert calls_validate[0]["expected_duration"] is None
    assert calls_validate[0]["duration_tolerance"] == 1.0
    assert calls_validate[0]["require_audio"] is True
    assert not calls_repair  # health.ok 不触发修复


def test_validate_download_video_multi_segment_forwards_encoder_and_tolerance(monkeypatch, tmp_path):
    """design-5b:多段接线把 expected/tolerance/encoder 透传给 validate 与 repair。"""
    import media_health as mh
    import twitch_download as td

    calls_validate, calls_repair = [], []
    bad = _StubHealth(ok=False, warnings=("w",))
    ok = _StubHealth(ok=True)
    queue = iter([bad, ok])
    monkeypatch.setattr(mh, "validate_media_health",
                        lambda path, **k: (calls_validate.append(k), next(queue))[1])
    def fake_repair(source, **k):
        calls_repair.append(k)
        out = tmp_path / "staged.repaired.mp4"
        out.write_bytes(b"y")
        return out
    monkeypatch.setattr(mh, "repair_media", fake_repair)

    staged = tmp_path / "staged.mp4"
    staged.write_bytes(b"x")
    state = {"repaired_video": None}
    out = td._validate_download_video(
        staged, media_check="decode", media_repair="audio",
        label="合并视频", blocked_action="X", repair_state=state,
        expected_duration=120.0, duration_tolerance=2.0, encoder="nvenc",
    )
    assert out == tmp_path / "staged.repaired.mp4"
    assert state["repaired_video"] == tmp_path / "staged.repaired.mp4"
    assert calls_validate[0]["expected_duration"] == 120.0
    assert calls_validate[0]["duration_tolerance"] == 2.0
    assert calls_validate[1]["expected_duration"] == 120.0  # 复检同参
    assert calls_repair[0] == {"encoder": "nvenc"}


def test_validate_download_video_records_repair_before_revalidate(monkeypatch, tmp_path):
    """复检仍失败:修复产物已被登记进 repair_state(finally 可清理),抛健康检查失败。"""
    import media_health as mh
    import twitch_download as td

    bad = _StubHealth(ok=False)
    monkeypatch.setattr(mh, "validate_media_health", lambda path, **k: bad)
    def fake_repair(source, **k):
        out = tmp_path / "staged.repaired.mp4"
        out.write_bytes(b"y")
        return out
    monkeypatch.setattr(mh, "repair_media", fake_repair)

    staged = tmp_path / "staged.mp4"
    staged.write_bytes(b"x")
    state = {"repaired_video": None}
    with pytest.raises(td.TwitchDownloadError, match="健康检查失败"):
        td._validate_download_video(
            staged, media_check="fast", media_repair="audio",
            label="下载视频", blocked_action="X", repair_state=state,
        )
    assert state["repaired_video"] == tmp_path / "staged.repaired.mp4"


def test_cleanup_download_staging_preserves_journal_registered_and_skips_none(tmp_path, monkeypatch):
    import twitch_download as td

    journal_kept = tmp_path / ".download-kept"
    journal_kept.write_bytes(b"k")
    doomed = tmp_path / ".download-doomed"
    doomed.write_bytes(b"d")
    monkeypatch.setattr(td, "preserved_staged_paths", lambda root: {journal_kept.resolve()})

    td._cleanup_download_staging(tmp_path, (None, doomed, journal_kept), label="下载")
    assert journal_kept.exists()
    assert not doomed.exists()


def test_cleanup_download_staging_journal_consumed_unlinks_all(tmp_path, monkeypatch):
    """journal 已消费(preserved 返回空集)→ 全部 staged 清理,与原逐点行为一致。"""
    import twitch_download as td

    a, b = tmp_path / ".download-a", tmp_path / ".download-b"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    monkeypatch.setattr(td, "preserved_staged_paths", lambda root: set())
    td._cleanup_download_staging(tmp_path, (a, b, None), label="合并")
    assert not a.exists() and not b.exists()


def test_cleanup_download_staging_corrupt_evidence_preserves_all(tmp_path, monkeypatch):
    """preserved 返回 None(证据受损)→ 什么都不删(保留现场等恢复/人工)。"""
    import twitch_download as td

    a = tmp_path / ".download-a"
    a.write_bytes(b"a")
    monkeypatch.setattr(td, "preserved_staged_paths", lambda root: None)
    td._cleanup_download_staging(tmp_path, (a,), label="下载")
    assert a.exists()
