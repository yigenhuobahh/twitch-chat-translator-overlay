#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Encode preset legality regressions (deep-review finding correctness-1).

Pin the default per-family presets emitted by ``resolve_encode_options`` to
values the concrete ffmpeg encoders actually accept. Verified on this machine
against the local ffmpeg (Windows x64, 2026-09-05) with a live
``ffmpeg -f lavfi -i color=c=black:s=256x256:d=0.04 -frames:v 1 -c:v <codec>
...`` encode:

- h264_qsv ``-preset``: only veryfast..veryslow (0-7 scale, no "balanced").
  "balanced" exits 127 with ``Unable to parse "preset" option value "balanced"``;
  veryfast/faster/fast/medium/slow/slower/veryslow all exit 0.
- h264_amf ``-quality``: balanced/speed/quality/high_quality parse fine
  (rc=8 device-missing is a hardware failure, not an option-parse failure);
  "medium"/"fast" exit 127 with ``Unable to parse "quality" option``.
- h264_nvenc ``-preset``: p1/p4/p7 parse fine (rc=127 nvcuda-missing is a
  driver failure, not option parsing); "balanced" exits 127 parse error.

The bug: the qsv/amf default shared "balanced", which is illegal for
h264_qsv; and vod_merge passed video_preset="medium" for encoder auto/qsv,
which leaked into the AMF branch as an illegal ``-quality medium``.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Measured against the local ffmpeg binary (see module docstring for method).
QSV_LEGAL_PRESETS = {"veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"}
AMF_LEGAL_QUALITY = {"balanced", "speed", "quality", "high_quality"}
NVENC_LEGAL_PRESETS = {"p1", "p2", "p3", "p4", "p5", "p6", "p7"}

FFMPEG_LAVFI = [
    "-y", "-v", "error",
    "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.04",
    "-frames:v", "1",
]


def _ffmpeg_is_qsv_capable() -> bool:
    """True when h264_qsv can actually encode here (needed for live round-trip)."""
    try:
        proc = subprocess.run(
            ["ffmpeg", *FFMPEG_LAVFI, "-c:v", "h264_qsv", "-preset", "medium",
             "-global_quality", "22", "-pix_fmt", "nv12", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _resolve(monkeypatch, encoder: str):
    """Resolve options with detection/trial stubs so tests are machine-independent."""
    import encode_options as mod

    concrete = {"nvenc": "h264_nvenc", "qsv": "h264_qsv", "amf": "h264_amf", "x264": "libx264"}
    monkeypatch.setattr(mod, "detect_hw_encoders", lambda available=None: {encoder: concrete[encoder]})
    monkeypatch.setattr(mod, "_trial_encode", lambda codec: True)
    return mod.resolve_encode_options(encoder=encoder, video_preset=None)


# ---------------------------------------------------------------------------
# resolve_encode_options defaults
# ---------------------------------------------------------------------------


def test_default_preset_qsv_is_legal(monkeypatch):
    opts = _resolve(monkeypatch, "qsv")
    assert opts.resolved_encoder == "qsv"
    assert opts.video_preset in QSV_LEGAL_PRESETS


def test_default_preset_amf_is_legal(monkeypatch):
    opts = _resolve(monkeypatch, "amf")
    assert opts.resolved_encoder == "amf"
    assert opts.video_preset in AMF_LEGAL_QUALITY


def test_default_preset_nvenc_is_legal(monkeypatch):
    opts = _resolve(monkeypatch, "nvenc")
    assert opts.resolved_encoder == "nvenc"
    assert opts.video_preset in NVENC_LEGAL_PRESETS


def test_default_preset_x264_unchanged(monkeypatch):
    opts = _resolve(monkeypatch, "x264")
    assert opts.resolved_encoder == "x264"
    assert opts.video_preset == "fast"


# ---------------------------------------------------------------------------
# build_video_encode_args emits only legal -preset / -quality values
# ---------------------------------------------------------------------------


def test_qsv_branch_preset_is_legal(monkeypatch):
    import encode_options as mod

    opts = _resolve(monkeypatch, "qsv")
    args = mod.build_video_encode_args(opts)
    assert args[0:2] == ["-c:v", "h264_qsv"]
    assert args[2:4] == ["-preset", opts.video_preset]
    assert opts.video_preset in QSV_LEGAL_PRESETS


def test_amf_branch_quality_is_legal(monkeypatch):
    import encode_options as mod

    opts = _resolve(monkeypatch, "amf")
    args = mod.build_video_encode_args(opts)
    assert args[0:2] == ["-c:v", "h264_amf"]
    assert args[2:4] == ["-quality", opts.video_preset]
    assert opts.video_preset in AMF_LEGAL_QUALITY


def test_nvenc_branch_preset_is_legal(monkeypatch):
    import encode_options as mod

    opts = _resolve(monkeypatch, "nvenc")
    args = mod.build_video_encode_args(opts)
    assert args[0:2] == ["-c:v", "h264_nvenc"]
    assert args[2:4] == ["-preset", opts.video_preset]
    assert opts.video_preset in NVENC_LEGAL_PRESETS


# ---------------------------------------------------------------------------
# Live ffmpeg round-trip on the exact argv build_video_encode_args emits
# (skipped on machines without working QSV hardware)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _ffmpeg_is_qsv_capable(), reason="h264_qsv not functional on this machine")
def test_live_qsv_accepts_built_argv(monkeypatch):
    import encode_options as mod

    opts = _resolve(monkeypatch, "qsv")
    args = mod.build_video_encode_args(opts)
    proc = subprocess.run(
        ["ffmpeg", *FFMPEG_LAVFI, *args, "-f", "null", "-"],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Unable to parse" not in proc.stderr


# ---------------------------------------------------------------------------
# vod_merge must not leak a preset across encoder families
# ---------------------------------------------------------------------------


def test_vod_merge_no_cross_family_preset_leak(monkeypatch, tmp_path):
    """vod_merge used to pass video_preset="medium" for encoder auto/qsv.

    "medium" is only legal for h264_qsv; when auto resolved to amf that
    produced an illegal ``-quality medium``. vod_merge must hand
    video_preset=None to resolve_encode_options so the per-family default
    applies.
    """
    import encode_options as eo
    import media_health as mh
    import twitch_download as td
    import vod_merge as vm

    # concat_videos lives in vod_merge but resolves probe/start-time/run_tracked
    # through lazy forwarders into twitch_download, so patch twitch_download.
    monkeypatch.setattr(td, "probe_media_duration", lambda _path: 10.0)
    monkeypatch.setattr(td, "get_stream_start_time", lambda _path, _stream: 0.0)

    captured: dict = {}

    def fake_run_tracked(cmd, **kwargs):
        captured["cmd"] = cmd
        # The primary filter_complex path runs ffmpeg on fake 1-byte segments;
        # that would genuinely fail, so simulate success and let the health
        # check stub return ok.
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(td, "run_tracked", fake_run_tracked)

    # The inline `_run_ffmpeg` closure in concat_videos calls vod_merge's
    # module-global run_tracked forwarder; make sure that one is faked too
    # (it re-imports from twitch_download each call, so the patch above is
    # what matters — but patch vm.run_tracked as well for direct calls).
    monkeypatch.setattr(vm, "run_tracked", fake_run_tracked)

    class FakeHealth:
        ok = True

        @staticmethod
        def reason():
            return ""

    monkeypatch.setattr(mh, "validate_media_health", lambda *a, **k: FakeHealth())

    # Two fake segments force the filter_complex concat path. The fake
    # run_tracked never writes `out`, so pre-create a non-empty output file to
    # pass the post-run size check.
    paths = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    for p in paths:
        p.write_bytes(b"x")
    out = tmp_path / "out.mp4"
    out.write_bytes(b"fake-encode-output")

    for target in ("amf", "qsv"):
        concrete = {"amf": "h264_amf", "qsv": "h264_qsv"}[target]
        family_args = {
            "amf": ["-quality", "balanced", "-pix_fmt", "yuv420p"],
            "qsv": ["-preset", "medium", "-pix_fmt", "nv12"],
        }[target]

        # Bind the loop variables so the closures cannot observe a later
        # iteration's family (ruff B023).
        def fake_resolve(_concrete=concrete, _family_args=family_args, _target=target, **kwargs):
            # The regression: vod_merge must not force a cross-family preset.
            assert kwargs.get("video_preset") is None, (
                "vod_merge must pass video_preset=None so family defaults apply"
            )
            return eo.EncodeOptions(
                encoder="auto",
                video_codec=_concrete,
                video_preset=_family_args[1],
                crf=18,
                resolved_encoder=_target,
            )

        # concat_videos imports these from encode_options inside the function
        # body, so patching the module attributes is what takes effect.
        monkeypatch.setattr(eo, "resolve_encode_options", fake_resolve)
        monkeypatch.setattr(
            eo, "build_video_encode_args",
            lambda opts, _family_args=family_args: ["-c:v", opts.video_codec, *_family_args],
        )

        mode = td.concat_videos(paths, out, encoder="auto")
        assert mode == "reencode"
        cmd = captured["cmd"]
        if target == "amf":
            assert "-quality" in cmd
            quality = cmd[cmd.index("-quality") + 1]
            assert quality in AMF_LEGAL_QUALITY, quality
            assert quality != "medium"
        else:
            assert "-preset" in cmd
            preset = cmd[cmd.index("-preset") + 1]
            assert preset in QSV_LEGAL_PRESETS, preset
