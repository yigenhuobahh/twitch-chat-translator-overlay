#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Video concat + segmented chat HTML merge, split from twitch_download (W3-A1).

Every function body below is copied verbatim from twitch_download.py.
Collaborators that remain in twitch_download (ffprobe probing, run_tracked,
chat HTML validation) are reached through the lazy forwarders so tests that
``monkeypatch.setattr(twitch_download, ...)`` keep working when these helpers
are driven via ``twitch_download.concat_videos`` / ``twitch_download.merge_chat_html``.
"""

from __future__ import annotations

from fractions import Fraction
import math
from pathlib import Path
import re
from typing import TYPE_CHECKING

from chat_parser import _MAX_HTML_BYTES
from common_utils import require_executable
from cut_timeline import CutTimeline, CutTimelineError
from twitch_download_types import TwitchDownloadError

if TYPE_CHECKING:
    # Annotation-only: SegmentDownload stays in twitch_download; importing it
    # here (not at runtime) avoids a module import cycle with the lazy
    # forwarders below.
    from twitch_download import SegmentDownload


def probe_media_duration(path: Path) -> float:
    """Lazy forwarder: keeps twitch_download.probe_media_duration patches effective."""
    from twitch_download import probe_media_duration as _probe_media_duration

    return _probe_media_duration(path)


def get_stream_start_time(path: Path, stream_selector: str) -> float:
    """Lazy forwarder: keeps twitch_download.get_stream_start_time patches effective."""
    from twitch_download import get_stream_start_time as _get_stream_start_time

    return _get_stream_start_time(path, stream_selector)


def run_tracked(*args, **kwargs):
    """Lazy forwarder: keeps twitch_download.run_tracked patches effective."""
    from twitch_download import run_tracked as _run_tracked

    return _run_tracked(*args, **kwargs)


def validate_chat_html(path: Path) -> None:
    """Lazy forwarder: keeps twitch_download.validate_chat_html patches effective."""
    from twitch_download import validate_chat_html as _validate_chat_html

    _validate_chat_html(path)


# TD time link: [<a href="...?t=0h12m26s">0:12:26</a>] — display lives inside <a>.
_T_QUERY_RE = re.compile(r"([?&]t=)(\d+h\d+m\d+s)", re.IGNORECASE)
_TIME_LINK_DISPLAY_RE = re.compile(
    r"(<a\b[^>]*[?&]t=\d+h\d+m\d+s[^>]*>)([^<]+)(</a>)",
    re.IGNORECASE,
)
_COMMENT_ROOT_SPLIT_RE = re.compile(
    r'(?=<pre\b[^>]*\bclass\s*=\s*["\'][^"\']*\bcomment-root\b)',
    re.IGNORECASE,
)
_EMOTE_CLASS_RE = re.compile(r"\.([A-Za-z0-9_-]+)")
_EMOTE_PREFIXES = ("first-", "second-", "third-")


def format_td_t_seconds(seconds: float) -> tuple[str, str]:
    """Return (query_t, display) for integer seconds: ('0h12m26s', '0:12:26')."""
    value = float(seconds)
    if not math.isfinite(value):
        raise TwitchDownloadError(f"时间必须是有限数值: {seconds!r}")
    total = int(max(0, round(value)))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}h{m}m{s}s", f"{h}:{m:02d}:{s:02d}"


def _ffmpeg_concat_list_line(path: Path) -> str:
    """Escape path for ffmpeg concat demuxer file directive."""
    # concat demuxer: file 'path' with ' → '\''
    p = str(path.resolve()).replace("\\", "/")
    p = p.replace("'", r"'\''")
    return f"file '{p}'"


def _fps_filter_expr(output_fps: float | str) -> str:
    """Render output_fps for the ffmpeg fps filter (fps=fps=<expr> form).

    Fractional strings ("30000/1001") are normalized via Fraction to an exact
    num/den expression (AV_OPT_TYPE_VIDEO_RATE accepts num/den); floats keep
    the legacy ``%.6f`` rendering byte-for-byte (30000/1001 as a float ≈
    29.97002997 must not change behavior).
    """
    if isinstance(output_fps, str):
        value = Fraction(output_fps.strip())
        if value.denominator == 1:
            return f"fps=fps={value.numerator}"
        return f"fps=fps={value.numerator}/{value.denominator}"
    return f"fps=fps={float(output_fps):.6f}"


def concat_videos(
    paths: list[Path],
    out: Path,
    *,
    list_path: Path | None = None,
    remove_ranges: list[tuple[float, float]] | None = None,
    cut_timeline: CutTimeline | None = None,
    output_fps: float | str | None = None,
    encoder: str = "auto",
) -> str:
    """Concat N videos → out. Returns 'copy' or 'reencode'.

    Uses filter_complex concat (not concat demuxer) to normalize per-segment
    timestamps. The concat demuxer with stream copy does not handle non-zero
    start_time correctly: TwitchDownloader Exact crop produces video start=1.0 /
    audio start=0.0, and concatenating such segments yields DTS non-monotonicity
    and cumulative A/V desync. filter_complex concat resets each input's PTS to
    0 before joining, which is the correct behavior. ``remove_ranges`` are
    continuous merged-timeline ranges in seconds; cuts are applied while
    decoding the source segments, so this never re-encodes an assembled output.
    ``output_fps`` optionally forces CFR without disabling B-frames: a float
    (e.g. 60) or an exact fractional expression string ("30000/1001").
    ``encoder`` selects the video encoder: auto (default) detects hardware
    encoders (QSV/NVENC/AMF) with libx264 fallback; or explicitly specify
    x264/nvenc/qsv/amf.
    """
    if not paths:
        raise TwitchDownloadError("没有可拼接的视频段")
    if len(paths) == 1 and not remove_ranges and cut_timeline is None:
        import shutil as _shutil

        out.parent.mkdir(parents=True, exist_ok=True)
        if paths[0].resolve() != out.resolve():
            _shutil.copy2(paths[0], out)
        return "copy"

    # Still write concat_list.txt for debugging / manual use
    list_file = list_path or (out.parent / "concat_list.txt")
    list_file.write_text(
        "\n".join(_ffmpeg_concat_list_line(p) for p in paths) + "\n",
        encoding="utf-8",
    )

    def _run_ffmpeg(cmd: list[str], label: str) -> int:
        print(f"\n$ {' '.join(cmd)}", flush=True)
        try:
            completed = run_tracked(cmd, stdout=None, stderr=None, text=False, check=False)
        except FileNotFoundError as e:
            raise TwitchDownloadError(
                f"无法启动 ffmpeg（{label}）: {e}\n  请安装 FFmpeg 并加入 PATH"
            ) from e
        return int(completed.returncode)

    # Primary: filter_complex concat — normalizes timestamps per input
    print("-- 拼接视频 (filter_complex concat, 时间戳归零) ...", flush=True)
    cmd: list[str] = [require_executable("ffmpeg"), "-hide_banner", "-y"]
    for p in paths:
        cmd.extend(["-i", str(p)])
    # TwitchDownloader Exact trim writes video at +1s and audio at 0. Before
    # concatenating, freeze the first decoded video frame for that lead-in and
    # trim it to the segment's audio/container duration. Each segment then has
    # a common zero-based timeline without dropping a second from its tail.
    # Probe failure must abort loudly: recording 0.0 used to shrink that
    # segment's trim to 1ms and surface later as a misleading "裁切时间轴…
    # 总时长不一致" error (or silently drop the segment for direct callers).
    durations: list[float] = [probe_media_duration(p) for p in paths]
    try:
        timeline = cut_timeline or CutTimeline.from_ranges(remove_ranges, sum(durations))
    except CutTimelineError as exc:
        raise TwitchDownloadError(str(exc)) from exc
    if abs(timeline.original_duration - sum(durations)) > 1e-6:
        raise TwitchDownloadError("裁切时间轴与待拼接视频总时长不一致")
    cut_ranges = timeline.cuts
    if len(paths) == 1 and not cut_ranges:
        import shutil as _shutil

        out.parent.mkdir(parents=True, exist_ok=True)
        if paths[0].resolve() != out.resolve():
            _shutil.copy2(paths[0], out)
        return "copy"
    chains: list[str] = []
    concat_inputs: list[str] = []
    for i, duration in enumerate(durations):
        video_start = get_stream_start_time(paths[i], "v:0") or 0.0
        audio_start = get_stream_start_time(paths[i], "a:0") or 0.0
        lead_in = max(0.0, float(video_start) - float(audio_start))
        trim = max(0.001, float(duration or 0.0))
        segment_start = sum(durations[:i])
        keep_ranges = timeline.local_keep_ranges(segment_start, trim)
        if not keep_ranges:
            continue
        v_base = f"[{i}:v:0]setpts=PTS-STARTPTS"
        if lead_in > 0.001:
            v_base += f",tpad=start_duration={lead_in:.6f}:start_mode=clone"
        for part, (keep_start, keep_end) in enumerate(keep_ranges):
            v_label = f"v{i}_{part}"
            a_label = f"a{i}_{part}"
            chains.append(
                f"{v_base},trim=start={keep_start:.6f}:end={keep_end:.6f},setpts=PTS-STARTPTS[{v_label}]"
            )
            chains.append(
                f"[{i}:a:0]asetpts=PTS-STARTPTS,atrim=start={keep_start:.6f}:end={keep_end:.6f},"
                f"asetpts=PTS-STARTPTS[{a_label}]"
            )
            concat_inputs.append(f"[{v_label}][{a_label}]")
    if not concat_inputs:
        raise TwitchDownloadError("裁切范围移除了全部视频内容")
    concat_count = len(concat_inputs)
    fc = ";".join(chains) + ";" + "".join(concat_inputs) + f"concat=n={concat_count}:v=1:a=1[v][a]"
    if output_fps:
        fc += f";[v]{_fps_filter_expr(output_fps)}[v_cfr]"
    # Resolve encoder via encode_options (auto-detect hardware vs software)
    from encode_options import build_video_encode_args, resolve_encode_options

    # Pass video_preset=None so resolve_encode_options applies its per-family
    # defaults (qsv -> "medium", amf -> "balanced", nvenc -> "p4", x264 ->
    # "fast"), which are the only values the concrete encoders accept.
    # Hard-coding "medium" here leaked into the AMF branch as an illegal
    # `-quality medium` when auto resolved to amf.
    enc_opts = resolve_encode_options(
        encoder=encoder,
        crf=18,
        video_preset=None,
    )
    # QSV look_ahead is beneficial but only valid for h264_qsv
    enc_args = build_video_encode_args(enc_opts)
    if enc_opts.resolved_encoder == "qsv":
        enc_args += ["-look_ahead", "1"]
    cmd.extend(
        [
            "-filter_complex",
            fc,
            "-map",
            "[v_cfr]" if output_fps else "[v]",
            "-map",
            "[a]",
        ]
        + enc_args
        + [
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            "-fps_mode",
            "cfr",
            str(out),
        ]
    )
    rc = _run_ffmpeg(cmd, "filter_complex concat")
    if rc == 0 and out.is_file() and out.stat().st_size > 0:
        from media_health import validate_media_health
        health = validate_media_health(out, mode="fast", require_audio=True)
        if health.ok:
            return "reencode"
        print(f"  [WARN] 拼接输出健康检查失败，尝试安全回退: {health.reason()}", flush=True)

    if cut_ranges:
        raise TwitchDownloadError(
            "带 --cut 的拼接主流程失败，已停止以避免回退流程输出未裁剪视频而导致聊天时间轴失步。"
        )

    # Fallback: concat demuxer with reencode (resets timestamps via -avoid_negative_ts)
    print("  [WARN] filter_complex concat 失败，尝试 concat demuxer + reencode…", flush=True)
    # 回退链路是固定 libx264 软编码；指定了硬件 encoder 或帧率时必须显式告知，
    # 否则用户以为回退产物保持了 nvenc/CFR 属性。
    if encoder not in ("auto", "x264"):
        print(
            f"  [WARN] 回退编码固定 libx264，忽略指定 encoder: {encoder!r}",
            flush=True,
        )
    re_cmd = [
        require_executable("ffmpeg"),
        "-hide_banner",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
    ]
    if output_fps:
        re_cmd.extend(["-r", str(output_fps)])
    re_cmd.extend(
        [
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-avoid_negative_ts",
            "make_zero",
            str(out),
        ]
    )
    rc = _run_ffmpeg(re_cmd, "concat demuxer reencode")
    if rc != 0 or not out.is_file() or out.stat().st_size <= 0:
        raise TwitchDownloadError(f"视频拼接失败 (exit {rc})")
    from media_health import validate_media_health
    health = validate_media_health(out, mode="fast", require_audio=True)
    if not health.ok:
        raise TwitchDownloadError("视频拼接后健康检查失败: " + health.reason())
    return "reencode"


def extract_emote_css_rules(html: str) -> dict[str, str]:
    """Map emote class → CSS rule text (first wins), without regex backtracking.

    TwitchDownloader embeds large base64 payloads in its ``<style>`` element.
    The former whole-document regex could catastrophically backtrack on a
    multi-megabyte HTML export, preventing segmented-chat merge from finishing.
    CSS rules do not nest braces here, so scanning style blocks one rule at a
    time is both sufficient and linear in the input size.
    """
    rules: dict[str, str] = {}
    for style in re.finditer(r"<style\b[^>]*>(.*?)</style\s*>", html or "", re.IGNORECASE | re.DOTALL):
        for raw_rule in style.group(1).split("}"):
            if "content" not in raw_rule or "data:image/" not in raw_rule:
                continue
            selector_blob, sep, declarations = raw_rule.partition("{")
            if not sep or "content" not in declarations or "url(" not in declarations:
                continue
            full_rule = f"{selector_blob.strip()} {{{declarations.strip()}}}"
            for sel in selector_blob.split(","):
                for tok in _EMOTE_CLASS_RE.findall(sel):
                    if tok.startswith(_EMOTE_PREFIXES) and tok not in rules:
                        rules[tok] = full_rule
    return rules


def iter_comment_root_blocks(html: str) -> list[str]:
    """Return each <pre class=comment-root>… chunk (may include trailing junk until next)."""
    text = html or ""
    if "comment-root" not in text:
        return []
    parts = _COMMENT_ROOT_SPLIT_RE.split(text)
    blocks: list[str] = []
    for part in parts:
        if "comment-root" not in part:
            continue
        # Trim to first complete </pre> when present
        lower = part.lower()
        end = lower.find("</pre>")
        if end >= 0:
            blocks.append(part[: end + len("</pre>")])
        else:
            blocks.append(part)
    return blocks


def remap_comment_block(
    block: str,
    *,
    begin_s: float,
    cum_s: float,
    duration_s: float,
) -> tuple[str, float] | None:
    """Rewrite stream-absolute t= into continuous merged timeline.

    Returns (new_block, merged_ts) or None if dropped.
    """
    m = re.search(
        r"""([?&]t=)(\d+)h(\d+)m(\d+)s""",
        block,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    stream = int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4))
    rel = float(stream) - float(begin_s)
    # Drop outliers outside the segment window (slack for TD edges)
    if rel < -1.0 or rel > float(duration_s) + 2.0:
        return None
    rel = max(0.0, rel)
    merged = float(cum_s) + rel
    t_query, t_disp = format_td_t_seconds(merged)

    def _sub_t(mm: re.Match[str]) -> str:
        return mm.group(1) + t_query

    new_block = _T_QUERY_RE.sub(_sub_t, block, count=1)

    # Rewrite visible text inside the first time <a href="...?t=...">DISPLAY</a>
    def _sub_display(mm: re.Match[str]) -> str:
        return mm.group(1) + t_disp + mm.group(3)

    new_block, n_disp = _TIME_LINK_DISPLAY_RE.subn(_sub_display, new_block, count=1)
    if n_disp == 0:
        # Fallback: bare [H:MM:SS] if some exporters write that form
        new_block = re.sub(
            r"\[(\d+:\d{2}:\d{2}|\d+:\d{2}|\d+h\d+m\d+s)\]",
            f"[{t_disp}]",
            new_block,
            count=1,
            flags=re.IGNORECASE,
        )
    return new_block, merged


def merge_chat_html(
    segments: list[SegmentDownload],
    *,
    source_id: str,
    out_path: Path,
    remove_ranges: list[tuple[float, float]] | None = None,
    cut_timeline: CutTimeline | None = None,
) -> Path:
    """Merge segment chat HTMLs with remapped continuous timestamps → out_path."""
    if not segments:
        raise TwitchDownloadError("没有可合并的聊天段")

    timeline_duration = sum(float(seg.duration_s) for seg in segments)
    try:
        timeline = cut_timeline or CutTimeline.from_ranges(remove_ranges, timeline_duration)
    except CutTimelineError as exc:
        raise TwitchDownloadError(str(exc)) from exc
    if abs(timeline.original_duration - timeline_duration) > 1e-6:
        raise TwitchDownloadError("裁切时间轴与聊天片段总时长不一致")

    emote_rules: dict[str, str] = {}
    collected: list[tuple[float, int, int, str]] = []  # merged_ts, seg_i, order, block
    cum = 0.0
    dropped = 0

    for seg in segments:
        html_path = seg.chat_html_path
        try:
            size = html_path.stat().st_size
        except OSError as e:
            raise TwitchDownloadError(f"无法读取聊天 HTML: {html_path}: {e}") from e
        if size > _MAX_HTML_BYTES:
            raise TwitchDownloadError(
                f"聊天 HTML 超过 {_MAX_HTML_BYTES / 2**30:.0f} GiB 上限"
                f"({size / 2**30:.1f} GiB),拒绝读取: {html_path}"
            )
        try:
            html = html_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise TwitchDownloadError(f"无法读取聊天 HTML: {html_path}: {e}") from e
        for cls, rule in extract_emote_css_rules(html).items():
            if cls not in emote_rules:
                emote_rules[cls] = rule
            # silent first-wins for identical; warn only if payload differs
            elif rule != emote_rules[cls]:
                print(f"  [WARN] emote class 冲突，保留先到者: .{cls}", flush=True)
        blocks = iter_comment_root_blocks(html)
        for order, block in enumerate(blocks):
            remapped = remap_comment_block(
                block,
                begin_s=seg.segment.begin_s,
                cum_s=cum,
                duration_s=seg.duration_s,
            )
            if remapped is None:
                dropped += 1
                continue
            new_block, merged_ts = remapped
            adjusted_ts = timeline.map_time(merged_ts)
            if adjusted_ts is None:
                dropped += 1
                continue
            t_query, t_disp = format_td_t_seconds(adjusted_ts)
            new_block = _T_QUERY_RE.sub(
                lambda mm, query=t_query: mm.group(1) + query,
                new_block,
                count=1,
            )
            new_block = _TIME_LINK_DISPLAY_RE.sub(
                lambda mm, display=t_disp: mm.group(1) + display + mm.group(3),
                new_block,
                count=1,
            )
            collected.append((adjusted_ts, seg.index, order, new_block))
        cum += float(seg.duration_s)

    collected.sort(key=lambda t: (t[0], t[1], t[2]))
    # Normalize each emote rule to single-class form for stable CSS
    style_parts: list[str] = []
    for cls, rule in emote_rules.items():
        # Prefer extract content:url payload and rewrite as .cls { content:url(...); }
        um = re.search(
            r"content\s*:\s*url\(\s*(['\"])(data:image/[^'\"]*;base64,[^'\"]+)\1\s*\)",
            rule,
            flags=re.IGNORECASE,
        )
        if um:
            q, payload = um.group(1), um.group(2)
            style_parts.append(f".{cls} {{ content:url({q}{payload}{q}); }}")
        else:
            style_parts.append(rule)

    body = "\n".join(t[3] for t in collected)
    # video id in href is cosmetic for parser (only t= matters)
    vid = re.sub(r"[^\w-]+", "", str(source_id))[:32] or "1"
    # Ensure hrefs point at a plausible videos/N if missing — blocks already have hrefs
    doc = (
        "<!DOCTYPE html>\n<html>\n<head>\n<meta charset=\"utf-8\">\n"
        f"<title>merged chat vod {vid}</title>\n<style>\n"
        + "\n".join(style_parts)
        + "\n</style>\n</head>\n<body>\n"
        + body
        + "\n</body>\n</html>\n"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")
    validate_chat_html(out_path)
    print(
        f"[OK] 合并聊天: {out_path}  (消息 {len(collected)} 条"
        + (f", 丢弃 {dropped}" if dropped else "")
        + ")",
        flush=True,
    )
    return out_path.resolve()
