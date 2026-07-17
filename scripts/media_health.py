#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail-closed media health gates for download and render stages."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import subprocess

from common_utils import require_executable, safe_which

_BENIGN_EXTRA_STREAMS = {"attachment", "data", "subtitle"}
_AUDIO_PACKET_SAMPLE_SECONDS = 60.0


@dataclass
class MediaHealth:
    path: Path
    ok: bool
    duration: float = 0.0
    has_video: bool = False
    has_audio: bool = False
    video_start: float = 0.0
    audio_start: float = 0.0
    video_fps: str = ""
    is_cfr: bool | None = None
    extra_streams: list[str] = field(default_factory=list)
    abnormal_audio_packets: int = 0
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def reason(self) -> str:
        return "; ".join(self.issues) or "未知媒体健康错误"


def _number(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rate(value) -> str:
    text = str(value or "")
    return "" if text in ("", "0/0") else text


def _count_abnormal_audio_packets(path: Path, duration: float) -> tuple[int | None, str | None]:
    """Sample audio packet durations without buffering an entire VOD in memory."""
    intervals = ["0%+60"]
    if duration > 2 * _AUDIO_PACKET_SAMPLE_SECONDS:
        intervals.append(f"{max(0.0, duration - _AUDIO_PACKET_SAMPLE_SECONDS):.3f}%+60")

    count = 0
    for interval in intervals:
        packet_cmd = [
            require_executable("ffprobe"),
            "-v",
            "error",
            "-read_intervals",
            interval,
            "-select_streams",
            "a:0",
            "-show_packets",
            "-show_entries",
            "packet=duration_time",
            "-of",
            "csv=p=0",
            str(path),
        ]
        try:
            packets = subprocess.run(
                packet_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return None, str(e)
        if packets.returncode != 0:
            return None, (packets.stderr or "ffprobe 返回失败").strip()[:500]
        count += sum(
            1
            for line in (packets.stdout or "").splitlines()
            if _number(line.strip()) > 0.25
        )
    return count, None


def probe_media_health(path: Path, *, require_audio: bool = True,
                       expected_duration: float | None = None,
                       tolerance: float = 1.0) -> MediaHealth:
    """Run one ffprobe JSON query and return a user-readable health verdict."""
    result = MediaHealth(path=Path(path), ok=False)
    if not result.path.is_file():
        result.issues.append(f"文件不存在: {result.path}")
        return result
    if not safe_which("ffprobe"):
        result.issues.append("未找到 ffprobe，无法执行媒体健康检查")
        return result
    cmd = [require_executable("ffprobe"), "-v", "error", "-show_entries",
           "format=duration:stream=index,codec_type,codec_name,width,height,start_time,r_frame_rate,avg_frame_rate",
           "-of", "json", str(result.path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=45)
    except (OSError, subprocess.TimeoutExpired) as e:
        result.issues.append(f"ffprobe 执行失败: {e}")
        return result
    if proc.returncode != 0:
        result.issues.append((proc.stderr or "ffprobe 返回失败").strip()[:500])
        return result
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as e:
        result.issues.append(f"ffprobe JSON 无法解析: {e}")
        return result
    result.duration = _number((data.get("format") or {}).get("duration"))
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    result.extra_streams = [str(s.get("codec_type")) for s in streams
                            if s.get("codec_type") not in ("video", "audio")]
    if result.duration <= 0:
        result.issues.append("容器时长无效")
    if not video:
        result.issues.append("缺少视频流")
    else:
        result.has_video = True
        result.video_start = _number(video.get("start_time"))
        if _number(video.get("width")) <= 0 or _number(video.get("height")) <= 0:
            result.issues.append("视频尺寸无效")
        r = _rate(video.get("r_frame_rate"))
        a = _rate(video.get("avg_frame_rate"))
        result.video_fps = a or r
        result.is_cfr = None if not r or not a else r == a
    if audio:
        result.has_audio = True
        result.audio_start = _number(audio.get("start_time"))
    elif require_audio:
        result.issues.append("缺少音频流")
    if expected_duration is not None and abs(result.duration - float(expected_duration)) > float(tolerance):
        result.issues.append(f"时长 {result.duration:.3f}s 与预期 {float(expected_duration):.3f}s 偏差超过 {tolerance:.3f}s")
    unsupported_streams = [s for s in result.extra_streams if s not in _BENIGN_EXTRA_STREAMS]
    benign_streams = [s for s in result.extra_streams if s in _BENIGN_EXTRA_STREAMS]
    if unsupported_streams:
        result.issues.append("含不支持的附加流: " + ", ".join(unsupported_streams))
    if benign_streams:
        result.warnings.append("保留附加流: " + ", ".join(benign_streams))
    # AAC packets are normally about 21 ms at 48 kHz. Sample the beginning and
    # end of long VODs so the fast check remains bounded in time and memory.
    if audio:
        result.abnormal_audio_packets, packet_error = _count_abnormal_audio_packets(
            result.path,
            result.duration,
        )
        if packet_error:
            result.issues.append(f"音频包检查失败: {packet_error}")
        elif result.abnormal_audio_packets:
            result.issues.append(
                f"检测到 {result.abnormal_audio_packets} 个异常长音频包（>0.25s）"
            )
    result.ok = not result.issues
    return result


def decode_check_media(path: Path) -> tuple[bool, str]:
    """Full FFmpeg decode check; caller must choose this potentially slow mode."""
    if not safe_which("ffmpeg"):
        return False, "未找到 ffmpeg，无法执行完整解码检查"
    try:
        proc = subprocess.run(
            [require_executable("ffmpeg"), "-v", "error", "-xerror", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=24 * 3600,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"完整解码检查失败: {e}"
    return (proc.returncode == 0, (proc.stderr or "").strip()[-700:])


def validate_media_health(path: Path, *, mode: str = "fast", require_audio: bool = True,
                          expected_duration: float | None = None, allow_extra_streams: bool = False) -> MediaHealth:
    mode = str(mode or "fast").lower()
    if mode == "off":
        return MediaHealth(path=Path(path), ok=True)
    health = probe_media_health(path, require_audio=require_audio, expected_duration=expected_duration)
    if allow_extra_streams and health.extra_streams:
        health.issues = [x for x in health.issues if not x.startswith("含不支持的附加流:")]
        health.ok = not health.issues
    if health.ok and mode == "decode":
        ok, reason = decode_check_media(path)
        if not ok:
            health.ok = False
            health.issues.append("完整解码失败" + (f": {reason}" if reason else ""))
    return health


def repair_media(source: Path, *, encoder: str = "auto", output: Path | None = None) -> Path:
    """Non-destructive A/V normalization; returns a sibling repaired MP4."""
    from encode_options import build_video_encode_args, resolve_encode_options
    source = Path(source)
    final = output or source.with_name(source.stem + ".repaired.mp4")
    partial = final.with_name(final.stem + ".partial.mp4")
    opts = resolve_encode_options(encoder=encoder, crf=18, audio_codec="aac", audio_bitrate="160k")
    cmd = [require_executable("ffmpeg"), "-hide_banner", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
           "-vf", "setpts=PTS-STARTPTS", *build_video_encode_args(opts),
           "-af", "aresample=async=1:first_pts=0", "-c:a", "aac", "-b:a", "160k",
           "-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart", str(partial)]
    proc = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not partial.is_file():
        raise RuntimeError("媒体修复 FFmpeg 失败；原文件未改动")
    health = validate_media_health(partial, mode="fast", require_audio=True)
    if not health.ok:
        raise RuntimeError("修复输出仍不健康: " + health.reason())
    partial.replace(final)
    return final
