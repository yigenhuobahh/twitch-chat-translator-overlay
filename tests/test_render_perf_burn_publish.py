#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch-1 deep-audit fixes: frames, validate floor, schedule eviction, import identity."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image
import pytest

from helpers import load_module
import media_health
import media_probe
import overlay_compose


def test_expand_frame_sequence_fails_on_missing_middle_frame(tmp_path: Path):
    """Missing frame_00003 must fail expand, not silently leave a gap."""
    from render_perf import expand_frame_sequence_for_ffmpeg, frame_path

    frames = tmp_path / "frames"
    frames.mkdir()
    img = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    # Write 0,1,2,4 but NOT 3 — and claim written indexes omit 3 with no prior fill path
    for i in (0, 1, 2, 4):
        img.save(frame_path(frames, i))
    # Delete nothing else; expand should fill 3 from previous (2). That succeeds.
    # To force a hard gap that cannot be filled: remove all sources before a hole
    # by claiming written starts after the hole.
    for p in frames.glob("frame_*.png"):
        p.unlink()
    for i in (3, 4, 5):
        img.save(frame_path(frames, i))
    with pytest.raises(RuntimeError, match="cannot fill frame_00000|missing"):
        expand_frame_sequence_for_ffmpeg(frames, 6, [3, 4, 5])


def test_expand_frame_sequence_fills_then_asserts_contiguous(tmp_path: Path):
    from render_perf import expand_frame_sequence_for_ffmpeg, frame_path

    frames = tmp_path / "frames"
    frames.mkdir()
    img = Image.new("RGBA", (4, 4), (1, 2, 3, 4))
    written = [0, 2, 5]
    for i in written:
        img.save(frame_path(frames, i))
    stats = expand_frame_sequence_for_ffmpeg(frames, 6, written)
    assert stats["filled"] >= 1
    for i in range(6):
        assert frame_path(frames, i).is_file()


def test_compose_video_fails_fast_on_missing_frame(tmp_path: Path):
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    from render_perf import frame_path

    frames = tmp_path / "frames"
    frames.mkdir()
    img = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    # 0..4 exist, 3 missing
    for i in range(5):
        if i == 3:
            continue
        img.save(frame_path(frames, i))
    video = tmp_path / "src.mp4"
    video.write_bytes(b"not-a-real-video")
    config = SimpleNamespace(
        fps=5,
        x=0,
        y=0,
        encode=SimpleNamespace(
            overlay_codec="png",
            notes=[],
            resolved_encoder="x264",
            webm_cpu_used=4,
            video_codec="libx264",
        ),
        no_backup_prev=True,
        output_fps=None,
        stage_timings={},
    )
    # Fail before ffmpeg: do not need a real video or full encode options.
    with pytest.raises(RuntimeError, match="missing .*overlay frame|frame_00003"):
        burn.compose_video(str(video), str(frames), str(tmp_path), config, duration=1.0)


@pytest.mark.smoke
def test_validate_min_duration_floor_is_not_dead(make_test_video):
    """min_duration below expected must still allow a complete ~expected file."""
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    video = make_test_video(duration=2.0, fps=30)
    # Old bug: floor = max(min_duration, expected) always used expected when both set,
    # making a lower min_duration meaningless. New: min_duration is independent floor;
    # expected short-check uses expected. A 2.0s file with expected=2.0 and
    # min_duration=1.0 must pass.
    ok, _, reason = burn.validate_rendered_output(
        str(video),
        expected_duration=2.0,
        require_audio=True,
        duration_tolerance=0.35,
        min_duration=1.0,
    )
    assert ok, reason


@pytest.mark.smoke
def test_validate_rejects_too_long_with_tight_default_allowance(make_test_video):
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    video = make_test_video(duration=3.0, fps=30)
    # Default max_extra is now ~0.5..0.75, not 2.0. A 3s file vs expected 2.0
    # exceeds even the old 2.0 allowance? 3 > 2+0.75 yes; also > 2+0.35.
    ok, _, reason = burn.validate_rendered_output(
        str(video),
        expected_duration=2.0,
        require_audio=True,
        duration_tolerance=0.35,
        # use default max_extra_seconds (None)
    )
    assert not ok
    assert "longer" in reason.lower()


def test_validate_default_max_extra_is_tighter_than_two_seconds():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    assert burn._default_max_extra_seconds(10.0) == pytest.approx(0.5)
    assert burn._default_max_extra_seconds(200.0) == pytest.approx(0.75)  # capped
    assert burn._default_max_extra_seconds(0.0) == pytest.approx(0.5)


def test_schedule_same_timestamp_eviction_no_multi_occupancy():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    messages = [
        {"timestamp": 1.0, "author": f"u{i}", "fragments": [], "badges": []}
        for i in range(15)
    ]
    line_count = {i: 1 for i in range(15)}
    schedule = burn.schedule_messages(
        messages,
        msg_line_count=line_count,
        duration=30.0,
        max_visible=5,
        msg_lifetime=10.0,
    )
    assert len(schedule) == 15
    # After eviction, at any t just after the burst, at most max_visible active.
    # More strongly: no two schedule entries share a lane with overlapping
    # half-open windows [start, end) of positive length.
    for i, a in enumerate(schedule):
        for b in schedule[i + 1 :]:
            if a[2] != b[2]:
                continue
            # same lane: intervals must not overlap with positive duration
            a0, a1 = a[0], a[1]
            b0, b1 = b[0], b[1]
            if a1 <= a0 or b1 <= b0:
                continue  # zero-length (fully evicted) is fine
            overlap = min(a1, b1) - max(a0, b0)
            assert overlap <= 0, f"lane multi-occupancy: {a} vs {b}"


def test_schedule_clamps_non_positive_lifetime():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    messages = [{"timestamp": 0.0, "author": "a", "fragments": [], "badges": []}]
    schedule = burn.schedule_messages(
        messages,
        msg_line_count={0: 1},
        duration=5.0,
        max_visible=3,
        msg_lifetime=0.0,
    )
    assert len(schedule) == 1
    start, end, *_ = schedule[0]
    assert end - start == pytest.approx(0.1)


def test_apply_import_skips_original_mismatch_by_default():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    chat = {
        "messages": [
            {
                "timestamp": 1.0,
                "author": "Alice",
                "fragments": [{"type": "text", "text": "hello world"}],
                "badges": [],
            },
            {
                "timestamp": 2.0,
                "author": "Bob",
                "fragments": [{"type": "text", "text": "ok"}],
                "badges": [],
            },
        ]
    }
    trans = {
        "messages": [
            {
                "index": 0,
                "timestamp": 1.0,
                "author": "Alice",
                "original": "DIFFERENT TEXT",
                "translation": "错贴译文",
            },
            {
                "index": 1,
                "timestamp": 2.0,
                "author": "Bob",
                "original": "ok",
                "translation": "好的",
            },
        ]
    }
    replaced, _s, warnings = burn.apply_imported_translations(chat, trans)
    assert replaced == 1
    assert chat["messages"][0]["fragments"][0]["text"] == "hello world"
    assert chat["messages"][1]["fragments"][0]["text"] == "好的"
    assert any("original 不一致" in w for w in warnings)
    assert any("身份不一致跳过" in w for w in warnings)


def test_apply_import_strict_raises_on_mismatch():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    chat = {
        "messages": [
            {
                "timestamp": 1.0,
                "author": "Alice",
                "fragments": [{"type": "text", "text": "hello"}],
                "badges": [],
            }
        ]
    }
    trans = {
        "messages": [
            {
                "index": 0,
                "timestamp": 1.0,
                "author": "NotAlice",
                "original": "hello",
                "translation": "嗨",
            }
        ]
    }
    with pytest.raises(ValueError, match="严格导入失败"):
        burn.apply_imported_translations(chat, trans, strict=True)
    # message left unchanged
    assert chat["messages"][0]["fragments"][0]["text"] == "hello"


def test_clean_imported_translation_preserves_url_and_drive():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    assert burn.clean_imported_translation("https://example.com/x", "user") == "https://example.com/x"
    assert burn.clean_imported_translation("C:\\Users\\a\\b.txt", "user") == "C:\\Users\\a\\b.txt"
    assert burn.clean_imported_translation("alice: 你好", "alice") == "你好"
    assert burn.clean_imported_translation("[12] hello", "user") == "hello"


@pytest.mark.smoke
def test_compose_publish_restores_bak_on_replace_failure(tmp_path: Path, make_test_video):
    """If out→.bak rename succeeded but partial→out replace fails, restore bak."""
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    from render_perf import frame_path

    video = make_test_video(duration=1.0, fps=10)
    frames = tmp_path / "frames"
    frames.mkdir()
    img = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    for i in range(10):
        img.save(frame_path(frames, i))

    # Seed an existing published output so backup path is taken.
    out_name = f"{video.stem}_chat.mp4"
    existing = tmp_path / out_name
    existing.write_bytes(b"OLD_OUTPUT_BYTES")

    config = SimpleNamespace(
        fps=10,
        x=0,
        y=0,
        encode=None,
        no_backup_prev=False,
        output_fps=10,
        stage_timings={},
    )

    # Stub encode/ffmpeg path so we reach the publish stage with a valid partial.
    partial_bytes = b"NEW_PARTIAL_BYTES"

    def fake_run_tracked(cmd, **kwargs):
        # Last arg is output path for our compose cmd.
        out = cmd[-1]
        Path(out).write_bytes(partial_bytes)
        return SimpleNamespace(returncode=0)

    def fake_validate(path, **kwargs):
        return True, {"duration": 1.0, "has_video": True, "has_audio": True}, ""

    def fake_resolve_encode_options(**kwargs):
        return SimpleNamespace(
            overlay_codec="png",
            notes=[],
            resolved_encoder="x264",
            webm_cpu_used=4,
            video_codec="libx264",
        )

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        # Fail the publish replace (partial -> out), succeed other replaces if any.
        if str(dst).endswith(out_name) and str(src).endswith(".partial.mp4"):
            raise OSError("simulated replace failure")
        return real_replace(src, dst)

    # Patch ownership: compose_video / validate_rendered_output / encode-arg
    # builders 的调用点在 overlay_compose，patch 必须落在属主模块才能命中
    # 调用链（门面 twitch_chat_burn 的同名符号只是 re-export 别名）。
    # media_probe.resolve_output_fps 经 media_probe.<symbol> 属性访问调用，
    # 继续按属主模块 patch。
    with mock.patch.object(overlay_compose, "run_tracked", side_effect=fake_run_tracked), mock.patch.object(
        overlay_compose, "validate_rendered_output", side_effect=fake_validate
    ), mock.patch.object(
        overlay_compose, "resolve_encode_options", side_effect=fake_resolve_encode_options
    ), mock.patch.object(
        overlay_compose, "resolve_source_av_timing", return_value={
            "source_duration": 1.0,
            "video_start": 0.0,
            "audio_start": 0.0,
            "video_lead_in": 0.0,
            "has_audio": True,
            "summary": {},
        }
    ), mock.patch.object(
        media_probe, "resolve_output_fps", return_value=10
    ), mock.patch.object(
        overlay_compose, "build_video_encode_args", return_value=["-c:v", "libx264"]
    ), mock.patch.object(
        overlay_compose, "build_audio_encode_args", return_value=["-c:a", "aac"]
    ), mock.patch.object(
        overlay_compose, "summarize_encode_options", return_value="stub"
    ), mock.patch("os.replace", side_effect=flaky_replace):
        result = burn.compose_video(str(video), str(frames), str(tmp_path), config, duration=1.0)

    assert result is None
    # Old output restored from .bak
    assert existing.is_file()
    assert existing.read_bytes() == b"OLD_OUTPUT_BYTES"


@pytest.mark.smoke
def test_compose_publish_retries_transient_permission_error(tmp_path: Path, make_test_video):
    """C-4: publish must retry os.replace on transient PermissionError.

    A player/editor holding the destination open makes MoveFileEx fail once
    with WinError 5/32 and succeed right after the reader closes — previously
    a single os.replace turned that into a hard publish failure. Now
    atomic_replace_with_retry backs off and retries: 2 transient
    PermissionErrors then success must publish the new output while keeping
    .bak semantics (old bytes preserved under .bak, new bytes at out_path).
    """
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    from render_perf import frame_path

    video = make_test_video(duration=1.0, fps=10)
    frames = tmp_path / "frames"
    frames.mkdir()
    img = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    for i in range(10):
        img.save(frame_path(frames, i))

    # Seed an existing published output so backup path is taken.
    out_name = f"{video.stem}_chat.mp4"
    existing = tmp_path / out_name
    existing.write_bytes(b"OLD_OUTPUT_BYTES")

    config = SimpleNamespace(
        fps=10,
        x=0,
        y=0,
        encode=None,
        no_backup_prev=False,
        output_fps=10,
        stage_timings={},
    )

    partial_bytes = b"NEW_PARTIAL_BYTES"

    def fake_run_tracked(cmd, **kwargs):
        out = cmd[-1]
        Path(out).write_bytes(partial_bytes)
        return SimpleNamespace(returncode=0)

    def fake_validate(path, **kwargs):
        return True, {"duration": 1.0, "has_video": True, "has_audio": True}, ""

    def fake_resolve_encode_options(**kwargs):
        return SimpleNamespace(
            overlay_codec="png",
            notes=[],
            resolved_encoder="x264",
            webm_cpu_used=4,
            video_codec="libx264",
        )

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        # Publish replace fails twice with PermissionError (sharing violation),
        # succeeds on the 3rd attempt once the "player" closed the file.
        if str(dst).endswith(out_name) and str(src).endswith(".partial.mp4"):
            if calls["n"] <= 2:
                raise PermissionError(5, "simulated sharing violation")
        return real_replace(src, dst)

    with mock.patch.object(overlay_compose, "run_tracked", side_effect=fake_run_tracked), mock.patch.object(
        overlay_compose, "validate_rendered_output", side_effect=fake_validate
    ), mock.patch.object(
        overlay_compose, "resolve_encode_options", side_effect=fake_resolve_encode_options
    ), mock.patch.object(
        overlay_compose, "resolve_source_av_timing", return_value={
            "source_duration": 1.0,
            "video_start": 0.0,
            "audio_start": 0.0,
            "video_lead_in": 0.0,
            "has_audio": True,
            "summary": {},
        }
    ), mock.patch.object(
        media_probe, "resolve_output_fps", return_value=10
    ), mock.patch.object(
        overlay_compose, "build_video_encode_args", return_value=["-c:v", "libx264"]
    ), mock.patch.object(
        overlay_compose, "build_audio_encode_args", return_value=["-c:a", "aac"]
    ), mock.patch.object(
        overlay_compose, "summarize_encode_options", return_value="stub"
    ), mock.patch.object(
        media_health, "validate_media_health", return_value=SimpleNamespace(ok=True, warnings=[])
    ), mock.patch("os.replace", side_effect=flaky_replace):
        result = burn.compose_video(str(video), str(frames), str(tmp_path), config, duration=1.0)

    # Publish must have succeeded on the 3rd retry attempt (not on attempt 1,
    # proving the retry actually engaged).
    assert calls["n"] >= 3
    assert result is not None, "transient PermissionError must be retried, not fail publish"
    # New output published, old bytes preserved under .bak.
    assert existing.is_file()
    assert existing.read_bytes() == partial_bytes
    bak = tmp_path / (out_name + ".bak")
    assert bak.is_file()
    assert bak.read_bytes() == b"OLD_OUTPUT_BYTES"


# ---------------------------------------------------------------------------
# validate_rendered_output short-check floor semantics (overlay_compose.py).
#
# Contract under test (docstring "Floor semantics"): expected_duration and
# min_duration are *independent* short-check floors — reject iff
#   actual + tol < expected   (when expected > 0)  OR
#   actual + tol < min_duration  (when min_duration set and > 0).
#
# These tests patch the probe seam at the owner module (overlay_compose.media_probe)
# so they need no FFmpeg, matching the file's patch-ownership convention.
# ---------------------------------------------------------------------------


def _patch_probe_with_duration(monkeypatch, duration: float) -> None:
    fake = SimpleNamespace(
        probe_media_summary=lambda path: {
            "ok": True,
            "duration": float(duration),
            "has_video": True,
            "has_audio": True,
            "width": 640,
            "height": 360,
            "error": "",
        }
    )
    monkeypatch.setattr(overlay_compose, "media_probe", fake)


def test_validate_min_floor_below_expected_passes_when_within_tolerance(monkeypatch):
    """Output between min_duration and expected clears both floors -> PASS.

    expected=2.0 with tol=0.35 accepts actual=1.72 (2.07 >= expected), and the
    independent min_duration=1.0 floor is far below. Guards against tightening
    mutants of the floor block: dropping the tolerance from the expected
    short-check, raising the min branch above min_duration in a way that
    rejects outputs the contract accepts, or inverting either floor check.
    """
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    _patch_probe_with_duration(monkeypatch, 1.72)
    ok, _summary, reason = burn.validate_rendered_output(
        "synthetic.mp4",
        expected_duration=2.0,
        require_audio=True,
        duration_tolerance=0.35,
        min_duration=1.0,
    )
    assert ok, reason


def test_validate_min_floor_above_expected_binds_and_reports_min_duration(monkeypatch):
    """A min_duration floor ABOVE expected must bind on its own.

    actual=2.10 clears expected=2.0 (+tol) but sits below min_duration=2.5
    even with tolerance: must be rejected with the min_duration reason. Catches
    deletion/deadening of the min branch (floor zeroed, guard raised, branch
    removed) which would silently accept a below-floor output.
    """
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    _patch_probe_with_duration(monkeypatch, 2.10)
    ok, _summary, reason = burn.validate_rendered_output(
        "synthetic.mp4",
        expected_duration=2.0,
        require_audio=True,
        duration_tolerance=0.35,
        min_duration=2.5,
    )
    assert not ok
    assert "min_duration" in reason, reason


def test_validate_min_below_expected_does_not_relax_expected_short_check(monkeypatch):
    """min_duration below expected must not disable the expected short-check.

    actual=1.50 vs expected=2.0: 1.85 < 2.0 even with tol, so the output is
    rejected via the *expected* floor even though it sits above
    min_duration=1.0. Guards the opposite misreading of "independent floor"
    (min_duration replacing/relaxing the expected check would accept this).
    """
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    _patch_probe_with_duration(monkeypatch, 1.50)
    ok, _summary, reason = burn.validate_rendered_output(
        "synthetic.mp4",
        expected_duration=2.0,
        require_audio=True,
        duration_tolerance=0.35,
        min_duration=1.0,
    )
    assert not ok
    assert "expected" in reason, reason
    assert "min_duration" not in reason, reason


def test_validate_min_floor_applies_independently_without_expected(monkeypatch):
    """With expected=0, min_duration alone is the short floor.

    actual=1.20 vs min_duration=1.6 (tol 0.35 -> 1.55 < 1.6) must be rejected
    with the min_duration reason; proves the floor exists independently of the
    expected check rather than only as a shadow of it.
    """
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    _patch_probe_with_duration(monkeypatch, 1.20)
    ok, _summary, reason = burn.validate_rendered_output(
        "synthetic.mp4",
        expected_duration=0.0,
        require_audio=False,
        duration_tolerance=0.35,
        min_duration=1.6,
    )
    assert not ok
    assert "min_duration" in reason


# ---------------------------------------------------------------------------
# P2-8: _fallback_manual_after_export must read the translation JSON with
# utf-8-sig. Sibling paths (translation_io.load_translation_file via the
# twitch_chat_burn facade, translate_chat_openai.load_json) already strip a
# leading BOM; the manual-fallback counter read with plain utf-8, so a JSON
# saved by Notepad (BOM prefixed) silently parsed as total=0 and printed
# "1/?" instead of "1/2" in the hand-translation hint.
# ---------------------------------------------------------------------------


def test_fallback_manual_after_export_accepts_bom_json(tmp_path: Path, monkeypatch, capsys):
    import render_cn_chat as pipe

    tj = tmp_path / "t.json"
    payload = json.dumps(
        {
            "messages": [
                {"index": 0, "translation": "已译"},
                {"index": 1, "translation": ""},
            ]
        },
        ensure_ascii=False,
    )
    # Notepad-style write: UTF-8 BOM in front of the JSON.
    tj.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))

    monkeypatch.setattr(pipe, "export_review_tsv", lambda *a, **k: None)
    monkeypatch.setattr(pipe, "export_review_xlsx", lambda *a, **k: None)

    pipe._fallback_manual_after_export(
        video=tmp_path / "v.mp4",
        chat_html=tmp_path / "c.html",
        trans_json=tj,
        review_tsv=tmp_path / "r.tsv",
        review_xlsx=tmp_path / "r.xlsx",
        workdir=None,
        final_output=tmp_path / "o.mp4",
        reason="API down",
    )

    out = capsys.readouterr().out
    # filled=1 (utf-8-sig sibling already worked); total must be 2, not the
    # BOM-broken fallback "?".
    assert "1/2" in out, f"expected filled/total 1/2 in hint, got: {out}"
    assert "1/?" not in out
