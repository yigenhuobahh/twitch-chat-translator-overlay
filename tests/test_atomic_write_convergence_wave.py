# -*- coding: utf-8 -*-
"""Convergence wave (0906): three bare os.replace sites route through the
shared helper, plus the redaction / stale-partial hardening that landed with
them.

Pinned here:
- concurrency-1: translate_chat_openai.save_json (final + throttled progress
  saves) retries transient Windows sharing violations via
  common_utils.atomic_replace_with_retry instead of crashing with
  PermissionError after the API cost is already sunk.
- concurrency-2: twitch_chat_burn._promote_to_out_base_locked publishes
  through the same helper (twin of the overlay_compose.py publish site).
- concurrency-3: tui_task.TaskSession.retain_result retries the transient
  path before falling back to its original None-on-failure semantics, so a
  concurrent TUI reader can no longer break manifest linkage.
- security-6 (regex part): redact_text covers DICT-REPR-shaped secrets
  (single-quoted ``'api_key': 'sk-...'`` pairs), mirroring _JSON_SECRET.
- concurrency-5 (burn part): the pre-publish stale-partial cleanup tolerates
  any OSError (held-open leftovers), not just FileNotFoundError, so a stale
  ``.partial`` no longer turns a publishable run into a failed one.

Seam notes (repo rule: patch the owner module): after the convergence the
retry helper performs the actual replace, so the flaky injector must patch
``common_utils.os.replace`` (the helper resolves ``os`` through its own
module globals). translate_chat_openai / twitch_chat_burn / tui_task keep
their own ``os`` bindings untouched.
"""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import re
import textwrap
from types import SimpleNamespace

import pytest

import common_utils
from task_results import RESULT_SCHEMA_VERSION, read_task_result
import translate_chat_openai
import tui_task
from tui_task import TaskSession


def _flaky_replace(real_replace, state, fail_times=2, match=None):
    """os.replace double that fails the first ``fail_times`` calls."""

    def flaky(src, dst, *args, **kwargs):
        if match is not None and not match(src, dst):
            return real_replace(src, dst, *args, **kwargs)
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise PermissionError(5, "拒绝访问。")
        return real_replace(src, dst, *args, **kwargs)

    return flaky


def _patch_retry_sleep(monkeypatch):
    """No-op the helper's backoff so permanent-failure tests stay instant."""
    monkeypatch.setattr(common_utils.time, "sleep", lambda _seconds: None)


def _session_with_result(tmp_path: Path) -> tuple[TaskSession, Path]:
    """A TaskSession in the post-poll state: result read, manifest on disk."""
    session_result = tmp_path / "task_abc.result.json"
    session_result.write_text(
        json.dumps(
            {
                "schema_version": RESULT_SCHEMA_VERSION,
                "state": "succeeded",
                "mode": "video",
                "returncode": 0,
                "finished_at": 0.0,
                "artifacts": [{"path": "video_chat.mp4", "kind": "video"}],
            }
        ),
        encoding="utf-8",
    )
    session = TaskSession(["python", "-c", "pass"])
    session.result_path = session_result
    session.result = read_task_result(session_result)
    assert session.result is not None
    return session, session_result


def _burn_promote_locked(args: SimpleNamespace, out_dir, out_base):
    """Rebuild twitch_chat_burn._promote_to_out_base_locked from live source.

    The promote closure is nested inside _main (cells: args/out_dir/out_base)
    and unreachable without a full CLI run. Re-executing the verbatim body of
    the live function inside an equivalent closure keeps the test honest: any
    drift in the published body is exercised, not a copy. The nested
    docstring's column-0 continuation lines must be dropped for re-exec.
    """
    import twitch_chat_burn as burn

    source = inspect.getsource(burn)
    match = re.search(
        r"(    def _promote_to_out_base_locked\(src_path: str\).*?)"
        r"(?=\n    def promote_to_out_base)",
        source,
        re.S,
    )
    assert match, "promote closure vanished from twitch_chat_burn._main"
    # Dedent the whole def (common prefix = the _main nesting level), then
    # re-indent it as the factory body: def lands at col 4, its body at col 8.
    nested = textwrap.dedent(match.group(1))
    namespace: dict = {}
    exec(  # noqa: S102 - test-only re-exec of repo source
        "def _factory(args, out_dir, out_base):\n"
        + textwrap.indent(nested, "    ")
        + "    return _promote_to_out_base_locked\n",
        dict(vars(burn)),
        namespace,
    )
    return namespace["_factory"](args, out_dir, out_base)


# ---------------------------------------------------------------------------
# concurrency-1: translate_chat_openai.save_json
# ---------------------------------------------------------------------------


def test_save_json_survives_transient_sharing_violation(tmp_path, monkeypatch):
    """Two transient PermissionErrors then success: final save must not crash."""
    real_replace = common_utils.os.replace
    state = {"calls": 0}
    monkeypatch.setattr(common_utils.os, "replace", _flaky_replace(real_replace, state))
    target = tmp_path / "translation_final.json"
    target.write_text('{"messages": []}', encoding="utf-8")

    translate_chat_openai.save_json(target, {"messages": [{"index": 1, "translation": "x"}]})

    assert state["calls"] == 3
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "messages": [{"index": 1, "translation": "x"}]
    }
    assert not list(tmp_path.glob(".*.tmp")), "unique tmp must be cleaned up"


def test_save_json_progress_save_survives_transient_sharing_violation(tmp_path, monkeypatch):
    """The throttled progress writer delegates to save_json -> same retry."""
    real_replace = common_utils.os.replace
    state = {"calls": 0}
    monkeypatch.setattr(common_utils.os, "replace", _flaky_replace(real_replace, state))
    target = tmp_path / "translation_final.json"

    translate_chat_openai.save_progress(target, {"translations": {"1": "x"}})

    assert state["calls"] == 3
    assert "translations" in json.loads(target.read_text(encoding="utf-8"))


def test_save_json_permanent_failure_still_raises(tmp_path, monkeypatch):
    """A permanently held destination keeps the original raise semantics."""
    _patch_retry_sleep(monkeypatch)
    monkeypatch.setattr(
        common_utils.os,
        "replace",
        lambda src, dst: (_ for _ in ()).throw(PermissionError(5, "拒绝访问。")),
    )
    target = tmp_path / "translation_final.json"
    with pytest.raises(PermissionError):
        translate_chat_openai.save_json(target, {"messages": []})
    assert not target.exists()


def test_save_json_source_uses_shared_helper():
    """Drift guard: save_json must route through the shared helper."""
    source = inspect.getsource(translate_chat_openai.save_json)
    assert "atomic_replace_with_retry(tmp, path)" in source
    assert not re.search(r"(?m)^\s*os\.replace\(tmp, path\)\s*$", source)


# ---------------------------------------------------------------------------
# concurrency-3: tui_task.TaskSession.retain_result
# ---------------------------------------------------------------------------


def test_retain_result_survives_transient_sharing_violation(tmp_path, monkeypatch):
    """A concurrent TUI reader holding the manifest must not lose the link."""
    real_replace = common_utils.os.replace
    state = {"calls": 0}
    monkeypatch.setattr(common_utils.os, "replace", _flaky_replace(real_replace, state))

    manifests = tmp_path / "manifests"
    manifests.mkdir()
    target = manifests / "record1.json"
    target.write_text('{"state": "running"}', encoding="utf-8")
    session, session_result = _session_with_result(tmp_path)

    retained = session.retain_result(target)

    assert retained == target
    assert json.loads(target.read_text(encoding="utf-8"))["state"] == "succeeded"
    assert session.result_path is None
    assert not session_result.exists(), "source manifest moves into history"
    assert state["calls"] >= 3, "retry must have engaged"
    assert not list(tmp_path.glob(".*.tmp"))


def test_retain_result_permanent_failure_keeps_none_semantics(tmp_path, monkeypatch):
    """Retry exhaustion falls back to the original None-on-failure behaviour."""
    _patch_retry_sleep(monkeypatch)
    monkeypatch.setattr(
        common_utils.os,
        "replace",
        lambda src, dst: (_ for _ in ()).throw(PermissionError(5, "拒绝访问。")),
    )

    manifests = tmp_path / "manifests"
    manifests.mkdir()
    target = manifests / "record1.json"
    target.write_text('{"state": "running"}', encoding="utf-8")
    session, session_result = _session_with_result(tmp_path)

    retained = session.retain_result(target)

    assert retained is None
    assert target.read_text(encoding="utf-8") == '{"state": "running"}'
    assert session.result_path == session_result, "source manifest kept for retry later"
    assert session_result.exists()


def test_retain_result_source_uses_shared_helper():
    """Drift guard: retain_result goes through the canonical helper."""
    source = inspect.getsource(TaskSession.retain_result)
    assert "atomic_replace_with_retry(temporary, target)" in source


# ---------------------------------------------------------------------------
# concurrency-2: twitch_chat_burn._promote_to_out_base_locked
# ---------------------------------------------------------------------------


def _promote_env(tmp_path: Path):
    """Job dir + pre-published destination; job tag avoids the alt-name branch."""
    out_dir = tmp_path / "job_001"
    out_base = tmp_path / "out"
    out_dir.mkdir()
    out_base.mkdir()
    job_file = out_dir / "video_chat.mp4"
    job_file.write_bytes(b"NEW-RENDER")
    return job_file


def _burn_promote_no_alt_name(args: SimpleNamespace, out_dir, out_base):
    """Wrap the closure builder with a job tag outside the alt-name branch.

    The alt-name branch only fires when the job dir basename starts with
    job_/batch_ (the real concurrent-run layout). Using a neutral tag keeps
    the test on the default-name path it is asserting on.
    """
    promote = _burn_promote_locked(args, out_dir, out_base)
    real_basename = os.path.basename

    def neutral_basename(path):
        name = real_basename(path)
        return "solo" if name == "job_001" else name

    def run(src_path: str) -> str | None:
        os.path.basename = neutral_basename
        try:
            return promote(src_path)
        finally:
            os.path.basename = real_basename

    return run


def test_burn_promote_survives_transient_sharing_violation(tmp_path, monkeypatch):
    """A viewer holding the published mp4 must not fail the publish step."""
    real_replace = common_utils.os.replace
    state = {"calls": 0}
    promoted_match = lambda src, dst: str(dst).endswith("video_chat.mp4")  # noqa: E731
    monkeypatch.setattr(
        common_utils.os, "replace", _flaky_replace(real_replace, state, match=promoted_match)
    )

    out_base = tmp_path / "out"
    job_file = _promote_env(tmp_path)
    dest = out_base / "video_chat.mp4"
    dest.write_bytes(b"OLD-RENDER")

    promote = _burn_promote_no_alt_name(
        SimpleNamespace(no_backup_prev=True, job_dir="job_001"), out_base / "job_001", out_base
    )
    result = promote(str(job_file))

    assert result == str(dest)
    assert dest.read_bytes() == b"NEW-RENDER"
    assert state["calls"] >= 3, "publish replace must retry"
    assert not (out_base / "video_chat.mp4.partial").exists()


def test_burn_promote_permanent_failure_restores_bak(tmp_path, monkeypatch):
    """Permanent sharing violation keeps old semantics: warn, restore .bak."""
    _patch_retry_sleep(monkeypatch)
    real_replace = common_utils.os.replace

    def permanent(src, dst, *args, **kwargs):
        if str(dst).endswith("video_chat.mp4") and str(src).endswith(".partial"):
            raise PermissionError(5, "拒绝访问。")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(common_utils.os, "replace", permanent)

    out_base = tmp_path / "out"
    job_file = _promote_env(tmp_path)
    dest = out_base / "video_chat.mp4"
    dest.write_bytes(b"OLD-RENDER")

    promote = _burn_promote_no_alt_name(
        SimpleNamespace(no_backup_prev=False, job_dir="job_001"), out_base / "job_001", out_base
    )
    result = promote(str(job_file))

    assert result is None, "publish failure must be reported, not crash"
    assert dest.read_bytes() == b"OLD-RENDER", ".bak must be restored"
    assert not (out_base / "video_chat.mp4.bak").is_file(), "backup consumed by restore"
    assert job_file.read_bytes() == b"NEW-RENDER", "job artifact preserved"


def test_burn_promote_stale_partial_cleanup_tolerates_oserror(tmp_path, monkeypatch):
    """concurrency-5 (burn): a held-open stale .partial must not abort publish.

    Old shape: cleanup caught only FileNotFoundError, so PermissionError from
    os.remove fell into the publish failure path and the run recorded failed.
    New shape: cleanup tolerates any OSError and the publish proceeds.
    """
    import twitch_chat_burn as burn

    real_remove = burn.os.remove
    state = {"calls": 0}

    def held_partial_remove(path, *args, **kwargs):
        if str(path).endswith("video_chat.mp4.partial"):
            state["calls"] += 1
            raise PermissionError(5, "拒绝访问。")
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(burn.os, "remove", held_partial_remove)

    out_base = tmp_path / "out"
    job_file = _promote_env(tmp_path)
    stale_partial = out_base / "video_chat.mp4.partial"
    stale_partial.write_bytes(b"STALE")

    promote = _burn_promote_no_alt_name(
        SimpleNamespace(no_backup_prev=True, job_dir="job_001"), out_base / "job_001", out_base
    )
    result = promote(str(job_file))

    assert state["calls"] == 1, "stale-partial cleanup must have been attempted"
    assert result == str(out_base / "video_chat.mp4"), "publish must proceed"
    assert (out_base / "video_chat.mp4").read_bytes() == b"NEW-RENDER"


def test_burn_promote_source_uses_shared_helper_and_broad_oserror():
    """Drift guard: publish replace via helper; stale cleanup catches OSError.

    D3 lifted the promote closures to module level (publish_promotable /
    _publish_promotable_locked); the drift guard now inspects the lifted
    function's live source instead of the former nested closure text.
    """
    import twitch_chat_burn as burn

    source = inspect.getsource(burn._publish_promotable_locked)
    assert "atomic_replace_with_retry(partial_promoted, promoted)" in source
    cleanup = source.split("os.remove(partial_promoted)", 1)[1]
    assert cleanup.lstrip().startswith("except OSError"), (
        "stale-partial cleanup must tolerate any OSError, not only FileNotFoundError"
    )


def _burn_promote_locked_source_text() -> str:
    """Verbatim source text of the lifted _publish_promotable_locked."""
    import twitch_chat_burn as burn

    return inspect.getsource(burn._publish_promotable_locked)


# ---------------------------------------------------------------------------
# security-6 (regex part): redact_text DICT-REPR secrets
# ---------------------------------------------------------------------------


def test_redact_text_covers_single_quoted_dict_repr_secrets():
    text = "x {'api_key': 'sk-proj-ABCDEF123'} y"
    redacted = tui_task.redact_text(text)
    assert "sk-proj-ABCDEF123" not in redacted
    assert "'api_key': '[redacted]'" in redacted


def test_redact_text_covers_dict_repr_secret_names():
    for name in ("token", "password", "oauth", "secret", "client_secret", "client secret", "api key"):
        text = f"Error code: 401 - {{'{name}': 'leak-value-123', 'hint': 'x'}}"
        redacted = tui_task.redact_text(text)
        assert "leak-value-123" not in redacted, name
        assert "'hint': 'x'" in redacted, "neighbouring keys stay intact"


def test_redact_text_still_covers_json_double_quote_shape():
    redacted = tui_task.redact_text('{"api_key": "sk-double-quoted", "n": 1}')
    assert "sk-double-quoted" not in redacted
    assert '"n": 1' in redacted


def test_redact_text_does_not_over_redact_innocent_prose():
    text = "the 'api_key' doc page explains colon rules without any value"
    assert tui_task.redact_text(text) == text
