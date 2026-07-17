#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Long-term correctness, resource-bound, and scaling regressions."""

from __future__ import annotations

from collections import OrderedDict
import gc
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

from helpers import load_module


@pytest.fixture(scope="module")
def burn():
    return load_module("twitch_chat_burn_longterm", "twitch_chat_burn.py")


def test_normalize_text_preserves_supplementary_unicode(burn):
    source = "emoji: 😀\u200d🚀, CJK-B: 𠀀, compatibility: 𝔸"
    normalized = burn.normalize_text(source)

    assert "😀\u200d🚀" in normalized
    assert "𠀀" in normalized
    assert normalized.endswith("A")


def test_read_html_mixed_invalid_utf8_uses_replacement(tmp_path: Path):
    parser = load_module("chat_parser_longterm_encoding", "chat_parser.py")
    html_path = tmp_path / "mixed.html"
    html_path.write_bytes("你好".encode() + b"\xff" + "世界".encode())

    assert parser._read_html_text(str(html_path)) == "你好�世界"


def test_read_html_still_supports_genuine_latin1(tmp_path: Path):
    parser = load_module("chat_parser_longterm_latin1", "chat_parser.py")
    html_path = tmp_path / "latin1.html"
    html_path.write_bytes(b"caf\xe9")

    assert parser._read_html_text(str(html_path)) == "café"


def test_render_preset_output_fps_remains_fractional():
    preset = load_module("render_preset_longterm", "render_preset.py")
    normalized = preset.normalize_render_dict({"output_fps": "29.97"})

    assert normalized["output_fps"] == pytest.approx(29.97)
    assert isinstance(normalized["output_fps"], float)


def _valid_runtime_args(**overrides):
    values = {
        "fps": 15,
        "output_fps": 29.97,
        "width": 497,
        "height": 363,
        "font_size": 15,
        "emote_height": 22,
        "max_visible": 0,
        "message_image_cache_size": 256,
        "stack_mode": "lanes",
        "msg_lifetime": 14.0,
        "max_message_lines": 0,
        "min_visible_seconds": 0.0,
        "arrival_interval": 0.0,
        "x_ratio": 0.0,
        "y_ratio": 0.0,
        "width_ratio": 0.0,
        "height_ratio": 0.0,
        "font_size_ratio": 0.0,
        "preview_frame": None,
        "preview_clip": None,
        "offset": None,
        "blank_hold_seconds": 0.5,
        "bg_alpha": 255,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_final_runtime_validation_accepts_fractional_output_fps(burn):
    args = _valid_runtime_args()
    burn._validate_runtime_args(args)

    with pytest.raises(ValueError, match="output-fps"):
        burn._validate_runtime_args(_valid_runtime_args(output_fps=240.01))
    with pytest.raises(ValueError, match="message-image-cache-size"):
        burn._validate_runtime_args(_valid_runtime_args(message_image_cache_size=7))


@pytest.mark.parametrize("offset", [float("nan"), float("inf"), float("-inf")])
def test_final_runtime_validation_rejects_nonfinite_offset(burn, offset):
    with pytest.raises(ValueError, match="offset must be finite"):
        burn._validate_runtime_args(_valid_runtime_args(offset=offset))


def _rounded_schedule(schedule):
    return [
        (round(start, 4), round(end, 4), lane, idx, lines)
        for start, end, lane, idx, lines in schedule
    ]


@pytest.mark.parametrize(
    ("timestamps", "line_counts", "max_visible", "lifetime", "expected"),
    [
        (
            [0.0, 0.2, 0.4, 0.6],
            [1, 1, 1, 1],
            2,
            1.0,
            [
                (0.0, 0.4, 0, 0, 1),
                (0.2, 0.6, 1, 1, 1),
                (0.4, 1.4, 0, 2, 1),
                (0.6, 1.6, 1, 3, 1),
            ],
        ),
        (
            [0.0, 0.5, 1.0],
            [2, 1, 2],
            3,
            5.0,
            [
                (0.0, 1.0, 0, 0, 2),
                (0.5, 5.5, 2, 1, 1),
                (1.0, 6.0, 0, 2, 2),
            ],
        ),
    ],
)
def test_lane_scheduler_small_sample_equivalence(
    burn,
    timestamps,
    line_counts,
    max_visible,
    lifetime,
    expected,
):
    messages = [{"timestamp": stamp} for stamp in timestamps]
    result = burn.schedule_messages(
        messages,
        {i: count for i, count in enumerate(line_counts)},
        duration=20.0,
        max_visible=max_visible,
        msg_lifetime=lifetime,
    )

    assert _rounded_schedule(result) == expected


def _naive_visible(schedule, current_t):
    visible = [
        (lane, idx, start, end, lines)
        for start, end, lane, idx, lines in schedule
        if start <= current_t < end
    ]
    visible.sort(key=lambda row: row[0])
    return visible


def test_lane_visibility_cursor_matches_naive_and_supports_rewind(burn):
    messages = [{"timestamp": value} for value in (0.0, 0.2, 0.4, 1.2, 2.0)]
    schedule = burn.schedule_messages(
        messages,
        {0: 2, 1: 1, 2: 1, 3: 2, 4: 1},
        duration=5.0,
        max_visible=3,
        msg_lifetime=1.5,
    )
    cursor = burn._LaneVisibilityCursor(schedule)

    for current_t in (-0.1, 0.0, 0.2, 0.4, 1.0, 1.2, 1.7, 2.0, 3.5, 0.75):
        assert cursor.at(current_t) == _naive_visible(schedule, current_t)


def test_lane_scheduler_dense_scaling_is_not_quadratic(burn):
    def elapsed(count):
        messages = [{"timestamp": i * 0.001} for i in range(count)]
        lines = {i: 1 for i in range(count)}
        gc.collect()
        started = time.perf_counter()
        schedule = burn.schedule_messages(
            messages,
            lines,
            duration=20.0,
            max_visible=10,
            msg_lifetime=10.0,
        )
        assert len(schedule) == count
        return time.perf_counter() - started

    small = elapsed(1500)
    large = elapsed(6000)

    # 4x input should stay near linear. The absolute floor avoids timer noise.
    assert large < max(0.75, small * 9.0), (small, large)


def test_float_visibility_trusts_cached_sorted_schedule(burn):
    class CountingEvents(burn._FloatEventList):
        def __init__(self, values):
            super().__init__(values)
            self.reads = 0

        def __getitem__(self, item):
            self.reads += 1
            return super().__getitem__(item)

    events = CountingEvents(
        [(float(i), 1_000_000_000.0, 0, i, 1) for i in range(10_000)]
    )
    events.starts = [row[0] for row in events]
    events.sorted_by_start = True

    visible = burn.active_float_stack(events, 9_999.0, 12)

    assert len(visible) == 12
    assert events.reads <= 20


def test_auto_lazy_policy_and_lru_cache_cap(burn):
    threshold = burn.AUTO_LAZY_MESSAGE_THRESHOLD
    assert burn.resolve_message_image_cache_policy(threshold - 1, False, 16) == (
        False,
        16,
        False,
    )
    assert burn.resolve_message_image_cache_policy(threshold, False, 16) == (
        True,
        16,
        True,
    )

    images = OrderedDict()
    lines = {}
    for idx in range(40):
        burn._store_message_image(
            images,
            lines,
            idx,
            object(),
            1,
            lazy=True,
            cache_cap=8,
        )

    assert list(images) == list(range(32, 40))
    assert list(lines) == list(range(32, 40))


def test_emote_oversize_is_rejected_before_base64_decode(tmp_path: Path, monkeypatch):
    parser = load_module("chat_parser_longterm_oversize", "chat_parser.py")
    monkeypatch.setattr(parser, "_MAX_EMOTE_BASE64_CHARS", 8)
    decode_calls = 0

    def forbidden_decode(_payload):
        nonlocal decode_calls
        decode_calls += 1
        raise AssertionError("oversized payload must not be decoded")

    monkeypatch.setattr(parser.base64, "b64decode", forbidden_decode)
    html_path = tmp_path / "oversize.html"
    html_path.write_text(
        '<style>.first-huge{content:url("data:image/png;base64,AAAAAAAAAAAA")}</style>',
        encoding="utf-8",
    )

    data = parser.parse_chat_html(str(html_path), str(tmp_path / "oversize-out"))

    assert decode_calls == 0
    assert data["emote_map"] == {}


def test_emote_cumulative_byte_budget_stops_further_writes(tmp_path: Path, monkeypatch):
    parser = load_module("chat_parser_longterm_budget", "chat_parser.py")
    monkeypatch.setattr(parser, "_MAX_EMOTE_BYTES", 100)
    monkeypatch.setattr(parser, "_MAX_EMOTE_BASE64_CHARS", 200)
    monkeypatch.setattr(parser, "_MAX_TOTAL_EMOTE_BYTES", 4)
    html_path = tmp_path / "budget.html"
    html_path.write_text(
        '<style>.first-a,.first-b{content:url("data:image/png;base64,iVBORw==")}</style>',
        encoding="utf-8",
    )

    data = parser.parse_chat_html(str(html_path), str(tmp_path / "budget-out"))

    assert set(data["emote_map"]) == {"first-a"}
    written = [Path(path).stat().st_size for path in data["emote_map"].values()]
    assert sum(written) <= parser._MAX_TOTAL_EMOTE_BYTES


def _configure_fake_main(
    burn,
    monkeypatch,
    tmp_path: Path,
    *,
    preview: bool,
    empty_chat: bool = False,
):
    video = tmp_path / "video.mp4"
    chat = tmp_path / "chat.html"
    out_base = tmp_path / "out"
    job_dir = out_base / "job_publish_test"
    video.write_bytes(b"video")
    chat.write_text("<html></html>", encoding="utf-8")

    argv = [
        "twitch_chat_burn.py",
        str(video),
        str(chat),
        "--out-dir",
        str(out_base),
        "--job-dir",
        str(job_dir),
    ]
    if preview:
        argv.extend(["--preview-frame", "1"])
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(burn, "install_process_cleanup_handlers", lambda: None)
    monkeypatch.setattr(burn, "require_executable", lambda _name: "ffmpeg")
    monkeypatch.setattr(
        burn.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(burn, "resolve_font_paths", lambda *_args: ("font.ttf", "font.ttf"))
    monkeypatch.setattr(burn, "apply_relative_layout", lambda *_args: None)
    monkeypatch.setattr(burn, "adapt_absolute_layout_to_source", lambda *_args: None)
    monkeypatch.setattr(burn, "layout_bounds_warnings", lambda *_args: [])
    monkeypatch.setattr(burn, "resolve_output_fps", lambda *_args, **_kwargs: 29.97)
    monkeypatch.setattr(burn, "probe_video_duration", lambda *_args: 5.0)

    messages = [] if empty_chat else [
        {
            "timestamp": 1.0,
            "author": "user",
            "fragments": [{"type": "text", "text": "hello"}],
            "badges": [],
        }
    ]
    monkeypatch.setattr(
        burn,
        "parse_chat_html",
        lambda *_args: {"messages": messages, "emote_map": {}},
    )

    def fake_render(_chat_data, out_dir, _video_path, config):
        frames = Path(out_dir) / "overlay_frames"
        frames.mkdir(parents=True, exist_ok=True)
        if config.preview_frame is not None:
            preview_path = Path(out_dir) / "video_preview_1s.png"
            preview_path.write_bytes(b"png")
            config.preview_image = str(preview_path)
        return str(frames), 2.0

    def fake_compose(_video_path, _frames_dir, out_dir, _config, _duration):
        result = Path(out_dir) / "video_chat.mp4"
        result.write_bytes(b"finished-video")
        return str(result)

    monkeypatch.setattr(burn, "render_overlay", fake_render)
    monkeypatch.setattr(burn, "compose_video", fake_compose)
    return out_base, job_dir


def test_empty_chat_fails_before_render(burn, tmp_path: Path, monkeypatch):
    _out_base, job_dir = _configure_fake_main(
        burn,
        monkeypatch,
        tmp_path,
        preview=False,
        empty_chat=True,
    )
    called = False

    def forbidden_render(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("empty chat must stop before render")

    monkeypatch.setattr(burn, "render_overlay", forbidden_render)

    assert burn.main() == 1
    assert called is False
    assert job_dir.is_dir()


@pytest.mark.parametrize("preview", [False, True])
def test_publish_copy_failure_is_nonzero_and_retains_job_artifact(
    burn,
    tmp_path: Path,
    monkeypatch,
    preview: bool,
):
    _out_base, job_dir = _configure_fake_main(
        burn,
        monkeypatch,
        tmp_path,
        preview=preview,
    )
    real_copy2 = burn.shutil.copy2

    def fail_publish_copy(src, dst, *args, **kwargs):
        if str(dst).endswith(".partial"):
            raise OSError("simulated publish failure")
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(burn.shutil, "copy2", fail_publish_copy)

    assert burn.main() == 1
    artifact = (
        job_dir / "video_preview_1s.png"
        if preview
        else job_dir / "video_chat.mp4"
    )
    assert artifact.read_bytes() in (b"png", b"finished-video")
    assert job_dir.is_dir()

    meta = json.loads((job_dir / "run_meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "failed"
    assert meta["stage"] == ("publish_preview" if preview else "publish")


def test_short_media_probes_handle_timeout(burn, monkeypatch):
    seen_timeouts = []

    def timed_out(_cmd, **kwargs):
        seen_timeouts.append(kwargs.get("timeout"))
        raise burn.subprocess.TimeoutExpired("ffprobe", kwargs.get("timeout"))

    monkeypatch.setattr(burn.subprocess, "run", timed_out)

    with pytest.raises(RuntimeError, match="超时"):
        burn.probe_video_duration("video.mp4")
    assert burn.probe_video_dimensions("video.mp4") is None
    assert burn.probe_video_fps("video.mp4") is None
    assert burn.get_stream_start_time("video.mp4", "v:0") == 0.0
    summary = burn.probe_media_summary("video.mp4")
    assert summary["ok"] is False
    assert "timed out" in summary["error"]
    assert seen_timeouts == [burn._PROBE_TIMEOUT_SECONDS] * 5

def test_float_visibility_ignores_unmarked_stale_starts_cache(burn):
    class MisleadingEvents(list):
        pass

    events = MisleadingEvents(
        [
            (0.0, 100.0, 0, 0, 1),
            (1.0, 100.0, 0, 1, 1),
        ]
    )
    events.starts = [999.0, 999.0]

    assert burn.active_float_stack(events, 1.0, 2) == [
        (0, 1, 1.0, 100.0, 1),
        (1, 0, 0.0, 100.0, 1),
    ]


def test_emote_decode_plan_enforces_frame_pixel_and_memory_budgets(burn):
    width, decoded = burn.emote_decode_plan(
        112,
        112,
        10,
        22,
        burn._MAX_EMOTE_DECODED_BYTES_TOTAL,
    )
    assert width == 22
    assert decoded == 22 * 22 * 4 * 10

    with pytest.raises(ValueError, match="too many frames"):
        burn.emote_decode_plan(
            112,
            112,
            burn._MAX_EMOTE_ANIMATION_FRAMES + 1,
            22,
            burn._MAX_EMOTE_DECODED_BYTES_TOTAL,
        )
    with pytest.raises(ValueError, match="too many pixels"):
        burn.emote_decode_plan(
            burn._MAX_EMOTE_SOURCE_PIXELS + 1,
            1,
            1,
            22,
            burn._MAX_EMOTE_DECODED_BYTES_TOTAL,
        )
    with pytest.raises(ValueError, match="remaining global budget"):
        burn.emote_decode_plan(112, 112, 10, 22, decoded - 1)

def test_render_disk_guard_stops_copy_fallback_before_reserve_is_consumed(
    tmp_path: Path,
    monkeypatch,
):
    import render_perf

    frames = tmp_path / "frames"
    frames.mkdir()
    render_perf.frame_path(frames, 0).write_bytes(b"png")

    monkeypatch.setattr(
        render_perf.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(
            free=render_perf.MIN_RENDER_DISK_RESERVE_BYTES - 1
        ),
    )
    with pytest.raises(RuntimeError, match="low on free space"):
        render_perf.ensure_render_disk_headroom(frames)

    monkeypatch.setattr(
        render_perf.os,
        "link",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no hardlinks")),
    )
    with pytest.raises(RuntimeError, match="low on free space"):
        render_perf.expand_frame_sequence_for_ffmpeg(frames, 2, [0])
    assert not render_perf.frame_path(frames, 1).exists()

def test_burn_media_probes_reject_non_finite_values(burn, monkeypatch):
    monkeypatch.setattr(
        burn.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="nan\n",
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="时长无效"):
        burn.probe_video_duration("video.mp4")
    assert burn.get_stream_start_time("video.mp4", "v:0") == 0.0
