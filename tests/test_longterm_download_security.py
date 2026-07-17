#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Security and durability tests for downloaded portable tools and assets."""

from __future__ import annotations

import io
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import urllib.request
import zipfile

import pytest


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def make_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def test_safe_extract_zip_rejects_path_traversal_before_writing(tmp_path: Path):
    import env_bootstrap as env

    buffer = io.BytesIO(make_zip({"../outside.exe": b"bad", "ok.exe": b"ok"}))
    with zipfile.ZipFile(buffer) as archive:
        with pytest.raises(ValueError, match="unsafe archive member"):
            env.safe_extract_zip(archive, tmp_path / "extract")

    assert not (tmp_path / "outside.exe").exists()
    assert not (tmp_path / "extract" / "ok.exe").exists()


def test_safe_extract_zip_rejects_symlink_and_expansion_budget(tmp_path: Path):
    import env_bootstrap as env

    symlink_buffer = io.BytesIO()
    with zipfile.ZipFile(symlink_buffer, "w") as archive:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "target")
    symlink_buffer.seek(0)
    with zipfile.ZipFile(symlink_buffer) as archive:
        with pytest.raises(ValueError, match="symlink"):
            env.safe_extract_zip(archive, tmp_path / "symlink")

    size_buffer = io.BytesIO(make_zip({"large.bin": b"12345"}))
    with zipfile.ZipFile(size_buffer) as archive:
        with pytest.raises(ValueError, match="allowed size"):
            env.safe_extract_zip(
                archive,
                tmp_path / "oversize",
                max_uncompressed_bytes=4,
            )


def test_stream_download_rejects_oversized_content_length(tmp_path: Path):
    import env_bootstrap as env

    response = FakeResponse(b"123456")
    response.headers = {"Content-Length": "6"}
    destination = tmp_path / "download.zip"
    with pytest.raises(ValueError, match="allowed size"):
        env.stream_response_to_path(response, destination, max_bytes=5)
    assert not destination.exists()


def test_atomic_directory_replace_restores_previous_install_on_failure(
    tmp_path: Path, monkeypatch
):
    import env_bootstrap as env

    destination = tmp_path / "tool"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    staged = tmp_path / "ready"
    staged.mkdir()
    (staged / "new.txt").write_text("new", encoding="utf-8")

    real_replace = os.replace

    def fail_final_replace(source, target):
        if Path(source) == staged and Path(target) == destination:
            raise OSError("simulated final rename failure")
        return real_replace(source, target)

    monkeypatch.setattr(env.os, "replace", fail_final_replace)
    with pytest.raises(OSError, match="simulated"):
        env.atomic_replace_directory(staged, destination)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (destination / "new.txt").exists()


def test_failed_ffmpeg_archive_does_not_replace_existing_install(
    tmp_path: Path, monkeypatch
):
    import env_bootstrap as env

    root = tmp_path / "root"
    destination = root / "tools" / "ffmpeg"
    destination.mkdir(parents=True)
    marker = destination / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    payload = make_zip({"../escape.exe": b"bad"})

    monkeypatch.setattr(env, "safe_which", lambda _name: None)
    monkeypatch.setattr(env, "prepend_tools_ffmpeg_to_path", lambda _root=None: None)
    monkeypatch.setattr(env, "_system", lambda: "Windows")
    monkeypatch.setattr(env, "urlopen", lambda *_args, **_kwargs: FakeResponse(payload))

    assert env.try_portable_ffmpeg(assume_yes=True, root=root) is False
    assert marker.read_text(encoding="utf-8") == "keep"
    assert not (root / "tools" / "escape.exe").exists()


def test_failed_td_archive_does_not_replace_existing_install(
    tmp_path: Path, monkeypatch
):
    import twitch_download as td

    root = tmp_path / "root"
    destination = root / "tools" / "TwitchDownloaderCLI"
    destination.mkdir(parents=True)
    marker = destination / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    payload = make_zip({"../escape.exe": b"bad"})

    monkeypatch.setattr(td, "find_twitchdownloader_cli", lambda root=None: None)
    monkeypatch.setattr(
        td,
        "fetch_latest_td_cli_release_asset",
        lambda timeout=30.0: ("test", "asset.zip", "https://example.test/asset.zip"),
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(payload),
    )

    assert td.try_portable_td_cli(assume_yes=True, root=root) is False
    assert marker.read_text(encoding="utf-8") == "keep"
    assert not (root / "tools" / "escape.exe").exists()


def test_default_download_session_names_are_collision_resistant(
    tmp_path: Path, monkeypatch
):
    import twitch_download as td

    monkeypatch.setattr(td.time, "strftime", lambda _format: "20260717_120000")
    first = td.new_download_session_dir(tmp_path, "2819850140")
    second = td.new_download_session_dir(tmp_path, "2819850140")

    assert first != second
    assert first.parent == td.default_download_dir(tmp_path)
    assert first.name.startswith("2819850140_20260717_120000_")


def _install_fake_download_cli(td, tmp_path: Path, monkeypatch, *, create_video: bool):
    cli = tmp_path / "TwitchDownloaderCLI.exe"
    cli.write_bytes(b"fake")
    monkeypatch.setattr(td, "find_twitchdownloader_cli", lambda root=None: cli)
    monkeypatch.setattr(td, "safe_which", lambda _name: None)

    def fake_run(command, **_kwargs):
        output = Path(command[command.index("-o") + 1])
        if create_video and output.suffix == ".mp4":
            output.write_bytes(b"\x00\x00")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(td, "run_tracked", fake_run)


def test_download_does_not_reuse_arbitrary_stale_video(tmp_path: Path, monkeypatch):
    import twitch_download as td

    output = tmp_path / "download"
    output.mkdir()
    (output / "stale.mp4").write_bytes(b"stale")
    _install_fake_download_cli(td, tmp_path, monkeypatch, create_video=False)

    with pytest.raises(td.TwitchDownloadError, match="指定文件"):
        td.download_assets("2819850140", out_dir=output, media_check="off")


def test_download_does_not_reuse_arbitrary_stale_chat(tmp_path: Path, monkeypatch):
    import twitch_download as td

    output = tmp_path / "download"
    output.mkdir()
    (output / "stale.html").write_text(
        '<pre class="comment-root">stale</pre>',
        encoding="utf-8",
    )
    _install_fake_download_cli(td, tmp_path, monkeypatch, create_video=True)

    with pytest.raises(td.TwitchDownloadError, match="指定文件"):
        td.download_assets("2819850140", out_dir=output, media_check="off")

def test_ffprobe_helpers_handle_timeout(tmp_path: Path, monkeypatch):
    import subprocess

    import twitch_download as td

    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("ffprobe", td._FFPROBE_TIMEOUT_SECONDS)

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(td.TwitchDownloadError, match="超时"):
        td.probe_media_duration(video)
    assert td.get_stream_start_time(video, "v:0") == 0.0
    assert td.probe_av_fingerprint(video) == ("", "", "", "", "", "")

def test_valid_ffmpeg_archive_atomically_replaces_previous_install(
    tmp_path: Path, monkeypatch
):
    import env_bootstrap as env

    root = tmp_path / "root"
    destination = root / "tools" / "ffmpeg"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("old", encoding="utf-8")
    payload = make_zip(
        {
            "ffmpeg-release/bin/ffmpeg.exe": b"ffmpeg",
            "ffmpeg-release/bin/ffprobe.exe": b"ffprobe",
        }
    )

    def locate(_root=None):
        binary_dir = destination / "ffmpeg-release" / "bin"
        if (binary_dir / "ffmpeg.exe").is_file() and (
            binary_dir / "ffprobe.exe"
        ).is_file():
            return str(binary_dir)
        return None

    monkeypatch.setattr(env, "safe_which", lambda _name: None)
    monkeypatch.setattr(env, "prepend_tools_ffmpeg_to_path", locate)
    monkeypatch.setattr(env, "_system", lambda: "Windows")
    monkeypatch.setattr(env, "urlopen", lambda *_args, **_kwargs: FakeResponse(payload))

    assert env.try_portable_ffmpeg(assume_yes=True, root=root) is True
    assert not (destination / "old.txt").exists()
    assert (destination / "ffmpeg-release" / "bin" / "ffmpeg.exe").is_file()
    assert (destination / "ffmpeg-release" / "bin" / "ffprobe.exe").is_file()

def test_release_metadata_rejects_untrusted_asset_url(monkeypatch):
    import json

    import twitch_download as td

    metadata = {
        "tag_name": "test",
        "assets": [
            {
                "name": "TwitchDownloaderCLI-test-Windows-x64.zip",
                "browser_download_url": "https://evil.example/asset.zip",
            }
        ],
    }
    monkeypatch.setattr(td, "platform_td_asset_token", lambda: "Windows-x64")
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(json.dumps(metadata).encode("utf-8")),
    )

    with pytest.raises(td.TwitchDownloadError, match="GitHub 下载路径"):
        td.fetch_latest_td_cli_release_asset()

def test_non_finite_time_and_media_values_are_rejected(
    tmp_path: Path,
    monkeypatch,
):
    import media_health
    import twitch_download as td

    with pytest.raises(td.TwitchDownloadError, match="有限"):
        td.parse_td_time("nan:1")
    with pytest.raises(td.TwitchDownloadError, match="有限"):
        td.format_td_t_seconds(float("inf"))
    with pytest.raises(td.TwitchDownloadError, match="有限"):
        td.normalize_cut_ranges([(float("nan"), 2.0)], 10.0)

    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    monkeypatch.setattr(
        td,
        "_run_ffprobe",
        lambda _arguments: SimpleNamespace(
            returncode=0,
            stdout="nan\n",
            stderr="",
        ),
    )
    with pytest.raises(td.TwitchDownloadError, match="时长无效"):
        td.probe_media_duration(video)
    assert td.get_stream_start_time(video, "v:0") == 0.0
    assert media_health._number("nan") == 0.0
    assert media_health._number("inf") == 0.0

def test_download_does_not_reuse_canonical_stale_video(
    tmp_path: Path,
    monkeypatch,
):
    import twitch_download as td

    output = tmp_path / "download"
    output.mkdir()
    stale_video = output / "video.mp4"
    stale_video.write_bytes(b"old-video")
    _install_fake_download_cli(td, tmp_path, monkeypatch, create_video=False)

    with pytest.raises(td.TwitchDownloadError, match="新的指定文件"):
        td.download_assets("2819850140", out_dir=output, media_check="off")

    assert stale_video.read_bytes() == b"old-video"
    assert not any(".download-" in path.name for path in output.iterdir())


def test_download_keeps_existing_pair_when_fresh_chat_is_missing(
    tmp_path: Path,
    monkeypatch,
):
    import twitch_download as td

    output = tmp_path / "download"
    output.mkdir()
    stale_video = output / "video.mp4"
    stale_chat = output / "chat.html"
    stale_video.write_bytes(b"old-video")
    stale_chat.write_text(
        '<pre class="comment-root">old-chat</pre>',
        encoding="utf-8",
    )
    _install_fake_download_cli(td, tmp_path, monkeypatch, create_video=True)

    with pytest.raises(td.TwitchDownloadError, match="新的指定文件"):
        td.download_assets("2819850140", out_dir=output, media_check="off")

    assert stale_video.read_bytes() == b"old-video"
    assert stale_chat.read_text(encoding="utf-8").endswith("old-chat</pre>")
    assert not any(".download-" in path.name for path in output.iterdir())


def test_deep_td_archive_layout_does_not_replace_existing_install(
    tmp_path: Path,
    monkeypatch,
):
    import twitch_download as td

    root = tmp_path / "root"
    destination = root / "tools" / "TwitchDownloaderCLI"
    destination.mkdir(parents=True)
    marker = destination / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    payload = make_zip(
        {"outer/inner/TwitchDownloaderCLI.exe": b"MZ-fake"}
    )

    monkeypatch.setattr(td, "find_twitchdownloader_cli", lambda root=None: None)
    monkeypatch.setattr(
        td,
        "fetch_latest_td_cli_release_asset",
        lambda timeout=30.0: (
            "test",
            "asset.zip",
            "https://github.com/lay295/TwitchDownloader/releases/download/test/asset.zip",
        ),
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(payload),
    )

    assert td.try_portable_td_cli(assume_yes=True, root=root) is False
    assert marker.read_text(encoding="utf-8") == "keep"
    assert not (destination / "outer").exists()


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ([], "根节点"),
        ({"tag_name": "test", "assets": [None]}, "asset 项"),
    ],
)
def test_release_metadata_rejects_malformed_schema(
    metadata,
    message: str,
    monkeypatch,
):
    import json

    import twitch_download as td

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(
            json.dumps(metadata).encode("utf-8")
        ),
    )

    with pytest.raises(td.TwitchDownloadError, match=message):
        td.fetch_latest_td_cli_release_asset()

    assert td.pick_td_cli_asset([None]) is None

def test_download_pair_publish_restores_old_files_on_second_replace_failure(
    tmp_path: Path,
    monkeypatch,
):
    import twitch_download as td

    video = tmp_path / "video.mp4"
    chat = tmp_path / "chat.html"
    staged_video = tmp_path / "fresh-video.mp4"
    staged_chat = tmp_path / "fresh-chat.html"
    video.write_bytes(b"old-video")
    chat.write_bytes(b"old-chat")
    staged_video.write_bytes(b"new-video")
    staged_chat.write_bytes(b"new-chat")
    real_replace = os.replace

    def fail_chat_publish(source, destination):
        if Path(source) == staged_chat and Path(destination) == chat:
            raise OSError("simulated chat publish failure")
        return real_replace(source, destination)

    monkeypatch.setattr(td.os, "replace", fail_chat_publish)

    with pytest.raises(td.TwitchDownloadError, match="旧文件已恢复"):
        td._publish_download_pair(staged_video, video, staged_chat, chat)

    assert video.read_bytes() == b"old-video"
    assert chat.read_bytes() == b"old-chat"
    assert not any(".backup-" in path.name for path in tmp_path.iterdir())