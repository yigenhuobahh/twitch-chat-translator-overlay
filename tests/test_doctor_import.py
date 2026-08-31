#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Doctor should import chat_parser instead of exec'ing burn module source."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def test_doctor_source_imports_chat_parser_not_exec_burn():
    # doctor 实现位于 doctor_check.py（自 render_cn_chat 搬出）；编排层与其自身
    # 都不得 exec 编译 burn 源码。
    text = "\n".join(
        (SCRIPTS / name).read_text(encoding="utf-8")
        for name in ("render_cn_chat.py", "doctor_check.py")
    )
    assert "from chat_parser import parse_chat_html" in text
    assert 'exec(compile(code, str(burn_path), "exec"), glb)' not in text
    assert 'spec_from_file_location("_doctor_burn"' not in text
