#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Frame-sequence performance helpers: static reuse, blank-gap skipping, stage timing."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import time


@dataclass
class StageTimer:
    """Collect named stage durations for run_meta / console summary."""

    stages: dict[str, float] = field(default_factory=dict)
    _starts: dict[str, float] = field(default_factory=dict)

    def start(self, name: str) -> None:
        self._starts[name] = time.perf_counter()

    def stop(self, name: str) -> float:
        begin = self._starts.pop(name, None)
        if begin is None:
            return 0.0
        elapsed = time.perf_counter() - begin
        self.stages[name] = self.stages.get(name, 0.0) + elapsed
        return elapsed

    def mark(self, name: str, seconds: float) -> None:
        self.stages[name] = self.stages.get(name, 0.0) + float(seconds)

    def summary_lines(self) -> list[str]:
        if not self.stages:
            return []
        total = sum(self.stages.values()) or 1.0
        lines = []
        for name, sec in self.stages.items():
            lines.append(f"  - {name}: {sec:.1f}s ({sec / total * 100:.0f}%)")
        lines.append(f"  - total_tracked: {sum(self.stages.values()):.1f}s")
        return lines

    def to_dict(self) -> dict[str, float]:
        return {k: round(v, 3) for k, v in self.stages.items()}


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


def is_blank_visible(visible: Iterable) -> bool:
    """True when no messages are visible (fully transparent frames)."""
    return not list(visible)


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
    indexes = list(range(start_i, end_i, stride))
    if not indexes or indexes[0] != start_i:
        indexes.insert(0, start_i)
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


def estimate_disk_bytes(path: str | Path) -> int:
    path = Path(path)
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total
