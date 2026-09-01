#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""twitch_chat_burn render-option regressions (audit findings O5/O6/O2/O4).

- O5: badge color lookup must be case-insensitive; capitalized Twitch badge
  titles ("Broadcaster") used to miss the lowercase table and render gray.
- O6: a large negative --offset that pushes every message before t=0 must be
  counted and warned about instead of silently producing a message-free render.
- O2: the eager pre-render loop must only rasterize messages the scheduler
  actually placed on screen (dropped entries were rendered and never used).
- O4: --output-fps accepts exact rationals ("30000/1001") and the exact rate
  reaches ffmpeg -r; plain float input behaves exactly as before.
"""

from __future__ import annotations

import argparse
import re

import pytest

from helpers import load_module


@pytest.fixture(scope="module")
def burn():
    return load_module("twitch_chat_burn_render_options", "twitch_chat_burn.py")


# ---------------------------------------------------------------------------
# O5: badge colors
# ---------------------------------------------------------------------------


def test_badge_color_lookup_is_case_insensitive(burn):
    assert burn.badge_color_for("Broadcaster") == (255, 50, 50)
    assert burn.badge_color_for("Moderator") == (0, 160, 0)
    assert burn.badge_color_for("VIP") == (213, 0, 213)
    assert burn.badge_color_for("Subscriber") == (100, 100, 255)
    # Already-lowercase titles keep their exact color (no behavior change).
    assert burn.badge_color_for("premium") == (0, 169, 255)
    assert burn.badge_color_for("verified") == (0, 169, 255)


def test_badge_color_normalizes_whitespace_and_suffix(burn):
    assert burn.badge_color_for("  moderator ") == (0, 160, 0)
    assert burn.badge_color_for("subscriber-6") == (100, 100, 255)
    assert burn.badge_color_for("Broadcaster-1") == (255, 50, 50)


def test_badge_color_unknown_and_empty_fall_back_to_gray(burn):
    gray = (85, 85, 85)
    assert burn.badge_color_for("founder") == gray
    assert burn.badge_color_for("staff-xyz") == gray
    assert burn.badge_color_for("") == gray
    assert burn.badge_color_for(None) == gray


@pytest.mark.smoke
def test_badge_pixel_uses_badge_color_not_gray(tmp_path, make_test_video, burn):
    """End-to-end: a capitalized badge title must draw red, not the gray fallback."""
    from common_utils import resolve_font_paths
    from overlay_config import OverlayConfig

    video = make_test_video(duration=2.0, width=320, height=180, fps=10)
    chat = {
        "messages": [
            {
                "timestamp": 0.0,
                "author": "alice",
                "color": "#00FF00",  # green author: cannot collide with badge red
                "badges": [{"title": "Broadcaster"}],
                "fragments": [{"type": "text", "text": ": hi"}],
            }
        ],
        "emote_map": {},
    }
    config = OverlayConfig(x=0, y=0, width=200, height=120, font_size=14, fps=10)
    config.font_path, config.font_bold_path = resolve_font_paths("auto", "auto")

    burn.render_overlay(chat, str(tmp_path), str(video), config)

    from PIL import Image

    frame = Image.open(tmp_path / "overlay_frames" / "frame_00010.png").convert("RGBA")
    badge_red = sum(1 for r, g, b, _a in frame.getdata() if (r, g, b) == (255, 50, 50))
    # The 9x9 badge is ~100 solid pixels; gray fallback (old bug) would yield 0.
    assert badge_red >= 50, f"badge not rendered red, red pixels={badge_red}"


# ---------------------------------------------------------------------------
# O6: negative-offset messages dropped before t=0
# ---------------------------------------------------------------------------


def _msg(ts: float) -> dict:
    return {"timestamp": ts, "author": "u", "fragments": [], "badges": []}


def test_schedule_counts_and_warns_when_all_messages_before_start(burn, capsys):
    messages = [_msg(-100.0) for _ in range(3)]
    schedule = burn.schedule_messages(
        messages,
        msg_line_count={},
        duration=10.0,
        max_visible=10,
        msg_lifetime=14.0,
    )
    assert schedule == [], "messages ending before t=0 must not be scheduled"
    out = capsys.readouterr().out
    assert "3/3" in out
    assert "无任何消息上屏" in out
    assert "--offset" in out


def test_schedule_mixed_negative_and_in_window_no_offset_hint(burn, capsys):
    messages = [_msg(-100.0), _msg(-100.0), _msg(1.0), _msg(2.0)]
    schedule = burn.schedule_messages(
        messages,
        msg_line_count={2: 1, 3: 1},
        duration=10.0,
        max_visible=10,
        msg_lifetime=14.0,
    )
    assert [row[3] for row in schedule] == [2, 3], "in-window messages must survive"
    out = capsys.readouterr().out
    assert "--offset" not in out, f"false-positive offset hint: {out!r}"
    assert "无任何消息上屏" not in out


def test_schedule_majority_before_start_but_visible_no_offset_hint(burn, capsys):
    """ >50% early drops alone must not warn; the blank-render conjunction matters. """
    messages = [_msg(-100.0) for _ in range(6)] + [_msg(1.0 + i) for i in range(5)]
    schedule = burn.schedule_messages(
        messages,
        msg_line_count={i: 1 for i in range(6, 11)},
        duration=10.0,
        max_visible=10,
        msg_lifetime=14.0,
    )
    assert len(schedule) == 5
    out = capsys.readouterr().out
    assert "--offset" not in out, f"false-positive offset hint: {out!r}"
    assert "无任何消息上屏" not in out


# ---------------------------------------------------------------------------
# O2: eager pre-render must only cover scheduled messages
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_eager_prerender_skips_messages_dropped_by_scheduler(
    tmp_path, make_test_video, burn, capsys
):
    """Dropped entries (timestamp >= duration / ended before t=0) must not be
    pre-rendered; the composition loop only ever draws msg_schedule rows."""
    from common_utils import resolve_font_paths
    from overlay_config import OverlayConfig

    video = make_test_video(duration=2.0, width=320, height=180, fps=10)
    chat = {
        "messages": [
            {
                "timestamp": 0.5,
                "author": "a",
                "color": "#FFFFFF",
                "badges": [],
                "fragments": [{"type": "text", "text": ": hi"}],
            },
            {   # timestamp >= duration -> dropped_past_duration
                "timestamp": 5.0,
                "author": "b",
                "color": "#FFFFFF",
                "badges": [],
                "fragments": [{"type": "text", "text": ": late"}],
            },
            {   # timestamp + msg_lifetime <= 0 -> dropped before start
                "timestamp": -100.0,
                "author": "c",
                "color": "#FFFFFF",
                "badges": [],
                "fragments": [{"type": "text", "text": ": early"}],
            },
        ],
        "emote_map": {},
    }
    config = OverlayConfig(x=0, y=0, width=200, height=120, font_size=14, fps=10)
    config.font_path, config.font_bold_path = resolve_font_paths("auto", "auto")

    burn.render_overlay(chat, str(tmp_path), str(video), config)

    out = capsys.readouterr().out
    scheduled = re.search(r"调度\(lanes\): (\d+) 条消息", out)
    assert scheduled, f"schedule log missing:\n{out}"
    assert scheduled.group(1) == "1"
    rendered = re.search(r"渲染 (\d+) 条消息图片", out)
    assert rendered, f"pre-render log missing:\n{out}"
    assert rendered.group(1) == "1", (
        f"eager loop rendered {rendered.group(1)} images; only the 1 scheduled "
        f"message should be pre-rendered"
    )


# ---------------------------------------------------------------------------
# O4: --output-fps rational parsing
# ---------------------------------------------------------------------------


def test_output_fps_parses_exact_rational(burn):
    fps = burn.parse_output_fps_arg("30000/1001")
    assert fps == pytest.approx(30000 / 1001, abs=1e-12)
    # Stored as float; the NTSC family re-snaps exactly on the way to -r.
    resolved = burn.resolve_output_fps("missing.mp4", explicit=fps, fallback=30)
    assert burn.fps_to_ffmpeg_rate(resolved) == "30000/1001"


@pytest.mark.parametrize(
    ("text", "expected_rate"),
    [
        ("24000/1001", "24000/1001"),
        ("30000/1001", "30000/1001"),
        ("60000/1001", "60000/1001"),
        ("30/1", "30"),
        ("60/1", "60"),
    ],
)
def test_output_fps_rational_family_reaches_ffmpeg_rate_exact(burn, text, expected_rate):
    resolved = burn.resolve_output_fps(
        "missing.mp4", explicit=burn.parse_output_fps_arg(text), fallback=30
    )
    assert burn.fps_to_ffmpeg_rate(resolved) == expected_rate, text


def test_output_fps_plain_float_behavior_unchanged(burn):
    assert burn.parse_output_fps_arg("29.97") == pytest.approx(29.97)
    assert burn.parse_output_fps_arg("24") == 24.0
    assert burn.parse_output_fps_arg(" 59.94 ") == pytest.approx(59.94)
    resolved = burn.resolve_output_fps("missing.mp4", explicit=29.97, fallback=30)
    assert burn.fps_to_ffmpeg_rate(resolved) == "30000/1001"
    resolved = burn.resolve_output_fps("missing.mp4", explicit=24, fallback=30)
    assert burn.fps_to_ffmpeg_rate(resolved) == "24"


@pytest.mark.parametrize(
    "bad",
    ["30000/0", "abc", "1/2/3", "30000/1001x", "", "nan", "inf"],
)
def test_output_fps_rejects_invalid_values(burn, bad):
    with pytest.raises(argparse.ArgumentTypeError):
        burn.parse_output_fps_arg(bad)
