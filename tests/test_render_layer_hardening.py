#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hardening tests for the render layer (A6 wave).

- P-5: FrameRenderer.text_width memoizes per-string getbbox results.
- P-5: static-message layout is computed once per render session and shared
  between the line-count prepass (calc_msg_lines) and bitmap rasterization
  (render_message); animated messages are not cached (per-frame layout kept).
- P-6: progress coverage counting is maintained via a parallel set, so
  repeated writes of the same frame index are not double-counted.
"""

from __future__ import annotations

from pathlib import Path
import sys
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from overlay_config import OverlayConfig  # noqa: E402
import overlay_render  # noqa: E402


class _FakeFont:
    """Counts getbbox calls; 1 unit per character for deterministic metrics."""

    def __init__(self):
        self.getbbox_calls = 0

    def getbbox(self, s: str):
        self.getbbox_calls += 1
        w = len(s or "")
        return (0, 0, w, 10)

    def getmask(self, text, *args, **kwargs):
        # draw.text rasterizes through getmask -> draw.draw_bitmap(mask), which
        # requires an ImagingCore; delegate to PIL's built-in bitmap font so no
        # system font file is needed.
        from PIL import ImageFont as _ImageFont

        mask_font = getattr(self, "_mask_font", None)
        if mask_font is None:
            mask_font = _ImageFont.load_default()
            self._mask_font = mask_font
        return mask_font.getmask(str(text) or " ")


def _make_messages(count=5, text="hello world"):
    return [
        {
            "timestamp": float(i) * 0.5,
            "author": f"u{i}",
            "color": "#ffffff",
            "badges": [],
            "fragments": [{"type": "text", "text": text}],
        }
        for i in range(count)
    ]


def _make_renderer(messages, font):
    config = OverlayConfig(width=200, height=120, font_size=14)
    with mock.patch(
        "PIL.ImageFont.truetype", side_effect=[font, _FakeFont()]
    ), mock.patch("PIL.Image.open") as fake_open:
        # No emotes: emote_map empty means Image.open is never called, but the
        # patch guards against accidental asset probing in the test env.
        fake_open.side_effect = AssertionError("no emote assets expected")
        renderer = overlay_render.FrameRenderer(messages, {}, config)
    return renderer


# --- P-5a: text_width memoization ---

def test_text_width_memoizes_same_string():
    font = _FakeFont()
    renderer = _make_renderer(_make_messages(), font)
    first = renderer.text_width("hello")
    calls_after_first = font.getbbox_calls
    assert calls_after_first >= 1
    for _ in range(5):
        assert renderer.text_width("hello") == first
    assert font.getbbox_calls == calls_after_first, (
        "text_width must hit the cache for repeated strings"
    )
    # A different string still goes to the font once.
    renderer.text_width("hello!")
    assert font.getbbox_calls == calls_after_first + 1


# --- P-5b: static-message layout reuse ---

def test_static_message_layout_computed_once_across_calc_and_render():
    font = _FakeFont()
    messages = _make_messages(3)
    renderer = _make_renderer(messages, font)

    with mock.patch.object(
        overlay_render, "layout_message_lines", wraps=overlay_render.layout_message_lines
    ) as spy:
        # Prepass measures all 3 messages...
        renderer.measure_message_lines(messages, duration=10.0)
        # ...and each message is then rasterized once (static, cold cache).
        for idx in range(3):
            renderer.message_image(idx)
        assert spy.call_count == 3, (
            f"each static message must be laid out exactly once, got {spy.call_count}"
        )


def test_static_message_layout_matches_uncached_result():
    font = _FakeFont()
    messages = _make_messages(1, text="word " * 30)
    renderer = _make_renderer(messages, font)

    cached_lines, cached_header, cached_n = renderer._layout_for_message(
        messages[0], truncate_with_ellipsis=True
    )
    uncached_lines, uncached_header, uncached_n = overlay_render.layout_message_lines(
        messages[0],
        max_w=renderer.max_w,
        font=renderer.font,
        font_bold=renderer.font_bold,
        text_width_fn=renderer.text_width,
        emote_width_fn=renderer.emote_width,
        emote_available_fn=lambda cls: cls in renderer.emote_imgs,
        max_message_lines=renderer.max_message_lines,
        truncate_with_ellipsis=True,
        padding=renderer.padding,
        badge_size=renderer.badge_size,
        gap=renderer.gap,
        indent=renderer.indent,
    )
    assert cached_n == uncached_n
    assert cached_lines == uncached_lines
    assert cached_header == uncached_header


def test_layout_cache_uses_object_identity_key():
    """Two distinct message dicts with identical content must each be laid out."""
    font = _FakeFont()
    a = {"author": "u", "badges": [], "fragments": [{"type": "text", "text": "same"}]}
    b = {"author": "u", "badges": [], "fragments": [{"type": "text", "text": "same"}]}
    renderer = _make_renderer([a, b], font)

    with mock.patch.object(
        overlay_render, "layout_message_lines", wraps=overlay_render.layout_message_lines
    ) as spy:
        renderer.calc_msg_lines(a)
        renderer.calc_msg_lines(b)
        assert spy.call_count == 2, "identical content under different ids must not share cache"


def test_layout_cache_size_matches_message_count():
    font = _FakeFont()
    messages = _make_messages(7)
    renderer = _make_renderer(messages, font)
    renderer.measure_message_lines(messages, duration=10.0)
    assert len(renderer._layout_cache) == 7


# --- P-6: coverage counting without list re-set ---

def test_written_index_set_deduplicates_repeated_writes():
    """Simulated frame writes: the parallel set counts unique indexes only."""
    written_indexes: list[int] = []
    written_index_set: set[int] = set()

    def record(idx):
        written_indexes.append(idx)
        written_index_set.add(idx)

    for idx in (0, 1, 1, 2, 2, 2, 3):
        record(idx)

    # Old behavior: covered = len(set(written_indexes)) — same value, but the
    # new maintenance path is O(1) per write.
    assert len(written_index_set) == len(set(written_indexes)) == 4
    # The list is still intact for expand_frame_sequence_for_ffmpeg.
    assert written_indexes == [0, 1, 1, 2, 2, 2, 3]


def test_render_overlay_maintains_written_set_alongside_list():
    """render_overlay keeps list and set in lockstep (behavior-equivalence check)."""
    import inspect

    source = inspect.getsource(overlay_render.render_overlay)
    assert "written_index_set.add(out_frame_num)" in source
    # Every list append is paired with a set add.
    assert source.count("written_indexes.append(out_frame_num)") == source.count(
        "written_index_set.add(out_frame_num)"
    )
    # The progress print no longer rebuilds a set from the list each tick.
    assert "len(set(written_indexes))" not in source
    assert "len(written_index_set)" in source
    # The list is still handed to expand_frame_sequence_for_ffmpeg.
    assert "expand_frame_sequence_for_ffmpeg(frames_dir, total_frames, written_indexes)" in source
