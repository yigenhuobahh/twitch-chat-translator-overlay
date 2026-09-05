#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pure text layout: CJK-aware wrapping, fragments, message lines, badges.

Extracted verbatim from twitch_chat_burn for maintainability. No I/O: every
function is pure and receives its measurement callables (text width, emote
width / availability, fonts) as arguments, so the schedule line-count
prepass and the bitmap renderer share exactly one layout implementation."""

from __future__ import annotations

import re
import unicodedata

from common_utils import hex_to_rgb_soft

# Invisible/hostile characters that must never reach Pillow draw.text:
# - bidi embedding/override controls (U+202A-U+202E, U+2066-U+2069) can visually
#   reorder/reverse rendered text (display spoofing, e.g. U+202E RLO).
# - zero-width characters (U+200B, U+200C, U+FEFF) are invisible but occupy
#   layout space and can hide content from human review. U+200D (ZWJ) is
#   deliberately kept: it carries no display-spoofing capability and removing
#   it breaks emoji ZWJ ligature sequences (e.g. 😀‍🚀).
# - C0/C1 control characters render as tofu boxes (or are otherwise undefined
#   glyphs) in bitmap fonts.
# \t \n \r are excluded from the control class because they are normalized to
# plain spaces (matching common_utils.normalize_text and the space-based
# wrapping in split_text_for_wrap, which draws a bare newline as tofu).
_BIDI_CONTROL_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069]")
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\ufeff]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def sanitize_render_text(t) -> str:
    """Make arbitrary text safe for direct Pillow draw.text rendering.

    Removes bidi override/embedding controls, zero-width characters and
    C0/C1 control characters; collapses \\r \\n \\t to plain spaces (render
    only wraps on spaces, so a literal newline would draw as tofu). Shared by
    the chat HTML text path (via normalize_text) and the LLM translation path
    (via translation_support.clean_translation_text). NFKC neither produces
    nor removes any of these characters, so callers may sanitize before or
    after NFKC; this module always sanitizes after NFKC (fixed order).
    """
    s = str(t or "")
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = _BIDI_CONTROL_RE.sub("", s)
    s = _ZERO_WIDTH_RE.sub("", s)
    s = _CONTROL_RE.sub("", s)
    return s


def is_cjk_char(ch):
    """判断字符是否为 CJK 字符（中文/日文假名/韩文谚文）"""
    cp = ord(ch)
    if (0x4E00 <= cp <= 0x9FFF or      # CJK Unified Ideographs
        0x3400 <= cp <= 0x4DBF or      # CJK Extension A
        0x20000 <= cp <= 0x2A6DF or    # CJK Extension B
        0xFF00 <= cp <= 0xFFEF or      # Fullwidth Forms
        0x3000 <= cp <= 0x303F or      # CJK Symbols & Punctuation
        0x3040 <= cp <= 0x30FF or      # Hiragana + Katakana (日文假名)
        0xAC00 <= cp <= 0xD7AF):       # Hangul Syllables (韩文谚文)
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


def normalize_text(t):
    """Normalize compatibility glyphs without destroying supplementary Unicode.

    Also sanitizes invisible/hostile characters (bidi overrides, zero-width,
    C0/C1 controls; see sanitize_render_text) so nothing reachable from remote
    chat HTML reaches draw.text. Sanitization happens after NFKC (fixed order;
    NFKC never creates or removes these characters). Emoji, ZWJ sequences and
    supplementary CJK are kept whenever the font supports them.
    """
    # NFKC still simplifies mathematical compatibility letters.
    return sanitize_render_text(unicodedata.normalize("NFKC", str(t or "")))


def hex_to_rgb(hex_color):
    """Compat wrapper: author colors fall back to white (shared implementation)."""
    return hex_to_rgb_soft(hex_color, default=(255, 255, 255))


# Layout defaults shared by line-count prepass and message bitmap render.
# Keep these in one place so schedule capacity and drawn height cannot drift.
MESSAGE_PAD = 5
MESSAGE_BADGE_SIZE = 9
MESSAGE_GAP = 3
MESSAGE_INDENT = 12

# Badge title -> display color. Twitch badge titles arrive with arbitrary
# casing ("Broadcaster", "Moderator"), so lookups normalize via badge_color_for.
BADGE_COLORS = {
    "broadcaster": (255, 50, 50),
    "moderator": (0, 160, 0),
    "vip": (213, 0, 213),
    "subscriber": (100, 100, 255),
    "premium": (0, 169, 255),
    "verified": (0, 169, 255),
}
BADGE_FALLBACK_COLOR = (85, 85, 85)


def badge_color_for(title) -> tuple[int, int, int]:
    """Map a badge title to its color, case/whitespace-insensitive, else gray."""
    key = str(title or "").split("-")[0].strip().lower()
    return BADGE_COLORS.get(key, BADGE_FALLBACK_COLOR)


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


# S-3: 硬上限兜底。max_message_lines 默认 0 = 不限行数,但一条恶意/意外的
# 超长消息(如 50k 字符)会先按全量行数分配 RGBA 位图再裁剪,数百行即可
# 产生数十 MB 级单消息贴图。此常量只在"用户未配置行数上限"时兜底截断
# (截断复用现有省略号路径,与显式配置截断观感一致);显式配置的
# max_message_lines > 200 是用户的明确选择,不会被本兜底压缩。
# 默认行为变化: 未配置 max_message_lines 时,单消息最多渲染
# _HARD_MAX_MESSAGE_LINES 行,末行以 "..." 结尾(此前为无限行)。
_HARD_MAX_MESSAGE_LINES = 200


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
    """Cap wrapped lines and append '...' so truncation is visible (not silent crop).

    When ``max_message_lines`` is unset (0/negative), the hard cap
    ``_HARD_MAX_MESSAGE_LINES`` bounds the line count instead — the ellipsis
    path is reused so the fallback truncation looks identical to an explicit
    configured limit. An explicit ``max_message_lines`` above the hard cap is
    honored as-is (user's explicit choice).
    """
    limit = max_message_lines if max_message_lines and max_message_lines > 0 else _HARD_MAX_MESSAGE_LINES
    if len(lines) <= limit:
        return lines
    lines = lines[:limit]
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
        # S-3 hard cap: mirror the render-side fallback so the line-count
        # prepass never reports more lines than truncate_wrapped_lines_with_ellipsis
        # would actually produce when no explicit limit is configured.
        effective_limit = (
            max_message_lines if max_message_lines and max_message_lines > 0 else _HARD_MAX_MESSAGE_LINES
        )
        num_lines = min(num_lines, effective_limit)
    return lines, header, num_lines
