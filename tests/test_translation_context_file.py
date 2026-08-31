# -*- coding: utf-8 -*-
"""PARSE-O5: oversized translation context must travel via a file, not argv.

Windows caps the whole CreateProcess command line at 32,767 chars; a profile
with a large glossary exceeds it and dies before Python even starts. The
pipeline therefore passes contexts longer than a safe threshold through
``--context-file`` instead of ``--context``.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from helpers import load_module

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


@pytest.fixture(scope="module")
def pipe():
    return load_module("render_cn_chat", "render_cn_chat.py")


@pytest.fixture(scope="module")
def tr():
    return load_module("translate_chat_openai", "translate_chat_openai.py")


def test_short_context_travels_via_argv(pipe):
    args = pipe._prepare_translation_context("gaming chat", Path("."))
    assert args == ["--context", "gaming chat"]


def test_long_context_written_to_workdir_file(pipe, tmp_path):
    context = "词汇" * 5000  # 10,000 chars > threshold
    args = pipe._prepare_translation_context(context, tmp_path)
    assert args[0] == "--context-file"
    path = Path(args[1])
    assert path.parent.resolve() == tmp_path.resolve()
    assert path.read_text(encoding="utf-8") == context
    # context is part of every batch prompt: the per-batch cap must rise with it
    assert args[2] == "--max-batch-chars"
    assert int(args[3]) == min(200_000, len(context) + 16_000)


def test_huge_context_caps_max_batch_chars_at_ceiling(pipe, tmp_path):
    context = "词" * 300_000
    args = pipe._prepare_translation_context(context, tmp_path)
    assert int(args[3]) == 200_000
    assert Path(args[1]).read_text(encoding="utf-8") == context


def test_translator_exposes_context_file_flag():
    r = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "translate_chat_openai.py"), "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )
    assert r.returncode == 0
    assert "--context-file" in (r.stdout or "") + (r.stderr or "")


def test_translator_resolve_reads_utf8_file(tr, tmp_path):
    ctx_file = tmp_path / "ctx.txt"
    ctx_file.write_text("术语表：\n  PogChamp -> 惊讶", encoding="utf-8")
    assert tr.resolve_translation_context("argv-unused", str(ctx_file)) == (
        "术语表：\n  PogChamp -> 惊讶"
    )


def test_translator_resolve_without_file_returns_argv_value(tr):
    assert tr.resolve_translation_context("livestream chat", None) == "livestream chat"


def test_translator_parse_accepts_context_file(tr, tmp_path):
    json_path = tmp_path / "export.json"
    json_path.write_text("{}", encoding="utf-8")
    ctx_file = tmp_path / "ctx.txt"
    ctx_file.write_text("x" * 40_000, encoding="utf-8")
    args = tr.build_arg_parser().parse_args(
        [str(json_path), "--context-file", str(ctx_file)]
    )
    assert args.context_file == str(ctx_file)
    assert tr.resolve_translation_context(args.context, args.context_file) == "x" * 40_000
