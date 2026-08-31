#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""--download 下载流程 —— 从 render_cn_chat.py 原样搬出（搬运而非重写）。

只做"取素材"这一件事，且只在早退分支被调用。与编排层的两个联动点经显式
参数注入（由 render_cn_chat 的包装传入，保持测试按模块属性 monkeypatch
emit_task_event / _post_download_next_steps 的历史语义）：
  emit        —— 任务事件（stage_started/completed/failed）
  next_steps  —— 下载完成后的"下一步"处理
"""

from __future__ import annotations

from pathlib import Path
import sys

from pipeline_runner import active_runner
from process_util import is_dangerous_publish_path


def _parse_cli_segments(raw_segments) -> list[tuple[str, str]]:
    """Parse --segment BEGIN-END values into (begin, end) pairs."""
    from twitch_download import TwitchDownloadError, parse_segment_line

    pairs: list[tuple[str, str]] = []
    for raw in raw_segments or []:
        text = str(raw or "").strip()
        if not text:
            continue
        # Accept "begin-end", "begin end", or "begin,end"
        try:
            seg = parse_segment_line(text)
        except TwitchDownloadError:
            # Rare: hyphen-only form failed earlier heuristics — try space on first '-'
            if "-" in text and " " not in text and "," not in text:
                left, _, right = text.partition("-")
                seg = parse_segment_line(f"{left} {right}")
            else:
                raise
        if seg is None:
            raise TwitchDownloadError(f"无效 --segment: {raw!r}")
        pairs.append((seg.begin, seg.end))
    return pairs


def run_download_flow(args, *, runner=None, emit, next_steps) -> int:
    """CLI entry for --download: fetch VOD/clip + HTML via TwitchDownloaderCLI."""
    if getattr(args, "dry_run", False):
        # --download 在 _main 里先于 DRY_RUN 全局赋值执行，必须在此写侧拦截，
        # 否则 --dry-run 也会真实请求 Twitch 并写出媒体文件。
        print("[dry-run] 已跳过下载：--dry-run 不会真实下载视频/聊天 HTML。")
        return 0
    from twitch_download import TwitchDownloadError, download_assets, download_assets_multi

    out_dir = Path(args.download_dir).resolve() if getattr(args, "download_dir", None) else None
    if out_dir is not None and (
        is_dangerous_publish_path(out_dir) or is_dangerous_publish_path(out_dir.parent)
    ):
        print(f"错误: --download-dir 不能是系统目录: {out_dir}", file=sys.stderr)
        return 2

    raw_segments = list(getattr(args, "segment", None) or [])
    # Parse --cut START-END into (start_s, end_s) pairs
    cut_ranges: list[tuple[float, float]] = []
    for raw_cut in (getattr(args, "cut", None) or []):
        text = str(raw_cut or "").strip()
        if not text:
            continue
        # Accept "start-end", "start end", or "start,end"
        try:
            from twitch_download import parse_segment_line
            seg = parse_segment_line(text)
            if seg is None:
                raise TwitchDownloadError(f"无效 --cut: {raw_cut!r}")
            cut_ranges.append((seg.begin_s, seg.end_s))
        except TwitchDownloadError:
            # Try hyphen split for time-like values
            if "-" in text and " " not in text and "," not in text:
                left, _, right = text.partition("-")
                seg = parse_segment_line(f"{left} {right}")
                if seg is not None:
                    cut_ranges.append((seg.begin_s, seg.end_s))
                    continue
            raise

    emit("stage_started", stage="download", completed=0, total=1)
    try:
        multi = _parse_cli_segments(raw_segments) if raw_segments else []
        if multi and (getattr(args, "begin", None) or getattr(args, "end", None)):
            print(
                "警告: 已指定 --segment，忽略 --begin/--end",
                file=sys.stderr,
            )
        if cut_ranges and not multi:
            print(
                "警告: --cut 仅在 --segment 多段下载时生效，已忽略",
                file=sys.stderr,
            )
            cut_ranges = []
        if multi:
            result = download_assets_multi(
                str(args.download),
                multi,
                out_dir=out_dir,
                kind=str(getattr(args, "kind", "auto") or "auto"),
                quality=getattr(args, "quality", None) or None,
                oauth=getattr(args, "oauth", None),
                remove_ranges=cut_ranges or None,
                output_fps=getattr(args, "download_output_fps", None),
                encoder=str(getattr(args, "download_encoder", "auto") or "auto"),
                trim_mode=str(getattr(args, "download_trim_mode", "Safe") or "Safe"),
                media_check=str(getattr(args, "media_check", "fast") or "fast"),
                media_repair=str(getattr(args, "media_repair", "audio") or "audio"),
            )
        else:
            result = download_assets(
                str(args.download),
                out_dir=out_dir,
                kind=str(getattr(args, "kind", "auto") or "auto"),
                quality=getattr(args, "quality", None) or None,
                begin=getattr(args, "begin", None),
                end=getattr(args, "end", None),
                oauth=getattr(args, "oauth", None),
                trim_mode=str(getattr(args, "download_trim_mode", "Safe") or "Safe"),
                media_check=str(getattr(args, "media_check", "fast") or "fast"),
                media_repair=str(getattr(args, "media_repair", "audio") or "audio"),
            )
    except TwitchDownloadError as e:
        emit("stage_failed", stage="download", completed=0, total=1)
        print(f"错误: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        emit("stage_failed", stage="download", completed=0, total=1)
        print(f"错误: 下载失败: {e}", file=sys.stderr)
        return 1
    task_runner = runner or active_runner()
    if task_runner is not None:
        task_runner.configure(
            mode="download",
            artifacts=[("video", result.video_path), ("chat_html", result.chat_html_path)],
        )
    emit("stage_completed", stage="download", completed=1, total=1)
    return next_steps(
        result.video_path,
        result.chat_html_path,
        download_only=bool(getattr(args, "download_only", False)),
        yes=bool(getattr(args, "yes", False)),
    )
