#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Doctor should import chat_parser instead of exec'ing burn module source."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def test_doctor_source_imports_chat_parser_not_exec_burn():
    text = (SCRIPTS / "render_cn_chat.py").read_text(encoding="utf-8")
    assert "from chat_parser import parse_chat_html" in text
    assert 'exec(compile(code, str(burn_path), "exec"), glb)' not in text
    assert 'spec_from_file_location("_doctor_burn"' not in text
