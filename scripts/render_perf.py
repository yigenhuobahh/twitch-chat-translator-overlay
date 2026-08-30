#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Frame-sequence performance helpers: static reuse, blank-gap skipping, disk headroom."""

from __future__ import annotations

import os
from pathlib import Path
import shutil

MIN_RENDER_DISK_RESERVE_BYTES = 512 * 1024 * 1024


def ensure_render_disk_headroom(
    path: str | Path,
    *,
    reserve_bytes: int = MIN_RENDER_DISK_RESERVE_BYTES,
) -> int | None:
    """Refuse further frame materialization before the filesystem is exhausted."""
    try:
        free_bytes = int(shutil.disk_usage(Path(path)).free)
    except OSError:
        return None
    reserve = max(0, int(reserve_bytes))
    if free_bytes < reserve:
        raise RuntimeError(
            "overlay frame filesystem is low on free space "
            f"({free_bytes / (1024 * 1024):.1f} MiB free; "
            f"{reserve / (1024 * 1024):.0f} MiB reserve required)"
        )
    return free_bytes


def frame_path(frames_dir: str | Path, frame_index: int) -> Path:
    return Path(frames_dir) / f"frame_{int(frame_index):05d}.png"


def write_or_reuse_frame(
    frames_dir: str | Path,
    frame_index: int,
    image,
    *,
    reuse_from: int | None = None,
    prefer_hardlink: bool = True,
) -> str:
    """
    Save a PNG frame, or reuse a previous identical frame via hardlink/copy.

    Returns action: "write" | "hardlink" | "copy"
    """
    dest = frame_path(frames_dir, frame_index)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if reuse_from is not None and reuse_from != frame_index:
        src = frame_path(frames_dir, reuse_from)
        if src.is_file():
            try:
                if dest.exists():
                    dest.unlink()
            except OSError:
                pass
            if prefer_hardlink:
                try:
                    os.link(src, dest)
                    return "hardlink"
                except OSError:
                    pass
            shutil.copy2(src, dest)
            return "copy"
        if image is None:
            raise FileNotFoundError(f"reuse source missing: {src}")

    if image is None:
        raise ValueError("image is required when reuse_from is not usable")

    # Fresh encode; compress_level=3 is much faster than Pillow default 6 on bulk frames.
    image.save(dest, format="PNG", compress_level=3)
    return "write"


def blank_gap_frame_indexes(
    start_i: int,
    end_i: int,
    *,
    hold_stride: int,
) -> list[int]:
    """
    For a fully blank segment [start_i, end_i), only emit keyframes every hold_stride,
    always including the first index. end_i is exclusive.
    """
    if end_i <= start_i:
        return []
    stride = max(1, int(hold_stride))
    # range(start_i, end_i, stride) is non-empty (end_i > start_i, stride >= 1)
    # and always starts exactly at start_i, so no first-index fixup is needed.
    indexes = list(range(start_i, end_i, stride))
    # Ensure last covered frame before end exists when stride skips tail.
    last_needed = end_i - 1
    if indexes[-1] != last_needed:
        indexes.append(last_needed)
    return indexes


def missing_frame_indexes(
    frames_dir: str | Path,
    total_frames: int,
    *,
    start: int = 0,
) -> list[int]:
    """Return frame indexes in [start, start+total_frames) missing on disk."""
    frames_dir = Path(frames_dir)
    total = int(total_frames)
    if total <= 0:
        return []
    start = int(start)
    missing: list[int] = []
    for frame_i in range(start, start + total):
        if not frame_path(frames_dir, frame_i).is_file():
            missing.append(frame_i)
    return missing


def assert_contiguous_frame_sequence(
    frames_dir: str | Path,
    total_frames: int,
    *,
    start: int = 0,
    context: str = "frame sequence",
) -> None:
    """Fail fast when frame_XXXXX.png has gaps (FFmpeg would silently shorten)."""
    missing = missing_frame_indexes(frames_dir, total_frames, start=start)
    if not missing:
        return
    preview = ", ".join(f"frame_{i:05d}.png" for i in missing[:12])
    more = "" if len(missing) <= 12 else f" ... (+{len(missing) - 12} more)"
    raise RuntimeError(
        f"{context}: missing {len(missing)} frame(s) in "
        f"[{start:05d}..{start + int(total_frames) - 1:05d}]; first gaps: {preview}{more}. "
        f"Refuse to publish incomplete overlay."
    )


def expand_frame_sequence_for_ffmpeg(
    frames_dir: str | Path,
    total_frames: int,
    written_indexes: list[int],
) -> dict[str, int]:
    """
    Ensure frame_00000..frame_{N-1} exist for FFmpeg sequence demuxer.

    If blank-gap / static reuse only wrote sparse keyframes, fill missing numbers
    by hardlink/copy from the nearest previous written frame.

    Raises RuntimeError if the contiguous sequence cannot be materialized
    (missing sources / unfilled gaps). Callers must not publish incomplete overlays.
    """
    frames_dir = Path(frames_dir)
    total_frames = int(total_frames)
    if total_frames <= 0:
        return {"filled": 0, "hardlink": 0, "copy": 0}

    written = sorted(set(int(i) for i in written_indexes if 0 <= int(i) < total_frames))
    if not written:
        # Fall back to whatever already exists on disk (resume / external write).
        existing = [
            i for i in range(total_frames) if frame_path(frames_dir, i).is_file()
        ]
        if not existing:
            raise RuntimeError(
                f"expand_frame_sequence_for_ffmpeg: no frames written for "
                f"0..{total_frames - 1} under {frames_dir}"
            )
        written = existing

    stats = {"filled": 0, "hardlink": 0, "copy": 0}
    cursor = 0
    for frame_i in range(total_frames):
        path = frame_path(frames_dir, frame_i)
        if path.is_file():
            # Advance cursor to this or previous written
            while cursor + 1 < len(written) and written[cursor + 1] <= frame_i:
                cursor += 1
            continue
        # Find nearest previous written index
        while cursor + 1 < len(written) and written[cursor + 1] <= frame_i:
            cursor += 1
        src_idx = written[cursor]
        if src_idx > frame_i:
            # No previous keyframe to fill from — cannot invent earlier frames.
            raise RuntimeError(
                f"expand_frame_sequence_for_ffmpeg: cannot fill frame_{frame_i:05d}.png; "
                f"nearest written keyframe is frame_{src_idx:05d}.png (no earlier source)"
            )
        src = frame_path(frames_dir, src_idx)
        if not src.is_file():
            raise RuntimeError(
                f"expand_frame_sequence_for_ffmpeg: missing source frame_{src_idx:05d}.png "
                f"while filling frame_{frame_i:05d}.png under {frames_dir}"
            )
        try:
            os.link(src, path)
            stats["hardlink"] += 1
        except OSError:
            ensure_render_disk_headroom(frames_dir)
            shutil.copy2(src, path)
            stats["copy"] += 1
        stats["filled"] += 1

    # Hard guarantee for FFmpeg image2 demuxer: every index must exist.
    assert_contiguous_frame_sequence(
        frames_dir,
        total_frames,
        start=0,
        context="expand_frame_sequence_for_ffmpeg",
    )
    return stats
