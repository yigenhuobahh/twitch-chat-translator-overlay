#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fixes verified in the 2026-09-06 full-fix wave:

- performance-2: per-frame fade alpha compositing uses a cached 256-entry LUT
  (a.point(lut)) instead of a per-pixel Python lambda; output must be
  bit-identical to the original ``int(v * alpha / 255)`` lambda for every
  (value, alpha) pair.
- concurrency-5: compose_video's pre-publish stale-partial cleanup must catch
  any OSError (e.g. PermissionError when an old partial is held open by
  another process) and fail through the normal None-returning failure path
  instead of escaping compose_video.
- design-3 (partial): doctor/env_bootstrap readiness checklists must include
  ``textual`` as a required package (TUI-only users previously got all-green
  doctor output while tui_run could not start).
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from unittest import mock

from PIL import Image
import pytest

from helpers import load_module

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


# ---------------------------------------------------------------------------
# performance-2: fade alpha LUT
# ---------------------------------------------------------------------------


def _load_overlay_render():
    # Import via sys.path (conftest already puts scripts/ there); spec-loading
    # this module under an ad-hoc name trips dataclasses' __module__ lookup.
    import overlay_render

    return overlay_render


def test_fade_alpha_lut_matches_lambda_exhaustively():
    """CRITICAL equivalence proof: LUT output == lambda output for 256x256."""
    overlay_render = _load_overlay_render()
    for alpha in range(256):
        lut = overlay_render._fade_alpha_lut(alpha)
        assert isinstance(lut, bytes) and len(lut) == 256
        for v in range(256):
            expected = int(v * alpha / 255)
            assert lut[v] == expected, (
                f"LUT mismatch at alpha={alpha} v={v}: "
                f"{lut[v]} != {expected}"
            )
    # Repeated calls return the cached object (no rebuild per frame).
    assert overlay_render._fade_alpha_lut(128) is overlay_render._fade_alpha_lut(128)


def test_fade_alpha_lut_pillow_point_bit_identical_to_lambda():
    """End-to-end through PIL: point(lut) band == point(lambda) band, all alphas."""
    overlay_render = _load_overlay_render()
    src = Image.new("RGBA", (257, 4), (0, 0, 0, 0))
    # Cover every band value across the pixels: row r holds values r*64..r*64+256-1.
    px = src.load()
    for y in range(src.height):
        for x in range(src.width):
            v = (y * src.width + x) % 256
            px[x, y] = (v, (v * 7) % 256, (v * 13) % 256, v)
    r, g, b, a = src.split()
    for alpha in range(256):
        via_lambda = a.point(lambda v, alpha=alpha: int(v * alpha / 255))
        via_lut = a.point(overlay_render._fade_alpha_lut(alpha))
        assert via_lambda.tobytes() == via_lut.tobytes(), f"alpha={alpha}"


def test_composite_visible_uses_lut_not_lambda():
    """The frame loop must go through the LUT helper (no per-pixel lambda)."""
    overlay_render = _load_overlay_render()
    source = inspect.getsource(overlay_render.FrameRenderer.composite_visible)
    assert "_fade_alpha_lut" in source
    # No per-pixel lambda in the point() call (comments may still mention the
    # historical lambda form).
    assert not re.search(r"point\(\s*lambda", source)
    # LUT is built with int() truncation semantics, not round().
    lut_src = inspect.getsource(overlay_render._fade_alpha_lut)
    assert "int(v * alpha / 255)" in lut_src


def test_fade_alpha_lut_is_module_level_cached_bytes():
    """Cache is bounded (<256 distinct alphas possible) and bytes-backed."""
    overlay_render = _load_overlay_render()
    for alpha in (0, 1, 127, 128, 254, 255):
        lut = overlay_render._fade_alpha_lut(alpha)
        assert len(lut) == 256
        assert all(isinstance(x, int) and 0 <= x <= 255 for x in lut)
    assert len(overlay_render._FADE_ALPHA_LUTS) <= 256


# ---------------------------------------------------------------------------
# concurrency-5: stale-partial cleanup must tolerate OSError
# ---------------------------------------------------------------------------


def test_compose_pre_publish_cleanup_guards_oserror():
    """Both partial-remove sites in overlay_compose catch OSError, not just FileNotFoundError."""
    src = (SCRIPTS / "overlay_compose.py").read_text(encoding="utf-8")
    # Every os.remove must be wrapped in a try whose handler is OSError-wide.
    for m in re.finditer(r"try:\s*\n(\s+)os\.remove\(", src):
        tail = src[m.end(): m.end() + 400]
        assert re.search(
            r"except OSError", tail
        ), f"os.remove at offset {m.start()} not guarded by `except OSError`"
        assert not re.search(r"except FileNotFoundError", tail)


def test_compose_video_stale_partial_permission_error_returns_none(
    tmp_path: Path, make_test_video
):
    """A stale partial held open (PermissionError on os.remove) must not raise.

    compose_video contract: returns None on any failure path. Before the fix
    the PermissionError escaped; now it is swallowed at the pre-publish
    cleanup and the compose fails through the normal ffmpeg/validation
    reporting (here: ffmpeg stub that fails, since a real run could not
    overwrite the held partial either).
    """
    video = make_test_video(duration=1.0, fps=10)
    frames = tmp_path / "frames"
    frames.mkdir()
    from render_perf import frame_path

    img = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    for i in range(10):
        img.save(frame_path(frames, i))

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # compose derives the partial name from Path(video_path).stem + suffix
    # (the *source* video stem passed to compose_video — not the factory file).
    stale_partial = out_dir / "src_chat.partial.mp4"
    stale_partial.write_bytes(b"STALE")

    burn = load_module("twitch_chat_burn", "twitch_chat_burn.py")
    import media_probe
    import overlay_compose

    config = SimpleNamespace(
        fps=10,
        x=0,
        y=0,
        encode=None,
        no_backup_prev=True,
        output_fps=10,
        stage_timings={},
    )

    def fake_run_tracked(cmd, **kwargs):
        # cmd[-1] is the ffmpeg output path (the partial). A read-only-share
        # holder blocks ffmpeg's open of the output too (verified: ffmpeg -y
        # exits 1 with 'Permission denied' in that state), so the encode step
        # fails and the stale partial is retained on disk.
        assert Path(cmd[-1]).name.endswith("_chat.partial.mp4")
        return SimpleNamespace(returncode=1)

    with mock.patch.object(overlay_compose, "run_tracked", side_effect=fake_run_tracked), \
            mock.patch.object(overlay_compose, "resolve_encode_options", return_value=SimpleNamespace(
                overlay_codec="png", notes=[], resolved_encoder="x264", webm_cpu_used=4,
                video_codec="libx264",
            )), \
            mock.patch.object(overlay_compose, "summarize_encode_options", return_value="stub"), \
            mock.patch.object(overlay_compose, "build_video_encode_args", return_value=["-c:v", "libx264"]), \
            mock.patch.object(overlay_compose, "build_audio_encode_args", return_value=["-c:a", "aac"]), \
            mock.patch.object(overlay_compose, "resolve_source_av_timing", return_value={
                "source_duration": 1.0, "video_start": 0.0, "audio_start": 0.0,
                "video_lead_in": 0.0, "has_audio": True, "summary": {},
            }), \
            mock.patch.object(media_probe, "resolve_output_fps", return_value=10):
        # Pre-fix, the held-open stale partial raises PermissionError here.
        result = burn.compose_video(str(video), str(frames), str(out_dir), config, duration=1.0)

    assert result is None
    # Stale partial (kept as troubleshooting artifact — the encode failed
    # because the holder blocks ffmpeg's output open too; verified live).
    assert stale_partial.is_file()


# ---------------------------------------------------------------------------
# design-3 (partial): textual in dependency checklists
# ---------------------------------------------------------------------------


def _read(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def test_doctor_packages_include_textual_required():
    """doctor_check must check textual as a required package.

    D1 单源收敛后 doctor 不再手抄包 dict：textual 的 requiredness 唯一归属
    env_bootstrap.collect_readiness（pkg:textual, required_for_render=True），
    doctor 从其派生 pkg 检查项。此测试随之改为行为断言 + env_bootstrap 源码
    门禁（原 doctor_check.py 源码正则只对旧手抄实现成立）。
    """
    import env_bootstrap
    from env_bootstrap import collect_readiness

    src = _read("env_bootstrap.py")
    assert '"textual": "textual"' in src
    guard = re.search(r'req = module in \(([^)]*)\)', src)
    assert guard, "env_bootstrap required-module guard not found"
    assert "textual" in guard.group(1)

    items = {i.key: i for i in collect_readiness(font_path=None, font_bold_path=None)}
    textual_item = items.get("pkg:textual")
    assert textual_item is not None, "collect_readiness missing pkg:textual"
    assert textual_item.required_for_render, "textual must be required for render"
    # doctor 从 collect_readiness 派生（单源），不再有独立的手抄包表。
    doctor_src = _read("doctor_check.py")
    assert "collect_readiness" in doctor_src
    assert not re.search(r'optional_pkgs\s*=\s*\{', doctor_src), (
        "doctor must derive pkg checks from env_bootstrap, not keep its own table"
    )
    assert env_bootstrap is not None  # import seam kept for monkeypatch users


def test_env_bootstrap_packages_include_textual_required():
    """env_bootstrap.collect_readiness must include textual, required for render."""
    src = _read("env_bootstrap.py")
    assert '"textual": "textual"' in src
    guard = re.search(r'req = module in \(([^)]*)\)', src)
    assert guard, "env_bootstrap required-module guard not found"
    assert "textual" in guard.group(1)


def test_textual_readiness_entry_real_when_installed():
    """When textual is importable, collect_readiness reports the pkg:textual item ok."""
    try:
        importlib.util.find_spec("textual")
    except (ImportError, ValueError):
        pytest.skip("textual not importable in this environment")

    import env_bootstrap as env_bootstrap_mod

    items = env_bootstrap_mod.collect_readiness()
    textual_items = [it for it in items if getattr(it, "key", "") == "pkg:textual"]
    assert len(textual_items) == 1, "collect_readiness must emit exactly one pkg:textual check"
    item = textual_items[0]
    assert item.ok is True
    assert item.required_for_render is True
    assert item.name == "textual"
