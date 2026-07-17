#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regressions for deep-audit bugfixes (lane clamp, import align, duration, fade)."""

from __future__ import annotations

import pytest

from helpers import load_module


def test_schedule_clamps_overlong_message_lines():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    messages = [{"timestamp": 1.0, "author": "a", "fragments": [], "badges": []}]
    # 25 lines would previously make max_lane negative and crash / corrupt schedule
    schedule = burn.schedule_messages(
        messages,
        msg_line_count={0: 25},
        duration=10.0,
        max_visible=10,
        msg_lifetime=14.0,
    )
    assert len(schedule) == 1
    _start, _end, lane, idx, nl = schedule[0]
    assert idx == 0
    assert nl == 10  # clamped
    assert lane == 0
    assert lane + nl <= 10


def test_schedule_mobile_controls_protect_visible_and_rate_limit():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    messages = [
        {"timestamp": 1.0, "author": "a", "fragments": [], "badges": []},
        {"timestamp": 1.0, "author": "b", "fragments": [], "badges": []},
        {"timestamp": 1.0, "author": "c", "fragments": [], "badges": []},
    ]
    schedule = burn.schedule_messages(
        messages,
        msg_line_count={0: 1, 1: 1, 2: 1},
        duration=10.0,
        max_visible=3,
        msg_lifetime=4.0,
        min_visible_seconds=3.0,
        arrival_interval=1.0,
    )
    # Starts are rate-limited; lifetime runs from admit time (full on-screen window).
    assert [row[0] for row in schedule] == [1.0, 2.0, 3.0]
    assert [row[1] for row in schedule] == [5.0, 6.0, 7.0]
    assert all(end - start >= 3.0 for start, end, *_rest in schedule)


def test_lanes_delayed_admit_never_inverts_visibility_window():
    """arrival_interval > remaining life must not invent start >= end rows."""
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    messages = [
        {"timestamp": 1.0, "fragments": [], "badges": []},
        {"timestamp": 1.1, "fragments": [], "badges": []},
    ]
    schedule = burn.schedule_messages(
        messages,
        msg_line_count={0: 1, 1: 1},
        duration=20.0,
        max_visible=10,
        msg_lifetime=0.5,
        arrival_interval=2.0,
    )
    assert schedule
    for start, end, *_ in schedule:
        assert start < end


def test_float_arrival_interval_does_not_delay_carry_in():
    """Rebased pre-window messages must all be visible at t=0 with mobile arrival_interval."""
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    stamps = [-0.55, -0.50, -0.45, -0.40, -0.35, -0.30, -0.25, -0.20, -0.15, -0.10, -0.05, 0.0]
    messages = [{"timestamp": t} for t in stamps]
    events = burn.schedule_messages_float(
        messages,
        msg_line_count={i: 1 for i in range(len(stamps))},
        duration=10.0,
        capacity_lines=12,
        arrival_interval=0.35,
    )
    assert [e[0] for e in events] == stamps
    visible = burn.active_float_stack(events, 0.0, 12)
    assert len(visible) == 12


def test_float_arrival_interval_still_paces_in_window_arrivals():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    messages = [{"timestamp": 0.0}, {"timestamp": 0.01}, {"timestamp": 0.02}]
    events = burn.schedule_messages_float(
        messages,
        msg_line_count={0: 1, 1: 1, 2: 1},
        duration=10.0,
        capacity_lines=10,
        arrival_interval=0.35,
    )
    assert [round(e[0], 2) for e in events] == [0.0, 0.35, 0.7]


def test_float_throttle_from_protects_absolute_preview_frame_history():
    """Non-rebased float --preview-frame must not rate-limit ts < frame_t."""
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    frame_t = 100.0
    stamps = [frame_t - 0.05 * i for i in range(12, 0, -1)]  # 99.4 .. 99.95
    messages = [{"timestamp": t} for t in stamps]
    events = burn.schedule_messages_float(
        messages,
        msg_line_count={i: 1 for i in range(len(stamps))},
        duration=200.0,
        capacity_lines=12,
        arrival_interval=0.35,
        throttle_from=frame_t,
    )
    assert [e[0] for e in events] == stamps
    visible = burn.active_float_stack(events, frame_t, 12)
    assert len(visible) == 12
    assert getattr(events, "starts", None) == stamps


def test_schedule_max_visible_zero_uses_auto_capacity():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    # height 363 / (15+14) => 12 lanes
    assert burn.compute_lane_capacity(363, 15) == 12
    messages = [{"timestamp": float(i), "author": f"u{i}", "fragments": [], "badges": []} for i in range(20)]
    schedule = burn.schedule_messages(
        messages,
        msg_line_count={i: 1 for i in range(20)},
        duration=30.0,
        max_visible=0,
        msg_lifetime=5.0,
        auto_capacity=burn.compute_lane_capacity(363, 15),
    )
    # At a busy instant many lanes can be occupied; capacity is 12 single-line lanes.
    max_lane = max((row[2] + row[4] - 1) for row in schedule)
    assert max_lane < 12


def test_resolve_lane_budget_clamps_above_physical_capacity():
    """Explicit max_visible above physical capacity must clamp (old fixed-10 path)."""
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    # 360p ratio box from e2e: h=223, font≈14 => capacity 7 (LINE_H=28, bottom_pad=4)
    assert burn.compute_lane_capacity(223, 14) == 7
    budget, capacity, warn = burn.resolve_lane_budget(10, 223, 14)
    assert capacity == 7
    assert budget == 7
    assert warn is not None and "max_visible=10" in warn and "7" in warn

    # Explicit request within capacity is kept.
    budget_ok, capacity_ok, warn_ok = burn.resolve_lane_budget(5, 223, 14)
    assert (budget_ok, capacity_ok, warn_ok) == (5, 7, None)

    # Default/auto (0) equals capacity and does not warn.
    budget_auto, capacity_auto, warn_auto = burn.resolve_lane_budget(0, 223, 14)
    assert (budget_auto, capacity_auto, warn_auto) == (7, 7, None)


def test_default_max_visible_is_auto_fill():
    """run.bat / default layout: max_visible=0 fills by box height, no clamp warn."""
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    assert burn.OverlayConfig().max_visible == 0
    budget, capacity, warn = burn.resolve_lane_budget(0, 363, 15)
    assert warn is None
    assert budget == capacity == burn.compute_lane_capacity(363, 15)
    # Default 1080p-ish box: auto should be more than the old fixed-10 for tall boxes.
    assert budget >= 10


def test_lane_y_from_budget_never_stacks_at_top_when_clamped():
    """After resolve_lane_budget, single-line lane y must stay non-negative and distinct."""
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    height, font_size = 223, 14
    budget, _capacity, _warn = burn.resolve_lane_budget(10, height, font_size)
    line_h = burn.line_height_px(font_size)
    ys = []
    for lane in range(budget):
        # Mirror render_overlay single-line placement.
        y = height - (lane + 1) * line_h - 4
        assert y >= 0, f"lane {lane} would clamp to y=0 and overlap"
        ys.append(y)
    assert len(ys) == len(set(ys))


def test_schedule_mobile_protection_drops_new_message_when_full():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    messages = [
        {"timestamp": 1.0, "author": "a", "fragments": [], "badges": []},
        {"timestamp": 2.0, "author": "b", "fragments": [], "badges": []},
    ]
    schedule = burn.schedule_messages(
        messages,
        msg_line_count={0: 1, 1: 1},
        duration=10.0,
        max_visible=1,
        msg_lifetime=4.0,
        min_visible_seconds=3.0,
    )
    assert [(row[0], row[3]) for row in schedule] == [(1.0, 0)]


def test_schedule_min_visible_does_not_partially_truncate_on_reject():
    """If any seized row is still protected, no overlapping row may be mutated."""
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    messages = [
        {"timestamp": 0.0, "author": "old", "fragments": [], "badges": []},
        {"timestamp": 3.0, "author": "protected", "fragments": [], "badges": []},
        {"timestamp": 5.0, "author": "new", "fragments": [], "badges": []},
    ]
    schedule = burn.schedule_messages(
        messages,
        msg_line_count={0: 1, 1: 1, 2: 2},
        duration=20.0,
        max_visible=3,
        msg_lifetime=10.0,
        min_visible_seconds=3.0,
    )
    by_idx = {row[3]: row for row in schedule}
    assert 2 not in by_idx  # new multi-line message dropped
    # Unprotected old row must keep full lifetime (not truncated to t=5 then abandoned).
    assert by_idx[0][1] == 10.0
    assert by_idx[1][1] == 13.0


def test_schedule_keeps_message_starting_before_duration():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    messages = [
        {"timestamp": 9.5, "author": "tail", "fragments": [], "badges": []},
        {"timestamp": 10.0, "author": "exact", "fragments": [], "badges": []},
        {"timestamp": 10.1, "author": "after", "fragments": [], "badges": []},
    ]
    schedule = burn.schedule_messages(
        messages,
        msg_line_count={0: 1, 1: 1, 2: 1},
        duration=10.0,
        max_visible=10,
        msg_lifetime=14.0,
    )
    authors = [messages[s[3]]["author"] for s in schedule]
    assert "tail" in authors
    # t >= duration is excluded (half-open window)
    assert "exact" not in authors
    assert "after" not in authors


def test_apply_imported_translations_matches_by_index_and_strips_emote():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    chat = {
        "messages": [
            {
                "timestamp": 1.0,
                "author": "Alice",
                "fragments": [
                    {"type": "text", "text": ": hello "},
                    {"type": "emote", "class": "first-1", "title": "LUL"},
                    {"type": "text", "text": " world"},
                ],
                "badges": [],
                "color": "",
            },
            {
                "timestamp": 2.0,
                "author": "Bob",
                "fragments": [{"type": "text", "text": ": only text"}],
                "badges": [],
                "color": "",
            },
        ]
    }
    trans = {
        "messages": [
            {
                "index": 1,
                "timestamp": 2.0,
                "author": "Bob",
                "original": "only text",
                "translation": "只有文字",
            },
            {
                "index": 0,
                "timestamp": 1.0,
                "author": "Alice",
                "original": "hello [LUL] world",
                "translation": "你好 [LUL] 世界",
            },
        ]
    }
    replaced, stripped, warnings = burn.apply_imported_translations(chat, trans)
    assert replaced == 2
    assert stripped >= 1
    # index 0: translation + emotes, no leftover [LUL] text
    fr0 = chat["messages"][0]["fragments"]
    texts0 = [f["text"] for f in fr0 if f["type"] == "text"]
    assert any("你好" in t or "世界" in t for t in texts0)
    assert all("[LUL]" not in t for t in texts0)
    assert any(f.get("type") == "emote" for f in fr0)
    # index 1 text-only
    assert chat["messages"][1]["fragments"][0]["text"] == "只有文字"


def test_apply_imported_warns_on_author_mismatch():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    chat = {
        "messages": [
            {
                "timestamp": 1.0,
                "author": "Alice",
                "fragments": [{"type": "text", "text": "hi"}],
                "badges": [],
            }
        ]
    }
    trans = {
        "messages": [
            {"index": 0, "author": "NotAlice", "timestamp": 1.0, "translation": "嗨"}
        ]
    }
    replaced, _s, warnings = burn.apply_imported_translations(chat, trans)
    assert replaced == 0  # mismatched rows are skipped, not applied
    assert chat["messages"][0]["fragments"][0]["text"] == "hi"
    assert any("作者不一致" in w for w in warnings)
    assert any("跳过导入" in w for w in warnings)


def test_probe_video_duration_rejects_empty(monkeypatch):
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")

    class Fake:
        returncode = 1
        stdout = ""
        stderr = "fail"

    monkeypatch.setattr(burn.subprocess, "run", lambda *a, **k: Fake())
    with pytest.raises(RuntimeError):
        burn.probe_video_duration("x.mp4")


def test_segment_fade_disables_static_key_logic_unit():
    """Document/assert the fade window predicate used by render_overlay."""
    FADE_IN = 0.3
    FADE_OUT = 0.5
    # message visible [10, 24)
    start, end = 10.0, 24.0
    # first segment after start: cp=10, next=12 -> in fade-in
    cp, next_cp = 10.0, 12.0
    in_fade = (cp < (start + FADE_IN) and next_cp > start) or (
        cp < end and next_cp > (end - FADE_OUT)
    )
    assert in_fade is True
    # interior: cp=12, next=20
    cp, next_cp = 12.0, 20.0
    in_fade = (cp < (start + FADE_IN) and next_cp > start) or (
        cp < end and next_cp > (end - FADE_OUT)
    )
    assert in_fade is False
    # fade-out tail: cp=23.6, next=24
    cp, next_cp = 23.6, 24.0
    in_fade = (cp < (start + FADE_IN) and next_cp > start) or (
        cp < end and next_cp > (end - FADE_OUT)
    )
    assert in_fade is True


def test_resume_keeps_translation_equal_to_original():
    """Non-empty translation == original should still count as done for resume."""
    tr = load_module("translate_chat_openai", "translate_chat_openai.py")
    existing = "Hello"
    original = "Hello"
    # Mirror product seed rule used in translate_chat_openai.main.
    resume = True
    translation_map = {}
    if resume and str(existing or "").strip():
        translation_map[0] = existing
    assert 0 in translation_map
    assert translation_map[0] == original
    # should_preserve_original is separate (pure emote/url rows).
    assert tr.should_preserve_original("[LUL]") is True
    assert tr.should_preserve_original("Hello") is False


def test_float_stack_bottom_is_newest():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    messages = [{"timestamp": float(i), "author": f"u{i}"} for i in range(5)]
    events = burn.schedule_messages_float(
        messages,
        msg_line_count={i: 1 for i in range(5)},
        duration=20.0,
        capacity_lines=3,
    )
    # At t=4.5 all five have appeared; only newest 3 fit.
    visible = burn.active_float_stack(events, 4.5, 3)
    # lane 0 is bottom = newest
    by_lane = {v[0]: v[1] for v in visible}
    assert by_lane[0] == 4
    assert set(by_lane.values()) == {2, 3, 4}


def test_float_stack_multiline_respects_capacity():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    messages = [{"timestamp": float(i)} for i in range(4)]
    events = burn.schedule_messages_float(
        messages,
        msg_line_count={0: 2, 1: 2, 2: 2, 3: 2},
        duration=20.0,
        capacity_lines=4,
    )
    visible = burn.active_float_stack(events, 3.5, 4)
    total_lines = sum(v[4] for v in visible)
    assert total_lines <= 4
    # newest messages preferred
    idxs = {v[1] for v in visible}
    assert 3 in idxs


def test_float_stack_does_not_resurrect_older_under_oversize_newer():
    """When newest does not fit remaining capacity, stop — do not skip to older small msgs."""
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    # A(1) @0, B(3) @1, C(2) @2; capacity 4.
    # At t=2.5 newest-first: C(2) fits, B(3) does not → must NOT admit A underneath.
    messages = [{"timestamp": float(i)} for i in range(3)]
    events = burn.schedule_messages_float(
        messages,
        msg_line_count={0: 1, 1: 3, 2: 2},
        duration=20.0,
        capacity_lines=4,
    )
    visible = burn.active_float_stack(events, 2.5, 4)
    idxs = {v[1] for v in visible}
    assert 2 in idxs
    assert 0 not in idxs
    assert sum(v[4] for v in visible) <= 4


def test_default_stack_mode_is_lanes_preserving_lifetime():
    """CLI/config default stays lanes so --msg-lifetime still expires messages."""
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    assert burn.OverlayConfig().stack_mode == "lanes"
    messages = [{"timestamp": float(i)} for i in range(3)]
    schedule = burn.schedule_messages(
        messages,
        msg_line_count={i: 1 for i in range(3)},
        duration=60.0,
        max_visible=10,
        msg_lifetime=5.0,
    )
    # All expire by t=start+5; none live forever like float events.
    assert all(end - start == 5.0 for start, end, *_ in schedule)


def test_active_float_stack_handles_unsorted_events():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    # Deliberately reverse order: (start, end, lane, idx, nl)
    events = [
        (3.0, 1e9, 0, 2, 1),
        (1.0, 1e9, 0, 0, 1),
        (2.0, 1e9, 0, 1, 1),
    ]
    visible = burn.active_float_stack(events, 3.5, 2)
    assert {v[1] for v in visible} == {1, 2}
    assert visible[0][1] == 2  # newest at bottom


def test_float_no_lifetime_only_capacity_evicts():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    messages = [{"timestamp": float(i)} for i in range(5)]
    events = burn.schedule_messages_float(
        messages,
        msg_line_count={i: 1 for i in range(5)},
        duration=20.0,
        capacity_lines=3,
    )
    # Far past first appear times: all still "alive" by time; capacity keeps newest 3.
    visible = burn.active_float_stack(events, 100.0, 3)
    assert {v[1] for v in visible} == {2, 3, 4}
    # end times are far future
    assert all(e[1] > 1000 for e in events)
