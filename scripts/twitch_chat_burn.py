#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Twitch Chat Overlay Tool
========================
从 Twitch HTML 聊天记录生成弹幕覆盖层，并合成到视频上。

用法:
  python twitch_chat_burn.py <video.mp4> <chat.html> [选项]

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
import bisect
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import unicodedata

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
    trim_float_carry_in_messages,
)
from common_utils import (
    current_cli_invocation,
    ensure_utf8_stdio,
    hex_to_rgb_soft,
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
from encode_options import (
    build_audio_encode_args,
    build_video_encode_args,
    build_webm_encode_args,
    resolve_encode_options,
    summarize_encode_options,
)
from layout_preset import apply_layout_preset_to_namespace, load_layout_preset
from overlay_config import OverlayConfig
from process_util import (
    clean_companion_flags_error,
    clean_temp_artifacts,
    install_process_cleanup_handlers,
    is_dangerous_publish_path,
    make_job_dir,
    path_is_under,
    run_tracked,
)
from render_perf import (
    assert_contiguous_frame_sequence,
    blank_gap_frame_indexes,
    expand_frame_sequence_for_ffmpeg,
    missing_frame_indexes,
    write_or_reuse_frame,
)
from render_preset import apply_render_preset_to_namespace, load_render_preset
from run_meta import mark_run_status, write_run_meta
from translation_support import clean_translation_text as clean_imported_translation

# ============================================================
# 中文换行辅助函数
# ============================================================

def is_cjk_char(ch):
    """判断字符是否为 CJK 字符（中文/日文/韩文）"""
    cp = ord(ch)
    if (0x4E00 <= cp <= 0x9FFF or      # CJK Unified Ideographs
        0x3400 <= cp <= 0x4DBF or      # CJK Extension A
        0x20000 <= cp <= 0x2A6DF or    # CJK Extension B
        0xFF00 <= cp <= 0xFFEF or      # Fullwidth Forms
        0x3000 <= cp <= 0x303F):       # CJK Symbols & Punctuation
        return True
    return False

def split_text_for_wrap(text, text_width_fn, max_w):
    """
    将文本拆分为可在 max_w 宽度内显示的行。
    支持中文（逐字换行）和英文（按词换行）混合文本。
    返回 list[str]，每个元素是一行文本。
    """
    lines = []
    cur = ""
    cur_w = 0
    space_w = text_width_fn(" ")
    
    i = 0
    while i < len(text):
        ch = text[i]
        ch_w = text_width_fn(ch)
        
        if ch == " ":
            if cur_w + space_w > max_w and cur:
                lines.append(cur)
                cur = ""
                cur_w = 0
            else:
                cur += ch
                cur_w += space_w
            i += 1
            continue
        
        if is_cjk_char(ch):
            # CJK 字符：可以任意位置断行
            if cur_w + ch_w > max_w and cur:
                lines.append(cur.rstrip())
                cur = ch
                cur_w = ch_w
            else:
                cur += ch
                cur_w += ch_w
            i += 1
            continue
        else:
            # ASCII/拉丁字符：按词拆分
            word_end = i
            while word_end < len(text) and text[word_end] != " " and not is_cjk_char(text[word_end]):
                word_end += 1
            word = text[i:word_end]
            word_w = text_width_fn(word)
            
            if cur_w + (space_w if cur and not cur.endswith(" ") else 0) + word_w > max_w and cur:
                lines.append(cur.rstrip())
                cur = word
                cur_w = word_w
            else:
                if cur and not cur.endswith(" "):
                    cur += " "
                    cur_w += space_w
                cur += word
                cur_w += word_w
            i = word_end
    
    if cur:
        lines.append(cur.rstrip())
    
    return lines if lines else [""]

def wrap_fragments(frag_list, header_w, max_w, padding, indent, gap, text_width_fn):
    """
    将 fragments 列表按宽度拆分成多行（支持中文换行）。
    frag_list: [("text", text_str, width) | ("emote", class_name, width)]
    返回: list[list[tuple]] — 每行是 (type, content, width) 的列表
    """
    lines = []
    cur_line = []
    cur_x = header_w  # 第一行从 header 之后开始
    
    for ftype, fcontent, fwidth in frag_list:
        if ftype == "text":
            # 当前可用宽度
            avail = max_w - cur_x
            if avail < 20:
                # 换行
                if cur_line:
                    lines.append(cur_line)
                cur_line = []
                cur_x = padding + indent
                avail = max_w - cur_x
            
            sub_lines = split_text_for_wrap(fcontent, text_width_fn, avail)
            
            for si, sl in enumerate(sub_lines):
                if si > 0:
                    if cur_line:
                        lines.append(cur_line)
                    cur_line = []
                    cur_x = padding + indent
                
                sl_stripped = sl.strip()
                if not sl_stripped:
                    continue
                sl_w = text_width_fn(sl_stripped)
                limit = max_w - cur_x
                
                if sl_w <= limit:
                    cur_line.append(("text", sl_stripped, sl_w))
                    cur_x += sl_w
                else:
                    # 逐字/逐词添加
                    for ci in range(len(sl_stripped)):
                        ch = sl_stripped[ci]
                        ch_w = text_width_fn(ch)
                        if cur_x + ch_w > max_w and cur_line:
                            lines.append(cur_line)
                            cur_line = []
                            cur_x = padding + indent
                        cur_line.append(("text", ch, ch_w))
                        cur_x += ch_w
        elif ftype == "emote":
            ew = fwidth
            if cur_x + ew + gap > max_w and cur_line:
                lines.append(cur_line)
                cur_line = []
                cur_x = padding + indent
            cur_line.append(("emote", fcontent, ew))
            cur_x += ew + gap
    
    if cur_line:
        lines.append(cur_line)
    
    return lines if lines else [[]]

# ============================================================
# 1. 解析 HTML 聊天记录
# ============================================================

# parse_chat_html lives in chat_parser.py (re-exported above for compatibility).
# ============================================================
# 2. 渲染 Overlay PNG 帧序列
# ============================================================

def normalize_text(t):
    """将不支持的 Unicode 字符（如数学字母符号）转为 ASCII。"""
    result = []
    for ch in t:
        cp = ord(ch)
        if 0x1D400 <= cp <= 0x1D7FF:  # Mathematical Alphanumeric Symbols
            try:
                name = unicodedata.name(ch, "")
                parts = name.split()
                if parts and parts[-1] in ("SMALL", "CAPITAL") and len(parts) >= 3:
                    letter = parts[-2].lower() if parts[-1] == "SMALL" else parts[-2].upper()
                    result.append(letter)
                else:
                    result.append(ch)
            except Exception:
                result.append(ch)
        elif cp >= 0x10000:
            decomp = unicodedata.normalize("NFKD", ch)
            if all(ord(c) < 0x10000 for c in decomp):
                result.append(decomp)
            else:
                result.append("?")
        else:
            result.append(ch)
    return "".join(result)


def hex_to_rgb(hex_color):
    """Compat wrapper: author colors fall back to white (shared implementation)."""
    return hex_to_rgb_soft(hex_color, default=(255, 255, 255))


# Layout defaults shared by line-count prepass and message bitmap render.
# Keep these in one place so schedule capacity and drawn height cannot drift.
MESSAGE_PAD = 5
MESSAGE_BADGE_SIZE = 9
MESSAGE_GAP = 3
MESSAGE_INDENT = 12


def compute_message_header_width(msg, *, padding, badge_size, gap, font, font_bold):
    """Width of badges + author + colon on the first line (before body fragments)."""
    badge_count = len(msg.get("badges") or [])
    badge_total_w = badge_count * (badge_size + gap) if badge_count else 0
    author = msg.get("author") or ""
    ab = font_bold.getbbox(author)
    author_w = ab[2] - ab[0]
    cb = font.getbbox(":")
    colon_w = cb[2] - cb[0]
    header_w = padding + badge_total_w + author_w + gap + colon_w + gap
    return {
        "header_w": header_w,
        "author_w": author_w,
        "colon_w": colon_w,
        "badge_count": badge_count,
        "badge_total_w": badge_total_w,
        "author": author,
    }


def build_message_frag_list(msg, *, text_width_fn, emote_width_fn, emote_available_fn):
    """Normalize message fragments into (type, content, width) for wrap/render.

    Text fragments drop the leading ": " TwitchDownloader often prepends.
    Missing emote images become ``[title]`` text placeholders so pure-emote
    rows still occupy width during layout.
    """
    frag_list = []
    for frag in msg.get("fragments") or []:
        if frag.get("type") == "text":
            t = frag.get("text") or ""
            if t.startswith(": "):
                t = t[2:]
            elif t == ":":
                continue
            t = normalize_text(t).strip()
            if not t:
                continue
            frag_list.append(("text", t, text_width_fn(t)))
        elif frag.get("type") == "emote":
            cls = frag.get("class", "")
            if emote_available_fn(cls):
                frag_list.append(("emote", cls, emote_width_fn(cls)))
            else:
                t = f'[{frag.get("title", "")}]'
                frag_list.append(("text", t, text_width_fn(t)))
    return frag_list


def truncate_wrapped_lines_with_ellipsis(
    lines,
    *,
    max_message_lines,
    max_w,
    padding,
    indent,
    gap,
    text_width_fn,
):
    """Cap wrapped lines and append '...' so truncation is visible (not silent crop)."""
    if not max_message_lines or len(lines) <= max_message_lines:
        return lines
    lines = lines[:max_message_lines]
    ellipsis = "..."
    ellipsis_w = text_width_fn(ellipsis)
    last_is_first_line = len(lines) == 1
    last_limit = (max_w - padding) if last_is_first_line else (max_w - padding - indent)
    while lines[-1] and sum(
        item[2] + (gap if item[0] == "emote" else 0) for item in lines[-1]
    ) + ellipsis_w > last_limit:
        kind, content, width = lines[-1][-1]
        if kind == "text" and len(content) > 1:
            content = content[:-1]
            lines[-1][-1] = (kind, content, text_width_fn(content))
        else:
            lines[-1].pop()
    lines[-1].append(("text", ellipsis, ellipsis_w))
    return lines


def layout_message_lines(
    msg,
    *,
    max_w,
    font,
    font_bold,
    text_width_fn,
    emote_width_fn,
    emote_available_fn,
    max_message_lines=0,
    truncate_with_ellipsis=False,
    padding=MESSAGE_PAD,
    badge_size=MESSAGE_BADGE_SIZE,
    gap=MESSAGE_GAP,
    indent=MESSAGE_INDENT,
):
    """Shared schedule/render layout: header metrics + wrapped fragment lines.

    When ``truncate_with_ellipsis`` is False (line-count prepass), only the
    returned ``num_lines`` is capped by ``max_message_lines``. When True
    (bitmap render), lines are actually truncated and get a visible ellipsis.
    """
    header = compute_message_header_width(
        msg, padding=padding, badge_size=badge_size, gap=gap, font=font, font_bold=font_bold
    )
    frag_list = build_message_frag_list(
        msg,
        text_width_fn=text_width_fn,
        emote_width_fn=emote_width_fn,
        emote_available_fn=emote_available_fn,
    )
    lines = wrap_fragments(
        frag_list, header["header_w"], max_w, padding, indent, gap, text_width_fn
    )
    if truncate_with_ellipsis:
        lines = truncate_wrapped_lines_with_ellipsis(
            lines,
            max_message_lines=max_message_lines,
            max_w=max_w,
            padding=padding,
            indent=indent,
            gap=gap,
            text_width_fn=text_width_fn,
        )
        if not lines:
            lines = [[]]
        num_lines = len(lines)
    else:
        num_lines = max(1, len(lines))
        if max_message_lines:
            num_lines = min(num_lines, max_message_lines)
    return lines, header, num_lines


def expected_overlay_frame_count(duration, fps):
    """How many overlay frames cover [0, duration) at the given fps.

    Samples at t = i/fps for i = 0..n-1 with t < duration, so
    n = ceil(duration * fps - eps). Must match compose_video's expected count
    (previously floor here vs ceil there broke fractional VOD durations).
    """
    if duration <= 0 or fps <= 0:
        return 1
    return max(1, int(math.ceil(float(duration) * float(fps) - 1e-9)))


def frame_index_range(start_t, end_t, fps, total_frames):
    """Map a half-open time range [start_t, end_t) onto global frame indexes."""
    if total_frames <= 0 or fps <= 0:
        return 0, 0
    start_i = int(math.ceil(float(start_t) * float(fps) - 1e-12))
    end_i = int(math.ceil(float(end_t) * float(fps) - 1e-12))
    start_i = max(0, min(total_frames, start_i))
    end_i = max(0, min(total_frames, end_i))
    if end_i < start_i:
        end_i = start_i
    return start_i, end_i


def probe_video_duration(video_path):
    """Read media duration via ffprobe. Returns float seconds or raises RuntimeError."""
    probe = subprocess.run(
        [require_executable("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video_path],
        capture_output=True,
        text=True,
    )
    raw = (probe.stdout or "").strip().splitlines()
    if probe.returncode != 0 or not raw:
        err = (probe.stderr or probe.stdout or "ffprobe failed").strip()[:400]
        raise RuntimeError(f"无法读取视频时长: {video_path}: {err}")
    try:
        duration = float(raw[0].strip() or 0.0)
    except ValueError as e:
        raise RuntimeError(f"无法解析视频时长 {raw[0]!r}: {e}") from e
    if duration <= 0:
        raise RuntimeError(f"视频时长无效 ({duration}): {video_path}")
    return duration



def probe_video_dimensions(video_path):
    """Read the first video stream dimensions via ffprobe, or return None."""
    probe = subprocess.run(
        [
            require_executable("ffprobe"), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json", video_path,
        ],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        return None
    try:
        stream = (json.loads(probe.stdout or "{}").get("streams") or [{}])[0]
        width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
        return (width, height) if width > 0 and height > 0 else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


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
    dimensions = probe_video_dimensions(video_path)
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
    dimensions = probe_video_dimensions(video_path)
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


def probe_video_fps(video_path):
    """Best-effort source video FPS via ffprobe. Returns float or None."""
    probe = subprocess.run(
        [
            require_executable("ffprobe"), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate,avg_frame_rate",
            "-of", "json", video_path,
        ],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        return None
    try:
        data = json.loads(probe.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
    except (json.JSONDecodeError, IndexError, TypeError):
        return None

    def _parse_rate(rate):
        if not rate or rate in ("0/0", "N/A"):
            return None
        try:
            if "/" in str(rate):
                num, den = str(rate).split("/", 1)
                den_f = float(den)
                if den_f <= 0:
                    return None
                return float(num) / den_f
            return float(rate)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    # Prefer r_frame_rate for constant sources, then avg.
    for key in ("r_frame_rate", "avg_frame_rate"):
        val = _parse_rate(stream.get(key))
        if val and 1.0 <= val <= 240.0:
            return val
    return None


def _quantize_fps(value: float) -> float:
    """Keep common NTSC rates exact; leave other floats; clamp to [1, 240]."""
    v = float(value)
    if v < 1.0:
        return 1.0
    if v > 240.0:
        return 240.0
    # Known broadcast rates (within 0.02 of nominal).
    known = (
        24000 / 1001,  # ~23.976
        24.0,
        25.0,
        30000 / 1001,  # ~29.970
        30.0,
        50.0,
        60000 / 1001,  # ~59.940
        60.0,
        120.0,
    )
    for k in known:
        if abs(v - k) < 0.02:
            return k
    # Near-integer CFR
    if abs(v - round(v)) < 1e-3:
        return float(int(round(v)))
    return v


def fps_to_ffmpeg_rate(fps) -> str:
    """Format fps for ffmpeg -r / -framerate (prefer exact NTSC rationals)."""
    v = _quantize_fps(float(fps))
    rationals = {
        24000 / 1001: "24000/1001",
        30000 / 1001: "30000/1001",
        60000 / 1001: "60000/1001",
    }
    for k, s in rationals.items():
        if abs(v - k) < 1e-6:
            return s
    if abs(v - round(v)) < 1e-6:
        return str(int(round(v)))
    return f"{v:.6f}".rstrip("0").rstrip(".")


def resolve_output_fps(video_path, explicit=None, fallback=30):
    """Resolve final encode FPS: explicit > source probe > fallback.

    Returns a float (may be fractional, e.g. 30000/1001). Use fps_to_ffmpeg_rate()
    when passing to ffmpeg -r so NTSC sources are not rounded to 30.
    """
    if explicit is not None:
        return _quantize_fps(float(explicit))
    probed = probe_video_fps(video_path)
    if probed is not None:
        return _quantize_fps(probed)
    return _quantize_fps(fallback)


def line_height_px(font_size: int) -> int:
    """Single-line pitch used by capacity math and render_overlay (font_size + 14)."""
    return max(1, int(font_size) + 14)


def compute_lane_capacity(height: int, font_size: int, *, bottom_pad: int = 4) -> int:
    """How many single-line lanes fit in the overlay box.

    max_visible=0 / auto uses this so the box fills by height and type size.
    """
    line_h = line_height_px(font_size)
    usable = max(1, int(height) - int(bottom_pad))
    return max(1, usable // line_h)


def resolve_lane_budget(
    max_visible: int,
    height: int,
    font_size: int,
    *,
    bottom_pad: int = 4,
) -> tuple[int, int, str | None]:
    """Resolve the effective lane/line budget for the current overlay box.

    Returns ``(budget, capacity, warning_or_none)``.

    - ``max_visible <= 0``: auto-fill by box height / font size (``budget == capacity``).
    - ``max_visible > capacity``: clamp to capacity. Without this, high lanes paste at
      ``y=0`` and stack on top of each other (silent-wrong on short/ratio layouts).
    """
    capacity = compute_lane_capacity(height, font_size, bottom_pad=bottom_pad)
    raw = int(max_visible or 0)
    if raw <= 0:
        return capacity, capacity, None
    if raw > capacity:
        line_h = line_height_px(font_size)
        warn = (
            f"max_visible={raw} 超过当前框高可容纳的 {capacity} 行 "
            f"(height={int(height)}px, font={int(font_size)}px, LINE_H={line_h})，"
            f"已钳制为 {capacity}，避免弹幕叠在顶部"
        )
        return capacity, capacity, warn
    return raw, capacity, None


def layout_bounds_warnings(config, video_path) -> list[str]:
    """Warn when the chat box is mostly/fully outside the source frame.

    Default pixel layout targets ~1080p; on 360p/720p sources the box often sits
    below the frame so the burn looks like "no chat". Prefer
    ``adapt_absolute_layout_to_source`` first so run.bat defaults auto-scale;
    this remains a safety net for custom absolute crops that still overflow.
    """
    dimensions = probe_video_dimensions(video_path)
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
        """Truncate schedule rows overlapping [base_lane, base_lane+need_nl) at t.

        Multi-line parents are one schedule row but occupy nl consecutive lanes;
        free *all* of those sublanes when any overlap is seized.

        Two-pass: check min_visible protection for every overlapping row first.
        Mutating mid-scan then returning False left earlier rows truncated while
        the new message was dropped (silent schedule corruption).
        """
        seize_lo = base_lane
        seize_hi = base_lane + need_nl
        victims: list[int] = []
        for si, s in enumerate(msg_schedule):
            s_start, s_end, s_lane, s_idx, s_nl = s
            # Active at or through t (include same-timestamp occupants).
            if not (s_start <= t < s_end):
                continue
            row_lo = s_lane
            row_hi = s_lane + max(1, int(s_nl))
            if row_hi <= seize_lo or row_lo >= seize_hi:
                continue
            # A protected message wins over a newly arriving one. The caller
            # drops the new message instead of cutting an on-screen message short.
            if t - s_start < min_visible:
                return False
            victims.append(si)
        for si in victims:
            s_start, s_end, s_lane, s_idx, s_nl = msg_schedule[si]
            msg_schedule[si] = (s_start, t, s_lane, s_idx, s_nl)
            for sub in range(max(1, int(s_nl))):
                lane_ends[s_lane + sub] = t
        return True

    dropped_past_duration = 0
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
                for sub in range(nl):
                    lane_ends[lane + sub] = end
                msg_schedule.append((t, end, lane, i, nl))
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
                continue
            for sub in range(nl):
                lane_ends[best_lane + sub] = end
            msg_schedule.append((t, end, best_lane, i, nl))
            last_admitted_at = t

    if dropped_past_duration:
        print(
            f"  [WARN] lanes 调度: {dropped_past_duration} 条因 arrival_interval 延后超出 "
            f"时长 {float(duration):.2f}s 未上屏",
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
    return out


class _FloatEventList(list):
    """Schedule list with optional .starts cache for active_float_stack."""

    starts: list[float]


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

    # Prefer chronological order; schedule_messages_float returns sorted events.
    # Only re-sort when a caller hands unsorted history.
    needs_sort = False
    for i in range(1, len(events)):
        if (events[i][0], events[i][3]) < (events[i - 1][0], events[i - 1][3]):
            needs_sort = True
            break
    ordered = (
        sorted(events, key=lambda e: (e[0], e[3]))
        if needs_sort
        else events
    )

    # Candidates with start <= current_t (float ends are far future / open).
    # Prefer precomputed starts from schedule_messages_float (full-render hot path).
    starts = getattr(events, "starts", None)
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


def _normalize_import_identity_text(text):
    """Collapse whitespace for import identity comparisons."""
    text = str(text or "").replace("\r", " ").replace("\n", " ")
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def message_export_original(message):
    """Rebuild the export-time original text for one chat message."""
    parts = []
    for frag in message.get("fragments") or []:
        if frag.get("type") == "text":
            parts.append(str(frag.get("text", "") or ""))
        else:
            parts.append(f'[{frag.get("title", "emote")}]')
    original_text = " ".join(parts)
    if original_text.startswith(": "):
        original_text = original_text[2:]
    return original_text


def _message_stream_timestamp(message: dict) -> float:
    """Stream-absolute timestamp for export/import identity (pre-offset when available)."""
    if message.get("stream_timestamp") is not None:
        try:
            return float(message["stream_timestamp"])
        except (TypeError, ValueError):
            pass
    try:
        return float(message.get("timestamp", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def translation_json_nonempty_count(path: str | Path) -> int:
    """How many rows already have a non-empty translation field (0 if missing/unreadable)."""
    p = Path(path)
    if not p.is_file():
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return 0
    items = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return 0
    n = 0
    for item in items:
        if isinstance(item, dict) and str(item.get("translation", "") or "").strip():
            n += 1
    return n


def build_export_translation_payload(
    chat_data: dict,
    *,
    offset_info: dict | None = None,
) -> dict:
    """Build export JSON using stream-absolute timestamps for stable identity."""
    offset_info = offset_info or {}
    try:
        applied_offset = float(offset_info.get("offset") or 0.0)
    except (TypeError, ValueError):
        applied_offset = 0.0
    items = []
    for i, m in enumerate(chat_data.get("messages") or []):
        original_text = message_export_original(m)
        stream_ts = _message_stream_timestamp(m)
        items.append({
            "index": i,
            # Stream-absolute time (broadcast timeline). Import matches this field
            # so changing --offset between export and burn does not mass-skip rows.
            "timestamp": round(stream_ts, 1),
            "stream_timestamp": round(stream_ts, 1),
            "author": m.get("author"),
            "original": original_text,
            "translation": "",
        })
    return {
        "schema_version": 2,
        "time_base": "stream",
        "export_offset": applied_offset,
        "offset_mode": offset_info.get("mode"),
        "messages": items,
    }


def write_export_translation_json(
    export_path: str | Path,
    chat_data: dict,
    *,
    offset_info: dict | None = None,
    force: bool = False,
) -> dict:
    """Write translation export JSON. Refuses to wipe non-empty translations unless force."""
    export_path = Path(export_path)
    existing_n = translation_json_nonempty_count(export_path)
    if existing_n > 0 and not force:
        raise FileExistsError(
            f"翻译 JSON 已有 {existing_n} 条非空 translation，拒绝覆盖以免丢失译文: {export_path}\n"
            f"  复用请加 --reuse-translation；确需重新导出请加 --force-export"
        )
    payload = build_export_translation_payload(chat_data, offset_info=offset_info)
    export_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = export_path.with_suffix(export_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, export_path)
    return payload


def apply_imported_translations(chat_data, trans_data, strict=False):
    """
    Apply translation JSON onto parsed chat messages by stable export index.

    Export uses list position at export time as index. Import matches that
    index field (not a re-enumerate of a possibly reordered list), and when
    available cross-checks author/timestamp/original to catch silent mismatch.

    Timestamp identity uses stream-absolute time when available (export schema
    v2 / stream_timestamp), so re-burning with a different --offset does not
    mass-skip translations.

    On identity mismatch: skip applying that row by default; with strict=True
    raise ValueError after collecting mismatches.
    Returns (replaced, stripped_placeholders, warnings).
    """
    messages = chat_data.get("messages") or []
    items = trans_data.get("messages") if isinstance(trans_data, dict) else None
    if not isinstance(items, list):
        raise ValueError("翻译 JSON 缺少 messages 数组")

    trans_map = {}
    dup_indexes: list[int] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item["index"])
        except (KeyError, TypeError, ValueError):
            continue
        if idx in trans_map:
            dup_indexes.append(idx)
        trans_map[idx] = item

    warnings = []
    if dup_indexes:
        uniq = sorted(set(dup_indexes))
        preview = uniq[:20]
        more = "" if len(uniq) <= 20 else f" ... (+{len(uniq) - 20} more)"
        warnings.append(
            f"翻译 JSON 含重复 index（后写覆盖先写）: {preview}{more}"
        )
    if len(messages) != len(trans_map) and len(trans_map) > 0:
        warnings.append(
            f"翻译条数 ({len(trans_map)}) 与当前解析消息数 ({len(messages)}) 不一致；"
            f"将按 index 对齐，可能有漏贴/错贴风险"
        )

    replaced = 0
    stripped_placeholders = 0
    mismatch_count = 0
    for i, m in enumerate(messages):
        item = trans_map.get(i)
        if not item:
            continue
        raw_tr = str(item.get("translation", "") or "").strip()
        if not raw_tr:
            continue

        # Identity checks: author / stream timestamp / original (normalized whitespace).
        mismatch_reasons = []
        exp_author = item.get("author")
        if exp_author is not None and str(exp_author) != str(m.get("author", "")):
            mismatch_reasons.append(
                f"作者不一致: 翻译 JSON={exp_author!r} HTML={m.get('author')!r}"
            )
        # Prefer stream-absolute times (schema v2) so offset changes do not break identity.
        # Legacy exports stored post-offset video-relative timestamps only.
        exp_stream = item.get("stream_timestamp")
        exp_ts = exp_stream if exp_stream is not None else item.get("timestamp")
        if exp_ts is not None:
            time_base = ""
            if isinstance(trans_data, dict):
                time_base = str(trans_data.get("time_base") or "").strip().lower()
            use_stream = time_base == "stream" or exp_stream is not None
            if use_stream:
                html_ts = _message_stream_timestamp(m)
            else:
                try:
                    html_ts = float(m.get("timestamp", 0) or 0)
                except (TypeError, ValueError):
                    html_ts = 0.0
            try:
                if abs(float(exp_ts) - float(html_ts)) > 0.51:
                    label = "stream" if use_stream else "video-relative"
                    mismatch_reasons.append(
                        f"时间戳不一致({label}): 翻译 JSON={exp_ts} HTML={html_ts}"
                    )
            except (TypeError, ValueError):
                mismatch_reasons.append(
                    f"时间戳无法解析: 翻译 JSON={exp_ts!r}"
                )
        exp_original = item.get("original")
        if exp_original is not None:
            html_original = message_export_original(m)
            if _normalize_import_identity_text(exp_original) != _normalize_import_identity_text(
                html_original
            ):
                mismatch_reasons.append(
                    f"original 不一致: 翻译 JSON={exp_original!r} HTML={html_original!r}"
                )

        if mismatch_reasons:
            mismatch_count += 1
            for reason in mismatch_reasons:
                warnings.append(f"index={i} {reason}")
            warnings.append(
                f"index={i} 跳过导入（身份不一致，避免错贴译文）"
            )
            continue

        translation = clean_imported_translation(raw_tr, m.get("author"))
        emote_titles = [
            str(f.get("title", "")).strip()
            for f in m.get("fragments") or []
            if f.get("type") == "emote" and str(f.get("title", "")).strip()
        ]
        for title in set(emote_titles):
            placeholder = f"[{title}]"
            count = translation.count(placeholder)
            if count:
                translation = translation.replace(placeholder, "")
                stripped_placeholders += count
        translation = re.sub(r"[ \t]{2,}", " ", translation).strip()

        emote_frags = [f for f in (m.get("fragments") or []) if f.get("type") == "emote"]
        text_frags = [f for f in (m.get("fragments") or []) if f.get("type") == "text"]

        if not translation and emote_frags:
            # Pure-emote after placeholder strip: keep image fragments only.
            m["fragments"] = list(emote_frags)
            replaced += 1
        elif not emote_frags:
            # Text-only: single translated text fragment (merge multi-text).
            m["fragments"] = [{"type": "text", "text": translation}]
            replaced += 1
        else:
            # Mixed text+emote: put full translation as one leading text block,
            # then original emote fragments in order. Avoids stuffing only the
            # first text slot and leaving trailing empty texts mid-layout.
            m["fragments"] = [{"type": "text", "text": translation}] + list(emote_frags)
            replaced += 1
            if len(text_frags) > 1:
                # Informational only; layout is intentionally simplified.
                pass

    missing_idx = [i for i in range(len(messages)) if i not in trans_map]
    if missing_idx and len(missing_idx) <= 20:
        warnings.append(f"以下 index 在翻译 JSON 中缺失: {missing_idx[:20]}")
    elif missing_idx:
        warnings.append(f"{len(missing_idx)} 个 index 在翻译 JSON 中缺失")

    if mismatch_count:
        warnings.append(f"身份不一致跳过 {mismatch_count} 条翻译")
        if strict:
            raise ValueError(
                f"严格导入失败: {mismatch_count} 条翻译与 HTML 身份不一致"
                f"（作者/时间戳/原文），已拒绝错贴译文"
            )

    return replaced, stripped_placeholders, warnings


def render_overlay(chat_data, out_dir, video_path, config):
    """渲染聊天覆盖层为 PNG 帧序列。"""
    from PIL import Image, ImageDraw, ImageFont

    print("[2/4] 渲染 overlay 帧序列...", flush=True)

    messages = chat_data["messages"]
    emote_map = chat_data.get("emote_map", {})

    # GIF / animated WebP 不能直接 convert，否则只会取第一帧。
    # 预解码后按消息显示时间选择动画帧。
    emote_imgs = {}
    for cls, path in emote_map.items():
        try:
            source = Image.open(path)
            frames = []
            durations = []
            for frame_index in range(getattr(source, "n_frames", 1)):
                source.seek(frame_index)
                img = source.convert("RGBA")
                if img.height != config.emote_h:
                    scale = config.emote_h / img.height
                    img = img.resize((max(1, int(img.width * scale)), config.emote_h), Image.LANCZOS)
                frames.append(img)
                durations.append(max(10, int(source.info.get("duration", 100))))
            emote_imgs[cls] = {"frames": frames, "durations": durations, "cycle_ms": sum(durations), "width": frames[0].width}
            state = f"动画 {len(frames)} 帧" if len(frames) > 1 else "静态"
            print(f"  emote: {cls} ({frames[0].width}x{frames[0].height}, {state})", flush=True)
        except Exception as e:
            print(f"  emote 加载失败 {cls}: {e}", flush=True)

    def emote_image(cls, message_age=0.0):
        emote = emote_imgs.get(cls)
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

    def emote_width(cls):
        return emote_imgs[cls]["width"]

    # 字体
    try:
        font = ImageFont.truetype(config.font_path, config.font_size)
        font_bold = ImageFont.truetype(config.font_bold_path or config.font_path, config.font_size)
    except OSError as e:
        raise RuntimeError(
            f"无法加载字体: regular={config.font_path!r} bold={config.font_bold_path!r}: {e}. "
            "请安装 CJK 字体或用 --font-path 指定。"
        ) from e

    # Badge 颜色
    BADGE_COLORS = {
        "broadcaster": (255, 50, 50),
        "moderator": (0, 160, 0),
        "vip": (213, 0, 213),
        "subscriber": (100, 100, 255),
        "premium": (0, 169, 255),
        "verified": (0, 169, 255),
    }

    LINE_H = line_height_px(config.font_size)
    # Layout constants shared by line-count prepass and bitmap render.
    padding = MESSAGE_PAD
    badge_size = MESSAGE_BADGE_SIZE
    gap = MESSAGE_GAP
    indent = MESSAGE_INDENT

    # --- 预计算每条消息的行数（用于 lane 分配）---
    MAX_W = config.width - 4
    def text_width(s):
        bb = font.getbbox(s)
        return bb[2] - bb[0]

    max_message_lines = max(0, int(getattr(config, "max_message_lines", 0) or 0))

    def calc_msg_lines(msg):
        """计算消息需要多少行（与 render_message 共用 layout_message_lines）。"""
        _lines, _header, num_lines = layout_message_lines(
            msg,
            max_w=MAX_W,
            font=font,
            font_bold=font_bold,
            text_width_fn=text_width,
            emote_width_fn=emote_width,
            emote_available_fn=lambda cls: cls in emote_imgs,
            max_message_lines=max_message_lines,
            truncate_with_ellipsis=False,
            padding=padding,
            badge_size=badge_size,
            gap=gap,
            indent=indent,
        )
        return num_lines

    msg_line_count = {}
    for i, m in enumerate(messages):
        msg_line_count[i] = calc_msg_lines(m)

    # --- 分配 lane ---
    MSG_LIFETIME = config.msg_lifetime
    raw_max_visible = int(getattr(config, "max_visible", 0) or 0)
    auto_capacity = compute_lane_capacity(config.height, config.font_size)
    stack_mode = str(getattr(config, "stack_mode", "lanes") or "lanes").strip().lower()
    if stack_mode not in ("float", "lanes"):
        stack_mode = "lanes"
    MAX_VISIBLE, auto_capacity, budget_warn = resolve_lane_budget(
        raw_max_visible,
        config.height,
        config.font_size,
    )
    if raw_max_visible <= 0:
        print(
            f"  max_visible=auto → {MAX_VISIBLE} lanes "
            f"(height={config.height}px, font={config.font_size}px, LINE_H={line_height_px(config.font_size)})",
            flush=True,
        )
    elif budget_warn:
        print(f"  [WARN] {budget_warn}", flush=True)
    print(f"  stack_mode={stack_mode}", flush=True)

    # 获取视频时长（带防护，避免 float('') 直接崩溃）
    duration = probe_video_duration(video_path)
    print(f"  视频时长: {duration:.1f}s", flush=True)
    # preview_clip may start mid-video (densest window). Chat timestamps are rebased
    # to 0 in main() when clip_start > 0; compose seeks the source with -ss.
    # Here we only shorten the render duration to the clip length.
    if getattr(config, "preview_clip", None):
        clip_len = float(config.preview_clip)
        duration = min(duration, clip_len)
        clip_start = float(getattr(config, "preview_clip_start", 0.0) or 0.0)
        if clip_start > 1e-6:
            print(
                f"  预览短片模式: 源窗口 [{clip_start:.1f}s, {clip_start + clip_len:.1f}s] "
                f"(聊天已 rebase→0，渲染时长 {duration:.1f}s)",
                flush=True,
            )
        else:
            print(f"  预览短片模式: 仅渲染前 {duration:.1f}s", flush=True)

    if stack_mode == "float":
        # Absolute window origin for arrival throttle: rebased dense clips use 0;
        # float --preview-frame keeps absolute timestamps so throttle from frame t.
        clip_start_abs = float(getattr(config, "preview_clip_start", 0.0) or 0.0)
        preview_frame_abs = getattr(config, "preview_frame", None)
        if clip_start_abs > 1e-6:
            float_throttle_from = 0.0
        elif preview_frame_abs is not None:
            float_throttle_from = max(0.0, float(preview_frame_abs))
        else:
            float_throttle_from = 0.0
        msg_schedule = schedule_messages_float(
            messages,
            msg_line_count,
            duration=duration,
            capacity_lines=MAX_VISIBLE,
            arrival_interval=getattr(config, "arrival_interval", 0.0),
            throttle_from=float_throttle_from,
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

    from collections import Counter
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

    # --- 渲染单条消息为图片（支持自动换行）---
    def render_message(msg, message_age=0.0):
        """渲染单条消息，超宽时自动换行。返回 (image, num_lines)。"""
        lines, header, num_lines = layout_message_lines(
            msg,
            max_w=MAX_W,
            font=font,
            font_bold=font_bold,
            text_width_fn=text_width,
            emote_width_fn=emote_width,
            emote_available_fn=lambda cls: cls in emote_imgs,
            max_message_lines=max_message_lines,
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
            bc = BADGE_COLORS.get(title.split("-")[0], (85, 85, 85))
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
                eimg = emote_image(fi[1], message_age)
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
                    eimg = emote_image(fi[1], message_age)
                    if eimg:
                        ey = y + (LINE_H - eimg.height) // 2
                        img.paste(eimg, (x, ey), eimg)
                        x += fi[2] + gap

        return img, num_lines

    animated_message_ids = {
        i for i, message in enumerate(messages)
        if any(
            fragment.get("type") == "emote"
            and len(emote_imgs.get(fragment.get("class", ""), {}).get("frames", [])) > 1
            for fragment in message["fragments"]
        )
    }
    if animated_message_ids:
        print(f"  动画表情: {len(animated_message_ids)} 条消息将逐帧更新", flush=True)

    # Message bitmap cache:
    # - default: pre-render all static message images (predictable, existing behavior)
    # - --lazy-message-images: only render when a message becomes visible; LRU cap for long VODs
    from collections import OrderedDict

    lazy_images = bool(getattr(config, "lazy_message_images", False))
    cache_cap = max(8, int(getattr(config, "message_image_cache_size", 256) or 256))
    msg_images = OrderedDict()  # idx -> Image
    msg_lines = {}  # msg_index -> num_lines

    def message_image(idx, message_age=0.0, force_dynamic=False):
        """Return (img, nl) for message idx; animated/dynamic always re-renders."""
        if force_dynamic or idx in animated_message_ids:
            img, nl = render_message(messages[idx], message_age)
            msg_lines[idx] = nl
            return img, nl
        if idx in msg_images:
            msg_images.move_to_end(idx)
            return msg_images[idx], msg_lines.get(idx, 1)
        img, nl = render_message(messages[idx], 0.0)
        msg_lines[idx] = nl
        msg_images[idx] = img
        if lazy_images:
            while len(msg_images) > cache_cap:
                msg_images.popitem(last=False)
        return img, nl

    if lazy_images:
        print(
            f"  消息图: lazy 模式 (cache_size={cache_cap}, messages={len(messages)})",
            flush=True,
        )
    else:
        for i in range(len(messages)):
            img, nl = message_image(i)
        print(f"  渲染 {len(msg_images)} 条消息图片", flush=True)
    # --- 生成帧序列 ---
    frames_dir = os.path.join(out_dir, "overlay_frames")
    os.makedirs(frames_dir, exist_ok=True)
    # 清除旧帧
    for old in os.listdir(frames_dir):
        if old.endswith(".png"):
            os.remove(os.path.join(frames_dir, old))

    FPS = config.fps
    W, H = config.width, config.height
    BG_ALPHA = config.bg_alpha

    # 找 change points
    preview_frame_time = getattr(config, "preview_frame", None)
    if preview_frame_time is not None:
        preview_t = max(0.0, min(float(preview_frame_time), duration))
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
    total_frames = 1 if preview_frame_time is not None else expected_overlay_frame_count(duration, FPS)
    frame_num = 0
    render_start_time = time.time()
    last_progress_time = render_start_time
    reuse_static = bool(getattr(config, "reuse_static_frames", True))
    skip_blank = bool(getattr(config, "skip_blank_frames", True))
    blank_hold_seconds = float(getattr(config, "blank_hold_seconds", 0.5) or 0.5)
    blank_stride = max(1, int(round(blank_hold_seconds * FPS)))
    stats = {
        "written": 0,
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

    for cp_idx in range(len(change_points)):
        cp = change_points[cp_idx]
        next_cp = change_points[cp_idx + 1] if cp_idx + 1 < len(change_points) else duration

        if stack_mode == "float":
            # Bottom-up Twitch stack: recompute lanes from currently active messages.
            visible = active_float_stack(msg_schedule, cp, MAX_VISIBLE)
        else:
            visible = []
            for start, end, lane, idx, nl_sch in msg_schedule:
                if start <= cp < end:
                    visible.append((lane, idx, start, end, nl_sch))
            visible.sort(key=lambda v: v[0])

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
        # IMPORTANT: fade-in (first 0.3s) / fade-out (last 0.5s) make alpha time-dependent.
        # Even with change_points at start/end, the *boundary segment that still contains*
        # the fade window must NOT be static-reused, or every hardlinked frame freezes
        # the first (or last) alpha sample.
        FADE_IN = 0.3
        FADE_OUT = 0.5
        segment_has_anim = any(idx in animated_message_ids for _lane, idx, _s, _e, _nl in visible)
        segment_has_fade = any(
            (cp < (start + FADE_IN) and next_cp > start)
            or (cp < end and next_cp > (end - FADE_OUT))
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
                for lane, idx, start, end, nl_vis in visible:
                    if idx in animated_message_ids:
                        msg_img, nl = message_image(idx, current_t - start, force_dynamic=True)
                    else:
                        msg_img, nl = message_image(idx)
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
                        if age < 0.3:
                            alpha = int(255 * min(1.0, max(0.0, age / 0.3)))
                        elif remaining < 0.5:
                            alpha = int(255 * max(0.0, remaining / 0.5))

                        if alpha < 255:
                            msg_img = msg_img.copy()
                            r, g, b, a = msg_img.split()
                            # Bind alpha as default so the lambda does not close over the loop var.
                            a = a.point(lambda v, alpha=alpha: int(v * alpha / 255))
                            msg_img = Image.merge("RGBA", (r, g, b, a))

                        frame.paste(msg_img, (2, y), msg_img)

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
            pct = (frame_num / total_frames * 100) if total_frames > 0 else 100
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
                f"{pct:.0f}% write={stats['written']} reuse={stats['reused_static']}{eta_str}",
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
        assert_contiguous_frame_sequence(
            frames_dir,
            total_frames,
            start=0,
            context="render_overlay",
        )
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
    config.frame_stats = stats
    config.stage_timings = dict(getattr(config, "stage_timings", {}) or {})
    config.stage_timings["render_frames"] = elapsed_total
    print(
        f"  完成: {frame_num} 帧, 用时 {int(elapsed_total//60)}m{int(elapsed_total%60)}s "
        f"(write={stats['written']}, hardlink={stats['hardlink']}, copy={stats['copy']}, "
        f"static_reuse={stats['reused_static']}, blank_sparse={stats['blank_sparse']}, filled={stats['filled']})",
        flush=True,
    )
    if preview_frame_time is not None:
        default_name = f"{Path(video_path).stem}_preview_{preview_t:.1f}s.png".replace(".0s", "s")
        requested_preview = getattr(config, "preview_image", None)
        # Always write under out_dir first (safe job/temp location).
        if requested_preview:
            safe_name = os.path.basename(str(requested_preview)) or default_name
        else:
            safe_name = default_name
        preview_path = os.path.join(out_dir, safe_name)
        bg_path = os.path.join(out_dir, "preview_video_frame.png")
        # Accurate single-frame extract (decode then seek) for preview alignment.
        r = subprocess.run([
            require_executable("ffmpeg"), "-y", "-i", video_path, "-ss", str(preview_t),
            "-frames:v", "1", bg_path,
        ], capture_output=True, text=True)
        if r.returncode == 0 and os.path.isfile(bg_path):
            bg = Image.open(bg_path).convert("RGBA")
            overlay = Image.open(os.path.join(frames_dir, "frame_00000.png")).convert("RGBA")
            bg.paste(overlay, (config.x, config.y), overlay)
            bg.save(preview_path)
            try:
                os.remove(bg_path)
            except OSError:
                pass
        else:
            print(f"  警告: 无法抽取视频帧，改为输出 overlay 透明图: {r.stderr[-300:]}", flush=True)
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
        # Stash actual path so main() can promote/report the right file.
        config.preview_image = preview_path
        print(f"  预览图: {preview_path}", flush=True)
    return frames_dir, duration


# ============================================================
# 3. 合成视频
# ============================================================

def detect_frame_start_number(frames_dir):
    """Return the first numeric frame id in frame_%05d.png sequences."""
    numbers = []
    for name in os.listdir(frames_dir):
        m = re.fullmatch(r"frame_(\d+)\.png", name)
        if m:
            numbers.append(int(m.group(1)))
    return min(numbers) if numbers else 0


def get_stream_start_time(video_path, stream_selector):
    """读取流起始时间；缺失/异常时回退 0。"""
    probe = subprocess.run(
        [
            require_executable("ffprobe"), "-v", "error", "-select_streams", stream_selector,
            "-show_entries", "stream=start_time", "-of", "csv=p=0", video_path,
        ],
        capture_output=True, text=True,
    )
    try:
        raw = (probe.stdout or "").strip().splitlines()
        if not raw:
            return 0.0
        return float(raw[0] or 0)
    except ValueError:
        return 0.0


def probe_media_summary(path):
    """Return basic stream/duration info for publish validation."""
    summary = {
        "ok": False,
        "duration": 0.0,
        "has_video": False,
        "has_audio": False,
        "width": 0,
        "height": 0,
        "error": "",
    }
    probe = subprocess.run(
        [
            require_executable("ffprobe"), "-v", "error",
            "-show_entries", "format=duration:stream=index,codec_type,width,height",
            "-of", "json", path,
        ],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        summary["error"] = (probe.stderr or probe.stdout or "ffprobe failed").strip()[:400]
        return summary
    try:
        data = json.loads(probe.stdout or "{}")
    except json.JSONDecodeError as e:
        summary["error"] = f"ffprobe json parse failed: {e}"
        return summary

    try:
        summary["duration"] = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        summary["duration"] = 0.0

    for stream in data.get("streams") or []:
        codec_type = stream.get("codec_type")
        if codec_type == "video":
            summary["has_video"] = True
            try:
                summary["width"] = int(stream.get("width") or 0)
                summary["height"] = int(stream.get("height") or 0)
            except (TypeError, ValueError):
                pass
        elif codec_type == "audio":
            summary["has_audio"] = True

    summary["ok"] = summary["duration"] > 0 and summary["has_video"]
    if not summary["ok"] and not summary["error"]:
        summary["error"] = "missing video stream or non-positive duration"
    return summary


def resolve_source_av_timing(video_path, source_has_audio=None):
    """Probe container/audio/video timing used by compose + validation.

    Returns dict:
      source_duration, video_start, audio_start, video_lead_in, has_audio
    """
    source_summary = probe_media_summary(video_path)
    has_audio = (
        bool(source_has_audio)
        if source_has_audio is not None
        else bool(source_summary.get("has_audio"))
    )
    video_start = get_stream_start_time(video_path, "v:0")
    audio_start = get_stream_start_time(video_path, "a:0") if has_audio else 0.0
    video_lead_in = max(0.0, float(video_start) - float(audio_start)) if has_audio else 0.0
    source_duration = float(source_summary.get("duration") or 0.0)
    return {
        "source_duration": source_duration,
        "video_start": float(video_start or 0.0),
        "audio_start": float(audio_start or 0.0),
        "video_lead_in": float(video_lead_in or 0.0),
        "has_audio": has_audio,
        "summary": source_summary,
    }


def expected_compose_duration(render_duration, video_lead_in=0.0):
    """Target container duration for compose_video.

    `video_lead_in` only rewrites timestamps so both streams start at 0 and the
    first video frame freezes for editors. It does **not** mean we must publish
    a file longer than the source container / render window. Using
    render_duration + lead_in here previously false-failed complete encodes
    (~source length) as "too short".
    """
    return max(0.0, float(render_duration or 0.0))


def _default_max_extra_seconds(expected_duration):
    """Tight upper allowance: max(0.5, 0.5% of expected), capped near 0.75s."""
    expected = max(0.0, float(expected_duration or 0.0))
    return min(0.75, max(0.5, expected * 0.005))


def validate_rendered_output(
    path,
    expected_duration,
    require_audio=False,
    duration_tolerance=0.35,
    max_extra_seconds=None,
    min_width=2,
    min_height=2,
    min_duration=None,
):
    """Validate a partial/final MP4 before publishing the user-facing name.

    Checks:
    - readable media with video stream and positive duration
    - optional audio presence
    - not too short vs expected (and optional absolute min_duration floor)
    - not suspiciously long vs expected (catches wrong -t / filter mistakes)
    - video dimensions present (catches empty/corrupt encodes that still open)

    When source video is delayed vs audio, compose freezes the first frame and
    rewrites start times to 0. The published duration should still be about the
    render/source length — pass that as expected_duration, not source+lead_in.
    Optional min_duration rejects outputs that lost more than a lead-in worth of
    content (e.g. truncated tails).

    Floor semantics: if both expected and min_duration are set, the short-check
    uses min_duration as an independent lower bound (not max(min, expected),
    which previously made min_duration dead when expected was also set).
    When only expected is set, expected is the short floor.
    """
    summary = probe_media_summary(path)
    if not summary["ok"]:
        return False, summary, summary.get("error") or "output validation failed"
    if require_audio and not summary["has_audio"]:
        return False, summary, "expected audio stream is missing"

    actual = float(summary.get("duration") or 0.0)
    expected = float(expected_duration or 0.0)
    tol = float(duration_tolerance)
    if max_extra_seconds is None:
        max_extra_seconds = _default_max_extra_seconds(expected)
    else:
        max_extra_seconds = float(max_extra_seconds)

    # Short-check floors (independent):
    # - expected: primary target length
    # - min_duration: optional absolute floor that can be *below* expected
    #   (e.g. allow losing at most lead-in) without being raised to expected.
    if expected > 0 and actual + tol < expected:
        return (
            False,
            summary,
            f"output duration {actual:.3f}s is shorter than expected {expected:.3f}s",
        )
    if min_duration is not None and float(min_duration) > 0:
        floor = float(min_duration)
        if actual + tol < floor:
            return (
                False,
                summary,
                f"output duration {actual:.3f}s is shorter than min_duration {floor:.3f}s",
            )

    if expected > 0 and actual > expected + max(tol, float(max_extra_seconds)):
        return (
            False,
            summary,
            (
                f"output duration {actual:.3f}s is longer than expected "
                f"{expected:.3f}s (+{max_extra_seconds}s allowance)"
            ),
        )
    w = int(summary.get("width") or 0)
    h = int(summary.get("height") or 0)
    if w < int(min_width) or h < int(min_height):
        return False, summary, f"video dimensions too small: {w}x{h}"
    return True, summary, ""


def compose_video(video_path, frames_dir, out_dir, config, duration):
    """PNG 帧序列 → (可选 WebM alpha) → 叠加到源视频。"""
    # Fail fast on incomplete frame sequences before encode setup / ffmpeg publish.
    start_number = detect_frame_start_number(frames_dir)
    fps = max(1, int(getattr(config, "fps", 30) or 30))
    expected_frames = max(1, int(math.ceil(float(duration or 0.0) * fps - 1e-9)))
    missing = missing_frame_indexes(frames_dir, expected_frames, start=start_number)
    if missing:
        preview = ", ".join(f"frame_{i:05d}.png" for i in missing[:12])
        more = "" if len(missing) <= 12 else f" ... (+{len(missing) - 12} more)"
        raise RuntimeError(
            f"compose_video: missing {len(missing)} overlay frame(s) for "
            f"start={start_number} count={expected_frames} under {frames_dir}; "
            f"first gaps: {preview}{more}. Refuse to publish incomplete overlay."
        )

    encode = getattr(config, "encode", None)
    if encode is None:
        encode = resolve_encode_options()
        config.encode = encode

    print("[3/4] 合成 overlay 视频...", flush=True)
    print(f"  编码参数: {summarize_encode_options(encode)}", flush=True)
    for note in encode.notes:
        print(f"  [encode] {note}", flush=True)

    stage_timings = dict(getattr(config, "stage_timings", None) or {})
    frames_pattern = os.path.join(frames_dir, "frame_%05d.png")

    # Overlay path for filter input: either intermediate WebM or direct PNG sequence.
    overlay_input = None
    use_png_direct = str(getattr(encode, "overlay_codec", "vp9")).lower() == "png"

    if not use_png_direct:
        print(f"  步骤 1/2: PNG 帧 → WebM (alpha, cpu-used={encode.webm_cpu_used})...", flush=True)
        webm_path = os.path.join(out_dir, "overlay_temp.webm")
        cmd1 = [
            require_executable("ffmpeg"), "-y",
            "-framerate", str(config.fps),
            "-start_number", str(start_number),
            "-i", frames_pattern,
            *build_webm_encode_args(encode),
            "-t", str(duration),
            webm_path,
        ]
        webm_log_path = os.path.join(out_dir, "ffmpeg-webm.log")
        t0 = time.perf_counter()
        with open(webm_log_path, "w", encoding="utf-8", errors="replace") as log_file:
            r = run_tracked(cmd1, stdout=subprocess.DEVNULL, stderr=log_file, text=True)
        stage_timings["webm_encode"] = time.perf_counter() - t0
        if r.returncode != 0:
            try:
                tail = Path(webm_log_path).read_text(encoding="utf-8", errors="replace")[-1200:]
            except OSError:
                tail = "日志不可读取"
            print(f"  WebM 编码错误；完整日志: {webm_log_path}\n{tail}", flush=True)
            config.stage_timings = stage_timings
            return None
        overlay_input = webm_path
        print(f"  WebM 完成: {stage_timings['webm_encode']:.1f}s", flush=True)
        # Validate WebM duration: if the intermediate overlay is shorter than
        # expected, the final compose will silently lack chat in the tail.
        webm_summary = probe_media_summary(webm_path)
        if webm_summary["ok"]:
            webm_dur = float(webm_summary.get("duration") or 0.0)
            # Allow small encoder margin (VP9 often ±0.1s). Short WebM used to only
            # warn then compose with eof_action=pass — final MP4 length looked fine
            # while chat was missing in the tail (silent-wrong). Hard-fail instead.
            if webm_dur + 0.5 < float(duration or 0.0):
                print(
                    f"  错误: WebM overlay 时长 {webm_dur:.3f}s 显著短于预期 {duration:.3f}s；"
                    f"拒绝合成以免尾段弹幕静默缺失。可改 --overlay-codec png 或检查编码日志: {webm_log_path}",
                    flush=True,
                )
                config.stage_timings = stage_timings
                return None
        else:
            print(
                f"  错误: WebM 中间文件无法探测 ({webm_summary.get('error', 'unknown')})；"
                f"拒绝合成。日志: {webm_log_path}",
                flush=True,
            )
            config.stage_timings = stage_timings
            return None
    else:
        print("  步骤 1/2: 跳过 WebM，直接用 PNG 序列作为 overlay 输入", flush=True)
        stage_timings["webm_encode"] = 0.0

    print(f"  步骤 2/2: overlay 合成到源视频 ({encode.video_codec})...", flush=True)

    # Overlay. Write to a temporary MP4 first so an interrupted FFmpeg run
    # never leaves a broken file at the user-facing output path.
    out_path = os.path.join(out_dir, Path(video_path).stem + "_chat.mp4")
    partial_path = os.path.join(out_dir, Path(video_path).stem + "_chat.partial.mp4")
    try:
        os.remove(partial_path)
    except FileNotFoundError:
        pass
    # 源文件可能用时间戳表达“音频先开始、视频稍后进入”。VLC 会遵守，
    # 但部分剪辑软件会忽略该非零 start_time。把这段差显式编码为首帧冻结，
    # 让导出的 MP4 两条流都从 0 开始，同时保留原本的内容时序。
    #
    # 例外：预览 seek 到片中（preview_clip_start > 0）时，输入已经过了源
    # 片头 A/V 错位，再 tpad 会把“当前 seek 到的画面”冻 1 秒，表现为卡顿。
    timing = resolve_source_av_timing(video_path)
    source_has_audio = bool(timing["has_audio"])
    source_lead_in = float(timing["video_lead_in"] or 0.0)
    seek_ss = float(getattr(config, "preview_clip_start", 0.0) or 0.0)
    # Only apply lead-in freeze when composing from the true start of the source.
    video_lead_in = 0.0 if seek_ss > 1e-6 else source_lead_in
    # Lead-in rewrites A/V start times (freeze first frame for editors).
    # Container duration target stays the render window (~source length),
    # not source+lead_in — otherwise validation false-fails complete encodes.
    output_duration = expected_compose_duration(duration, video_lead_in)
    # Floor rejects truly truncated outputs (lost more than a small lead-in).
    min_output_duration = max(
        0.0,
        float(duration) - max(0.0, video_lead_in) - 0.05,
    )
    # Final CFR for the published video. Keep chat overlay at config.fps;
    # do not force the whole encode down to overlay sampling rate.
    output_fps = resolve_output_fps(
        video_path,
        explicit=getattr(config, "output_fps", None),
        fallback=max(1, int(getattr(config, "fps", 30) or 30)),
    )
    config.output_fps = output_fps
    print(f"  成片输出帧率: {output_fps}fps (弹幕层 {config.fps}fps)", flush=True)

    if video_lead_in > 0.001:
        print(
            f"  检测到视频相对音频延后 {video_lead_in:.3f}s；"
            f"首帧冻结并把两条流改写为从 0 开始（编辑器友好）",
            flush=True,
        )
        print(
            f"  成片目标时长约 {output_duration:.3f}s（与源/渲染窗一致，不额外 +lead-in）",
            flush=True,
        )
        # Pad main with frozen first frame for lead-in, then trim back to
        # output_duration so container length stays ~source (not source+lead_in).
        # Chat is delayed by lead-in without padding its tail, so the full
        # render window of chat remains visible after the freeze.
        main_filter = (
            f"[0:v]setpts=PTS-STARTPTS,"
            f"tpad=start_duration={video_lead_in:.6f}:start_mode=clone,"
            f"trim=duration={output_duration:.6f},setpts=PTS-STARTPTS[main]"
        )
        chat_filter = (
            f"[1:v]setpts=PTS-STARTPTS+"
            f"{video_lead_in:.6f}/TB[chat]"
        )
    else:
        if source_lead_in > 0.001 and seek_ss > 1e-6:
            print(
                f"  跳过源片头 lead-in 冻结（preview seek={seek_ss:.3f}s，"
                f"源 lead-in={source_lead_in:.3f}s 仅作用于片头）",
                flush=True,
            )
        main_filter = "[0:v]setpts=PTS-STARTPTS[main]"
        chat_filter = "[1:v]setpts=PTS-STARTPTS[chat]"

    # eof_action=pass keeps the main video when overlay ends early, instead of
    # shortest=1 which can silently truncate the finished product.
    video_filter = (
        f"{main_filter};"
        f"{chat_filter};"
        f"[main][chat]overlay={config.x}:{config.y}:eof_action=pass:shortest=0[outv]"
    )

    # Dense/mid preview: -ss MUST bind to the source VIDEO input (next -i), not the
    # overlay. FFmpeg applies input options to the following -i only — putting
    # -ss after video -i and before overlay -i seeks the wrong stream and leaves
    # head picture under mid-VOD rebased chat (silent A/V vs chat mismatch).
    cmd2 = [require_executable("ffmpeg"), "-y"]
    if seek_ss > 1e-6:
        cmd2 += ["-ss", f"{seek_ss:.6f}"]
    cmd2 += ["-i", video_path]
    if use_png_direct:
        cmd2 += [
            "-framerate", str(config.fps),
            "-start_number", str(start_number),
            "-i", frames_pattern,
        ]
    else:
        cmd2 += ["-i", overlay_input]

    cmd2 += [
        "-filter_complex", video_filter,
        "-map", "[outv]",
        "-map", "0:a?",
        *build_video_encode_args(encode),
        "-r", fps_to_ffmpeg_rate(output_fps), "-fps_mode", "cfr",
        *build_audio_encode_args(
            encode,
            source_has_audio,
            video_lead_in=video_lead_in,
            notes=encode.notes if hasattr(encode, "notes") else None,
        ),
        "-movflags", "+faststart",
        # MP4 的 make_zero 会引入 AAC priming / H.264 重排后的首帧偏移；
        # 保留重编码后从 0 开始的时间戳，对编辑器兼容性更好。
        "-avoid_negative_ts", "disabled",
        "-t", str(output_duration),
        partial_path,
    ]
    log_path = os.path.join(out_dir, "ffmpeg-overlay.log")
    t1 = time.perf_counter()
    with open(log_path, "w", encoding="utf-8", errors="replace") as log_file:
        r = run_tracked(cmd2, stdout=subprocess.DEVNULL, stderr=log_file, text=True)
    stage_timings["mux_encode"] = time.perf_counter() - t1
    config.stage_timings = stage_timings

    if r.returncode != 0:
        try:
            tail = Path(log_path).read_text(encoding="utf-8", errors="replace")[-1200:]
        except OSError:
            tail = "日志不可读取"
        print(f"  视频合成错误；完整日志: {log_path}\n{tail}", flush=True)
        # If hardware encoder failed under auto/nvenc/qsv/amf, surface a clear hint.
        if encode.resolved_encoder in ("nvenc", "qsv", "amf"):
            print(
                "  提示: 硬件编码器失败时可改用 --encoder x264，或检查 GPU 驱动 / ffmpeg 是否支持该 encoder",
                flush=True,
            )
        return None

    ok, summary, reason = validate_rendered_output(
        partial_path,
        expected_duration=output_duration,
        require_audio=source_has_audio,
        min_duration=min_output_duration if min_output_duration > 0 else None,
    )
    # Do not publish a technically playable overlay with malformed timeline
    # metadata.  This gate is intentionally after FFmpeg but before os.replace.
    if ok:
        from media_health import validate_media_health
        health = validate_media_health(partial_path, mode="fast", require_audio=source_has_audio)
        if not health.ok:
            ok = False
            reason = "媒体健康检查失败: " + health.reason()
    if not ok:
        print(
            f"  输出验证失败: {reason}\n"
            f"  探测结果: duration={summary.get('duration')} has_video={summary.get('has_video')} "
            f"has_audio={summary.get('has_audio')}\n"
            f"  保留临时文件供排查: {partial_path}",
            flush=True,
        )
        return None

    # Back up existing output before overwriting (default behavior).
    # If the subsequent replace fails, restore .bak when possible.
    backup = None
    backup_created = False
    if not getattr(config, "no_backup_prev", False) and os.path.isfile(out_path):
        backup = out_path + ".bak"
        try:
            if os.path.isfile(backup):
                os.remove(backup)
            os.rename(out_path, backup)
            backup_created = True
            print(f"  [backup] {backup}", flush=True)
        except OSError as e:
            print(f"  warning: cannot backup {out_path}: {e}", flush=True)
            backup = None
            backup_created = False
    try:
        os.replace(partial_path, out_path)
    except OSError as e:
        print(f"  发布失败: 无法将 {partial_path} 替换为 {out_path}: {e}", flush=True)
        if backup_created and backup and os.path.isfile(backup) and not os.path.isfile(out_path):
            try:
                os.rename(backup, out_path)
                print(f"  已从备份恢复: {out_path}", flush=True)
            except OSError as restore_err:
                print(f"  警告: 无法从备份恢复 {backup}: {restore_err}", flush=True)
        return None
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(
        f"  输出: {out_path} ({size_mb:.1f} MB, {summary['duration']:.2f}s, "
        f"video={summary['has_video']}, audio={summary['has_audio']})",
        flush=True,
    )
    if stage_timings:
        print("  阶段耗时:", flush=True)
        total = sum(stage_timings.values()) or 1.0
        for name, sec in stage_timings.items():
            print(f"    - {name}: {sec:.1f}s ({sec / total * 100:.0f}%)", flush=True)
    return out_path


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


def main():
    from env_bootstrap import prepend_tools_ffmpeg_to_path

    prepend_tools_ffmpeg_to_path()
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
        type=float,
        default=None,
        help="最终成片视频帧率（可用 29.97 / 30000/1001 等）；默认跟随源视频。不要与 --fps 混用：--fps 只控弹幕层",
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

    # Explicit range checks (fail early with clear messages).
    try:
        validate_positive_int("--fps", args.fps, minimum=1, maximum=240)
        if args.output_fps is not None:
            validate_positive_float("--output-fps", args.output_fps, minimum=1.0, maximum=240.0)
        validate_positive_int("--w/--width", args.width, minimum=16, maximum=7680)
        validate_positive_int("--h/--height", args.height, minimum=16, maximum=4320)
        validate_positive_int("--font-size", args.font_size, minimum=8, maximum=128)
        validate_positive_int("--emote-height", args.emote_height, minimum=8, maximum=256)
        validate_positive_int("--max-visible", args.max_visible, minimum=0, maximum=100)
        stack_mode_cli = str(getattr(args, "stack_mode", "lanes") or "lanes").strip().lower()
        if stack_mode_cli not in ("float", "lanes"):
            raise ValueError(f"--stack-mode must be float or lanes, got {args.stack_mode!r}")
        args.stack_mode = stack_mode_cli
        if stack_mode_cli == "lanes":
            validate_positive_float("--msg-lifetime", args.msg_lifetime, minimum=0.1, maximum=600.0)
        # float stack_mode: msg_lifetime is ignored (capacity-only eviction)
        validate_positive_int("--max-message-lines", args.max_message_lines, minimum=0, maximum=100)
        validate_non_negative_float("--min-visible-seconds", args.min_visible_seconds, maximum=600.0)
        validate_non_negative_float("--arrival-interval", args.arrival_interval, maximum=600.0)
        for ratio_arg in ("x_ratio", "y_ratio", "width_ratio", "height_ratio", "font_size_ratio"):
            validate_non_negative_float(f"--{ratio_arg.replace('_', '-')}", getattr(args, ratio_arg), maximum=1.0)
        if (
            str(getattr(args, "stack_mode", "lanes")).lower() == "lanes"
            and args.msg_lifetime > 0
            and args.min_visible_seconds > args.msg_lifetime
        ):
            raise ValueError("--min-visible-seconds must be <= --msg-lifetime")
        if args.preview_frame is not None:
            validate_non_negative_float("--preview-frame", args.preview_frame, maximum=24 * 3600.0)
        if args.preview_clip is not None:
            validate_non_negative_float("--preview-clip", args.preview_clip, maximum=24 * 3600.0)
            if float(args.preview_clip) <= 0:
                raise ValueError("--preview-clip must be > 0")
        if args.offset is not None:
            # Offset may be negative (chat behind video) or large positive (VOD clip start).
            off = float(args.offset)
            if abs(off) > 7 * 24 * 3600.0:
                raise ValueError("--offset absolute value must be <= 7 days")
        validate_non_negative_float("--blank-hold-seconds", args.blank_hold_seconds, maximum=30.0)
        if args.blank_hold_seconds <= 0:
            raise ValueError("--blank-hold-seconds must be > 0")
    except ValueError as e:
        parser.error(str(e))

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

    if not 0 <= args.bg_alpha <= 255:
        parser.error("--bg-alpha 必须在 0 到 255 之间")

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
            subprocess.run([require_executable("ffmpeg"), "-version"], capture_output=True, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            print("错误: 未找到 ffmpeg，请确保已安装并在 PATH 中")
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
    resolved_out_fps = resolve_output_fps(video_path, explicit=config.output_fps, fallback=30)
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

    # Structured offset diagnosis (manual / auto / warnings).
    video_dur = None
    if (not args.export_translation) or args.offset is None:
        if os.path.isfile(video_path):
            try:
                video_dur = probe_video_duration(video_path)
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
        config.preview_clip_start = clip_start

    win_start, win_end = preview_window(
        args.preview_frame,
        args.preview_clip,
        window_life,
        clip_start=clip_start if args.preview_clip is not None else None,
    )
    # Float has no lifetime. preview_window(preview_frame) would set start=t-life and
    # make nearly every message "in-window", defeating capacity carry-in trim.
    # Anchor the window at the frame instant so pre-window = history before t.
    if (
        stack_mode_cli == "float"
        and args.preview_frame is not None
        and args.preview_clip is None
    ):
        frame_t = max(0.0, float(args.preview_frame))
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
    frames_dir, duration = render_overlay(chat_data, out_dir, video_path, config)
    if args.preview_clip is not None:
        duration = min(duration, max(0.1, float(args.preview_clip)))

    def promote_to_out_base(src_path: str) -> str:
        """Copy a job-dir artifact to out_base with temp+replace and .bak restore.

        Concurrent runs sharing the same out_base each have a unique job_ dir.
        If the basenames would collide (e.g. both promote video_chat.mp4), derive a
        job-unique name so the last writer does not silently overwrite the other.
        """
        if not src_path or not os.path.isfile(src_path):
            return src_path
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
            return src_path

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
        # If still inside job dir, promote basename to out_base; if already outside, keep.
        if path_is_under(preview_path, out_dir) and os.path.abspath(out_dir) != os.path.abspath(out_base):
            final_preview = promote_to_out_base(preview_path)
        else:
            final_preview = preview_path
        print(f"\n[OK] 预览图已生成，跳过视频合成: {final_preview}")
        if not args.keep_temp and os.path.abspath(out_dir) != os.path.abspath(out_base):
            shutil.rmtree(out_dir, ignore_errors=True)
        return

    # Step 3: 合成视频
    result = compose_video(video_path, frames_dir, out_dir, config, duration)

    # Promote final video from job dir to out_base when they differ.
    final_result = promote_to_out_base(result) if result else None

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
            job_output=result,
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
        mark_run_status(
            out_dir,
            "failed",
            stage="compose_or_render",
            note="partial.mp4 / ffmpeg logs may remain in job dir",
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
        if used_isolated_job and final_result and os.path.isfile(final_result):
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
        sys.exit(1)


if __name__ == "__main__":
    # Propagate int return codes from early-exit paths (--clean, validation).
    raise SystemExit(main())
