#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YAML layout presets for render geometry / style (no network, no translation)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None


# Keys accepted in YAML under top-level `layout:` or flattened at root.
LAYOUT_KEYS = {
    "x": int,
    "y": int,
    "width": int,
    "w": int,  # alias -> width
    "height": int,
    "h": int,  # alias -> height
    "font_size": int,
    "font-size": int,
    "font_path": str,
    "font-path": str,
    "font_bold_path": str,
    "font-bold-path": str,
    "fps": int,
    "max_visible": int,
    "max-visible": int,
    "msg_lifetime": float,
    "msg-lifetime": float,
    "max_message_lines": int,
    "max-message-lines": int,
    "min_visible_seconds": float,
    "min-visible-seconds": float,
    "arrival_interval": float,
    "arrival-interval": float,
    "stack_mode": str,
    "stack-mode": str,
    "x_ratio": float,
    "x-ratio": float,
    "y_ratio": float,
    "y-ratio": float,
    "width_ratio": float,
    "width-ratio": float,
    "height_ratio": float,
    "height-ratio": float,
    "font_size_ratio": float,
    "font-size-ratio": float,
    "bg_alpha": int,
    "bg-alpha": int,
    "emote_height": int,
    "emote-height": int,
    "emote_h": int,
    "blank_hold_seconds": float,
    "blank-hold-seconds": float,
    "reuse_static_frames": bool,
    "skip_blank_frames": bool,
}


def _require_yaml() -> None:
    if yaml is None:
        raise ValueError("需要 PyYAML 才能加载 layout preset：pip install pyyaml")


def _coerce(key: str, value: Any) -> Any:
    # normalize key first for type lookup
    nk = key.replace("-", "_")
    if nk in ("w",):
        nk = "width"
    if nk in ("h",):
        nk = "height"
    if nk == "emote_h":
        nk = "emote_height"
    typ = {
        "x": int,
        "y": int,
        "width": int,
        "height": int,
        "font_size": int,
        "font_path": str,
        "font_bold_path": str,
        "fps": int,
        "max_visible": int,
        "msg_lifetime": float,
        "max_message_lines": int,
        "min_visible_seconds": float,
        "arrival_interval": float,
        "stack_mode": str,
        "x_ratio": float,
        "y_ratio": float,
        "width_ratio": float,
        "height_ratio": float,
        "font_size_ratio": float,
        "bg_alpha": int,
        "emote_height": int,
        "blank_hold_seconds": float,
        "reuse_static_frames": bool,
        "skip_blank_frames": bool,
    }.get(nk)
    if typ is None:
        return value
    if typ is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"layout preset 字段 {key} 需要布尔值，收到 {value!r}")
    if typ is int:
        return int(value)
    if typ is float:
        return float(value)
    return str(value)


def normalize_layout_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Return canonical Overlay/CLI field names from a raw YAML dict."""
    if not isinstance(data, dict):
        raise ValueError("layout preset 必须是 YAML mapping/object")
    raw = data.get("layout") if isinstance(data.get("layout"), dict) else data
    out: dict[str, Any] = {}
    meta = {}
    for k in ("name", "label", "description"):
        if k in data and data[k] is not None:
            meta[k] = data[k]
    for key, value in raw.items():
        if key in ("name", "label", "description", "context", "glossary", "preserve", "translation_style"):
            # allow combined translation+layout files; ignore translation-only keys here
            continue
        nk = str(key).replace("-", "_")
        if nk == "w":
            nk = "width"
        elif nk == "h":
            nk = "height"
        elif nk == "emote_h":
            nk = "emote_height"
        # LAYOUT_KEYS is the single allowlist (aliases already normalized above).
        allowed = {
            k.replace("-", "_")
            for k in LAYOUT_KEYS
            if k.replace("-", "_") not in ("w", "h", "emote_h")
        } | {"width", "height", "emote_height"}
        if nk not in allowed:
            # silently ignore unknown keys so translation profiles can share a file if needed
            continue
        out[nk] = _coerce(nk, value)
        if nk == "stack_mode":
            sm = str(out[nk]).strip().lower()
            if sm not in ("float", "lanes"):
                raise ValueError(f"layout preset stack_mode must be float or lanes, got {out[nk]!r}")
            out[nk] = sm
    if meta:
        out["_meta"] = meta
    return out


def _resolve_preset_path(path: str | Path) -> Path:
    """Resolve layout preset path/short name (compact → layout_compact.yaml)."""
    from common_utils import resolve_profiles_preset

    return resolve_profiles_preset(path, prefix="layout")


def load_layout_preset(path: str | Path) -> dict[str, Any]:
    _require_yaml()
    p = _resolve_preset_path(path)
    if not p.is_file():
        raise ValueError(f"layout preset 不存在: {path}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"layout preset 根节点必须是 mapping: {p}")
    return normalize_layout_dict(data)


def apply_layout_preset_to_namespace(args, preset: dict[str, Any], cli_defaults: dict[str, Any] | None = None) -> list[str]:
    """Apply preset only onto fields still at CLI default (explicit CLI wins).

    argparse always fills defaults, so we compare against known CLI defaults.
    Never treat "default is None" as always-apply: only overwrite when the
    current value still equals the known CLI default (or is unset when default
    is unknown).
    Returns list of applied field names.
    """
    cli_defaults = cli_defaults or {}
    # Map preset keys -> argparse attribute names
    attr_map = {
        "x": "x",
        "y": "y",
        "width": "width",
        "height": "height",
        "font_size": "font_size",
        "font_path": "font_path",
        "font_bold_path": "font_bold_path",
        "fps": "fps",
        "max_visible": "max_visible",
        "msg_lifetime": "msg_lifetime",
        "max_message_lines": "max_message_lines",
        "min_visible_seconds": "min_visible_seconds",
        "arrival_interval": "arrival_interval",
        "stack_mode": "stack_mode",
        "x_ratio": "x_ratio",
        "y_ratio": "y_ratio",
        "width_ratio": "width_ratio",
        "height_ratio": "height_ratio",
        "font_size_ratio": "font_size_ratio",
        "bg_alpha": "bg_alpha",
        "emote_height": "emote_height",  # burn CLI uses emote_height
        "blank_hold_seconds": "blank_hold_seconds",
    }
    applied: list[str] = []
    for key, attr in attr_map.items():
        if key not in preset:
            continue
        if not hasattr(args, attr):
            # render_cn_chat may lack some burn-only fields; skip quietly
            continue
        current = getattr(args, attr)
        if attr in cli_defaults:
            default = cli_defaults[attr]
        elif key in cli_defaults:
            default = cli_defaults[key]
        else:
            # Unknown default: only fill when still unset.
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
        # Inverted CLI flag: no_reuse_static_frames default is False.
        # Apply only when the flag is still at default (user did not pass it).
        if not args.no_reuse_static_frames:
            want_reuse = bool(preset["reuse_static_frames"])
            if not want_reuse:
                args.no_reuse_static_frames = True
                applied.append("no_reuse_static_frames")
    if "skip_blank_frames" in preset and hasattr(args, "no_skip_blank_frames"):
        if not args.no_skip_blank_frames:
            want_skip = bool(preset["skip_blank_frames"])
            if not want_skip:
                args.no_skip_blank_frames = True
                applied.append("no_skip_blank_frames")
    return applied
