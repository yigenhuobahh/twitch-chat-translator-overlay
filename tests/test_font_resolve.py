#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from pathlib import Path
import sys
from unittest import mock

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

# tests-5: 镜像 common_utils.detect_cjk_font 的三平台候选表(common_utils.py)。
# 这里刻意写死一份,用于钉住各平台候选探测顺序——调整真实候选表必须是
# 有意识的、测试可见的变更。
_PLATFORM_FONT_CANDIDATES = {
    "Windows": [
        (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc"),
        (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyhbd.ttc"),
        (r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simhei.ttf"),
        (r"C:\Windows\Fonts\simsun.ttc", r"C:\Windows\Fonts\simsun.ttc"),
        (r"C:\Windows\Fonts\msjh.ttc", r"C:\Windows\Fonts\msjhbd.ttc"),
    ],
    "Darwin": [
        ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/PingFang.ttc"),
        (
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
        ),
        ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
    ],
    "Linux": [
        (
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        ),
        (
            "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
            "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
        ),
        (
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
        ),
        (
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        ),
        (
            "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Regular.otf",
            "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Bold.otf",
        ),
        (
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        ),
        (
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        ),
        (
            "/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc",
            "/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc",
        ),
        (
            "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
            "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
        ),
    ],
}


def _install_fake_font_fs(monkeypatch, *, existing: set[str]) -> list[str]:
    """Stub os.path.isfile to record the probed candidate sequence.

    detect_cjk_font 经模块级 ``import os`` 使用全局 os.path,因此 patch 点是
    os.path.isfile(monkeypatch teardown 还原);同时把 isdir 短路为 False,
    关闭 last-resort 目录扫描,使断言只覆盖候选表语义。
    """
    probed: list[str] = []

    def fake_isfile(path) -> bool:
        probed.append(str(path))
        return str(path) in existing

    monkeypatch.setattr(os.path, "isfile", fake_isfile)
    monkeypatch.setattr(os.path, "isdir", lambda _p: False)
    return probed


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


@pytest.mark.parametrize("system", ["Windows", "Darwin", "Linux"])
def test_detect_cjk_font_probe_order_matches_platform_table(monkeypatch, system):
    """tests-5: 三平台强制分支——候选表按声明顺序探测,与宿主 OS 无关。"""
    import common_utils
    from common_utils import detect_cjk_font

    table = _PLATFORM_FONT_CANDIDATES[system]
    # common_utils 顶部 `import platform` 后经 platform.system() 分派,
    # monkeypatch 该绑定即可强制平台分支(teardown 还原全局 platform)。
    monkeypatch.setattr(common_utils.platform, "system", lambda: system)
    probed = _install_fake_font_fs(monkeypatch, existing=set())

    reg, bold = detect_cjk_font()

    # 无一命中:按序探测每个候选的 regular 路径(bold 只在 regular 命中后探测),
    # 之后进入 last-resort 扫描——isdir 已被短路,扫描立即为空。
    assert probed == [reg_path for reg_path, _bold in table]
    assert reg is None and bold is None


@pytest.mark.parametrize("system", ["Windows", "Darwin", "Linux"])
def test_detect_cjk_font_bold_missing_falls_back_to_regular(monkeypatch, system):
    """tests-5: regular 命中而 bold 缺失 → 返回 (regular, regular)(common_utils.py:703)。"""
    import common_utils
    from common_utils import detect_cjk_font

    first_reg, first_bold = _PLATFORM_FONT_CANDIDATES[system][0]
    monkeypatch.setattr(common_utils.platform, "system", lambda: system)
    probed = _install_fake_font_fs(monkeypatch, existing={first_reg})

    reg, bold = detect_cjk_font()

    # 只探测首对:regular 命中后立即探测其 bold,不再继续后续候选。
    assert probed == [first_reg, first_bold]
    assert reg == first_reg
    # bold 缺失 → 回退 regular(Darwin 各候选对 bold==regular,回退退化为
    # 恒等,断言仍必须成立)。
    assert bold == reg
