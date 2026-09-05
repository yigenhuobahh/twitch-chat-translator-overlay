#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hardening tests for the render path (Wave 2 REND items).

Covers:
- REND-O4: --offset keeps negative (pre-show carry-in) timestamps; schedulers
  keep them mid-life instead of bursting them at t=0, and still drop rows whose
  whole lifetime ended before 0.
- REND-O5: min_visible eviction rejection is counted and logged.
- REND-O2: hardware-encoder trial probe runs at 256x256 (not 2x2).
- REND-O6: unexpected main() crashes mark run_meta failed before re-raising.
- REND-O8 guarantee: scheduler drops timestamp >= duration rows (prepass skip).
- REND-N1/N9/N10: dead render_perf helpers / mid-function imports removed.
- REND-N3/N4/N5/N6/N8: signature, CJK ranges, fade constants, import cleaning,
  float prefilter/trim single implementation.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from chat_window import (  # noqa: E402
    apply_time_offset,
    filter_chat_for_time_window,
    trim_float_carry_in_messages,
)
import encode_options  # noqa: E402
from helpers import load_module  # noqa: E402
import render_perf  # noqa: E402


@pytest.fixture(scope="module")
def burn():
    return load_module("twitch_chat_burn_render_hardening", "twitch_chat_burn.py")


# ---------------------------------------------------------------------------
# REND-O4: negative (pre-show) timestamps survive apply_time_offset
# ---------------------------------------------------------------------------

def test_apply_time_offset_keeps_negative_timestamps():
    messages = [{"timestamp": 100.0}, {"timestamp": 108.5}, {"timestamp": 97.0}]
    out = apply_time_offset(messages, 105.0)
    assert out is messages
    # Pre-show messages keep negative video-relative times (carry-in mid-life).
    assert [m["timestamp"] for m in messages] == pytest.approx([-5.0, 3.5, -8.0])
    # Export identity stays intact (stream_timestamp = pre-offset value).
    assert [m["stream_timestamp"] for m in messages] == pytest.approx([100.0, 108.5, 97.0])


def test_lanes_schedule_shows_preshow_carry_in_midlife_not_piled_at_zero(burn):
    messages = [{"timestamp": -10.0}, {"timestamp": -8.0}, {"timestamp": 2.0}]
    counts = {i: 1 for i in range(len(messages))}
    schedule = burn.schedule_messages(
        messages,
        counts,
        duration=10.0,
        max_visible=3,
        msg_lifetime=14.0,
    )
    starts = sorted(row[0] for row in schedule)
    # lifetime 14s: -10 and -8 are still alive at t=0 and must keep their own
    # (negative) starts. Clamping to 0 would have produced [0.0, 0.0, 2.0].
    assert starts == pytest.approx([-10.0, -8.0, 2.0])
    assert 0.0 not in starts


def test_lanes_schedule_drops_fully_expired_preshow_messages(burn):
    messages = [{"timestamp": -30.0}, {"timestamp": -2.0}, {"timestamp": 1.0}]
    counts = {i: 1 for i in range(len(messages))}
    schedule = burn.schedule_messages(
        messages,
        counts,
        duration=10.0,
        max_visible=3,
        msg_lifetime=14.0,
    )
    # (-30 + 14) <= 0: whole lifetime over before t=0 -> dropped, unchanged.
    assert sorted(row[3] for row in schedule) == [1, 2]


def test_float_schedule_keeps_negative_carry_in(burn):
    messages = [{"timestamp": -9.0}, {"timestamp": -4.0}, {"timestamp": 0.5}]
    counts = {i: 1 for i in range(len(messages))}
    events = burn.schedule_messages_float(
        messages, counts, duration=5.0, capacity_lines=3,
    )
    assert [e[0] for e in events] == pytest.approx([-9.0, -4.0, 0.5])
    stack = burn.active_float_stack(events, 0.0, 3)
    # Newest-first bottom-up stack at t=0 shows both carry-in rows mid-age.
    assert [row[1] for row in stack] == [1, 0]


# ---------------------------------------------------------------------------
# REND-O5: min_visible eviction rejection is counted, not silent
# ---------------------------------------------------------------------------

def test_min_visible_eviction_rejection_is_counted_and_logged(burn, capsys):
    messages = [{"timestamp": 0.0}, {"timestamp": 1.0}]
    counts = {0: 1, 1: 1}
    schedule = burn.schedule_messages(
        messages,
        counts,
        duration=10.0,
        max_visible=1,
        msg_lifetime=6.0,
        min_visible_seconds=3.0,
    )
    # Second message cannot evict the first (1s on screen < 3s min) and has no
    # free lane: it must be dropped AND counted in the schedule log.
    assert [row[3] for row in schedule] == [0]
    out = capsys.readouterr().out
    assert "min_visible_seconds" in out
    assert "1 条" in out


# ---------------------------------------------------------------------------
# REND-O8: the prepass skip relies on this scheduler guarantee
# ---------------------------------------------------------------------------

def test_schedule_drops_messages_at_or_after_duration(burn):
    messages = [{"timestamp": 9.0}, {"timestamp": 10.0}, {"timestamp": 12.5}]
    counts = {i: 1 for i in range(len(messages))}
    schedule = burn.schedule_messages(
        messages, counts, duration=10.0, max_visible=2, msg_lifetime=4.0,
    )
    assert [row[3] for row in schedule] == [0]


# ---------------------------------------------------------------------------
# REND-O2: hardware trial encode probes at 256x256
# ---------------------------------------------------------------------------

def test_trial_encode_probes_at_256x256(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        with open(cmd[-1], "wb") as fh:
            fh.write(b"fake mp4 payload")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(encode_options, "require_executable", lambda name: "ffmpeg")
    monkeypatch.setattr(encode_options.subprocess, "run", fake_run)

    assert encode_options._trial_encode("h264_nvenc") is True
    assert "color=c=black:s=256x256:d=0.04" in captured["cmd"]
    assert not any("s=2x2" in part for part in captured["cmd"])
    assert captured["cmd"][captured["cmd"].index("-c:v") + 1] == "h264_nvenc"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg on PATH required")
def test_trial_encode_accepts_libx264_at_probe_resolution():
    assert encode_options._trial_encode("libx264") is True


# ---------------------------------------------------------------------------
# REND-O6: main() wrapper marks run_meta failed before re-raising
# ---------------------------------------------------------------------------

def test_main_wrapper_marks_run_failed_on_crash(burn, tmp_path, monkeypatch):
    job_dir = tmp_path / "job_crash"
    job_dir.mkdir()
    burn.write_run_meta(str(job_dir), {"status": "running", "video": "v.mp4"})

    def fake_pipeline(status_sink=None):
        status_sink["out_dir"] = str(job_dir)
        raise RuntimeError("disk full while compositing")

    monkeypatch.setattr(burn, "_main", fake_pipeline)
    with pytest.raises(RuntimeError, match="disk full"):
        burn.main()

    meta = json.loads((job_dir / "run_meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "failed"
    assert meta["stage"] == "unexpected_error"
    assert "RuntimeError" in meta["note"] and "disk full" in meta["note"]


def test_main_wrapper_reraises_without_job_dir(burn, monkeypatch):
    def fake_pipeline(status_sink=None):
        raise RuntimeError("early crash before out_dir")

    monkeypatch.setattr(burn, "_main", fake_pipeline)
    with pytest.raises(RuntimeError, match="early crash"):
        burn.main()


# ---------------------------------------------------------------------------
# REND-N1 / N9 / N10 / O1: dead code removed, behavior kept
# ---------------------------------------------------------------------------

def test_render_perf_dead_helpers_removed():
    for gone in ("StageTimer", "is_blank_visible", "estimate_disk_bytes"):
        assert not hasattr(render_perf, gone), gone
    for kept in (
        "frame_path",
        "write_or_reuse_frame",
        "blank_gap_frame_indexes",
        "missing_frame_indexes",
        "assert_contiguous_frame_sequence",
        "expand_frame_sequence_for_ffmpeg",
        "ensure_render_disk_headroom",
    ):
        assert hasattr(render_perf, kept), kept


@pytest.mark.parametrize("stride", [1, 2, 3, 4, 7, 16])
def test_blank_gap_indexes_cover_start_and_tail(stride):
    idxs = render_perf.blank_gap_frame_indexes(5, 23, hold_stride=stride)
    assert idxs[0] == 5
    assert idxs[-1] == 22
    assert idxs == sorted(set(idxs))
    assert all(5 <= i < 23 for i in idxs)


def test_burn_surface_after_cleanup(burn):
    # REND-O1: duplicate contiguity assert removed from render_overlay
    # (expand_frame_sequence_for_ffmpeg keeps its own hard guarantee).
    assert not hasattr(burn, "assert_contiguous_frame_sequence")
    # REND-N9 (repealed): the collections re-export was removed; the facade
    # must no longer surface Counter/OrderedDict.
    assert not hasattr(burn, "Counter") and not hasattr(burn, "OrderedDict")
    # REND-N5: fade envelope is module-level, single source of truth.
    assert burn.FADE_IN_SECONDS == 0.3
    assert burn.FADE_OUT_SECONDS == 0.5


# ---------------------------------------------------------------------------
# REND-N3 / N4: API + CJK coverage
# ---------------------------------------------------------------------------

def test_expected_compose_duration_drops_unused_lead_in_param(burn):
    assert burn.expected_compose_duration(12.5) == pytest.approx(12.5)
    assert burn.expected_compose_duration(0.0) == 0.0
    with pytest.raises(TypeError):
        burn.expected_compose_duration(12.5, video_lead_in=1.0)


@pytest.mark.parametrize(
    "ch",
    ["あ", "ア", "ン", "か", "한", "중", "힣", "中", "、", "Ａ"],
)
def test_is_cjk_char_covers_kana_and_hangul(burn, ch):
    assert burn.is_cjk_char(ch) is True


@pytest.mark.parametrize("ch", ["a", "Z", "1", "!", "é", "г", "😀"])
def test_is_cjk_char_rejects_non_cjk(burn, ch):
    assert burn.is_cjk_char(ch) is False


# ---------------------------------------------------------------------------
# REND-N6: translations that clean to empty are skipped, not written as ""
# ---------------------------------------------------------------------------

def test_import_skips_rows_cleaned_to_empty_without_emotes(burn):
    chat = {
        "messages": [
            {
                "timestamp": 0.0,
                "author": "alice",
                "fragments": [{"type": "text", "text": "hello"}],
            },
            {
                "timestamp": 1.0,
                "author": "bob",
                "fragments": [{"type": "text", "text": "hi"}],
            },
        ],
    }
    trans = {
        "messages": [
            # "alice:" cleans to "" via the author-prefix strip -> row skipped.
            {"index": 0, "author": "alice", "original": "hello", "translation": "alice:"},
            {"index": 1, "author": "bob", "original": "hi", "translation": "你好"},
        ],
    }
    replaced, stripped_placeholders, warnings = burn.apply_imported_translations(chat, trans)

    assert replaced == 1
    assert stripped_placeholders == 0
    # Row 0 keeps its original fragments — no {"text": ""} fragment written.
    assert chat["messages"][0]["fragments"] == [{"type": "text", "text": "hello"}]
    assert chat["messages"][1]["fragments"] == [{"type": "text", "text": "你好"}]
    assert any("译文清洗后为空" in w and "1 条" in w for w in warnings)


# ---------------------------------------------------------------------------
# REND-N8: float prefilter / carry-in trim share one newest-rows implementation
# ---------------------------------------------------------------------------

def test_float_prefilter_keeps_newest_cap_and_trims_meta_fields():
    data = {
        "messages": [{"timestamp": float(i), "fragments": []} for i in range(200)],
        "emote_map": {},
    }
    out = filter_chat_for_time_window(
        data, 100.0, 110.0, 3600.0, float_capacity_lines=5,
    )
    meta = out["_window"]["float_prefilter"]
    assert meta["pre_window_before"] == 100
    assert meta["pre_window_after"] == 5
    assert "per_msg_lines" not in meta
    assert "soft_max_message_lines" not in meta
    stamps = [m["timestamp"] for m in out["messages"]]
    # window_end itself is still "visible" (t <= window_end), hence 100..110.
    assert stamps == pytest.approx(
        [95.0, 96.0, 97.0, 98.0, 99.0] + [float(i) for i in range(100, 111)]
    )


def test_trim_float_carry_in_uses_shared_selection_and_meta():
    data = {
        "messages": [{"timestamp": float(i), "fragments": []} for i in range(90, 106)],
        "emote_map": {},
    }
    wide = filter_chat_for_time_window(data, 100.0, 110.0, 3600.0)
    trimmed = trim_float_carry_in_messages(wide, 100.0, 5)
    stamps = [m["timestamp"] for m in trimmed["messages"]]
    assert stamps == pytest.approx(
        [95.0, 96.0, 97.0, 98.0, 99.0] + [float(i) for i in range(100, 106)]
    )
    window_meta = trimmed["_window"]
    assert window_meta["pre_window_before_trim"] == 10
    assert window_meta["pre_window_after_trim"] == 5
    assert "trim_per_msg_lines" not in window_meta
