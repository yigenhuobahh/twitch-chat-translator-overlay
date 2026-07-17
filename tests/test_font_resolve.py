#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from pathlib import Path
import sys
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def test_resolve_font_paths_uses_existing_file(tmp_path: Path):
    from common_utils import resolve_font_paths

    fake = tmp_path / "FakeCJK.ttf"
    fake.write_bytes(b"0")
    reg, bold = resolve_font_paths(str(fake), str(fake))
    assert reg == str(fake)
    assert bold == str(fake)


def test_resolve_font_paths_auto_raises_when_missing():
    from common_utils import resolve_font_paths

    with mock.patch("common_utils.detect_cjk_font", return_value=(None, None)):
        try:
            resolve_font_paths("auto", "auto")
        except FileNotFoundError as e:
            assert "CJK" in str(e) or "font" in str(e).lower()
        else:
            raise AssertionError("expected FileNotFoundError")


def test_detect_cjk_font_never_returns_missing_path():
    from common_utils import detect_cjk_font

    reg, bold = detect_cjk_font()
    if reg is not None:
        assert os.path.isfile(reg)
    if bold is not None:
        assert os.path.isfile(bold)
