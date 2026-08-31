#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PURE_PRESERVE_RE 灾难性回溯(ReDoS)回归测试。

历史缺陷:旧正则的交替分支两侧都带 \\s*,token 间空白归属有歧义,
整体失配时 fullmatch 穷举 2^k 种切分。空格 join 的 emote 刷屏
(如 "[kek] " * N + 普通词)即可触发指数级耗时。
修复后空白只归后一个 token 消费,匹配线性,恶意样本微秒级返回。
"""

from __future__ import annotations

from pathlib import Path
import sys
import time

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# 恶意样本:纯序列 + 一个普通词结尾,整体必然失配,专门打回溯路径。
MALICIOUS_SAMPLES = [
    "[kek] " * 40 + "你好",
    "100 " * 50 + "go",
    "[a] " * 60 + "b",
    "12 " * 60 + "x",
    "[LUL] " * 20 + "12 " * 20 + "end",
]

PURE_SEQUENCE_SAMPLES = [
    "[a] [b]",
    "12 34",
    "[a]",
    "12",
    "  [a] [b]  ",
    "[a]   [b]",
    "[LUL]",
    "[Hey] [xdx]",
    "12345",
]


@pytest.mark.parametrize("sample", MALICIOUS_SAMPLES)
def test_should_preserve_original_rejects_malicious_flood_quickly(sample):
    import translate_chat_openai as tr

    start = time.perf_counter()
    result = tr.should_preserve_original(sample)
    elapsed = time.perf_counter() - start

    assert result is False
    # 上限给足余量避免 CI 抖动:修复后为线性(微秒级),旧实现指数增长必超。
    assert elapsed < 1.0


@pytest.mark.parametrize("sample", PURE_SEQUENCE_SAMPLES)
def test_should_preserve_original_accepts_pure_sequences(sample):
    import translate_chat_openai as tr

    assert tr.should_preserve_original(sample) is True


@pytest.mark.parametrize(
    "sample",
    ["[a] [b] 你好", "1 2 x", "", "   ", "hello [LUL]"],
)
def test_should_preserve_original_rejects_plain_text(sample):
    import translate_chat_openai as tr

    assert tr.should_preserve_original(sample) is False


# ---------------------------------------------------------------------------
# chat_parser.parse_chat_html(主解析入口)回归:emote CSS 扫描旧正则带
# `[^{}]*?` 无界回看,~200KB 无大括号病态文本 / <style> 中途截断
# (content:url(" 引号未闭合,真实损坏导出形态)会灾难性回溯分钟级卡死。
# 改为线性锚点 + 有界回看后必须秒级完成,且不得误收集 emote(emote_map 为空)。

# 病态样本(均 ~200KB):
# - plain_x_200kb:纯 'x' 流,全程无大括号、无 CSS 结构;
# - truncated_style_unclosed_quote:<style> 中途截断,引号未闭合、</style> 缺失,
#   锚点命中后同引号配对失败,必须立即放弃;
# - mixed_chat_text_shapes:真实聊天文本形态的重复刷屏词,无大括号。
PATHOLOGICAL_HTML_SAMPLES = {
    "plain_x_200kb": (
        "<html><head><style>" + "x" * 200_000 + "</style></head><body></body></html>"
    ),
    "truncated_style_unclosed_quote": (
        '<html><head><style>.first-emote1{background-image:content:url("data:image/png;base64,'
        + "x" * 200_000
    ),
    "mixed_chat_text_shapes_200kb": (
        "<html><head><style>"
        + "PogChamp kek LUL omg 12345 " * 8_000
        + "</style></head><body></body></html>"
    ),
}


@pytest.mark.parametrize("name", sorted(PATHOLOGICAL_HTML_SAMPLES))
def test_parse_chat_html_200kb_pathological_completes_quickly(name, tmp_path):
    import chat_parser

    html_path = tmp_path / "chat.html"
    html_path.write_text(PATHOLOGICAL_HTML_SAMPLES[name], encoding="utf-8")

    start = time.perf_counter()
    data = chat_parser.parse_chat_html(str(html_path), str(tmp_path))
    elapsed = time.perf_counter() - start

    # 上限给足余量避免 CI 抖动:修复后为线性(200KB 实测毫秒级),
    # 旧实现在锚点 + 无界回看样本上呈平方/指数级增长,必然超时。
    assert elapsed < 2.0
    # 结果正确性:病态文本不得误收集 emote,也不得伪造消息。
    assert data["emote_map"] == {}
    assert data["messages"] == []


def test_parse_chat_html_truncated_style_keeps_real_messages(tmp_path):
    import chat_parser

    # <style> 中途截断(引号未闭合、</style> 缺失)+ body 两条真实消息:
    # emote 不得误收集(未配对引号 / 尾部非 ")" 必须放弃),真实消息照常解析。
    chat_lines = (
        '<pre class="comment-root">[<a href="?t=0h0m1s">0:01</a>] '
        '<a><span class="comment-author" style="color: #ffffff">DemoUser</span></a>'
        '<span class="comment-message">: hello world</span></pre>'
        '<pre class="comment-root">[<a href="?t=0h0m2s">0:02</a>] '
        '<a><span class="comment-author" style="color: #00ff00">SecondUser</span></a>'
        '<span class="comment-message">: PogChamp</span></pre>'
    )
    html = (
        '<html><head><style>.first-emote1{background-image:content:url("data:image/png;base64,'
        + "x" * 200_000
        + "</head><body>"
        + chat_lines
        + "</body></html>"
    )
    html_path = tmp_path / "chat.html"
    html_path.write_text(html, encoding="utf-8")

    start = time.perf_counter()
    data = chat_parser.parse_chat_html(str(html_path), str(tmp_path))
    elapsed = time.perf_counter() - start

    assert elapsed < 2.0
    # 截断的 emote 规则(引号未闭合)不得被收集为 emote。
    assert data["emote_map"] == {}
    # 真实消息不受病态 <style> 拖累,照常解析。
    assert [m["author"] for m in data["messages"]] == ["DemoUser", "SecondUser"]
    assert data["messages"][0]["timestamp"] == 1.0
    assert data["messages"][0]["fragments"] == [{"type": "text", "text": "hello world"}]
    assert data["messages"][1]["fragments"] == [{"type": "text", "text": "PogChamp"}]
