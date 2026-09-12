#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""perf-5 / perf-3 修复护栏测试。

- performance-5: export_review_xlsx 样式对象提升（hoist）后，导出语义不变
  —— 值回读、FAIL/WARN 行填充、original/translation 列 '@' 文本格式、
  表头字体/填充全部保持。
- performance-3: chat_data.json 与翻译导出 JSON 改为紧凑序列化
  （separators=(",", ":")、无 indent）——机器消费面（json.load）对空白
  不敏感；人工编辑面是 TSV/XLSX 复核表，不受影响。
"""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path

import pytest

from helpers import load_module

openpyxl = pytest.importorskip("openpyxl")

import review_tables  # noqa: E402  (scripts 已在 conftest/sys.path)
import translation_io  # noqa: E402


def _write_review_json(path: Path):
    data = {
        "messages": [
            {
                "index": 0,
                "timestamp": "00:00:01",
                "author": "alice",
                "original": "2024-01-02",
                "translation": "已译内容",
            },
            {
                "index": 1,
                "timestamp": "00:00:02",
                "author": "bob",
                "original": "=1+1",
                "translation": "",
            },
            {
                "index": 2,
                "timestamp": "00:00:03",
                "author": "carol",
                "original": "plain",
                "translation": "warn 行",
            },
        ]
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def _export_quiet(json_path: Path, xlsx_path: Path):
    """导出并吞掉 print 输出（导出函数本身带 print）。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        review_tables.export_review_xlsx(json_path, xlsx_path, issue_map={})


def test_xlsx_export_roundtrip_values_fills_and_text_formats(tmp_path: Path):
    """样式 hoist 不得改变导出语义：值/填充/'@' 文本格式/表头样式。"""
    json_path = tmp_path / "trans.json"
    _write_review_json(json_path)
    xlsx_path = tmp_path / "review.xlsx"
    _export_quiet(json_path, xlsx_path)
    assert xlsx_path.is_file()
    assert not list(tmp_path.glob("*.tmp.xlsx")), "不得残留临时文件"

    wb = openpyxl.load_workbook(xlsx_path)
    ws = wb.active
    assert ws.title == "review"
    header = [ws.cell(row=1, column=i).value for i in range(1, 9)]
    assert header[:5] == ["index", "timestamp", "author", "original", "translation"]

    # 值回读（含防注入前缀的 original）
    assert ws.cell(row=2, column=1).value == 0
    assert ws.cell(row=2, column=4).value == "2024-01-02"
    assert ws.cell(row=2, column=5).value == "已译内容"
    assert str(ws.cell(row=3, column=4).value).startswith("'")  # "=1+1" 防注入

    # 类型漂移防护：original/translation 列按文本存储
    for row_no in range(2, ws.max_row + 1):
        assert ws.cell(row=row_no, column=4).number_format == "@"
        assert ws.cell(row=row_no, column=5).number_format == "@"

    # FAIL/WARN 行填充仍按 severity 应用（fail=F8CBAD / warn=FFE699）：
    # 行 1/2 无 lint 数据（issue_map={}）→ severity 空 → 无主题填充；
    # 这里构造带 lint 语义的导出验证 FAIL/WARN 分支。
    issue_map = {
        0: {"severity": "FAIL", "codes": ["empty_translation"], "notes": ["x"]},
        2: {"severity": "WARN", "codes": ["too_long"], "notes": ["y"]},
    }
    xlsx2 = tmp_path / "review2.xlsx"
    buf = io.StringIO()
    with redirect_stdout(buf):
        review_tables.export_review_xlsx(json_path, xlsx2, issue_map=issue_map)
    ws2 = openpyxl.load_workbook(xlsx2).active
    assert "F8CBAD" in str(ws2.cell(row=2, column=6).fill.start_color.rgb)
    assert "FFE699" in str(ws2.cell(row=4, column=6).fill.start_color.rgb)
    assert str(ws2.cell(row=3, column=6).fill.start_color.rgb or "") in ("00000000", "None")

    # 表头样式保持（Arial 粗体 + 蓝色填充 + 居中）
    assert ws.cell(row=1, column=1).font.bold is True
    assert ws.cell(row=1, column=1).font.name == "Arial"
    assert "D9EAF7" in str(ws.cell(row=1, column=1).fill.start_color.rgb)

    # 冻结表头 / 自动筛选 / 行高
    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref == ws.dimensions
    assert ws.row_dimensions[2].height == 36

    # 导入回写往返：编辑 XLSX 的 translation 列后回写 JSON 不丢
    # （导入方向是 XLSX -> JSON；这正是复核场景中人工编辑复核表的动作）。
    wb_edit = openpyxl.load_workbook(xlsx_path)
    ws_edit = wb_edit.active
    ws_edit.cell(row=2, column=5).value = "改后译文"
    wb_edit.save(xlsx_path)
    buf = io.StringIO()
    with redirect_stdout(buf):
        review_tables.import_review_xlsx(json_path, xlsx_path)
    assert json.loads(json_path.read_text(encoding="utf-8"))["messages"][0]["translation"] == "改后译文"


def test_xlsx_body_font_and_alignment_shared(tmp_path: Path):
    """正文单元格仍获得 Arial + wrap_text 样式（hoist 后逐格赋值保持）。"""
    json_path = tmp_path / "trans.json"
    _write_review_json(json_path)
    xlsx_path = tmp_path / "review.xlsx"
    _export_quiet(json_path, xlsx_path)
    ws = openpyxl.load_workbook(xlsx_path).active
    for row_no in range(2, ws.max_row + 1):
        for col in (1, 3, 4, 5):
            cell = ws.cell(row=row_no, column=col)
            assert cell.font.name == "Arial"
            assert cell.alignment.wrap_text is True
            assert cell.alignment.vertical == "top"


def test_chat_parser_writes_compact_json(tmp_path: Path):
    """chat_data.json 紧凑序列化：无 indent 换行，json.load 结构不变。"""
    from helpers import FIXTURES_DIR

    parser = load_module("chat_parser", "chat_parser.py")
    out_dir = tmp_path / "out"
    data = parser.parse_chat_html(str(FIXTURES_DIR / "td_minified.html"), str(out_dir))
    assert isinstance(data, dict) and "messages" in data
    raw = (out_dir / "chat_data.json").read_text(encoding="utf-8")
    # 紧凑：不含 indent 产生的换行缩进
    assert "\n  " not in raw
    loaded = json.loads(raw)
    assert loaded == data
    assert list(loaded.keys()) == ["messages", "emote_map"]


def test_export_translation_json_is_compact(tmp_path: Path):
    """翻译导出 JSON 紧凑序列化：调用写入函数后检查落盘字节无 indent。"""
    chat = {
        "messages": [
            {
                "author": "a",
                "timestamp": 1.0,
                "stream_timestamp": 1.0,
                "fragments": [{"type": "text", "text": ": hi"}],
            }
        ]
    }
    path = tmp_path / "export.json"
    payload = translation_io.write_export_translation_json(path, chat, force=True)
    raw = path.read_text(encoding="utf-8")
    # 紧凑：无 indent 的换行缩进，且无 ", "/": " 默认分隔符空格
    assert "\n" not in raw
    assert '", "' not in raw and '": ' not in raw
    assert json.loads(raw) == payload
    assert payload["schema_version"] == 2
    assert payload["messages"][0]["translation"] == ""


def test_compact_json_equivalent_to_pretty(tmp_path: Path):
    """紧凑与 indent 序列化在 json.load 后等价（机器消费面回归护栏）。"""
    chat = {
        "messages": [
            {"author": "a", "timestamp": 1.0, "stream_timestamp": 1.0,
             "fragments": [{"type": "text", "text": "héllo 中文"}]}
        ]
    }
    payload = translation_io.build_export_translation_payload(chat)
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    pretty = json.dumps(payload, ensure_ascii=False, indent=2)
    assert json.loads(compact) == json.loads(pretty)


# ---------------------------------------------------------------------------
# performance-2: export_review_xlsx 改 write_only 流式写出
# ---------------------------------------------------------------------------


def test_xlsx_export_write_only_roundtrip_small_table(tmp_path: Path):
    """write_only 导出小表可完整读回：标题/行数/表头/值不变。"""
    json_path = tmp_path / "trans.json"
    _write_review_json(json_path)
    xlsx_path = tmp_path / "review.xlsx"
    _export_quiet(json_path, xlsx_path)

    wb = openpyxl.load_workbook(xlsx_path)
    ws = wb.active
    assert ws.title == "review"
    assert ws.max_row == 4  # 表头 + 3 行消息
    assert [ws.cell(row=1, column=i).value for i in range(1, 6)] == [
        "index", "timestamp", "author", "original", "translation",
    ]
    assert ws.cell(row=4, column=1).value == 2
    assert ws.cell(row=4, column=5).value == "warn 行"


def test_xlsx_row_height_skipped_over_threshold(tmp_path: Path, monkeypatch):
    """行高仅在小表保留：注入小阈值后逐行 row_dimensions 跳过，其余样式保持。"""
    json_path = tmp_path / "trans.json"
    _write_review_json(json_path)  # 3 行数据 + 表头 = 4 行
    monkeypatch.setattr(review_tables, "_XLSX_ROW_HEIGHT_LIMIT", 2)
    xlsx_path = tmp_path / "review.xlsx"
    _export_quiet(json_path, xlsx_path)

    ws = openpyxl.load_workbook(xlsx_path).active
    assert not ws.row_dimensions, "超阈值不得写逐行行高"
    # write_only 支持的属性保持：冻结窗格 / 列宽。
    assert ws.freeze_panes == "A2"
    assert ws.column_dimensions["A"].width == 8
    assert ws.cell(row=2, column=5).number_format == "@"
