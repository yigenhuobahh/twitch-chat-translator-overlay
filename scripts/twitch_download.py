#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Thin wrapper around TwitchDownloaderCLI for VOD/clip video + embedded chat HTML.

This project only consumes TwitchDownloader HTML with CSS-embedded emotes
(content:url(data:image...)). Chat download always uses --embed-images (-E).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import sys
import time
import uuid

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
from cut_timeline import CutTimeline, CutTimelineError
from process_util import run_tracked
from twitch_download_transaction import (
    preserved_staged_paths,
    resolve_download_targets,
)
from twitch_download_transaction import (
    publish_download_pair as _publish_download_pair,
)
from twitch_download_transaction import (
    recover_download_transaction as _recover_download_transaction,
)
from twitch_download_types import TwitchDownloadError

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


def new_download_session_dir(root: Path, slug: str) -> Path:
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    nonce = uuid.uuid4().hex[:8]
    return default_download_dir(root) / f"{slug}_{timestamp}_{nonce}"


def _new_download_staging_path(destination: Path) -> Path:
    """Return a unique sibling path that preserves the requested file suffix."""
    return destination.with_name(
        f".{destination.name}.download-{uuid.uuid4().hex}{destination.suffix}"
    )


def _reject_option_like(value: str | None, label: str) -> str:
    """Reject slot values that a .NET CLI would parse as its own options."""
    text = "" if value is None else str(value).strip()
    if text.startswith("-"):
        raise TwitchDownloadError(
            f"无效{label}: 不能以 '-' 开头（会被 TwitchDownloaderCLI 当作选项）: {text!r}"
        )
    return text


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
    source_id = _reject_option_like(source_id, "Twitch VOD/Clip ID")
    cmd = [str(cli), mode, "--id", source_id, "-o", str(output), "--collision", "Overwrite"]
    if quality:
        cmd.extend(["-q", _reject_option_like(quality, "下载画质")])
    if kind == "vod":
        if begin:
            cmd.extend(["-b", _reject_option_like(begin, "裁切开始时间")])
        if end:
            cmd.extend(["-e", _reject_option_like(end, "裁切结束时间")])
        trim = str(trim_mode or "Safe").strip().capitalize()
        if trim not in ("Safe", "Exact"):
            raise TwitchDownloadError(f"无效 trim mode: {trim_mode!r}（Safe|Exact）")
        # Safe avoids the known ~1s A/V desync from Exact crop + stream copy.
        cmd.extend(["--trim-mode", trim])
    if oauth:
        cmd.extend(["--oauth", _reject_option_like(oauth, "OAuth 令牌")])
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
        _reject_option_like(source_id, "Twitch VOD/Clip ID"),
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
        cmd.extend(["-b", _reject_option_like(begin, "裁切开始时间")])
    if end:
        cmd.extend(["-e", _reject_option_like(end, "裁切结束时间")])
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
    kind_r, source_id = parse_twitch_source(source, kind_hint=kind)
    if kind_r == "clip" and (begin or end):
        print(
            "  [提示] Clip 本身已是片段，忽略 --begin/--end（仅 VOD 支持裁切）",
            flush=True,
        )
        begin, end = None, None

    slug = slug_for_source(kind_r, source_id)
    base = Path(out_dir) if out_dir else new_download_session_dir(app_root, slug)
    try:
        from process_util import is_dangerous_publish_path

        if is_dangerous_publish_path(base) or is_dangerous_publish_path(base.parent):
            raise TwitchDownloadError(f"下载目录不能是系统路径: {base}")
    except ImportError:
        pass
    base.mkdir(parents=True, exist_ok=True)
    transaction_root, video_path, chat_path = resolve_download_targets(
        base,
        base / video_name,
        base / chat_name,
    )
    _recover_download_transaction(transaction_root)
    video_path.parent.mkdir(parents=True, exist_ok=True)
    chat_path.parent.mkdir(parents=True, exist_ok=True)

    cli = find_twitchdownloader_cli(tools_root)
    if cli is None:
        raise TwitchDownloadError(
            "未找到 TwitchDownloaderCLI。\n"
            "  1) 从 https://github.com/lay295/TwitchDownloader/releases 下载 CLI\n"
            "  2) 运行 --offer-td-cli 安装到可信工具目录\n"
            "  3) 或加入 PATH / 设置 TWITCHDOWNLOADER_CLI\n"
            "  安装结束时也可选择可选增强下载引导。"
        )

    staged_video = _new_download_staging_path(video_path)
    staged_chat = _new_download_staging_path(chat_path)
    repaired_video: Path | None = None

    try:
        # Prefer system/tools ffmpeg for TD video mux when available
        ffmpeg_path = safe_which("ffmpeg")

        vcmd = build_video_cmd(
            cli,
            kind=kind_r,
            source_id=source_id,
            output=staged_video,
            quality=quality,
            begin=begin if kind_r == "vod" else None,
            end=end if kind_r == "vod" else None,
            oauth=oauth,
            ffmpeg_path=ffmpeg_path,
            trim_mode=trim_mode,
        )
        _run_cli(vcmd, label="视频下载")
        if not staged_video.is_file():
            raise TwitchDownloadError(f"视频下载未生成新的指定文件: {video_path}")
        from media_health import repair_media, validate_media_health
        health = validate_media_health(staged_video, mode=media_check, require_audio=True)
        _print_media_health_warnings(health)
        video_to_publish = staged_video
        if not health.ok and str(media_repair or "off").lower() == "audio":
            try:
                repaired_video = repair_media(staged_video)
                health = validate_media_health(
                    repaired_video,
                    mode=media_check,
                    require_audio=True,
                )
                _print_media_health_warnings(health)
                if health.ok:
                    video_to_publish = repaired_video
            except (OSError, RuntimeError) as e:
                raise TwitchDownloadError(f"下载视频修复失败，原文件未覆盖: {e}") from e
        if not health.ok:
            raise TwitchDownloadError("下载视频健康检查失败，已阻止继续下载聊天/翻译/渲染: " + health.reason())

        ccmd = build_chat_cmd(
            cli,
            source_id=source_id,
            output=staged_chat,
            begin=begin if kind_r == "vod" else None,
            end=end if kind_r == "vod" else None,
            embed=True,
        )
        _run_cli(ccmd, label="聊天 HTML 下载")
        if not staged_chat.is_file():
            raise TwitchDownloadError(f"聊天下载未生成新的指定文件: {chat_path}")

        validate_chat_html(staged_chat)
        _publish_download_pair(
            video_to_publish,
            video_path,
            staged_chat,
            chat_path,
            transaction_root=transaction_root,
        )
    finally:
        preserve_paths = preserved_staged_paths(transaction_root)
        for temporary in (staged_video, staged_chat, repaired_video):
            if temporary is None or preserve_paths is None:
                continue
            try:
                resolved_temporary = temporary.resolve(strict=False)
            except OSError:
                continue
            if resolved_temporary in preserve_paths:
                continue
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                print(f"  [WARN] 无法清理下载临时文件 {temporary}: {exc}", file=sys.stderr, flush=True)
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
        if not all(math.isfinite(part) for part in (h, m, s)):
            raise TwitchDownloadError(f"时间必须是有限数值: {value!r}")
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
    if not all(math.isfinite(part) for part in (h, mi, s)):
        raise TwitchDownloadError(f"时间必须是有限数值: {value!r}")
    # Disallow pure junk like "h" — already needs digits.
    if h == 0 and mi == 0 and s == 0 and not re.search(r"\d", text):
        raise TwitchDownloadError(f"无法解析时间: {value!r}")
    return h * 3600.0 + mi * 60.0 + s


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
    try:
        timeline = CutTimeline.from_ranges(ranges, total_duration)
    except CutTimelineError as exc:
        raise TwitchDownloadError(str(exc)) from exc
    return list(timeline.cuts)


_FFPROBE_TIMEOUT_SECONDS = 45.0


def _run_ffprobe(arguments: list[str]):
    import subprocess as _sp

    try:
        return _sp.run(
            [require_executable("ffprobe"), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_FFPROBE_TIMEOUT_SECONDS,
        )
    except (OSError, _sp.TimeoutExpired):
        return None


def probe_media_duration(path: Path) -> float:
    """ffprobe format duration (seconds). Local helper — avoids importing burn."""
    if not path.is_file():
        raise TwitchDownloadError(f"无法探测时长，文件不存在: {path}")
    probe = _run_ffprobe(
        [
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ]
    )
    if probe is None:
        raise TwitchDownloadError(f"ffprobe 启动失败或超时: {path}")
    raw = (probe.stdout or "").strip().splitlines()
    if probe.returncode != 0 or not raw:
        err = (probe.stderr or probe.stdout or "ffprobe failed").strip()[:400]
        raise TwitchDownloadError(f"无法读取视频时长: {path}: {err}")
    try:
        duration = float(raw[0].strip() or 0.0)
    except ValueError as exc:
        raise TwitchDownloadError(f"无法解析视频时长 {raw[0]!r}: {exc}") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise TwitchDownloadError(f"视频时长无效 ({duration}): {path}")
    return duration


def get_stream_start_time(path: Path, stream_selector: str) -> float:
    """Return a stream start_time in seconds, defaulting to 0 when absent."""
    probe = _run_ffprobe(
        [
            "-v",
            "error",
            "-select_streams",
            stream_selector,
            "-show_entries",
            "stream=start_time",
            "-of",
            "csv=p=0",
            str(path),
        ]
    )
    if probe is None:
        return 0.0
    raw = (probe.stdout or "").strip().splitlines()
    if probe.returncode != 0 or not raw:
        return 0.0
    try:
        value = float(raw[0].strip() or 0.0)
    except ValueError:
        return 0.0
    return value if math.isfinite(value) else 0.0


def probe_av_fingerprint(path: Path) -> tuple[str, str, str, str, str, str]:
    """Best-effort (vcodec, width, height, pix_fmt, acodec, sample_rate)."""
    import json as _json

    empty = ("", "", "", "", "", "")
    probe = _run_ffprobe(
        [
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,pix_fmt,sample_rate",
            "-of",
            "json",
            str(path),
        ]
    )
    if probe is None or probe.returncode != 0:
        return empty
    try:
        data = _json.loads(probe.stdout or "{}")
    except (TypeError, ValueError):
        return empty
    vcodec = width = height = pix = acodec = rate = ""
    for stream in data.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        if stream.get("codec_type") == "video" and not vcodec:
            vcodec = str(stream.get("codec_name") or "")
            width = str(stream.get("width") or "")
            height = str(stream.get("height") or "")
            pix = str(stream.get("pix_fmt") or "")
        elif stream.get("codec_type") == "audio" and not acodec:
            acodec = str(stream.get("codec_name") or "")
            rate = str(stream.get("sample_rate") or "")
    return (vcodec, width, height, pix, acodec, rate)


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
    base = Path(out_dir) if out_dir else new_download_session_dir(app_root, slug)
    try:
        from process_util import is_dangerous_publish_path

        if is_dangerous_publish_path(base) or is_dangerous_publish_path(base.parent):
            raise TwitchDownloadError(f"下载目录不能是系统路径: {base}")
    except ImportError:
        pass
    base.mkdir(parents=True, exist_ok=True)
    transaction_root, final_video, final_chat = resolve_download_targets(
        base,
        base / video_name,
        base / chat_name,
    )
    _recover_download_transaction(transaction_root)
    final_video.parent.mkdir(parents=True, exist_ok=True)
    final_chat.parent.mkdir(parents=True, exist_ok=True)

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
    try:
        timeline = CutTimeline.from_ranges(remove_ranges, timeline_duration)
    except CutTimelineError as exc:
        raise TwitchDownloadError(str(exc)) from exc
    cut_ranges = list(timeline.cuts)
    staged_video = _new_download_staging_path(final_video)
    staged_chat = _new_download_staging_path(final_chat)
    repaired_video: Path | None = None
    mode = ""

    try:
        mode = concat_videos(
            [s.video_path for s in seg_downloads],
            staged_video,
            list_path=base / "concat_list.txt",
            remove_ranges=cut_ranges,
            cut_timeline=timeline,
            output_fps=output_fps,
            encoder=encoder,
        )

        expected = timeline.remaining_duration
        from media_health import repair_media, validate_media_health
        health = validate_media_health(
            staged_video, mode=media_check, require_audio=True, expected_duration=expected
        )
        _print_media_health_warnings(health)
        video_to_publish = staged_video
        if not health.ok and str(media_repair or "off").lower() == "audio":
            try:
                repaired_video = repair_media(staged_video, encoder=encoder)
                health = validate_media_health(
                    repaired_video, mode=media_check, require_audio=True, expected_duration=expected
                )
                _print_media_health_warnings(health)
                if health.ok:
                    video_to_publish = repaired_video
            except (OSError, RuntimeError) as e:
                raise TwitchDownloadError(f"合并视频修复失败，原文件未覆盖: {e}") from e
        if not health.ok:
            raise TwitchDownloadError("合并视频健康检查失败，已阻止合并聊天/翻译/渲染: " + health.reason())

        print("-- 合并聊天时间轴 ...", flush=True)
        merge_chat_html(
            seg_downloads,
            source_id=source_id,
            out_path=staged_chat,
            remove_ranges=cut_ranges,
            cut_timeline=timeline,
        )
        _publish_download_pair(
            video_to_publish,
            final_video,
            staged_chat,
            final_chat,
            transaction_root=transaction_root,
        )
    finally:
        preserve_paths = preserved_staged_paths(transaction_root)
        for temporary in (staged_video, staged_chat, repaired_video):
            if temporary is None or preserve_paths is None:
                continue
            try:
                resolved_temporary = temporary.resolve(strict=False)
            except OSError:
                continue
            if resolved_temporary in preserve_paths:
                continue
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                print(f"  [WARN] 无法清理合并临时文件 {temporary}: {exc}", file=sys.stderr, flush=True)

    print(f"[OK] 合并视频: {final_video}  (mode={mode})", flush=True)
    print(f"[OK] 合并聊天: {final_chat}", flush=True)

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


# ---------------------------------------------------------------------------
# Split-module re-exports (W3-A1): the concat/chat-merge pipeline and the TD
# CLI installer moved to vod_merge.py / td_cli_install.py. They are re-exported
# here so ``import twitch_download as td`` consumers (scripts/*, tests) keep
# working unchanged, and patches on these twitch_download attributes stay
# effective for internal callers like download_assets_multi.
# ---------------------------------------------------------------------------
from td_cli_install import (  # noqa: F401
    _flatten_td_cli_into,
    fetch_latest_td_cli_release_asset,
    pick_td_cli_asset,
    platform_td_asset_token,
    try_portable_td_cli,
)
from vod_merge import (  # noqa: F401
    _ffmpeg_concat_list_line,
    concat_videos,
    extract_emote_css_rules,
    format_td_t_seconds,
    iter_comment_root_blocks,
    merge_chat_html,
    remap_comment_block,
)
