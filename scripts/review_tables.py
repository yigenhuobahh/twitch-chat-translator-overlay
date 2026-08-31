#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人工复核表（TSV/XLSX）导出导入与翻译质检（lint）。

从 render_cn_chat.py 原样搬出（搬运而非重写）：本模块保持纯函数 —— 不读
模块级可变全局；dry-run 与日志经显式参数注入，由调用方（render_cn_chat 的
薄包装）传入 `dry_run=DRY_RUN, log=log`，以保留测试按模块属性 monkeypatch
DRY_RUN 的语义。render_cn_chat 对这些名字保持 re-export/包装，外部签名不变。
"""

from __future__ import annotations

import json
from pathlib import Path
import re

from common_utils import atomic_write_json


def _review_issue_map(json_path: Path, max_chars: int = 90, data: dict | None = None, lint_fn=None):
    """Map message index -> (severity, codes, notes) from lint, without printing a full report.

    data: 调用方已解析好的翻译 JSON；缺省 None 时自行读盘解析（失败仅告警返回空表）。
    lint_fn: 缺省用本模块 lint_translation；render_cn_chat 的包装传入自己的
    （可能被测试 monkeypatch 的）lint_translation，保持历史 spy 语义。
    """
    if data is None:
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(f"[WARN] 复核表 lint 跳过：找不到 {json_path}", flush=True)
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError) as e:
            # Do not fail export on bad JSON here; surface why lint columns are empty.
            print(f"[WARN] 复核表 lint 跳过：无法解析 {json_path}: {e}", flush=True)
            return {}
    # Reuse lint rules quietly; suppress console noise via stdout redirect.
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            issues = (lint_fn or lint_translation)(json_path, report_path=None, max_chars=max_chars, data=data)
        except SystemExit:
            issues = []
        except Exception as e:
            print(f"[WARN] 复核表 lint 失败: {e}", flush=True)
            issues = []
    by_index: dict = {}
    for issue in issues:
        idx = issue.get("index")
        bucket = by_index.setdefault(idx, {"severity": "OK", "codes": [], "notes": []})
        sev = issue.get("severity", "WARN")
        if sev == "FAIL" or bucket["severity"] != "FAIL":
            if sev == "FAIL":
                bucket["severity"] = "FAIL"
            elif bucket["severity"] != "FAIL":
                bucket["severity"] = sev
        code = str(issue.get("code", ""))
        if code:
            bucket["codes"].append(code)
        note = str(issue.get("message", ""))
        if note:
            bucket["notes"].append(note)
    return by_index


def _review_rows(
    json_path: Path,
    include_lint: bool = True,
    max_chars: int = 90,
    data: dict | None = None,
    issue_map: dict | None = None,
):
    if data is None:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    if include_lint:
        if issue_map is None:
            issue_map = _review_issue_map(json_path, max_chars=max_chars, data=data)
    else:
        issue_map = {}
    rows = []
    for msg in data.get("messages", []):
        idx = msg.get("index", "")
        info = issue_map.get(idx) or issue_map.get(str(idx)) or {}
        severity = info.get("severity", "OK") if include_lint else ""
        codes = ",".join(info.get("codes") or []) if include_lint else ""
        notes = " | ".join(info.get("notes") or []) if include_lint else ""
        rows.append([
            idx,
            msg.get("timestamp", ""),
            msg.get("author", ""),
            str(msg.get("original", "")).replace("\t", " ").replace("\r", " ").replace("\n", " "),
            str(msg.get("translation", "")).replace("\t", " ").replace("\r", " ").replace("\n", " "),
            severity,
            codes,
            notes,
        ])
    return rows


def _prepare_review_export(json_path: Path, max_chars: int = 90, lint_fn=None) -> tuple[dict, dict]:
    """翻译 JSON 只解析一次、lint 只跑一次；结果供 TSV/XLSX 两个导出函数共用。"""
    data = json.loads(json_path.read_text(encoding="utf-8"))
    issue_map = _review_issue_map(json_path, max_chars=max_chars, data=data, lint_fn=lint_fn)
    return data, issue_map


def export_review_tsv(
    json_path: Path,
    review_path: Path,
    *,
    data: dict | None = None,
    issue_map: dict | None = None,
    dry_run: bool = False,
    log=None,
):
    """导出人工复核 TSV。translation 列可直接编辑后再导入。

    data/issue_map: 由 _prepare_review_export 预计算传入（JSON/lint 只算一次）；
    缺省时函数内部自行计算。
    """
    if dry_run:
        (log or print)(f"[dry-run] 跳过复核表 TSV 写出: {review_path}")
        return
    lines = ["index\ttimestamp\tauthor\toriginal\ttranslation\tlint_severity\tlint_codes\tlint_notes"]
    for row in _review_rows(json_path, include_lint=True, data=data, issue_map=issue_map):
        lines.append("\t".join(map(str, row)))
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    print(f"\n[人工复核] 已导出中英对照 TSV: {review_path}")


def export_review_xlsx(
    json_path: Path,
    review_path: Path,
    *,
    data: dict | None = None,
    issue_map: dict | None = None,
    dry_run: bool = False,
    log=None,
):
    """导出带列宽、换行和冻结表头的人工复核 XLSX。

    data/issue_map: 与 export_review_tsv 相同的预计算入参；dry-run 下不写出。
    """
    if dry_run:
        (log or print)(f"[dry-run] 跳过复核表 XLSX 写出: {review_path}")
        return
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError as e:
        raise SystemExit("错误: 导出 XLSX 需要 openpyxl，请先运行 python -m pip install openpyxl") from e

    wb = Workbook()
    ws = wb.active
    ws.title = "review"
    header = ["index", "timestamp", "author", "original", "translation", "lint_severity", "lint_codes", "lint_notes"]
    ws.append(header)
    for row in _review_rows(json_path, include_lint=True, data=data, issue_map=issue_map):
        ws.append(row)

    header_fill = PatternFill("solid", fgColor="D9EAF7")
    fail_fill = PatternFill("solid", fgColor="F8CBAD")
    warn_fill = PatternFill("solid", fgColor="FFE699")
    for cell in ws[1]:
        cell.font = Font(name="Arial", bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    widths = {"A": 8, "B": 10, "C": 20, "D": 50, "E": 50, "F": 12, "G": 24, "H": 40}
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = Font(name="Arial")
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        row[3].alignment = Alignment(vertical="top", wrap_text=True)
        row[4].alignment = Alignment(vertical="top", wrap_text=True)
        sev = str(row[5].value or "").upper()
        if sev == "FAIL":
            row[5].fill = fail_fill
        elif sev == "WARN":
            row[5].fill = warn_fill

    for idx in range(2, ws.max_row + 1):
        ws.row_dimensions[idx].height = 36

    review_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(review_path)
    print(f"[人工复核] 已导出更适合 Excel/WPS 的 XLSX: {review_path}")


def import_review_xlsx(json_path: Path, review_path: Path, *, dry_run: bool = False):
    """把人工复核 XLSX 的 translation 列回写到 JSON。"""
    if dry_run:
        print(f"[dry-run] 跳过 XLSX 回写: {review_path} -> {json_path}")
        return
    try:
        from openpyxl import load_workbook
    except ImportError as e:
        raise SystemExit("错误: 读取 XLSX 需要 openpyxl，请先运行 python -m pip install openpyxl") from e
    if not review_path.is_file():
        raise SystemExit(f"错误: 找不到人工复核文件: {review_path}")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    by_index = {int(m.get("index")): m for m in data.get("messages", []) if str(m.get("index", "")).isdigit()}
    wb = load_workbook(review_path)
    ws = wb.active
    header = [ws.cell(row=1, column=i).value for i in range(1, 9)]
    required = ["index", "timestamp", "author", "original", "translation"]
    if header[:5] != required:
        # Backward compatible with old 5-column review sheets.
        header5 = [ws.cell(row=1, column=i).value for i in range(1, 6)]
        if header5 != required:
            raise SystemExit(
                "错误: XLSX 表头不匹配，请保持 index/timestamp/author/original/translation 五列"
                "（可选附加 lint_severity/lint_codes/lint_notes）"
            )
    changed = 0
    for row_no in range(2, ws.max_row + 1):
        idx_value = ws.cell(row=row_no, column=1).value
        if idx_value is None:
            continue
        try:
            idx = int(idx_value)
        except ValueError:
            print(f"警告: 第 {row_no} 行 index 非数字，已跳过")
            continue
        if idx not in by_index:
            print(f"警告: 第 {row_no} 行 index={idx} 不存在，已跳过")
            continue
        raw_cell = ws.cell(row=row_no, column=5).value
        translation = str(raw_cell or "").strip()
        # Empty cells must not wipe existing non-empty translations on writeback.
        existing = str(by_index[idx].get("translation", "") or "").strip()
        if not translation and existing:
            continue
        if by_index[idx].get("translation") != translation:
            by_index[idx]["translation"] = translation
            changed += 1
    atomic_write_json(json_path, data)
    print(f"\n[人工复核] 已从 XLSX 回写 {changed} 条修改到: {json_path}")


def import_review_tsv(json_path: Path, review_path: Path, *, dry_run: bool = False):
    """把人工复核 TSV 的 translation 列回写到 JSON。"""
    if dry_run:
        print(f"[dry-run] 跳过 TSV 回写: {review_path} -> {json_path}")
        return
    if not review_path.is_file():
        raise SystemExit(f"错误: 找不到人工复核文件: {review_path}")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    by_index = {int(m.get("index")): m for m in data.get("messages", []) if str(m.get("index", "")).isdigit()}
    lines = review_path.read_text(encoding="utf-8-sig").splitlines()
    if not lines:
        raise SystemExit(f"错误: 人工复核文件为空: {review_path}")
    header = lines[0].split("\t")
    if len(header) < 5 or header[:5] != ["index", "timestamp", "author", "original", "translation"]:
        raise SystemExit(
            "错误: TSV 表头不匹配，请保持 index/timestamp/author/original/translation 五列"
            "（可选附加 lint_severity/lint_codes/lint_notes）"
        )
    changed = 0
    for line_no, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            print(f"警告: 第 {line_no} 行列数不足，已跳过")
            continue
        try:
            idx = int(parts[0])
        except ValueError:
            print(f"警告: 第 {line_no} 行 index 非数字，已跳过")
            continue
        if idx not in by_index:
            print(f"警告: 第 {line_no} 行 index={idx} 不存在，已跳过")
            continue
        translation = parts[4].strip()
        # Empty cells must not wipe existing non-empty translations on writeback.
        existing = str(by_index[idx].get("translation", "") or "").strip()
        if not translation and existing:
            continue
        if by_index[idx].get("translation") != translation:
            by_index[idx]["translation"] = translation
            changed += 1
    atomic_write_json(json_path, data)
    print(f"\n[人工复核] 已从 TSV 回写 {changed} 条修改到: {json_path}")


LINT_URL_RE = re.compile(r"https?://\S+")
LINT_MENTION_RE = re.compile(r"@[A-Za-z0-9_]+")
LINT_BRACKET_TOKEN_RE = re.compile(r"\[[^\]]+\]")
# 消歧空白：旧写法 (?:\s*\[[^\]]+\]\s*)+ 的 \s* 与 [^\]]+（可吞空格）存在歧义，
# 对"尾部未闭合 token"的输入会灾难性回溯（n=26 约 5 秒、n=30 约 81 秒）。
# 改为 token 之间必须出现显式分隔空白，回溯线性，语义不变（纯 [emote] 序列，
# 含多空格分隔与首尾空白）。注意：此处与 translate_chat_openai 的 PURE_PRESERVE_RE
# 规则不同，不要互相照抄。
LINT_PURE_EMOTE_RE = re.compile(r"^\s*\[[^\]]+\](?:\s+\[[^\]]+\])*\s*$")


def _lint_issue(issues, idx, code, message, severity="WARN", original="", translation=""):
    issues.append({
        "index": idx,
        "severity": severity,
        "code": code,
        "message": message,
        "original": original,
        "translation": translation,
    })


def lint_translation(
    json_path: Path,
    report_path: Path | None = None,
    max_chars: int = 90,
    max_ratio: float = 2.8,
    data: dict | None = None,
    dry_run: bool = False,
):
    """检查翻译 JSON 中的常见可疑问题，返回 issue 列表。

    data: 调用方已解析好的翻译 JSON（复核表导出等场景避免同一文件反复读盘解析）；
    缺省 None 时从 json_path 读取。对外位置签名保持兼容。
    """
    if not json_path.is_file():
        raise SystemExit(f"错误: 翻译 JSON 不存在: {json_path}")
    if data is None:
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise SystemExit(f"错误: JSON 解析失败: {json_path}: {e}")

    issues = []
    messages = data.get("messages")
    if not isinstance(messages, list):
        _lint_issue(issues, "", "schema_missing_messages", "顶层字段 messages 缺失或不是数组", "FAIL")
        messages = []

    seen_indexes = set()
    for pos, msg in enumerate(messages):
        if not isinstance(msg, dict):
            _lint_issue(issues, pos, "schema_message_not_object", "消息条目不是对象", "FAIL")
            continue

        idx = msg.get("index", pos)
        original = str(msg.get("original", ""))
        translation = str(msg.get("translation", "")) if msg.get("translation") is not None else ""
        original_s = original.strip()
        translation_s = translation.strip()

        for required in ["index", "original", "translation"]:
            if required not in msg:
                _lint_issue(issues, idx, "schema_missing_field", f"缺少字段: {required}", "FAIL", original, translation)

        if idx in seen_indexes:
            _lint_issue(issues, idx, "duplicate_index", "index 重复", "FAIL", original, translation)
        seen_indexes.add(idx)

        if not translation_s:
            _lint_issue(issues, idx, "empty_translation", "translation 为空", "FAIL", original, translation)
            continue

        is_pure_emote = bool(LINT_PURE_EMOTE_RE.fullmatch(original_s))
        if is_pure_emote and translation_s != original_s:
            _lint_issue(issues, idx, "pure_emote_changed", "纯 emote 消息应保持原样", "WARN", original, translation)

        original_mentions = set(LINT_MENTION_RE.findall(original))
        missing_mentions = sorted(m for m in original_mentions if m not in translation)
        if missing_mentions:
            _lint_issue(issues, idx, "mention_lost", "翻译丢失 @用户名: " + ", ".join(missing_mentions), "WARN", original, translation)

        original_urls = set(LINT_URL_RE.findall(original))
        missing_urls = sorted(u for u in original_urls if u not in translation)
        if missing_urls:
            _lint_issue(issues, idx, "url_lost", "翻译丢失 URL: " + ", ".join(missing_urls), "WARN", original, translation)

        original_brackets = set(LINT_BRACKET_TOKEN_RE.findall(original))
        missing_brackets = sorted(b for b in original_brackets if b not in translation)
        if missing_brackets:
            _lint_issue(issues, idx, "bracket_token_lost", "翻译丢失方括号 token/emote: " + ", ".join(missing_brackets), "WARN", original, translation)

        if not is_pure_emote and len(translation_s) > max_chars:
            _lint_issue(issues, idx, "too_long", f"翻译超过 {max_chars} 字，可能不适合弹幕显示", "WARN", original, translation)
        elif not is_pure_emote and original_s and len(translation_s) / max(1, len(original_s)) > max_ratio and len(translation_s) > 24:
            _lint_issue(issues, idx, "expansion_ratio_high", f"翻译长度超过原文 {max_ratio:.1f} 倍", "WARN", original, translation)

    fail_count = sum(1 for issue in issues if issue["severity"] == "FAIL")
    warn_count = sum(1 for issue in issues if issue["severity"] == "WARN")
    print(f"\n[翻译质检] 文件: {json_path}")
    print(f"  消息数: {len(messages)}")
    print(f"  FAIL: {fail_count}, WARN: {warn_count}")

    if issues:
        for issue in issues[:80]:
            print(f"  [{issue['severity']}] #{issue['index']} {issue['code']}: {issue['message']}")
        if len(issues) > 80:
            print(f"  ... 还有 {len(issues) - 80} 条未显示")
    else:
        print("  未发现确定性规则问题。")

    if report_path:
        if dry_run:
            # dry-run 不落盘：质检报告写出统一在写侧拦截（lint 计算本身只读，照常执行）。
            print(f"  [dry-run] 跳过质检报告写出: {report_path}")
        else:
            lines = ["index\tseverity\tcode\tmessage\toriginal\ttranslation"]
            for issue in issues:
                row = [
                    str(issue["index"]),
                    issue["severity"],
                    issue["code"],
                    issue["message"],
                    str(issue.get("original", "")).replace("\t", " ").replace("\r", " ").replace("\n", " "),
                    str(issue.get("translation", "")).replace("\t", " ").replace("\r", " ").replace("\n", " "),
                ]
                lines.append("\t".join(row))
            report_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
            print(f"  质检报告: {report_path}")

    return issues
