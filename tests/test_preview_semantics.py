#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""REND-R1: --preview-frame 的聊天过滤时刻必须与渲染钳制时刻一致。

回归背景: --preview-clip 10 + --preview-frame 50 时, 过滤窗口曾用原始
preview_frame=50 构造, 而渲染把预览时刻钳制为 min(50, min(源时长, clip))=10,
导致过滤留下的消息在调度时全部因 t >= duration 被丢弃 → 全透明预览、exit 0
无告警; float 模式则聊天与底图错位。
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from chat_window import (  # noqa: E402
    filter_chat_for_time_window,
    preview_window,
    resolve_preview_frame_time,
)
from helpers import load_module  # noqa: E402
from overlay_config import OverlayConfig  # noqa: E402
from overlay_scene import OverlayScenePlan  # noqa: E402


def _burn():
    return load_module("twitch_chat_burn_preview_semantics", "twitch_chat_burn.py")


def _chat():
    """Chat spanning early / near-render-instant / future timestamps."""
    return {
        "messages": [
            {"timestamp": 1.0, "author": "a", "fragments": [], "badges": []},
            {"timestamp": 9.0, "author": "b", "fragments": [], "badges": []},
            {"timestamp": 9.5, "author": "c", "fragments": [], "badges": []},
            {"timestamp": 50.0, "author": "d", "fragments": [], "badges": []},
            {"timestamp": 200.0, "author": "e", "fragments": [], "badges": []},
        ],
        "emote_map": {},
    }


def _line_counts(messages):
    return {i: 1 for i in range(len(messages))}


def test_resolve_preview_frame_time_clamps_to_effective_preview_duration():
    # preview_clip=10 + preview_frame=50 + source 600s → render instant is 10.
    t, warning = resolve_preview_frame_time(50.0, 10.0, 600.0)
    assert t == pytest.approx(10.0)
    assert warning is not None
    assert "50" in warning and "10" in warning

    # The filter instant must match OverlayScenePlan's clamped render instant.
    plan = OverlayScenePlan.from_config(
        source_duration=600.0,
        config=OverlayConfig(fps=30, preview_clip=10, preview_frame=50),
        message_count=1,
    )
    assert plan.preview_time == pytest.approx(t)


def test_clamped_window_centers_filter_on_render_instant():
    t, _warning = resolve_preview_frame_time(50.0, 10.0, 600.0)
    win_start, win_end = preview_window(t, 10.0, msg_lifetime=4.0)
    assert win_start == pytest.approx(6.0)
    assert win_end == pytest.approx(10.05)
    # Old behavior kept the raw frame instant (46..50.05) — the regression.
    old_start, old_end = preview_window(50.0, 10.0, msg_lifetime=4.0)
    assert old_start == pytest.approx(46.0)
    assert old_end == pytest.approx(50.05)


def test_clamped_preview_keeps_lanes_schedule_non_empty():
    burn = _burn()
    t, _warning = resolve_preview_frame_time(50.0, 10.0, 600.0)
    win_start, win_end = preview_window(t, 10.0, msg_lifetime=14.0)
    filtered = filter_chat_for_time_window(_chat(), win_start, win_end, 14.0)
    assert [m["timestamp"] for m in filtered["messages"]] == pytest.approx([1.0, 9.0, 9.5])

    schedule = burn.schedule_messages(
        filtered["messages"],
        _line_counts(filtered["messages"]),
        duration=10.0,
        max_visible=6,
        msg_lifetime=14.0,
    )
    assert schedule, "clamped preview must not schedule to an empty (fully transparent) overlay"
    visible_at_render = [row for row in schedule if row[0] <= 10.0 < row[1]]
    assert visible_at_render, "chat must be on-screen at the clamped render instant"

    # Characterize the old failure: raw frame=50 window starves the schedule.
    old_start, old_end = preview_window(50.0, 10.0, msg_lifetime=14.0)
    old_filtered = filter_chat_for_time_window(_chat(), old_start, old_end, 14.0)
    old_schedule = burn.schedule_messages(
        old_filtered["messages"],
        _line_counts(old_filtered["messages"]),
        duration=10.0,
        max_visible=6,
        msg_lifetime=14.0,
    )
    assert not old_schedule


def test_clamped_preview_keeps_float_schedule_non_empty():
    burn = _burn()
    t, _warning = resolve_preview_frame_time(50.0, 10.0, 600.0)
    # float window_life = max(video_dur, msg_lifetime, 3600) — no time eviction.
    win_start, win_end = preview_window(t, 10.0, msg_lifetime=3600.0)
    filtered = filter_chat_for_time_window(
        _chat(), win_start, win_end, 3600.0, float_capacity_lines=6, max_message_lines=1,
    )
    assert [m["timestamp"] for m in filtered["messages"]] == pytest.approx([1.0, 9.0, 9.5])

    events = burn.schedule_messages_float(
        filtered["messages"],
        _line_counts(filtered["messages"]),
        duration=10.0,
        capacity_lines=6,
    )
    visible = burn.active_float_stack(events, 10.0, 6)
    assert visible, "float preview at the clamped instant must not be fully transparent"


def test_standalone_preview_frame_within_duration_unchanged():
    t, warning = resolve_preview_frame_time(55.0, None, 600.0)
    assert t == pytest.approx(55.0)
    assert warning is None

    win_start, win_end = preview_window(t, None, msg_lifetime=14.0)
    assert win_start == pytest.approx(41.0)
    assert win_end == pytest.approx(55.05)
    filtered = filter_chat_for_time_window(_chat(), win_start, win_end, 14.0)
    assert [m["timestamp"] for m in filtered["messages"]] == pytest.approx([50.0])

    plan = OverlayScenePlan.from_config(
        source_duration=600.0,
        config=OverlayConfig(fps=30, preview_frame=55),
        message_count=1,
    )
    assert plan.preview_time == pytest.approx(t)


def test_standalone_preview_frame_beyond_duration_clamps_with_warning():
    t, warning = resolve_preview_frame_time(120.0, None, 20.0)
    assert t == pytest.approx(20.0)
    assert warning is not None
    win_start, win_end = preview_window(t, None, msg_lifetime=14.0)
    assert win_start == pytest.approx(6.0)
    assert win_end == pytest.approx(20.05)


def test_standalone_preview_clip_unchanged():
    t, warning = resolve_preview_frame_time(None, 10.0, 600.0)
    assert t is None
    assert warning is None

    win_start, win_end = preview_window(t, 10.0, msg_lifetime=14.0)
    assert win_start == pytest.approx(0.0)
    assert win_end == pytest.approx(10.0)
    filtered = filter_chat_for_time_window(_chat(), win_start, win_end, 14.0)
    assert [m["timestamp"] for m in filtered["messages"]] == pytest.approx([1.0, 9.0, 9.5])


def test_combined_flags_without_clamp_still_warn_about_effective_instant():
    t, warning = resolve_preview_frame_time(5.0, 10.0, 600.0)
    assert t == pytest.approx(5.0)
    assert warning is not None
    win_start, win_end = preview_window(t, 10.0, msg_lifetime=14.0)
    assert win_start == pytest.approx(0.0)
    assert win_end == pytest.approx(5.05)


def test_resolve_preview_frame_time_without_known_duration():
    # Unknown source duration but clip present: clamp to the clip length.
    t, warning = resolve_preview_frame_time(50.0, 10.0, None)
    assert t == pytest.approx(10.0)
    assert warning is not None
    # Nothing known at all: keep raw instant (renderer fails later on probe).
    t2, warning2 = resolve_preview_frame_time(50.0, None, None)
    assert t2 == pytest.approx(50.0)
    assert warning2 is None
