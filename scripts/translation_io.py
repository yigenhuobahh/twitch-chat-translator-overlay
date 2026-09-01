#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Translation export / import JSON round-trip.

Extracted verbatim from twitch_chat_burn for maintainability: export
payload building (stream-absolute timestamps for stable identity),
non-destructive JSON writes, and identity-checked import onto parsed
chat messages."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re

from translation_support import clean_translation_text as clean_imported_translation


def _normalize_import_identity_text(text):
    """Collapse whitespace for import identity comparisons."""
    text = str(text or "").replace("\r", " ").replace("\n", " ")
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def message_export_original(message):
    """Rebuild the export-time original text for one chat message."""
    parts = []
    for frag in message.get("fragments") or []:
        if frag.get("type") == "text":
            parts.append(str(frag.get("text", "") or ""))
        else:
            parts.append(f'[{frag.get("title", "emote")}]')
    original_text = " ".join(parts)
    if original_text.startswith(": "):
        original_text = original_text[2:]
    return original_text


def _message_stream_timestamp(message: dict) -> float:
    """Stream-absolute timestamp for export/import identity (pre-offset when available)."""
    if message.get("stream_timestamp") is not None:
        try:
            return float(message["stream_timestamp"])
        except (TypeError, ValueError):
            pass
    try:
        return float(message.get("timestamp", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def translation_json_nonempty_count(path: str | Path) -> int:
    """How many rows already have a non-empty translation field (0 if missing/unreadable)."""
    p = Path(path)
    if not p.is_file():
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return 0
    items = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return 0
    n = 0
    for item in items:
        if isinstance(item, dict) and str(item.get("translation", "") or "").strip():
            n += 1
    return n


def build_export_translation_payload(
    chat_data: dict,
    *,
    offset_info: dict | None = None,
) -> dict:
    """Build export JSON using stream-absolute timestamps for stable identity."""
    offset_info = offset_info or {}
    try:
        applied_offset = float(offset_info.get("offset") or 0.0)
    except (TypeError, ValueError):
        applied_offset = 0.0
    items = []
    for i, m in enumerate(chat_data.get("messages") or []):
        original_text = message_export_original(m)
        stream_ts = _message_stream_timestamp(m)
        items.append({
            "index": i,
            # Stream-absolute time (broadcast timeline). Import matches this field
            # so changing --offset between export and burn does not mass-skip rows.
            "timestamp": round(stream_ts, 1),
            "stream_timestamp": round(stream_ts, 1),
            "author": m.get("author"),
            "original": original_text,
            "translation": "",
        })
    return {
        "schema_version": 2,
        "time_base": "stream",
        "export_offset": applied_offset,
        "offset_mode": offset_info.get("mode"),
        "messages": items,
    }


def write_export_translation_json(
    export_path: str | Path,
    chat_data: dict,
    *,
    offset_info: dict | None = None,
    force: bool = False,
) -> dict:
    """Write translation export JSON. Refuses to wipe non-empty translations unless force."""
    export_path = Path(export_path)
    existing_n = translation_json_nonempty_count(export_path)
    if existing_n > 0 and not force:
        raise FileExistsError(
            f"翻译 JSON 已有 {existing_n} 条非空 translation，拒绝覆盖以免丢失译文: {export_path}\n"
            f"  复用请加 --reuse-translation；确需重新导出请加 --force-export"
        )
    payload = build_export_translation_payload(chat_data, offset_info=offset_info)
    export_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = export_path.with_suffix(export_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, export_path)
    return payload


def apply_imported_translations(chat_data, trans_data, strict=False):
    """
    Apply translation JSON onto parsed chat messages by stable export index.

    Export uses list position at export time as index. Import matches that
    index field (not a re-enumerate of a possibly reordered list), and when
    available cross-checks author/timestamp/original to catch silent mismatch.

    Timestamp identity uses stream-absolute time when available (export schema
    v2 / stream_timestamp), so re-burning with a different --offset does not
    mass-skip translations.

    On identity mismatch: skip applying that row by default; with strict=True
    raise ValueError after collecting mismatches.
    Returns (replaced, stripped_placeholders, warnings).
    """
    messages = chat_data.get("messages") or []
    items = trans_data.get("messages") if isinstance(trans_data, dict) else None
    if not isinstance(items, list):
        raise ValueError("翻译 JSON 缺少 messages 数组")

    trans_map = {}
    dup_indexes: list[int] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item["index"])
        except (KeyError, TypeError, ValueError):
            continue
        if idx in trans_map:
            dup_indexes.append(idx)
        trans_map[idx] = item

    warnings = []
    if dup_indexes:
        uniq = sorted(set(dup_indexes))
        preview = uniq[:20]
        more = "" if len(uniq) <= 20 else f" ... (+{len(uniq) - 20} more)"
        warnings.append(
            f"翻译 JSON 含重复 index（后写覆盖先写）: {preview}{more}"
        )
    if len(messages) != len(trans_map) and len(trans_map) > 0:
        warnings.append(
            f"翻译条数 ({len(trans_map)}) 与当前解析消息数 ({len(messages)}) 不一致；"
            f"将按 index 对齐，可能有漏贴/错贴风险"
        )

    replaced = 0
    stripped_placeholders = 0
    mismatch_count = 0
    dropped_empty_translations = 0
    for i, m in enumerate(messages):
        item = trans_map.get(i)
        if not item:
            continue
        raw_tr = str(item.get("translation", "") or "").strip()
        if not raw_tr:
            continue

        # Identity checks: author / stream timestamp / original (normalized whitespace).
        mismatch_reasons = []
        exp_author = item.get("author")
        if exp_author is not None and str(exp_author) != str(m.get("author", "")):
            mismatch_reasons.append(
                f"作者不一致: 翻译 JSON={exp_author!r} HTML={m.get('author')!r}"
            )
        # Prefer stream-absolute times (schema v2) so offset changes do not break identity.
        # Legacy exports stored post-offset video-relative timestamps only.
        exp_stream = item.get("stream_timestamp")
        exp_ts = exp_stream if exp_stream is not None else item.get("timestamp")
        if exp_ts is not None:
            time_base = ""
            if isinstance(trans_data, dict):
                time_base = str(trans_data.get("time_base") or "").strip().lower()
            use_stream = time_base == "stream" or exp_stream is not None
            if use_stream:
                html_ts = _message_stream_timestamp(m)
            else:
                try:
                    html_ts = float(m.get("timestamp", 0) or 0)
                except (TypeError, ValueError):
                    html_ts = 0.0
            try:
                if abs(float(exp_ts) - float(html_ts)) > 0.51:
                    label = "stream" if use_stream else "video-relative"
                    mismatch_reasons.append(
                        f"时间戳不一致({label}): 翻译 JSON={exp_ts} HTML={html_ts}"
                    )
            except (TypeError, ValueError):
                mismatch_reasons.append(
                    f"时间戳无法解析: 翻译 JSON={exp_ts!r}"
                )
        exp_original = item.get("original")
        if exp_original is not None:
            html_original = message_export_original(m)
            if _normalize_import_identity_text(exp_original) != _normalize_import_identity_text(
                html_original
            ):
                mismatch_reasons.append(
                    f"original 不一致: 翻译 JSON={exp_original!r} HTML={html_original!r}"
                )

        if mismatch_reasons:
            mismatch_count += 1
            for reason in mismatch_reasons:
                warnings.append(f"index={i} {reason}")
            warnings.append(
                f"index={i} 跳过导入（身份不一致，避免错贴译文）"
            )
            continue

        translation = clean_imported_translation(raw_tr, m.get("author"))
        emote_titles = [
            str(f.get("title", "")).strip()
            for f in m.get("fragments") or []
            if f.get("type") == "emote" and str(f.get("title", "")).strip()
        ]
        for title in set(emote_titles):
            placeholder = f"[{title}]"
            count = translation.count(placeholder)
            if count:
                translation = translation.replace(placeholder, "")
                stripped_placeholders += count
        translation = re.sub(r"[ \t]{2,}", " ", translation).strip()

        emote_frags = [f for f in (m.get("fragments") or []) if f.get("type") == "emote"]
        text_frags = [f for f in (m.get("fragments") or []) if f.get("type") == "text"]

        if not translation and emote_frags:
            # Pure-emote after placeholder strip: keep image fragments only.
            m["fragments"] = list(emote_frags)
            replaced += 1
        elif not translation and not emote_frags:
            # Cleaned translation ended up empty with no emote to keep. Skip the
            # row (original fragments stay) instead of writing a {"text": ""}
            # fragment that layout must filter out again.
            dropped_empty_translations += 1
        elif not emote_frags:
            # Text-only: single translated text fragment (merge multi-text).
            m["fragments"] = [{"type": "text", "text": translation}]
            replaced += 1
        else:
            # Mixed text+emote: put full translation as one leading text block,
            # then original emote fragments in order. Avoids stuffing only the
            # first text slot and leaving trailing empty texts mid-layout.
            m["fragments"] = [{"type": "text", "text": translation}] + list(emote_frags)
            replaced += 1
            if len(text_frags) > 1:
                # Informational only; layout is intentionally simplified.
                pass

    missing_idx = [i for i in range(len(messages)) if i not in trans_map]
    if missing_idx and len(missing_idx) <= 20:
        warnings.append(f"以下 index 在翻译 JSON 中缺失: {missing_idx[:20]}")
    elif missing_idx:
        warnings.append(f"{len(missing_idx)} 个 index 在翻译 JSON 中缺失")

    if mismatch_count:
        warnings.append(f"身份不一致跳过 {mismatch_count} 条翻译")
        if strict:
            raise ValueError(
                f"严格导入失败: {mismatch_count} 条翻译与 HTML 身份不一致"
                f"（作者/时间戳/原文），已拒绝错贴译文"
            )
    if dropped_empty_translations:
        warnings.append(
            f"{dropped_empty_translations} 条译文清洗后为空且无 emote，已跳过该行（保留原消息碎片）"
        )

    return replaced, stripped_placeholders, warnings
