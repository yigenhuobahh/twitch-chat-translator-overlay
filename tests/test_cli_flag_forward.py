#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Contract: pipeline append_* helpers forward shared burn flags."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _has(cmd: list, flag: str, value: str | None = None) -> bool:
    if flag not in cmd:
        return False
    if value is None:
        return True
    i = cmd.index(flag)
    return i + 1 < len(cmd) and str(cmd[i + 1]) == value


def _representative_namespace(**overrides) -> SimpleNamespace:
    """Namespace covering every SHARED_FORWARD_FLAGS attr with non-default-ish values."""
    base = dict(
        # fps
        fps=15,
        output_fps=60,
        # layout
        max_visible=8,
        msg_lifetime=12.0,
        max_message_lines=3,
        min_visible_seconds=1.5,
        arrival_interval=0.2,
        stack_mode="float",
        x_ratio=0.1,
        y_ratio=0.2,
        width_ratio=0.3,
        height_ratio=0.4,
        font_size_ratio=0.05,
        emote_height=28,
        lazy_message_images=True,
        message_image_cache_size=64,
        # perf / encode
        encoder="x264",
        video_preset="fast",
        crf=20,
        video_bitrate="8M",
        maxrate="12M",
        bufsize="16M",
        audio_codec="aac",
        audio_bitrate="160k",
        overlay_codec="png",
        webm_crf=28,
        webm_cpu_used=5,
        no_reuse_static_frames=True,
        no_skip_blank_frames=False,
        blank_hold_seconds=0.75,
        # import-related
        strict_import=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_shared_forward_constants_document_burn_only():
    import render_cn_chat as pipe

    expected_burn_only = {
        "export-translation",
        "import-translation",
        "force-export",
        "strict-import",
        "job-dir",
        "no-job-dir",
        "out-dir",
    }
    assert set(pipe.BURN_ONLY_FLAGS) == expected_burn_only

    # Shared flags must not collide with burn-only (except strict-import is
    # documented as burn-only path flag with a thin pipeline forward helper).
    shared_names = {f.lstrip("-") for f in pipe.SHARED_FORWARD_FLAGS}
    assert "export-translation" not in shared_names
    assert "import-translation" not in shared_names
    assert "job-dir" not in shared_names
    assert "no-job-dir" not in shared_names
    assert "out-dir" not in shared_names


def test_shared_forward_flags_all_appear_from_representative_namespace():
    import render_cn_chat as pipe

    # Enable all store_true-style flags so every SHARED_FORWARD_FLAGS entry appears.
    args = _representative_namespace(
        no_reuse_static_frames=True,
        no_skip_blank_frames=True,
        lazy_message_images=True,
    )
    cmd: list = []
    pipe.append_shared_burn_args(cmd, args)

    missing = [f for f in pipe.SHARED_FORWARD_FLAGS if f not in cmd]
    assert not missing, f"shared flags missing from cmd: {missing}\ncmd={cmd}"

    # spot-check values
    assert _has(cmd, "--fps", "15")
    assert _has(cmd, "--output-fps", "60")
    assert _has(cmd, "--stack-mode", "float")
    assert _has(cmd, "--max-visible", "8")
    assert _has(cmd, "--arrival-interval", "0.2")
    assert _has(cmd, "--emote-height", "28")
    assert "--lazy-message-images" in cmd
    assert _has(cmd, "--message-image-cache-size", "64")
    assert _has(cmd, "--encoder", "x264")
    assert _has(cmd, "--video-preset", "fast")
    assert _has(cmd, "--video-bitrate", "8M")
    assert _has(cmd, "--maxrate", "12M")
    assert _has(cmd, "--bufsize", "16M")
    assert _has(cmd, "--overlay-codec", "png")
    assert _has(cmd, "--crf", "20")
    assert "--no-reuse-static-frames" in cmd
    assert "--no-skip-blank-frames" in cmd
    assert _has(cmd, "--blank-hold-seconds", "0.75")


def test_append_layout_and_perf_forward_key_flags():
    """Backward-compatible coverage for the original helper contract."""
    import render_cn_chat as pipe

    args = _representative_namespace(
        video_bitrate=None,
        maxrate=None,
        bufsize=None,
    )
    cmd: list = []
    pipe.append_fps_args(cmd, args)
    pipe.append_layout_burn_args(cmd, args)
    pipe.append_perf_encode_args(cmd, args)

    assert _has(cmd, "--fps", "15")
    assert _has(cmd, "--output-fps", "60")
    assert _has(cmd, "--stack-mode", "float")
    assert _has(cmd, "--max-visible", "8")
    assert _has(cmd, "--arrival-interval", "0.2")
    assert _has(cmd, "--emote-height", "28")
    assert "--lazy-message-images" in cmd
    assert _has(cmd, "--message-image-cache-size", "64")
    assert _has(cmd, "--encoder", "x264")
    assert _has(cmd, "--overlay-codec", "png")
    assert _has(cmd, "--crf", "20")
    assert "--no-reuse-static-frames" in cmd
    assert "--no-skip-blank-frames" not in cmd
    assert _has(cmd, "--blank-hold-seconds", "0.75")
    # opt_truthy: None/empty not forwarded
    assert "--video-bitrate" not in cmd
    assert "--maxrate" not in cmd
    assert "--bufsize" not in cmd


def test_layout_skips_missing_and_empty_attrs():
    import render_cn_chat as pipe

    args = SimpleNamespace(
        max_visible=None,
        msg_lifetime="",
        # only stack_mode present
        stack_mode="lanes",
        lazy_message_images=False,
    )
    cmd: list = []
    pipe.append_layout_burn_args(cmd, args)
    assert _has(cmd, "--stack-mode", "lanes")
    assert "--max-visible" not in cmd
    assert "--msg-lifetime" not in cmd
    assert "--lazy-message-images" not in cmd
    assert "--message-image-cache-size" not in cmd


def test_output_fps_omitted_when_none():
    import render_cn_chat as pipe

    args = SimpleNamespace(fps=12, output_fps=None)
    cmd: list = []
    pipe.append_fps_args(cmd, args)
    assert _has(cmd, "--fps", "12")
    assert "--output-fps" not in cmd


def test_append_strict_import_arg():
    import render_cn_chat as pipe

    cmd: list = []
    pipe.append_strict_import_arg(cmd, SimpleNamespace(strict_import=False))
    assert "--strict-import" not in cmd

    cmd2: list = []
    pipe.append_strict_import_arg(cmd2, SimpleNamespace(strict_import=True))
    assert cmd2 == ["--strict-import"]

    # missing attr is safe (no hard fail)
    cmd3: list = []
    pipe.append_strict_import_arg(cmd3, SimpleNamespace())
    assert "--strict-import" not in cmd3


def test_export_translation_forwards_offset_and_force():
    import render_cn_chat as pipe

    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = list(cmd)

    # empty json path → will call burn
    pipe.run = fake_run  # type: ignore
    tj = Path("no_such_yet.json")
    pipe._export_translation_json(
        burn=Path("burn.py"),
        video=Path("v.mp4"),
        chat_html=Path("c.html"),
        trans_json=tj,
        force=True,
        offset=33.5,
    )
    cmd = seen["cmd"]
    assert "--export-translation" in cmd
    assert "--force-export" in cmd
    assert "--offset" in cmd
    assert cmd[cmd.index("--offset") + 1] == "33.5"
    # export path must not invent strict-import
    assert "--strict-import" not in cmd


def test_render_preview_clip_forwards_strict_import():
    import render_cn_chat as pipe

    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = list(cmd)

    pipe.run = fake_run  # type: ignore
    # avoid publishing / glob side effects by letting run succeed and
    # preview_dir empty → returns None after run; we only care about cmd.
    args = _representative_namespace(strict_import=True, offset=1.25, preview_dense=True)
    # geometry required by builder
    args.x = 10
    args.y = 20
    args.width = 100
    args.height = 200
    args.font_size = 16
    args.font_path = "auto"
    args.font_bold_path = "auto"
    args.bg_alpha = 200

    out = pipe._render_preview_clip(
        video=Path("v.mp4"),
        chat_html=Path("c.html"),
        trans_json=Path("t.json"),
        args=args,
        workdir=None,
        seconds=7.0,
        burn=Path("burn.py"),
    )
    assert out is None  # no real file produced
    cmd = seen["cmd"]
    assert "--import-translation" in cmd
    assert "--strict-import" in cmd
    assert _has(cmd, "--preview-clip", "7.0")
    assert "--preview-dense" in cmd
    assert _has(cmd, "--offset", "1.25")
    # shared tables still applied
    assert _has(cmd, "--encoder", "x264")
    assert "--lazy-message-images" in cmd


def test_pipeline_parser_exposes_strict_import():
    import render_cn_chat as pipe

    # build_parser is main()-local; re-parse via argparse by invoking the
    # same defaults through a tiny harness: exec the add_argument region is
    # heavy, so just check the flag is registered by importing and running
    # parse on a stub. We reconstruct via the module's documented default map
    # and the source contract.
    src = Path(pipe.__file__).read_text(encoding="utf-8")
    assert '"--strict-import"' in src or "'--strict-import'" in src
    assert "append_strict_import_arg" in src
    assert "strict_import" in pipe.PIPELINE_CLI_DEFAULTS
    assert pipe.PIPELINE_CLI_DEFAULTS["strict_import"] is False


def test_forward_specs_cover_shared_flags_without_duplicates():
    import render_cn_chat as pipe

    pairs = list(pipe.FPS_FORWARD_SPECS) + list(pipe.LAYOUT_FORWARD_SPECS) + list(pipe.PERF_FORWARD_SPECS)
    flags = [flag for _a, flag, _k in pairs]
    assert len(flags) == len(set(flags)), f"duplicate flags in specs: {flags}"
    # every table flag is listed in SHARED_FORWARD_FLAGS
    for flag in flags:
        assert flag in pipe.SHARED_FORWARD_FLAGS, flag
    assert "--message-image-cache-size" in pipe.SHARED_FORWARD_FLAGS


def test_unknown_kind_raises():
    import render_cn_chat as pipe

    try:
        pipe._append_flag_specs([], SimpleNamespace(x=1), (("x", "--x", "nope"),))
        raised = False
    except ValueError:
        raised = True
    assert raised
