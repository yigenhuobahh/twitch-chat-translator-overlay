#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for --render-preset YAML loading / CLI default merge."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from render_preset import (  # noqa: E402
    apply_render_preset_to_namespace,
    load_render_preset,
    normalize_render_dict,
)


def test_load_public_render_fast_preset():
    preset = load_render_preset(ROOT / "profiles" / "render_fast.yaml")
    assert preset["encoder"] == "x264"
    assert preset["overlay_codec"] == "png"
    assert preset["crf"] == 23
    assert preset["lazy_message_images"] is True


def test_apply_only_defaults_cli_wins():
    preset = {
        "encoder": "nvenc",
        "crf": 20,
        "overlay_codec": "png",
        "reuse_static_frames": False,
    }
    args = SimpleNamespace(
        encoder="x264",
        crf=18,
        overlay_codec="vp9",
        video_preset=None,
        video_bitrate=None,
        maxrate=None,
        bufsize=None,
        audio_codec="aac",
        audio_bitrate="192k",
        webm_crf=30,
        webm_cpu_used=4,
        output_fps=None,
        fps=30,
        blank_hold_seconds=0.5,
        message_image_cache_size=256,
        lazy_message_images=False,
        no_reuse_static_frames=False,
        no_skip_blank_frames=False,
    )
    applied = apply_render_preset_to_namespace(
        args,
        preset,
        cli_defaults={
            "encoder": "x264",
            "crf": 18,
            "overlay_codec": "vp9",
            "lazy_message_images": False,
        },
    )
    assert args.encoder == "nvenc"
    assert args.crf == 20
    assert args.overlay_codec == "png"
    assert args.no_reuse_static_frames is True
    assert "encoder" in applied

    # explicit CLI non-default should win
    args2 = SimpleNamespace(
        encoder="qsv",  # user overrode
        crf=18,
        overlay_codec="vp9",
        video_preset=None,
        video_bitrate=None,
        maxrate=None,
        bufsize=None,
        audio_codec="aac",
        audio_bitrate="192k",
        webm_crf=30,
        webm_cpu_used=4,
        output_fps=None,
        fps=30,
        blank_hold_seconds=0.5,
        message_image_cache_size=256,
        lazy_message_images=False,
        no_reuse_static_frames=False,
        no_skip_blank_frames=False,
    )
    applied2 = apply_render_preset_to_namespace(
        args2,
        preset,
        cli_defaults={"encoder": "x264", "crf": 18, "overlay_codec": "vp9"},
    )
    assert args2.encoder == "qsv"
    assert "encoder" not in applied2


def test_normalize_accepts_flat_or_nested():
    flat = normalize_render_dict({"encoder": "x264", "crf": 19})
    nested = normalize_render_dict({"render": {"encoder": "x264", "crf": 19}, "name": "t"})
    assert flat["crf"] == 19
    assert nested["crf"] == 19
    assert nested["_meta"]["name"] == "t"


def test_none_default_fields_only_apply_when_still_none():
    """default is None must NOT mean always-apply; explicit CLI values win."""
    preset = {
        "video_preset": "slow",
        "video_bitrate": "8M",
        "maxrate": "12M",
        "bufsize": "16M",
        "output_fps": 60,
        "encoder": "nvenc",
    }
    # User set None-default fields explicitly
    args = SimpleNamespace(
        encoder="x264",
        video_preset="ultrafast",
        video_bitrate="4M",
        maxrate="6M",
        bufsize="8M",
        output_fps=24,
        crf=18,
        audio_codec="aac",
        audio_bitrate="192k",
        overlay_codec="vp9",
        webm_crf=30,
        webm_cpu_used=4,
        fps=30,
        blank_hold_seconds=0.5,
        message_image_cache_size=256,
        lazy_message_images=False,
        no_reuse_static_frames=False,
        no_skip_blank_frames=False,
    )
    applied = apply_render_preset_to_namespace(
        args,
        preset,
        cli_defaults={
            "encoder": "x264",
            "video_preset": None,
            "video_bitrate": None,
            "maxrate": None,
            "bufsize": None,
            "output_fps": None,
            "crf": 18,
            "overlay_codec": "vp9",
        },
    )
    assert args.video_preset == "ultrafast"
    assert args.video_bitrate == "4M"
    assert args.maxrate == "6M"
    assert args.bufsize == "8M"
    assert args.output_fps == 24
    assert args.encoder == "nvenc"
    assert "video_preset" not in applied
    assert "video_bitrate" not in applied
    assert "encoder" in applied

    # Still-None fields should receive preset values
    args3 = SimpleNamespace(
        encoder="x264",
        video_preset=None,
        video_bitrate=None,
        maxrate=None,
        bufsize=None,
        output_fps=None,
        crf=18,
        audio_codec="aac",
        audio_bitrate="192k",
        overlay_codec="vp9",
        webm_crf=30,
        webm_cpu_used=4,
        fps=30,
        blank_hold_seconds=0.5,
        message_image_cache_size=256,
        lazy_message_images=False,
        no_reuse_static_frames=False,
        no_skip_blank_frames=False,
    )
    applied3 = apply_render_preset_to_namespace(
        args3,
        preset,
        cli_defaults={
            "encoder": "x264",
            "video_preset": None,
            "video_bitrate": None,
            "maxrate": None,
            "bufsize": None,
            "output_fps": None,
            "crf": 18,
        },
    )
    assert args3.video_preset == "slow"
    assert args3.video_bitrate == "8M"
    assert args3.output_fps == 60
    assert "video_preset" in applied3
    assert "output_fps" in applied3
