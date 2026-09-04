#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression: lead-in must not false-fail ~source-length encodes."""

from __future__ import annotations

import math
from pathlib import Path
import subprocess
from unittest import mock

import pytest

from helpers import ffprobe_json, load_module


def _make_start_offset_video(out_path: Path, content_s: float = 3.0, lead_in: float = 1.0, fps: int = 30) -> Path:
    """
    Build MP4 similar to real VODs: audio from 0, video stream start_time≈lead_in.

    Uses -itsoffset on the video input so ffprobe reports video start_time > 0
    while format duration stays ~ content_s + lead_in or ~content depending on mux.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Generate plain A/V then remux with video delay via itsoffset.
    # Simpler reliable approach used by many tools:
    #   ffmpeg -i video -i audio -itsoffset LEAD -i video -map delayed_v -map a
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={content_s + lead_in}",
        "-f", "lavfi", "-itsoffset", str(lead_in),
        "-i", f"color=c=black:s=320x180:r={fps}:d={content_s}",
        "-map", "1:v:0", "-map", "0:a:0",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-t", str(content_s + lead_in),
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert out_path.is_file()
    return out_path


def test_expected_compose_duration_ignores_lead_in_padding():
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    # video_lead_in parameter was removed (never read); duration passes through.
    assert burn.expected_compose_duration(374.03) == pytest.approx(374.03)
    assert burn.expected_compose_duration(15.0) == pytest.approx(15.0)
    assert burn.expected_compose_duration(10.0) == pytest.approx(10.0)


@pytest.mark.smoke
def test_validate_accepts_source_length_when_expected_is_source_not_source_plus_leadin(
    make_test_video,
):
    """The Fontinalia false-fail: actual≈374, bad expected was 375.03."""
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    video = make_test_video(duration=3.0, fps=30)
    # Correct policy: expected = source/render length
    ok, summary, reason = burn.validate_rendered_output(
        str(video),
        expected_duration=3.0,
        require_audio=True,
        duration_tolerance=0.35,
        min_duration=3.0 - 1.0,  # allow losing at most lead-in
    )
    assert ok, reason
    # Old wrong policy would demand 4.0 and fail a complete 3.0s file
    ok_bad, _, reason_bad = burn.validate_rendered_output(
        str(video),
        expected_duration=4.0,
        require_audio=True,
        duration_tolerance=0.35,
    )
    assert not ok_bad
    assert "shorter" in reason_bad


@pytest.mark.smoke
def test_validate_min_duration_still_rejects_truncated_tail(make_test_video):
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    video = make_test_video(duration=2.0, fps=30)
    ok, _, reason = burn.validate_rendered_output(
        str(video),
        expected_duration=2.0,
        require_audio=True,
        duration_tolerance=0.35,
        min_duration=5.0,  # floor higher than actual
    )
    assert not ok
    assert "shorter" in reason


@pytest.mark.smoke
def test_resolve_source_av_timing_fields(make_test_video):
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    video = make_test_video(duration=2.0, fps=30)
    timing = burn.resolve_source_av_timing(str(video))
    assert timing["has_audio"] is True
    assert timing["source_duration"] > 0
    assert timing["video_lead_in"] >= 0.0
    assert "summary" in timing


def test_compose_math_matches_fontinalia_case():
    """Document the exact numbers from the failed full render."""
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    source_duration = 374.033667
    video_lead_in = 1.0
    # render_duration from probe_video_duration ≈ format duration
    expected = burn.expected_compose_duration(source_duration)
    assert expected == pytest.approx(374.033667)
    # actual partial was ~374.067 — must pass with default 0.35 tolerance
    # simulate by checking the comparison logic via a real short file is enough;
    # here just assert the inequality that previously failed is now OK:
    actual = 374.066667
    bad_expected = source_duration + video_lead_in  # old formula
    assert actual + 0.35 < bad_expected  # old code would FAIL
    assert actual + 0.35 >= expected  # new code PASS


# ---------------------------------------------------------------------------
# Audio-late source (audio_start > video_start) must keep A/V aligned.
# ---------------------------------------------------------------------------


def _make_audio_late_video(out_path: Path, duration: float = 3.0, audio_late: float = 0.6, fps: int = 30) -> Path:
    """Build an MP4 whose AUDIO stream starts ~audio_late s after the video.

    Uses -itsoffset on the audio input; ffprobe then reports
    audio start_time ≈ audio_late while video starts at 0 — the mirror image
    of make_leadin_video (video late) in tests/helpers.py.

    The audio timeline carries BEEPS at 0.5-1.0s and 1.5-2.0s with digital
    silence elsewhere, so silencedetect on the composed output can locate the
    beep positions and prove content-level A/V alignment (not just container
    start times). The pattern is baked into the generated samples (aevalsrc's
    own t), so -itsoffset shifts the whole stream without moving the pattern.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # aevalsrc: 0.5 amplitude (~-6 dBFS) sine inside the two beep windows,
    # exact 0 outside; \, escapes keep the graph parser away from the commas.
    beep_expr = r"0.5*sin(440*2*PI*t)*(between(t\,0.5\,1.0)+between(t\,1.5\,2.0))"
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s=320x180:r={fps}:d={duration}",
        "-itsoffset", str(audio_late),
        "-f", "lavfi", "-i", f"aevalsrc={beep_expr}:d={duration}",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert out_path.is_file()
    return out_path


def _make_video_late_offset_video(out_path: Path, content_s: float = 3.0, lead_in: float = 1.0, fps: int = 30) -> Path:
    """MP4 with NON-ZERO video start_time (≈lead_in) so compose's lead-in branch engages.

    make_leadin_video freezes via tpad and ends up with both stream starts at 0
    after remux; a real VOD reports video start_time > 0. This reproduces that
    shape: the video PTS is shifted forward with setpts (muxer records an elst
    edit list so ffprobe reports video start_time≈lead_in) while audio starts
    at 0.

    Note: ``-itsoffset`` was previously used here, but some ffmpeg builds
    (Ubuntu apt builds seen on CI) drop the input offset for the video stream
    at mux time, yielding start_time=0 and silently disabling the lead-in
    branch this fixture must exercise. The filter-level setpts shift survives
    across builds; a post-generation probe asserts the shift took effect so a
    build regression fails loudly here instead of as a confusing compose
    assertion later.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=880:duration={content_s + lead_in}",
        "-f", "lavfi", "-i", f"color=c=black:s=320x180:r={fps}:d={content_s}",
        "-filter_complex", f"[1:v]setpts=PTS+{lead_in}/TB[v]",
        "-map", "[v]", "-map", "0:a:0",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-t", str(content_s + lead_in),
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert out_path.is_file()
    info = ffprobe_json(out_path)
    starts = [
        float(s.get("start_time") or 0.0)
        for s in info.get("streams") or []
        if s.get("codec_type") == "video"
    ]
    assert starts and abs(starts[0] - lead_in) <= 0.05, (
        f"fixture build failed: video start_time did not pick up the setpts "
        f"shift (got {starts}; ffmpeg build drops it?)"
    )
    return out_path


def _stream_start(info: dict, codec_type: str) -> float:
    starts = [
        float(s.get("start_time") or 0.0)
        for s in info.get("streams") or []
        if s.get("codec_type") == codec_type
    ]
    return starts[0] if starts else 0.0


def _write_blank_frames(frames_dir: Path, count: int) -> None:
    from PIL import Image

    frames_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        Image.new("RGBA", (8, 8), (0, 0, 0, 0)).save(frames_dir / f"frame_{i:05d}.png")


def _spy_on_run_tracked(burn, recorded: list) -> mock.Mock:
    """Record every run_tracked argv while still running the real command.

    run_tracked 的调用点在 overlay_compose.compose_video（门面
    twitch_chat_burn.run_tracked 只是 re-export 别名），spy 必须落在属主
    模块上才能看到 compose 的 ffmpeg 调用。``burn`` 参数保留以兼容调用点。
    """
    import overlay_compose

    original = overlay_compose.run_tracked

    def spy(cmd, *args, **kwargs):
        recorded.append([str(c) for c in cmd])
        return original(cmd, *args, **kwargs)

    return mock.patch.object(overlay_compose, "run_tracked", side_effect=spy)


@pytest.mark.smoke
def test_resolve_source_av_timing_reports_audio_late(make_test_video):
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    video = make_test_video(duration=2.0, fps=30)
    timing = burn.resolve_source_av_timing(str(video))
    assert timing["audio_late"] == pytest.approx(0.0, abs=0.01)
    assert timing["video_lead_in"] == pytest.approx(0.0, abs=0.01)


@pytest.mark.smoke
def test_compose_audio_late_source_stays_av_aligned(tmp_path: Path):
    """Audio branch erases a late audio start (asetpts); adelay must restore it.

    Regression for: audio_start > video_start made video_lead_in 0 while the
    audio filter unconditionally rewrote the stream to 0, playing audio early
    by the erased offset. End-to-end: real ffmpeg compose of a source whose
    audio starts 0.6s late; the first beep must land at 1.1s on the composed
    timeline (0.5s beep + 0.6s restored offset), not at 0.5s (broken).
    """
    from encode_options import EncodeOptions
    from overlay_config import OverlayConfig

    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    src = _make_audio_late_video(tmp_path / "audiolate_src.mp4", duration=3.0, audio_late=0.6)
    timing = burn.resolve_source_av_timing(str(src))
    assert timing["audio_late"] == pytest.approx(0.6, abs=0.05), f"fixture lacks audio-late offset: {timing}"

    # Real pipeline: main() passes scene.duration == probe_video_duration(source).
    duration = burn.probe_video_duration(str(src))
    frames_dir = tmp_path / "frames"
    _write_blank_frames(frames_dir, math.ceil(duration * 10))

    config = OverlayConfig(x=0, y=0, width=100, height=100, font_size=14, fps=10)
    config.preview_clip_start = 0.0
    config.encode = EncodeOptions(
        encoder="x264", video_codec="libx264", video_preset="ultrafast",
        crf=28, audio_codec="aac", audio_bitrate="192k",
        overlay_codec="vp9", prefer_hw=False, resolved_encoder="x264", notes=[],
    )

    recorded_cmds: list = []
    with _spy_on_run_tracked(burn, recorded_cmds):
        compose_result = burn.compose_video(str(src), str(frames_dir), str(tmp_path), config, duration=duration)
    # compose_video returns ComposeResult (output_path/...), None on failure.
    out = compose_result.output_path if compose_result else None

    assert out is not None and Path(out).is_file(), "real compose failed (see compose stdout)"
    # compose_video returning non-None means the REAL validate_rendered_output
    # and media_health gates passed for the adelay-corrected output.
    assert recorded_cmds, "compose never invoked ffmpeg"
    mux_cmds = [c for c in recorded_cmds if "-af" in c]
    assert mux_cmds, "final mux command without -af"
    af_value = mux_cmds[-1][mux_cmds[-1].index("-af") + 1]
    # The muxer rounds the itsoffset (0.6 requested → ~0.577 probed), so derive
    # the expected delay from the probed audio_late, not from the requested 0.6.
    expected_delay_ms = int(round(timing["audio_late"] * 1000))
    assert af_value == f"asetpts=PTS-STARTPTS,adelay={expected_delay_ms}:all=1", (
        f"adelay must be appended AFTER asetpts: {af_value}"
    )

    out_info = ffprobe_json(Path(out))
    v_start = _stream_start(out_info, "video")
    a_start = _stream_start(out_info, "audio")
    assert abs(v_start - a_start) <= 0.05, f"A/V start mismatch: video={v_start} audio={a_start}"

    # Content-level alignment: beep timeline of the composed audio.
    # Source beeps at 0.5-1.0s and 1.5-2.0s of the (late) audio timeline.
    # Fixed:    +0.6s restored → silence gaps end at ~1.1s and ~2.1s.
    # Broken:   offset erased    → silence gaps end at ~0.5s and ~1.5s.
    detect = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", str(out), "-vn",
         "-af", "silencedetect=n=-35dB:d=0.3", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    silence_ends = [
        float(line.split("silence_end:")[1].split("|")[0])
        for line in (detect.stderr or "").splitlines()
        if "silence_end:" in line
    ]
    assert len(silence_ends) >= 2, f"expected >=2 beep gaps, got {silence_ends}"
    assert silence_ends[0] == pytest.approx(1.1, abs=0.1), (
        f"first beep at {silence_ends[0]:.3f}s — audio-late offset was not restored (broken ≈0.5)"
    )
    assert silence_ends[1] == pytest.approx(2.1, abs=0.1), (
        f"second beep at {silence_ends[1]:.3f}s — drift in restored offset"
    )


@pytest.mark.smoke
def test_compose_lead_in_tail_cap_and_validate_floors(tmp_path: Path):
    """Lead-in -t upper bound = output_duration + video_lead_in, floors intact.

    Locks the new -t formula at argv level (streams end naturally at ~3.0s, so
    the published length cannot distinguish the cap), then proves end-to-end
    that the raised cap still satisfies both validate floors: the expected
    (render window) floor and the min_duration truncated-tail floor.
    """
    from encode_options import EncodeOptions
    from overlay_config import OverlayConfig

    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    src = _make_video_late_offset_video(tmp_path / "leadin_src.mp4", content_s=3.0, lead_in=1.0)
    timing = burn.resolve_source_av_timing(str(src))
    assert timing["video_lead_in"] == pytest.approx(1.0, abs=0.05), f"lead-in branch must engage: {timing}"
    assert timing["audio_late"] == pytest.approx(0.0, abs=0.02), (
        f"fixture must not be audio-late (adelay would perturb the tail): {timing}"
    )

    # Real pipeline: main() passes scene.duration == probe_video_duration(source)
    # (format duration ≈ content_s + lead_in for this fixture shape).
    duration = burn.probe_video_duration(str(src))
    frames_dir = tmp_path / "frames"
    _write_blank_frames(frames_dir, math.ceil(duration * 10))

    config = OverlayConfig(x=0, y=0, width=100, height=100, font_size=14, fps=10)
    config.preview_clip_start = 0.0
    config.encode = EncodeOptions(
        encoder="x264", video_codec="libx264", video_preset="ultrafast",
        crf=28, audio_codec="aac", audio_bitrate="192k",
        overlay_codec="vp9", prefer_hw=False, resolved_encoder="x264", notes=[],
    )

    recorded_cmds: list = []
    with _spy_on_run_tracked(burn, recorded_cmds):
        compose_result = burn.compose_video(str(src), str(frames_dir), str(tmp_path), config, duration=duration)
    # compose_video returns ComposeResult (output_path/...), None on failure.
    out = compose_result.output_path if compose_result else None

    assert out is not None and Path(out).is_file(), "real compose failed (see compose stdout)"
    mux_cmds = [c for c in recorded_cmds if "-movflags" in c]
    assert mux_cmds, "final mux command not captured"
    t_idx = mux_cmds[-1].index("-t")
    t_cap = float(mux_cmds[-1][t_idx + 1])
    # New contract: cap = output_duration + video_lead_in (old code passed 3.0...).
    assert t_cap == pytest.approx(duration + timing["video_lead_in"], abs=1e-6), (
        f"-t cap must be output_duration+lead_in, got {t_cap}"
    )

    out_info = ffprobe_json(Path(out))
    actual = float(out_info.get("format", {}).get("duration") or 0.0)
    # The raised cap is an upper bound only — the published file must NOT grow
    # by the lead-in headroom (old false-fail story in reverse).
    assert duration - 0.35 <= actual <= duration + 0.35, (
        f"output length {actual:.3f}s vs expected window {duration:.3f}s"
    )
    assert actual < duration + timing["video_lead_in"] - 0.1, (
        f"output {actual:.3f}s absorbed the lead-in headroom (cap {t_cap:.3f})"
    )

    # Double-floor contract (floor 1 = expected window, floor 2 = min_duration
    # that rejects truncated tails) on the published file.
    ok, _, reason = burn.validate_rendered_output(
        str(Path(out)),
        expected_duration=duration,
        require_audio=True,
        duration_tolerance=0.35,
    )
    assert ok, f"expected floor broke under raised -t: {reason} (actual={actual:.3f})"
    ok2, _, reason2 = burn.validate_rendered_output(
        str(Path(out)),
        expected_duration=duration,
        require_audio=True,
        duration_tolerance=0.35,
        min_duration=duration - timing["video_lead_in"] - 0.05,
    )
    assert ok2, f"min_duration floor broke under raised -t: {reason2} (actual={actual:.3f})"


def test_schedule_admits_messages_in_final_lead_in_window():
    """Messages inside the last lead_in of the render window are scheduled.

    The renderer paints them; with the -t cap raised by video_lead_in the muxer
    no longer caps that delayed chat region away at output_duration. (Whether
    the overlay filter emits frames past the main input's EOF is an ffmpeg
    filtergraph behavior outside this fix's scope; this locks the scheduling
    precondition of the tail-visibility contract.)
    """
    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    msgs = [{"timestamp": 2.6, "message": "tail"}]
    schedule = burn.schedule_messages(msgs, {0: 1}, duration=3.0, max_visible=10, msg_lifetime=14.0)
    assert schedule, "message in the final lead_in window was dropped by the scheduler"
    assert schedule[0][0] == pytest.approx(2.6)


def test_build_audio_encode_args_for_compose_adelay_postprocessing():
    """Burn-side wrapper injects adelay AFTER asetpts; copy falls back to aac."""
    from encode_options import EncodeOptions

    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    opts = EncodeOptions(encoder="x264", resolved_encoder="x264", audio_codec="aac")
    notes: list[str] = []

    # aac + delay → adelay appended after the existing asetpts value.
    assert burn.build_audio_encode_args_for_compose(
        opts, True, video_lead_in=0.0, audio_delay_ms=600, notes=notes,
    ) == ["-c:a", "aac", "-b:a", "192k", "-af", "asetpts=PTS-STARTPTS,adelay=600:all=1"]
    # No delay → unchanged passthrough.
    assert burn.build_audio_encode_args_for_compose(
        opts, True, video_lead_in=0.0, audio_delay_ms=0,
    ) == ["-c:a", "aac", "-b:a", "192k", "-af", "asetpts=PTS-STARTPTS"]
    # copy has no filter slot for adelay → aac fallback + note (mirrors
    # encode_options' copy→aac fallback for lead-in).
    copy_opts = EncodeOptions(encoder="x264", resolved_encoder="x264", audio_codec="copy")
    out_args = burn.build_audio_encode_args_for_compose(
        copy_opts, True, video_lead_in=0.0, audio_delay_ms=400, notes=notes,
    )
    assert out_args[:2] == ["-c:a", "aac"]
    assert out_args[-1] == "asetpts=PTS-STARTPTS,adelay=400:all=1"
    assert any("adelay=400" in n for n in notes)
    # No audio → no args regardless of delay.
    assert burn.build_audio_encode_args_for_compose(opts, False, audio_delay_ms=600) == []


def test_compose_timing_reads_defensive_gets():
    """Old stubs (test_burn_compose_and_encode, test_render_perf_burn_publish) return only 5 keys.

    compose_video must read the new timing fields via .get with defaults, never
    via [] — a missing audio_late/audio_start/video_start key must not KeyError.
    """
    import inspect

    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    five_keys = {
        "source_duration": 1.0,
        "video_start": 0.0,
        "audio_start": 0.0,
        "video_lead_in": 0.0,
        "has_audio": True,
    }
    # Mirror of compose_video's defensive reads on a bare five-key dict:
    assert float(five_keys.get("audio_late") or 0.0) == 0.0
    fallback = max(
        0.0,
        float(five_keys.get("audio_start") or 0.0) - float(five_keys.get("video_start") or 0.0),
    )
    assert fallback == 0.0
    source = inspect.getsource(burn.compose_video)
    assert 'timing.get("audio_late")' in source
    assert 'timing.get("has_audio")' in source
    assert 'timing["audio_late"]' not in source


# ---------------------------------------------------------------------------
# Preview single-frame extraction must use input seek (-ss before -i).
# ---------------------------------------------------------------------------


def _make_color_steps_video(out_path: Path, fps: int = 10) -> Path:
    """4s video whose background color codes the second: 0=red, 1=lime, 2=blue, 3=yellow."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=red:s=320x180:r=10:d=1",
        "-f", "lavfi", "-i", "color=c=lime:s=320x180:r=10:d=1",
        "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=10:d=1",
        "-f", "lavfi", "-i", "color=c=yellow:s=320x180:r=10:d=1",
        "-filter_complex", "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0[out]",
        "-map", "[out]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert out_path.is_file()
    return out_path


def _spy_on_subprocess_run(burn, recorded: list) -> mock.Mock:
    """Record every subprocess.run argv while still running the real command."""
    original = burn.subprocess.run

    def spy(cmd, *args, **kwargs):
        recorded.append([str(c) for c in cmd])
        return original(cmd, *args, **kwargs)

    return mock.patch.object(burn.subprocess, "run", side_effect=spy)


@pytest.mark.smoke
def test_render_overlay_preview_frame_extracts_with_input_seek(tmp_path: Path):
    """render_overlay's single-frame preview extract must bind -ss to the input.

    Regression: -ss used to sit AFTER -i (output seek), decoding the whole
    prefix — O(t) work that reliably blew the 120s timeout on mid-VOD previews.
    Locks the argv ordering (-ss before -i) while proving the real extraction
    still yields the frame content at the requested t (blue = second 2 of 4).
    """
    from common_utils import resolve_font_paths
    from overlay_config import OverlayConfig

    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    video = _make_color_steps_video(tmp_path / "color_steps.mp4")
    chat = {"messages": [], "emote_map": {}}
    config = OverlayConfig(x=0, y=0, width=50, height=50, font_size=14, fps=10, preview_frame=2.0)
    config.font_path, config.font_bold_path = resolve_font_paths("auto", "auto")

    recorded_cmds: list = []
    with _spy_on_subprocess_run(burn, recorded_cmds):
        burn.render_overlay(chat, str(tmp_path), str(video), config)

    extract_cmds = [c for c in recorded_cmds if "-frames:v" in c]
    assert extract_cmds, "preview single-frame extraction never ran"
    cmd = extract_cmds[-1]
    assert cmd.index("-ss") < cmd.index("-i"), f"-ss must bind to the input (before -i): {cmd}"
    assert cmd[cmd.index("-ss") + 1] == "2.0", f"extraction must target preview_t=2.0: {cmd}"

    previews = list(tmp_path.glob("*_preview_*.png"))
    assert previews, f"preview PNG not published: {sorted(p.name for p in tmp_path.iterdir())}"
    from PIL import Image

    px = Image.open(previews[0]).convert("RGB")
    # Sample far from the 50x50 overlay box at (0,0): background must be the
    # second-2 color (blue) — the frame content at the requested t.
    rgb = px.getpixel((280, 140))
    assert rgb[2] > 175 and rgb[0] < 80 and rgb[1] < 80, f"frame at t=2.0 is not blue: {rgb}"
