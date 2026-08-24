#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Canonical task-plan projection for pipeline entry adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FlagSpec = tuple[str, str, str]

# Burn CLI options that are path or mode specific rather than shared pipeline
# settings. Pipeline call sites may still append them deliberately.
BURN_ONLY_FLAGS: tuple[str, ...] = (
    "export-translation",
    "import-translation",
    "force-export",
    "strict-import",
    "job-dir",
    "no-job-dir",
    "out-dir",
)

FPS_FORWARD_SPECS: tuple[FlagSpec, ...] = (
    ("fps", "--fps", "always"),
    ("output_fps", "--output-fps", "opt"),
)

LAYOUT_FORWARD_SPECS: tuple[FlagSpec, ...] = (
    ("max_visible", "--max-visible", "opt"),
    ("msg_lifetime", "--msg-lifetime", "opt"),
    ("max_message_lines", "--max-message-lines", "opt"),
    ("min_visible_seconds", "--min-visible-seconds", "opt"),
    ("arrival_interval", "--arrival-interval", "opt"),
    ("stack_mode", "--stack-mode", "opt"),
    ("x_ratio", "--x-ratio", "opt"),
    ("y_ratio", "--y-ratio", "opt"),
    ("width_ratio", "--width-ratio", "opt"),
    ("height_ratio", "--height-ratio", "opt"),
    ("font_size_ratio", "--font-size-ratio", "opt"),
    ("emote_height", "--emote-height", "opt"),
    ("lazy_message_images", "--lazy-message-images", "flag"),
)

PERF_FORWARD_SPECS: tuple[FlagSpec, ...] = (
    ("encoder", "--encoder", "always"),
    ("video_preset", "--video-preset", "opt_truthy"),
    ("crf", "--crf", "always"),
    ("video_bitrate", "--video-bitrate", "opt_truthy"),
    ("maxrate", "--maxrate", "opt_truthy"),
    ("bufsize", "--bufsize", "opt_truthy"),
    ("audio_codec", "--audio-codec", "always"),
    ("audio_bitrate", "--audio-bitrate", "always"),
    ("overlay_codec", "--overlay-codec", "always"),
    ("webm_crf", "--webm-crf", "always"),
    ("webm_cpu_used", "--webm-cpu-used", "always"),
    ("no_reuse_static_frames", "--no-reuse-static-frames", "flag"),
    ("no_skip_blank_frames", "--no-skip-blank-frames", "flag"),
    ("blank_hold_seconds", "--blank-hold-seconds", "always"),
)

SHARED_FORWARD_FLAGS: tuple[str, ...] = tuple(
    flag
    for _attr, flag, _kind in (
        *FPS_FORWARD_SPECS,
        *LAYOUT_FORWARD_SPECS,
        *PERF_FORWARD_SPECS,
    )
) + ("--message-image-cache-size",)

PIPELINE_VALUE_FLAGS: tuple[tuple[str, str], ...] = (
    ("output", "--output"),
    ("translation_json", "--translation-json"),
    ("target_language", "--target-language"),
    ("layout_preset", "--layout-preset"),
    ("render_preset", "--render-preset"),
    ("profile", "--profile"),
    ("rules", "--rules"),
    ("encoder", "--encoder"),
    ("crf", "--crf"),
    ("workers", "--workers"),
    ("source_media_check", "--source-media-check"),
    ("offset", "--offset"),
)

PIPELINE_BOOLEAN_FLAGS: tuple[tuple[str, str], ...] = (
    ("render_original", "--render-original"),
    ("reuse_translation", "--reuse-translation"),
    ("keep_temp", "--keep-temp"),
    ("review", "--review"),
    ("manual_translation", "--manual-translation"),
)


def append_flag_specs(command: list[str], args: object, specs: Sequence[FlagSpec]) -> list[str]:
    """Project shared burn options from an argparse-like object."""
    for attr, flag, kind in specs:
        if kind == "always":
            command.extend([flag, str(getattr(args, attr))])
            continue
        if not hasattr(args, attr):
            continue
        value = getattr(args, attr)
        if kind == "opt":
            if value is not None and value != "":
                command.extend([flag, str(value)])
        elif kind == "opt_truthy":
            if value:
                command.extend([flag, str(value)])
        elif kind == "flag":
            if value:
                command.append(flag)
        else:
            raise ValueError(f"unknown flag-forward kind: {kind!r} for {attr}")
    return command


def append_fps_args(command: list[str], args: object) -> list[str]:
    return append_flag_specs(command, args, FPS_FORWARD_SPECS)


def append_layout_burn_args(command: list[str], args: object) -> list[str]:
    append_flag_specs(command, args, LAYOUT_FORWARD_SPECS)
    if getattr(args, "lazy_message_images", False):
        command.extend(["--message-image-cache-size", str(getattr(args, "message_image_cache_size", 256))])
    return command


def append_perf_encode_args(command: list[str], args: object) -> list[str]:
    return append_flag_specs(command, args, PERF_FORWARD_SPECS)


def append_strict_import_arg(command: list[str], args: object) -> list[str]:
    if getattr(args, "strict_import", False):
        command.append("--strict-import")
    return command


def append_shared_burn_args(command: list[str], args: object) -> list[str]:
    append_fps_args(command, args)
    append_layout_burn_args(command, args)
    return append_perf_encode_args(command, args)


@dataclass(frozen=True)
class PipelinePlan:
    """Canonical job fields projected into a compatible pipeline command."""

    fields: Mapping[str, Any]
    source_job: str = ""

    def build_command(
        self,
        python: str,
        pipeline: str | Path,
        *,
        job_path: str | Path | None = None,
    ) -> list[str]:
        fields = self.fields
        video = fields.get("video")
        chat_html = fields.get("chat_html")
        mode = fields.get("mode")
        if not video or not chat_html or not mode:
            raise ValueError("pipeline plan requires video, chat_html, and mode")

        command = [str(python), str(pipeline)]
        source_value = str(job_path) if job_path is not None else self.source_job
        source = Path(source_value).expanduser() if source_value.strip() else None
        if source and source.is_file():
            command.extend(["--job", str(source)])
        command.extend([str(video), str(chat_html), "--yes", "--mode", str(mode)])
        for key, flag in PIPELINE_VALUE_FLAGS:
            value = fields.get(key)
            if value not in (None, ""):
                command.extend([flag, str(value)])
        if fields.get("preview_clip") is not None:
            command.extend(["--preview-clip", str(fields["preview_clip"])])
        for key, flag in PIPELINE_BOOLEAN_FLAGS:
            if fields.get(key):
                command.append(flag)
        return command
