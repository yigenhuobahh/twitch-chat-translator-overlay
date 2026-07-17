#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Video encode option parsing, hardware encoder detection, and FFmpeg argv builders."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from functools import lru_cache
import re
import subprocess
from typing import Any

from common_utils import require_executable, safe_which

SUPPORTED_ENCODERS = ("auto", "x264", "nvenc", "qsv", "amf")
SUPPORTED_AUDIO = ("aac", "copy")
SUPPORTED_OVERLAY_CODECS = ("vp9", "png")  # png = feed PNG sequence directly to overlay


@dataclass
class EncodeOptions:
    """User-facing encode knobs for compose_video."""

    encoder: str = "auto"  # auto|x264|nvenc|qsv|amf
    video_codec: str = "libx264"  # resolved concrete codec
    video_preset: str = "fast"
    crf: int | None = 18
    video_bitrate: str | None = None  # e.g. 8M; if set, prefer bitrate mode over CRF/CQ
    maxrate: str | None = None
    bufsize: str | None = None
    audio_codec: str = "aac"  # aac|copy
    audio_bitrate: str = "192k"
    overlay_codec: str = "vp9"  # vp9|png
    webm_crf: int = 30
    webm_cpu_used: int = 4  # libvpx-vp9 speed: 0=slow/best .. 8=fast
    prefer_hw: bool = True
    resolved_encoder: str = "x264"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_bitrate(value: str | None, name: str = "bitrate") -> str | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().lower()
    if not re.fullmatch(r"\d+[kmg]?", text):
        raise ValueError(f"{name} must look like 8M / 4000k / 12000000, got {value!r}")
    return text


@lru_cache(maxsize=1)
def list_ffmpeg_encoders() -> set[str]:
    """Return encoder names advertised by local ffmpeg."""
    ffmpeg = safe_which("ffmpeg")
    if not ffmpeg:
        return set()
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    names: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        # lines look like: " V..... h264_nvenc           NVIDIA NVENC H.264 encoder"
        m = re.match(r"\s*[A-Z\.]{6}\s+(\S+)", line)
        if m:
            names.add(m.group(1))
    return names


def detect_hw_encoders(available: set[str] | None = None) -> dict[str, str]:
    """
    Map logical encoder id -> concrete ffmpeg codec name if present.
    Does not prove the GPU driver works, only that ffmpeg was built with the encoder.
    """
    encoders = available if available is not None else list_ffmpeg_encoders()
    found: dict[str, str] = {}
    if "h264_nvenc" in encoders:
        found["nvenc"] = "h264_nvenc"
    if "h264_qsv" in encoders:
        found["qsv"] = "h264_qsv"
    if "h264_amf" in encoders:
        found["amf"] = "h264_amf"
    if "libx264" in encoders:
        found["x264"] = "libx264"
    return found


def _trial_encode(codec: str) -> bool:
    """Encode a single blank frame with the given codec to verify it works.

    Returns True if the encoder produces a valid output, False otherwise.
    """
    import os
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name
        r = subprocess.run(
            [
                require_executable("ffmpeg"), "-y", "-f", "lavfi", "-i",
                "color=c=black:s=2x2:d=0.04",
                "-frames:v", "1", "-c:v", codec,
                "-pix_fmt", "yuv420p", tmp_path,
            ],
            capture_output=True, text=True, timeout=15,
        )
        ok = r.returncode == 0 and os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0
        return ok
    except Exception:
        return False
    finally:
        try:
            os.remove(tmp_path)
        except (OSError, UnboundLocalError):
            pass


def resolve_encode_options(
    *,
    encoder: str = "auto",
    video_preset: str | None = None,
    crf: int | None = 18,
    video_bitrate: str | None = None,
    maxrate: str | None = None,
    bufsize: str | None = None,
    audio_codec: str = "aac",
    audio_bitrate: str = "192k",
    overlay_codec: str = "vp9",
    webm_crf: int = 30,
    webm_cpu_used: int = 4,
    prefer_hw: bool = True,
) -> EncodeOptions:
    encoder = (encoder or "auto").strip().lower()
    if encoder not in SUPPORTED_ENCODERS:
        raise ValueError(f"unsupported encoder {encoder!r}; choose from {', '.join(SUPPORTED_ENCODERS)}")

    audio_codec = (audio_codec or "aac").strip().lower()
    if audio_codec not in SUPPORTED_AUDIO:
        raise ValueError(f"unsupported audio codec {audio_codec!r}; choose aac or copy")

    overlay_codec = (overlay_codec or "vp9").strip().lower()
    if overlay_codec not in SUPPORTED_OVERLAY_CODECS:
        raise ValueError(f"unsupported overlay codec {overlay_codec!r}; choose vp9 or png")

    video_bitrate = parse_bitrate(video_bitrate, "video_bitrate")
    maxrate = parse_bitrate(maxrate, "maxrate")
    bufsize = parse_bitrate(bufsize, "bufsize")
    audio_bitrate = parse_bitrate(audio_bitrate, "audio_bitrate") or "192k"

    if crf is not None and not (0 <= int(crf) <= 51):
        raise ValueError("crf must be between 0 and 51")
    if not (0 <= int(webm_crf) <= 63):
        raise ValueError("webm_crf must be between 0 and 63")
    if not (0 <= int(webm_cpu_used) <= 8):
        raise ValueError("webm_cpu_used must be between 0 and 8")

    available = detect_hw_encoders()
    notes: list[str] = []
    resolved = "x264"
    concrete = "libx264"

    if encoder == "auto":
        if prefer_hw:
            for key in ("nvenc", "qsv", "amf"):
                if key in available:
                    candidate_concrete = available[key]
                    if _trial_encode(candidate_concrete):
                        resolved = key
                        concrete = candidate_concrete
                        notes.append(f"auto selected hardware encoder: {key} ({concrete})")
                        break
                    else:
                        notes.append(
                            f"auto: {key} ({candidate_concrete}) listed by ffmpeg but trial encode failed; "
                            f"skipping (driver/GPU may be unavailable)"
                        )
            else:
                resolved = "x264"
                concrete = available.get("x264", "libx264")
                notes.append("auto: no working hardware H.264 encoder found, using libx264")
        else:
            resolved = "x264"
            concrete = available.get("x264", "libx264")
            notes.append("prefer_hw=false, using libx264")
    elif encoder == "x264":
        resolved = "x264"
        concrete = available.get("x264", "libx264")
    else:
        if encoder not in available:
            notes.append(
                f"requested encoder {encoder} not listed by ffmpeg -encoders; "
                f"will still try { {'nvenc':'h264_nvenc','qsv':'h264_qsv','amf':'h264_amf'}[encoder] } and may fail"
            )
            concrete = {"nvenc": "h264_nvenc", "qsv": "h264_qsv", "amf": "h264_amf"}[encoder]
        else:
            concrete = available[encoder]
            # Trial: if explicitly requested HW encoder doesn't actually work,
            # warn early but still respect user's explicit choice.
            if encoder in ("nvenc", "qsv", "amf") and not _trial_encode(concrete):
                notes.append(
                    f"warning: {encoder} ({concrete}) trial encode failed; "
                    f"render may fail (driver/GPU issue?). Use --encoder x264 as fallback."
                )
        resolved = encoder

    # Default presets differ by family.
    if video_preset is None or str(video_preset).strip() == "":
        if resolved == "nvenc":
            video_preset = "p4"
        elif resolved in ("qsv", "amf"):
            video_preset = "balanced"
        else:
            video_preset = "fast"
    else:
        video_preset = str(video_preset).strip()

    return EncodeOptions(
        encoder=encoder,
        video_codec=concrete,
        video_preset=video_preset,
        crf=None if video_bitrate else (int(crf) if crf is not None else 18),
        video_bitrate=video_bitrate,
        maxrate=maxrate,
        bufsize=bufsize,
        audio_codec=audio_codec,
        audio_bitrate=audio_bitrate,
        overlay_codec=overlay_codec,
        webm_crf=int(webm_crf),
        webm_cpu_used=int(webm_cpu_used),
        prefer_hw=prefer_hw,
        resolved_encoder=resolved,
        notes=notes,
    )


def build_video_encode_args(opts: EncodeOptions) -> list[str]:
    """Return ffmpeg argv fragments for the final video encode (no -i / maps)."""
    args: list[str] = ["-c:v", opts.video_codec]
    family = opts.resolved_encoder

    if family == "nvenc":
        args += ["-preset", opts.video_preset]
        if opts.video_bitrate:
            args += ["-b:v", opts.video_bitrate]
            if opts.maxrate:
                args += ["-maxrate", opts.maxrate]
            if opts.bufsize:
                args += ["-bufsize", opts.bufsize]
        else:
            # CQ / VBR quality mode for NVENC
            args += [
                "-rc", "vbr",
                "-cq", str(opts.crf if opts.crf is not None else 19),
                "-b:v", "0",
            ]
            if opts.maxrate:
                args += ["-maxrate", opts.maxrate]
            if opts.bufsize:
                args += ["-bufsize", opts.bufsize]
        args += ["-pix_fmt", "yuv420p"]
        return args

    if family == "qsv":
        args += ["-preset", opts.video_preset]
        if opts.video_bitrate:
            args += ["-b:v", opts.video_bitrate]
        else:
            args += ["-global_quality", str(opts.crf if opts.crf is not None else 22)]
        if opts.maxrate:
            args += ["-maxrate", opts.maxrate]
        if opts.bufsize:
            args += ["-bufsize", opts.bufsize]
        args += ["-pix_fmt", "nv12"]
        return args

    if family == "amf":
        args += ["-quality", opts.video_preset]
        if opts.video_bitrate:
            args += ["-b:v", opts.video_bitrate]
        else:
            args += ["-rc", "cqp", "-qp_i", str(opts.crf if opts.crf is not None else 20),
                     "-qp_p", str(opts.crf if opts.crf is not None else 20)]
        args += ["-pix_fmt", "yuv420p"]
        return args

    # libx264 default
    args += ["-preset", opts.video_preset]
    if opts.video_bitrate:
        args += ["-b:v", opts.video_bitrate]
        if opts.maxrate:
            args += ["-maxrate", opts.maxrate]
        if opts.bufsize:
            args += ["-bufsize", opts.bufsize]
    else:
        args += ["-crf", str(opts.crf if opts.crf is not None else 18)]
    args += ["-pix_fmt", "yuv420p"]
    return args


def build_audio_encode_args(
    opts: EncodeOptions,
    source_has_audio: bool,
    *,
    video_lead_in: float = 0.0,
    notes: list[str] | None = None,
) -> list[str]:
    """Build audio encode args.

    When video_lead_in > 0, stream timestamps are rewritten for video (setpts/tpad).
    Stream-copy cannot apply the same rewrite, so we fall back to AAC + asetpts to
    keep A/V aligned with the editor-friendly zero-based timeline.
    """
    if not source_has_audio:
        return []
    lead = float(video_lead_in or 0.0)
    if opts.audio_codec == "copy" and lead <= 0.001:
        return ["-c:a", "copy"]
    if opts.audio_codec == "copy" and lead > 0.001:
        if notes is not None:
            notes.append(
                f"audio-codec copy 在 lead-in={lead:.3f}s 时无法对齐时间戳，已回退 aac+asetpts"
            )
    return ["-c:a", "aac", "-b:a", opts.audio_bitrate, "-af", "asetpts=PTS-STARTPTS"]


def build_webm_encode_args(opts: EncodeOptions) -> list[str]:
    return [
        "-c:v", "libvpx-vp9",
        "-pix_fmt", "yuva420p",
        "-b:v", "0",
        "-crf", str(opts.webm_crf),
        "-cpu-used", str(opts.webm_cpu_used),
        "-row-mt", "1",
    ]


def summarize_encode_options(opts: EncodeOptions) -> str:
    parts = [
        f"encoder={opts.resolved_encoder}({opts.video_codec})",
        f"preset={opts.video_preset}",
    ]
    if opts.video_bitrate:
        parts.append(f"bitrate={opts.video_bitrate}")
    else:
        parts.append(f"crf/cq={opts.crf}")
    parts.append(f"audio={opts.audio_codec}")
    parts.append(f"overlay={opts.overlay_codec}")
    return ", ".join(parts)
