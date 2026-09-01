#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render chat messages into an overlay PNG frame sequence.

Extracted from twitch_chat_burn for maintainability. The message rasterization
closures (emote asset assembly, font resolution, line measurement, single
message bitmap rendering, message-image cache) are collected in FrameRenderer;
``render_overlay`` stays the orchestration shell (scene budget, scheduling,
frame loop, sparse-blank expansion, preview extraction).

render_overlay returns a RenderResult and never mutates OverlayConfig at
runtime — main() injects the result values into the config object at the
pipeline boundary (the only legitimate writeback point).
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import subprocess
import time

from chat_schedule import (
    _LaneVisibilityCursor,
    active_float_stack,
    schedule_messages,
    schedule_messages_float,
)
from chat_text_layout import (
    MESSAGE_BADGE_SIZE,
    MESSAGE_GAP,
    MESSAGE_INDENT,
    MESSAGE_PAD,
    badge_color_for,
    hex_to_rgb,
    layout_message_lines,
)
from common_utils import require_executable
import media_probe
from overlay_scene import (
    AUTO_LAZY_MESSAGE_THRESHOLD,
    OverlayScenePlan,
    frame_index_range,
    line_height_px,
)
from process_util import is_dangerous_publish_path, path_is_under
from render_perf import (
    blank_gap_frame_indexes,
    ensure_render_disk_headroom,
    expand_frame_sequence_for_ffmpeg,
    write_or_reuse_frame,
)

_PREVIEW_FRAME_TIMEOUT_SECONDS = 120.0
_MAX_EMOTE_ANIMATION_FRAMES = 300
_MAX_EMOTE_SOURCE_PIXELS = 4_000_000
_MAX_EMOTE_DECODED_BYTES_PER_ASSET = 32 * 1024 * 1024
_MAX_EMOTE_DECODED_BYTES_TOTAL = 256 * 1024 * 1024

# Chat fade envelope (seconds): message alpha ramps in over FADE_IN_SECONDS from
# its first visible frame and out over FADE_OUT_SECONDS before it leaves.
FADE_IN_SECONDS = 0.3
FADE_OUT_SECONDS = 0.5


def emote_decode_plan(
    width,
    height,
    frame_count,
    target_height,
    remaining_bytes,
):
    """Validate one emote before Pillow materializes every animation frame."""
    width = int(width)
    height = int(height)
    frame_count = int(frame_count)
    target_height = int(target_height)
    remaining_bytes = max(0, int(remaining_bytes))
    if width <= 0 or height <= 0 or frame_count <= 0 or target_height <= 0:
        raise ValueError("emote dimensions, frame count, and target height must be positive")
    if width * height > _MAX_EMOTE_SOURCE_PIXELS:
        raise ValueError(f"emote source has too many pixels ({width}x{height})")
    if frame_count > _MAX_EMOTE_ANIMATION_FRAMES:
        raise ValueError(
            f"emote animation has too many frames "
            f"({frame_count} > {_MAX_EMOTE_ANIMATION_FRAMES})"
        )
    target_width = max(1, int(width * target_height / height))
    decoded_bytes = target_width * target_height * 4 * frame_count
    if decoded_bytes > _MAX_EMOTE_DECODED_BYTES_PER_ASSET:
        raise ValueError(
            f"emote decoded size exceeds per-asset budget ({decoded_bytes} bytes)"
        )
    if decoded_bytes > remaining_bytes:
        raise ValueError(
            f"emote decoded size exceeds remaining global budget ({decoded_bytes} bytes)"
        )
    return target_width, decoded_bytes


def _store_message_image(msg_images, msg_lines, idx, image, nl, *, lazy, cache_cap):
    msg_lines[idx] = nl
    msg_images[idx] = image
    msg_images.move_to_end(idx)
    if lazy:
        while len(msg_images) > cache_cap:
            evicted_idx, _image = msg_images.popitem(last=False)
            msg_lines.pop(evicted_idx, None)
    return len(msg_images)


@dataclass
class RenderResult:
    """Outcome of render_overlay.

    Fields mirror the values that used to be written back onto OverlayConfig
    (frame_stats / stage_timings / preview_image) plus the frame bookkeeping
    main() needs; main() injects them into the config at the boundary.
    """

    frames_dir: str
    duration: float
    frame_count: int
    stats: dict[str, int] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    preview_path: str | None = None


class FrameRenderer:
    """Message rasterization for the overlay frame loop.

    Collects the closures that used to live inside render_overlay: emote asset
    decoding, font resolution, message-line measurement, single-message bitmap
    rendering and the message-image cache. Logic is verbatim; render_overlay's
    frame loop drives it via measure_message_lines / prepare_message_cache /
    message_image / composite_visible.
    """

    def __init__(self, messages, emote_map, config):
        # GIF / animated WebP 不能直接 convert，否则只会取第一帧。
        # 预解码后按消息显示时间选择动画帧。
        from PIL import Image, ImageDraw, ImageFont

        self.messages = messages
        self.config = config
        self._Image = Image
        self._ImageDraw = ImageDraw

        emote_imgs = {}
        decoded_emote_bytes = 0
        for cls, path in emote_map.items():
            try:
                with Image.open(path) as source:
                    frame_count = int(getattr(source, "n_frames", 1) or 1)
                    target_width, decoded_bytes = emote_decode_plan(
                        source.width,
                        source.height,
                        frame_count,
                        config.emote_h,
                        _MAX_EMOTE_DECODED_BYTES_TOTAL - decoded_emote_bytes,
                    )
                    frames = []
                    durations = []
                    for frame_index in range(frame_count):
                        source.seek(frame_index)
                        image = source.convert("RGBA")
                        if (
                            image.width != target_width
                            or image.height != config.emote_h
                        ):
                            image = image.resize(
                                (target_width, config.emote_h),
                                Image.LANCZOS,
                            )
                        frames.append(image)
                        durations.append(
                            max(10, int(source.info.get("duration", 100)))
                        )
                if not frames:
                    raise ValueError("emote has no decodable frames")
                decoded_emote_bytes += decoded_bytes
                emote_imgs[cls] = {
                    "frames": frames,
                    "durations": durations,
                    "cycle_ms": sum(durations),
                    "width": frames[0].width,
                }
                state = f"动画 {len(frames)} 帧" if len(frames) > 1 else "静态"
                print(
                    f"  emote: {cls} "
                    f"({frames[0].width}x{frames[0].height}, {state})",
                    flush=True,
                )
            except Exception as exc:
                print(f"  emote 加载失败 {cls}: {exc}", flush=True)
        self.emote_imgs = emote_imgs

        # 字体
        try:
            font = ImageFont.truetype(config.font_path, config.font_size)
            font_bold = ImageFont.truetype(config.font_bold_path or config.font_path, config.font_size)
        except OSError as e:
            raise RuntimeError(
                f"无法加载字体: regular={config.font_path!r} bold={config.font_bold_path!r}: {e}. "
                "请安装 CJK 字体或用 --font-path 指定。"
            ) from e
        self.font = font
        self.font_bold = font_bold

        self.line_h = line_height_px(config.font_size)
        # Layout constants shared by line-count prepass and bitmap render.
        self.padding = MESSAGE_PAD
        self.badge_size = MESSAGE_BADGE_SIZE
        self.gap = MESSAGE_GAP
        self.indent = MESSAGE_INDENT

        self.max_w = config.width - 4
        self.max_message_lines = max(0, int(getattr(config, "max_message_lines", 0) or 0))

        # Message bitmap cache state (filled by prepare_message_cache).
        self.animated_message_ids: set[int] = set()
        self.msg_images = OrderedDict()  # idx -> Image
        self.msg_lines: dict[int, int] = {}  # msg_index -> num_lines
        self.cache_peak = 0
        self.lazy_images = False
        self.cache_cap = 0
        self.auto_lazy = False

    # --- emote helpers (verbatim closures) ---

    def emote_image(self, cls, message_age=0.0):
        emote = self.emote_imgs.get(cls)
        if not emote:
            return None
        if len(emote["frames"]) == 1:
            return emote["frames"][0]
        elapsed_ms = int(max(0.0, message_age) * 1000) % emote["cycle_ms"]
        elapsed = 0
        for img, frame_duration in zip(emote["frames"], emote["durations"]):
            elapsed += frame_duration
            if elapsed_ms < elapsed:
                return img
        return emote["frames"][-1]

    def emote_width(self, cls):
        return self.emote_imgs[cls]["width"]

    def text_width(self, s):
        bb = self.font.getbbox(s)
        return bb[2] - bb[0]

    # --- line measurement (verbatim prepass) ---

    def calc_msg_lines(self, msg):
        """计算消息需要多少行（与 render_message 共用 layout_message_lines）。"""
        _lines, _header, num_lines = layout_message_lines(
            msg,
            max_w=self.max_w,
            font=self.font,
            font_bold=self.font_bold,
            text_width_fn=self.text_width,
            emote_width_fn=self.emote_width,
            emote_available_fn=lambda cls: cls in self.emote_imgs,
            max_message_lines=self.max_message_lines,
            truncate_with_ellipsis=False,
            padding=self.padding,
            badge_size=self.badge_size,
            gap=self.gap,
            indent=self.indent,
        )
        return num_lines

    def measure_message_lines(self, messages, duration):
        """预计算每条消息的行数（用于 lane 分配）。

        Messages starting at/after the render duration are always dropped by
        the scheduler (t >= duration); skip their layout measurement entirely.
        """
        msg_line_count = {}
        for i, m in enumerate(messages):
            if float(m.get("timestamp", 0) or 0) >= duration:
                continue
            msg_line_count[i] = self.calc_msg_lines(m)
        return msg_line_count

    # --- single message bitmap (verbatim) ---

    def render_message(self, msg, message_age=0.0):
        """渲染单条消息，超宽时自动换行。返回 (image, num_lines)。"""
        Image = self._Image
        ImageDraw = self._ImageDraw
        font = self.font
        font_bold = self.font_bold
        LINE_H = self.line_h
        MAX_W = self.max_w
        padding = self.padding
        badge_size = self.badge_size
        gap = self.gap
        indent = self.indent

        lines, header, num_lines = layout_message_lines(
            msg,
            max_w=MAX_W,
            font=font,
            font_bold=font_bold,
            text_width_fn=self.text_width,
            emote_width_fn=self.emote_width,
            emote_available_fn=lambda cls: cls in self.emote_imgs,
            max_message_lines=self.max_message_lines,
            truncate_with_ellipsis=True,
            padding=padding,
            badge_size=badge_size,
            gap=gap,
            indent=indent,
        )
        author = header["author"]
        author_w = header["author_w"]
        colon_w = header["colon_w"]

        total_h = LINE_H * num_lines
        img = Image.new("RGBA", (MAX_W, total_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # --- 绘制第一行头部 ---
        x = padding
        for badge in msg.get("badges") or []:
            title = str((badge or {}).get("title") or "")
            bc = badge_color_for(title)
            draw.rectangle([x, 3, x + badge_size, 3 + badge_size], fill=bc + (255,))
            x += badge_size + gap

        color = hex_to_rgb(msg["color"]) if msg.get("color") else (255, 255, 255)
        draw.text((x + 1, 1), author, fill=(0, 0, 0, 200), font=font_bold)
        draw.text((x, 0), author, fill=color + (255,), font=font_bold)
        x += author_w + gap

        draw.text((x + 1, 1), ":", fill=(0, 0, 0, 200), font=font)
        draw.text((x, 0), ":", fill=(200, 200, 200, 255), font=font)
        x += colon_w + gap

        # --- 绘制第一行的 fragments ---
        for fi in lines[0]:
            if fi[0] == "text":
                draw.text((x + 1, 1), fi[1], fill=(0, 0, 0, 200), font=font)
                draw.text((x, 0), fi[1], fill=(239, 239, 239, 255), font=font)
                x += fi[2]
            elif fi[0] == "emote":
                eimg = self.emote_image(fi[1], message_age)
                if eimg:
                    ey = (LINE_H - eimg.height) // 2
                    img.paste(eimg, (x, ey), eimg)
                    x += fi[2] + gap

        # --- 绘制续行 ---
        for line_idx in range(1, num_lines):
            y = line_idx * LINE_H
            x = padding + indent
            for fi in lines[line_idx]:
                if fi[0] == "text":
                    draw.text((x + 1, y + 1), fi[1], fill=(0, 0, 0, 200), font=font)
                    draw.text((x, y), fi[1], fill=(239, 239, 239, 255), font=font)
                    x += fi[2]
                elif fi[0] == "emote":
                    eimg = self.emote_image(fi[1], message_age)
                    if eimg:
                        ey = y + (LINE_H - eimg.height) // 2
                        img.paste(eimg, (x, ey), eimg)
                        x += fi[2] + gap

        return img, num_lines

    # --- message image cache (verbatim) ---

    def message_image(self, idx, message_age=0.0, force_dynamic=False):
        """Return (img, nl) for message idx; animated/dynamic always re-renders."""
        if force_dynamic or idx in self.animated_message_ids:
            img, nl = self.render_message(self.messages[idx], message_age)
            self.msg_lines[idx] = nl
            return img, nl
        if idx in self.msg_images:
            self.msg_images.move_to_end(idx)
            return self.msg_images[idx], self.msg_lines.get(idx, 1)
        img, nl = self.render_message(self.messages[idx], 0.0)
        cache_size = _store_message_image(
            self.msg_images, self.msg_lines, idx, img, nl,
            lazy=self.lazy_images,
            cache_cap=self.cache_cap,
        )
        self.cache_peak = max(self.cache_peak, cache_size)
        return img, nl

    def prepare_message_cache(self, msg_schedule, scene):
        """Set up the bitmap cache policy and eager pre-render (verbatim).

        - default: pre-render all static message images (predictable, existing
          behavior) — restricted to messages the scheduler actually placed on
          screen (dropped entries never appear in any output frame).
        - --lazy-message-images: only render when a message becomes visible;
          LRU cap for long VODs.
        """
        lazy_images = scene.lazy_message_images
        cache_cap = scene.message_image_cache_size
        auto_lazy = scene.auto_lazy_message_images
        self.lazy_images = lazy_images
        self.cache_cap = cache_cap
        self.auto_lazy = auto_lazy

        self.animated_message_ids = {
            i for i, message in enumerate(self.messages)
            if any(
                fragment.get("type") == "emote"
                and len(self.emote_imgs.get(fragment.get("class", ""), {}).get("frames", [])) > 1
                for fragment in message["fragments"]
            )
        }
        if self.animated_message_ids:
            print(f"  动画表情: {len(self.animated_message_ids)} 条消息将逐帧更新", flush=True)

        if lazy_images:
            print(
                f"  消息图: lazy 模式 (cache_size={cache_cap}, messages={len(self.messages)})",
                flush=True,
            )
            if auto_lazy:
                print(
                    f"  消息数达到 {AUTO_LAZY_MESSAGE_THRESHOLD}，已自动启用 lazy 缓存",
                    flush=True,
                )
        else:
            # Pre-render only messages the scheduler actually placed on screen.
            # The composition loop draws bitmaps exclusively from msg_schedule rows,
            # so eagerly rendering dropped entries (timestamp >= duration, or ended
            # before t=0) only wasted memory/CPU without changing any output frame.
            scheduled_indexes = sorted({row[3] for row in msg_schedule})
            for i in scheduled_indexes:
                self.message_image(i)
            print(f"  渲染 {len(self.msg_images)} 条消息图片", flush=True)

    # --- frame composition (verbatim inner loop) ---

    def composite_visible(self, frame, visible, current_t):
        """Paste every visible message bitmap onto ``frame`` (verbatim)."""
        Image = self._Image
        LINE_H = self.line_h
        H = self.config.height
        for lane, idx, start, end, nl_vis in visible:
            if idx in self.animated_message_ids:
                msg_img, nl = self.message_image(idx, current_t - start, force_dynamic=True)
            else:
                msg_img, nl = self.message_image(idx)
            if msg_img:
                # Schedule may clamp overlong messages to max_visible lanes.
                # Crop the bitmap so layout matches lane assignment.
                if nl_vis and nl and nl > nl_vis:
                    crop_h = max(1, LINE_H * int(nl_vis))
                    if msg_img.height > crop_h:
                        msg_img = msg_img.crop((0, 0, msg_img.width, crop_h))
                    nl = int(nl_vis)
                msg_h = LINE_H * nl
                y = H - (lane + 1) * LINE_H - 4 - (msg_h - LINE_H)
                # 确保 y 不会超出顶部
                if y < 0:
                    y = 0
                age = current_t - start
                remaining = end - current_t
                alpha = 255
                if age < FADE_IN_SECONDS:
                    alpha = int(255 * min(1.0, max(0.0, age / FADE_IN_SECONDS)))
                elif remaining < FADE_OUT_SECONDS:
                    alpha = int(255 * max(0.0, remaining / FADE_OUT_SECONDS))

                if alpha < 255:
                    msg_img = msg_img.copy()
                    r, g, b, a = msg_img.split()
                    # Bind alpha as default so the lambda does not close over the loop var.
                    a = a.point(lambda v, alpha=alpha: int(v * alpha / 255))
                    msg_img = Image.merge("RGBA", (r, g, b, a))

                frame.paste(msg_img, (2, y), msg_img)


def render_overlay(chat_data, out_dir, video_path, config):
    """渲染聊天覆盖层为 PNG 帧序列。

    Returns RenderResult (frames_dir / duration / frame_count / stats /
    timings / preview_path); no runtime writes back onto ``config``.
    """
    from PIL import Image

    print("[2/4] 渲染 overlay 帧序列...", flush=True)

    messages = chat_data["messages"]
    emote_map = chat_data.get("emote_map", {})

    renderer = FrameRenderer(messages, emote_map, config)

    # Build the immutable scene budget before the line-count prepass, scheduling,
    # or generating frames (its duration drives the prepass skip below).
    scene = OverlayScenePlan.from_config(
        source_duration=media_probe.probe_video_duration(video_path),
        config=config,
        message_count=len(messages),
    )
    MSG_LIFETIME = config.msg_lifetime
    raw_max_visible = scene.raw_max_visible
    auto_capacity = scene.auto_capacity
    stack_mode = scene.stack_mode
    MAX_VISIBLE = scene.max_visible
    if raw_max_visible <= 0:
        print(
            f"  max_visible=auto → {MAX_VISIBLE} lanes "
            f"(height={config.height}px, font={config.font_size}px, LINE_H={line_height_px(config.font_size)})",
            flush=True,
        )
    elif scene.budget_warning:
        print(f"  [WARN] {scene.budget_warning}", flush=True)
    print(f"  stack_mode={stack_mode}", flush=True)

    duration = scene.duration
    print(f"  视频时长: {scene.source_duration:.1f}s", flush=True)
    # preview_clip may start mid-video (densest window). Chat timestamps are rebased
    # to 0 in main() when clip_start > 0; compose seeks the source with -ss.
    # Here we only shorten the render duration to the clip length.
    if scene.preview_clip is not None:
        clip_len = scene.preview_clip
        clip_start = scene.preview_clip_start
        if clip_start > 1e-6:
            print(
                f"  预览短片模式: 源窗口 [{clip_start:.1f}s, {clip_start + clip_len:.1f}s] "
                f"(聊天已 rebase→0，渲染时长 {duration:.1f}s)",
                flush=True,
            )
        else:
            print(f"  预览短片模式: 仅渲染前 {duration:.1f}s", flush=True)

    # --- 预计算每条消息的行数（用于 lane 分配）---
    msg_line_count = renderer.measure_message_lines(messages, duration)

    if stack_mode == "float":
        msg_schedule = schedule_messages_float(
            messages,
            msg_line_count,
            duration=duration,
            capacity_lines=MAX_VISIBLE,
            arrival_interval=getattr(config, "arrival_interval", 0.0),
            throttle_from=scene.float_throttle_from,
        )
    else:
        msg_schedule = schedule_messages(
            messages,
            msg_line_count,
            duration=duration,
            max_visible=MAX_VISIBLE,
            msg_lifetime=MSG_LIFETIME,
            min_visible_seconds=getattr(config, "min_visible_seconds", 0.0),
            arrival_interval=getattr(config, "arrival_interval", 0.0),
            auto_capacity=auto_capacity,
        )

    if stack_mode == "float":
        print(
            f"  调度(float上浮): {len(msg_schedule)} 条事件, capacity={MAX_VISIBLE} 行",
            flush=True,
        )
    else:
        lane_counts = Counter(s[2] for s in msg_schedule)
        print(
            f"  调度(lanes): {len(msg_schedule)} 条消息, lanes={MAX_VISIBLE}, "
            f"lane 分布: {dict(sorted(lane_counts.items()))}",
            flush=True,
        )

    renderer.prepare_message_cache(msg_schedule, scene)

    # --- 生成帧序列 ---
    frames_dir = os.path.join(out_dir, "overlay_frames")
    os.makedirs(frames_dir, exist_ok=True)
    # 清除旧帧
    for old in os.listdir(frames_dir):
        if old.endswith(".png"):
            os.remove(os.path.join(frames_dir, old))
    ensure_render_disk_headroom(frames_dir)

    FPS = scene.fps
    W, H = config.width, config.height
    BG_ALPHA = config.bg_alpha

    # 找 change points
    preview_frame_time = scene.preview_frame_time
    if preview_frame_time is not None:
        preview_t = scene.preview_time
        assert preview_t is not None
        change_points = [preview_t, min(duration, preview_t + 1 / max(FPS, 1))]
        if change_points[1] <= change_points[0]:
            change_points[1] = change_points[0] + 1 / max(FPS, 1)
        print(f"  预览帧模式: t={preview_t:.2f}s", flush=True)
    else:
        change_points = set()
        for start, end, _lane, _idx, _nl_sch in msg_schedule:
            change_points.add(start)
            change_points.add(end)
        change_points.add(0)
        change_points.add(duration)
        change_points = sorted(cp for cp in change_points if 0 <= cp <= duration)

    # Use a global frame index so short chat segments do not inflate the total
    # frame count via repeated ceil() rounding.
    total_frames = scene.total_frames
    frame_num = 0
    render_start_time = time.time()
    last_progress_time = render_start_time
    reuse_static = bool(getattr(config, "reuse_static_frames", True))
    skip_blank = bool(getattr(config, "skip_blank_frames", True))
    blank_hold_seconds = float(getattr(config, "blank_hold_seconds", 0.5) or 0.5)
    blank_stride = max(1, int(round(blank_hold_seconds * FPS)))
    stats = {
        "write": 0,
        "hardlink": 0,
        "copy": 0,
        "reused_static": 0,
        "blank_sparse": 0,
        "composited": 0,
        "filled": 0,
    }
    written_indexes: list[int] = []
    last_static_key = None
    last_static_frame_idx = None
    lane_visibility = None if stack_mode == "float" else _LaneVisibilityCursor(msg_schedule)

    for cp_idx in range(len(change_points)):
        cp = change_points[cp_idx]
        next_cp = change_points[cp_idx + 1] if cp_idx + 1 < len(change_points) else duration

        if stack_mode == "float":
            # Bottom-up Twitch stack: recompute lanes from currently active messages.
            visible = active_float_stack(msg_schedule, cp, MAX_VISIBLE)
        else:
            visible = lane_visibility.at(cp)

        if preview_frame_time is not None:
            frame_indexes = [0]
        else:
            start_i, end_i = frame_index_range(cp, next_cp, FPS, total_frames)
            # Fully blank segments: only materialize sparse keyframes, then expand later.
            if skip_blank and not visible and preview_frame_time is None:
                frame_indexes = blank_gap_frame_indexes(start_i, end_i, hold_stride=blank_stride)
                stats["blank_sparse"] += max(0, (end_i - start_i) - len(frame_indexes))
            else:
                frame_indexes = list(range(start_i, end_i))

        # Static segment key: same visible message set, none animated, and no fade edges
        # inside this change-point range. Safe to draw once and hardlink the rest.
        # Blank segments are also static (fully transparent).
        # IMPORTANT: fade-in (first FADE_IN_SECONDS) / fade-out (last FADE_OUT_SECONDS)
        # make alpha time-dependent.
        # Even with change_points at start/end, the *boundary segment that still contains*
        # the fade window must NOT be static-reused, or every hardlinked frame freezes
        # the first (or last) alpha sample.
        segment_has_anim = any(idx in renderer.animated_message_ids for _lane, idx, _s, _e, _nl in visible)
        segment_has_fade = any(
            (cp < (start + FADE_IN_SECONDS) and next_cp > start)
            or (cp < end and next_cp > (end - FADE_OUT_SECONDS))
            for _lane, idx, start, end, _nl in visible
        )
        static_key = None
        if reuse_static and not segment_has_anim and not segment_has_fade and preview_frame_time is None:
            if not visible:
                static_key = ("__blank__",)
            else:
                static_key = tuple((lane, idx, nl_sch) for lane, idx, _s, _e, nl_sch in visible)

        segment_template = None
        segment_template_idx = None

        for frame_i in frame_indexes:
            # Always use clamped preview_t for visibility (not raw preview_frame_time,
            # which can sit past duration and empty the stack at EOF).
            if preview_frame_time is not None:
                current_t = preview_t
            else:
                current_t = frame_i / float(FPS)
            if preview_frame_time is None and current_t >= duration:
                break

            out_frame_num = 0 if preview_frame_time is not None else frame_i
            if frame_num % 256 == 0:
                ensure_render_disk_headroom(frames_dir)

            # Reuse previous identical static frame without re-compositing.
            if (
                static_key is not None
                and segment_template is not None
                and segment_template_idx is not None
                and static_key == last_static_key
            ):
                action = write_or_reuse_frame(
                    frames_dir,
                    out_frame_num,
                    segment_template,
                    reuse_from=segment_template_idx,
                )
                stats[action] = stats.get(action, 0) + 1
                stats["reused_static"] += 1
                written_indexes.append(out_frame_num)
                frame_num += 1
                continue

            if (
                static_key is not None
                and last_static_key == static_key
                and last_static_frame_idx is not None
                and segment_template is None
            ):
                # Carry reuse across change-point boundaries with same visible set.
                action = write_or_reuse_frame(
                    frames_dir,
                    out_frame_num,
                    None,
                    reuse_from=last_static_frame_idx,
                )
                stats[action] = stats.get(action, 0) + 1
                stats["reused_static"] += 1
                segment_template_idx = last_static_frame_idx
                # Keep a dummy non-None marker so subsequent frames in this segment reuse.
                segment_template = True
                written_indexes.append(out_frame_num)
                frame_num += 1
                continue

            if visible and BG_ALPHA:
                # Reuse one solid chat-box background (avoids per-frame full alloc when possible).
                frame = Image.new("RGBA", (W, H), (0, 0, 0, BG_ALPHA))
            else:
                frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))

            if visible:
                renderer.composite_visible(frame, visible, current_t)

            action = write_or_reuse_frame(frames_dir, out_frame_num, frame, reuse_from=None)
            stats[action] = stats.get(action, 0) + 1
            stats["composited"] += 1
            written_indexes.append(out_frame_num)
            frame_num += 1

            if static_key is not None:
                segment_template = frame
                segment_template_idx = out_frame_num
                last_static_key = static_key
                last_static_frame_idx = out_frame_num
            else:
                last_static_key = None
                last_static_frame_idx = None

        # 进度
        now = time.time()
        if (cp_idx + 1) % 10 == 0 or cp_idx == len(change_points) - 1 or now - last_progress_time >= 5:
            # Progress against timeline coverage, not sparse write count.
            covered = len(set(written_indexes))
            pct = (covered / total_frames * 100) if total_frames > 0 else 100
            elapsed = now - render_start_time
            if covered > 0 and covered < total_frames:
                eta = elapsed / covered * (total_frames - covered)
                eta_str = f" ETA {int(eta//60)}m{int(eta%60)}s"
            else:
                eta_str = ""
            print(
                f"  [{cp_idx+1}/{len(change_points)}] t={cp:.1f}s {len(visible)}msgs "
                f"{pct:.0f}% write={stats['write']} reuse={stats['reused_static']}{eta_str}",
                flush=True,
            )
            last_progress_time = now
        if preview_frame_time is not None:
            break

    # Materialize full contiguous sequence for FFmpeg demuxer when blank gaps were sparse.
    # Missing frames must fail hard — FFmpeg image2 would otherwise silently emit a short overlay.
    if preview_frame_time is None and total_frames > 0:
        fill_stats = expand_frame_sequence_for_ffmpeg(frames_dir, total_frames, written_indexes)
        stats["filled"] += int(fill_stats.get("filled", 0))
        stats["hardlink"] += int(fill_stats.get("hardlink", 0))
        stats["copy"] += int(fill_stats.get("copy", 0))
        # expand_frame_sequence_for_ffmpeg ends with its own contiguous-coverage
        # hard guarantee; keep only the on-disk count cross-check before compose.
        final_count = len(
            [n for n in os.listdir(frames_dir) if n.startswith("frame_") and n.endswith(".png")]
        )
        if final_count != total_frames:
            raise RuntimeError(
                f"render_overlay: disk frame count {final_count} != target {total_frames}; "
                f"refuse incomplete overlay under {frames_dir}"
            )
        frame_num = final_count

    elapsed_total = time.time() - render_start_time
    stats["message_cache_peak"] = renderer.cache_peak
    stats["lazy_message_images"] = int(renderer.lazy_images)
    stats["auto_lazy_message_images"] = int(renderer.auto_lazy)
    timings = {"render_frames": elapsed_total}
    print(
        f"  完成: {frame_num} 帧, 用时 {int(elapsed_total//60)}m{int(elapsed_total%60)}s "
        f"(write={stats['write']}, hardlink={stats['hardlink']}, copy={stats['copy']}, "
        f"static_reuse={stats['reused_static']}, blank_sparse={stats['blank_sparse']}, filled={stats['filled']})",
        flush=True,
    )
    preview_path = None
    if preview_frame_time is not None:
        preview_path = _extract_and_publish_preview(
            frames_dir, out_dir, video_path, config, preview_t
        )
    return RenderResult(
        frames_dir=frames_dir,
        duration=duration,
        frame_count=frame_num,
        stats=stats,
        timings=timings,
        preview_path=preview_path,
    )


def _extract_and_publish_preview(frames_dir, out_dir, video_path, config, preview_t):
    """Single-frame preview extraction + optional publish copy (verbatim).

    Writes the preview under out_dir first (safe job/temp location); when the
    user requested a path outside out_dir, also publishes a copy there and
    returns the final preview path (request path after a successful copy).
    """
    from PIL import Image

    default_name = f"{Path(video_path).stem}_preview_{preview_t:.1f}s.png".replace(".0s", "s")
    requested_preview = getattr(config, "preview_image", None)
    # Always write under out_dir first (safe job/temp location).
    if requested_preview:
        safe_name = os.path.basename(str(requested_preview)) or default_name
    else:
        safe_name = default_name
    preview_path = os.path.join(out_dir, safe_name)
    bg_path = os.path.join(out_dir, "preview_video_frame.png")
    # Accurate single-frame extract for preview alignment. -ss MUST bind to
    # the input (placed before -i): input seek jumps straight to the nearest
    # keyframe at/before the target and decodes only from there. Output seek
    # (-ss after -i) decodes the whole prefix first — O(t) work that reliably
    # blew the 120s timeout on mid-VOD previews. compose_video's dense/mid
    # preview seek uses the same input-seek ordering.
    try:
        r = subprocess.run(
            [
                require_executable("ffmpeg"), "-y",
                "-ss", str(preview_t), "-i", video_path,
                "-frames:v", "1", bg_path,
            ],
            capture_output=True,
            text=True,
            timeout=_PREVIEW_FRAME_TIMEOUT_SECONDS,
        )
        preview_error = (r.stderr or "ffmpeg failed")[-300:]
    except subprocess.TimeoutExpired:
        r = None
        preview_error = (
            f"单帧抽取超过 {_PREVIEW_FRAME_TIMEOUT_SECONDS:g}s，已终止"
        )
    if r is not None and r.returncode == 0 and os.path.isfile(bg_path):
        bg = Image.open(bg_path).convert("RGBA")
        overlay = Image.open(os.path.join(frames_dir, "frame_00000.png")).convert("RGBA")
        bg.paste(overlay, (config.x, config.y), overlay)
        bg.save(preview_path)
        try:
            os.remove(bg_path)
        except OSError:
            pass
    else:
        print(f"  警告: 无法抽取视频帧，改为输出 overlay 透明图: {preview_error}", flush=True)
        Image.open(os.path.join(frames_dir, "frame_00000.png")).save(preview_path)
    # If user requested a path outside out_dir, also publish a copy there after
    # the safe write (explicit user intent; still keep the in-job copy).
    if requested_preview:
        try:
            req_abs = os.path.abspath(str(requested_preview))
            if not path_is_under(req_abs, out_dir) and os.path.isfile(preview_path):
                # Safety: preview is already written under out_dir; this copy
                # publishes to the user-requested location as a convenience.
                # Refuse OS system directories (Windows + Unix/macOS).
                if is_dangerous_publish_path(req_abs):
                    print(f"  警告: --preview-image 路径在系统目录下，已跳过复制: {req_abs}", flush=True)
                else:
                    os.makedirs(os.path.dirname(req_abs) or ".", exist_ok=True)
                    shutil.copy2(preview_path, req_abs)
                    print(f"  预览图已复制到请求路径: {req_abs}", flush=True)
                    preview_path = req_abs
        except OSError as e:
            print(f"  警告: 无法复制预览图到请求路径: {e}", flush=True)
    print(f"  预览图: {preview_path}", flush=True)
    return preview_path
