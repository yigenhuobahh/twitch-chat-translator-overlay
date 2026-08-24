#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One immutable mapping for cuts on a continuous media timeline."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import math

Interval = tuple[float, float]


class CutTimelineError(ValueError):
    """Raised when a timeline duration or cut interval is invalid."""


@dataclass(frozen=True)
class CutTimeline:
    """Normalized cuts plus conversions between original and retained time.

    ``cuts`` use half-open intervals: a timestamp at ``start`` is removed while
    a timestamp at ``end`` remains and maps immediately after the prior media.
    """

    original_duration: float
    cuts: tuple[Interval, ...]

    @classmethod
    def from_ranges(
        cls,
        ranges: Iterable[Interval] | None,
        original_duration: float,
    ) -> CutTimeline:
        try:
            total = float(original_duration)
        except (TypeError, ValueError) as exc:
            raise CutTimelineError(f"总时长必须是有限数值: {original_duration!r}") from exc
        if not math.isfinite(total):
            raise CutTimelineError(f"总时长必须是有限数值: {original_duration!r}")
        total = max(0.0, total)

        clipped: list[Interval] = []
        for raw_range in ranges or ():
            try:
                raw_start, raw_end = raw_range
                start = float(raw_start)
                end = float(raw_end)
            except (TypeError, ValueError) as exc:
                raise CutTimelineError(
                    f"裁切范围必须是两个有限数值: {raw_range!r}"
                ) from exc
            if not math.isfinite(start) or not math.isfinite(end):
                raise CutTimelineError(
                    f"裁切范围必须是有限数值: {(raw_start, raw_end)!r}"
                )
            if end <= start:
                raise CutTimelineError(f"无效切除范围: {start:g}-{end:g}")
            start = min(total, max(0.0, start))
            end = min(total, max(0.0, end))
            if end > start:
                clipped.append((start, end))
        clipped.sort()

        merged: list[Interval] = []
        for start, end in clipped:
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        return cls(original_duration=total, cuts=tuple(merged))

    @property
    def removed_duration(self) -> float:
        return sum(end - start for start, end in self.cuts)

    @property
    def remaining_duration(self) -> float:
        return max(0.0, self.original_duration - self.removed_duration)

    def map_time(self, timestamp: float) -> float | None:
        """Map original time to retained time, or ``None`` when cut away."""
        try:
            value = float(timestamp)
        except (TypeError, ValueError) as exc:
            raise CutTimelineError(f"时间戳必须是有限数值: {timestamp!r}") from exc
        if not math.isfinite(value):
            raise CutTimelineError(f"时间戳必须是有限数值: {timestamp!r}")

        removed = 0.0
        for start, end in self.cuts:
            if value < start:
                break
            if value < end:
                return None
            removed += end - start
        return value - removed

    def split_interval(self, start: float, end: float) -> tuple[Interval, ...]:
        """Return retained original-time portions of ``[start, end)``."""
        try:
            left = float(start)
            right = float(end)
        except (TypeError, ValueError) as exc:
            raise CutTimelineError(f"时间范围必须是有限数值: {(start, end)!r}") from exc
        if not math.isfinite(left) or not math.isfinite(right):
            raise CutTimelineError(f"时间范围必须是有限数值: {(start, end)!r}")
        if right <= left:
            return ()
        left = min(self.original_duration, max(0.0, left))
        right = min(self.original_duration, max(0.0, right))
        if right <= left:
            return ()

        retained: list[Interval] = []
        cursor = left
        for cut_start, cut_end in self.cuts:
            if cut_end <= cursor:
                continue
            if cut_start >= right:
                break
            if cut_start > cursor:
                retained.append((cursor, min(cut_start, right)))
            cursor = max(cursor, cut_end)
            if cursor >= right:
                break
        if cursor < right:
            retained.append((cursor, right))
        return tuple((part_start, part_end) for part_start, part_end in retained if part_end > part_start)

    def local_keep_ranges(self, segment_start: float, segment_duration: float) -> tuple[Interval, ...]:
        """Return retained ranges in a segment-local timeline."""
        try:
            start = float(segment_start)
            duration = float(segment_duration)
        except (TypeError, ValueError) as exc:
            raise CutTimelineError(
                f"片段范围必须是有限数值: {(segment_start, segment_duration)!r}"
            ) from exc
        if not math.isfinite(start) or not math.isfinite(duration):
            raise CutTimelineError(f"片段范围必须是有限数值: {(segment_start, segment_duration)!r}")
        if duration <= 0:
            return ()
        return tuple(
            (part_start - start, part_end - start)
            for part_start, part_end in self.split_interval(start, start + duration)
        )
