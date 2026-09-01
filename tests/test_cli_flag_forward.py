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
    import cli_spec
    import render_cn_chat as pipe

    # argparse 定义已单源迁至 scripts/cli_spec.py（render_cn_chat re-export）；
    # 源码契约断言改为读 cli_spec，并补一个真实解析检查。
    spec_src = (SCRIPTS / "cli_spec.py").read_text(encoding="utf-8")
    assert '"--strict-import"' in spec_src or "'--strict-import'" in spec_src
    pipe_src = Path(pipe.__file__).read_text(encoding="utf-8")
    assert "append_strict_import_arg" in pipe_src
    assert cli_spec.build_arg_parser().parse_args(["--strict-import"]).strict_import is True
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


# ---------------------------------------------------------------------------
# PIPE-O6: job YAML 的 source_media_check 必须生效
# ---------------------------------------------------------------------------


def test_pipeline_cli_defaults_include_source_media_check():
    import render_cn_chat as pipe

    # 缺了这条，job 值会落进 apply_job_to_namespace 的 unknown-default 分支被静默丢弃
    assert "source_media_check" in pipe.PIPELINE_CLI_DEFAULTS
    assert pipe.PIPELINE_CLI_DEFAULTS["source_media_check"] == "fast"


def test_job_field_aliases_accept_source_media_check():
    import job_config as jc

    assert jc._norm_key("source_media_check") == "source_media_check"
    assert jc._norm_key("source-media-check") == "source_media_check"


def test_job_source_media_check_applies_when_at_default():
    import job_config as jc
    import render_cn_chat as pipe

    args = SimpleNamespace(source_media_check="fast")
    applied = jc.apply_job_to_namespace(
        args, {"source_media_check": "decode"}, cli_defaults=pipe.PIPELINE_CLI_DEFAULTS
    )
    assert "source_media_check" in applied
    assert args.source_media_check == "decode"


def test_job_source_media_check_respects_explicit_cli():
    import job_config as jc
    import render_cn_chat as pipe

    args = SimpleNamespace(source_media_check="off")  # 非 argparse 默认值 → 视为显式
    applied = jc.apply_job_to_namespace(
        args, {"source_media_check": "decode"}, cli_defaults=pipe.PIPELINE_CLI_DEFAULTS
    )
    assert "source_media_check" not in applied
    assert args.source_media_check == "off"


def test_job_source_media_check_forwarded_to_media_gate(monkeypatch, tmp_path, capsys):
    """job YAML 设 source_media_check: decode → 管线媒体门禁收到 decode。"""
    import render_cn_chat as pipe

    job = tmp_path / "style.yaml"
    job.write_text("source_media_check: decode\n", encoding="utf-8")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00\x00")
    html = tmp_path / "chat.html"
    html.write_text("<html></html>", encoding="utf-8")

    seen: dict = {}
    monkeypatch.setattr(
        pipe, "validate_source_media", lambda v, *, mode, dry_run=False: seen.update(mode=mode)
    )
    monkeypatch.setattr(pipe, "resolve_font_paths", lambda *a, **k: ("font.ttf", "font-bold.ttf"))
    monkeypatch.setattr(pipe, "run", lambda cmd, *a, **k: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "render_cn_chat.py",
            str(video),
            str(html),
            "--render-original",
            "--job",
            str(job),
        ],
    )

    assert pipe.main() is None
    assert seen.get("mode") == "decode"


# ---------------------------------------------------------------------------
# W1-A1: CLI 单源化——scripts/cli_spec.py 与 PIPELINE_CLI_DEFAULTS 同步契约
# ---------------------------------------------------------------------------


def _parser_action_defaults(parser) -> dict:
    defaults = {}
    for action in parser._actions:
        if action.dest == "help":  # argparse 自带的 -h/--help
            continue
        defaults[action.dest] = action.default
    return defaults


def test_pipeline_cli_defaults_match_every_parser_action_default():
    """PIPELINE_CLI_DEFAULTS 必须与 build_arg_parser 的逐项 default 完全一致。

    字典是 job/layout/render preset "CLI wins" 判断的唯一来源；历史上曾因
    手工复刻漏同步而静默丢过 job YAML 字段（如 source_media_check）。
    本测试同时拦住两侧漂移：字典缺项 / 多项 / 值不一致都会失败。
    """
    import cli_spec

    parser_defaults = _parser_action_defaults(cli_spec.build_arg_parser())
    defaults = cli_spec.PIPELINE_CLI_DEFAULTS

    missing = sorted(set(parser_defaults) - set(defaults))
    extra = sorted(set(defaults) - set(parser_defaults))
    mismatched = sorted(
        key
        for key in set(parser_defaults) & set(defaults)
        if parser_defaults[key] != defaults[key]
    )
    problems = []
    if missing:
        problems.append(f"PIPELINE_CLI_DEFAULTS 缺少 argparse 选项默认值: {missing}")
    if extra:
        problems.append(f"PIPELINE_CLI_DEFAULTS 含非 argparse 选项的键: {extra}")
    for key in mismatched:
        problems.append(
            f"{key}: parser default={parser_defaults[key]!r} != dict={defaults[key]!r}"
        )
    assert not problems, "CLI 默认值漂移:\n" + "\n".join(problems)


def test_render_cn_chat_reexports_cli_spec_symbols():
    import cli_spec
    import render_cn_chat as pipe

    # `from render_cn_chat import PIPELINE_CLI_DEFAULTS` 等旧导入路径保持可用。
    assert pipe.PIPELINE_CLI_DEFAULTS is cli_spec.PIPELINE_CLI_DEFAULTS
    assert pipe.build_arg_parser is cli_spec.build_arg_parser
    assert pipe._cli_flag_present is cli_spec._cli_flag_present


def test_cli_flag_present_matches_exact_and_value_forms(monkeypatch):
    import cli_spec

    monkeypatch.setattr(sys, "argv", ["prog", "--overlay-codec", "png"])
    assert cli_spec._cli_flag_present("--overlay-codec") is True
    assert cli_spec._cli_flag_present("--offset") is False
    # --flag=value 形式也算显式传入
    monkeypatch.setattr(sys, "argv", ["prog", "--offset=1.5"])
    assert cli_spec._cli_flag_present("--offset") is True
    monkeypatch.setattr(sys, "argv", ["prog", "video.mp4", "chat.html"])
    assert cli_spec._cli_flag_present("--offset") is False


def test_build_arg_parser_set_defaults_from_single_source():
    """build_arg_parser 末尾 set_defaults(**PIPELINE_CLI_DEFAULTS)：空 argv 解析
    得到的 namespace 与字典完全一致（字典即默认值的单一权威来源）。"""
    import cli_spec

    parsed = vars(cli_spec.build_arg_parser().parse_args([]))
    assert parsed == cli_spec.PIPELINE_CLI_DEFAULTS
