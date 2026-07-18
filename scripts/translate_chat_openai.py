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
import threading
import time
import uuid

# Allow sibling imports when loaded as a script or via importlib from tests.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore

from common_utils import (
    ensure_utf8_stdio,
    load_dotenv_if_present,
    positive_float_arg,
)
from translation_support import (
    TranslationCache,
    TranslationErrorKind,
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
MAX_BATCH_CHARS = 16_000
PROGRESS_SCHEMA_VERSION = 2
TRANSLATION_PROVIDER = "openai-compatible"
PROMPT_VERSION = 1
DEFAULT_REQUEST_TIMEOUT = 300.0

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
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with open(tmp, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def progress_path_for(json_path: str | Path) -> Path:
    p = Path(json_path)
    return p.with_name(p.name + ".progress.json")


def fingerprint_message(msg: dict) -> str:
    raw = f"{msg.get('index')}\0{msg.get('original', '')}\0{msg.get('author', '')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def fingerprint_translation(value) -> str:
    """Fingerprint the JSON translation value observed at a progress save."""
    raw = str(value or "")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def paths_refer_to_same_file(first: str | Path, second: str | Path) -> bool:
    """Return whether two path spellings identify the same output target."""
    first_path = Path(first)
    second_path = Path(second)
    try:
        return first_path.samefile(second_path)
    except OSError:
        first_normalized = os.path.normcase(
            os.path.abspath(str(first_path.resolve(strict=False)))
        )
        second_normalized = os.path.normcase(
            os.path.abspath(str(second_path.resolve(strict=False)))
        )
        return first_normalized == second_normalized


def empty_progress() -> dict:
    return {
        "schema_version": PROGRESS_SCHEMA_VERSION,
        "translations": {},
        "fingerprints": {},
        "json_translation_fingerprints": {},
        "failed": [],
    }


def base_url_fingerprint(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def progress_compatibility_errors(
    progress: dict,
    *,
    target_language: str,
    context: str,
    provider: str,
    base_url: str,
    model: str,
    prompt_version: int,
) -> list[str]:
    """Return metadata fields that make persisted translations unsafe to reuse."""
    expected = {
        "schema_version": PROGRESS_SCHEMA_VERSION,
        "target_language": str(target_language or "").strip(),
        "context": str(context or "").strip(),
        "provider": str(provider or "").strip().lower(),
        "base_url_fingerprint": base_url_fingerprint(base_url),
        "model": str(model or "").strip(),
        "prompt_version": int(prompt_version),
    }
    mismatches = []
    for key, expected_value in expected.items():
        actual = progress.get(key)
        if key in {"schema_version", "prompt_version"}:
            try:
                actual = int(actual)
            except (TypeError, ValueError):
                actual = None
        else:
            actual = str(actual or "").strip()
            if key == "provider":
                actual = actual.lower()
        if actual != expected_value:
            mismatches.append(key)
    return mismatches


def load_progress(path: Path) -> dict:
    if not path.is_file():
        return empty_progress()
    try:
        data = load_json(path)
    except Exception:
        return empty_progress()
    if not isinstance(data, dict):
        return empty_progress()

    normalized = dict(data)
    normalized.setdefault("schema_version", 0)

    raw_translations = normalized.get("translations")
    translations = {}
    if isinstance(raw_translations, dict):
        for key, value in raw_translations.items():
            translations[str(key)] = value
    normalized["translations"] = translations

    raw_fingerprints = normalized.get("fingerprints")
    fingerprints = {}
    if isinstance(raw_fingerprints, dict):
        for key, value in raw_fingerprints.items():
            fingerprints[str(key)] = str(value or "")
    normalized["fingerprints"] = fingerprints

    raw_json_fingerprints = normalized.get("json_translation_fingerprints")
    json_fingerprints = {}
    if isinstance(raw_json_fingerprints, dict):
        for key, value in raw_json_fingerprints.items():
            json_fingerprints[str(key)] = str(value or "")
    normalized["json_translation_fingerprints"] = json_fingerprints

    raw_failed = normalized.get("failed")
    normalized["failed"] = list(raw_failed) if isinstance(raw_failed, list) else []
    return normalized


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


def build_translation_batches(
    messages: list[dict],
    *,
    max_messages: int,
    max_prompt_chars: int,
    context: str,
    target_language: str,
) -> list[list[dict]]:
    """Split work so both message count and complete prompt size stay bounded."""
    batches: list[list[dict]] = []
    current: list[dict] = []

    def prompt_size(items: list[dict]) -> int:
        messages_text = prepare_messages_for_llm(items)
        prompt = TRANSLATE_PROMPT.format(
            context=context,
            messages=messages_text,
            target_language=target_language,
        )
        return len(prompt)

    for msg in messages:
        if current and len(current) >= max_messages:
            batches.append(current)
            current = []

        candidate = [*current, msg]
        if prompt_size(candidate) <= max_prompt_chars:
            current = candidate
            continue

        if current:
            batches.append(current)
            current = [msg]
        else:
            current = candidate

        if prompt_size(current) > max_prompt_chars:
            raise ValueError(
                f"消息 {msg.get('index')} 单条提示超过 --max-batch-chars="
                f"{max_prompt_chars}；请缩短 context 或提高上限"
            )

    if current:
        batches.append(current)
    return batches


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
        hit = cache.get(
            original,
            target_language,
            MODEL or "",
            context or "",
            provider=TRANSLATION_PROVIDER,
            base_url=BASE_URL or "",
            prompt_version=str(PROMPT_VERSION),
        )
        if hit is not None:
            cleaned_hit = clean_translation_text(hit)
            if cleaned_hit:
                cached_items.append(
                    {"index": msg["index"], "translation": cleaned_hit}
                )
                continue
            # A legacy or corrupted cache row must not suppress a model call.
            need_model.append(msg)
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

            cleaned_translations = []
            invalid_translations = 0
            for item in translations:
                if not isinstance(item, dict):
                    invalid_translations += 1
                    continue
                cleaned = clean_translation_text(item.get("translation", ""))
                if not cleaned:
                    invalid_translations += 1
                    continue
                fixed = dict(item)
                fixed["translation"] = cleaned
                cleaned_translations.append(fixed)
            if invalid_translations:
                _count_error(TranslationErrorKind.BAD_JSON)
                print(
                    f"  [批次 {batch_num}] 忽略 {invalid_translations} 条清洗后为空的译文",
                    flush=True,
                )
            translations = cleaned_translations

            # Write successful model results into cache.
            by_index = {item.get("index"): item for item in translations if isinstance(item, dict)}
            for msg in need_model:
                item = by_index.get(msg["index"])
                if item and str(item.get("translation", "")).strip():
                    try:
                        cache.put(
                            str(msg.get("original", "") or ""),
                            target_language,
                            MODEL or "",
                            context or "",
                            str(item["translation"]),
                            provider=TRANSLATION_PROVIDER,
                            base_url=BASE_URL or "",
                            prompt_version=str(PROMPT_VERSION),
                        )
                    except Exception:
                        # Caching is optional; never discard a successful API result.
                        pass

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
            if kind in (TranslationErrorKind.AUTH, TranslationErrorKind.CLIENT):
                print(
                    "  API 配置/请求错误不可重试，停止本批。"
                    "请检查 API key、base URL、model 与请求限制。",
                    flush=True,
                )
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
        "--max-batch-chars",
        type=int,
        default=MAX_BATCH_CHARS,
        help=f"单批完整提示字符上限（1000..200000，默认 {MAX_BATCH_CHARS}）",
    )
    parser.add_argument(
        "--request-timeout",
        type=positive_float_arg,
        default=DEFAULT_REQUEST_TIMEOUT,
        help=f"单次 API 请求超时秒数（1..600，默认 {DEFAULT_REQUEST_TIMEOUT:g}）",
    )
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
    if args.max_batch_chars < 1000 or args.max_batch_chars > 200_000:
        parser.error("--max-batch-chars 必须在 1000..200000")
    if args.request_timeout < 1 or args.request_timeout > 600:
        parser.error("--request-timeout 必须在 1..600")

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

    progress_file = (
        Path(args.progress_file)
        if args.progress_file
        else progress_path_for(json_path)
    )
    if paths_refer_to_same_file(json_path, progress_file):
        parser.error("--progress-file 不能与输入 JSON 指向同一文件")
    progress = (
        load_progress(progress_file)
        if resume or args.retry_failed
        else empty_progress()
    )
    require_compatible_progress = bool(resume or args.retry_failed)
    compatibility_errors = (
        progress_compatibility_errors(
            progress,
            target_language=args.target_language,
            context=args.context,
            provider=TRANSLATION_PROVIDER,
            base_url=BASE_URL or "",
            model=MODEL or "",
            prompt_version=PROMPT_VERSION,
        )
        if require_compatible_progress
        else []
    )
    progress_compatible = require_compatible_progress and not compatibility_errors
    if compatibility_errors:
        print(
            "警告: 进度文件与当前翻译配置不兼容"
            f"（{', '.join(compatibility_errors)}），忽略旧进度和 JSON 译文",
            flush=True,
        )
        progress = empty_progress()
    progress_map = {}
    progress_fps = progress.get("fingerprints") or {}
    progress_json_fps = progress.get("json_translation_fingerprints") or {}
    for k, v in (progress.get("translations") or {}).items():
        try:
            progress_map[int(k)] = v
        except (TypeError, ValueError):
            continue

    failed_set = set()
    for item in progress.get("failed") or []:
        try:
            failed_set.add(int(item))
        except (TypeError, ValueError):
            pass

    # Reuse translations only when the run identity and per-message fingerprint
    # match. A JSON snapshot distinguishes later human edits from stale values
    # or original-text fallbacks left by an interrupted/failed run.
    translation_map = {}
    skipped_existing = 0
    for msg in data["messages"]:
        idx = msg["index"]
        original = msg.get("original", "")
        if should_preserve_original(original):
            continue
        if not resume or not progress_compatible:
            continue
        progress_translation = str(progress_map.get(idx, "") or "").strip()
        raw_existing_translation = str(msg.get("translation", "") or "")
        existing_translation = raw_existing_translation.strip()
        fp_now = fingerprint_message(msg)
        fp_old = str(progress_fps.get(str(idx)) or "").strip()
        if not fp_old or fp_old != fp_now:
            continue

        json_fp_old = str(progress_json_fps.get(str(idx)) or "").strip()
        json_snapshot_known = bool(
            re.fullmatch(r"[0-9a-f]{16}", json_fp_old)
        )
        json_changed = json_snapshot_known and (
            json_fp_old != fingerprint_translation(raw_existing_translation)
        )
        if json_changed:
            if existing_translation:
                translation_map[idx] = existing_translation
                failed_set.discard(idx)
                skipped_existing += 1
            # Clearing a reviewed value explicitly requests a fresh translation.
            continue
        if idx in failed_set:
            # Unchanged failed rows contain an empty/stale value or our original
            # fallback, not a reviewed translation.
            continue
        if not json_snapshot_known and existing_translation:
            # Legacy compatible progress has no JSON snapshot; retain the prior
            # behavior for successful rows only.
            translation_map[idx] = existing_translation
        elif progress_translation:
            # Progress may be newer than JSON after an interrupted run.
            translation_map[idx] = progress_translation
        elif existing_translation:
            translation_map[idx] = existing_translation
        else:
            continue
        skipped_existing += 1

    # Decide which messages still need model translation.
    todo = []

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

    client = OpenAI(
        api_key=API_KEY,
        base_url=BASE_URL,
        timeout=args.request_timeout,
        max_retries=0,
    )
    cache = TranslationCache(None if args.no_cache else args.cache_dir)
    if cache.enabled:
        print(f"翻译缓存目录: {cache.cache_dir}", flush=True)

    try:
        grouped_batches = build_translation_batches(
            todo,
            max_messages=args.batch_size,
            max_prompt_chars=args.max_batch_chars,
            context=args.context,
            target_language=args.target_language,
        )
    except ValueError as exc:
        parser.error(str(exc))
    batches = [(index + 1, batch) for index, batch in enumerate(grouped_batches)]

    total_batches = len(batches)
    if total_batches:
        print(f"分 {total_batches} 批，每批 {args.batch_size} 条，{args.workers} 并发")
        print()

    failed_indexes = set(failed_set)
    error_counts = {}
    progress_lock = threading.Lock()
    error_counts_lock = threading.Lock()
    try:
        from task_events import emit_task_event
    except ImportError:  # pragma: no cover - script remains standalone
        def emit_task_event(*_args, **_kwargs) -> bool:
            return False
    completed_batches = 0

    def bump_error(kind: str) -> None:
        with error_counts_lock:
            error_counts[kind] = error_counts.get(kind, 0) + 1

    def persist_progress():
        # Input fingerprints authenticate every row, including failed rows.
        # JSON-value snapshots let resume recognize edits made after this save.
        fps = {}
        json_translation_fps = {}
        for msg in data["messages"]:
            if not isinstance(msg, dict):
                continue
            key = str(msg.get("index"))
            fps[key] = fingerprint_message(msg)
            json_translation_fps[key] = fingerprint_translation(
                msg.get("translation", "")
            )
        payload = {
            "schema_version": PROGRESS_SCHEMA_VERSION,
            "provider": TRANSLATION_PROVIDER,
            "base_url_fingerprint": base_url_fingerprint(BASE_URL or ""),
            "model": MODEL,
            "prompt_version": PROMPT_VERSION,
            "target_language": args.target_language,
            "context": args.context,
            "translations": {str(k): v for k, v in translation_map.items()},
            "fingerprints": fps,
            "json_translation_fingerprints": json_translation_fps,
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
            completed_batches += 1
            emit_task_event(
                "stage_progress",
                stage="translate",
                completed=completed_batches,
                total=total_batches,
                unit="batches",
            )

    # Retry non-preserve missing messages once, still in original batch sizes.
    missing_for_retry = []
    for msg in data["messages"]:
        idx = msg["index"]
        if should_preserve_original(msg.get("original", "")):
            continue
        if idx not in translation_map:
            missing_for_retry.append(msg)
    terminal_error = any(
        error_counts.get(kind, 0) > 0
        for kind in (TranslationErrorKind.AUTH, TranslationErrorKind.CLIENT)
    )
    if missing_for_retry and terminal_error:
        print(
            f"\nAPI 配置/鉴权错误不可重试，跳过 "
            f"{len(missing_for_retry)} 条缺失翻译的最终重试。",
            flush=True,
        )
    elif missing_for_retry:
        print(f"\n重试缺失翻译 {len(missing_for_retry)} 条...", flush=True)
        retry_batches = build_translation_batches(
            missing_for_retry,
            max_messages=args.batch_size,
            max_prompt_chars=args.max_batch_chars,
            context=args.context,
            target_language=args.target_language,
        )
        for retry_i, retry_batch in enumerate(retry_batches, start=1):
            retry_num = f"retry-{retry_i}"
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
        cleaned_translation = clean_translation_text(translation_map.get(idx, ""))
        if cleaned_translation:
            translation_map[idx] = cleaned_translation
            msg["translation"] = cleaned_translation
            failed_indexes.discard(idx)
            updated += 1
        else:
            # Never count a value that becomes empty after cleanup as success.
            translation_map.pop(idx, None)
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
