#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FPS probe / resolve edges including NTSC-style 30000/1001."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import twitch_chat_burn as burn  # noqa: E402


def test_apply_relative_layout_resolves_source_scaled_values(monkeypatch):
    cfg = burn.OverlayConfig(
        x=15, y=327, width=497, height=363, font_size=15, emote_h=22,
        x_ratio=0.016, y_ratio=0.55, width_ratio=0.58, height_ratio=0.30,
        font_size_ratio=0.034,
    )
    monkeypatch.setattr(burn, "probe_video_dimensions", lambda _path: (1920, 1080))
    burn.apply_relative_layout(cfg, "video.mp4")
    assert (cfg.x, cfg.y, cfg.width, cfg.height, cfg.font_size) == (31, 594, 1114, 324, 37)
    assert cfg.emote_h == 40


def test_apply_relative_layout_geometry_ratio_keeps_explicit_emote_h(monkeypatch):
    cfg = burn.OverlayConfig(
        x=15, y=327, width=497, height=363, font_size=15, emote_h=40,
        width_ratio=0.3,
    )
    monkeypatch.setattr(burn, "probe_video_dimensions", lambda _path: (1920, 1080))
    burn.apply_relative_layout(cfg, "video.mp4")
    assert cfg.width == 576
    assert cfg.font_size == 15
    assert cfg.emote_h == 40


def test_layout_bounds_warnings_detects_default_box_outside_360p(monkeypatch):
    cfg = burn.OverlayConfig(x=15, y=327, width=497, height=363, font_size=15)
    monkeypatch.setattr(burn, "probe_video_dimensions", lambda _path: (640, 360))
    warns = burn.layout_bounds_warnings(cfg, "video.mp4")
    assert warns
    assert "画面外" in warns[0] or "画面内" in warns[0]


def test_layout_bounds_warnings_silent_when_box_fits(monkeypatch):
    cfg = burn.OverlayConfig(x=13, y=126, width=352, height=223, font_size=14)
    monkeypatch.setattr(burn, "probe_video_dimensions", lambda _path: (640, 360))
    assert burn.layout_bounds_warnings(cfg, "video.mp4") == []


def test_adapt_absolute_layout_scales_default_box_for_360p(monkeypatch):
    """run.bat / layout_default absolute pixels must auto-fit 360p sources."""
    cfg = burn.OverlayConfig(
        x=15, y=327, width=497, height=363, font_size=15, emote_h=22,
    )
    monkeypatch.setattr(burn, "probe_video_dimensions", lambda _path: (640, 360))
    note = burn.adapt_absolute_layout_to_source(cfg, "video.mp4")
    assert note is not None
    assert cfg.x >= 0 and cfg.y >= 0
    assert cfg.x + cfg.width <= 640
    assert cfg.y + cfg.height <= 360
    assert cfg.width >= 16 and cfg.height >= 16
    assert cfg.font_size >= 8
    # After adaptation the box should no longer trip bounds warnings.
    assert burn.layout_bounds_warnings(cfg, "video.mp4") == []


def test_adapt_absolute_layout_skips_when_ratios_set(monkeypatch):
    cfg = burn.OverlayConfig(
        x=15, y=327, width=497, height=363, font_size=15,
        x_ratio=0.02, y_ratio=0.35, width_ratio=0.55, height_ratio=0.62,
    )
    monkeypatch.setattr(burn, "probe_video_dimensions", lambda _path: (640, 360))
    before = (cfg.x, cfg.y, cfg.width, cfg.height)
    assert burn.adapt_absolute_layout_to_source(cfg, "video.mp4") is None
    assert (cfg.x, cfg.y, cfg.width, cfg.height) == before


def test_adapt_absolute_layout_skips_fully_inside_custom_crop(monkeypatch):
    """A user-authored box already inside a non-1080p frame must not be rewritten."""
    cfg = burn.OverlayConfig(x=10, y=40, width=200, height=120, font_size=12, emote_h=16)
    monkeypatch.setattr(burn, "probe_video_dimensions", lambda _path: (640, 360))
    before = (cfg.x, cfg.y, cfg.width, cfg.height, cfg.font_size, cfg.emote_h)
    assert burn.adapt_absolute_layout_to_source(cfg, "video.mp4") is None
    assert (cfg.x, cfg.y, cfg.width, cfg.height, cfg.font_size, cfg.emote_h) == before


def test_probe_video_fps_parses_ntsc_fraction():
    payload = {
        "streams": [
            {
                "r_frame_rate": "30000/1001",
                "avg_frame_rate": "30000/1001",
            }
        ]
    }
    fake = subprocess.CompletedProcess(
        args=["ffprobe"], returncode=0, stdout=json.dumps(payload), stderr=""
    )
    with mock.patch("twitch_chat_burn.subprocess.run", return_value=fake):
        fps = burn.probe_video_fps("dummy.mp4")
    assert fps is not None
    assert abs(fps - (30000 / 1001)) < 1e-6


def test_resolve_output_fps_preserves_ntsc_fraction():
    payload = {
        "streams": [
            {
                "r_frame_rate": "30000/1001",
                "avg_frame_rate": "30000/1001",
            }
        ]
    }
    fake = subprocess.CompletedProcess(
        args=["ffprobe"], returncode=0, stdout=json.dumps(payload), stderr=""
    )
    with mock.patch("twitch_chat_burn.subprocess.run", return_value=fake):
        fps = burn.resolve_output_fps("dummy.mp4", explicit=None, fallback=24)
        assert abs(fps - (30000 / 1001)) < 1e-6
        assert burn.fps_to_ffmpeg_rate(fps) == "30000/1001"


def test_resolve_output_fps_prefers_explicit_over_probe():
    with mock.patch("twitch_chat_burn.probe_video_fps", return_value=59.94):
        assert burn.resolve_output_fps("dummy.mp4", explicit=24, fallback=30) == 24.0
        # NTSC 59.94 explicit stays fractional
        fps = burn.resolve_output_fps("dummy.mp4", explicit=59.94, fallback=30)
        assert abs(fps - (60000 / 1001)) < 0.02
        assert burn.fps_to_ffmpeg_rate(fps) == "60000/1001"


def test_probe_video_fps_invalid_returns_none():
    fake = subprocess.CompletedProcess(args=["ffprobe"], returncode=1, stdout="", stderr="boom")
    with mock.patch("twitch_chat_burn.subprocess.run", return_value=fake):
        assert burn.probe_video_fps("dummy.mp4") is None


def test_expand_sparse_long_sequence(tmp_path: Path):
    """Simulate long sparse blank gaps without rendering full video."""
    from PIL import Image

    from render_perf import blank_gap_frame_indexes, expand_frame_sequence_for_ffmpeg, frame_path

    # ~1h @ 15fps would be 54000 frames; keep test light: 5000 frames with stride
    total = 5000
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    idxs = blank_gap_frame_indexes(0, total, hold_stride=120)
    img = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    written = []
    for i in idxs:
        p = frame_path(frames_dir, i)
        img.save(p)
        written.append(i)
    stats = expand_frame_sequence_for_ffmpeg(frames_dir, total, written)
    assert stats["filled"] > 0
    # endpoints and a middle index must exist
    assert frame_path(frames_dir, 0).is_file()
    assert frame_path(frames_dir, total - 1).is_file()
    assert frame_path(frames_dir, total // 2).is_file()


def test_find_densest_preview_start_picks_busy_cluster():
    from chat_window import find_densest_preview_start
    msgs = [{"timestamp": t} for t in (1.0, 50.0, 51.0, 52.0, 53.0, 200.0)]
    info = find_densest_preview_start(msgs, 10.0, video_duration=300.0, msg_lifetime=5.0)
    assert info["start"] >= 40.0
    assert info["score"] >= 3


def test_filter_chat_rebase_to_zero_for_dense_clip():
    from chat_window import filter_chat_for_time_window
    data = {
        "messages": [
            {"timestamp": 50.0, "fragments": []},
            {"timestamp": 51.0, "fragments": [{"type": "emote", "class": "first-a"}]},
            {"timestamp": 5.0, "fragments": []},
        ],
        "emote_map": {"first-a": "x.png", "first-b": "y.png"},
    }
    out = filter_chat_for_time_window(data, 48.0, 58.0, 5.0, rebase_to_zero=True)
    assert len(out["messages"]) == 2
    assert out["messages"][0]["timestamp"] == 2.0
    assert "first-a" in out["emote_map"] and "first-b" not in out["emote_map"]


def test_filter_chat_rebase_keeps_negative_carry_in_timestamp():
    from chat_window import filter_chat_for_time_window
    data = {
        "messages": [
            {"timestamp": 45.0, "fragments": []},  # started 5s before window; still alive with life=14
            {"timestamp": 50.0, "fragments": []},
            {"timestamp": 20.0, "fragments": []},  # expired before window
        ],
        "emote_map": {},
    }
    out = filter_chat_for_time_window(data, 50.0, 60.0, 14.0, rebase_to_zero=True)
    stamps = [m["timestamp"] for m in out["messages"]]
    assert stamps == [-5.0, 0.0]


def test_float_preview_clip_filter_keeps_deep_carry_in():
    """Float stack has no lifetime; filter horizon must not be clip_len only."""
    from chat_window import filter_chat_for_time_window
    data = {
        "messages": [
            {"timestamp": 70.0, "fragments": []},   # 30s before window — still on float stack
            {"timestamp": 95.0, "fragments": []},
            {"timestamp": 102.0, "fragments": []},
        ],
        "emote_map": {},
    }
    # Mirrors burn main(): float window_life is a large horizon, not clip length.
    out = filter_chat_for_time_window(data, 100.0, 110.0, 3600.0, rebase_to_zero=True)
    stamps = [m["timestamp"] for m in out["messages"]]
    assert stamps == [-30.0, -5.0, 2.0]


def test_trim_float_carry_in_keeps_newest_pre_window_only():
    from chat_window import filter_chat_for_time_window, trim_float_carry_in_messages
    data = {
        "messages": [
            {"timestamp": float(i), "fragments": []} for i in range(0, 100, 5)
        ] + [
            {"timestamp": 102.0, "fragments": []},
            {"timestamp": 105.0, "fragments": []},
        ],
        "emote_map": {},
    }
    wide = filter_chat_for_time_window(data, 100.0, 110.0, 3600.0, rebase_to_zero=True)
    # Many pre-window negatives after rebase; capacity 3 keeps newest 3 carry-ins + in-window.
    trimmed = trim_float_carry_in_messages(wide, 0.0, 3)
    stamps = [m["timestamp"] for m in trimmed["messages"]]
    pre = [t for t in stamps if t < 0]
    post = [t for t in stamps if t >= 0]
    assert len(pre) == 3
    assert pre == sorted(pre)[-3:] or pre == sorted(pre)
    assert pre == [-15.0, -10.0, -5.0]  # 85,90,95 rebased from 100
    assert post == [2.0, 5.0]


def test_float_preview_frame_window_anchor_then_capacity_trim():
    """preview-frame float must anchor at t so trim can drop deep history."""
    from chat_window import filter_chat_for_time_window, trim_float_carry_in_messages
    frame_t = 100.0
    data = {
        "messages": [{"timestamp": float(i), "fragments": []} for i in range(0, 101)],
        "emote_map": {},
    }
    # Mirror burn: wide life filter around [frame_t, frame_t+0.05], then trim pre at frame_t.
    wide = filter_chat_for_time_window(data, frame_t, frame_t + 0.05, 3600.0)
    trimmed = trim_float_carry_in_messages(wide, frame_t, 5)
    stamps = [m["timestamp"] for m in trimmed["messages"]]
    assert stamps == [95.0, 96.0, 97.0, 98.0, 99.0, 100.0]


def test_filter_float_prefilter_limits_deepcopy_before_trim():
    from chat_window import filter_chat_for_time_window
    data = {
        "messages": [{"timestamp": float(i), "fragments": []} for i in range(0, 200)],
        "emote_map": {},
    }
    out = filter_chat_for_time_window(
        data,
        100.0,
        110.0,
        3600.0,
        rebase_to_zero=True,
        float_capacity_lines=5,
        max_message_lines=2,
    )
    # Without prefilter this would keep ~100+ msgs; prefilter keeps capacity pre + in-window.
    assert len(out["messages"]) < 30
    meta = out["_window"]["float_prefilter"]
    assert meta["pre_window_before"] > meta["pre_window_after"]
    # Soft over-fetch: 1 line/msg so single-line stacks can fill capacity (5), not 5/2.
    assert meta["pre_window_after"] == 5
    assert meta["per_msg_lines"] == 1


def test_trim_float_carry_in_keeps_capacity_messages_for_stack_fill():
    from chat_window import filter_chat_for_time_window, trim_float_carry_in_messages
    data = {
        "messages": [{"timestamp": float(i), "fragments": []} for i in range(90, 105)],
        "emote_map": {},
    }
    wide = filter_chat_for_time_window(data, 100.0, 110.0, 3600.0, rebase_to_zero=True)
    # capacity 4 => newest 4 pre-window messages (line≈1); stack enforces multi-line later.
    trimmed = trim_float_carry_in_messages(wide, 0.0, 4, max_message_lines=2)
    pre = [m["timestamp"] for m in trimmed["messages"] if m["timestamp"] < 0]
    assert len(pre) == 4


def test_find_densest_without_video_duration_forces_head():
    from chat_window import find_densest_preview_start
    info = find_densest_preview_start(
        [{"timestamp": t} for t in (100.0, 101.0, 102.0)],
        10.0,
        video_duration=None,
        msg_lifetime=5.0,
    )
    assert info["start"] == 0.0
    assert "warning" in info
