# -*- coding: utf-8 -*-
"""Regression tests for the shared Windows sharing-violation retry helper.

A concurrent reader holding the destination open without FILE_SHARE_DELETE
makes os.replace fail once with PermissionError (WinError 5/32) on Windows;
run_meta and translation_io already retried that transient path. These tests
pin the same semantics onto the shared helper and every entrypoint that now
routes through it: common_utils.atomic_write_json, job_config.write_job_file
and env_bootstrap.atomic_replace_directory (both its internal replaces).

Pattern mirrors test_cli_clean_and_contracts.test_write_run_meta_survives_windows_replace_sharing_violation:
inject a flaky os.replace (first N calls raise PermissionError) and assert the
write succeeds via retry; then a permanently failing os.replace must still
propagate (no silent data-loss claim).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import common_utils
import env_bootstrap
import job_config


def _flaky_replace(real_replace, state, fail_times=2):
    def flaky(src, dst, *args, **kwargs):
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise PermissionError(5, "拒绝访问。")
        return real_replace(src, dst, *args, **kwargs)

    return flaky


def test_atomic_replace_with_retry_survives_transient_permission_error(tmp_path, monkeypatch):
    """The helper retries 2 transient PermissionErrors, then replaces."""
    real_replace = common_utils.os.replace
    state = {"calls": 0}
    src = tmp_path / "src.txt"
    src.write_text("payload", encoding="utf-8")
    dst = tmp_path / "dst.txt"

    monkeypatch.setattr(common_utils.os, "replace", _flaky_replace(real_replace, state))
    common_utils.atomic_replace_with_retry(src, dst)

    assert state["calls"] == 3
    assert dst.is_file()
    assert dst.read_text(encoding="utf-8") == "payload"


def test_atomic_replace_with_retry_permanent_error_propagates(tmp_path, monkeypatch):
    """A permanent PermissionError still raises (original semantics)."""
    monkeypatch.setattr(
        common_utils.os,
        "replace",
        lambda src, dst: (_ for _ in ()).throw(PermissionError(5, "拒绝访问。")),
    )
    with pytest.raises(PermissionError):
        common_utils.atomic_replace_with_retry(tmp_path / "src.txt", tmp_path / "dst.txt", attempts=3)


def test_atomic_write_json_survives_windows_replace_sharing_violation(tmp_path, monkeypatch):
    """Regression (concurrency-2): atomic_write_json must retry, not crash."""
    real_replace = common_utils.os.replace
    state = {"calls": 0}
    target = tmp_path / "out.json"

    monkeypatch.setattr(common_utils.os, "replace", _flaky_replace(real_replace, state))
    common_utils.atomic_write_json(target, {"hello": "world"})

    assert state["calls"] == 3
    assert json.loads(target.read_text(encoding="utf-8")) == {"hello": "world"}
    # The unique tmp file is cleaned up after the successful replace.
    assert not list(tmp_path.glob(".*.tmp"))


def test_atomic_write_json_permanent_error_propagates(tmp_path, monkeypatch):
    monkeypatch.setattr(
        common_utils.os,
        "replace",
        lambda src, dst: (_ for _ in ()).throw(PermissionError(5, "拒绝访问。")),
    )
    target = tmp_path / "out.json"
    with pytest.raises(PermissionError):
        common_utils.atomic_write_json(target, {"hello": "world"})
    assert not target.exists()


def test_write_job_file_survives_windows_replace_sharing_violation(tmp_path, monkeypatch):
    """Regression (concurrency-2): job_config.write_job_file must retry."""
    real_replace = job_config.os.replace
    state = {"calls": 0}
    target = tmp_path / "demo.job.yaml"

    monkeypatch.setattr(job_config.os, "replace", _flaky_replace(real_replace, state))
    path = job_config.write_job_file(
        target,
        {"video": "in.mp4", "chat_html": "chat.html", "output": "out.mp4"},
        overwrite=True,
    )

    assert state["calls"] == 3
    assert path.is_file()
    assert "video: in.mp4" in path.read_text(encoding="utf-8")


def test_write_job_file_permanent_error_propagates(tmp_path, monkeypatch):
    monkeypatch.setattr(
        job_config.os,
        "replace",
        lambda src, dst: (_ for _ in ()).throw(PermissionError(5, "拒绝访问。")),
    )
    with pytest.raises(PermissionError):
        job_config.write_job_file(tmp_path / "demo.job.yaml", {"video": "in.mp4"}, overwrite=True)


def test_atomic_replace_directory_survives_windows_sharing_violation(tmp_path, monkeypatch):
    """Regression (concurrency-9): both internal replaces retry; no residue.

    The flaky injector fails the first 2 calls (covering the destination->backup
    replace) and the first 2 calls of the staged->destination replace as well,
    proving retry wraps each os.replace individually.
    """
    real_replace = env_bootstrap.os.replace

    dest = tmp_path / "tool"
    (dest / "old").mkdir(parents=True)
    (dest / "old" / "f.txt").write_text("old", encoding="utf-8")
    staged = tmp_path / ".tool.ready-abc"
    (staged / "new").mkdir(parents=True)
    (staged / "new" / "g.txt").write_text("new", encoding="utf-8")

    fail_times = {"n": 0}

    def flaky(src, dst, *args, **kwargs):
        fail_times["n"] += 1
        # Fail 2 calls in a row, let 1 through, fail 2 more, let the rest through.
        if fail_times["n"] <= 2 or 4 <= fail_times["n"] <= 5:
            raise PermissionError(5, "拒绝访问。")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(env_bootstrap.os, "replace", flaky)
    env_bootstrap.atomic_replace_directory(staged, dest)

    assert fail_times["n"] >= 5, "both internal replaces must have hit the retry path"
    assert (dest / "new" / "g.txt").is_file()
    assert not staged.exists()
    assert not any(p.name.startswith(".tool.backup-") for p in tmp_path.iterdir())


def test_atomic_replace_directory_permanent_error_restores_backup(tmp_path, monkeypatch):
    """Rollback semantics are unchanged: a permanent failure of the
    staged->destination replace restores the backup; the original OSError
    propagates (rollback replace is not retried away into a false success)."""
    real_replace = env_bootstrap.os.replace

    dest = tmp_path / "tool"
    (dest / "old").mkdir(parents=True)
    (dest / "old" / "f.txt").write_text("old", encoding="utf-8")
    staged = tmp_path / ".tool.ready-abc"
    (staged / "new").mkdir(parents=True)
    (staged / "new" / "g.txt").write_text("new", encoding="utf-8")

    def fail_staged(src, dst):
        if Path(src) == staged and Path(dst) == dest:
            raise OSError(5, "permanent failure")
        return real_replace(src, dst)

    monkeypatch.setattr(env_bootstrap.os, "replace", fail_staged)
    with pytest.raises(OSError):
        env_bootstrap.atomic_replace_directory(staged, dest)

    assert (dest / "old" / "f.txt").is_file(), "backup must be restored"
    assert not (dest / "new").exists()
