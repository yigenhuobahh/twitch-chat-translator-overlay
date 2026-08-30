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
