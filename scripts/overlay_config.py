#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared overlay configuration object (replaces ad-hoc Config class attributes)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class OverlayConfig:
    x: int = 15
    y: int = 327
    width: int = 497
    height: int = 363
    font_size: int = 15
    font_path: str = "auto"
    font_bold_path: str = "auto"
    fps: int = 15
    # Final published video FPS (float; may be NTSC 30000/1001). None = probe source.
    output_fps: float | None = None
    # 0 = auto-fill by overlay box height / font size (resolve_lane_budget).
    max_visible: int = 0
    msg_lifetime: float = 14.0
    # Optional readability controls. Zero keeps legacy desktop behavior.
    max_message_lines: int = 0
    min_visible_seconds: float = 0.0
    arrival_interval: float = 0.0
    # Chat stack behavior: "lanes" = lifetime lane deposit (CLI default, preserves
    # historical msg_lifetime eviction); "float" = Twitch bottom-up capacity push
    # (opt-in / layout_mobile).
    stack_mode: str = "lanes"
    # Source-video-relative dimensions. Zero keeps the corresponding pixel value.
    x_ratio: float = 0.0
    y_ratio: float = 0.0
    width_ratio: float = 0.0
    height_ratio: float = 0.0
    font_size_ratio: float = 0.0
    bg_alpha: int = 255
    emote_h: int = 22
    preview_frame: float | None = None
    preview_image: str | None = None
    preview_clip: float | None = None
    # Absolute start of a densest-segment preview clip (seconds on the offset timeline).
    # Overlay timestamps are shifted by this amount so the clip still renders from t=0.
    preview_clip_start: float = 0.0
    # Performance / encode knobs
    reuse_static_frames: bool = True
    skip_blank_frames: bool = True
    blank_hold_seconds: float = 0.5
    lazy_message_images: bool = False
    message_image_cache_size: int = 256
    no_backup_prev: bool = False
    encode: Any = None  # EncodeOptions | None
    stage_timings: dict[str, float] = field(default_factory=dict)
    frame_stats: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        enc = self.encode
        if enc is not None and hasattr(enc, "to_dict"):
            data["encode"] = enc.to_dict()
        return data
