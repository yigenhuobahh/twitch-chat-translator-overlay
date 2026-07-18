"""Crash-recovery contracts for paired Twitch video/chat publication."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


class SimulatedCrash(BaseException):
    """Fault injection that intentionally bypasses ``except Exception``."""


@pytest.fixture
def td_module():
    import twitch_download_transaction as td

    with td._ACTIVE_DOWNLOAD_TRANSACTION_LOCK:
        td._ACTIVE_DOWNLOAD_TRANSACTIONS.clear()
    yield td
    with td._ACTIVE_DOWNLOAD_TRANSACTION_LOCK:
        td._ACTIVE_DOWNLOAD_TRANSACTIONS.clear()


def _pair_paths(root: Path) -> tuple[Path, Path, Path, Path]:
    root.mkdir()
    video = root / "video.mp4"
    chat = root / "chat.html"
    staged_video = root / ".video.mp4.download-test.mp4"
    staged_chat = root / ".chat.html.download-test.html"
    video.write_bytes(b"old-video")
    chat.write_bytes(b"old-chat")
    staged_video.write_bytes(b"new-video")
    staged_chat.write_bytes(b"new-chat")
    return video, chat, staged_video, staged_chat


def _leave_prepared_transaction(
    td,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    crash_point: str = "publish",
) -> tuple[Path, Path, Path, Path, str]:
    video, chat, staged_video, staged_chat = _pair_paths(root)
    real_replace = td.os.replace

    def crashing_replace(source, destination):
        source_path = Path(source).resolve(strict=False)
        destination_path = Path(destination).resolve(strict=False)
        real_replace(source, destination)
        backup_hit = (
            crash_point == "backup"
            and source_path == video.resolve(strict=False)
            and destination_path.name.startswith(".video.mp4.backup-")
        )
        publish_hit = (
            crash_point == "publish"
            and source_path == staged_video.resolve(strict=False)
            and destination_path == video.resolve(strict=False)
        )
        if backup_hit or publish_hit:
            raise SimulatedCrash(crash_point)

    with monkeypatch.context() as patch:
        patch.setattr(td.os, "replace", crashing_replace)
        with pytest.raises(SimulatedCrash, match=crash_point):
            td._publish_download_pair(
                staged_video,
                video,
                staged_chat,
                chat,
                transaction_root=root,
            )

    claim = json.loads(td._download_transaction_claim_path(root).read_text(encoding="utf-8"))
    transaction_id = claim["transaction_id"]
    assert not td._download_transaction_is_active(transaction_id)
    assert td._download_transaction_journal_path(root).is_file()
    return video, chat, staged_video, staged_chat, transaction_id


@pytest.mark.parametrize("crash_point", ["backup", "publish"])
def test_prepared_crash_propagates_and_next_run_restores_old_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    td_module,
    crash_point: str,
):
    td = td_module
    root = tmp_path / "download"
    video, chat, staged_video, staged_chat, _txid = _leave_prepared_transaction(
        td, root, monkeypatch, crash_point=crash_point
    )

    assert td._recover_download_transaction(root) == "prepared"
    assert video.read_bytes() == b"old-video"
    assert chat.read_bytes() == b"old-chat"
    assert not staged_video.exists()
    assert not staged_chat.exists()
    assert not td._download_transaction_journal_path(root).exists()
    assert not td._download_transaction_claim_path(root).exists()
    assert not list(root.glob(".*.backup-*"))


def test_committed_crash_finishes_cleanup_without_rolling_back_new_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    td_module,
):
    td = td_module
    root = tmp_path / "download"
    video, chat, staged_video, staged_chat = _pair_paths(root)
    real_recover = td._recover_download_transaction
    real_recover_locked = td._recover_download_transaction_locked

    def crash_before_committed_cleanup(root_path, *, expected_transaction_id=None):
        payload = json.loads(td._download_transaction_journal_path(Path(root_path)).read_text(encoding="utf-8"))
        if expected_transaction_id is not None and payload.get("state") == "committed":
            raise SimulatedCrash("committed")
        return real_recover_locked(root_path, expected_transaction_id=expected_transaction_id)

    with monkeypatch.context() as patch:
        patch.setattr(td, "_recover_download_transaction_locked", crash_before_committed_cleanup)
        with pytest.raises(SimulatedCrash, match="committed"):
            td._publish_download_pair(
                staged_video,
                video,
                staged_chat,
                chat,
                transaction_root=root,
            )

    payload = json.loads(td._download_transaction_journal_path(root).read_text(encoding="utf-8"))
    assert payload["state"] == "committed"
    first_backup = root / payload["entries"][0]["backup"]
    first_backup.unlink()

    assert real_recover(root) == "committed"
    assert video.read_bytes() == b"new-video"
    assert chat.read_bytes() == b"new-chat"
    assert not td._download_transaction_journal_path(root).exists()
    assert not td._download_transaction_claim_path(root).exists()
    assert not list(root.glob(".*.backup-*"))


def test_manual_replacement_mismatch_fails_closed_and_preserves_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    td_module,
):
    td = td_module
    root = tmp_path / "download"
    video, _chat, _staged_video, _staged_chat, _txid = _leave_prepared_transaction(td, root, monkeypatch)
    video.write_bytes(b"manual-replacement")

    with pytest.raises(td.TwitchDownloadError, match="签名不匹配"):
        td._recover_download_transaction(root)

    assert video.read_bytes() == b"manual-replacement"
    assert td._download_transaction_journal_path(root).is_file()
    assert td._download_transaction_claim_path(root).is_file()


def test_path_escape_in_journal_fails_closed_without_touching_outside_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    td_module,
):
    td = td_module
    root = tmp_path / "download"
    _leave_prepared_transaction(td, root, monkeypatch)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    journal = td._download_transaction_journal_path(root)
    payload = json.loads(journal.read_text(encoding="utf-8"))
    payload["entries"][0]["destination"] = "../outside.txt"
    journal.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(td.TwitchDownloadError, match="路径逃逸"):
        td._recover_download_transaction(root)

    assert outside.read_text(encoding="utf-8") == "keep"
    assert journal.is_file()
    assert td._download_transaction_claim_path(root).is_file()


@pytest.mark.parametrize("claim_bytes", [b"", b"{"])
def test_partial_claim_without_journal_is_cleaned_under_guard(
    tmp_path: Path,
    td_module,
    claim_bytes: bytes,
):
    td = td_module
    root = tmp_path / "download"
    root.mkdir()
    claim_path = td._download_transaction_claim_path(root)
    claim_path.write_bytes(claim_bytes)
    journal = td._download_transaction_journal_path(root)
    journal_temporary = journal.with_name(f".{journal.name}.tmp-{'d' * 32}")
    journal_temporary.write_bytes(b"partial")

    assert td._recover_download_transaction(root) is None

    assert not claim_path.exists()
    assert not journal_temporary.exists()


def test_prepared_recovery_ignores_reused_live_owner_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    td_module,
):
    td = td_module
    root = tmp_path / "download"
    video, chat, _staged_video, _staged_chat, _txid = _leave_prepared_transaction(td, root, monkeypatch)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    try:
        assert sleeper.poll() is None
        for evidence in (
            td._download_transaction_claim_path(root),
            td._download_transaction_journal_path(root),
        ):
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            payload["owner_pid"] = sleeper.pid
            evidence.write_text(json.dumps(payload), encoding="utf-8")

        assert td._recover_download_transaction(root) == "prepared"
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)

    assert video.read_bytes() == b"old-video"
    assert chat.read_bytes() == b"old-chat"
    assert not td._download_transaction_journal_path(root).exists()
    assert not td._download_transaction_claim_path(root).exists()


def test_exclusive_claim_blocks_second_publisher_and_active_recovery(
    tmp_path: Path,
    td_module,
):
    td = td_module
    root = tmp_path / "download"
    root.mkdir()
    first = "b" * 32
    second = "c" * 32
    td._claim_download_transaction(root, first, os.getpid(), time.time_ns())
    try:
        with pytest.raises(td.TwitchDownloadError, match="独占"):
            td._claim_download_transaction(root, second, os.getpid(), time.time_ns())
        assert td._download_transaction_is_active(first)
        assert not td._download_transaction_is_active(second)
        with pytest.raises(td.TwitchDownloadError, match="活动状态"):
            td._recover_download_transaction(root)
    finally:
        td._unregister_active_download_transaction(first)
        td._download_transaction_claim_path(root).unlink(missing_ok=True)


@pytest.mark.parametrize("operation", ["recover", "publish"])
def test_cross_process_guard_serializes_recovery_and_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    td_module,
    operation: str,
):
    td = td_module
    root = tmp_path / "download"
    root.mkdir()
    ready = tmp_path / "guard-ready"
    release = tmp_path / "guard-release"
    scripts_dir = Path(td.__file__).resolve().parent
    child_code = """
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import twitch_download_transaction as td

root = Path(sys.argv[2])
ready = Path(sys.argv[3])
release = Path(sys.argv[4])
with td._download_transaction_guard(root):
    ready.write_text("ready", encoding="utf-8")
    while not release.exists():
        time.sleep(0.01)
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child_code,
            str(scripts_dir),
            str(root),
            str(ready),
            str(release),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    video = root / "video.mp4"
    chat = root / "chat.html"
    staged_video = root / ".video.mp4.download-test.mp4"
    staged_chat = root / ".chat.html.download-test.html"
    if operation == "publish":
        video.write_bytes(b"old-video")
        chat.write_bytes(b"old-chat")
        staged_video.write_bytes(b"new-video")
        staged_chat.write_bytes(b"new-chat")

    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if not ready.exists():
            stdout, stderr = process.communicate(timeout=1)
            pytest.fail(f"guard helper did not start: stdout={stdout!r}, stderr={stderr!r}")

        monkeypatch.setattr(td, "_DOWNLOAD_TRANSACTION_GUARD_WAIT_SECONDS", 0.15)
        with pytest.raises(td.TwitchDownloadError, match="另一个进程"):
            if operation == "recover":
                td._recover_download_transaction(root)
            else:
                td._publish_download_pair(
                    staged_video,
                    video,
                    staged_chat,
                    chat,
                    transaction_root=root,
                )
        if operation == "publish":
            assert video.read_bytes() == b"old-video"
            assert chat.read_bytes() == b"old-chat"
            assert staged_video.read_bytes() == b"new-video"
            assert staged_chat.read_bytes() == b"new-chat"
    finally:
        release.write_text("release", encoding="utf-8")
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    assert process.returncode == 0


@pytest.mark.parametrize("destination_existed", [False, True])
def test_claim_holder_revalidates_destination_before_writing_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    td_module,
    destination_existed: bool,
):
    td = td_module
    root = tmp_path / "download"
    root.mkdir()
    video = root / "video.mp4"
    chat = root / "chat.html"
    if destination_existed:
        video.write_bytes(b"old-video")
        chat.write_bytes(b"old-chat")
    staged_video = root / ".video.mp4.download-test.mp4"
    staged_chat = root / ".chat.html.download-test.html"
    staged_video.write_bytes(b"new-video")
    staged_chat.write_bytes(b"new-chat")
    real_claim = td._claim_download_transaction

    def claim_then_change_destination(root_path, transaction_id, owner_pid, created_ns):
        real_claim(root_path, transaction_id, owner_pid, created_ns)
        video.write_bytes(b"competing-video")

    monkeypatch.setattr(td, "_claim_download_transaction", claim_then_change_destination)

    with pytest.raises(td.TwitchDownloadError, match="取得发布独占权后发生变化"):
        td._publish_download_pair(
            staged_video,
            video,
            staged_chat,
            chat,
            transaction_root=root,
        )

    assert video.read_bytes() == b"competing-video"
    assert not td._download_transaction_journal_path(root).exists()
    assert not td._download_transaction_claim_path(root).exists()


@pytest.mark.parametrize(
    "reserved_name",
    [
        ".twitch-download-publish.json",
        ".twitch-download-publish.lock",
        ".twitch-download-publish.guard",
    ],
)
def test_transaction_metadata_paths_cannot_be_output_targets(
    tmp_path: Path,
    td_module,
    reserved_name: str,
):
    td = td_module
    root = tmp_path / "download"
    root.mkdir()
    staged_video = root / ".video.mp4.download-test.mp4"
    staged_chat = root / ".chat.html.download-test.html"
    staged_video.write_bytes(b"new-video")
    staged_chat.write_bytes(b"new-chat")

    with pytest.raises(td.TwitchDownloadError, match="事务元数据冲突"):
        td._publish_download_pair(
            staged_video,
            root / reserved_name,
            staged_chat,
            root / "chat.html",
            transaction_root=root,
        )

    assert not td._download_transaction_journal_path(root).exists()
    assert not td._download_transaction_claim_path(root).exists()


def test_successful_pair_publish_removes_all_transaction_evidence(
    tmp_path: Path,
    td_module,
):
    td = td_module
    root = tmp_path / "download"
    video, chat, staged_video, staged_chat = _pair_paths(root)

    td._publish_download_pair(
        staged_video,
        video,
        staged_chat,
        chat,
        transaction_root=root,
    )

    assert video.read_bytes() == b"new-video"
    assert chat.read_bytes() == b"new-chat"
    assert not td._download_transaction_journal_path(root).exists()
    assert not td._download_transaction_claim_path(root).exists()
    assert not list(root.glob(".*.backup-*"))


def test_download_assets_recovers_interrupted_pair_before_fresh_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    td_module,
):
    tx = td_module
    import twitch_download as td

    root = tmp_path / "download"
    _leave_prepared_transaction(tx, root, monkeypatch)
    cli = tmp_path / "TwitchDownloaderCLI.exe"
    cli.write_bytes(b"fake-cli")
    monkeypatch.setattr(td, "find_twitchdownloader_cli", lambda _root=None: cli)
    monkeypatch.setattr(td, "safe_which", lambda _name: None)

    def fake_run(command, **_kwargs):
        output = Path(command[command.index("-o") + 1])
        if command[1] == "videodownload":
            output.write_bytes(b"fresh-video")
        else:
            output.write_text(
                '<pre class="comment-root">fresh-chat</pre>',
                encoding="utf-8",
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(td, "run_tracked", fake_run)
    result = td.download_assets(
        "2819850140",
        out_dir=root,
        media_check="off",
    )

    assert result.video_path.read_bytes() == b"fresh-video"
    assert "fresh-chat" in result.chat_html_path.read_text(encoding="utf-8")
    assert not tx._download_transaction_journal_path(root).exists()
    assert not tx._download_transaction_claim_path(root).exists()
    assert not list(root.glob(".*.backup-*"))
    assert not list(root.glob(".*.download-*"))
