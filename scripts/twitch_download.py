#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Thin wrapper around TwitchDownloaderCLI for VOD/clip video + embedded chat HTML.

This project only consumes TwitchDownloader HTML with CSS-embedded emotes
(content:url(data:image...)). Chat download always uses --embed-images (-E).
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
import time
import zipfile

# Sibling imports when loaded as script or via importlib.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from common_utils import (
    current_cli_invocation,
    env_loaded_from_dotenv,
    require_executable,
    runtime_app_root,
    safe_which,
    trusted_tools_root,
)
from process_util import run_tracked

_REPO_ROOT = runtime_app_root(__file__)
_TOOLS_ROOT = trusted_tools_root(__file__)

# VOD numeric id in common Twitch URL shapes.
_VOD_ID_RE = re.compile(
    r"(?:twitch\.tv/(?:[^/]+/)?videos?/|twitch\.tv/videos/)(\d+)",
    re.IGNORECASE,
)
_CLIP_URL_RE = re.compile(
    r"(?:clips\.twitch\.tv/|twitch\.tv/\w+/clip/)([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_BARE_VOD_RE = re.compile(r"^\d{6,}$")
_BARE_CLIP_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{3,}$")


class TwitchDownloadError(RuntimeError):
    """User-facing download failure."""


@dataclass
class DownloadResult:
    video_path: Path
    chat_html_path: Path
    kind: str  # vod | clip
    source_id: str
    quality: str | None
    begin: str | None
    end: str | None
    out_dir: Path


def tools_td_bin_dirs(root: Path | None = None) -> list[Path]:
    """Candidate dirs under tools/ for TwitchDownloaderCLI."""
    tools_root = root or _TOOLS_ROOT
    base = tools_root / "tools" / "TwitchDownloaderCLI"
    return [
        base,
        base / "bin",
        tools_root / "tools",
    ]


def td_exe_names() -> list[str]:
    if os.name == "nt":
        return ["TwitchDownloaderCLI.exe", "TwitchDownloaderCLI"]
    return ["TwitchDownloaderCLI", "TwitchDownloaderCLI.exe"]


def find_twitchdownloader_cli(root: Path | None = None) -> Path | None:
    """Resolve TwitchDownloaderCLI binary or None."""
    env = (os.environ.get("TWITCHDOWNLOADER_CLI") or "").strip()
    if env and not env_loaded_from_dotenv("TWITCHDOWNLOADER_CLI"):
        p = Path(env).expanduser()
        if p.is_absolute() and p.is_file():
            return p.resolve()
    for d in tools_td_bin_dirs(root):
        if not d.is_dir():
            continue
        for name in td_exe_names():
            cand = d / name
            if cand.is_file():
                return cand.resolve()
    for name in td_exe_names():
        which = safe_which(name)
        if which:
            return Path(which)
    return None


def prepend_tools_td_to_path(root: Path | None = None) -> str | None:
    """If tools/TwitchDownloaderCLI has the binary, prepend its dir to PATH."""
    found = find_twitchdownloader_cli(root)
    if not found:
        return None
    bin_dir = str(found.parent)
    path = os.environ.get("PATH") or ""
    parts = path.split(os.pathsep) if path else []
    if bin_dir not in parts:
        os.environ["PATH"] = bin_dir + (os.pathsep + path if path else "")
    return bin_dir


def parse_twitch_source(raw: str, *, kind_hint: str = "auto") -> tuple[str, str]:
    """Return (kind, id_or_url) where kind is vod|clip.

    kind_hint: auto|vod|clip. On auto, detect from URL/shape; bare digits → vod.
    """
    text = (raw or "").strip()
    if not text:
        raise TwitchDownloadError("缺少 Twitch VOD/Clip URL 或 ID")
    hint = (kind_hint or "auto").strip().lower()
    if hint not in ("auto", "vod", "clip"):
        raise TwitchDownloadError(f"无效 --kind: {kind_hint!r}（auto|vod|clip）")

    m_vod = _VOD_ID_RE.search(text)
    m_clip = _CLIP_URL_RE.search(text)

    if hint == "vod":
        if m_vod:
            return "vod", m_vod.group(1)
        if _BARE_VOD_RE.match(text):
            return "vod", text
        # Pass through URL/id for CLI to resolve
        return "vod", text
    if hint == "clip":
        if m_clip:
            return "clip", m_clip.group(1)
        return "clip", text

    # auto
    if m_vod and not m_clip:
        return "vod", m_vod.group(1)
    if m_clip and not m_vod:
        return "clip", m_clip.group(1)
    if m_vod and m_clip:
        # Prefer explicit /videos/ over clip if both somehow match
        return "vod", m_vod.group(1)
    if _BARE_VOD_RE.match(text):
        return "vod", text
    if "clip" in text.lower() or "clips.twitch" in text.lower():
        return "clip", text
    if _BARE_CLIP_RE.match(text) and not text.isdigit():
        return "clip", text
    # Default: let videodownload try (CLI accepts URL)
    if "twitch.tv" in text.lower() or text.isdigit():
        return "vod", text
    raise TwitchDownloadError(
        f"无法识别为 VOD 或 Clip: {text!r}\n"
        "  示例: https://www.twitch.tv/videos/123456789\n"
        "        https://clips.twitch.tv/SomeClipSlug\n"
        "  或加 --kind vod|clip"
    )


def slug_for_source(kind: str, source_id: str) -> str:
    """Filesystem-safe folder name."""
    # Prefer trailing numeric VOD id
    m = re.search(r"(\d{6,})", source_id)
    if kind == "vod" and m:
        base = m.group(1)
    else:
        base = source_id.rstrip("/").split("/")[-1] or "twitch"
    base = re.sub(r"[^\w.\-]+", "_", base, flags=re.UNICODE).strip("._") or "twitch"
    return base[:80]


def default_download_dir(root: Path | None = None) -> Path:
    return (root or _REPO_ROOT) / "downloads"


def build_video_cmd(
    cli: Path,
    *,
    kind: str,
    source_id: str,
    output: Path,
    quality: str | None = None,
    begin: str | None = None,
    end: str | None = None,
    oauth: str | None = None,
    ffmpeg_path: str | None = None,
    trim_mode: str = "Safe",
) -> list[str]:
    mode = "videodownload" if kind == "vod" else "clipdownload"
    cmd = [str(cli), mode, "--id", source_id, "-o", str(output), "--collision", "Overwrite"]
    if quality:
        cmd.extend(["-q", quality])
    if kind == "vod":
        if begin:
            cmd.extend(["-b", begin])
        if end:
            cmd.extend(["-e", end])
        trim = str(trim_mode or "Safe").strip().capitalize()
        if trim not in ("Safe", "Exact"):
            raise TwitchDownloadError(f"无效 trim mode: {trim_mode!r}（Safe|Exact）")
        # Safe avoids the known ~1s A/V desync from Exact crop + stream copy.
        cmd.extend(["--trim-mode", trim])
    if oauth:
        cmd.extend(["--oauth", oauth])
    if ffmpeg_path:
        cmd.extend(["--ffmpeg-path", ffmpeg_path])
    return cmd


def build_chat_cmd(
    cli: Path,
    *,
    source_id: str,
    output: Path,
    begin: str | None = None,
    end: str | None = None,
    embed: bool = True,
    bttv: bool = True,
    ffz: bool = True,
    stv: bool = True,
) -> list[str]:
    cmd = [
        str(cli),
        "chatdownload",
        "--id",
        source_id,
        "-o",
        str(output),
        "--collision",
        "Overwrite",
    ]
    if embed:
        cmd.append("-E")
    # Explicit third-party toggles (defaults true when embeds on)
    cmd.append(f"--bttv={'true' if bttv else 'false'}")
    cmd.append(f"--ffz={'true' if ffz else 'false'}")
    cmd.append(f"--stv={'true' if stv else 'false'}")
    if begin:
        cmd.extend(["-b", begin])
    if end:
        cmd.extend(["-e", end])
    return cmd


def validate_chat_html(path: Path) -> None:
    """Fail if HTML is missing TD markers or embedded emote CSS."""
    if not path.is_file():
        raise TwitchDownloadError(f"聊天 HTML 不存在: {path}")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise TwitchDownloadError(f"无法读取聊天 HTML: {e}") from e
    if "comment-root" not in text and "comment-author" not in text:
        raise TwitchDownloadError(
            f"HTML 不像 TwitchDownloader 聊天导出（缺少 comment-root）: {path}"
        )
    # Emotes optional if chat has no emotes, but warn-level hard fail only if
    # file claims images without data embeds is hard to detect. Require either
    # embed CSS or zero emote-image tags.
    has_data = "content:url(" in text and "base64," in text.lower()
    has_emote_img = "emote-image" in text or "first-" in text or "third-" in text
    if has_emote_img and not has_data:
        # Likely remote CDN only — this project will not fetch.
        if "static-cdn.jtvnw.net" in text or "cdn.betterttv.net" in text:
            raise TwitchDownloadError(
                "聊天 HTML 含远程 emote URL，但未嵌入 base64。\n"
                "  请用 TwitchDownloaderCLI chatdownload 加 -E / --embed-images 重新导出。"
            )
    # Soft: pure-text chats are ok without embeds
    _ = has_data


def _run_cli(cmd: list[str], *, label: str) -> None:
    # Never print oauth token values
    safe = []
    skip_next = False
    for part in cmd:
        if skip_next:
            safe.append("***")
            skip_next = False
            continue
        if part in ("--oauth",):
            safe.append(part)
            skip_next = True
            continue
        safe.append(part)
    print(f"\n$ {' '.join(safe)}", flush=True)
    try:
        completed = run_tracked(
            cmd,
            stdout=None,
            stderr=None,
            text=False,
            check=False,
        )
    except FileNotFoundError as e:
        raise TwitchDownloadError(
            f"无法启动 TwitchDownloaderCLI: {e}\n"
            "  请安装 CLI 并加入 PATH，或运行 --offer-td-cli 安装到可信工具目录\n"
            "  或设置环境变量 TWITCHDOWNLOADER_CLI=完整路径"
        ) from e
    if completed.returncode != 0:
        raise TwitchDownloadError(
            f"{label} 失败 (exit {completed.returncode})。\n"
            "  请检查网络、URL/ID、是否需 --oauth（订阅限定），以及 CLI 版本。"
        )


def _print_media_health_warnings(health) -> None:
    for warning in getattr(health, "warnings", []):
        print(f"  [WARN] 媒体健康检查: {warning}", flush=True)


def download_assets(
    source: str,
    *,
    out_dir: Path | None = None,
    kind: str = "auto",
    quality: str | None = "1080p60",
    begin: str | None = None,
    end: str | None = None,
    oauth: str | None = None,
    root: Path | None = None,
    video_name: str = "video.mp4",
    chat_name: str = "chat.html",
    trim_mode: str = "Safe",
    media_check: str = "fast",
    media_repair: str = "audio",
) -> DownloadResult:
    """Download video + embedded chat HTML into out_dir."""
    app_root = root or _REPO_ROOT
    tools_root = _TOOLS_ROOT
    cli = find_twitchdownloader_cli(tools_root)
    if cli is None:
        raise TwitchDownloadError(
            "未找到 TwitchDownloaderCLI。\n"
            "  1) 从 https://github.com/lay295/TwitchDownloader/releases 下载 CLI\n"
            "  2) 运行 --offer-td-cli 安装到可信工具目录\n"
            "  3) 或加入 PATH / 设置 TWITCHDOWNLOADER_CLI\n"
            "  安装结束时也可选择可选增强下载引导。"
        )

    kind_r, source_id = parse_twitch_source(source, kind_hint=kind)
    if kind_r == "clip" and (begin or end):
        print(
            "  [提示] Clip 本身已是片段，忽略 --begin/--end（仅 VOD 支持裁切）",
            flush=True,
        )
        begin, end = None, None

    slug = slug_for_source(kind_r, source_id)
    ts = time.strftime("%Y%m%d_%H%M%S")
    base = Path(out_dir) if out_dir else default_download_dir(app_root) / f"{slug}_{ts}"
    try:
        from process_util import is_dangerous_publish_path

        if is_dangerous_publish_path(base) or is_dangerous_publish_path(base.parent):
            raise TwitchDownloadError(f"下载目录不能是系统路径: {base}")
    except ImportError:
        pass
    base.mkdir(parents=True, exist_ok=True)
    video_path = base / video_name
    chat_path = base / chat_name

    # Prefer system/tools ffmpeg for TD video mux when available
    ffmpeg_path = safe_which("ffmpeg")

    vcmd = build_video_cmd(
        cli,
        kind=kind_r,
        source_id=source_id,
        output=video_path,
        quality=quality,
        begin=begin if kind_r == "vod" else None,
        end=end if kind_r == "vod" else None,
        oauth=oauth,
        ffmpeg_path=ffmpeg_path,
        trim_mode=trim_mode,
    )
    _run_cli(vcmd, label="视频下载")
    if not video_path.is_file():
        # Some CLI versions may write alternate names; search dir
        mp4s = list(base.glob("*.mp4"))
        if mp4s:
            video_path = mp4s[0]
        else:
            raise TwitchDownloadError(f"视频下载后未找到 mp4: {base}")
    from media_health import repair_media, validate_media_health
    health = validate_media_health(video_path, mode=media_check, require_audio=True)
    _print_media_health_warnings(health)
    if not health.ok and str(media_repair or "off").lower() == "audio":
        try:
            video_path = repair_media(video_path)
            health = validate_media_health(video_path, mode=media_check, require_audio=True)
            _print_media_health_warnings(health)
        except (OSError, RuntimeError) as e:
            raise TwitchDownloadError(f"下载视频修复失败，原文件未覆盖: {e}") from e
    if not health.ok:
        raise TwitchDownloadError("下载视频健康检查失败，已阻止继续下载聊天/翻译/渲染: " + health.reason())

    ccmd = build_chat_cmd(
        cli,
        source_id=source_id,
        output=chat_path,
        begin=begin if kind_r == "vod" else None,
        end=end if kind_r == "vod" else None,
        embed=True,
    )
    _run_cli(ccmd, label="聊天 HTML 下载")
    if not chat_path.is_file():
        htmls = list(base.glob("*.html"))
        if htmls:
            chat_path = htmls[0]
        else:
            raise TwitchDownloadError(f"聊天下载后未找到 html: {base}")

    validate_chat_html(chat_path)
    print(f"\n[OK] 视频: {video_path}", flush=True)
    print(f"[OK] 聊天: {chat_path}", flush=True)
    return DownloadResult(
        video_path=video_path.resolve(),
        chat_html_path=chat_path.resolve(),
        kind=kind_r,
        source_id=source_id,
        quality=quality,
        begin=begin,
        end=end,
        out_dir=base.resolve(),
    )


# ---------------------------------------------------------------------------
# Same-VOD multi-segment crop: download parts → concat video → merge chat
# Merged HTML timestamps are continuous video-relative from 0 (not VOD-absolute).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CropSegment:
    """One VOD crop range (raw TD strings + parsed seconds)."""

    begin: str
    end: str
    begin_s: float
    end_s: float


@dataclass
class SegmentDownload:
    index: int
    segment: CropSegment
    video_path: Path
    chat_html_path: Path
    duration_s: float


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
_EMOTE_CSS_RULE_RE = re.compile(
    r"([^{}]+)\{[^{}]*?content\s*:\s*url\(\s*(['\"])data:image/[^'\"]*;base64,([^'\"]+)\2\s*\)[^{}]*\}",
    re.IGNORECASE | re.DOTALL,
)
_EMOTE_CLASS_RE = re.compile(r"\.([A-Za-z0-9_-]+)")
_EMOTE_PREFIXES = ("first-", "second-", "third-")


def parse_td_time(value: str) -> float:
    """Parse TD/user time strings to seconds.

    Accepts: 100, 100s, 1m40s, 0h1m40s, 0:01:40, 1:40, optional fractional seconds.
    """
    text = (value or "").strip()
    if not text:
        raise TwitchDownloadError("时间字符串为空")
    # Colon forms: H:MM:SS(.fff) or M:SS(.fff)
    if ":" in text:
        parts = text.split(":")
        try:
            if len(parts) == 3:
                h, m, s = float(parts[0]), float(parts[1]), float(parts[2])
            elif len(parts) == 2:
                h, m, s = 0.0, float(parts[0]), float(parts[1])
            else:
                raise ValueError("bad colon time")
        except ValueError as e:
            raise TwitchDownloadError(f"无法解析时间: {value!r}") from e
        if m < 0 or s < 0 or h < 0:
            raise TwitchDownloadError(f"时间不能为负: {value!r}")
        return h * 3600.0 + m * 60.0 + s

    # Compact: 0h1m40s / 1m40s / 100s / 100
    m = re.fullmatch(
        r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s?)?",
        text,
        flags=re.IGNORECASE,
    )
    if not m or not any(m.groups()):
        raise TwitchDownloadError(
            f"无法解析时间: {value!r}\n"
            "  支持: 0:01:40 / 1:40 / 100s / 1m40s / 0h1m40s"
        )
    # Bare number without unit: treat as seconds (group 3 from optional s?)
    # fullmatch with all optional can match empty — already guarded.
    # For "100" the pattern puts it in the last group via (\d+)s? — actually
    # "100" matches group3="100" with optional s absent. Good.
    # "100s" → group3=100. "1m" → group2=1. "1m40s" → g2=1 g3=40.
    try:
        h = float(m.group(1) or 0)
        mi = float(m.group(2) or 0)
        s = float(m.group(3) or 0)
    except ValueError as e:
        raise TwitchDownloadError(f"无法解析时间: {value!r}") from e
    # Disallow pure junk like "h" — already needs digits.
    if h == 0 and mi == 0 and s == 0 and not re.search(r"\d", text):
        raise TwitchDownloadError(f"无法解析时间: {value!r}")
    return h * 3600.0 + mi * 60.0 + s


def format_td_t_seconds(seconds: float) -> tuple[str, str]:
    """Return (query_t, display) for integer seconds: ('0h12m26s', '0:12:26')."""
    total = int(max(0, round(float(seconds))))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}h{m}m{s}s", f"{h}:{m:02d}:{s:02d}"


def make_crop_segment(begin: str, end: str) -> CropSegment:
    b = (begin or "").strip()
    e = (end or "").strip()
    if not b or not e:
        raise TwitchDownloadError("多段裁切每段都需要起点和终点")
    begin_s = parse_td_time(b)
    end_s = parse_td_time(e)
    if end_s <= begin_s:
        raise TwitchDownloadError(f"终点必须大于起点: begin={b!r} end={e!r}")
    return CropSegment(begin=b, end=e, begin_s=begin_s, end_s=end_s)


def parse_segment_line(line: str) -> CropSegment | None:
    """Parse 'begin end' or 'begin,end' or 'begin-end' (when both sides look like times).

    Empty/whitespace → None (end of multi-prompt loop).
    """
    text = (line or "").strip()
    if not text:
        return None
    # Prefer whitespace or comma split
    if "," in text:
        parts = [p.strip() for p in text.split(",") if p.strip()]
    else:
        parts = text.split()
    if len(parts) == 1 and "-" in text:
        # Ambiguous: "0:10:00-0:12:30" — split on last-ish hyphen between times
        # Try split into two time-like tokens on '-'
        m = re.match(
            r"^(.+?)\s*-\s*(.+)$",
            text,
        )
        if m:
            parts = [m.group(1).strip(), m.group(2).strip()]
    if len(parts) != 2:
        raise TwitchDownloadError(
            f"无法解析裁切段 {text!r}（需要: 起点 终点，例如 0:10:00 0:12:30）"
        )
    return make_crop_segment(parts[0], parts[1])


def validate_segments(segments: list[CropSegment], *, allow_overlap: bool = True) -> None:
    if not segments:
        raise TwitchDownloadError("未输入任何裁切段")
    for i, seg in enumerate(segments, start=1):
        if seg.end_s <= seg.begin_s:
            raise TwitchDownloadError(
                f"第 {i} 段终点必须大于起点: begin={seg.begin!r} end={seg.end!r}"
            )
    if not allow_overlap:
        ordered = sorted(segments, key=lambda s: s.begin_s)
        for a, b in zip(ordered, ordered[1:]):
            if b.begin_s < a.end_s:
                raise TwitchDownloadError(
                    f"裁切段重叠: {a.begin}-{a.end} 与 {b.begin}-{b.end}"
                )
        return
    # Soft overlap warning
    ordered = sorted(enumerate(segments), key=lambda it: it[1].begin_s)
    for (i, a), (j, b) in zip(ordered, ordered[1:]):
        if b.begin_s < a.end_s:
            print(
                f"  [WARN] 第 {i + 1} 段与第 {j + 1} 段时间重叠，合并后可能出现重复弹幕",
                flush=True,
            )


def normalize_cut_ranges(
    ranges: list[tuple[float, float]] | None,
    total_duration: float,
) -> list[tuple[float, float]]:
    """Clamp, sort, and merge cuts on one original merged-video timeline."""
    total = max(0.0, float(total_duration))
    clipped: list[tuple[float, float]] = []
    for raw_start, raw_end in ranges or []:
        start = float(raw_start)
        end = float(raw_end)
        if end <= start:
            raise TwitchDownloadError(f"无效切除范围: {start:g}-{end:g}")
        start = min(total, max(0.0, start))
        end = min(total, max(0.0, end))
        if end > start:
            clipped.append((start, end))
    clipped.sort()

    merged: list[tuple[float, float]] = []
    for start, end in clipped:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def probe_media_duration(path: Path) -> float:
    """ffprobe format duration (seconds). Local helper — avoids importing burn."""
    import subprocess as _sp

    if not path.is_file():
        raise TwitchDownloadError(f"无法探测时长，文件不存在: {path}")
    probe = _sp.run(
        [
            require_executable("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    raw = (probe.stdout or "").strip().splitlines()
    if probe.returncode != 0 or not raw:
        err = (probe.stderr or probe.stdout or "ffprobe failed").strip()[:400]
        raise TwitchDownloadError(f"无法读取视频时长: {path}: {err}")
    try:
        duration = float(raw[0].strip() or 0.0)
    except ValueError as e:
        raise TwitchDownloadError(f"无法解析视频时长 {raw[0]!r}: {e}") from e
    if duration <= 0:
        raise TwitchDownloadError(f"视频时长无效 ({duration}): {path}")
    return duration


def get_stream_start_time(path: Path, stream_selector: str) -> float:
    """Return a stream start_time in seconds, defaulting to 0 when absent."""
    import subprocess as _sp

    probe = _sp.run(
        [
            require_executable("ffprobe"), "-v", "error", "-select_streams", stream_selector,
            "-show_entries", "stream=start_time", "-of", "csv=p=0", str(path),
        ],
        capture_output=True,
        text=True,
    )
    raw = (probe.stdout or "").strip().splitlines()
    if probe.returncode != 0 or not raw:
        return 0.0
    try:
        return float(raw[0].strip() or 0.0)
    except ValueError:
        return 0.0


def probe_av_fingerprint(path: Path) -> tuple[str, str, str, str, str, str]:
    """Best-effort (vcodec, width, height, pix_fmt, acodec, sample_rate)."""
    import json as _json
    import subprocess as _sp

    empty = ("", "", "", "", "", "")
    probe = _sp.run(
        [
            require_executable("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,pix_fmt,sample_rate",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        return empty
    try:
        data = _json.loads(probe.stdout or "{}")
    except Exception:
        return empty
    vcodec = width = height = pix = acodec = rate = ""
    for st in data.get("streams") or []:
        if not isinstance(st, dict):
            continue
        if st.get("codec_type") == "video" and not vcodec:
            vcodec = str(st.get("codec_name") or "")
            width = str(st.get("width") or "")
            height = str(st.get("height") or "")
            pix = str(st.get("pix_fmt") or "")
        elif st.get("codec_type") == "audio" and not acodec:
            acodec = str(st.get("codec_name") or "")
            rate = str(st.get("sample_rate") or "")
    return (vcodec, width, height, pix, acodec, rate)


def _ffmpeg_concat_list_line(path: Path) -> str:
    """Escape path for ffmpeg concat demuxer file directive."""
    # concat demuxer: file 'path' with ' → '\''
    p = str(path.resolve()).replace("\\", "/")
    p = p.replace("'", r"'\''")
    return f"file '{p}'"


def concat_videos(
    paths: list[Path],
    out: Path,
    *,
    list_path: Path | None = None,
    remove_ranges: list[tuple[float, float]] | None = None,
    output_fps: float | None = None,
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
    ``output_fps`` optionally forces CFR without disabling B-frames.
    ``encoder`` selects the video encoder: auto (default) detects hardware
    encoders (QSV/NVENC/AMF) with libx264 fallback; or explicitly specify
    x264/nvenc/qsv/amf.
    """
    if not paths:
        raise TwitchDownloadError("没有可拼接的视频段")
    if len(paths) == 1:
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
    durations: list[float] = []
    for p in paths:
        try:
            durations.append(probe_media_duration(p))
        except TwitchDownloadError:
            durations.append(0.0)
    cut_ranges = normalize_cut_ranges(remove_ranges, sum(durations))
    chains: list[str] = []
    concat_inputs: list[str] = []
    for i, duration in enumerate(durations):
        video_start = get_stream_start_time(paths[i], "v:0") or 0.0
        audio_start = get_stream_start_time(paths[i], "a:0") or 0.0
        lead_in = max(0.0, float(video_start) - float(audio_start))
        trim = max(0.001, float(duration or 0.0))
        segment_start = sum(durations[:i])
        keep_ranges = [(0.0, trim)]
        for cut_start, cut_end in cut_ranges:
            local_start = max(0.0, float(cut_start) - segment_start)
            local_end = min(trim, float(cut_end) - segment_start)
            if local_end <= local_start:
                continue
            next_ranges: list[tuple[float, float]] = []
            for keep_start, keep_end in keep_ranges:
                if local_start > keep_start:
                    next_ranges.append((keep_start, min(keep_end, local_start)))
                if local_end < keep_end:
                    next_ranges.append((max(keep_start, local_end), keep_end))
            keep_ranges = [(a, b) for a, b in next_ranges if b - a > 1e-6]
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
        fc += f";[v]fps=fps={float(output_fps):.6f}[v_cfr]"
    # Resolve encoder via encode_options (auto-detect hardware vs software)
    from encode_options import build_video_encode_args, resolve_encode_options

    enc_opts = resolve_encode_options(
        encoder=encoder,
        crf=18,
        video_preset="medium" if encoder in ("auto", "qsv") else None,
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
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-avoid_negative_ts",
        "make_zero",
        str(out),
    ]
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
) -> Path:
    """Merge segment chat HTMLs with remapped continuous timestamps → out_path."""
    if not segments:
        raise TwitchDownloadError("没有可合并的聊天段")

    cut_ranges = normalize_cut_ranges(
        remove_ranges,
        sum(float(seg.duration_s) for seg in segments),
    )

    emote_rules: dict[str, str] = {}
    collected: list[tuple[float, int, int, str]] = []  # merged_ts, seg_i, order, block
    cum = 0.0
    dropped = 0

    for seg in segments:
        try:
            html = seg.chat_html_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise TwitchDownloadError(f"无法读取聊天 HTML: {seg.chat_html_path}: {e}") from e
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
            adjusted_ts = merged_ts
            dropped_for_cut = False
            for cut_start, cut_end in cut_ranges:
                if float(cut_start) <= merged_ts < float(cut_end):
                    dropped_for_cut = True
                    break
                if merged_ts >= float(cut_end):
                    adjusted_ts -= float(cut_end) - float(cut_start)
            if dropped_for_cut:
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


def download_assets_multi(
    source: str,
    segments: list[tuple[str, str]] | list[CropSegment],
    *,
    out_dir: Path | None = None,
    kind: str = "auto",
    quality: str | None = "1080p60",
    oauth: str | None = None,
    root: Path | None = None,
    video_name: str = "video.mp4",
    chat_name: str = "chat.html",
    remove_ranges: list[tuple[float, float]] | None = None,
    output_fps: float | None = None,
    encoder: str = "auto",
    trim_mode: str = "Safe",
    media_check: str = "fast",
    media_repair: str = "audio",
) -> DownloadResult:
    """Download multiple same-VOD crops, concat video, merge chat.

    Final HTML timestamps are continuous from 0 (video-relative for the merged file).
    Single segment falls back to plain download_assets.

    ``remove_ranges``: continuous merged-timeline ranges (seconds) to cut from
    the final video and chat, e.g. [(1261, 1379)] removes 21:01–22:59.
    ``output_fps``: force CFR at this fps (e.g. 60). None keeps source fps.
    ``encoder``: video encoder for concat re-encode (auto/x264/nvenc/qsv/amf).
    """
    app_root = root or _REPO_ROOT
    crops: list[CropSegment] = []
    for item in segments or []:
        if isinstance(item, CropSegment):
            crops.append(item)
        else:
            b, e = item[0], item[1]
            crops.append(make_crop_segment(str(b), str(e)))
    validate_segments(crops)

    kind_r, source_id = parse_twitch_source(source, kind_hint=kind)
    if kind_r == "clip":
        raise TwitchDownloadError("多段裁切仅支持 VOD；当前识别为 Clip")

    if len(crops) == 1:
        if remove_ranges or output_fps is not None:
            raise TwitchDownloadError(
                "--cut / --download-output-fps 需要至少两个 --segment；"
                "单段请先下载，再以多段流程处理，不能静默忽略参数"
            )
        return download_assets(
            source,
            out_dir=out_dir,
            kind="vod",
            quality=quality,
            begin=crops[0].begin,
            end=crops[0].end,
            oauth=oauth,
            root=root,
            video_name=video_name,
            chat_name=chat_name,
            trim_mode=trim_mode,
            media_check=media_check,
            media_repair=media_repair,
        )

    slug = slug_for_source(kind_r, source_id)
    ts = time.strftime("%Y%m%d_%H%M%S")
    base = Path(out_dir) if out_dir else default_download_dir(app_root) / f"{slug}_{ts}"
    try:
        from process_util import is_dangerous_publish_path

        if is_dangerous_publish_path(base) or is_dangerous_publish_path(base.parent):
            raise TwitchDownloadError(f"下载目录不能是系统路径: {base}")
    except ImportError:
        pass
    base.mkdir(parents=True, exist_ok=True)

    seg_downloads: list[SegmentDownload] = []
    n = len(crops)
    for i, crop in enumerate(crops):
        print(
            f"\n-- 多段 {i + 1}/{n}: begin={crop.begin} end={crop.end}",
            flush=True,
        )
        part = download_assets(
            source,
            out_dir=base,
            kind="vod",
            quality=quality,
            begin=crop.begin,
            end=crop.end,
            oauth=oauth,
            root=root,
            video_name=f"seg_{i:02d}.mp4",
            chat_name=f"seg_{i:02d}.html",
            trim_mode=trim_mode,
            media_check=media_check,
            media_repair=media_repair,
        )
        try:
            dur = probe_media_duration(part.video_path)
        except TwitchDownloadError as e:
            raise TwitchDownloadError(f"第 {i + 1} 段{e}") from e
        print(f"  时长 {dur:.2f}s", flush=True)
        seg_downloads.append(
            SegmentDownload(
                index=i,
                segment=crop,
                video_path=part.video_path,
                chat_html_path=part.chat_html_path,
                duration_s=dur,
            )
        )

    timeline_duration = sum(s.duration_s for s in seg_downloads)
    cut_ranges = normalize_cut_ranges(remove_ranges, timeline_duration)
    final_video = base / video_name
    final_chat = base / chat_name
    mode = concat_videos(
        [s.video_path for s in seg_downloads],
        final_video,
        list_path=base / "concat_list.txt",
        remove_ranges=cut_ranges,
        output_fps=output_fps,
        encoder=encoder,
    )
    print(f"[OK] 合并视频: {final_video}  (mode={mode})", flush=True)

    expected = timeline_duration - sum(cut_end - cut_start for cut_start, cut_end in cut_ranges)
    from media_health import repair_media, validate_media_health
    health = validate_media_health(final_video, mode=media_check, require_audio=True, expected_duration=expected)
    _print_media_health_warnings(health)
    if not health.ok and str(media_repair or "off").lower() == "audio":
        try:
            repaired = repair_media(final_video, encoder=encoder)
            health = validate_media_health(repaired, mode=media_check, require_audio=True, expected_duration=expected)
            _print_media_health_warnings(health)
            if health.ok:
                final_video = repaired
        except (OSError, RuntimeError) as e:
            raise TwitchDownloadError(f"合并视频修复失败，原文件未覆盖: {e}") from e
    if not health.ok:
        raise TwitchDownloadError("合并视频健康检查失败，已阻止合并聊天/翻译/渲染: " + health.reason())

    print("-- 合并聊天时间轴 ...", flush=True)
    merge_chat_html(
        seg_downloads,
        source_id=source_id,
        out_path=final_chat,
        remove_ranges=cut_ranges,
    )

    return DownloadResult(
        video_path=final_video.resolve(),
        chat_html_path=final_chat.resolve(),
        kind=kind_r,
        source_id=source_id,
        quality=quality,
        begin=None,
        end=None,
        out_dir=base.resolve(),
    )


def td_install_hints() -> tuple[list[str], list[str]]:
    portable_dir = tools_td_bin_dirs()[0]
    cmds = [
        f"# 自动: {current_cli_invocation()} --offer-td-cli  (或 install 结束时询问)",
        f"# 手动: 下载 CLI zip 并解压到可信工具目录 {portable_dir}",
        f"# Windows: 确保存在 {portable_dir / 'TwitchDownloaderCLI.exe'}",
        "# 或设置环境变量 TWITCHDOWNLOADER_CLI=完整路径",
    ]
    urls = ["https://github.com/lay295/TwitchDownloader/releases"]
    return cmds, urls


def platform_td_asset_token() -> str | None:
    """Substring that matches lay295 release asset names for this OS/arch."""
    import platform as _platform

    sysname = _platform.system()
    machine = (_platform.machine() or "").lower()
    if sysname == "Windows":
        return "Windows-x64"
    if sysname == "Darwin":
        if machine in ("arm64", "aarch64"):
            return "MacOSArm64"
        return "MacOS-x64"
    if sysname == "Linux":
        if machine in ("aarch64", "arm64"):
            return "LinuxArm64"
        if machine.startswith("arm"):
            return "LinuxArm"
        # Prefer glibc x64 over Alpine build for desktop distros
        return "Linux-x64"
    return None


def pick_td_cli_asset(assets: list[dict]) -> dict | None:
    """Pick best TwitchDownloaderCLI zip asset from GitHub release asset list."""
    token = platform_td_asset_token()
    if not token:
        return None
    cli_assets = []
    for a in assets or []:
        name = str(a.get("name") or "")
        if "TwitchDownloaderCLI" not in name:
            continue
        if "GUI" in name:
            continue
        if not name.lower().endswith(".zip"):
            continue
        cli_assets.append(a)
    # Exact platform token match (avoid LinuxArm matching LinuxArm64).
    for a in cli_assets:
        name = str(a.get("name") or "")
        if token == "LinuxArm":
            if "LinuxArm64" in name:
                continue
            if "LinuxArm" in name:
                return a
        elif token in name:
            return a
    return None


def fetch_latest_td_cli_release_asset(
    *,
    timeout: float = 30.0,
) -> tuple[str, str, str]:
    """Return (tag, asset_name, browser_download_url) for this platform.

    Raises TwitchDownloadError on network/API/selection failure.
    """
    import json as _json
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    api = "https://api.github.com/repos/lay295/TwitchDownloader/releases/latest"
    req = Request(
        api,
        headers={
            "User-Agent": "twitch-chat-cn-overlay",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed GitHub API
            data = _json.loads(resp.read().decode("utf-8", errors="replace"))
    except HTTPError as e:
        raise TwitchDownloadError(f"GitHub releases API 失败 HTTP {e.code}: {e.reason}") from e
    except URLError as e:
        raise TwitchDownloadError(f"无法连接 GitHub releases: {e.reason}") from e
    except Exception as e:
        raise TwitchDownloadError(f"读取 releases 失败: {e}") from e

    tag = str(data.get("tag_name") or "unknown")
    assets = data.get("assets") or []
    if not isinstance(assets, list):
        raise TwitchDownloadError("releases 响应缺少 assets 列表")
    picked = pick_td_cli_asset(assets)
    if not picked:
        token = platform_td_asset_token() or "unknown-platform"
        names = [str(a.get("name") or "") for a in assets if "CLI" in str(a.get("name") or "")]
        raise TwitchDownloadError(
            f"当前平台 ({token}) 在 release {tag} 中无匹配的 TwitchDownloaderCLI zip。\n"
            f"  可用: {', '.join(names) or '(无)'}\n"
            "  请手动从 https://github.com/lay295/TwitchDownloader/releases 下载"
        )
    url = str(picked.get("browser_download_url") or "").strip()
    name = str(picked.get("name") or "").strip()
    if not url or not name:
        raise TwitchDownloadError("选中的 asset 缺少 name 或 browser_download_url")
    return tag, name, url


def _flatten_td_cli_into(dest: Path) -> Path | None:
    """If exe is nested under dest, keep using dest; return path to exe if found."""
    for name in td_exe_names():
        direct = dest / name
        if direct.is_file():
            return direct
    # Nested single top-level folder from zip
    try:
        children = [p for p in dest.iterdir() if p.is_dir() and not p.name.startswith(".")]
    except OSError:
        return None
    for child in children:
        for name in td_exe_names():
            cand = child / name
            if cand.is_file():
                # Move contents up one level for stable tools/TwitchDownloaderCLI/exe layout
                try:
                    for item in child.iterdir():
                        target = dest / item.name
                        if target.exists():
                            continue
                        item.rename(target)
                    try:
                        child.rmdir()
                    except OSError:
                        pass
                except OSError:
                    return cand
                flat = dest / name
                return flat if flat.is_file() else cand
    # Deep search (one extra level)
    for path in dest.rglob("*"):
        if path.is_file() and path.name in td_exe_names():
            return path
    return None


def try_portable_td_cli(
    *,
    assume_yes: bool = False,
    root: Path | None = None,
    timeout: float = 120.0,
) -> bool:
    """Download latest TwitchDownloaderCLI zip into tools/TwitchDownloaderCLI/.

    Requires network + user consent (unless assume_yes). Returns True if CLI is usable after.
    """
    from urllib.request import Request, urlopen

    root = root or _TOOLS_ROOT
    if find_twitchdownloader_cli(root):
        return True

    dest = root / "tools" / "TwitchDownloaderCLI"
    print("\n-- 自动安装 TwitchDownloaderCLI（便携）--")
    print(f"  目标目录: {dest}")
    print("  来源: GitHub lay295/TwitchDownloader releases/latest")
    print("  体积约数十 MB，需网络。")

    # Consent is usually already obtained by caller; still gate if not assume_yes
    # and we want a last confirmation — caller handles prompt; assume_yes means go.

    try:
        tag, asset_name, url = fetch_latest_td_cli_release_asset(timeout=min(30.0, timeout))
    except TwitchDownloadError as e:
        print(f"  [FAIL] {e}")
        return False

    print(f"  版本: {tag}")
    print(f"  资源: {asset_name}")
    print(f"  URL: {url}")

    dest.mkdir(parents=True, exist_ok=True)
    zip_path = dest / asset_name
    try:
        print("  下载中…")
        req = Request(url, headers={"User-Agent": "twitch-chat-cn-overlay"})
        with urlopen(req, timeout=timeout) as resp, open(zip_path, "wb") as out:  # noqa: S310
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
        print("  解压中…")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(dest)
        try:
            zip_path.unlink()
        except OSError:
            pass
    except Exception as e:
        print(f"  [FAIL] 下载/解压失败: {e}")
        print("  请手动从 https://github.com/lay295/TwitchDownloader/releases 下载")
        return False

    # Unix: ensure executable bit
    exe = _flatten_td_cli_into(dest)
    if exe and os.name != "nt":
        try:
            mode = exe.stat().st_mode
            exe.chmod(mode | 0o111)
        except OSError:
            pass

    found = find_twitchdownloader_cli(root)
    if found:
        print(f"  [OK] TwitchDownloaderCLI: {found}")
        return True
    print("  [FAIL] 解压后未找到 TwitchDownloaderCLI 可执行文件，请检查目录结构")
    return False
