#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TwitchDownloader / legacy Twitch web chat HTML parser.

Extracted from twitch_chat_burn for maintainability. Behavior is intentionally
unchanged: same message schema and emote map outputs.
"""

from __future__ import annotations

import base64
import html as html_mod
import json
import os
import re
import time

# Tiny 1x1 PNG used only for sniff tests / not required at runtime.
_EMOTE_PREFIXES = ("first-", "second-", "third-")
_CSS_LOOKBACK = 8192  # class selector may sit far before content:url
_MAX_EMOTE_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_EMOTE_BYTES = 128 * 1024 * 1024
# A padded base64 payload needs four characters for every three decoded bytes.
_MAX_EMOTE_BASE64_CHARS = 4 * ((_MAX_EMOTE_BYTES + 2) // 3)


def _read_html_text(html_path: str) -> str:
    """Read chat HTML robustly: prefer UTF-8 (with BOM), fall back to latin-1.

    Never hard-crash on residual non-UTF8 bytes; replace or re-decode.
    """
    with open(html_path, "rb") as bf:
        raw = bf.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # A damaged byte must not force valid UTF-8 CJK through latin-1.
        # Keep latin-1 only for genuinely legacy ASCII-plus-latin-1 exports.
        repaired = raw.decode("utf-8", errors="replace")
        if any(ch != "\ufffd" and ord(ch) > 127 for ch in repaired):
            return repaired
        return raw.decode("latin-1")


def _class_has_token(class_value: str, token: str) -> bool:
    return token in class_value.split()


def _first_emote_class(class_value: str) -> str | None:
    for tok in class_value.split():
        if tok.startswith(_EMOTE_PREFIXES):
            return tok
    return None


class _ParseProgress:
    """Throttle console progress so large HTML parses do not look hung."""

    def __init__(self, *, every_n: int = 25, every_sec: float = 2.0) -> None:
        self.every_n = max(1, int(every_n))
        self.every_sec = max(0.5, float(every_sec))
        self._t0 = time.perf_counter()
        self._last_t = self._t0
        self._last_n = 0

    def tick(self, n: int, label: str, *, force: bool = False, total: int | None = None) -> None:
        now = time.perf_counter()
        if not force:
            if (n - self._last_n) < self.every_n and (now - self._last_t) < self.every_sec:
                return
        self._last_n = n
        self._last_t = now
        elapsed = now - self._t0
        if total and total > 0:
            pct = min(100.0, 100.0 * float(n) / float(total))
            print(f"  … {label}: {n}/{total} ({pct:.0f}%) 用时 {elapsed:.1f}s", flush=True)
        else:
            print(f"  … {label}: {n} 用时 {elapsed:.1f}s", flush=True)


def parse_chat_html(html_path, out_dir):
    """从 Twitch HTML 聊天记录中提取消息和 emote 图片。"""
    print(f"[1/4] 解析聊天 HTML: {html_path}", flush=True)

    t_parse0 = time.perf_counter()
    try:
        file_size = os.path.getsize(html_path)
    except OSError:
        file_size = 0
    if file_size > 0:
        size_mb = file_size / (1024 * 1024)
        print(f"  读取文件中… ({size_mb:.1f} MB)", flush=True)
        if size_mb >= 3.0:
            print(
                "  [提示] 聊天 HTML 较大（常见于嵌入表情的长 VOD）；"
                "提取 emote/消息时可能需数十秒到数分钟，期间会持续输出进度",
                flush=True,
            )

    html = _read_html_text(html_path)
    html_chars = len(html)
    # Prefer scoped regions: TD puts emote CSS in <style> and messages in <body>.
    # Scan every style block: exports can have general page CSS before the emote
    # rules, and looking only at the first block loses those emotes.
    style_blocks = [
        match.group(1)
        for match in re.finditer(r"<style\b[^>]*>(.*?)</style\s*>", html, re.IGNORECASE | re.DOTALL)
    ]
    body_match = re.search(r"<body\b[^>]*>(.*?)</body\s*>", html, re.IGNORECASE | re.DOTALL)
    body_region = body_match.group(1) if body_match else html
    if style_blocks:
        css_region = "\n".join(style_blocks)
        print(
            f"  已载入 {html_chars / (1024 * 1024):.1f}M 字符"
            f"（style {len(css_region) / (1024 * 1024):.1f}M / body {len(body_region) / (1024 * 1024):.1f}M，"
            f"共 {len(style_blocks)} 个 style 块），"
            f"开始扫描 emote CSS…",
            flush=True,
        )
    else:
        css_region = html
        print(
            f"  已载入 {html_chars / (1024 * 1024):.1f}M 字符，开始扫描 emote CSS…",
            flush=True,
        )

    # --- 提取 emote CSS (base64 编码的图片) ---
    # TwitchDownloader embeds images as CSS content:url("data:image/...;base64,...").
    # Class prefixes seen in real exports:
    #   first-*  Twitch first-party (incl. first-emotesv2_*)
    #   third-*  third-party packs (BTTV / FFZ / 7TV etc. when TD embeds them)
    # Also accept optional single-quoted content:url('data:image/...') for robustness.
    emote_map = {}  # class_name -> image path
    emote_dir = os.path.join(out_dir, "emotes")
    os.makedirs(emote_dir, exist_ok=True)

    emote_count = 0
    emote_rules_seen = 0
    emote_bytes_written = 0
    # Match content:url("data:image/...;base64,...") only — require base64 inside *this* url()
    # so non-base64 data: URLs cannot steal a later rule's payload. Capture the full
    # selector text immediately before the opening "{" of this rule.
    # NOTE: avoid DOTALL over multi-MB CSS; rules are single-line or short in TD exports.
    # Keep DOTALL but only on css_region (style body) to bound cost.
    emote_rule_re = re.compile(
        r"([^{}]+)\{[^{}]*?content\s*:\s*url\(\s*(['\"])data:image/[^'\"]*;base64,([^'\"]+)\2\s*\)",
        re.IGNORECASE | re.DOTALL,
    )
    emote_prog = _ParseProgress(every_n=20, every_sec=1.5)
    last_scan_pos = 0
    last_scan_print = time.perf_counter()
    css_chars = len(css_region) or 1
    for m in emote_rule_re.finditer(css_region):
        emote_rules_seen += 1
        # While the regex walks a multi-MB <style> blob, emit scan progress even
        # before any class is accepted/written.
        now = time.perf_counter()
        pos = m.end()
        if pos - last_scan_pos >= max(256 * 1024, css_chars // 20) or (now - last_scan_print) >= 2.0:
            pct = min(100.0, 100.0 * float(pos) / float(css_chars))
            print(
                f"  … 扫描 emote CSS: {pct:.0f}% ，"
                f"已命中规则 {emote_rules_seen}，已写入 {emote_count}  "
                f"用时 {now - t_parse0:.1f}s",
                flush=True,
            )
            last_scan_pos = pos
            last_scan_print = now
        selector_blob = m.group(1)
        b64_data = (m.group(3) or "").replace("\n", "").replace("\r", "").strip()
        if not b64_data:
            continue
        if len(b64_data) > _MAX_EMOTE_BASE64_CHARS:
            print(
                f"  emote base64 过大，解码前已跳过 ({len(b64_data)} chars)",
                flush=True,
            )
            continue
        # Collect every .first- / .second- / .third- class in the selector list.
        class_names: list[str] = []
        for sel in selector_blob.split(","):
            # Prefer the last class token in the simple selector.
            tokens = re.findall(r"\.([A-Za-z0-9_-]+)", sel)
            for tok in tokens:
                if tok.startswith(_EMOTE_PREFIXES) and tok not in class_names:
                    class_names.append(tok)
        if not class_names:
            continue
        try:
            img_data = base64.b64decode(b64_data)
        except Exception as e:
            print(f"  emote CSS base64 解码失败: {e}", flush=True)
            continue
        # Soft size cap: skip absurd blobs (DoS / disk fill).
        if len(img_data) > _MAX_EMOTE_BYTES:
            print(f"  emote 图片过大已跳过 ({len(img_data)} bytes)", flush=True)
            continue
        ext = "bin"
        if img_data.startswith(b"\x89PNG"):
            ext = "png"
        elif img_data[:6] in (b"GIF87a", b"GIF89a"):
            ext = "gif"
        elif img_data[:4] == b"RIFF" and b"WEBP" in img_data[:16]:
            ext = "webp"
        elif img_data[:2] == b"\xff\xd8":
            ext = "jpg"
        budget_exhausted = False
        for class_name in class_names:
            if emote_bytes_written + len(img_data) > _MAX_TOTAL_EMOTE_BYTES:
                budget_exhausted = True
                break
            try:
                safe_name = re.sub(r"[^\w.-]+", "_", class_name)
                img_path = os.path.join(emote_dir, f"{safe_name}.{ext}")
                with open(img_path, "wb") as ef:
                    ef.write(img_data)
                emote_map[class_name] = img_path
                emote_count += 1
                emote_bytes_written += len(img_data)
            except Exception as e:
                print(f"  emote {class_name} 写入失败: {e}", flush=True)
        emote_prog.tick(emote_count, "写入 emote 图片")
        if budget_exhausted:
            print(
                f"  emote 累计写入达到上限 {_MAX_TOTAL_EMOTE_BYTES} bytes，停止继续提取",
                flush=True,
            )
            break
        if emote_count >= 5000:
            print("  emote 数量达到上限 5000，停止继续提取", flush=True)
            break

    print(
        f"  提取 {emote_count} 个 emote 图片"
        f"（CSS 规则命中 {emote_rules_seen}，用时 {time.perf_counter() - t_parse0:.1f}s）",
        flush=True,
    )

    # --- 提取消息 ---
    messages = []
    # Message markup lives in body; avoid re-scanning multi-MB CSS.
    msg_html = body_region
    print("  开始提取消息…", flush=True)

    # 检测 HTML 格式
    if "comment-root" in msg_html:
        # ===== TwitchDownloaderGUI 格式 =====
        # 每条消息: <pre class="comment-root">[<a href=".../?t=0h12m26s">0:12:26</a>] <img class="badge-image ..." title="xxx">... <a href="..."><span class="comment-author" style="color: #XXX">name</span></a><span class="comment-message">: ...</span></pre>

        # Double or single quoted href. Allow extra query/hash after t= (e.g. &foo=1, #chat).
        time_link_pattern = re.compile(
            r"""<a\s+href=["'][^"']*[?&]t=(\d+)h(\d+)m(\d+)s(?:[&"'#]|$)""",
            re.IGNORECASE,
        )
        # class/style attribute order must not matter; quotes must not matter.
        # Require class *token* "comment-author" (whitespace/quote bounded), not
        # hyphenated decoys like "not-comment-author".
        author_pattern = re.compile(
            r"<span\b(?=[^>]*\bclass\s*=\s*[\"'](?:[^\"']*\s)?comment-author(?:\s[^\"']*)?[\"'])([^>]*)>([^<]*)</span>",
            re.IGNORECASE,
        )
        # color: may appear anywhere inside style= (with other CSS props / !important).
        author_color_pattern = re.compile(
            r"""\bstyle\s*=\s*["'][^"']*?\bcolor\s*:\s*([^;"'!]+)""",
            re.IGNORECASE,
        )
        # Badge: class token badge-image (not not-badge-image); title any-quoted; attr order free.
        badge_img_pattern = re.compile(
            r"<img\b(?=[^>]*\bclass\s*=\s*[\"'](?:[^\"']*\s)?badge-image(?:\s[^\"']*)?[\"'])"
            r"(?=[^>]*\btitle\s*=\s*[\"']([^\"']*)[\"'])[^>]*>",
            re.IGNORECASE,
        )
        # Emote token: class has emote-image + first-/second-/third-* token (extra class
        # tokens allowed). title optional-ish but preferred; attr order free; quotes free.
        # Group 1 = full match wrapper handled by outer group; 2 = emote class; 3 = title.
        token_pattern = re.compile(
            r"(<img\b"
            r"(?=[^>]*\bclass\s*=\s*[\"']([^\"']+)[\"'])"
            r"(?=[^>]*\btitle\s*=\s*[\"']([^\"']*)[\"'])"
            r"[^>]*>\s*"
            r"(?:<span\b[^>]*\bclass\s*=\s*[\"'][^\"']*\btext-hide\b[^\"']*[\"'][^>]*>[^<]*</span>)?)",
            re.IGNORECASE,
        )
        # Fallback when title missing: still capture emote-image + first/second/third class.
        token_pattern_no_title = re.compile(
            r"(<img\b"
            r"(?=[^>]*\bclass\s*=\s*[\"']([^\"']+)[\"'])"
            r"(?![^>]*\btitle\s*=)"
            r"[^>]*>\s*"
            r"(?:<span\b[^>]*\bclass\s*=\s*[\"'][^\"']*\btext-hide\b[^\"']*[\"'][^>]*>[^<]*</span>)?)",
            re.IGNORECASE,
        )
        # comment-message open tag: class token may be mixed with others (deleted etc.)
        comment_message_open = re.compile(
            r"<span\b(?=[^>]*\bclass\s*=\s*[\"'][^\"']*\bcomment-message\b[^\"']*[\"'])[^>]*>",
            re.IGNORECASE,
        )
        # Split on comment-root regardless of quote style around class value.
        print("  切分 comment-root 消息块…", flush=True)
        t_split = time.perf_counter()
        pre_lines = re.split(
            r'(?=<pre\b[^>]*\bclass\s*=\s*["\'][^"\']*\bcomment-root\b)',
            msg_html,
            flags=re.IGNORECASE,
        )
        root_total = sum(1 for part in pre_lines if "comment-root" in part)
        print(
            f"  消息块约 {root_total} 个（切分用时 {time.perf_counter() - t_split:.1f}s）",
            flush=True,
        )
        msg_prog = _ParseProgress(every_n=50, every_sec=2.0)
        roots_seen = 0

        for line in pre_lines:
            if "comment-root" not in line:
                continue
            roots_seen += 1
            msg_prog.tick(roots_seen, "解析消息块", total=root_total or None)

            # 时间戳: 从链接 ?t=0h12m26s 解析
            ts_match = time_link_pattern.search(line)
            if not ts_match:
                continue
            h, m, s = int(ts_match.group(1)), int(ts_match.group(2)), int(ts_match.group(3))
            timestamp = h * 3600 + m * 60 + s

            # 作者（属性顺序 / 引号无关）
            author_match = author_pattern.search(line)
            if not author_match:
                continue
            attr_blob = author_match.group(1) or ""
            color_match = author_color_pattern.search(attr_blob)
            color = color_match.group(1) if color_match else ""
            author = html_mod.unescape(author_match.group(2).strip())

            # Badges
            badges = []
            for bm in badge_img_pattern.finditer(line):
                badges.append({"title": html_mod.unescape(bm.group(1))})

            # 提取 comment-message 内容
            # comment-message 内部嵌套有 <span class="text-hide">，其 </span> 会干扰非贪婪匹配
            # 直接找 comment-message 开始到 </pre> 之间的内容
            msg_start = -1
            # Fast exact markers first (common TD export)
            for marker in (
                '<span class="comment-message">',
                "<span class='comment-message'>",
            ):
                pos = line.find(marker)
                if pos != -1:
                    msg_start = pos + len(marker)
                    break
            if msg_start == -1:
                m_msg = comment_message_open.search(line)
                if not m_msg:
                    continue
                msg_start = m_msg.end()
            # 找对应的 </pre> 结尾
            pre_end = line.find("</pre>", msg_start)
            if pre_end == -1:
                continue
            # comment-message 的 </span> 在 </pre> 之前
            msg_raw = line[msg_start:pre_end]
            # 去掉末尾的 </span>
            if msg_raw.rstrip().endswith("</span>"):
                msg_content = msg_raw.rstrip()[: -len("</span>")]
            else:
                msg_content = msg_raw

            # 解析 fragments: text + emote 混合
            fragments = []

            # 去掉开头的 ": "
            if msg_content.startswith(": "):
                msg_content = msg_content[2:]
            elif msg_content.startswith(":"):
                msg_content = msg_content[1:]

            # Collect emote matches (with and without title). Prefer titled matches.
            matches: list[tuple[int, int, str, str]] = []  # start, end, class, title
            occupied: list[tuple[int, int]] = []

            def _overlaps(a0: int, a1: int, ranges: list[tuple[int, int]] = occupied) -> bool:
                for b0, b1 in ranges:
                    if a0 < b1 and a1 > b0:
                        return True
                return False

            for tm in token_pattern.finditer(msg_content):
                class_value = tm.group(2) or ""
                if not _class_has_token(class_value, "emote-image"):
                    continue
                emote_cls = _first_emote_class(class_value)
                if not emote_cls:
                    continue
                title = html_mod.unescape(tm.group(3) or "")
                matches.append((tm.start(), tm.end(), emote_cls, title))
                occupied.append((tm.start(), tm.end()))

            for tm in token_pattern_no_title.finditer(msg_content):
                if _overlaps(tm.start(), tm.end()):
                    continue
                class_value = tm.group(2) or ""
                if not _class_has_token(class_value, "emote-image"):
                    continue
                emote_cls = _first_emote_class(class_value)
                if not emote_cls:
                    continue
                matches.append((tm.start(), tm.end(), emote_cls, emote_cls))
                occupied.append((tm.start(), tm.end()))

            matches.sort(key=lambda x: x[0])

            last_end = 0
            for start, end, emote_cls, title in matches:
                text_before = msg_content[last_end:start]
                # Strip residual tags (including text-hide leftovers) before keeping text.
                text_clean = re.sub(
                    r'<span\b[^>]*\bclass\s*=\s*["\'][^"\']*\btext-hide\b[^"\']*["\'][^>]*>[^<]*</span>',
                    "",
                    text_before,
                    flags=re.IGNORECASE,
                )
                text_clean = re.sub(r"<[^>]+>", "", text_clean)
                text_clean = html_mod.unescape(text_clean).strip()
                if text_clean:
                    fragments.append({"type": "text", "text": text_clean})
                fragments.append(
                    {
                        "type": "emote",
                        "class": emote_cls,
                        "title": title,
                    }
                )
                last_end = end

            # 剩余文本
            text_after = msg_content[last_end:]
            text_after = re.sub(
                r'<span\b[^>]*\bclass\s*=\s*["\'][^"\']*\btext-hide\b[^"\']*["\'][^>]*>[^<]*</span>',
                "",
                text_after,
                flags=re.IGNORECASE,
            )
            text_clean = re.sub(r"<[^>]+>", "", text_after)
            text_clean = html_mod.unescape(text_clean).strip()
            if text_clean:
                fragments.append({"type": "text", "text": text_clean})

            if not fragments:
                # 纯文本消息
                text_only = re.sub(
                    r'<span\b[^>]*\bclass\s*=\s*["\'][^"\']*\btext-hide\b[^"\']*["\'][^>]*>[^<]*</span>',
                    "",
                    msg_content,
                    flags=re.IGNORECASE,
                )
                text_only = re.sub(r"<[^>]+>", "", text_only)
                text_only = html_mod.unescape(text_only).strip()
                if text_only:
                    fragments.append({"type": "text", "text": text_only})

            if fragments:
                messages.append(
                    {
                        "timestamp": timestamp,
                        "author": author,
                        "color": color.strip() if color else "",
                        "badges": badges,
                        "fragments": fragments,
                    }
                )
        msg_prog.tick(roots_seen, "解析消息块", force=True, total=root_total or None)

    else:
        # ===== 旧格式 (Twitch Web HTML) =====
        # Best-effort legacy path (P2 robustness deferred); keep existing happy path.
        msg_pattern = re.compile(
            r'<span[^>]*class="chat-author__display-name"[^>]*style="color:\s*([^"]*)"[^>]*>([^<]*)</span>'
        )
        badge_pattern = re.compile(
            r'<div[^>]*class="chat-line__badge-container"[^>]*title="([^"]*)"'
        )
        time_pattern = re.compile(r'data-timestamp="(\d+)"')
        print("  检测到旧版 web 聊天格式，切分 chat-line…", flush=True)
        lines = re.split(r'(?=<div class="chat-line__message")', msg_html)
        line_total = sum(1 for part in lines if 'class="chat-line__message"' in part)
        msg_prog = _ParseProgress(every_n=50, every_sec=2.0)
        lines_seen = 0

        for line in lines:
            if 'class="chat-line__message"' not in line:
                continue
            lines_seen += 1
            msg_prog.tick(lines_seen, "解析消息块", total=line_total or None)
            ts_match = time_pattern.search(line)
            if not ts_match:
                continue
            timestamp = int(ts_match.group(1)) / 1000.0
            author_match = msg_pattern.search(line)
            if not author_match:
                continue
            color = author_match.group(1).strip()
            author = html_mod.unescape(author_match.group(2).strip())
            badges = [{"title": bm.group(1)} for bm in badge_pattern.finditer(line)]

            fragments = []
            frag_pattern = re.compile(
                r'(<span[^>]*class="text-fragment"[^>]*>([^<]*)</span>)|'
                r'(<img[^>]*class="[^"]*?((?:first|second|third)-[\w-]+)[^"]*"[^>]*(?:alt|title)="([^"]*)")',
                re.DOTALL | re.IGNORECASE,
            )
            for fm in frag_pattern.finditer(line):
                if fm.group(2) is not None:
                    fragments.append({"type": "text", "text": html_mod.unescape(fm.group(2))})
                elif fm.group(4) is not None:
                    fragments.append(
                        {
                            "type": "emote",
                            "class": fm.group(4),
                            "title": html_mod.unescape(fm.group(5) or ""),
                        }
                    )

            if fragments:
                messages.append(
                    {
                        "timestamp": timestamp,
                        "author": author,
                        "color": color if color else "",
                        "badges": badges,
                        "fragments": fragments,
                    }
                )
        msg_prog.tick(lines_seen, "解析消息块", force=True, total=line_total or None)

    messages.sort(key=lambda m: m["timestamp"])
    if messages:
        print(
            f"  提取 {len(messages)} 条消息 (时间范围 {messages[0]['timestamp']:.1f}s - {messages[-1]['timestamp']:.1f}s)",
            flush=True,
        )
    else:
        print("  警告: 未提取到任何消息!", flush=True)
        print("  请确认 HTML 文件是 Twitch 聊天记录导出格式", flush=True)

    print(f"  解析总用时 {time.perf_counter() - t_parse0:.1f}s", flush=True)

    # 保存 chat_data.json
    chat_data = {"messages": messages, "emote_map": emote_map}
    json_path = os.path.join(out_dir, "chat_data.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(chat_data, f, ensure_ascii=False, indent=2)

    return chat_data
