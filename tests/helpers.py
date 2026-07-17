#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared helpers for strict real-world scenario tests."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def utf8_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if extra:
        env.update(extra)
    return env


def load_module(module_name: str, filename: str):
    """Load a scripts/*.py module by path, with light dependency stubs if needed."""
    if module_name == "translate_chat_openai" and "openai" not in sys.modules:
        fake = types.ModuleType("openai")

        class _OpenAI:
            def __init__(self, *args, **kwargs):
                pass

        fake.OpenAI = _OpenAI
        sys.modules["openai"] = fake

    path = SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def ffprobe_json(path: Path) -> dict:
    r = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=index,codec_type,width,height,start_time",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(r.stdout)


def stream_types(info: dict) -> set[str]:
    return {s.get("codec_type") for s in info.get("streams") or []}


def write_json(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def make_leadin_video(out_path: Path, duration: float = 3.0, lead_in: float = 1.0, fps: int = 30) -> Path:
    """
    Create a short MP4 where video content starts later than audio.

    Implementation:
    - generate black video of `duration`
    - pad video start by `lead_in` (freeze first frame)
    - audio is plain sine of `duration`
    - final container therefore has video longer than audio by ~lead_in
      and stream start times differ after remux/filter processing.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Use filter_complex so video is delayed while audio starts immediately.
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s=640x360:r={fps}:d={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=880:duration={duration}",
        "-filter_complex",
        f"[0:v]tpad=start_duration={lead_in}:start_mode=clone,setpts=PTS-STARTPTS[v];"
        f"[1:a]asetpts=PTS-STARTPTS[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-shortest",
        str(out_path),
    ]
    # Note: -shortest would cut to audio; omit it so lead-in remains.
    cmd = [c for c in cmd if c != "-shortest"]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert out_path.is_file()
    return out_path


def make_translation_for_export(export_data: dict, mapping: dict[str, str] | None = None) -> dict:
    """Fill translation field for exported messages with deterministic text."""
    mapping = mapping or {}
    out = {"messages": []}
    for msg in export_data.get("messages", []):
        original = str(msg.get("original", ""))
        if original in mapping:
            translation = mapping[original]
        elif original.startswith("[") and original.endswith("]") and " " not in original.strip():
            translation = original
        else:
            translation = f"译:{original}"
        item = dict(msg)
        item["translation"] = translation
        out["messages"].append(item)
    return out
