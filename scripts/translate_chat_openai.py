#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Twitch chat translator - OpenAI-compatible backend
==================================================
Translate exported Twitch chat messages into any target language
using any OpenAI-compatible chat completion API.

Usage:
  python translate_chat_openai.py <translation.json> [--context "..."] [--target-language zh]

Examples:
  python translate_chat_openai.py translations/example_translation.json
  python translate_chat_openai.py translations/example_translation.json --target-language ja
  python translate_chat_openai.py translations/example_translation.json --context "gaming livestream chat" --target-language ko

Environment variables:
  OPENAI_COMPAT_BASE_URL  API base URL, for example https://api.openai.com/v1
  OPENAI_COMPAT_API_KEY   API key
  OPENAI_COMPAT_MODEL     model name, for example gpt-4o-mini
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

# Allow sibling imports when loaded as a script or via importlib from tests.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore

from common_utils import ensure_utf8_stdio, load_dotenv_if_present
from translation_support import (
    TranslationCache,
    backoff_seconds,
    classify_api_error,
    clean_translation_text,
    summarize_errors,
)

ensure_utf8_stdio()
load_dotenv_if_present()

# Prefer OPENAI_COMPAT_*; fall back to legacy AGNES_* for local setups.
BASE_URL = os.getenv("OPENAI_COMPAT_BASE_URL") or os.getenv("AGNES_BASE_URL")
API_KEY = os.getenv("OPENAI_COMPAT_API_KEY") or os.getenv("AGNES_API_KEY")
MODEL = os.getenv("OPENAI_COMPAT_MODEL") or os.getenv("AGNES_MODEL")
BATCH_SIZE = 10
MAX_WORKERS = 4
PROGRESS_SCHEMA_VERSION = 1

TRANSLATE_PROMPT = """You are a livestream chat translation expert. Translate the following Twitch chat messages into {target_language}.

Context: {context}

Rules:
1. CRITICAL: If a glossary is provided in the context, you MUST use those exact translations. The glossary is authoritative — never override it with your own rendering.
2. Keep the casual, colloquial tone of chat messages.
3. Pure emote messages (only bracketed emote names like [Pog]) should be kept as-is.
4. Keep @usernames untranslated.
5. Channel-specific memes, slang, and inside jokes should be translated by context; if unsure, keep the original.
6. Numbers or pure symbols with no special meaning should be kept as-is.
7. Do not translate personal names.
8. Keep translations concise, suitable for on-screen chat display.
9. Output exactly one final translation per message. No alternatives or slash-separated options.
10. CRITICAL: Do NOT prefix the translation with usernames, angle brackets, or any metadata. Output ONLY the translated text.
11. Do NOT include the message index, author name, or any formatting like <username> in the output.

Messages to translate (JSON):
{messages}

Output JSON (no markdown code blocks, plain JSON only):
{{
  "translations": [
    {{"index": 0, "translation": "translation text"}},
    {{"index": 1, "translation": "translation text"}}
  ]
}}

Every message must have a corresponding entry with the same index. Each translation must be a single translation in {target_language}.
"""


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def progress_path_for(json_path: str | Path) -> Path:
    p = Path(json_path)
    return p.with_name(p.name + ".progress.json")


def fingerprint_message(msg: dict) -> str:
    raw = f"{msg.get('index')}\0{msg.get('original', '')}\0{msg.get('author', '')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def load_progress(path: Path) -> dict:
    if not path.is_file():
        return {"schema_version": PROGRESS_SCHEMA_VERSION, "translations": {}, "failed": []}
    try:
        data = load_json(path)
    except Exception:
        return {"schema_version": PROGRESS_SCHEMA_VERSION, "translations": {}, "failed": []}
    if not isinstance(data, dict):
        return {"schema_version": PROGRESS_SCHEMA_VERSION, "translations": {}, "failed": []}
    data.setdefault("schema_version", PROGRESS_SCHEMA_VERSION)
    data.setdefault("translations", {})
    data.setdefault("failed", [])
    # Normalize keys to str for JSON compatibility.
    translations = {}
    for k, v in (data.get("translations") or {}).items():
        translations[str(k)] = v
    data["translations"] = translations
    return data


def save_progress(path: Path, progress: dict) -> None:
    progress = dict(progress)
    progress["schema_version"] = PROGRESS_SCHEMA_VERSION
    progress["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_json(path, progress)


def prepare_messages_for_llm(messages):
    lines = []
    for msg in messages:
        original = msg.get("original", "").strip()
        if not original:
            original = "[空消息]"
        lines.append(f'[{msg["index"]}] {original}')
    return "\n".join(lines)


def extract_json(text):
    if "```" in text:
        first = text.find("```")
        last = text.rfind("```")
        if first != last:
            inner = text[first:last]
            lines = inner.split("\n")
            if lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
    return text.strip()


PURE_PRESERVE_RE = re.compile(r"^(?:\s*\[[^\]]+\]\s*|\s*\d+\s*)+$")


def should_preserve_original(text):
    text = str(text or "").strip()
    return bool(text) and bool(PURE_PRESERVE_RE.fullmatch(text))


def translate_batch(client, batch, batch_num, context, target_language, cache=None, error_counts=None, on_error=None):
    # Optional disk cache: fill hits first, only call the model for misses.
    cache = cache or TranslationCache(None)
    error_counts = error_counts if error_counts is not None else {}

    def _count_error(kind: str) -> None:
        # Prefer thread-safe callback from main(); fall back to unlocked dict for unit tests.
        if on_error is not None:
            on_error(kind)
        else:
            error_counts[kind] = error_counts.get(kind, 0) + 1

    cached_items = []
    need_model = []
    for msg in batch:
        original = str(msg.get("original", "") or "")
        hit = cache.get(original, target_language, MODEL or "", context or "")
        if hit is not None:
            cached_items.append({"index": msg["index"], "translation": hit})
        else:
            need_model.append(msg)

    if not need_model:
        print(f"  [批次 {batch_num}] 全部命中缓存 ({len(cached_items)} 条)", flush=True)
        return cached_items

    messages_text = prepare_messages_for_llm(need_model)
    prompt = TRANSLATE_PROMPT.format(context=context, messages=messages_text, target_language=target_language)

    print(
        f"  [批次 {batch_num}] 发送 {len(need_model)} 条消息"
        f"{f'（缓存命中 {len(cached_items)}）' if cached_items else ''}"
        f" (prompt: {len(prompt)} 字符)...",
        flush=True,
    )

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
            result = response.choices[0].message.content
            json_str = extract_json(result)
            parsed = json.loads(json_str)
            translations = parsed.get("translations", [])
            expected_indexes = [msg["index"] for msg in need_model]
            expected_set = set(expected_indexes)
            returned_indexes = [item.get("index") for item in translations if isinstance(item, dict)]
            # Reject duplicate indexes in model response — zip-order remap would scramble rows.
            if len(returned_indexes) != len(set(returned_indexes)):
                raise ValueError(
                    f"duplicate indexes in model response: {returned_indexes}"
                )
            returned_set = set(returned_indexes)
            if translations and (not returned_set <= expected_set or len(translations) != len(need_model)):
                # Some models return batch-local indexes (0..N-1) or omit one item.
                # Only remap by order when indexes look batch-local 0..N-1 and counts match.
                # Never zip-order remap when global indexes are present but shuffled/wrong.
                print(f"  [批次 {batch_num}] 索引/数量不匹配，尝试修复", flush=True)
                n = len(need_model)
                batch_local = set(range(n))
                is_batch_local = returned_set == batch_local and len(returned_indexes) == n
                if is_batch_local and len(translations) == n:
                    remapped = []
                    # Prefer index-based local remap (item index i -> need_model[i]), not zip-order.
                    by_local = {
                        item.get("index"): item
                        for item in translations
                        if isinstance(item, dict)
                    }
                    for local_i, msg in enumerate(need_model):
                        item = by_local.get(local_i)
                        if not isinstance(item, dict):
                            raise ValueError(
                                f"missing batch-local index {local_i} in model response"
                            )
                        fixed = dict(item)
                        fixed["index"] = msg["index"]
                        remapped.append(fixed)
                    translations = remapped
                elif len(translations) != n:
                    raise ValueError(
                        f"translation count mismatch: got {len(translations)}, expected {n}"
                    )
                else:
                    # Global indexes present but not a subset / wrong set: do not scramble by zip.
                    raise ValueError(
                        f"index mismatch (no batch-local remap): got {sorted(returned_set)}, "
                        f"expected {sorted(expected_set)}"
                    )

            # Write successful model results into cache.
            by_index = {item.get("index"): item for item in translations if isinstance(item, dict)}
            for msg in need_model:
                item = by_index.get(msg["index"])
                if item and str(item.get("translation", "")).strip():
                    cache.put(
                        str(msg.get("original", "") or ""),
                        target_language,
                        MODEL or "",
                        context or "",
                        str(item["translation"]),
                    )

            print(f"  [批次 {batch_num}] ✓ 收到 {len(translations)} 条翻译", flush=True)
            return cached_items + translations
        except json.JSONDecodeError as e:
            kind = "bad_json"
            _count_error(kind)
            print(f"  [批次 {batch_num}] JSON 解析失败: {e}", flush=True)
            if "result" in locals():
                print(f"  原始输出前500字符: {result[:500]}", flush=True)
            sleep_s = backoff_seconds(kind, attempt, e)
            if attempt < max_retries - 1 and sleep_s > 0:
                print(f"  [批次 {batch_num}] 退避 {sleep_s:.1f}s 后重试 ({kind})", flush=True)
                time.sleep(sleep_s)
        except Exception as e:
            kind = classify_api_error(e)
            _count_error(kind)
            print(
                f"  [批次 {batch_num}] 重试 {attempt+1}/{max_retries} [{kind}]: {type(e).__name__}: {e}",
                flush=True,
            )
            if kind == "auth":
                print("  鉴权失败，停止本批重试。请检查 OPENAI_COMPAT_API_KEY / BASE_URL。", flush=True)
                break
            sleep_s = backoff_seconds(kind, attempt, e)
            if attempt < max_retries - 1 and sleep_s > 0:
                print(f"  [批次 {batch_num}] 退避 {sleep_s:.1f}s 后重试", flush=True)
                time.sleep(sleep_s)

    return cached_items or None


def main():
    parser = argparse.ArgumentParser(description="Twitch 弹幕翻译工具（OpenAI-compatible 后端）")
    parser.add_argument("json_path", help="twitch_chat_burn.py 导出的翻译 JSON 文件路径")
    parser.add_argument("--context", default="livestream chat", help="Background context for translation")
    parser.add_argument("--target-language", default="zh", help="Target language for translation (e.g. zh, ja, ko, en). Default: zh")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help=f"每批消息数（默认 {BATCH_SIZE}）")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help=f"并发数（默认 {MAX_WORKERS}）")
    parser.add_argument(
        "--resume",
        action="store_true",
        default=True,
        help="断点续传：跳过已有有效 translation，并读取 .progress.json（默认开启）",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="禁用断点续传，强制重翻全部非保留消息",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="只重试 progress 中记录失败、或当前仍缺失的条目",
    )
    parser.add_argument(
        "--progress-file",
        default=None,
        help="进度文件路径（默认 <json>.progress.json）",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="可选翻译磁盘缓存目录（按 原文+语言+model+context 哈希）",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="禁用磁盘缓存（即使设置了 --cache-dir）",
    )

    args = parser.parse_args()
    resume = bool(args.resume) and (not args.no_resume)

    if args.batch_size < 1 or args.batch_size > 100:
        parser.error("--batch-size 必须在 1..100")
    if args.workers < 1 or args.workers > 32:
        parser.error("--workers 必须在 1..32")

    # Re-read env at runtime so late dotenv / test env changes are honored.
    # If env is unset, keep module-level values (import-time load or test monkeypatch).
    global BASE_URL, API_KEY, MODEL
    BASE_URL = (
        os.getenv("OPENAI_COMPAT_BASE_URL")
        or os.getenv("AGNES_BASE_URL")
        or BASE_URL
    )
    API_KEY = (
        os.getenv("OPENAI_COMPAT_API_KEY")
        or os.getenv("AGNES_API_KEY")
        or API_KEY
    )
    MODEL = (
        os.getenv("OPENAI_COMPAT_MODEL")
        or os.getenv("AGNES_MODEL")
        or MODEL
    )

    json_path = os.path.abspath(args.json_path)
    if not os.path.isfile(json_path):
        print(f"错误: 文件不存在: {json_path}")
        sys.exit(1)
    if OpenAI is None:
        print("错误: 需要安装 openai 库: pip install openai")
        sys.exit(1)
    if not BASE_URL:
        print("错误: 请先设置 OPENAI_COMPAT_BASE_URL 环境变量")
        sys.exit(1)
    if not API_KEY:
        print("错误: 请先设置 OPENAI_COMPAT_API_KEY 环境变量")
        sys.exit(1)
    if not MODEL:
        print("错误: 请先设置 OPENAI_COMPAT_MODEL 环境变量")
        sys.exit(1)

    data = load_json(json_path)
    total = len(data["messages"])
    print(f"已加载 {total} 条待翻译消息")

    progress_file = Path(args.progress_file) if args.progress_file else progress_path_for(json_path)
    progress = load_progress(progress_file) if resume or args.retry_failed else {
        "schema_version": PROGRESS_SCHEMA_VERSION,
        "translations": {},
        "failed": [],
    }
    # Invalidate progress when target language / context diverges from this run.
    progress_lang = str(progress.get("target_language") or "").strip()
    progress_ctx = str(progress.get("context") or "").strip()
    current_lang = str(args.target_language or "").strip()
    current_ctx = str(args.context or "").strip()
    progress_compatible = True
    if resume and progress_lang and progress_lang != current_lang:
        print(
            f"警告: 进度文件语言 {progress_lang!r} 与当前 {current_lang!r} 不一致，忽略进度续传",
            flush=True,
        )
        progress_compatible = False
    if resume and progress_ctx and progress_ctx != current_ctx:
        print(
            "警告: 进度文件 context 与当前不一致，忽略进度续传",
            flush=True,
        )
        progress_compatible = False
    if not progress_compatible:
        progress = {
            "schema_version": PROGRESS_SCHEMA_VERSION,
            "translations": {},
            "failed": [],
        }

    progress_map = {}
    progress_fps = progress.get("fingerprints") or {}
    for k, v in (progress.get("translations") or {}).items():
        try:
            progress_map[int(k)] = v
        except (TypeError, ValueError):
            continue

    # Seed translation_map from existing JSON + progress when resuming.
    # When progress lang/context was wiped (progress_compatible=False), also ignore
    # non-empty JSON translations so a target-language switch re-bills correctly.
    translation_map = {}
    skipped_existing = 0
    trust_existing_json = bool(resume and progress_compatible)
    if resume and not progress_compatible:
        print(
            "  已忽略 JSON 中旧 translation（语言/context 与当前不一致），将重新翻译",
            flush=True,
        )
    for msg in data["messages"]:
        idx = msg["index"]
        original = msg.get("original", "")
        if should_preserve_original(original):
            continue
        existing = str(msg.get("translation", "") or "").strip()
        # Treat any non-empty translation as valid, including intentional
        # keep-original (translation == original) so resume does not re-bill —
        # but only when progress is still compatible with this run's lang/context.
        if trust_existing_json and existing:
            translation_map[idx] = existing
            skipped_existing += 1
            continue
        if resume and idx in progress_map and str(progress_map[idx]).strip():
            # Only reuse progress when message identity still matches.
            # Missing fingerprint (legacy progress) is NOT trusted — re-translate.
            fp_now = fingerprint_message(msg)
            fp_old = str(progress_fps.get(str(idx)) or progress_fps.get(idx) or "").strip()
            if not fp_old or fp_old != fp_now:
                continue
            translation_map[idx] = progress_map[idx]
            skipped_existing += 1

    # Decide which messages still need model translation.
    todo = []
    failed_set = set()
    for item in progress.get("failed") or []:
        try:
            failed_set.add(int(item))
        except (TypeError, ValueError):
            pass

    for msg in data["messages"]:
        idx = msg["index"]
        original = msg.get("original", "")
        if should_preserve_original(original):
            continue
        if args.retry_failed:
            if idx in failed_set or idx not in translation_map:
                todo.append(msg)
            continue
        if idx not in translation_map:
            todo.append(msg)

    print(
        f"续传={resume} | 已有有效译文 {skipped_existing} 条 | 待翻译 {len(todo)} 条 | 进度文件: {progress_file}",
        flush=True,
    )

    client = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=300.0)
    cache = TranslationCache(None if args.no_cache else args.cache_dir)
    if cache.enabled:
        print(f"翻译缓存目录: {cache.cache_dir}", flush=True)

    batches = []
    for i in range(0, len(todo), args.batch_size):
        batch = todo[i:i + args.batch_size]
        batches.append((i // args.batch_size + 1, batch))

    total_batches = len(batches)
    if total_batches:
        print(f"分 {total_batches} 批，每批 {args.batch_size} 条，{args.workers} 并发")
        print()

    failed_indexes = set(failed_set)
    error_counts = {}
    import threading
    progress_lock = threading.Lock()
    error_counts_lock = threading.Lock()

    def bump_error(kind: str) -> None:
        with error_counts_lock:
            error_counts[kind] = error_counts.get(kind, 0) + 1

    def persist_progress():
        # Serialize maps for JSON (string keys). Store fingerprints so resume
        # can refuse progress when original/author changed under the same index.
        fps = {}
        by_idx = {int(m.get("index")): m for m in data["messages"] if str(m.get("index", "")).lstrip("-").isdigit()}
        for k in translation_map:
            msg = by_idx.get(int(k))
            if msg is not None:
                fps[str(k)] = fingerprint_message(msg)
        payload = {
            "schema_version": PROGRESS_SCHEMA_VERSION,
            "model": MODEL,
            "target_language": args.target_language,
            "context": args.context,
            "translations": {str(k): v for k, v in translation_map.items()},
            "fingerprints": fps,
            "failed": sorted(failed_indexes),
        }
        save_progress(progress_file, payload)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {}
        for batch_num, batch in batches:
            future = executor.submit(
                translate_batch,
                client,
                batch,
                batch_num,
                args.context,
                args.target_language,
                cache,
                error_counts,
                bump_error,
            )
            futures[future] = (batch_num, batch)

        for future in concurrent.futures.as_completed(futures):
            batch_num, batch = futures[future]
            try:
                translations = future.result()
                if translations:
                    with progress_lock:
                        for item in translations:
                            if "index" in item and "translation" in item:
                                idx = item["index"]
                                translation_map[idx] = item["translation"]
                                failed_indexes.discard(idx)
                        persist_progress()
                else:
                    print(f"批次 {batch_num} 翻译失败")
                    with progress_lock:
                        for msg in batch:
                            failed_indexes.add(msg["index"])
                        persist_progress()
            except Exception as e:
                print(f"批次 {batch_num} 异常: {type(e).__name__}: {e}")
                with progress_lock:
                    for msg in batch:
                        failed_indexes.add(msg["index"])
                    persist_progress()

    # Retry non-preserve missing messages once, still in original batch sizes.
    missing_for_retry = []
    for msg in data["messages"]:
        idx = msg["index"]
        if should_preserve_original(msg.get("original", "")):
            continue
        if idx not in translation_map:
            missing_for_retry.append(msg)
    if missing_for_retry:
        print(f"\n重试缺失翻译 {len(missing_for_retry)} 条...", flush=True)
        for retry_i in range(0, len(missing_for_retry), args.batch_size):
            retry_batch = missing_for_retry[retry_i:retry_i + args.batch_size]
            retry_num = f"retry-{retry_i // args.batch_size + 1}"
            retry_translations = translate_batch(
                client, retry_batch, retry_num, args.context, args.target_language, cache, error_counts, bump_error
            ) or []
            for item in retry_translations:
                if "index" in item and "translation" in item:
                    translation_map[item["index"]] = item["translation"]
                    failed_indexes.discard(item["index"])
            still_missing = [m["index"] for m in retry_batch if m["index"] not in translation_map]
            for idx in still_missing:
                failed_indexes.add(idx)
            persist_progress()

    updated = 0
    preserved = 0
    missing = 0
    for msg in data["messages"]:
        idx = msg["index"]
        if should_preserve_original(msg.get("original", "")):
            if msg.get("translation") != msg.get("original"):
                msg["translation"] = msg.get("original", "")
                preserved += 1
            continue
        if idx in translation_map and str(translation_map[idx]).strip():
            msg["translation"] = clean_translation_text(translation_map[idx])
            updated += 1
        elif "translation" not in msg or not str(msg.get("translation", "")).strip():
            msg["translation"] = msg.get("original", "")
            missing += 1
            failed_indexes.add(idx)

    save_json(json_path, data)
    persist_progress()
    print()
    print(f"完成: 更新 {updated}/{total} 条翻译，保留原文 {preserved} 条")
    print(f"已保存: {json_path}")
    print(f"进度文件: {progress_file}")
    if error_counts:
        print(f"错误分类统计: {summarize_errors(error_counts)}")

    if missing:
        print(f"警告: {missing} 条非保留消息未翻译，已保留原文；可用 --retry-failed 重试")
        if error_counts:
            print(f"  最近错误类型: {summarize_errors(error_counts)}")
        sys.exit(1)
    # Successful full run: keep progress file as audit trail, but clear failures.
    failed_indexes.clear()
    persist_progress()


if __name__ == "__main__":
    main()
