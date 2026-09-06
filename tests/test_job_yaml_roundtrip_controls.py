#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Control-character round-trip + context type validation for job YAML.

correctness-2: _yaml_quote 只转义 \\n/\\r，含 TAB/VT/FF 的值裸写进 job YAML 后
safe_load 直接拒绝（ScannerError / ReaderError）——write 成功、load 永远失败。
correctness-3: context 无类型校验，`context: 123` 载入后在翻译阶段
render_cn_chat 的 join 才 TypeError——违反「载入时即报错」契约（D-#7）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import load_module


@pytest.fixture(scope="module")
def job_mod():
    return load_module("job_config", "job_config.py")


# --- correctness-2: control chars must survive write -> load round-trip ------

# TAB/VT/FF 是 YAML plain scalar 的硬错误；NUL/ESC/DEL 等走 \xXX 兜底
# （YAML reader 拒绝一切不可打印字符，含 \x7f）。\n 与 \r 原有行为保持不变。
CONTROL_ROUNDTRIP_CASES = [
    ("tab", "hello\tworld"),
    ("vt", "a\x0bb"),
    ("ff", "a\x0cc"),
    ("nul", "a\x00b"),
    ("esc", "a\x1b[31m"),
    ("del", "a\x7fb"),
    ("mixed", "line1\nline2\ttabbed\x0cend"),
    ("plain", "just plain text"),
    ("colon_hash", "key: value # note"),
    ("dash_prefix", "- not a sequence"),
    ("multiline_crlf", "a\r\nb"),
    ("double_quoted", 'say "hi"'),
    ("leading_trailing_space", "  padded  "),
]


@pytest.mark.parametrize(("label", "value"), CONTROL_ROUNDTRIP_CASES, ids=[c[0] for c in CONTROL_ROUNDTRIP_CASES])
def test_write_load_roundtrip_control_chars(
    tmp_path: Path, job_mod, label: str, value: str
):
    p = job_mod.write_job_file(tmp_path / "job.yaml", {"context": value}, overwrite=True)
    data = job_mod.load_job_file(p)
    assert data["context"] == value


def test_roundtrip_control_chars_other_string_field(tmp_path: Path, job_mod):
    """_yaml_quote 是共享出口：非 context 字符串字段同样必须往返一致。"""
    value = "zh\tCN\x0bwide"
    p = job_mod.write_job_file(
        tmp_path / "job.yaml", {"target_language": value}, overwrite=True
    )
    data = job_mod.load_job_file(p)
    assert data["target_language"] == value


def test_yaml_quote_escapes_tab_inside_double_quotes(job_mod):
    """TAB 值必须进引号并转义成 \\t（对齐 yaml.safe_dump 的行为）。"""
    q = job_mod._yaml_quote("hello\tworld")
    assert q == '"hello\\tworld"'


def test_yaml_quote_escapes_other_c0_as_hex_escape(job_mod):
    """NUL/ESC/DEL 等必须转成 \\xXX，safe_load 才能读回同一字符。"""
    assert job_mod._yaml_quote("a\x00b") == '"a\\x00b"'
    assert job_mod._yaml_quote("a\x1bb") == '"a\\x1bb"'
    assert job_mod._yaml_quote("a\x7fb") == '"a\\x7fb"'
    assert job_mod._yaml_quote("a\x0bb") == '"a\\vb"'  # \v
    assert job_mod._yaml_quote("a\x0cb") == '"a\\fb"'  # \f


def test_yaml_quote_legal_values_unchanged(job_mod):
    """合法值不引入新的引号行为（既有输出格式回归护栏）。"""
    assert job_mod._yaml_quote(None) == "null"
    assert job_mod._yaml_quote(True) == "true"
    assert job_mod._yaml_quote(12) == "12"
    assert job_mod._yaml_quote(12.5) == "12.5"
    assert job_mod._yaml_quote("") == '""'
    assert job_mod._yaml_quote("plain text") == "plain text"
    assert job_mod._yaml_quote("multi\nline") == '"multi\\nline"'
    assert job_mod._yaml_quote("multi\r\nline") == '"multi\\r\\nline"'
    assert job_mod._yaml_quote("a: b") == '"a: b"'
    assert job_mod._yaml_quote("no") == '"no"'


# --- correctness-3: context must be a string, rejected at load time ----------


@pytest.mark.parametrize(
    "yaml_text",
    [
        "context: 123\n",
        "context: 1.5\n",
        "context: true\n",
        "context: [a, b]\n",
        "context: {k: v}\n",
        "job:\n  context: 123\n",  # 嵌套写法同样拒绝
        "chat-html: x.html\ncontext: 0\n",  # 与其他字段混排时同样拒绝
    ],
)
def test_load_job_rejects_non_string_context(tmp_path: Path, job_mod, yaml_text: str):
    p = tmp_path / "bad_context.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError, match="context"):
        job_mod.load_job_file(p)


def test_load_job_context_error_message_matches_validator_style(
    tmp_path: Path, job_mod
):
    """报错样式与数值校验器一致：job 字段 context 需要字符串，收到 <值>（类型）。"""
    p = tmp_path / "int_context.yaml"
    p.write_text("context: 123\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"context 需要字符串，收到 123（int）"):
        job_mod.load_job_file(p)


@pytest.mark.parametrize(
    ("yaml_text", "expected"),
    [
        ("context: hello world\n", "hello world"),
        ('context: "quoted context"\n', "quoted context"),
        ("context: |\n  line one\n  line two\n", "line one\nline two\n"),
        ("context: \"tab\\there\"\n", "tab\there"),
        ("context: \"esc\\x1bhere\"\n", "esc\x1bhere"),
    ],
)
def test_load_job_accepts_legal_string_context(
    tmp_path: Path, job_mod, yaml_text: str, expected: str
):
    """合法的裸标量 / 引号标量 / 块标量 context 载入行为不变。"""
    p = tmp_path / "legal_context.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    data = job_mod.load_job_file(p)
    assert data["context"] == expected
