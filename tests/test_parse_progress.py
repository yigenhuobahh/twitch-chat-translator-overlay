#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Progress output during long HTML parses should not stay silent."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _html_with_emotes(n_emotes: int = 5, n_msgs: int = 3) -> str:
    rules = []
    for i in range(n_emotes):
        rules.append(
            f'.first-e{i} {{ content:url("data:image/png;base64,{TINY_PNG_B64}"); }}'
        )
    msgs = []
    for i in range(n_msgs):
        msgs.append(
            f'<pre class="comment-root">[<a href="https://www.twitch.tv/videos/1?t=0h0m{i+1}s">'
            f"0:00:{i+1:02d}</a>] "
            f'<span class="comment-author">U{i}</span>'
            f'<span class="comment-message">: hi '
            f'<img class="emote-image first-e{i % max(1, n_emotes)}" title="E">'
            f'<span class="text-hide">E</span></span></pre>'
        )
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><style>\n"
        + "\n".join(rules)
        + "\n</style></head><body>\n"
        + "\n".join(msgs)
        + "\n</body></html>\n"
    )


def test_parse_prints_stage_progress(tmp_path: Path, capsys):
    from chat_parser import parse_chat_html

    html = tmp_path / "chat.html"
    html.write_text(_html_with_emotes(3, 3), encoding="utf-8")
    data = parse_chat_html(str(html), str(tmp_path / "out"))
    assert len(data["messages"]) == 3
    out = capsys.readouterr().out
    assert "读取文件" in out or "已载入" in out
    assert "emote" in out.lower() or "扫描 emote" in out or "提取" in out
    assert "开始提取消息" in out or "消息" in out
    assert "解析总用时" in out


def test_parse_collects_emotes_from_all_style_blocks(tmp_path: Path):
    from chat_parser import parse_chat_html

    html = _html_with_emotes(1, 1)
    html = html.replace(
        "</style>",
        f'</style><style>.first-later {{ content:url("data:image/png;base64,{TINY_PNG_B64}"); }}</style>',
        1,
    )
    html = html.replace('first-e0" title="E"', 'first-later" title="E"')
    source = tmp_path / "chat.html"
    source.write_text(html, encoding="utf-8")

    data = parse_chat_html(str(source), str(tmp_path / "out"))

    assert "first-later" in data["emote_map"]
    assert Path(data["emote_map"]["first-later"]).is_file()


def test_parse_progress_helper_throttles():
    from chat_parser import _ParseProgress

    p = _ParseProgress(every_n=10, every_sec=60.0)
    # First tick below threshold should no-op unless force
    p.tick(1, "x")
    p.tick(2, "x")
    # force always emits
    p.tick(2, "x", force=True)
