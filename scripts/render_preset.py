#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YAML render presets for encode / overlay pipeline knobs (no network, no translation)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None


# Canonical fields accepted under top-level `render:` or flattened at root.
RENDER_FIELDS = {
    "encoder",
    "video_preset",
    "crf",
    "video_bitrate",
    "maxrate",
    "bufsize",
    "audio_codec",
    "audio_bitrate",
    "overlay_codec",
    "webm_crf",
    "webm_cpu_used",
    "output_fps",
    "fps",  # chat overlay fps; optional convenience
    "reuse_static_frames",
    "skip_blank_frames",
    "blank_hold_seconds",
    "lazy_message_images",
    "message_image_cache_size",
}


def _require_yaml() -> None:
    if yaml is None:
        raise ValueError("需要 PyYAML 才能加载 render preset：pip install pyyaml")


def _norm_key(key: str) -> str:
    return str(key).strip().replace("-", "_")


def _coerce(key: str, value: Any) -> Any:
    nk = _norm_key(key)
    if nk in ("reuse_static_frames", "skip_blank_frames", "lazy_message_images"):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"render preset 字段 {key} 需要布尔值，收到 {value!r}")
    if nk in ("crf", "webm_crf", "webm_cpu_used", "fps", "output_fps", "message_image_cache_size"):
        if value is None or str(value).strip() == "":
            return None
        return int(value)
    if nk == "blank_hold_seconds":
        return float(value)
    if value is None:
        return None
    return str(value).strip() if isinstance(value, str) else value


def normalize_render_dict(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("render preset 必须是 YAML mapping/object")
    raw = data.get("render") if isinstance(data.get("render"), dict) else data
    out: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    for k in ("name", "label", "description"):
        if k in data and data[k] is not None:
            meta[k] = data[k]
    for key, value in raw.items():
        nk = _norm_key(key)
        if nk in (
            "name",
            "label",
            "description",
            "layout",
            "context",
            "glossary",
            "preserve",
            "translation_style",
        ):
            continue
        if nk not in RENDER_FIELDS:
            continue
        out[nk] = _coerce(nk, value)
    if meta:
        out["_meta"] = meta
    return out


def _resolve_preset_path(path: str | Path) -> Path:
    """Resolve render preset path/short name (fast → render_fast.yaml)."""
    from common_utils import resolve_profiles_preset

    return resolve_profiles_preset(path, prefix="render")


def load_render_preset(path: str | Path) -> dict[str, Any]:
    _require_yaml()
    p = _resolve_preset_path(path)
    if not p.is_file():
        raise ValueError(f"render preset 不存在: {path}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"render preset 根节点必须是 mapping: {p}")
    return normalize_render_dict(data)


def _cli_default_for(cli_defaults: dict[str, Any], attr: str, key: str):
    """Return CLI default for attr if known; KeyError if caller omitted it."""
    if attr in cli_defaults:
        return cli_defaults[attr]
    if key in cli_defaults:
        return cli_defaults[key]
    raise KeyError(attr)


def apply_render_preset_to_namespace(
    args,
    preset: dict[str, Any],
    cli_defaults: dict[str, Any] | None = None,
) -> list[str]:
    """Apply encode/perf fields only when still at CLI default (explicit CLI wins).

    Never treat "default is None" as always-apply. For None-default fields
    (video_preset/bitrate/maxrate/bufsize/output_fps), only overwrite when the
    current value is still None. Explicit non-default CLI values always win.
    """
    cli_defaults = cli_defaults or {}
    attr_map = {
        "encoder": "encoder",
        "video_preset": "video_preset",
        "crf": "crf",
        "video_bitrate": "video_bitrate",
        "maxrate": "maxrate",
        "bufsize": "bufsize",
        "audio_codec": "audio_codec",
        "audio_bitrate": "audio_bitrate",
        "overlay_codec": "overlay_codec",
        "webm_crf": "webm_crf",
        "webm_cpu_used": "webm_cpu_used",
        "output_fps": "output_fps",
        "fps": "fps",
        "blank_hold_seconds": "blank_hold_seconds",
        "message_image_cache_size": "message_image_cache_size",
    }
    applied: list[str] = []
    for key, attr in attr_map.items():
        if key not in preset or preset[key] is None:
            continue
        if not hasattr(args, attr):
            continue
        current = getattr(args, attr)
        try:
            default = _cli_default_for(cli_defaults, attr, key)
        except KeyError:
            # Unknown default: only fill when the arg is still unset.
            if current is not None:
                continue
            setattr(args, attr, preset[key])
            applied.append(attr)
            continue
        # Apply only when still exactly at CLI default (None-default inclusive).
        if current == default:
            setattr(args, attr, preset[key])
            applied.append(attr)

    if "reuse_static_frames" in preset and hasattr(args, "no_reuse_static_frames"):
        if not args.no_reuse_static_frames:
            if not bool(preset["reuse_static_frames"]):
                args.no_reuse_static_frames = True
                applied.append("no_reuse_static_frames")
    if "skip_blank_frames" in preset and hasattr(args, "no_skip_blank_frames"):
        if not args.no_skip_blank_frames:
            if not bool(preset["skip_blank_frames"]):
                args.no_skip_blank_frames = True
                applied.append("no_skip_blank_frames")
    if "lazy_message_images" in preset and hasattr(args, "lazy_message_images"):
        default = cli_defaults.get("lazy_message_images", False)
        current = args.lazy_message_images
        if current == default and bool(preset["lazy_message_images"]) and not current:
            args.lazy_message_images = True
            applied.append("lazy_message_images")
    return applied
