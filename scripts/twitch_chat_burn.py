#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Twitch Chat Overlay Tool
========================
从 Twitch HTML 聊天记录生成弹幕覆盖层，并合成到视频上。

用法:
  python twitch_chat_burn.py <video.mp4> <chat.html> [选项]

实现布局: 本模块现在是 CLI 门面（argparse / 预览时间窗规划 / 主流程编排）。
渲染与合成的实现分别位于 overlay_render.py / overlay_compose.py，其余
提取模块见下方 re-export 区。tests 仍可消费本模块的扁平命名空间。

示例:
  python twitch_chat_burn.py "video.mp4" "chat.html"
  python twitch_chat_burn.py "video.mp4" "chat.html" --x 15 --y 327 --w 497 --h 363
  python twitch_chat_burn.py "video.mp4" "chat.html" --font-size 15 --fps 30

输出:
  <video>_chat_overlay.mp4

依赖:
  pip install pillow
  需要系统安装 ffmpeg（在 PATH 中）
"""

import argparse
from collections import Counter, OrderedDict  # noqa: F401  (re-exported; REND-N9)
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys

# Allow sibling imports when loaded as a script or via importlib from tests.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from chat_parser import parse_chat_html
from chat_window import (
    apply_time_offset,
    compute_time_offset,
    filter_chat_for_time_window,
    find_densest_preview_start,
    format_offset_diagnosis,
    preview_window,
    resolve_preview_frame_time,
    trim_float_carry_in_messages,
)
from common_utils import (
    current_cli_invocation,
    ensure_utf8_stdio,
    positive_float_arg,
    quote_cli_arg,
    require_executable,
    resolve_font_paths,
    validate_non_negative_float,
    validate_positive_float,
    validate_positive_int,
)

# Windows runners often use cp1252; Chinese prints must not crash the CLI.
ensure_utf8_stdio()
# ============================================================
# Extracted modules: media probe / text layout / scheduling / translation IO
# ============================================================
# These regions moved to dedicated modules; this facade re-exports every
# moved symbol so scripts and tests keep the flat ``twitch_chat_burn``
# namespace. Internal callers address probes via ``media_probe.<symbol>``
# attribute access so monkeypatching the owner module keeps working.
import chat_schedule
import chat_text_layout
from encode_options import (
    build_audio_encode_args,  # noqa: F401  (re-exported; owner use in overlay_compose)
    build_video_encode_args,  # noqa: F401
    build_webm_encode_args,  # noqa: F401
    resolve_encode_options,
    summarize_encode_options,
)
from layout_preset import apply_layout_preset_to_namespace, load_layout_preset
import media_probe
from overlay_config import OverlayConfig
import overlay_scene as _overlay_scene
from overlay_scene import (  # noqa: F401  (planning re-exports below)
    OverlayScenePlan,
    frame_index_range,
    line_height_px,
    resolve_lane_budget,
)
from process_util import (
    clean_companion_flags_error,
    clean_temp_artifacts,
    exclusive_file_lock,
    install_process_cleanup_handlers,
    is_dangerous_publish_path,
    make_job_dir,
    path_is_under,
    run_tracked,  # noqa: F401  (re-exported; owner use in overlay_compose)
)
from render_perf import (  # noqa: F401  (frame-store helpers re-exported)
    blank_gap_frame_indexes,
    ensure_render_disk_headroom,
    expand_frame_sequence_for_ffmpeg,
    missing_frame_indexes,
    write_or_reuse_frame,
)
from render_preset import apply_render_preset_to_namespace, load_render_preset
from run_meta import mark_run_status, write_run_meta
import translation_io

# media_probe: ffprobe wrappers (lru-cached per absolute path + stat signature).
_PROBE_TIMEOUT_SECONDS = media_probe._PROBE_TIMEOUT_SECONDS
probe_video_dimensions = media_probe.probe_video_dimensions
probe_video_duration = media_probe.probe_video_duration
probe_video_fps = media_probe.probe_video_fps
probe_media_summary = media_probe.probe_media_summary
_quantize_fps = media_probe._quantize_fps
fps_to_ffmpeg_rate = media_probe.fps_to_ffmpeg_rate
resolve_output_fps = media_probe.resolve_output_fps

# chat_text_layout: pure CJK-aware wrapping / badge / message-line layout.
is_cjk_char = chat_text_layout.is_cjk_char
split_text_for_wrap = chat_text_layout.split_text_for_wrap
wrap_fragments = chat_text_layout.wrap_fragments
normalize_text = chat_text_layout.normalize_text
hex_to_rgb = chat_text_layout.hex_to_rgb
MESSAGE_PAD = chat_text_layout.MESSAGE_PAD
MESSAGE_BADGE_SIZE = chat_text_layout.MESSAGE_BADGE_SIZE
MESSAGE_GAP = chat_text_layout.MESSAGE_GAP
MESSAGE_INDENT = chat_text_layout.MESSAGE_INDENT
BADGE_COLORS = chat_text_layout.BADGE_COLORS
BADGE_FALLBACK_COLOR = chat_text_layout.BADGE_FALLBACK_COLOR
badge_color_for = chat_text_layout.badge_color_for
compute_message_header_width = chat_text_layout.compute_message_header_width
build_message_frag_list = chat_text_layout.build_message_frag_list
truncate_wrapped_lines_with_ellipsis = chat_text_layout.truncate_wrapped_lines_with_ellipsis
layout_message_lines = chat_text_layout.layout_message_lines

# chat_schedule: arrival throttling + lane/float scheduling.
admit_timestamp = chat_schedule.admit_timestamp
schedule_messages = chat_schedule.schedule_messages
schedule_messages_float = chat_schedule.schedule_messages_float
_FloatEventList = chat_schedule._FloatEventList
active_float_stack = chat_schedule.active_float_stack
_LaneVisibilityCursor = chat_schedule._LaneVisibilityCursor

# translation_io: translation JSON export / identity-checked import.
clean_imported_translation = translation_io.clean_imported_translation
_normalize_import_identity_text = translation_io._normalize_import_identity_text
message_export_original = translation_io.message_export_original
_message_stream_timestamp = translation_io._message_stream_timestamp
translation_json_nonempty_count = translation_io.translation_json_nonempty_count
build_export_translation_payload = translation_io.build_export_translation_payload
write_export_translation_json = translation_io.write_export_translation_json
apply_imported_translations = translation_io.apply_imported_translations

# Compatibility exports for existing scripts and tests.  Scene planning owns
# their definitions, while this module remains the established render facade.
AUTO_LAZY_MESSAGE_THRESHOLD = _overlay_scene.AUTO_LAZY_MESSAGE_THRESHOLD
compute_lane_capacity = _overlay_scene.compute_lane_capacity
expected_overlay_frame_count = _overlay_scene.expected_overlay_frame_count
resolve_message_image_cache_policy = _overlay_scene.resolve_message_image_cache_policy

# ============================================================
# Extracted modules: overlay render / overlay compose
# ============================================================
# render_overlay + FrameRenderer live in overlay_render.py; compose_video and
# the A/V timing / output-validation cluster live in overlay_compose.py. The
# facade re-exports every moved symbol. Result objects (RenderResult /
# ComposeResult) replaced the old runtime config writebacks — main() injects
# result values into OverlayConfig at the pipeline boundary. Tests patch the
# owner modules (overlay_render.X / overlay_compose.X) at the seams.
import overlay_compose
import overlay_render

# overlay_render: frame sequence rendering.
FrameRenderer = overlay_render.FrameRenderer
RenderResult = overlay_render.RenderResult
render_overlay = overlay_render.render_overlay
FADE_IN_SECONDS = overlay_render.FADE_IN_SECONDS
FADE_OUT_SECONDS = overlay_render.FADE_OUT_SECONDS
emote_decode_plan = overlay_render.emote_decode_plan
_store_message_image = overlay_render._store_message_image
_PREVIEW_FRAME_TIMEOUT_SECONDS = overlay_render._PREVIEW_FRAME_TIMEOUT_SECONDS
_MAX_EMOTE_ANIMATION_FRAMES = overlay_render._MAX_EMOTE_ANIMATION_FRAMES
_MAX_EMOTE_SOURCE_PIXELS = overlay_render._MAX_EMOTE_SOURCE_PIXELS
_MAX_EMOTE_DECODED_BYTES_PER_ASSET = overlay_render._MAX_EMOTE_DECODED_BYTES_PER_ASSET
_MAX_EMOTE_DECODED_BYTES_TOTAL = overlay_render._MAX_EMOTE_DECODED_BYTES_TOTAL

# overlay_compose: A/V timing, output validation, mux pipeline.
# Naming contract (audit_cli_clean guard): compose publishes
# Path(video_path).stem + "_chat.mp4" (implementation in overlay_compose).
ComposeResult = overlay_compose.ComposeResult
compose_video = overlay_compose.compose_video
detect_frame_start_number = overlay_compose.detect_frame_start_number
get_stream_start_time = overlay_compose.get_stream_start_time
resolve_source_av_timing = overlay_compose.resolve_source_av_timing
expected_compose_duration = overlay_compose.expected_compose_duration
_default_max_extra_seconds = overlay_compose._default_max_extra_seconds
validate_rendered_output = overlay_compose.validate_rendered_output
build_audio_encode_args_for_compose = overlay_compose.build_audio_encode_args_for_compose


# Absolute layout presets (layout_default / layout_mobile / CLI defaults) are
# authored against this design canvas. run.bat users almost never pass *-ratio.
DESIGN_LAYOUT_WIDTH = 1920
DESIGN_LAYOUT_HEIGHT = 1080


def _layout_uses_any_ratio(config) -> bool:
    return any(
        float(getattr(config, key, 0.0) or 0.0) > 0
        for key in ("x_ratio", "y_ratio", "width_ratio", "height_ratio", "font_size_ratio")
    )


def apply_relative_layout(config, video_path):
    """Resolve optional source-video-relative layout values into pixel fields."""
    if not _layout_uses_any_ratio(config):
        return
    dimensions = media_probe.probe_video_dimensions(video_path)
    if not dimensions:
        raise RuntimeError("无法读取源视频分辨率，不能使用 *-ratio 布局参数")
    video_w, video_h = dimensions
    if getattr(config, "x_ratio", 0.0):
        config.x = round(video_w * config.x_ratio)
    if getattr(config, "y_ratio", 0.0):
        config.y = round(video_h * config.y_ratio)
    if getattr(config, "width_ratio", 0.0):
        config.width = max(1, round(video_w * config.width_ratio))
    if getattr(config, "height_ratio", 0.0):
        config.height = max(1, round(video_h * config.height_ratio))
    font_from_ratio = bool(getattr(config, "font_size_ratio", 0.0))
    if font_from_ratio:
        config.font_size = max(8, round(video_h * config.font_size_ratio))
        # Only resync emotes when font size itself came from a ratio. Geometry-only
        # ratios (x/y/w/h) must not discard an explicit --emote-height.
        config.emote_h = max(8, round(config.font_size * 1.08))


def _box_visible_area(x: int, y: int, w: int, h: int, video_w: int, video_h: int) -> int:
    visible_w = max(0, min(video_w, x + w) - max(0, x))
    visible_h = max(0, min(video_h, y + h) - max(0, y))
    return visible_w * visible_h


def adapt_absolute_layout_to_source(config, video_path) -> str | None:
    """Scale 1080p-authored absolute pixel layouts into the source frame.

    Public presets and CLI defaults use absolute x/y/w/h for a ~1080p canvas.
    ``run.bat`` / job wizard almost always keep those defaults and never pass
    ``*-ratio``. On 360p/480p (and some 720p crops) the box sits mostly outside
    the frame, so chat looks missing. When no ratio is set and the absolute box
    is mostly outside the source, scale from the design canvas and clamp inside.

    Returns a short log line when adaptation ran, else None.
    """
    if _layout_uses_any_ratio(config):
        return None
    dimensions = media_probe.probe_video_dimensions(video_path)
    if not dimensions:
        return None
    video_w, video_h = int(dimensions[0]), int(dimensions[1])
    if video_w <= 0 or video_h <= 0:
        return None
    # Near the design canvas: keep absolute pixels as authored.
    if (
        abs(video_w - DESIGN_LAYOUT_WIDTH) / DESIGN_LAYOUT_WIDTH < 0.05
        and abs(video_h - DESIGN_LAYOUT_HEIGHT) / DESIGN_LAYOUT_HEIGHT < 0.05
    ):
        return None

    x = int(getattr(config, "x", 0) or 0)
    y = int(getattr(config, "y", 0) or 0)
    w = max(0, int(getattr(config, "width", 0) or 0))
    h = max(0, int(getattr(config, "height", 0) or 0))
    if w <= 0 or h <= 0:
        return None

    box_area = max(1, w * h)
    visible_area = _box_visible_area(x, y, w, h, video_w, video_h)
    fully_inside = (
        x >= 0
        and y >= 0
        and (x + w) <= video_w + 1
        and (y + h) <= video_h + 1
    )
    # Only rewrite when the authored absolute box is mostly outside / clipped.
    # Fully-inside custom crops on non-1080p stay untouched.
    if fully_inside and visible_area >= box_area // 2:
        return None

    before = (x, y, w, h, int(getattr(config, "font_size", 15) or 15), int(getattr(config, "emote_h", 22) or 22))
    sx = video_w / float(DESIGN_LAYOUT_WIDTH)
    sy = video_h / float(DESIGN_LAYOUT_HEIGHT)
    config.x = max(0, min(max(0, video_w - 1), round(x * sx)))
    config.y = max(0, min(max(0, video_h - 1), round(y * sy)))
    config.width = max(16, round(w * sx))
    config.height = max(16, round(h * sy))
    if config.x + config.width > video_w:
        config.width = max(16, video_w - config.x)
    if config.y + config.height > video_h:
        config.height = max(16, video_h - config.y)
    config.font_size = max(8, round(before[4] * sy))
    config.emote_h = max(8, round(before[5] * sy))
    after = (
        int(config.x),
        int(config.y),
        int(config.width),
        int(config.height),
        int(config.font_size),
        int(config.emote_h),
    )
    return (
        f"已按源分辨率自适应布局 {video_w}x{video_h} "
        f"(设计基准 {DESIGN_LAYOUT_WIDTH}x{DESIGN_LAYOUT_HEIGHT}; "
        f"run/默认绝对坐标在小分辨率上会画出画面): "
        f"区域 {before[0]},{before[1]} {before[2]}x{before[3]} → "
        f"{after[0]},{after[1]} {after[2]}x{after[3]}; "
        f"font {before[4]}→{after[4]}, emote {before[5]}→{after[5]}"
    )


def parse_output_fps_arg(text: str) -> float:
    """argparse type for --output-fps: decimal ("29.97") or exact rational ("30000/1001").

    Rationals are normalized to float for the config contract; the NTSC family
    snaps back to the exact rate in _quantize_fps, so fps_to_ffmpeg_rate still
    emits "30000/1001" for -r instead of a drifting 29.97 decimal.
    """
    s = str(text).strip()
    if "/" in s:
        num_s, _, den_s = s.partition("/")
        try:
            num = float(num_s.strip())
            den = float(den_s.strip())
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"invalid rational fps value: {text!r} (expected N/M, e.g. 30000/1001)"
            ) from None
        if not (math.isfinite(num) and math.isfinite(den)) or den == 0:
            raise argparse.ArgumentTypeError(f"invalid rational fps value: {text!r}")
        fps = num / den
        if not math.isfinite(fps):
            raise argparse.ArgumentTypeError(f"fps out of range: {text!r}")
        return fps
    try:
        parsed = float(s)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"invalid float value: {text!r}") from exc
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("must be finite")
    return parsed


def layout_bounds_warnings(config, video_path) -> list[str]:
    """Warn when the chat box is mostly/fully outside the source frame.

    Default pixel layout targets ~1080p; on 360p/720p sources the box often sits
    below the frame so the burn looks like "no chat". Prefer
    ``adapt_absolute_layout_to_source`` first so run.bat defaults auto-scale;
    this remains a safety net for custom absolute crops that still overflow.
    """
    dimensions = media_probe.probe_video_dimensions(video_path)
    if not dimensions:
        return []
    video_w, video_h = int(dimensions[0]), int(dimensions[1])
    x = int(getattr(config, "x", 0) or 0)
    y = int(getattr(config, "y", 0) or 0)
    w = max(0, int(getattr(config, "width", 0) or 0))
    h = max(0, int(getattr(config, "height", 0) or 0))
    if video_w <= 0 or video_h <= 0 or w <= 0 or h <= 0:
        return []
    box_area = max(1, w * h)
    visible_area = _box_visible_area(x, y, w, h, video_w, video_h)
    warns: list[str] = []
    if visible_area <= 0:
        warns.append(
            f"弹幕区域完全在画面外 (box x={x} y={y} w={w} h={h}, "
            f"video {video_w}x{video_h})。默认像素布局按约 1080p 设计；"
            f"小分辨率请改用 --x-ratio/--y-ratio/--width-ratio/--height-ratio，"
            f"或减小 --y/--h。"
        )
    elif visible_area < box_area // 2:
        pct = int(round(100.0 * visible_area / box_area))
        warns.append(
            f"弹幕区域约 {pct}% 在画面内 (box x={x} y={y} w={w} h={h}, "
            f"video {video_w}x{video_h})。大部分弹幕会画在画面外；"
            f"建议用比例布局 (*-ratio) 或调整像素坐标。"
        )
    return warns


# ============================================================
# 4. 主入口
# ============================================================

def _format_import_translation_command(
    video: str | Path,
    chat_html: str | Path,
    export_path: str | Path,
) -> str:
    return (
        f"{current_cli_invocation()} {quote_cli_arg(video)} {quote_cli_arg(chat_html)} "
        f"--import-translation {quote_cli_arg(export_path)}"
    )


def _validate_offset(value) -> float:
    offset = float(value)
    if not math.isfinite(offset):
        raise ValueError("--offset must be finite")
    if abs(offset) > 7 * 24 * 3600.0:
        raise ValueError("--offset absolute value must be <= 7 days")
    return offset


def _validate_runtime_args(args) -> None:
    validate_positive_int("--fps", args.fps, minimum=1, maximum=240)
    if args.output_fps is not None:
        validate_positive_float("--output-fps", args.output_fps, minimum=1.0, maximum=240.0)
    validate_positive_int("--w/--width", args.width, minimum=16, maximum=7680)
    validate_positive_int("--h/--height", args.height, minimum=16, maximum=4320)
    validate_positive_int("--font-size", args.font_size, minimum=8, maximum=128)
    validate_positive_int("--emote-height", args.emote_height, minimum=8, maximum=256)
    validate_positive_int("--max-visible", args.max_visible, minimum=0, maximum=100)
    validate_positive_int(
        "--message-image-cache-size",
        args.message_image_cache_size,
        minimum=8,
        maximum=100000,
    )
    stack_mode = str(getattr(args, "stack_mode", "lanes") or "lanes").strip().lower()
    if stack_mode not in ("float", "lanes"):
        raise ValueError(f"--stack-mode must be float or lanes, got {args.stack_mode!r}")
    args.stack_mode = stack_mode
    if stack_mode == "lanes":
        validate_positive_float("--msg-lifetime", args.msg_lifetime, minimum=0.1, maximum=600.0)
    validate_positive_int("--max-message-lines", args.max_message_lines, minimum=0, maximum=100)
    validate_non_negative_float("--min-visible-seconds", args.min_visible_seconds, maximum=600.0)
    validate_non_negative_float("--arrival-interval", args.arrival_interval, maximum=600.0)
    for ratio_arg in ("x_ratio", "y_ratio", "width_ratio", "height_ratio", "font_size_ratio"):
        validate_non_negative_float(f"--{ratio_arg.replace('_', '-')}", getattr(args, ratio_arg), maximum=1.0)
    if stack_mode == "lanes" and args.msg_lifetime > 0 and args.min_visible_seconds > args.msg_lifetime:
        raise ValueError("--min-visible-seconds must be <= --msg-lifetime")
    if args.preview_frame is not None:
        validate_non_negative_float("--preview-frame", args.preview_frame, maximum=24 * 3600.0)
    if args.preview_clip is not None:
        validate_non_negative_float("--preview-clip", args.preview_clip, maximum=24 * 3600.0)
        if float(args.preview_clip) <= 0:
            raise ValueError("--preview-clip must be > 0")
    if args.offset is not None:
        _validate_offset(args.offset)
    validate_non_negative_float("--blank-hold-seconds", args.blank_hold_seconds, maximum=30.0)
    if args.blank_hold_seconds <= 0:
        raise ValueError("--blank-hold-seconds must be > 0")
    if not 0 <= args.bg_alpha <= 255:
        raise ValueError("--bg-alpha must be between 0 and 255")

def build_arg_parser() -> argparse.ArgumentParser:
    """Build the burn CLI parser (argparse construction extracted from _main)."""
    parser = argparse.ArgumentParser(
        description="Twitch 聊天弹幕覆盖工具 - 从 HTML 聊天记录生成 overlay 并合成到视频",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s video.mp4 chat.html
  %(prog)s video.mp4 chat.html --x 15 --y 327 --w 497 --h 363
  %(prog)s video.mp4 chat.html --font-size 18 --fps 60

输出文件: <video>_chat.mp4
中间文件: 临时目录下 chat_data.json, emotes/, overlay_frames/
        """,
    )
    parser.add_argument("video", help="源视频文件路径")
    parser.add_argument("chat_html", help="Twitch HTML 聊天记录路径")
    parser.add_argument("--x", type=int, default=15, help="overlay 左上角 X 坐标 (默认 15)")
    parser.add_argument("--y", type=int, default=327, help="overlay 左上角 Y 坐标 (默认 327)")
    parser.add_argument("--w", "--width", dest="width", type=int, default=497, help="overlay 宽度 (默认 497)")
    parser.add_argument("--h", "--height", dest="height", type=int, default=363, help="overlay 高度 (默认 363)")
    parser.add_argument("--font-size", type=int, default=15, help="字体大小 (默认 15)")
    parser.add_argument("--font-path", default="auto", help="字体文件路径 (默认 auto，自动检测 CJK 字体)")
    parser.add_argument("--font-bold-path", default="auto", help="粗体字体路径 (默认 auto)")
    parser.add_argument("--fps", type=int, default=15, help="弹幕 overlay 渲染帧率 (默认 15；只影响聊天层采样，不强制成片帧率)")
    parser.add_argument(
        "--output-fps",
        type=parse_output_fps_arg,
        default=None,
        help="最终成片视频帧率（小数如 29.97 / 59.94，或精确有理数如 30000/1001）；"
        "默认跟随源视频。不要与 --fps 混用：--fps 只控弹幕层",
    )
    parser.add_argument(
        "--max-visible",
        type=int,
        default=0,
        help=(
            "最大同时可见消息数 (默认 0=按框高/字号自动填满；"
            "显式 N 固定条数；若 N 大于框高可容纳行数会自动钳制并告警，避免弹幕叠在顶部)"
        ),
    )
    parser.add_argument(
        "--preview-dense",
        action="store_true",
        help="与 --preview-clip 联用：自动选弹幕最密时间窗（而不是总从 0 秒开始）",
    )
    parser.add_argument(
        "--msg-lifetime",
        type=positive_float_arg,
        default=14.0,
        help="消息停留秒数（仅 stack_mode=lanes；float 上浮模式忽略，默认 14）",
    )
    parser.add_argument("--max-message-lines", type=int, default=0, help="单条消息最多显示行数；0 表示不额外限制")
    parser.add_argument(
        "--min-visible-seconds",
        type=float,
        default=0.0,
        help="已上屏消息最短可见秒数（仅 stack_mode=lanes；float 忽略）；0 表示允许立即被顶替",
    )
    parser.add_argument("--arrival-interval", type=float, default=0.0, help="新消息最小入场间隔秒数；0 表示不限流")
    parser.add_argument(
        "--stack-mode",
        choices=("float", "lanes"),
        default="lanes",
        help="聊天堆叠: lanes=lifetime lane沉积(默认), float=Twitch上浮(仅容量顶出)",
    )
    parser.add_argument("--x-ratio", type=float, default=0.0, help="相对源视频宽度的 X 坐标；0 使用 --x")
    parser.add_argument("--y-ratio", type=float, default=0.0, help="相对源视频高度的 Y 坐标；0 使用 --y")
    parser.add_argument("--width-ratio", type=float, default=0.0, help="相对源视频宽度的 overlay 宽度；0 使用 --width")
    parser.add_argument("--height-ratio", type=float, default=0.0, help="相对源视频高度的 overlay 高度；0 使用 --height")
    parser.add_argument("--font-size-ratio", type=float, default=0.0, help="相对源视频高度的字号；0 使用 --font-size")
    parser.add_argument("--bg-alpha", type=int, default=255, help="背景透明度 0-255 (默认 255，不透明黑底)")
    parser.add_argument("--emote-height", type=int, default=22, help="emote 图片高度像素 (默认 22)")
    parser.add_argument("--offset", type=float, default=None, help="时间偏移修正秒数")
    parser.add_argument(
        "--allow-empty-chat",
        action="store_true",
        help="允许解析结果为 0 条消息并继续（默认失败，避免静默生成无弹幕成片）",
    )
    parser.add_argument("--keep-temp", action="store_true", help="保留中间文件")
    parser.add_argument(
        "--job-dir",
        default=None,
        help="本次运行的独立工作目录；默认在 --out-dir 下自动创建 job_<timestamp>_<pid>_*",
    )
    parser.add_argument(
        "--no-job-dir",
        action="store_true",
        help="不创建独立 job 目录（兼容旧行为，直接写入 --out-dir；并行运行不安全）",
    )
    parser.add_argument(
        "--no-backup-prev",
        action="store_true",
        help="不备份旧输出文件（默认开启：发布前自动备份为 .bak）",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="清理指定 --out-dir 下的临时文件后退出：默认只删 *.partial.mp4；加 --clean-all 才删全部已结束 job_/batch_；或配合 --job-dir 只清一个；默认不删 *.progress.json",
    )
    parser.add_argument(
        "--clean-all",
        action="store_true",
        help="与 --clean 联用：删除 out-dir 下全部已结束的工具 job_/batch_ 目录（仍跳过 running）",
    )
    parser.add_argument(
        "--clean-progress",
        action="store_true",
        help="与 --clean 联用：同时删除 *.progress.json 进度文件",
    )
    parser.add_argument("--export-translation", metavar="JSON_PATH", default=None, help="导出待翻译消息为 JSON")
    parser.add_argument(
        "--force-export",
        action="store_true",
        help="与 --export-translation 联用：允许覆盖已有非空 translation 的 JSON（默认拒绝，防丢译）",
    )
    parser.add_argument("--import-translation", metavar="JSON_PATH", default=None, help="导入翻译后的 JSON 渲染视频")
    parser.add_argument(
        "--strict-import",
        action="store_true",
        help="导入翻译时若 author/timestamp/original 不一致则硬失败（默认跳过错配条目）",
    )
    parser.add_argument("--preview-frame", type=float, default=None, help="只导出指定秒数的一张预览图，不合成整片")
    parser.add_argument("--preview-image", default=None, help="预览图输出路径，默认 <video>_preview_<秒数>s.png")
    parser.add_argument("--preview-clip", type=float, default=None, help="只渲染开头 N 秒短片，用于快速检查样式")
    parser.add_argument("--out-dir", default=None, help="中间文件和默认输出目录；默认使用源视频所在目录")
    parser.add_argument(
        "--layout-preset",
        default=None,
        help="渲染布局 YAML 预设（x/y/w/h/font/alpha/lifetime/emote 等）；命令行参数覆盖预设",
    )
    parser.add_argument(
        "--render-preset",
        default=None,
        help="编码/性能 YAML 预设路径（encoder/crf/overlay-codec 等；命令行优先覆盖）",
    )
    parser.add_argument(
        "--lazy-message-images",
        action="store_true",
        help="长片省内存：不预渲染全部消息图，按可见窗口缓存/LRU 生成",
    )
    parser.add_argument(
        "--message-image-cache-size",
        type=int,
        default=256,
        help="--lazy-message-images 时最多缓存多少条静态消息图（默认 256）",
    )

    # Performance / encode controls
    parser.add_argument(
        "--encoder", default="x264",
        choices=["auto", "x264", "nvenc", "qsv", "amf"],
        help="最终视频编码器：x264(默认稳妥) / auto(优先硬件) / nvenc / qsv / amf",
    )
    parser.add_argument(
        "--video-preset", default=None,
        help="编码预设。x264: ultrafast..veryslow；nvenc: p1..p7；默认按编码器自动选择",
    )
    parser.add_argument("--crf", type=int, default=18, help="质量参数 CRF/CQ（与 --video-bitrate 互斥优先码率）默认 18")
    parser.add_argument("--video-bitrate", default=None, help="目标视频码率，如 8M / 4000k；设置后走码率模式")
    parser.add_argument("--maxrate", default=None, help="最大码率，如 12M")
    parser.add_argument("--bufsize", default=None, help="码率缓冲，如 16M")
    parser.add_argument("--audio-codec", default="aac", choices=["aac", "copy"], help="音频编码：aac(默认重编码) 或 copy")
    parser.add_argument("--audio-bitrate", default="192k", help="AAC 码率，默认 192k")
    parser.add_argument(
        "--overlay-codec", default="vp9", choices=["vp9", "png"],
        help="聊天层中间格式：vp9=先转透明 WebM(默认)，png=直接用 PNG 序列叠加",
    )
    parser.add_argument("--webm-crf", type=int, default=30, help="WebM VP9 质量 CRF，默认 30")
    parser.add_argument(
        "--webm-cpu-used", type=int, default=4,
        help="libvpx-vp9 速度 0(慢/好)-8(快)，默认 4",
    )
    parser.add_argument(
        "--no-reuse-static-frames", action="store_true",
        help="禁用静态帧 hardlink/copy 复用（调试用）",
    )
    parser.add_argument(
        "--no-skip-blank-frames", action="store_true",
        help="禁用空白时段稀疏写帧（调试用）",
    )
    parser.add_argument(
        "--blank-hold-seconds", type=float, default=0.5,
        help="空白时段关键帧间隔秒数，默认 0.5；最终仍会补齐给 FFmpeg",
    )
    return parser


def resolve_preview_plan(chat_data, args, config, video_dur):
    """Preview time-window planning: densest window, filter instant, filtering.

    Extracted verbatim from _main: chooses the densest preview start
    (--preview-dense + --preview-clip), resolves the clamped preview filter
    instant (REND-R1), computes the chat window (with the float-mode anchor
    override), filters messages/emotes for the window and trims float
    carry-in by lane budget. Runs AFTER translation import so translation
    indices still refer to the full message list.

    Returns (chat_data, clip_start, win_start, win_end); main() applies
    ``config.preview_clip_start = clip_start`` at the boundary.
    """
    # Preview time-window: only keep messages/emotes that can appear in the window.
    # Runs AFTER import so translation indices still refer to the full message list.
    dense_info = None
    clip_start = 0.0
    stack_mode_cli = str(getattr(args, "stack_mode", "lanes") or "lanes").lower()
    # Window membership lifetime for preview filtering vs densest scoring:
    # - float has no time eviction. Filter must use a large horizon so older
    #   messages still on the capacity stack survive (clip_len is too short and
    #   drops carry-in that a full float render would show).
    # - densest scoring for float should prefer arrivals *inside* the candidate
    #   window (near-zero life), not the whole history (which marks every past
    #   message visible in every window).
    if stack_mode_cli == "float":
        window_life = max(float(video_dur or 0.0), float(args.msg_lifetime or 14.0), 3600.0)
        dense_score_life = 0.05  # ~arrival-in-window only
    else:
        window_life = float(args.msg_lifetime or 14.0)
        dense_score_life = window_life
    if getattr(args, "preview_dense", False) and args.preview_clip is None and not args.export_translation:
        print("  [WARN] --preview-dense 需要同时指定 --preview-clip，已忽略", flush=True)
    if (
        args.preview_clip is not None
        and args.preview_frame is None
        and getattr(args, "preview_dense", False)
        and not args.export_translation
    ):
        dense_info = find_densest_preview_start(
            chat_data.get("messages") or [],
            float(args.preview_clip),
            video_duration=video_dur,
            msg_lifetime=max(0.05, float(dense_score_life)),
        )
        clip_start = float(dense_info.get("start") or 0.0)
        if dense_info.get("warning"):
            print(f"  [WARN] {dense_info['warning']}", flush=True)
        print(
            f"  预览最密段: start={clip_start:.2f}s end={dense_info.get('end'):.2f}s "
            f"score={dense_info.get('score')} mode={dense_info.get('mode')}",
            flush=True,
        )

    # REND-R1: filtering must use the same clamped instant the renderer uses
    # (min(preview_frame, min(source_dur, clip))). Otherwise a frame past the
    # preview duration keeps only messages the scheduler then drops → silent
    # empty (lanes) / misaligned (float) preview.
    preview_filter_t, preview_time_warning = resolve_preview_frame_time(
        args.preview_frame,
        args.preview_clip,
        video_dur,
    )
    if preview_time_warning:
        print(f"  [WARN] {preview_time_warning}", flush=True)
    win_start, win_end = preview_window(
        preview_filter_t,
        args.preview_clip,
        window_life,
        clip_start=clip_start if args.preview_clip is not None else None,
    )
    # Float has no lifetime. preview_window(preview_frame) would set start=t-life and
    # make nearly every message "in-window", defeating capacity carry-in trim.
    # Anchor the window at the frame instant so pre-window = history before t.
    if (
        stack_mode_cli == "float"
        and preview_filter_t is not None
        and args.preview_clip is None
    ):
        frame_t = max(0.0, float(preview_filter_t))
        win_start, win_end = frame_t, frame_t + 0.05
    if win_start is not None and win_end is not None and not args.export_translation:
        before_n = len(chat_data.get("messages") or [])
        before_e = len(chat_data.get("emote_map") or {})
        # Rebase timestamps relative to clip start for densest mid-video clips so
        # render/compose stay simple (pair with ffmpeg -ss). Negative timestamps
        # preserve remaining lanes lifetime for carry-in messages.
        rebase = bool(args.preview_clip is not None and clip_start > 1e-6)
        float_cap = None
        if stack_mode_cli == "float":
            raw_cap = int(getattr(config, "max_visible", 0) or 0)
            float_cap, _capacity, float_budget_warn = resolve_lane_budget(
                raw_cap,
                config.height,
                config.font_size,
            )
            if float_budget_warn:
                print(f"[WARN] {float_budget_warn}", flush=True)
        chat_data = filter_chat_for_time_window(
            chat_data,
            win_start,
            win_end,
            window_life,
            rebase_to_zero=rebase,
            float_capacity_lines=float_cap,
            max_message_lines=int(getattr(config, "max_message_lines", 0) or 0),
        )
        # Float safety net: trim pre-window by line budget (prefilter already limits deepcopy).
        if stack_mode_cli == "float" and float_cap is not None:
            carry_origin = 0.0 if rebase else float(win_start)
            before_trim = len(chat_data.get("messages") or [])
            chat_data = trim_float_carry_in_messages(
                chat_data,
                carry_origin,
                float_cap,
                max_message_lines=int(getattr(config, "max_message_lines", 0) or 0),
            )
            after_trim = len(chat_data.get("messages") or [])
            prefilter = (chat_data.get("_window") or {}).get("float_prefilter") or {}
            if prefilter:
                print(
                    f"  float 预览 prefilter: pre-window "
                    f"{prefilter.get('pre_window_before')}->{prefilter.get('pre_window_after')} "
                    f"(capacity≈{float_cap} lines)",
                    flush=True,
                )
            if after_trim < before_trim:
                print(
                    f"  float 预览 carry-in 截断: {before_trim}->{after_trim} "
                    f"(capacity≈{float_cap})",
                    flush=True,
                )
        after_n = len(chat_data.get("messages") or [])
        after_e = len(chat_data.get("emote_map") or {})
        print(
            f"  预览时间窗 [{win_start:.2f}s, {win_end:.2f}s]"
            f"{' (rebase→0)' if rebase else ''}: "
            f"消息 {before_n}->{after_n}, emote {before_e}->{after_e}",
            flush=True,
        )
    return chat_data, clip_start, win_start, win_end


def _main(status_sink=None):
    """Full CLI pipeline. ``status_sink`` (dict) receives the resolved job out_dir
    so the public main() wrapper can mark run_meta failed on unexpected crashes."""
    media_probe.cache_clear()
    from env_bootstrap import prepend_tools_ffmpeg_to_path

    prepend_tools_ffmpeg_to_path()
    parser = build_arg_parser()
    args = parser.parse_args()

    companion_err = clean_companion_flags_error(args)
    if companion_err:
        print(companion_err)
        return 2

    # --clean: scan and remove temp files from --out-dir, then exit
    if getattr(args, "clean", False):
        if args.out_dir:
            out_base = os.path.abspath(args.out_dir)
        elif args.video and os.path.isfile(args.video):
            out_base = os.path.dirname(os.path.abspath(args.video)) or os.getcwd()
        else:
            out_base = os.getcwd()
        if not os.path.isdir(out_base):
            print(f"--clean: 目录不存在: {out_base}")
            return 1
        if is_dangerous_publish_path(out_base):
            print(f"--clean: 拒绝在系统目录下清理: {out_base}")
            return 2
        only_job = None
        clean_all = bool(getattr(args, "clean_all", False))
        # --job-dir scopes clean even with --clean-all (one job, not whole out-dir).
        if args.job_dir:
            only_job = os.path.abspath(args.job_dir)
            if not path_is_under(only_job, out_base):
                print(
                    f"错误: --job-dir 必须位于 --out-dir 之下\n"
                    f"  job-dir: {only_job}\n"
                    f"  out-dir: {out_base}"
                )
                return 2
            if not os.path.isdir(only_job):
                print(f"错误: --job-dir 不存在: {only_job}")
                return 2
        count, freed = clean_temp_artifacts(
            out_base,
            clean_progress=bool(getattr(args, "clean_progress", False)),
            clean_all=clean_all if only_job is None else False,
            only_job_dir=only_job,
        )
        print(f"\n清理完成: {count} 项, 释放 {freed / (1024 * 1024):.1f} MB")
        return 0
    install_process_cleanup_handlers()

    if getattr(args, "layout_preset", None):
        try:
            preset = load_layout_preset(args.layout_preset)
            applied = apply_layout_preset_to_namespace(args, preset, cli_defaults={
                "x": 15, "y": 327, "width": 497, "height": 363,
                "font_size": 15, "font_path": "auto", "font_bold_path": "auto",
                "fps": 15, "max_visible": 0, "msg_lifetime": 14.0,
                "max_message_lines": 0, "min_visible_seconds": 0.0, "arrival_interval": 0.0,
                "stack_mode": "lanes",
                "x_ratio": 0.0, "y_ratio": 0.0, "width_ratio": 0.0, "height_ratio": 0.0,
                "font_size_ratio": 0.0, "bg_alpha": 255, "emote_height": 22,
                "blank_hold_seconds": 0.5,
            })
            if applied:
                print(f"[layout-preset] 已加载: {args.layout_preset} -> {', '.join(applied)}", flush=True)
        except (OSError, ValueError) as e:
            parser.error(str(e))

    # Note: explicit range checks run once via _validate_runtime_args AFTER both
    # presets apply (see below) — CLI flags and preset overrides share one path.

    if getattr(args, "render_preset", None):
        try:
            rpreset = load_render_preset(args.render_preset)
            rapplied = apply_render_preset_to_namespace(
                args,
                rpreset,
                cli_defaults={
                    # Must match argparse defaults above (encoder default is x264).
                    "encoder": "x264",
                    "video_preset": None,
                    "crf": 18,
                    "video_bitrate": None,
                    "maxrate": None,
                    "bufsize": None,
                    "audio_codec": "aac",
                    "audio_bitrate": "192k",
                    "overlay_codec": "vp9",
                    "webm_crf": 30,
                    "webm_cpu_used": 4,
                    "output_fps": None,
                    "fps": 15,
                    "blank_hold_seconds": 0.5,
                    "message_image_cache_size": 256,
                    "lazy_message_images": False,
                },
            )
            if rapplied:
                print(f"[render-preset] 已加载: {args.render_preset} -> {', '.join(rapplied)}", flush=True)
        except Exception as e:
            print(f"[render-preset] 加载失败: {e}", flush=True)
            return 2

    # Validate the final namespace after both layout and render presets apply.
    try:
        _validate_runtime_args(args)
    except ValueError as e:
        parser.error(str(e))
    try:
        encode_opts = resolve_encode_options(
            encoder=args.encoder,
            video_preset=args.video_preset,
            crf=args.crf,
            video_bitrate=args.video_bitrate,
            maxrate=args.maxrate,
            bufsize=args.bufsize,
            audio_codec=args.audio_codec,
            audio_bitrate=args.audio_bitrate,
            overlay_codec=args.overlay_codec,
            webm_crf=args.webm_crf,
            webm_cpu_used=args.webm_cpu_used,
            prefer_hw=(args.encoder == "auto"),
        )
    except ValueError as e:
        parser.error(str(e))

    video_path = os.path.abspath(args.video)
    html_path = os.path.abspath(args.chat_html)

    # 验证输入
    if not os.path.isfile(html_path):
        print(f"错误: HTML 文件不存在: {html_path}")
        sys.exit(1)
    # 导出翻译模式不需要视频文件
    if not args.export_translation and not os.path.isfile(video_path):
        print(f"错误: 视频文件不存在: {video_path}")
        sys.exit(1)
    # 检查 ffmpeg（导出翻译模式不需要）
    if not args.export_translation:
        try:
            subprocess.run(
                [require_executable("ffmpeg"), "-version"],
                capture_output=True,
                check=True,
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            print("错误: ffmpeg 不可用或版本探测超时，请检查安装与 PATH")
            sys.exit(1)

    # Working directories:
    # - out_base: user-facing directory for final video / export paths
    # - work_dir: unique per-run temp (job dir) so concurrent runs do not wipe each other
    if args.out_dir:
        out_base = os.path.abspath(args.out_dir)
    elif os.path.isfile(video_path):
        out_base = os.path.dirname(os.path.abspath(video_path))
    elif args.export_translation:
        # Export-only without a real video: allow writing next to the export path.
        out_base = os.path.dirname(os.path.abspath(args.export_translation)) or os.getcwd()
    else:
        out_base = os.getcwd()
    # Refuse system roots before creating anything under them.
    if is_dangerous_publish_path(out_base):
        print(f"错误: --out-dir 不能是系统目录: {out_base}")
        sys.exit(2)
    os.makedirs(out_base, exist_ok=True)

    if args.job_dir:
        work_dir = os.path.abspath(args.job_dir)
        # Security: refuse arbitrary --job-dir outside out_base.
        if not path_is_under(work_dir, out_base):
            print(
                f"错误: --job-dir 必须位于 --out-dir 之下\n"
                f"  job-dir: {work_dir}\n"
                f"  out-dir: {out_base}"
            )
            sys.exit(2)
        if is_dangerous_publish_path(work_dir):
            print(f"错误: --job-dir 不能是系统目录: {work_dir}")
            sys.exit(2)
        os.makedirs(work_dir, exist_ok=True)
    elif args.no_job_dir or args.export_translation:
        # Export-only and legacy mode can write directly into out_base.
        work_dir = out_base
    else:
        work_dir = str(make_job_dir(out_base, prefix="job_"))
        print(f"工作目录(job): {work_dir}", flush=True)

    out_dir = work_dir
    if status_sink is not None:
        # Publish the location before any rendering work so a later crash can
        # still mark run_meta failed at the right job directory.
        status_sink["out_dir"] = out_dir

    # Resolve "auto" fonts without inventing a platform-foreign path.
    try:
        font_path, font_bold_path = resolve_font_paths(args.font_path, args.font_bold_path)
    except FileNotFoundError as e:
        print(f"错误: {e}")
        sys.exit(1)

    config = OverlayConfig(
        x=args.x,
        y=args.y,
        width=args.width,
        height=args.height,
        font_size=args.font_size,
        font_path=font_path,
        font_bold_path=font_bold_path,
        fps=args.fps,
        output_fps=args.output_fps,
        max_visible=args.max_visible,
        msg_lifetime=args.msg_lifetime,
        max_message_lines=args.max_message_lines,
        min_visible_seconds=args.min_visible_seconds,
        arrival_interval=args.arrival_interval,
        stack_mode=getattr(args, "stack_mode", "lanes"),
        x_ratio=args.x_ratio,
        y_ratio=args.y_ratio,
        width_ratio=args.width_ratio,
        height_ratio=args.height_ratio,
        font_size_ratio=args.font_size_ratio,
        bg_alpha=args.bg_alpha,
        emote_h=args.emote_height,
        preview_frame=args.preview_frame,
        preview_image=args.preview_image,
        preview_clip=args.preview_clip,
        preview_clip_start=0.0,
        reuse_static_frames=not args.no_reuse_static_frames,
        skip_blank_frames=not args.no_skip_blank_frames,
        blank_hold_seconds=args.blank_hold_seconds,
        encode=encode_opts,
        lazy_message_images=bool(getattr(args, "lazy_message_images", False)),
        message_image_cache_size=int(getattr(args, "message_image_cache_size", 256) or 256),
        no_backup_prev=bool(getattr(args, "no_backup_prev", False)),
    )

    # 检查 PIL（导出翻译模式不需要）
    if not args.export_translation:
        import importlib.util

        if importlib.util.find_spec("PIL") is None:
            print("错误: 需要 Pillow 库，请运行 pip install pillow")
            sys.exit(1)

    apply_relative_layout(config, video_path)
    # run.bat / layout_default use absolute 1080p pixels; scale into non-1080p frames.
    adapt_note = adapt_absolute_layout_to_source(config, video_path)
    if adapt_note:
        print(f"[INFO] {adapt_note}", flush=True)
    print(f"视频: {video_path}")
    print(f"聊天: {html_path}")
    print(f"区域: x={config.x} y={config.y} w={config.width} h={config.height}")
    for warn in layout_bounds_warnings(config, video_path):
        print(f"[WARN] {warn}", flush=True)
    resolved_out_fps = media_probe.resolve_output_fps(video_path, explicit=config.output_fps, fallback=30)
    config.output_fps = resolved_out_fps
    print(
        f"字体: {config.font_size}px, 弹幕帧率: {config.fps}fps, "
        f"成片帧率: {config.output_fps}fps"
        + (" (跟随源视频)" if args.output_fps is None else "")
    )
    print(
        f"性能: static_reuse={'on' if config.reuse_static_frames else 'off'}, "
        f"blank_skip={'on' if config.skip_blank_frames else 'off'}, "
        f"{summarize_encode_options(encode_opts)}"
    )
    print()

    # Step 1: 解析 HTML
    chat_data = parse_chat_html(html_path, out_dir)
    if not (chat_data.get("messages") or []):
        if not bool(getattr(args, "allow_empty_chat", False)):
            print("错误: 聊天 HTML 未解析出任何有效消息，已停止以避免生成无弹幕成片")
            print("  如确认该 VOD 聊天本来为空，可显式添加 --allow-empty-chat")
            return 1
        print("  [WARN] --allow-empty-chat: 将继续生成无消息 overlay", flush=True)

    # Structured offset diagnosis (manual / auto / warnings).
    video_dur = None
    if (not args.export_translation) or args.offset is None:
        if os.path.isfile(video_path):
            try:
                video_dur = media_probe.probe_video_duration(video_path)
            except RuntimeError as e:
                print(f"  [WARN] {e}", flush=True)
                video_dur = None

    offset_info = compute_time_offset(
        chat_data.get("messages") or [],
        video_duration=video_dur,
        manual_offset=args.offset,
    )
    # Print structured diagnosis once (human-readable Chinese block).
    try:
        print(format_offset_diagnosis(offset_info), flush=True)
    except Exception:
        for warn in offset_info.get("warnings") or []:
            print(f"  [WARN] {warn}", flush=True)
        if offset_info["mode"] == "manual":
            print(f"  使用手动偏移: {offset_info['offset']:.1f}s", flush=True)
        elif offset_info["mode"] == "auto":
            print(f"  自动检测时间偏移: {offset_info['offset']:.0f}s (直播片段起始)", flush=True)

    if offset_info["offset"]:
        apply_time_offset(chat_data["messages"], offset_info["offset"])
        if chat_data["messages"]:
            print(
                f"  修正后时间范围: {chat_data['messages'][0]['timestamp']:.1f}s - "
                f"{chat_data['messages'][-1]['timestamp']:.1f}s",
                flush=True,
            )
    if offset_info.get("confirm_with_preview") or (
        (args.preview_frame is not None or args.preview_clip is not None)
        and offset_info["mode"] == "auto"
    ):
        print("  [提示] 偏移为启发式结果，请用预览图/短片人工确认后再出长片", flush=True)

    # --- 翻译导出（必须在时间窗过滤之前，index 对齐全量消息）---
    if args.export_translation:
        export_path = os.path.abspath(args.export_translation)
        # Confine export writes under out_base (same policy as --job-dir).
        if not path_is_under(export_path, out_base):
            # Allow writing next to video / cwd only when still under out_base after abs.
            # If user passes an absolute path outside out_base, refuse.
            print(
                f"错误: --export-translation 必须位于 --out-dir 之下\n"
                f"  export: {export_path}\n"
                f"  out-dir: {out_base}"
            )
            sys.exit(2)
        # Ensure stream_timestamp is stamped even when offset is 0.
        apply_time_offset(chat_data["messages"], 0.0)
        try:
            payload = write_export_translation_json(
                export_path,
                chat_data,
                offset_info=offset_info,
                force=bool(getattr(args, "force_export", False)),
            )
        except FileExistsError as e:
            print(f"错误: {e}")
            sys.exit(2)
        n = len(payload.get("messages") or [])
        print(f"\n[OK] 已导出 {n} 条待翻译消息到: {export_path}")
        print("   时间基准: stream（广播绝对时间）；export_offset="
              f"{payload.get('export_offset', 0)}")
        print("   编辑该文件，填写每条消息的 \"translation\" 字段，然后运行:")
        print(f"   {_format_import_translation_command(args.video, args.chat_html, export_path)}")
        return

    # --- 翻译导入（必须在预览时间窗过滤之前）---
    # 否则 filter 会缩短 messages 列表，JSON 的全局 index 会对错消息（静默错贴）。
    if args.import_translation:
        import_path = os.path.abspath(args.import_translation)
        with open(import_path, encoding="utf-8") as f:
            trans_data = json.load(f)
        try:
            replaced, stripped_placeholders, import_warnings = apply_imported_translations(
                chat_data,
                trans_data,
                strict=bool(getattr(args, "strict_import", False)),
            )
        except ValueError as e:
            print(f"错误: 翻译导入失败: {e}")
            sys.exit(1)
        for warn in import_warnings:
            print(f"  [WARN] {warn}", flush=True)
        print(f"  已导入 {replaced} 条翻译", flush=True)
        if stripped_placeholders:
            print(f"  已移除 {stripped_placeholders} 个与原始表情重复的 [表情名] 占位符", flush=True)
        # Filled translation JSON that applied zero rows is almost always identity
        # mismatch (offset/HTML drift). Continuing would burn original English with
        # exit 0 — silent-wrong. Fail unless the JSON truly has no translations.
        filled = 0
        try:
            for _it in (trans_data.get("messages") or []):
                if isinstance(_it, dict) and str(_it.get("translation", "") or "").strip():
                    filled += 1
        except Exception:
            filled = 0
        if filled > 0 and replaced == 0:
            print(
                "错误: 翻译 JSON 含非空 translation，但 0 条通过身份校验并导入。\n"
                "  请核对 HTML 是否同一导出、offset 是否一致；调试可用 --strict-import。\n"
                "  若确实只想烧原文，去掉 --import-translation / --reuse-translation。",
                flush=True,
            )
            sys.exit(1)

    # Preview time-window: only keep messages/emotes that can appear in the window.
    # Runs AFTER import so translation indices still refer to the full message list.
    # Ordering contract (deep-audit guard): the translation-import block above
    # must run BEFORE filter_chat_for_time_window — the actual
    # filter_chat_for_time_window / trim_float_carry_in_messages calls live in
    # resolve_preview_plan below, invoked here after both the export and import
    # translation branches, so indices stay aligned with the full message list.
    chat_data, clip_start, win_start, win_end = resolve_preview_plan(
        chat_data, args, config, video_dur,
    )
    config.preview_clip_start = clip_start

    # Persist run metadata early so failures still leave a breadcrumb.
    if not args.export_translation:
        write_run_meta(out_dir, {
            "status": "running",
            "video": video_path,
            "chat_html": html_path,
            "out_base": out_base,
            "job_dir": out_dir,
            "fps": config.fps,
            "offset": offset_info,
            "preview_frame": args.preview_frame,
            "preview_clip": args.preview_clip,
            "window": {"start": win_start, "end": win_end},
            "config": config.to_dict(),
            "encode": encode_opts.to_dict() if encode_opts else None,
            "argv": list(sys.argv),
        })

    # Step 2: 渲染帧
    render_result = render_overlay(chat_data, out_dir, video_path, config)
    # 唯一合法回写点: main 在边界把渲染结果注入 config —— 保持 run_meta 的
    # config dump (to_dict()) 与 compose 阶段汇总的历史形状，render_overlay
    # 自身不再有运行时 config 副作用。
    config.stage_timings = dict(render_result.timings)
    config.frame_stats = dict(render_result.stats)
    config.lazy_message_images = bool(
        int(render_result.stats.get("lazy_message_images", 0) or 0)
    )
    if render_result.preview_path:
        config.preview_image = render_result.preview_path
    frames_dir = render_result.frames_dir
    duration = render_result.duration
    if args.preview_clip is not None:
        duration = min(duration, max(0.1, float(args.preview_clip)))

    def _promote_to_out_base_locked(src_path: str) -> str | None:
        """Copy a job-dir artifact to out_base with temp+replace and .bak restore.

        Concurrent runs sharing the same out_base each have a unique job_ dir.
        If the basenames would collide (e.g. both promote video_chat.mp4), derive a
        job-unique name so the last writer does not silently overwrite the other.
        """
        if not src_path or not os.path.isfile(src_path):
            return None
        if os.path.abspath(out_dir) == os.path.abspath(out_base):
            return src_path
        base_name = os.path.basename(src_path)
        promoted = os.path.join(out_base, base_name)
        # Another process may own the same default name under out_base. Prefer a
        # job-tagged filename when the target exists and is not our own prior output.
        if os.path.isfile(promoted):
            job_tag = os.path.basename(os.path.abspath(out_dir))
            if job_tag.startswith("job_") or job_tag.startswith("batch_"):
                stem, ext = os.path.splitext(base_name)
                alt = os.path.join(out_base, f"{stem}__{job_tag}{ext}")
                # Only switch when alt is free or we are re-promoting into alt.
                if not os.path.isfile(alt) or os.path.abspath(src_path) != os.path.abspath(promoted):
                    # If promoted exists from a concurrent job, use unique name.
                    # Heuristic: if mtime is very recent and path differs from src, collide.
                    try:
                        same_file = os.path.samefile(src_path, promoted)
                    except OSError:
                        same_file = False
                    if not same_file:
                        print(
                            f"  [concurrent] 输出目录已有 {base_name}，改用唯一名: {os.path.basename(alt)}",
                            flush=True,
                        )
                        promoted = alt
        backup = None
        backup_created = False
        # Back up existing output before overwriting (default behavior).
        if not getattr(args, "no_backup_prev", False) and os.path.isfile(promoted):
            backup = promoted + ".bak"
            try:
                if os.path.isfile(backup):
                    os.remove(backup)
                os.rename(promoted, backup)
                backup_created = True
                print(f"  [backup] {backup}", flush=True)
            except OSError as e:
                print(f"  warning: cannot backup {promoted}: {e}", flush=True)
                backup = None
                backup_created = False
        partial_promoted = promoted + ".partial"
        try:
            try:
                os.remove(partial_promoted)
            except FileNotFoundError:
                pass
            shutil.copy2(src_path, partial_promoted)
            os.replace(partial_promoted, promoted)
            print(f"  已发布到输出目录: {promoted}", flush=True)
            return promoted
        except OSError as e:
            print(f"  警告: 无法发布到 {promoted}: {e}; 保留 job 内文件: {src_path}", flush=True)
            if backup_created and backup and os.path.isfile(backup) and not os.path.isfile(promoted):
                try:
                    os.rename(backup, promoted)
                    print(f"  已从备份恢复: {promoted}", flush=True)
                except OSError as restore_err:
                    print(f"  警告: 无法从备份恢复 {backup}: {restore_err}", flush=True)
            return None

    def promote_to_out_base(src_path: str) -> str | None:
        """Serialize basename selection and publication across concurrent jobs."""
        if not src_path or not os.path.isfile(src_path):
            return None
        if os.path.abspath(out_dir) == os.path.abspath(out_base):
            return src_path
        base_name = os.path.basename(src_path)
        lock_path = os.path.join(out_base, f".{base_name}.publish.guard")
        published = None
        try:
            with exclusive_file_lock(lock_path, timeout=30.0):
                published = _promote_to_out_base_locked(src_path)
        except OSError as exc:
            print(f"  警告: 等待输出发布锁失败 {lock_path}: {exc}", flush=True)
            return None
        if published:
            # Success: drop the guard file so out_base is not littered. Failure /
            # exception paths keep it for post-mortem. Best-effort — a concurrent
            # waiter still holding the lock open (notably on Windows) can block
            # the unlink; that only leaves the file behind, never breaks publishing.
            try:
                os.remove(lock_path)
            except OSError:
                pass
        return published


    if args.preview_frame is not None:
        # render_overlay already wrote the preview image and set config.preview_image
        # to the actual path (may be the user-requested path after copy).
        preview_path = getattr(config, "preview_image", None)
        if not preview_path or not os.path.isfile(preview_path):
            # Fallback: look under out_dir by requested basename / default name.
            preview_t = float(args.preview_frame)
            default_name = f"{Path(video_path).stem}_preview_{preview_t:.1f}s.png".replace(".0s", "s")
            candidates = []
            if args.preview_image:
                candidates.append(os.path.join(out_dir, os.path.basename(str(args.preview_image))))
                candidates.append(os.path.abspath(str(args.preview_image)))
            candidates.append(os.path.join(out_dir, default_name))
            preview_path = next((p for p in candidates if p and os.path.isfile(p)), candidates[0])
        needs_promote = (
            path_is_under(preview_path, out_dir)
            and os.path.abspath(out_dir) != os.path.abspath(out_base)
        )
        if needs_promote:
            final_preview = promote_to_out_base(preview_path)
        else:
            final_preview = preview_path if os.path.isfile(preview_path) else None
        if not final_preview:
            mark_run_status(
                out_dir,
                "failed",
                stage="publish_preview",
                job_output=preview_path,
                out_base=out_base,
            )
            print("\n[FAIL] 预览图发布失败，job 内文件已保留")
            print(f"   预览文件: {preview_path}")
            print(f"   排查目录: {out_dir}")
            return 1
        print(f"\n[OK] 预览图已生成，跳过视频合成: {final_preview}")
        if not args.keep_temp and os.path.abspath(out_dir) != os.path.abspath(out_base):
            shutil.rmtree(out_dir, ignore_errors=True)
        return 0

    # Step 3: 合成视频
    compose_result = compose_video(video_path, frames_dir, out_dir, config, duration)
    if compose_result is not None:
        # 唯一合法回写点: compose 结果（成片帧率 / 阶段耗时）经 main 注入 config。
        # compose 的 timings 已包含渲染阶段耗时（入口从 config.stage_timings 播种）。
        if compose_result.output_fps is not None:
            config.output_fps = compose_result.output_fps
        config.stage_timings = dict(compose_result.timings)

    # Promote final video from job dir to out_base when they differ.
    final_result = (
        promote_to_out_base(compose_result.output_path) if compose_result else None
    )

    # Step 4: 清理
    print("[4/4] 清理临时文件...", flush=True)
    cleaned = 0
    used_isolated_job = (
        (not args.no_job_dir)
        and (not args.export_translation)
        and os.path.abspath(out_dir) != os.path.abspath(out_base)
    )

    # Persist run_meta BEFORE deleting the job dir so success still leaves an audit trail
    # under out_base when the isolated job directory is removed.
    if final_result:
        mark_run_status(
            out_dir,
            "success",
            output=final_result,
            job_output=(compose_result.output_path if compose_result else None),
            out_base=out_base,
            keep_temp=bool(args.keep_temp),
        )
        # Always mirror run_meta next to the final output so a later full run
        # overwrites a stale preview success meta under out_base.
        if used_isolated_job:
            try:
                durable = os.path.join(out_base, Path(video_path).stem + "_run_meta.json")
                src_meta = os.path.join(out_dir, "run_meta.json")
                if os.path.isfile(src_meta):
                    shutil.copy2(src_meta, durable)
                    print(f"  运行元数据已保存: {durable}", flush=True)
            except OSError as e:
                print(f"  警告: 无法保存 run_meta 到输出目录: {e}", flush=True)
    else:
        failure_stage = "publish" if compose_result else "compose_or_render"
        mark_run_status(
            out_dir,
            "failed",
            stage=failure_stage,
            note="job output and diagnostics retained for recovery",
        )
        # Always leave a durable breadcrumb next to out_base so a failed full
        # render does not leave only a stale success meta from a prior preview.
        if used_isolated_job:
            try:
                durable = os.path.join(out_base, Path(video_path).stem + "_run_meta.json")
                src_meta = os.path.join(out_dir, "run_meta.json")
                if os.path.isfile(src_meta):
                    shutil.copy2(src_meta, durable)
                    print(f"  失败元数据已保存: {durable}", flush=True)
            except OSError as e:
                print(f"  警告: 无法保存失败 run_meta 到输出目录: {e}", flush=True)

    if not args.keep_temp:
        if not final_result:
            print(f"   失败现场已保留在 {out_dir}", flush=True)
        elif used_isolated_job and os.path.isfile(final_result):
            # Whole job directory is disposable after successful publish + meta copy.
            shutil.rmtree(out_dir, ignore_errors=True)
            cleaned = 1
        else:
            temp_items = [
                os.path.join(out_dir, "chat_data.json"),
                os.path.join(out_dir, "emotes"),
                os.path.join(out_dir, "overlay_frames"),
                os.path.join(out_dir, "overlay_temp.webm"),
                os.path.join(out_dir, "ffmpeg-webm.log"),
                os.path.join(out_dir, "ffmpeg-overlay.log"),
            ]
            for item in temp_items:
                if os.path.isfile(item):
                    os.remove(item)
                    cleaned += 1
                elif os.path.isdir(item):
                    shutil.rmtree(item, ignore_errors=True)
                    cleaned += 1
    else:
        print(f"   --keep-temp: 中间文件保留在 {out_dir}", flush=True)

    if final_result:
        print(f"\n[OK] 完成! 输出: {final_result}")
        if args.keep_temp:
            print(f"   中间文件保留在 {out_dir}")
            print(f"   运行元数据: {os.path.join(out_dir, 'run_meta.json')}")
        else:
            print(f"   已清理 {cleaned} 个临时文件/目录")
    else:
        print("\n[FAIL] 视频合成失败，请检查上方错误信息")
        print(f"   排查目录: {out_dir}")
        print(f"   运行元数据: {os.path.join(out_dir, 'run_meta.json')}")
        print(
            f"   FFmpeg 日志: {os.path.join(out_dir, 'ffmpeg-overlay.log')} / "
            f"{os.path.join(out_dir, 'ffmpeg-webm.log')}"
        )
        return 1


def main():
    """CLI entry point: run the pipeline, marking run_meta failed on crashes.

    An unexpected exception (disk-full OSError, renderer bug, ...) previously
    left run_meta stuck at status=running plus a bare traceback. The wrapper
    records a failed status at the job directory first, then re-raises so the
    caller still sees the real error and non-zero exit code.
    """
    media_probe.cache_clear()
    status_sink: dict[str, str | None] = {"out_dir": None}
    try:
        return _main(status_sink=status_sink)
    except Exception as exc:
        out_dir = status_sink.get("out_dir")
        if out_dir:
            try:
                mark_run_status(
                    out_dir,
                    "failed",
                    stage="unexpected_error",
                    note=f"{type(exc).__name__}: {exc}",
                )
            except OSError:
                pass
        raise


if __name__ == "__main__":
    # Propagate int return codes from early-exit paths (--clean, validation).
    raise SystemExit(main())
