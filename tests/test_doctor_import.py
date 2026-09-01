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
    # 这是"不得 exec 源码"契约的唯一正典断言（原先散落在 test_max_matrix 与
    # test_ux_init_doctor 的重复版本已收敛至此），勿再复制到其他文件。
    text = "\n".join(
        (SCRIPTS / name).read_text(encoding="utf-8")
        for name in ("render_cn_chat.py", "doctor_check.py")
    )
    assert "from chat_parser import parse_chat_html" in text
    assert "from chat_parser import" in text or "import chat_parser" in text
    # 精确串是历史回归形态；广义版覆盖任何 exec(compile 变体。
    assert 'exec(compile(code, str(burn_path), "exec"), glb)' not in text
    assert "exec(compile" not in text
    assert 'spec_from_file_location("_doctor_burn"' not in text
