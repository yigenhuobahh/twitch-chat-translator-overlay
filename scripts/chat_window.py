#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Time-offset diagnosis and preview time-window filtering for chat messages."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

# 预览窗口右端余量（秒）：窗口按闭右端 [start, t + slack] 过滤，
# 保证恰好落在预览时刻的消息仍可见。
_PREVIEW_WINDOW_SLACK_S = 0.05

# 预览模式成片时长（秒）：预览模式下未显式给 --preview-clip/--preview-frame 时
# 回退到此值。与 job_wizard._PREVIEW_CLIP_SECONDS 同源（job_wizard 侧写入
# job.yaml 的 preview_clip 与转发 "--preview-clip 10" 都取同一常量；勿只改一处）。
# 上波 8b3da86 预览常量化（_PREVIEW_WINDOW_SLACK_S / _PREVIEW_MODES）的延续。
_PREVIEW_CLIP_SECONDS = 10.0


def compute_time_offset(
    messages: list[dict],
    video_duration: float | None,
    manual_offset: float | None = None,
) -> dict[str, Any]:
    """
    Decide chat timestamp offset.

    Returns a structured diagnosis:
      offset, mode (manual|auto|none), warnings[], first_ts, last_ts, msg_span, video_duration
    """
    result: dict[str, Any] = {
        "offset": 0.0,
        "mode": "none",
        "auto_detected": False,
        "warnings": [],
        "first_ts": None,
        "last_ts": None,
        "msg_span": None,
        "video_duration": video_duration,
        "confirm_with_preview": False,
    }
    if not messages:
        return result

    first_ts = float(messages[0]["timestamp"])
    last_ts = float(messages[-1]["timestamp"])
    msg_span = last_ts - first_ts
    result["first_ts"] = first_ts
    result["last_ts"] = last_ts
    result["msg_span"] = msg_span

    if manual_offset is not None:
        result["offset"] = float(manual_offset)
        result["mode"] = "manual"
        return result

    if video_duration is None:
        result["warnings"].append("无法读取视频时长，跳过自动时间偏移检测")
        return result

    # Auto: first message far beyond video length, but span roughly matches video.
    if first_ts > video_duration and msg_span <= video_duration + 5:
        result["offset"] = first_ts
        result["mode"] = "auto"
        result["auto_detected"] = True
        result["confirm_with_preview"] = True
        result["warnings"].append(
            f"自动检测时间偏移 {first_ts:.0f}s（启发式，请用 --preview-frame / --preview-clip 确认）"
        )
        return result

    if first_ts > 60 and msg_span < video_duration * 0.5:
        result["warnings"].append(
            f"消息跨度 ({msg_span:.0f}s) 远小于视频时长 ({video_duration:.0f}s)；"
            f"首条 {first_ts:.0f}s / 末条 {last_ts:.0f}s，可能存在未修正偏移，建议 --offset"
        )
    elif first_ts > video_duration * 0.9:
        result["warnings"].append(
            f"首条消息时间戳 ({first_ts:.0f}s) 接近或超过视频时长 ({video_duration:.0f}s)，"
            f"但跨度 ({msg_span:.0f}s) 与视频不匹配，自动检测未触发；建议 --offset"
        )
    elif first_ts < 5 and msg_span > video_duration + 10:
        result["warnings"].append(
            f"消息跨度 ({msg_span:.0f}s) 明显超过视频时长 ({video_duration:.0f}s)，"
            f"可能含片段外消息；建议预览确认或 --offset"
        )
    return result


def apply_time_offset(messages: list[dict], offset: float) -> list[dict]:
    """Shift messages into video-relative time; preserve stream_timestamp for export identity.

    HTML timestamps are stream-absolute (seconds from broadcast start). After offset,
    ``timestamp`` is video-relative. ``stream_timestamp`` keeps the pre-offset value so
    translation JSON can identity-match across different --offset choices.

    Negative results are kept on purpose: messages before the video start are
    pre-show carry-in. Both schedulers drop a message only when its whole
    lifetime already ended (``(source_t + msg_lifetime) <= 0``), so clamping to
    0 here would burst every pre-show message into the lanes simultaneously at
    t=0 instead of showing them mid-life.
    """
    if not offset:
        # Still stamp stream_timestamp when missing so export/import share one field.
        for m in messages:
            if m.get("stream_timestamp") is None:
                try:
                    m["stream_timestamp"] = float(m.get("timestamp", 0) or 0)
                except (TypeError, ValueError):
                    m["stream_timestamp"] = 0.0
        return messages
    off = float(offset)
    for m in messages:
        try:
            current = float(m.get("timestamp", 0) or 0)
        except (TypeError, ValueError):
            current = 0.0
        if m.get("stream_timestamp") is None:
            m["stream_timestamp"] = current
        try:
            stream = float(m["stream_timestamp"])
        except (TypeError, ValueError):
            stream = current
            m["stream_timestamp"] = stream
        m["timestamp"] = stream - off
    return messages


def apply_preview_first_defaults(
    args,
    cli_defaults: dict[str, Any] | None = None,
    *,
    explicit_overlay_codec: bool | None = None,
) -> list[str]:
    """Apply safe preview-mode defaults without overriding explicit choices.

    Only when ``mode == "preview"``:
      - if both preview_clip and preview_frame are None: set preview_clip=10
      - if overlay_codec is still the CLI default (vp9), no render_preset, and
        the user did not pass --overlay-codec: use png
    Returns names of fields that were changed.
    """
    applied: list[str] = []
    if str(getattr(args, "mode", None) or "").strip().lower() != "preview":
        return applied

    if getattr(args, "preview_clip", None) is None and getattr(args, "preview_frame", None) is None:
        args.preview_clip = _PREVIEW_CLIP_SECONDS
        applied.append("preview_clip")

    defaults = dict(cli_defaults or {})
    default_codec = defaults.get("overlay_codec", "vp9")
    if explicit_overlay_codec:
        return applied
    if (
        getattr(args, "overlay_codec", None) == default_codec
        and default_codec == "vp9"
        and not getattr(args, "render_preset", None)
    ):
        args.overlay_codec = "png"
        applied.append("overlay_codec")
    return applied


def format_offset_diagnosis(diag: dict[str, Any] | None) -> str:
    """Human-readable multi-line Chinese summary of compute_time_offset() result."""
    if not diag:
        return "时间轴诊断: 无数据"

    mode = str(diag.get("mode") or "none")
    offset = float(diag.get("offset") or 0.0)
    first_ts = diag.get("first_ts")
    last_ts = diag.get("last_ts")
    msg_span = diag.get("msg_span")
    video_duration = diag.get("video_duration")
    warnings = list(diag.get("warnings") or [])
    confirm = bool(diag.get("confirm_with_preview"))

    mode_label = {
        "manual": "手动 --offset",
        "auto": "自动检测",
        "none": "未偏移 (offset=0)",
    }.get(mode, mode)

    lines = [
        "======== 时间轴 / Offset 诊断 ========",
        f"模式: {mode_label}",
        f"offset: {offset:.3f}s",
    ]
    if first_ts is not None:
        lines.append(f"首条消息时间戳: {float(first_ts):.1f}s")
    if last_ts is not None:
        lines.append(f"末条消息时间戳: {float(last_ts):.1f}s")
    if msg_span is not None:
        lines.append(f"消息跨度: {float(msg_span):.1f}s")
    if video_duration is not None:
        lines.append(f"视频时长: {float(video_duration):.1f}s")
    else:
        lines.append("视频时长: 未知")

    if warnings:
        lines.append("警告:")
        for w in warnings:
            lines.append(f"  - {w}")
    else:
        lines.append("警告: 无")

    if confirm or mode == "auto":
        lines.append("建议: 用预览确认后再出长片（启发式偏移可能不准）")
    elif mode == "none" and warnings:
        lines.append("建议: 检查是否需要手动 --offset")
    elif mode == "manual":
        lines.append("建议: 已使用手动偏移；仍可用预览核对布局")
    else:
        lines.append("建议: 首次建议预览确认布局与时间轴")

    lines.append("下一步:")
    lines.append("  --preview-frame <秒>     导出单帧预览图")
    lines.append("  --preview-clip 10        渲染 10 秒短片")
    lines.append("  --mode preview           预览优先（默认 10s + 更快 overlay）")
    lines.append("  --offset <秒>            手动指定时间偏移")
    lines.append("====================================")
    return "\n".join(lines)


def resolve_preview_frame_time(
    preview_frame: float | None,
    preview_clip: float | None,
    video_duration: float | None,
) -> tuple[float | None, str | None]:
    """Clamp --preview-frame to the effective preview duration used by rendering.

    The renderer clamps the preview instant to min(source_duration, preview_clip)
    (OverlayScenePlan.preview_time). Chat filtering must use the same instant or
    the kept messages all land past the rendered duration and the preview goes
    empty (lanes) / misaligned (float).

    Returns (preview_time, warning). warning is a Chinese one-liner when the
    frame was clamped past the duration or when both --preview-frame and
    --preview-clip are given; None means the instant is used as-is.
    """
    if preview_frame is None:
        return None, None
    t = max(0.0, float(preview_frame))
    duration: float | None = None
    if video_duration is not None and float(video_duration) > 0:
        duration = float(video_duration)
    clip_len: float | None = None
    if preview_clip is not None and float(preview_clip) > 0:
        clip_len = max(0.0, float(preview_clip))
        duration = clip_len if duration is None else min(duration, clip_len)
    warning = None
    if duration is not None:
        clamped = max(0.0, min(t, duration))
        if clamped != t:
            warning = (
                f"--preview-frame {t:g}s 超出预览时长 {duration:g}s，"
                f"已按 t={clamped:g}s 过滤聊天并渲染预览帧"
            )
        t = clamped
        if clip_len is not None:
            note = (
                f"同时指定 --preview-clip 与 --preview-frame: "
                f"聊天过滤与预览帧均取 t={t:g}s (预览时长 {duration:g}s)"
            )
            warning = f"{warning}；{note}" if warning else note
    return t, warning


def preview_window(
    preview_frame: float | None,
    preview_clip: float | None,
    msg_lifetime: float,
    *,
    clip_start: float | None = None,
) -> tuple[float | None, float | None]:
    """
    Return (window_start, window_end) for message filtering.
    None/None means full timeline (no filtering).

    clip_start: optional start of a preview-clip window (for densest-segment mode).
    """
    life = max(0.0, float(msg_lifetime or 0.0))
    if preview_frame is not None:
        t = max(0.0, float(preview_frame))
        # Keep messages that can still be visible at t.
        return max(0.0, t - life), t + _PREVIEW_WINDOW_SLACK_S
    if preview_clip is not None:
        length = max(0.0, float(preview_clip))
        start = max(0.0, float(clip_start or 0.0))
        # Messages that start within the clip, or are still alive into the clip.
        return start, start + length
    return None, None


def find_densest_preview_start(
    messages: list[dict],
    clip_len: float,
    *,
    video_duration: float | None = None,
    msg_lifetime: float = 14.0,
) -> dict[str, Any]:
    """Pick the preview-clip start time with the densest overlapping chat.

    Scores each candidate window [t, t+clip_len] by how many messages are visible
    in that window (using msg_lifetime). Candidates are message timestamps and 0.

    Returns dict: start, end, score, mode ('dense'|'head'), optional warning.

    When video_duration is unknown, force start=0 (do not seek past EOF using chat max).
    Scoring is O(N log N): sort timestamps once, bisect per candidate.
    """
    import bisect

    length = max(0.1, float(clip_len or 0.0))
    life = max(0.0, float(msg_lifetime or 0.0))
    result: dict[str, Any] = {
        "start": 0.0,
        "end": length,
        "score": 0,
        "mode": "head",
    }
    if not messages:
        return result

    times: list[float] = []
    for m in messages:
        try:
            times.append(float(m.get("timestamp", 0) or 0))
        except (TypeError, ValueError):
            continue
    if not times:
        return result
    times.sort()

    if video_duration is None or float(video_duration) <= 0:
        # Refuse mid-seek without a known media duration (chat max_t can exceed the VOD).
        result["warning"] = "video duration unknown; dense preview forced to start=0"
        end = length
        lo = bisect.bisect_right(times, 0.0 - life)
        hi = bisect.bisect_right(times, end)
        result["score"] = int(max(0, hi - lo))
        result["end"] = float(end)
        return result

    max_start = max(0.0, float(video_duration) - length)

    # One primary candidate family (message starts) plus clip-aligned offsets.
    candidates = {0.0}
    for ts in times:
        candidates.add(max(0.0, min(max_start, ts)))
        if length > 0:
            candidates.add(max(0.0, min(max_start, ts - length * 0.5)))

    best_start = 0.0
    best_score = -1
    for start in sorted(candidates):
        end = start + length
        # Visible iff start-life < t <= end  (same as message_visible_in_window).
        lo = bisect.bisect_right(times, start - life)
        hi = bisect.bisect_right(times, end)
        score = hi - lo
        if score > best_score:
            best_score = score
            best_start = start

    result["start"] = float(best_start)
    result["end"] = float(best_start + length)
    result["score"] = int(max(0, best_score))
    result["mode"] = "dense" if best_start > 1e-6 or best_score > 0 else "head"
    return result


def message_visible_in_window(timestamp: float, window_start: float, window_end: float, msg_lifetime: float) -> bool:
    """True if a message starting at timestamp can appear during [window_start, window_end]."""
    t = float(timestamp)
    life = max(0.0, float(msg_lifetime or 0.0))
    msg_end = t + life
    # Overlap between [t, msg_end) and [window_start, window_end]
    return (t <= window_end) and (msg_end > window_start)


def _newest_pre_window_rows(pre_rows: list, capacity: int) -> tuple[list, int, int]:
    """Keep only the newest ``capacity`` pre-window rows, sorted ascending by timestamp.

    Single shared implementation for the float preview prefilter (before deepcopy,
    to avoid materializing the whole VOD) and trim_float_carry_in_messages (after
    filtering). Rows are ``(timestamp, msg)`` tuples.

    Returns (kept_rows, rows_before, rows_after). Per-message line cost is not
    modeled here: active_float_stack enforces the real multi-line line budget at
    draw time.
    """
    rows = sorted(pre_rows, key=lambda row: row[0])
    keep_n = min(len(rows), max(1, int(capacity)))
    return rows[-keep_n:] if keep_n else [], len(rows), keep_n


def filter_chat_for_time_window(
    chat_data: dict,
    window_start: float | None,
    window_end: float | None,
    msg_lifetime: float = 14.0,
    *,
    rebase_to_zero: bool = False,
    float_capacity_lines: int | None = None,
    max_message_lines: int = 0,
) -> dict:
    """
    Return a shallow-filtered chat_data limited to messages relevant for a time window.
    Also drops unused emote_map entries so preview does not decode every emote.

    rebase_to_zero: when True, subtract window_start from each kept timestamp so the
    clip can be rendered/composed as if it starts at t=0 (pair with accurate seek).

    Carry-in messages that started before the window keep a *negative* rebased
    timestamp so lanes lifetime remaining and float stack age stay correct
    (clamping to 0 would burst them all as simultaneous new arrivals at t=0).

    float_capacity_lines: when set, only the newest pre-window rows that fit this
    *line* budget (plus all in-window arrivals) are deep-copied — avoids materializing
    the full VOD chat for float mid-clip previews.
    max_message_lines: accepted for backward compatibility; the prefilter keeps the
    newest ``float_capacity_lines`` *messages* (1 line each) and the real multi-line
    line budget is enforced later by active_float_stack.
    """
    if window_start is None or window_end is None:
        return chat_data

    data = {
        "messages": [],
        "emote_map": {},
    }
    # Preserve any extra top-level keys if present.
    for k, v in chat_data.items():
        if k not in ("messages", "emote_map"):
            data[k] = v

    # First pass: select which source messages to keep (no deepcopy yet).
    selected: list[tuple[float, dict]] = []
    for msg in chat_data.get("messages") or []:
        ts = float(msg.get("timestamp", 0) or 0)
        if not message_visible_in_window(ts, window_start, window_end, msg_lifetime):
            continue
        selected.append((ts, msg))

    prefilter_meta = None
    if float_capacity_lines is not None and selected:
        cap = max(1, int(float_capacity_lines))
        # Prefilter counts messages, not lines (1 line/msg) so single-line mobile
        # stacks open full; active_float_stack enforces the real line budget later.
        win0 = float(window_start)
        pre = [(ts, m) for ts, m in selected if ts < win0]
        in_win = [(ts, m) for ts, m in selected if ts >= win0]
        keep_pre, pre_before, pre_after = _newest_pre_window_rows(pre, cap)
        prefilter_meta = {
            "float_capacity_lines": cap,
            "pre_window_before": pre_before,
            "pre_window_after": pre_after,
        }
        selected = keep_pre + in_win

    used_classes: set[str] = set()
    shift = float(window_start) if rebase_to_zero else 0.0
    for ts, msg in selected:
        kept = deepcopy(msg)
        if rebase_to_zero:
            # Intentionally allow negative values (carry-in / remaining life).
            kept["timestamp"] = ts - shift
        data["messages"].append(kept)
        for frag in kept.get("fragments") or []:
            if frag.get("type") == "emote" and frag.get("class"):
                used_classes.add(str(frag["class"]))

    emote_map = chat_data.get("emote_map") or {}
    data["emote_map"] = {cls: path for cls, path in emote_map.items() if cls in used_classes}
    data["_window"] = {
        "start": window_start,
        "end": window_end,
        "msg_lifetime": msg_lifetime,
        "rebase_to_zero": bool(rebase_to_zero),
        "kept_messages": len(data["messages"]),
        "kept_emotes": len(data["emote_map"]),
        "source_messages": len(chat_data.get("messages") or []),
        "source_emotes": len(emote_map),
    }
    if prefilter_meta:
        data["_window"]["float_prefilter"] = prefilter_meta
    return data


def trim_float_carry_in_messages(
    chat_data: dict,
    window_start: float,
    capacity_lines: int,
    *,
    max_message_lines: int = 0,
) -> dict:
    """After a wide-horizon float preview filter, drop excess pre-window history.

    Float has no time lifetime: only the newest pre-window messages that fit
    ``capacity_lines`` (line budget) can still be on-screen at window_start.
    In-window arrivals (timestamp >= window_start) are always kept.

    Shares the newest-rows selection with the prefilter via
    ``_newest_pre_window_rows``. ``max_message_lines`` is accepted for backward
    compatibility; per-message line cost is enforced by active_float_stack at
    draw time, not here.
    """
    capacity = max(1, int(capacity_lines or 1))
    messages = list(chat_data.get("messages") or [])
    if not messages:
        return chat_data

    # Match prefilter: keep newest `capacity` pre-window messages (1 line each).
    start = float(window_start)
    pre: list[tuple[float, dict]] = []
    in_window: list[dict] = []
    for msg in messages:
        ts = float(msg.get("timestamp", 0) or 0)
        if ts >= start:
            in_window.append(msg)
        else:
            pre.append((ts, msg))

    # Fast path: all pre fit under line≈1 capacity.
    if len(pre) <= capacity:
        return chat_data

    keep_pre, pre_before, pre_after = _newest_pre_window_rows(pre, capacity)
    kept = [msg for _ts, msg in keep_pre] + in_window

    used_classes: set[str] = set()
    for msg in kept:
        for frag in msg.get("fragments") or []:
            if frag.get("type") == "emote" and frag.get("class"):
                used_classes.add(str(frag["class"]))

    emote_map = chat_data.get("emote_map") or {}
    data = {
        "messages": kept,
        "emote_map": {cls: path for cls, path in emote_map.items() if cls in used_classes},
    }
    for k, v in chat_data.items():
        if k not in ("messages", "emote_map"):
            data[k] = v
    if isinstance(data.get("_window"), dict):
        data["_window"] = dict(data["_window"])
        data["_window"]["float_carry_trim"] = True
        data["_window"]["kept_messages"] = len(kept)
        data["_window"]["kept_emotes"] = len(data["emote_map"])
        data["_window"]["pre_window_before_trim"] = pre_before
        data["_window"]["pre_window_after_trim"] = pre_after
    return data
