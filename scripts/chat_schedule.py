#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lane / float scheduling for chat messages.

Extracted verbatim from twitch_chat_burn for maintainability: timestamp
admission (arrival-interval throttling), lane allocation with eviction and
min-visible protection, the Twitch-style bottom-up float stack, and the
incremental lane-visibility cursor for monotonically increasing times."""

from __future__ import annotations

import bisect


def admit_timestamp(
    source_t: float,
    last_admitted_at,
    min_arrival: float,
    *,
    throttle_from: float | None = None,
) -> float:
    """Apply optional arrival_interval throttling to a message timestamp.

    Messages with source_t < throttle_from (e.g. already-on-screen carry-in after
    rebase) keep their original timestamp so rate limiting does not empty the
    stack at preview t=0.
    """
    src = float(source_t)
    if throttle_from is not None and src < float(throttle_from):
        return src
    if last_admitted_at is None:
        return src
    return max(src, float(last_admitted_at) + max(0.0, float(min_arrival)))


def schedule_messages(
    messages,
    msg_line_count,
    duration,
    max_visible,
    msg_lifetime,
    min_visible_seconds=0.0,
    arrival_interval=0.0,
    *,
    auto_capacity: int | None = None,
):
    """
    Assign lanes for messages that intersect [0, duration).

    Returns list of (start, end, lane, msg_index, num_lines).
    Caps multi-line messages so they never request more lanes than max_visible.

    max_visible:
      - >0: fixed lane budget (legacy desktop)
      - <=0: auto — use auto_capacity (from box height / font) or default 10

    When seizing a lane range, any active schedule row whose lane span overlaps
    is truncated to t, and *all* of that row's sublanes are freed in lane_ends
    (multi-line parents are one row but occupy nl consecutive lanes).
    """
    msg_schedule = []
    lane_ends = {}
    lane_owners = {}
    life = float(msg_lifetime or 0.0)
    if life <= 0:
        # Avoid zero/negative lifetimes that make every message permanently occupy
        # a lane or produce zero-length visibility windows.
        life = 0.1
    if int(max_visible) <= 0:
        max_visible = max(1, int(auto_capacity or 10))
    else:
        max_visible = max(1, int(max_visible))
    min_visible = min(max(0.0, float(min_visible_seconds or 0.0)), life)
    min_arrival_interval = max(0.0, float(arrival_interval or 0.0))
    last_admitted_at = None

    def _evict_overlapping(base_lane: int, need_nl: int, t: float) -> bool:
        """Evict current lane owners without scanning all historical rows."""
        victims = {
            lane_owners[lane]
            for lane in range(base_lane, base_lane + need_nl)
            if lane_ends.get(lane, 0) > t and lane in lane_owners
        }
        # Check every victim first so min_visible rejection is atomic.
        for si in sorted(victims):
            s_start, s_end, s_lane, s_idx, s_nl = msg_schedule[si]
            if not (s_start <= t < s_end):
                continue
            if t - s_start < min_visible:
                return False
        for si in sorted(victims):
            s_start, s_end, s_lane, s_idx, s_nl = msg_schedule[si]
            if not (s_start <= t < s_end):
                continue
            msg_schedule[si] = (s_start, t, s_lane, s_idx, s_nl)
            for sub in range(max(1, int(s_nl))):
                lane = s_lane + sub
                if lane_owners.get(lane) == si:
                    lane_ends[lane] = t
                    lane_owners.pop(lane, None)
        return True

    dropped_past_duration = 0
    dropped_min_visible = 0
    dropped_before_start = 0
    for i, m in enumerate(messages):
        source_t = float(m.get("timestamp", 0) or 0)
        # Rate limiting delays on-screen start; lifetime is measured from admit
        # time (t+life) so delayed rows still get a full visibility window.
        # Using source_t+life with delayed t can invent inverted windows when
        # arrival_interval > remaining life.
        t = admit_timestamp(
            source_t,
            last_admitted_at,
            min_arrival_interval,
            throttle_from=0.0 if min_arrival_interval > 0 else None,
        )
        # Keep messages that can still be visible inside the render window,
        # not only those that start before duration.
        if (source_t + life) <= 0:
            # Ends before the render window opens (e.g. a large negative
            # --offset); counted and reported below instead of dropped silently.
            dropped_before_start += 1
            continue
        if t >= duration:
            dropped_past_duration += 1
            if source_t >= 0.0:
                last_admitted_at = t if last_admitted_at is None else max(float(last_admitted_at), t)
            continue

        nl = int(msg_line_count.get(i, 1) or 1)
        if nl < 1:
            nl = 1
        if nl > max_visible:
            # Prevent max_lane < 0 / empty range / ValueError on max().
            nl = max_visible

        # lane + nl - 1 < max_visible  =>  lane <= max_visible - nl
        max_lane = max_visible - nl
        end = t + life

        assigned = False
        for lane in range(max_lane + 1):
            all_free = True
            for sub in range(nl):
                if lane_ends.get(lane + sub, 0) > t:
                    all_free = False
                    break
            if all_free:
                schedule_idx = len(msg_schedule)
                msg_schedule.append((t, end, lane, i, nl))
                for sub in range(nl):
                    occupied_lane = lane + sub
                    lane_ends[occupied_lane] = end
                    lane_owners[occupied_lane] = schedule_idx
                assigned = True
                last_admitted_at = t
                break

        if not assigned:
            best_lane = 0
            best_max_end = float("inf")
            # max_lane is always >= 0 after the nl clamp above.
            for lane in range(max_lane + 1):
                max_end = max(lane_ends.get(lane + sub, 0) for sub in range(nl))
                if max_end < best_max_end:
                    best_max_end = max_end
                    best_lane = lane
            if not _evict_overlapping(best_lane, nl, t):
                # min_visible protection refused to evict in-progress messages;
                # the new arrival is dropped. Count it instead of failing silently.
                dropped_min_visible += 1
                continue
            schedule_idx = len(msg_schedule)
            msg_schedule.append((t, end, best_lane, i, nl))
            for sub in range(nl):
                occupied_lane = best_lane + sub
                lane_ends[occupied_lane] = end
                lane_owners[occupied_lane] = schedule_idx
            last_admitted_at = t

    if dropped_before_start:
        if not msg_schedule and dropped_before_start * 2 > len(messages):
            print(
                f"  [WARN] lanes 调度: {dropped_before_start}/{len(messages)} 条消息时间戳早于 0s "
                f"且无任何消息上屏，成片将没有弹幕；建议检查 --offset 是否设置过大",
                flush=True,
            )
        else:
            print(
                f"  [WARN] lanes 调度: {dropped_before_start} 条消息时间戳早于 0s 未上屏",
                flush=True,
            )
    if dropped_past_duration:
        print(
            f"  [WARN] lanes 调度: {dropped_past_duration} 条因 arrival_interval 延后超出 "
            f"时长 {float(duration):.2f}s 未上屏",
            flush=True,
        )
    if dropped_min_visible:
        print(
            f"  [WARN] lanes 调度: {dropped_min_visible} 条因场上消息未达 "
            f"min_visible_seconds={min_visible:.2f}s 不可顶替、且无空闲 lane 被丢弃",
            flush=True,
        )
    return msg_schedule



def schedule_messages_float(
    messages,
    msg_line_count,
    duration,
    capacity_lines,
    arrival_interval=0.0,
    *,
    throttle_from: float = 0.0,
):
    """Twitch-style bottom-up stack: newest at bottom, older pushed upward.

    No time-based lifetime: messages leave only when capacity pushes them off the top.
    Returns (start, end, _lane, msg_index, nl) with end far past duration so render
    treats them as alive until active_float_stack drops them for height.

    throttle_from: only delay admissions with source_t >= this value (default 0).
    Carry-in (negative rebased timestamps) keeps original times so previews open full.
    Messages delayed past duration are counted and skipped with a log when any drop.
    """
    events = []
    capacity = max(1, int(capacity_lines or 1))
    min_arrival = max(0.0, float(arrival_interval or 0.0))
    last_admitted_at = None
    forever = max(float(duration) + 3600.0, 1e9)
    dropped_past_duration = 0
    origin = float(throttle_from)

    for i, m in enumerate(messages):
        source_t = float(m.get("timestamp", 0) or 0)
        t = admit_timestamp(
            source_t,
            last_admitted_at,
            min_arrival,
            throttle_from=origin if min_arrival > 0 else None,
        )
        if t >= duration:
            dropped_past_duration += 1
            # Still advance throttle cursor so later in-window bursts stay paced.
            if source_t >= origin:
                last_admitted_at = t if last_admitted_at is None else max(float(last_admitted_at), t)
            continue
        nl = int(msg_line_count.get(i, 1) or 1)
        if nl < 1:
            nl = 1
        if nl > capacity:
            nl = capacity
        events.append((t, forever, 0, i, nl))
        # Only pace future arrivals against other in-window admits; carry-in
        # must not push the first in-window message later than its source time.
        if source_t >= origin:
            last_admitted_at = t
    if dropped_past_duration:
        print(
            f"  [WARN] float 调度: {dropped_past_duration} 条因 arrival_interval 延后超出 "
            f"时长 {float(duration):.2f}s 未上屏",
            flush=True,
        )
    # Keep events chronological so active_float_stack can skip re-sorting.
    events.sort(key=lambda e: (e[0], e[3]))
    # List subclass carries precomputed starts for O(1) bisect keys across CPs.
    out = _FloatEventList(events)
    out.starts = [e[0] for e in out]
    out.sorted_by_start = True
    return out


class _FloatEventList(list):
    """Schedule list with optional .starts cache for active_float_stack."""

    starts: list[float]
    sorted_by_start: bool = False


def active_float_stack(events, current_t, capacity_lines):
    """Build bottom-up visible stack at current_t.

    events: (start, end, _lane, msg_index, nl)
    Returns list of (lane_from_bottom, msg_index, start, end, nl) with lane 0 = bottom.
    Keeps the newest messages that fit in capacity_lines (oldest dropped from the top).

    Performance: scan candidates newest-first and stop at the capacity wall
    (typically O(capacity) work after a bisect, not O(all history)) so long VODs
    stay usable under float mode.
    """
    capacity = max(1, int(capacity_lines or 1))
    if not events:
        return []

    # Product schedules carry a validated starts cache. Trust that marker instead
    # of re-scanning all VOD history at every change point.
    cached_starts = getattr(events, "starts", None)
    known_sorted = (
        isinstance(events, _FloatEventList)
        and bool(getattr(events, "sorted_by_start", False))
        and cached_starts is not None
        and len(cached_starts) == len(events)
    )
    needs_sort = False
    if not known_sorted:
        for i in range(1, len(events)):
            if (events[i][0], events[i][3]) < (events[i - 1][0], events[i - 1][3]):
                needs_sort = True
                break
    ordered = sorted(events, key=lambda e: (e[0], e[3])) if needs_sort else events

    # Candidates with start <= current_t (float ends are far future / open).
    # Prefer precomputed starts from schedule_messages_float (full-render hot path).
    starts = cached_starts if known_sorted else None
    if starts is None or len(starts) != len(ordered) or needs_sort:
        starts = [e[0] for e in ordered]
    hi = bisect.bisect_right(starts, current_t)
    selected = []  # newest-first
    used = 0
    for j in range(hi - 1, -1, -1):
        start, end, _lane, idx, nl = ordered[j]
        if not (start <= current_t < end):
            continue
        nl = max(1, int(nl))
        if used + nl > capacity:
            # Stop at the capacity wall. Skipping would resurrect older smaller
            # messages under a newer multi-line one — not Twitch bottom-up.
            break
        selected.append((start, end, idx, nl))
        used += nl
    out = []
    lane = 0
    for start, end, idx, nl in selected:  # newest first => lane 0 at bottom
        out.append((lane, idx, start, end, nl))
        lane += nl
    return out

class _LaneVisibilityCursor:
    """Incrementally resolve visible lane rows for monotonically increasing times."""

    def __init__(self, schedule):
        self._events = sorted(
            enumerate(schedule),
            key=lambda item: (item[1][0], item[0]),
        )
        self._cursor = 0
        self._active = {}
        self._last_t = float("-inf")

    def _reset(self):
        self._cursor = 0
        self._active.clear()
        self._last_t = float("-inf")

    def at(self, current_t):
        t = float(current_t)
        if t < self._last_t:
            self._reset()
        while (
            self._cursor < len(self._events)
            and self._events[self._cursor][1][0] <= t
        ):
            schedule_i, row = self._events[self._cursor]
            self._cursor += 1
            if row[1] > t:
                self._active[schedule_i] = row
        expired = [i for i, row in self._active.items() if row[1] <= t]
        for schedule_i in expired:
            self._active.pop(schedule_i, None)
        self._last_t = t
        visible = [
            (row[2], row[3], row[0], row[1], row[4])
            for row in self._active.values()
        ]
        visible.sort(key=lambda row: row[0])
        return visible
