#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""performance-1 regression tests: FrameRenderer._layout_cache must be bounded.

The line-count prepass (measure_message_lines) lays out every message with
timestamp < duration and used to retain one layout tuple per message for the
whole render (~1 KB each; ~90 MB at 100k messages). The cache is now capped
with the same policy as the message-image bitmap cache
(resolve_message_image_cache_policy, default 256) and evicts oldest entries
(dict insertion order). layout_message_lines is a pure function of
(msg, config), so a re-layout after eviction is correctness-safe.
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
from overlay_scene import resolve_message_image_cache_policy  # noqa: E402


class _FakeFont:
    """Mirrors tests/test_render_layer_hardening.py: 1 unit per character,
    rasterization delegated to PIL's built-in bitmap font (no font file)."""

    def __init__(self):
        self.getbbox_calls = 0

    def getbbox(self, s: str):
        self.getbbox_calls += 1
        return (0, 0, len(s or ""), 10)

    def getmask(self, text, *args, **kwargs):
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
        fake_open.side_effect = AssertionError("no emote assets expected")
        renderer = overlay_render.FrameRenderer(messages, {}, config)
    return renderer


def test_layout_cache_cap_matches_message_image_policy():
    """The cap must come from resolve_message_image_cache_policy, not ad hoc."""
    font = _FakeFont()
    messages = _make_messages(10)
    renderer = _make_renderer(messages, font)
    _lazy, expected_cap, _auto = resolve_message_image_cache_policy(
        len(messages),
        False,
        getattr(OverlayConfig(), "message_image_cache_size", 256),
    )
    assert renderer._layout_cache_cap == expected_cap


def test_layout_cache_bounded_and_evicts_when_over_cap():
    """Prepass over N > cap messages keeps at most cap entries (oldest evicted)."""
    font = _FakeFont()
    n = 400  # default cap is 256
    messages = _make_messages(n, text="word " * 12)
    renderer = _make_renderer(messages, font)
    renderer.measure_message_lines(messages, duration=1e18)
    assert len(renderer._layout_cache) <= renderer._layout_cache_cap < n


def test_layout_cache_reevicted_entry_relayouts_identically():
    """Eviction is correctness-safe: a re-laid-out message yields the same tuple."""
    font = _FakeFont()
    messages = _make_messages(300, text="word " * 12)
    renderer = _make_renderer(messages, font)
    renderer.measure_message_lines(messages, duration=1e18)
    # messages[0] is among the oldest -> evicted once the cap is exceeded.
    first, _header, first_n = renderer._layout_for_message(
        messages[0], truncate_with_ellipsis=True
    )
    again, _header2, again_n = renderer._layout_for_message(
        messages[0], truncate_with_ellipsis=True
    )
    assert again_n == first_n
    assert again == first


def test_render_output_identical_with_cap_smaller_than_message_count():
    """Small scenario where the cap is exceeded: re-layout after eviction must
    reproduce identical rendered bitmaps (the cache is a pure memo)."""
    from PIL import ImageChops

    font = _FakeFont()
    messages = _make_messages(8, text="wrap " * 10)
    renderer = _make_renderer(messages, font)
    renderer.measure_message_lines(messages, duration=1e18)

    bitmaps = {}
    for idx in range(len(messages)):
        img, _nl = renderer.message_image(idx)
        bitmaps[idx] = img

    # Force a full eviction, then re-render one message and compare bitmaps.
    renderer._layout_cache.clear()
    img_again, nl_again = renderer.message_image(0)
    diff = ImageChops.difference(bitmaps[0], img_again)
    assert nl_again == renderer.msg_lines[0]
    assert diff.getbbox() is None, "re-layout after eviction changed pixels"
