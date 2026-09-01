#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ffprobe wrappers: media duration / dimensions / fps / stream summary.

Extracted verbatim from twitch_chat_burn for maintainability. Callers must
address this module via attribute access (``import media_probe`` then
``media_probe.probe_video_dimensions(...)``) so tests can monkeypatch the
owner module. Probe results are memoized per (absolute path + file stat
signature, probe kind) so one render pass never repeats the same ffprobe;
``cache_clear()`` drops the cache and runs at the CLI entry points."""

from __future__ import annotations

import functools
import json
import math
import os
import subprocess

from common_utils import require_executable

_PROBE_TIMEOUT_SECONDS = 15.0

# One render pass used to run up to 6 identical ffprobe calls for the same
# file (layout adapt, bounds warn, fps resolve, scene duration, A/V timing,
# publish validation). Memoize by absolute path + stat signature: a file that
# changed on disk (mtime/size/inode) re-probes, repeat calls are free.
# functools.lru_cache never caches raised exceptions, so failed probes retry.
_PROBE_CACHE_MAXSIZE = 64
_CACHED_PROBES = []


def _probe_cache_key(path) -> tuple:
    """Cache key: absolute path plus its stat signature (None when missing)."""
    abs_path = os.path.abspath(os.fspath(path))
    try:
        st = os.stat(abs_path)
    except OSError:
        return (abs_path, None)
    return (abs_path, (st.st_mtime_ns, st.st_size, st.st_ino, st.st_dev))


def _cached_probe(fn):
    """Memoize one ffprobe wrapper keyed by (stat signature, probe kind).

    Each decorated function owns a private LRU, which is what separates the
    probe kinds; ``fn`` keeps receiving the caller's original path argument so
    error messages and subprocess argv stay identical to the undecorated code.
    """
    @functools.lru_cache(maxsize=_PROBE_CACHE_MAXSIZE)
    def cached(_key, path):
        return fn(path)

    @functools.wraps(fn)
    def wrapper(video_path):
        return cached(_probe_cache_key(video_path), video_path)

    wrapper.cache_clear = cached.cache_clear
    _CACHED_PROBES.append(wrapper)
    return wrapper


def cache_clear():
    """Drop every cached probe result (called at the CLI entry points)."""
    for wrapper in _CACHED_PROBES:
        wrapper.cache_clear()


@_cached_probe
def probe_video_duration(video_path):
    """Read media duration via ffprobe. Returns float seconds or raises RuntimeError."""
    try:
        probe = subprocess.run(
            [require_executable("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video_path],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"读取视频时长超时 ({_PROBE_TIMEOUT_SECONDS:g}s): {video_path}") from e
    raw = (probe.stdout or "").strip().splitlines()
    if probe.returncode != 0 or not raw:
        err = (probe.stderr or probe.stdout or "ffprobe failed").strip()[:400]
        raise RuntimeError(f"无法读取视频时长: {video_path}: {err}")
    try:
        duration = float(raw[0].strip() or 0.0)
    except ValueError as e:
        raise RuntimeError(f"无法解析视频时长 {raw[0]!r}: {e}") from e
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"视频时长无效 ({duration}): {video_path}")
    return duration



@_cached_probe
def probe_video_dimensions(video_path):
    """Read the first video stream dimensions via ffprobe, or return None."""
    try:
        probe = subprocess.run(
            [
                require_executable("ffprobe"), "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "json", video_path,
            ],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None
    if probe.returncode != 0:
        return None
    try:
        stream = (json.loads(probe.stdout or "{}").get("streams") or [{}])[0]
        width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
        return (width, height) if width > 0 and height > 0 else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


@_cached_probe
def probe_video_fps(video_path):
    """Best-effort source video FPS via ffprobe. Returns float or None."""
    try:
        probe = subprocess.run(
            [
                require_executable("ffprobe"), "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=r_frame_rate,avg_frame_rate",
                "-of", "json", video_path,
            ],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None
    if probe.returncode != 0:
        return None
    try:
        data = json.loads(probe.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
    except (json.JSONDecodeError, IndexError, TypeError):
        return None

    def _parse_rate(rate):
        if not rate or rate in ("0/0", "N/A"):
            return None
        try:
            if "/" in str(rate):
                num, den = str(rate).split("/", 1)
                den_f = float(den)
                if den_f <= 0:
                    return None
                return float(num) / den_f
            return float(rate)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    # Prefer r_frame_rate for constant sources, then avg.
    for key in ("r_frame_rate", "avg_frame_rate"):
        val = _parse_rate(stream.get(key))
        if val and 1.0 <= val <= 240.0:
            return val
    return None


def _quantize_fps(value: float) -> float:
    """Keep common NTSC rates exact; leave other floats; clamp to [1, 240]."""
    v = float(value)
    if v < 1.0:
        return 1.0
    if v > 240.0:
        return 240.0
    # Known broadcast rates (within 0.02 of nominal).
    known = (
        24000 / 1001,  # ~23.976
        24.0,
        25.0,
        30000 / 1001,  # ~29.970
        30.0,
        50.0,
        60000 / 1001,  # ~59.940
        60.0,
        120.0,
    )
    for k in known:
        if abs(v - k) < 0.02:
            return k
    # Near-integer CFR
    if abs(v - round(v)) < 1e-3:
        return float(int(round(v)))
    return v


def fps_to_ffmpeg_rate(fps) -> str:
    """Format fps for ffmpeg -r / -framerate (prefer exact NTSC rationals)."""
    v = _quantize_fps(float(fps))
    rationals = {
        24000 / 1001: "24000/1001",
        30000 / 1001: "30000/1001",
        60000 / 1001: "60000/1001",
    }
    for k, s in rationals.items():
        if abs(v - k) < 1e-6:
            return s
    if abs(v - round(v)) < 1e-6:
        return str(int(round(v)))
    return f"{v:.6f}".rstrip("0").rstrip(".")


def resolve_output_fps(video_path, explicit=None, fallback=30):
    """Resolve final encode FPS: explicit > source probe > fallback.

    Returns a float (may be fractional, e.g. 30000/1001). Use fps_to_ffmpeg_rate()
    when passing to ffmpeg -r so NTSC sources are not rounded to 30.
    """
    if explicit is not None:
        return _quantize_fps(float(explicit))
    probed = probe_video_fps(video_path)
    if probed is not None:
        return _quantize_fps(probed)
    return _quantize_fps(fallback)


@_cached_probe
def probe_media_summary(path):
    """Return basic stream/duration info for publish validation."""
    summary = {
        "ok": False,
        "duration": 0.0,
        "has_video": False,
        "has_audio": False,
        "width": 0,
        "height": 0,
        "error": "",
    }
    try:
        probe = subprocess.run(
            [
                require_executable("ffprobe"), "-v", "error",
                "-show_entries", "format=duration:stream=index,codec_type,width,height",
                "-of", "json", path,
            ],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        summary["error"] = f"ffprobe timed out after {_PROBE_TIMEOUT_SECONDS:g}s"
        return summary
    if probe.returncode != 0:
        summary["error"] = (probe.stderr or probe.stdout or "ffprobe failed").strip()[:400]
        return summary
    try:
        data = json.loads(probe.stdout or "{}")
    except json.JSONDecodeError as e:
        summary["error"] = f"ffprobe json parse failed: {e}"
        return summary

    try:
        summary["duration"] = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        summary["duration"] = 0.0

    for stream in data.get("streams") or []:
        codec_type = stream.get("codec_type")
        if codec_type == "video":
            summary["has_video"] = True
            try:
                summary["width"] = int(stream.get("width") or 0)
                summary["height"] = int(stream.get("height") or 0)
            except (TypeError, ValueError):
                pass
        elif codec_type == "audio":
            summary["has_audio"] = True

    summary["ok"] = summary["duration"] > 0 and summary["has_video"]
    if not summary["ok"] and not summary["error"]:
        summary["error"] = "missing video stream or non-positive duration"
    return summary
