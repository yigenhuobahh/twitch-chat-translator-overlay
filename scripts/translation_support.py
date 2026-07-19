#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Translation error classification, backoff, and optional disk cache."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid


class TranslationErrorKind:
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    CLIENT = "client"
    TIMEOUT = "timeout"
    BAD_JSON = "bad_json"
    SERVER = "server"
    NETWORK = "network"
    UNKNOWN = "unknown"


def classify_api_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None
    try:
        status_code = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_code = None

    if status_code == 429:
        return TranslationErrorKind.RATE_LIMIT
    if status_code in (401, 403):
        return TranslationErrorKind.AUTH
    if status_code == 408:
        return TranslationErrorKind.TIMEOUT
    if status_code is not None and 400 <= status_code < 500:
        return TranslationErrorKind.CLIENT
    if "rate limit" in text or "too many requests" in text or "1302" in text:
        return TranslationErrorKind.RATE_LIMIT
    if "unauthorized" in text or "forbidden" in text or "invalid api key" in text:
        return TranslationErrorKind.AUTH
    if "timeout" in text or "timed out" in text:
        return TranslationErrorKind.TIMEOUT
    if "json" in text and ("decode" in text or "parse" in text or "expecting" in text):
        return TranslationErrorKind.BAD_JSON
    if status_code is not None and status_code >= 500:
        return TranslationErrorKind.SERVER
    if "connection" in text or "network" in text or "temporarily unavailable" in text:
        return TranslationErrorKind.NETWORK
    return TranslationErrorKind.UNKNOWN


def extract_retry_after_seconds(exc: BaseException, default: float | None = None) -> float | None:
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) if resp is not None else None
    if headers:
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw is not None:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
    # Some providers put seconds in the message.
    m = re.search(r"retry after (\d+)", f"{exc}", flags=re.I)
    if m:
        return float(m.group(1))
    return default


def backoff_seconds(kind: str, attempt: int, exc: BaseException | None = None) -> float:
    """attempt is 0-based."""
    if exc is not None and kind == TranslationErrorKind.RATE_LIMIT:
        ra = extract_retry_after_seconds(exc)
        if ra is not None:
            return min(120.0, max(1.0, ra))
    base = {
        TranslationErrorKind.RATE_LIMIT: 20.0,
        TranslationErrorKind.TIMEOUT: 8.0,
        TranslationErrorKind.SERVER: 10.0,
        TranslationErrorKind.NETWORK: 8.0,
        TranslationErrorKind.BAD_JSON: 3.0,
        TranslationErrorKind.AUTH: 0.0,
        TranslationErrorKind.CLIENT: 0.0,
        TranslationErrorKind.UNKNOWN: 12.0,
    }.get(kind, 12.0)
    if base <= 0:
        return 0.0
    return min(120.0, base * (2 ** attempt))


def cache_key(
    original: str,
    target_language: str,
    model: str,
    context: str,
    *,
    provider: str = "",
    base_url: str = "",
    prompt_version: str = "",
) -> str:
    raw = "\0".join([
        original or "",
        target_language or "",
        model or "",
        context or "",
        (provider or "").strip().lower(),
        (base_url or "").strip().rstrip("/"),
        str(prompt_version or ""),
    ])
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()


class TranslationCache:
    def __init__(self, cache_dir: str | Path | None):
        self.enabled = bool(cache_dir)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._lock = threading.Lock()
        if self.enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get(
        self,
        original: str,
        target_language: str,
        model: str,
        context: str,
        *,
        provider: str = "",
        base_url: str = "",
        prompt_version: str = "",
    ) -> str | None:
        if not self.enabled:
            return None
        key = cache_key(
            original,
            target_language,
            model,
            context,
            provider=provider,
            base_url=base_url,
            prompt_version=prompt_version,
        )
        path = self.cache_dir / f"{key}.json"
        with self._lock:
            if not path.is_file():
                return None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                text = str(data.get("translation", "") or "").strip()
                return text or None
            except Exception:
                return None

    def put(
        self,
        original: str,
        target_language: str,
        model: str,
        context: str,
        translation: str,
        *,
        provider: str = "",
        base_url: str = "",
        prompt_version: str = "",
    ) -> bool:
        if not self.enabled:
            return False
        key = cache_key(
            original,
            target_language,
            model,
            context,
            provider=provider,
            base_url=base_url,
            prompt_version=prompt_version,
        )
        path = self.cache_dir / f"{key}.json"
        payload = {
            "original": original,
            "target_language": target_language,
            "model": model,
            "context": context,
            "provider": provider,
            "prompt_version": prompt_version,
            "translation": translation,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        tmp = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with self._lock:
                tmp.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        return True


def summarize_errors(error_counts: dict[str, int]) -> str:
    if not error_counts:
        return "无分类错误"
    parts = [f"{k}={v}" for k, v in sorted(error_counts.items())]
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Translation text cleaning (shared by translate + burn import paths)
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_WIN_DRIVE_IN_TEXT_RE = re.compile(r"[A-Za-z]:\\")
_SCHEME_PREFIX_RE = re.compile(r"(?i)^\s*(https?|ftp):")
_DRIVE_PREFIX_RE = re.compile(r"^\s*[A-Za-z]:[\\/]")
_INDEX_PREFIX_RE = re.compile(r"^\s*\[\d+\]\s*")
_ANGLE_PREFIX_RE = re.compile(r"^\s*<[^>\s]+>\s*")
_USERNAME_PREFIX_RE = re.compile(
    r"^\s*(?=[A-Za-z])[A-Za-z][A-Za-z0-9_\-]{1,24}\s*[:：]\s*(.+)$",
)
# Only treat "候选A / 候选B" style alternatives as multi-translation output.
# Do not touch URLs, paths, or ordinary words that merely contain a slash.
_ALT_TRANSLATION_RE = re.compile(
    r"^(?P<left>.+?)\s*/\s*(?P<right>.+)$",
    re.DOTALL,
)


def _has_cjk(s: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in s)


def looks_like_path_or_url(text) -> bool:
    text = str(text or "").strip()
    if not text:
        return False
    if _URL_RE.search(text):
        return True
    if _WIN_DRIVE_IN_TEXT_RE.search(text):
        return True
    if text.startswith("/") or text.startswith("./"):
        return True
    if text.count("/") >= 2 and re.search(r"[A-Za-z0-9]", text):
        return True
    return False


def clean_translation_text(text, author=None) -> str:
    """Remove common model echoes and fix multi-translation output.

    Shared by the translator post-process and burn-side import cleaning.
    Guards: never treat URL schemes (http:/https:/ftp:) or Windows drive
    letters (C:\\) as username prefixes. Optional *author* strips an exact
    author-name prefix even without CJK (import path).

    Burn import historically called this ``clean_imported_translation``; use
    this function (or the burn re-export alias) for both paths.
    """
    text = str(text or "").strip()
    text = _INDEX_PREFIX_RE.sub("", text).strip()
    text = _ANGLE_PREFIX_RE.sub("", text).strip()
    if author:
        escaped = re.escape(str(author).strip())
        if escaped:
            text = re.sub(rf"^{escaped}\s*[:：]\s*", "", text, flags=re.IGNORECASE).strip()
    # Common model leak: "username: 中文译文". Only strip when remainder has CJK so
    # times ("12:30") and labels ("Score: 5-0") stay intact.
    # Do not strip URL schemes (http:, https:) or Windows drive letters (C:).
    if not _SCHEME_PREFIX_RE.match(text) and not _DRIVE_PREFIX_RE.match(text):
        m = _USERNAME_PREFIX_RE.match(text)
        if m and _has_cjk(m.group(1)):
            text = m.group(1).strip()

    if "/" not in text or text.startswith("[") or looks_like_path_or_url(text):
        return text

    match = _ALT_TRANSLATION_RE.fullmatch(text)
    if not match:
        return text

    left = match.group("left").strip()
    right = match.group("right").strip()

    # Keep only short alternative pairs that look like dual Chinese candidates,
    # e.g. "太强了/真厉害". Leave English constructions like "and/or" and
    # mixed tokens like "A/B测试" alone.
    if (
        left
        and right
        and "/" not in left
        and "/" not in right
        and 2 <= len(left) <= 40
        and 2 <= len(right) <= 40
        and " " not in left
        and " " not in right
        and _has_cjk(left)
        and _has_cjk(right)
        and not looks_like_path_or_url(left)
        and not looks_like_path_or_url(right)
    ):
        return left
    return text
