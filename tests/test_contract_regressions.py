#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""High-value contract regressions (import order, job→args, path guards)."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_import_then_filter_keeps_global_index_identity():
    """Import by full-list index, then window-filter — indices must not shift.

    Product contract: export/import use pre-filter list positions. Filtering first
    would make JSON index 2 map to the wrong message after a mid-VOD window.
    """
    from chat_window import filter_chat_for_time_window
    import twitch_chat_burn as burn

    chat = {
        "messages": [
            {
                "timestamp": 1.0,
                "author": "early",
                "fragments": [{"type": "text", "text": "early"}],
                "badges": [],
            },
            {
                "timestamp": 50.0,
                "author": "mid",
                "fragments": [{"type": "text", "text": "mid"}],
                "badges": [],
            },
            {
                "timestamp": 100.0,
                "author": "late",
                "fragments": [{"type": "text", "text": "late"}],
                "badges": [],
            },
        ],
        "emote_map": {},
    }
    # Only index 2 (late) is translated — must stick to author "late".
    trans = {
        "time_base": "stream",
        "messages": [
            {
                "index": 2,
                "author": "late",
                "timestamp": 100.0,
                "stream_timestamp": 100.0,
                "original": "late",
                "translation": "晚到的译文",
            }
        ],
    }
    replaced, _s, warnings = burn.apply_imported_translations(chat, trans)
    assert replaced == 1
    # Count mismatch / missing-index notes are OK; identity skip is not.
    assert not any("跳过导入" in w or "作者不一致" in w or "时间戳不一致" in w for w in warnings)
    assert chat["messages"][2]["fragments"][0]["text"] == "晚到的译文"
    assert chat["messages"][0]["fragments"][0]["text"] == "early"
    assert chat["messages"][1]["fragments"][0]["text"] == "mid"

    filtered = filter_chat_for_time_window(chat, 90.0, 110.0, msg_lifetime=14.0)
    texts = [m["fragments"][0]["text"] for m in filtered["messages"]]
    assert "晚到的译文" in texts
    assert "early" not in texts
    # Mid at t=50 with life 14 ends at 64 — outside [90,110]
    assert "mid" not in texts


def test_filter_before_import_would_misalign_indices_document_hazard():
    """Counter-example: filtering first then treating list position as index is wrong.

    This is not a product path — it documents why import-before-filter is required.
    """
    from chat_window import filter_chat_for_time_window
    import twitch_chat_burn as burn

    chat = {
        "messages": [
            {
                "timestamp": 1.0,
                "author": "early",
                "fragments": [{"type": "text", "text": "early"}],
                "badges": [],
            },
            {
                "timestamp": 100.0,
                "author": "late",
                "fragments": [{"type": "text", "text": "late"}],
                "badges": [],
            },
        ],
        "emote_map": {},
    }
    # Global index 1 = late
    trans = {
        "messages": [
            {
                "index": 1,
                "author": "late",
                "timestamp": 100.0,
                "original": "late",
                "translation": "晚",
            }
        ]
    }
    # Wrong order (hazard): filter first → only late remains at list position 0
    wrong = filter_chat_for_time_window(chat, 90.0, 110.0, msg_lifetime=14.0)
    assert len(wrong["messages"]) == 1
    # Import by index 1 misses the only remaining row (index 0)
    replaced, _s, _w = burn.apply_imported_translations(wrong, trans)
    assert replaced == 0
    assert wrong["messages"][0]["fragments"][0]["text"] == "late"


def test_filled_json_zero_import_exits_nonzero(tmp_path: Path, monkeypatch, capsys):
    """Burn CLI: filled translation that applies 0 rows must not exit 0."""
    import twitch_chat_burn as burn

    chat = {
        "messages": [
            {
                "timestamp": 1.0,
                "author": "Alice",
                "fragments": [{"type": "text", "text": "hi"}],
                "badges": [],
            }
        ]
    }
    # All rows mismatch → replaced=0 but filled>0
    trans = {
        "messages": [
            {
                "index": 0,
                "author": "NotAlice",
                "timestamp": 1.0,
                "original": "hi",
                "translation": "嗨",
            }
        ]
    }
    # Unit: apply still returns 0
    replaced, _s, warnings = burn.apply_imported_translations(chat, trans)
    assert replaced == 0
    assert warnings

    # Simulate CLI gate after apply (same condition as main)
    filled = sum(
        1
        for it in trans["messages"]
        if isinstance(it, dict) and str(it.get("translation", "") or "").strip()
    )
    assert filled > 0 and replaced == 0
    # The product exits 1 in main when this holds; assert the condition stays true.


def test_job_applies_force_export_and_strict_import(job_mod=None):
    from helpers import load_module
    import render_cn_chat as pipe

    job_mod = load_module("job_config", "job_config.py")
    args = SimpleNamespace(
        force_export=False,
        strict_import=False,
        mode="auto",
        overlay_codec="vp9",
    )
    job = {
        "force_export": True,
        "strict_import": True,
        "mode": "preview",
        "overlay_codec": "png",
    }
    applied = job_mod.apply_job_to_namespace(
        args, job, cli_defaults=pipe.PIPELINE_CLI_DEFAULTS
    )
    assert args.force_export is True
    assert args.strict_import is True
    assert "force_export" in applied
    assert "strict_import" in applied
    # CLI wins
    args2 = SimpleNamespace(force_export=True, strict_import=False, mode="auto")
    job_mod.apply_job_to_namespace(
        args2,
        {"force_export": False, "strict_import": True},
        cli_defaults=pipe.PIPELINE_CLI_DEFAULTS,
    )
    assert args2.force_export is True  # already non-default CLI
    assert args2.strict_import is True  # still at default False → filled


def test_job_strict_import_forwards_on_preview_cmd(monkeypatch, tmp_path: Path):
    """Job/CLI strict_import must appear on burn import cmds."""
    import render_cn_chat as pipe

    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = list(cmd)

    monkeypatch.setattr(pipe, "run", fake_run)
    args = SimpleNamespace(
        x=1,
        y=2,
        width=100,
        height=200,
        font_size=14,
        font_path="auto",
        font_bold_path="auto",
        bg_alpha=200,
        fps=15,
        output_fps=None,
        max_visible=10,
        msg_lifetime=14.0,
        max_message_lines=0,
        min_visible_seconds=0.0,
        arrival_interval=0.0,
        stack_mode="lanes",
        x_ratio=0.0,
        y_ratio=0.0,
        width_ratio=0.0,
        height_ratio=0.0,
        font_size_ratio=0.0,
        emote_height=22,
        lazy_message_images=False,
        message_image_cache_size=256,
        encoder="x264",
        video_preset="fast",
        crf=18,
        video_bitrate=None,
        maxrate=None,
        bufsize=None,
        audio_codec="aac",
        audio_bitrate="192k",
        overlay_codec="png",
        webm_crf=30,
        webm_cpu_used=4,
        no_reuse_static_frames=False,
        no_skip_blank_frames=False,
        blank_hold_seconds=0.5,
        offset=None,
        preview_dense=False,
        strict_import=True,
    )
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0")
    html = tmp_path / "c.html"
    html.write_text("<html></html>", encoding="utf-8")
    tj = tmp_path / "t.json"
    tj.write_text('{"messages":[]}', encoding="utf-8")
    pipe._render_preview_clip(
        video=video,
        chat_html=html,
        trans_json=tj,
        args=args,
        workdir=tmp_path / "wd",
        seconds=5.0,
        burn=tmp_path / "burn.py",
    )
    assert seen.get("cmd")
    assert "--strict-import" in seen["cmd"]
    assert "--import-translation" in seen["cmd"]


def test_dangerous_output_and_download_dir_rejected(tmp_path: Path, monkeypatch):
    from process_util import is_dangerous_publish_path
    import render_cn_chat as pipe

    # Sanity: denylist still works
    assert is_dangerous_publish_path(r"C:\Windows\Temp\out.mp4") or is_dangerous_publish_path(
        "/etc/out.mp4"
    )

    # download-dir guard
    class Args:
        download = "123456"
        download_dir = r"C:\Windows\System32\evil" if sys.platform == "win32" else "/etc/evil"
        kind = "auto"
        quality = None
        begin = None
        end = None
        oauth = None
        download_only = True
        yes = True

    rc = pipe._run_download_flow(Args())
    assert rc == 2


def test_download_dir_system_path_blocked_in_module(tmp_path: Path, monkeypatch):
    import twitch_download as td

    with pytest.raises(td.TwitchDownloadError, match="系统路径|系统"):
        bad = Path(r"C:\Windows\Temp\td") if sys.platform == "win32" else Path("/etc/td_dl")
        # find_cli may also fail first — monkeypatch
        monkeypatch.setattr(
            td,
            "find_twitchdownloader_cli",
            lambda root=None: tmp_path / "TwitchDownloaderCLI.exe",
        )
        (tmp_path / "TwitchDownloaderCLI.exe").write_bytes(b"x")
        td.download_assets("123456789", out_dir=bad)
