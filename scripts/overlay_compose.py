#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compose the rendered overlay frame sequence onto the source video.

Extracted verbatim from twitch_chat_burn for maintainability: A/V stream-timing
probes, output validation floors, the burn-side audio adelay wrapper, and the
two-stage (WebM alpha → mux) compose pipeline including the lead-in freeze,
audio-late compensation, preview-seek ordering, and -t upper-bound fixes.

compose_video returns a ComposeResult (or None on failure, the historical
contract) and never mutates OverlayConfig at runtime — main() injects the
result values into the config object at the pipeline boundary. Tests patch
this module's globals (``overlay_compose.run_tracked``, ...) so the seams
stay addressable at the owner module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from common_utils import atomic_replace_with_retry, require_executable
from encode_options import (
    build_audio_encode_args,
    build_video_encode_args,
    build_webm_encode_args,
    resolve_encode_options,
    summarize_encode_options,
)
import media_probe
from process_util import run_tracked
from render_perf import missing_frame_indexes


@dataclass
class ComposeResult:
    """Outcome of compose_video.

    Fields mirror the values that used to be written back onto OverlayConfig:
    ``output_fps`` / ``timings`` are injected into the config by main() (the
    only legitimate writeback point) before run_meta / reporting consume them.
    """

    output_path: str | None
    output_fps: float | None
    summary: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)


def detect_frame_start_number(frames_dir):
    """Return the first numeric frame id in frame_%05d.png sequences."""
    numbers = []
    for name in os.listdir(frames_dir):
        m = re.fullmatch(r"frame_(\d+)\.png", name)
        if m:
            numbers.append(int(m.group(1)))
    return min(numbers) if numbers else 0


def get_stream_start_time(video_path, stream_selector):
    """读取流起始时间；缺失/异常时回退 0。"""
    try:
        probe = subprocess.run(
            [
                require_executable("ffprobe"), "-v", "error", "-select_streams", stream_selector,
                "-show_entries", "stream=start_time", "-of", "csv=p=0", video_path,
            ],
            capture_output=True,
            text=True,
            timeout=media_probe._PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(
            f"  [WARN] ffprobe start_time 探测超时,按 0 处理: {video_path}",
            flush=True,
        )
        return 0.0
    if probe.returncode != 0:
        print(
            f"  [WARN] ffprobe start_time 探测失败({probe.returncode}),按 0 处理: {video_path}",
            flush=True,
        )
        return 0.0
    try:
        raw = (probe.stdout or "").strip().splitlines()
        if not raw:
            return 0.0
        value = float(raw[0] or 0)
        return value if math.isfinite(value) else 0.0
    except ValueError:
        return 0.0


def resolve_source_av_timing(video_path, source_has_audio=None):
    """Probe container/audio/video timing used by compose + validation.

    Returns dict:
      source_duration, video_start, audio_start, video_lead_in, has_audio
      + audio_late: >=0 seconds by which the audio stream starts AFTER the
        video (mirror of video_lead_in, which measures video after audio).
        Compose uses it to re-insert the offset the audio branch's asetpts
        erases (see compose_video).
    """
    source_summary = media_probe.probe_media_summary(video_path)
    has_audio = (
        bool(source_has_audio)
        if source_has_audio is not None
        else bool(source_summary.get("has_audio"))
    )
    video_start = get_stream_start_time(video_path, "v:0")
    audio_start = get_stream_start_time(video_path, "a:0") if has_audio else 0.0
    video_lead_in = max(0.0, float(video_start) - float(audio_start)) if has_audio else 0.0
    audio_late = max(0.0, float(audio_start) - float(video_start)) if has_audio else 0.0
    source_duration = float(source_summary.get("duration") or 0.0)
    return {
        "source_duration": source_duration,
        "video_start": float(video_start or 0.0),
        "audio_start": float(audio_start or 0.0),
        "video_lead_in": float(video_lead_in or 0.0),
        "audio_late": float(audio_late or 0.0),
        "has_audio": has_audio,
        "summary": source_summary,
    }


def expected_compose_duration(render_duration):
    """Target container duration for compose_video.

    Lead-in handling only rewrites timestamps so both streams start at 0 and the
    first video frame freezes for editors. It does **not** mean we must publish
    a file longer than the source container / render window. Using
    render_duration + lead_in here previously false-failed complete encodes
    (~source length) as "too short", so the never-read ``video_lead_in``
    parameter was removed.
    """
    return max(0.0, float(render_duration or 0.0))


def _default_max_extra_seconds(expected_duration):
    """Tight upper allowance: max(0.5, 0.5% of expected), capped near 0.75s."""
    expected = max(0.0, float(expected_duration or 0.0))
    return min(0.75, max(0.5, expected * 0.005))


def validate_rendered_output(
    path,
    expected_duration,
    require_audio=False,
    duration_tolerance=0.35,
    max_extra_seconds=None,
    min_width=2,
    min_height=2,
    min_duration=None,
):
    """Validate a partial/final MP4 before publishing the user-facing name.

    Checks:
    - readable media with video stream and positive duration
    - optional audio presence
    - not too short vs expected (and optional absolute min_duration floor)
    - not suspiciously long vs expected (catches wrong -t / filter mistakes)
    - video dimensions present (catches empty/corrupt encodes that still open)

    When source video is delayed vs audio, compose freezes the first frame and
    rewrites start times to 0. The published duration should still be about the
    render/source length — pass that as expected_duration, not source+lead_in.
    Optional min_duration rejects outputs that lost more than a lead-in worth of
    content (e.g. truncated tails).

    Floor semantics: if both expected and min_duration are set, the short-check
    uses min_duration as an independent lower bound (not max(min, expected),
    which previously made min_duration dead when expected was also set).
    When only expected is set, expected is the short floor.
    """
    summary = media_probe.probe_media_summary(path)
    if not summary["ok"]:
        return False, summary, summary.get("error") or "output validation failed"
    if require_audio and not summary["has_audio"]:
        return False, summary, "expected audio stream is missing"

    actual = float(summary.get("duration") or 0.0)
    expected = float(expected_duration or 0.0)
    tol = float(duration_tolerance)
    if max_extra_seconds is None:
        max_extra_seconds = _default_max_extra_seconds(expected)
    else:
        max_extra_seconds = float(max_extra_seconds)

    # Short-check floors (independent):
    # - expected: primary target length
    # - min_duration: optional absolute floor that can be *below* expected
    #   (e.g. allow losing at most lead-in) without being raised to expected.
    if expected > 0 and actual + tol < expected:
        return (
            False,
            summary,
            f"output duration {actual:.3f}s is shorter than expected {expected:.3f}s",
        )
    if min_duration is not None and float(min_duration) > 0:
        floor = float(min_duration)
        if actual + tol < floor:
            return (
                False,
                summary,
                f"output duration {actual:.3f}s is shorter than min_duration {floor:.3f}s",
            )

    if expected > 0 and actual > expected + max(tol, float(max_extra_seconds)):
        return (
            False,
            summary,
            (
                f"output duration {actual:.3f}s is longer than expected "
                f"{expected:.3f}s (+{max_extra_seconds}s allowance)"
            ),
        )
    w = int(summary.get("width") or 0)
    h = int(summary.get("height") or 0)
    if w < int(min_width) or h < int(min_height):
        return False, summary, f"video dimensions too small: {w}x{h}"
    return True, summary, ""


def build_audio_encode_args_for_compose(
    opts,
    source_has_audio,
    *,
    video_lead_in=0.0,
    audio_delay_ms=0,
    notes=None,
):
    """Burn-side wrapper around build_audio_encode_args (encode_options.py).

    encode_options builds audio args without knowing the source's audio-late
    offset (audio_start > video_start): its re-encode branch rewrites the audio
    stream to 0 via asetpts=PTS-STARTPTS, which plays audio that starts late
    EARLY by that offset (A/V desync). This wrapper re-inserts the erased
    offset with adelay AFTER asetpts in the -af value (asetpts stays first).
    ``-c:a copy`` has no filter slot for adelay, so it falls back to AAC —
    mirroring the copy→aac fallback encode_options already applies for lead-in.
    Lives on the compose side only; encode_options is untouched.
    """
    args = list(build_audio_encode_args(
        opts,
        source_has_audio,
        video_lead_in=video_lead_in,
        notes=notes,
    ))
    if audio_delay_ms <= 0 or not args:
        return args
    af_idx = args.index("-af") if "-af" in args else -1
    if af_idx >= 0 and af_idx + 1 < len(args):
        args[af_idx + 1] = f"{args[af_idx + 1]},adelay={audio_delay_ms}:all=1"
        return args
    if notes is not None:
        notes.append(
            f"audio-codec copy 无法应用 adelay={audio_delay_ms}:all=1，已回退 aac 以恢复音画对齐"
        )
    return [
        "-c:a", "aac",
        "-b:a", getattr(opts, "audio_bitrate", "192k"),
        "-af", f"asetpts=PTS-STARTPTS,adelay={audio_delay_ms}:all=1",
    ]


def compose_video(video_path, frames_dir, out_dir, config, duration):
    """PNG 帧序列 → (可选 WebM alpha) → 叠加到源视频。

    Returns ComposeResult on success; None on any failure path (historical
    contract relied on by callers/tests). Results are returned, not written
    back onto ``config`` — main() owns the only config writeback.
    """
    # Fail fast on incomplete frame sequences before encode setup / ffmpeg publish.
    start_number = detect_frame_start_number(frames_dir)
    fps = max(1, int(getattr(config, "fps", 30) or 30))
    expected_frames = max(1, int(math.ceil(float(duration or 0.0) * fps - 1e-9)))
    missing = missing_frame_indexes(frames_dir, expected_frames, start=start_number)
    if missing:
        preview = ", ".join(f"frame_{i:05d}.png" for i in missing[:12])
        more = "" if len(missing) <= 12 else f" ... (+{len(missing) - 12} more)"
        raise RuntimeError(
            f"compose_video: missing {len(missing)} overlay frame(s) for "
            f"start={start_number} count={expected_frames} under {frames_dir}; "
            f"first gaps: {preview}{more}. Refuse to publish incomplete overlay."
        )

    encode = getattr(config, "encode", None)
    if encode is None:
        encode = resolve_encode_options()
        config.encode = encode

    print("[3/4] 合成 overlay 视频...", flush=True)
    print(f"  编码参数: {summarize_encode_options(encode)}", flush=True)
    for note in encode.notes:
        print(f"  [encode] {note}", flush=True)

    stage_timings = dict(getattr(config, "stage_timings", None) or {})
    frames_pattern = os.path.join(frames_dir, "frame_%05d.png")

    # Overlay path for filter input: either intermediate WebM or direct PNG sequence.
    overlay_input = None
    use_png_direct = str(getattr(encode, "overlay_codec", "vp9")).lower() == "png"

    if not use_png_direct:
        print(f"  步骤 1/2: PNG 帧 → WebM (alpha, cpu-used={encode.webm_cpu_used})...", flush=True)
        webm_path = os.path.join(out_dir, "overlay_temp.webm")
        cmd1 = [
            require_executable("ffmpeg"), "-y",
            "-framerate", str(config.fps),
            "-start_number", str(start_number),
            "-i", frames_pattern,
            *build_webm_encode_args(encode),
            "-t", str(duration),
            webm_path,
        ]
        webm_log_path = os.path.join(out_dir, "ffmpeg-webm.log")
        t0 = time.perf_counter()
        with open(webm_log_path, "w", encoding="utf-8", errors="replace") as log_file:
            r = run_tracked(cmd1, stdout=subprocess.DEVNULL, stderr=log_file, text=True)
        stage_timings["webm_encode"] = time.perf_counter() - t0
        if r.returncode != 0:
            try:
                tail = Path(webm_log_path).read_text(encoding="utf-8", errors="replace")[-1200:]
            except OSError:
                tail = "日志不可读取"
            print(f"  WebM 编码错误；完整日志: {webm_log_path}\n{tail}", flush=True)
            return None
        overlay_input = webm_path
        print(f"  WebM 完成: {stage_timings['webm_encode']:.1f}s", flush=True)
        # Validate WebM duration: if the intermediate overlay is shorter than
        # expected, the final compose will silently lack chat in the tail.
        webm_summary = media_probe.probe_media_summary(webm_path)
        if webm_summary["ok"]:
            webm_dur = float(webm_summary.get("duration") or 0.0)
            # Allow small encoder margin (VP9 often ±0.1s). Short WebM used to only
            # warn then compose with eof_action=pass — final MP4 length looked fine
            # while chat was missing in the tail (silent-wrong). Hard-fail instead.
            if webm_dur + 0.5 < float(duration or 0.0):
                print(
                    f"  错误: WebM overlay 时长 {webm_dur:.3f}s 显著短于预期 {duration:.3f}s；"
                    f"拒绝合成以免尾段弹幕静默缺失。可改 --overlay-codec png 或检查编码日志: {webm_log_path}",
                    flush=True,
                )
                return None
        else:
            print(
                f"  错误: WebM 中间文件无法探测 ({webm_summary.get('error', 'unknown')})；"
                f"拒绝合成。日志: {webm_log_path}",
                flush=True,
            )
            return None
    else:
        print("  步骤 1/2: 跳过 WebM，直接用 PNG 序列作为 overlay 输入", flush=True)
        stage_timings["webm_encode"] = 0.0

    print(f"  步骤 2/2: overlay 合成到源视频 ({encode.video_codec})...", flush=True)

    # Overlay. Write to a temporary MP4 first so an interrupted FFmpeg run
    # never leaves a broken file at the user-facing output path.
    out_path = os.path.join(out_dir, Path(video_path).stem + "_chat.mp4")
    partial_path = os.path.join(out_dir, Path(video_path).stem + "_chat.partial.mp4")
    try:
        os.remove(partial_path)
    except FileNotFoundError:
        pass
    # 源文件可能用时间戳表达“音频先开始、视频稍后进入”。VLC 会遵守，
    # 但部分剪辑软件会忽略该非零 start_time。把这段差显式编码为首帧冻结，
    # 让导出的 MP4 两条流都从 0 开始，同时保留原本的内容时序。
    #
    # 例外：预览 seek 到片中（preview_clip_start > 0）时，输入已经过了源
    # 片头 A/V 错位，再 tpad 会把“当前 seek 到的画面”冻 1 秒，表现为卡顿。
    timing = resolve_source_av_timing(video_path)
    # Stubs/mocks may return only the original five keys — always .get so a
    # missing newer field can never KeyError; fall back to the stream starts.
    source_has_audio = bool(timing.get("has_audio"))
    source_lead_in = float(timing.get("video_lead_in") or 0.0)
    audio_late = float(timing.get("audio_late") or 0.0)
    if source_has_audio and audio_late <= 0.0:
        audio_late = max(
            0.0,
            float(timing.get("audio_start") or 0.0) - float(timing.get("video_start") or 0.0),
        )
    seek_ss = float(getattr(config, "preview_clip_start", 0.0) or 0.0)
    # Only apply lead-in freeze when composing from the true start of the source.
    video_lead_in = 0.0 if seek_ss > 1e-6 else source_lead_in
    # Audio-late compensation: the audio branch (encode_options) rewrites the
    # audio stream to start at 0 (asetpts=PTS-STARTPTS). When the source audio
    # starts LATER than the video, that erases the offset and plays the audio
    # (audio_start - video_start) seconds early — A/V desync. Re-insert the
    # offset with adelay AFTER asetpts (mirror of the lead-in freeze, opposite
    # direction). Only from the true start: after a preview seek both streams
    # rebase at the seek point and the head offset no longer exists.
    audio_delay_ms = 0
    if source_has_audio and seek_ss <= 1e-6 and audio_late > 0.001:
        audio_delay_ms = int(round(audio_late * 1000.0))
        print(
            f"  检测到音频相对视频延后 {audio_late:.3f}s；"
            f"音频滤镜 asetpts 后追加 adelay={audio_delay_ms}:all=1 恢复音画对齐",
            flush=True,
        )
    # Lead-in rewrites A/V start times (freeze first frame for editors).
    # Container duration target stays the render window (~source length),
    # not source+lead_in — otherwise validation false-fails complete encodes.
    output_duration = expected_compose_duration(duration)
    # Floor rejects truly truncated outputs (lost more than a small lead-in).
    min_output_duration = max(
        0.0,
        float(duration) - max(0.0, video_lead_in) - 0.05,
    )
    # Final CFR for the published video. Keep chat overlay at config.fps;
    # do not force the whole encode down to overlay sampling rate.
    output_fps = media_probe.resolve_output_fps(
        video_path,
        explicit=getattr(config, "output_fps", None),
        fallback=max(1, int(getattr(config, "fps", 30) or 30)),
    )
    print(f"  成片输出帧率: {output_fps}fps (弹幕层 {config.fps}fps)", flush=True)

    if video_lead_in > 0.001:
        print(
            f"  检测到视频相对音频延后 {video_lead_in:.3f}s；"
            f"首帧冻结并把两条流改写为从 0 开始（编辑器友好）",
            flush=True,
        )
        print(
            f"  成片目标时长约 {output_duration:.3f}s（与源/渲染窗一致，不额外 +lead-in）",
            flush=True,
        )
        # Pad main with frozen first frame for lead-in, then trim back to
        # output_duration so container length stays ~source (not source+lead_in).
        # Chat is delayed by lead-in (setpts+lead_in/TB), so its last lead_in
        # seconds land past output_duration on the output timeline. The -t cap
        # below is raised by video_lead_in so the muxer itself cannot clip that
        # delayed region; streams still end naturally at ~output_duration, so
        # the published length stays ~render/source.
        main_filter = (
            f"[0:v]setpts=PTS-STARTPTS,"
            f"tpad=start_duration={video_lead_in:.6f}:start_mode=clone,"
            f"trim=duration={output_duration:.6f},setpts=PTS-STARTPTS[main]"
        )
        chat_filter = (
            f"[1:v]setpts=PTS-STARTPTS+"
            f"{video_lead_in:.6f}/TB[chat]"
        )
    else:
        if source_lead_in > 0.001 and seek_ss > 1e-6:
            print(
                f"  跳过源片头 lead-in 冻结（preview seek={seek_ss:.3f}s，"
                f"源 lead-in={source_lead_in:.3f}s 仅作用于片头）",
                flush=True,
            )
        main_filter = "[0:v]setpts=PTS-STARTPTS[main]"
        chat_filter = "[1:v]setpts=PTS-STARTPTS[chat]"

    # eof_action=pass keeps the main video when overlay ends early, instead of
    # shortest=1 which can silently truncate the finished product.
    video_filter = (
        f"{main_filter};"
        f"{chat_filter};"
        f"[main][chat]overlay={config.x}:{config.y}:eof_action=pass:shortest=0[outv]"
    )

    # Dense/mid preview: -ss MUST bind to the source VIDEO input (next -i), not the
    # overlay. FFmpeg applies input options to the following -i only — putting
    # -ss after video -i and before overlay -i seeks the wrong stream and leaves
    # head picture under mid-VOD rebased chat (silent A/V vs chat mismatch).
    cmd2 = [require_executable("ffmpeg"), "-y"]
    if seek_ss > 1e-6:
        cmd2 += ["-ss", f"{seek_ss:.6f}"]
    cmd2 += ["-i", video_path]
    if use_png_direct:
        cmd2 += [
            "-framerate", str(config.fps),
            "-start_number", str(start_number),
            "-i", frames_pattern,
        ]
    else:
        cmd2 += ["-i", overlay_input]

    cmd2 += [
        "-filter_complex", video_filter,
        "-map", "[outv]",
        "-map", "0:a?",
        *build_video_encode_args(encode),
        "-r", media_probe.fps_to_ffmpeg_rate(output_fps), "-fps_mode", "cfr",
        *build_audio_encode_args_for_compose(
            encode,
            source_has_audio,
            video_lead_in=video_lead_in,
            audio_delay_ms=audio_delay_ms,
            notes=encode.notes if hasattr(encode, "notes") else None,
        ),
        "-movflags", "+faststart",
        # MP4 的 make_zero 会引入 AAC priming / H.264 重排后的首帧偏移；
        # 保留重编码后从 0 开始的时间戳，对编辑器兼容性更好。
        "-avoid_negative_ts", "disabled",
        # -t 是上限而非目标：lead-in 冻结把 chat 层延后 video_lead_in 秒，
        # 其最后 lead_in 秒落在 output_duration 之后，若上限不含 lead-in，
        # muxer 会截掉聊天时间线末尾的消息（流自然结束时实际时长不受影响）。
        "-t", f"{output_duration + max(0.0, video_lead_in):.6f}",
        partial_path,
    ]
    log_path = os.path.join(out_dir, "ffmpeg-overlay.log")
    t1 = time.perf_counter()
    with open(log_path, "w", encoding="utf-8", errors="replace") as log_file:
        r = run_tracked(cmd2, stdout=subprocess.DEVNULL, stderr=log_file, text=True)
    stage_timings["mux_encode"] = time.perf_counter() - t1

    if r.returncode != 0:
        try:
            tail = Path(log_path).read_text(encoding="utf-8", errors="replace")[-1200:]
        except OSError:
            tail = "日志不可读取"
        print(f"  视频合成错误；完整日志: {log_path}\n{tail}", flush=True)
        # If hardware encoder failed under auto/nvenc/qsv/amf, surface a clear hint.
        if encode.resolved_encoder in ("nvenc", "qsv", "amf"):
            print(
                "  提示: 硬件编码器失败时可改用 --encoder x264，或检查 GPU 驱动 / ffmpeg 是否支持该 encoder",
                flush=True,
            )
        return None

    ok, summary, reason = validate_rendered_output(
        partial_path,
        expected_duration=output_duration,
        require_audio=source_has_audio,
        min_duration=min_output_duration if min_output_duration > 0 else None,
    )
    # Do not publish a technically playable overlay with malformed timeline
    # metadata.  This gate is intentionally after FFmpeg but before os.replace.
    if ok:
        from media_health import validate_media_health
        health = validate_media_health(partial_path, mode="fast", require_audio=source_has_audio)
        if not health.ok:
            ok = False
            reason = "媒体健康检查失败: " + health.reason()
    if not ok:
        print(
            f"  输出验证失败: {reason}\n"
            f"  探测结果: duration={summary.get('duration')} has_video={summary.get('has_video')} "
            f"has_audio={summary.get('has_audio')}\n"
            f"  保留临时文件供排查: {partial_path}",
            flush=True,
        )
        return None

    # Back up existing output before overwriting (default behavior).
    # If the subsequent replace fails, restore .bak when possible.
    backup = None
    backup_created = False
    if not getattr(config, "no_backup_prev", False) and os.path.isfile(out_path):
        backup = out_path + ".bak"
        try:
            if os.path.isfile(backup):
                os.remove(backup)
            os.rename(out_path, backup)
            backup_created = True
            print(f"  [backup] {backup}", flush=True)
        except OSError as e:
            print(f"  warning: cannot backup {out_path}: {e}", flush=True)
            backup = None
            backup_created = False
    try:
        # C-4: 发布走重试。播放器/编辑器占着旧输出文件时，单次 os.replace
        # 会在 Windows 上以 PermissionError (WinError 5/32) 失败；多数情况
        # 只是瞬时共享冲突（读方关闭后即可成功）。atomic_replace_with_retry
        # 仅对 PermissionError 做短退避重试（非 PermissionError 的 OSError
        # 原样抛出），耗尽后仍抛 PermissionError，由下方 except OSError
        # 接住并走既有 .bak 回滚分支——最终失败语义不变。
        atomic_replace_with_retry(partial_path, out_path)
    except OSError as e:
        print(f"  发布失败: 无法将 {partial_path} 替换为 {out_path}: {e}", flush=True)
        if backup_created and backup and os.path.isfile(backup) and not os.path.isfile(out_path):
            try:
                os.rename(backup, out_path)
                print(f"  已从备份恢复: {out_path}", flush=True)
            except OSError as restore_err:
                print(f"  警告: 无法从备份恢复 {backup}: {restore_err}", flush=True)
        return None
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(
        f"  输出: {out_path} ({size_mb:.1f} MB, {summary['duration']:.2f}s, "
        f"video={summary['has_video']}, audio={summary['has_audio']})",
        flush=True,
    )
    if stage_timings:
        print("  阶段耗时:", flush=True)
        total = sum(stage_timings.values()) or 1.0
        for name, sec in stage_timings.items():
            print(f"    - {name}: {sec:.1f}s ({sec / total * 100:.0f}%)", flush=True)
    return ComposeResult(
        output_path=out_path,
        output_fps=output_fps,
        summary=summary,
        timings=stage_timings,
    )
