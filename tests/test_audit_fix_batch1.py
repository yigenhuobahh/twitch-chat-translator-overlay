#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch-1 deep-audit fixes: frames, validate floor, schedule eviction, import identity."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image
import pytest

from helpers import load_module


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

    with mock.patch.object(burn, "run_tracked", side_effect=fake_run_tracked), mock.patch.object(
        burn, "validate_rendered_output", side_effect=fake_validate
    ), mock.patch.object(
        burn, "resolve_encode_options", side_effect=fake_resolve_encode_options
    ), mock.patch.object(
        burn, "resolve_source_av_timing", return_value={
            "source_duration": 1.0,
            "video_start": 0.0,
            "audio_start": 0.0,
            "video_lead_in": 0.0,
            "has_audio": True,
            "summary": {},
        }
    ), mock.patch.object(
        burn, "resolve_output_fps", return_value=10
    ), mock.patch.object(
        burn, "build_video_encode_args", return_value=["-c:v", "libx264"]
    ), mock.patch.object(
        burn, "build_audio_encode_args", return_value=["-c:a", "aac"]
    ), mock.patch.object(
        burn, "summarize_encode_options", return_value="stub"
    ), mock.patch("os.replace", side_effect=flaky_replace):
        result = burn.compose_video(str(video), str(frames), str(tmp_path), config, duration=1.0)

    assert result is None
    # Old output restored from .bak
    assert existing.is_file()
    assert existing.read_bytes() == b"OLD_OUTPUT_BYTES"
