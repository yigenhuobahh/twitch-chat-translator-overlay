#!/usr/bin/env python3
"""Immutable planning for chat-overlay frame rendering."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

AUTO_LAZY_MESSAGE_THRESHOLD = 1000


def expected_overlay_frame_count(duration: float, fps: float) -> int:
    """Return the frame count that covers the half-open interval [0, duration)."""
    if duration <= 0 or fps <= 0:
        return 1
    return max(1, int(math.ceil(float(duration) * float(fps) - 1e-9)))


def frame_index_range(start_t: float, end_t: float, fps: float, total_frames: int) -> tuple[int, int]:
    """Map a half-open time range onto global overlay frame indexes."""
    if total_frames <= 0 or fps <= 0:
        return 0, 0
    start_i = int(math.ceil(float(start_t) * float(fps) - 1e-12))
    end_i = int(math.ceil(float(end_t) * float(fps) - 1e-12))
    start_i = max(0, min(total_frames, start_i))
    end_i = max(0, min(total_frames, end_i))
    return start_i, max(start_i, end_i)


def line_height_px(font_size: int) -> int:
    """Return the common single-line pitch used by planning and rasterization."""
    return max(1, int(font_size) + 14)


def compute_lane_capacity(height: int, font_size: int, *, bottom_pad: int = 4) -> int:
    """Return how many one-line chat rows fit inside an overlay box."""
    usable = max(1, int(height) - int(bottom_pad))
    return max(1, usable // line_height_px(font_size))


def resolve_lane_budget(
    max_visible: int,
    height: int,
    font_size: int,
    *,
    bottom_pad: int = 4,
) -> tuple[int, int, str | None]:
    """Resolve requested lanes to a safe effective line capacity."""
    capacity = compute_lane_capacity(height, font_size, bottom_pad=bottom_pad)
    raw = int(max_visible or 0)
    if raw <= 0:
        return capacity, capacity, None
    if raw > capacity:
        line_h = line_height_px(font_size)
        warning = (
            f"max_visible={raw} 超过当前框高可容纳的 {capacity} 行 "
            f"(height={int(height)}px, font={int(font_size)}px, LINE_H={line_h})，"
            f"已钳制为 {capacity}，避免弹幕叠在顶部"
        )
        return capacity, capacity, warning
    return raw, capacity, None


def resolve_message_image_cache_policy(
    message_count: int,
    requested_lazy: bool,
    cache_size: int,
) -> tuple[bool, int, bool]:
    """Choose the bitmap cache policy without mutating the overlay config."""
    count = max(0, int(message_count or 0))
    capacity = max(8, int(cache_size or 256))
    auto_enabled = count >= AUTO_LAZY_MESSAGE_THRESHOLD
    return bool(requested_lazy or auto_enabled), capacity, auto_enabled


@dataclass(frozen=True)
class OverlayScenePlan:
    """Inputs and deterministic rendering budgets for one overlay scene."""

    source_duration: float
    duration: float
    fps: float
    total_frames: int
    stack_mode: str
    raw_max_visible: int
    max_visible: int
    auto_capacity: int
    budget_warning: str | None
    preview_clip: float | None
    preview_clip_start: float
    preview_frame_time: float | None
    preview_time: float | None
    float_throttle_from: float
    lazy_message_images: bool
    message_image_cache_size: int
    auto_lazy_message_images: bool

    @classmethod
    def from_config(cls, *, source_duration: float, config: Any, message_count: int) -> OverlayScenePlan:
        duration = max(0.0, float(source_duration))
        fps = float(getattr(config, "fps", 0) or 0)
        requested_clip = getattr(config, "preview_clip", None)
        preview_clip = None
        if requested_clip is not None and float(requested_clip) > 0:
            preview_clip = float(requested_clip)
            duration = min(duration, preview_clip)
        clip_start = max(0.0, float(getattr(config, "preview_clip_start", 0.0) or 0.0))
        raw_max_visible = int(getattr(config, "max_visible", 0) or 0)
        max_visible, auto_capacity, budget_warning = resolve_lane_budget(
            raw_max_visible,
            int(getattr(config, "height", 0) or 0),
            int(getattr(config, "font_size", 0) or 0),
        )
        stack_mode = str(getattr(config, "stack_mode", "lanes") or "lanes").strip().lower()
        if stack_mode not in {"float", "lanes"}:
            stack_mode = "lanes"
        preview_frame = getattr(config, "preview_frame", None)
        preview_frame_time = float(preview_frame) if preview_frame is not None else None
        preview_time = None
        if preview_frame_time is not None:
            preview_time = max(0.0, min(preview_frame_time, duration))
        if clip_start > 1e-6:
            float_throttle_from = 0.0
        elif preview_frame_time is not None:
            float_throttle_from = max(0.0, preview_frame_time)
        else:
            float_throttle_from = 0.0
        lazy, cache_size, auto_lazy = resolve_message_image_cache_policy(
            message_count,
            bool(getattr(config, "lazy_message_images", False)),
            int(getattr(config, "message_image_cache_size", 256) or 256),
        )
        return cls(
            source_duration=float(source_duration),
            duration=duration,
            fps=fps,
            total_frames=1 if preview_frame_time is not None else expected_overlay_frame_count(duration, fps),
            stack_mode=stack_mode,
            raw_max_visible=raw_max_visible,
            max_visible=max_visible,
            auto_capacity=auto_capacity,
            budget_warning=budget_warning,
            preview_clip=preview_clip,
            preview_clip_start=clip_start,
            preview_frame_time=preview_frame_time,
            preview_time=preview_time,
            float_throttle_from=float_throttle_from,
            lazy_message_images=lazy,
            message_image_cache_size=cache_size,
            auto_lazy_message_images=auto_lazy,
        )
