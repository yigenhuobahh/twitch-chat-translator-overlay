"""Focused contracts for one continuous media/chat cut timeline."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cut_timeline import CutTimeline, CutTimelineError


def test_timeline_normalizes_cuts_and_maps_half_open_boundaries():
    timeline = CutTimeline.from_ranges(
        [(9, 12), (2, 4), (6, 8), (3, 5), (-2, 0.5), (30, 40)],
        20,
    )

    assert timeline.cuts == ((0.0, 0.5), (2.0, 5.0), (6.0, 8.0), (9.0, 12.0))
    assert timeline.removed_duration == 8.5
    assert timeline.remaining_duration == 11.5
    assert timeline.map_time(0.0) is None
    assert timeline.map_time(0.5) == 0.0
    assert timeline.map_time(2.0) is None
    assert timeline.map_time(5.0) == 1.5
    assert timeline.map_time(6.0) is None
    assert timeline.map_time(8.0) == 2.5
    assert timeline.map_time(12.0) == 3.5
    assert timeline.map_time(20.0) == 11.5


def test_timeline_splits_global_and_segment_local_intervals():
    timeline = CutTimeline.from_ranges([(2, 4), (6, 8), (10, 12)], 20)

    assert timeline.split_interval(1, 14) == ((1.0, 2.0), (4.0, 6.0), (8.0, 10.0), (12.0, 14.0))
    assert timeline.local_keep_ranges(5, 7) == ((0.0, 1.0), (3.0, 5.0))
    assert timeline.local_keep_ranges(6, 2) == ()


@pytest.mark.parametrize(
    ("ranges", "duration"),
    [
        ([(1, 1)], 10),
        ([(float("nan"), 2)], 10),
        ([(1, 2)], float("inf")),
        ([(1, 2, 3)], 10),
        ([(1,)], 10),
    ],
)
def test_timeline_rejects_invalid_values(ranges, duration):
    with pytest.raises(CutTimelineError):
        CutTimeline.from_ranges(ranges, duration)
