#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Load job.yaml for scenarioized pipeline runs (CLI still wins over job values)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None

from common_utils import current_cli_invocation

# Canonical argparse attribute names accepted from job YAML (kebab or snake).
JOB_FIELD_ALIASES: dict[str, str] = {
    "video": "video",
    "chat_html": "chat_html",
    "chat": "chat_html",
    "chat-html": "chat_html",
    "html": "chat_html",
    "output": "output",
    "workdir": "workdir",
    "work-dir": "workdir",
    "translation_json": "translation_json",
    "translation-json": "translation_json",
    "layout_preset": "layout_preset",
    "layout-preset": "layout_preset",
    "render_preset": "render_preset",
    "render-preset": "render_preset",
    "profile": "profile",
    "rules": "rules",
    "render_original": "render_original",
    "render-original": "render_original",
    "reuse_translation": "reuse_translation",
    "reuse-translation": "reuse_translation",
    "preview_clip": "preview_clip",
    "preview-clip": "preview_clip",
    "preview_frame": "preview_frame",
    "preview-frame": "preview_frame",
    "preview_image": "preview_image",
    "preview-image": "preview_image",
    "preview_dense": "preview_dense",
    "preview-dense": "preview_dense",
    "offset": "offset",
    "mode": "mode",
    "target_language": "target_language",
    "target-language": "target_language",
    "context": "context",
    "encoder": "encoder",
    "overlay_codec": "overlay_codec",
    "overlay-codec": "overlay_codec",
    "fps": "fps",
    "output_fps": "output_fps",
    "output-fps": "output_fps",
    "crf": "crf",
    "video_preset": "video_preset",
    "video-preset": "video_preset",
    "video_bitrate": "video_bitrate",
    "video-bitrate": "video_bitrate",
    "maxrate": "maxrate",
    "bufsize": "bufsize",
    "audio_codec": "audio_codec",
    "audio-codec": "audio_codec",
    "audio_bitrate": "audio_bitrate",
    "audio-bitrate": "audio_bitrate",
    "webm_crf": "webm_crf",
    "webm-crf": "webm_crf",
    "webm_cpu_used": "webm_cpu_used",
    "webm-cpu-used": "webm_cpu_used",
    "batch_size": "batch_size",
    "batch-size": "batch_size",
    "workers": "workers",
    "font_path": "font_path",
    "font-path": "font_path",
    "font_bold_path": "font_bold_path",
    "font-bold-path": "font_bold_path",
    "font_size": "font_size",
    "font-size": "font_size",
    "bg_alpha": "bg_alpha",
    "bg-alpha": "bg_alpha",
    "x": "x",
    "y": "y",
    "width": "width",
    "w": "width",
    "height": "height",
    "h": "height",
    "max_visible": "max_visible",
    "max-visible": "max_visible",
    "msg_lifetime": "msg_lifetime",
    "msg-lifetime": "msg_lifetime",
    "stack_mode": "stack_mode",
    "stack-mode": "stack_mode",
    "keep_temp": "keep_temp",
    "keep-temp": "keep_temp",
    "skip_translate": "skip_translate",
    "skip-translate": "skip_translate",
    "manual_translation": "manual_translation",
    "manual-translation": "manual_translation",
    "review": "review",
    "review_done": "review_done",
    "review-done": "review_done",
    "review_tsv": "review_tsv",
    "review-tsv": "review_tsv",
    "review_xlsx": "review_xlsx",
    "review-xlsx": "review_xlsx",
    "lint_translation": "lint_translation",
    "lint-translation": "lint_translation",
    "lint_report": "lint_report",
    "lint-report": "lint_report",
    "no_backup_prev": "no_backup_prev",
    "no-backup-prev": "no_backup_prev",
    "no_reuse_static_frames": "no_reuse_static_frames",
    "no-reuse-static-frames": "no_reuse_static_frames",
    "no_skip_blank_frames": "no_skip_blank_frames",
    "no-skip-blank-frames": "no_skip_blank_frames",
    "blank_hold_seconds": "blank_hold_seconds",
    "blank-hold-seconds": "blank_hold_seconds",
    "lazy_message_images": "lazy_message_images",
    "lazy-message-images": "lazy_message_images",
    "message_image_cache_size": "message_image_cache_size",
    "message-image-cache-size": "message_image_cache_size",
    "x_ratio": "x_ratio",
    "x-ratio": "x_ratio",
    "y_ratio": "y_ratio",
    "y-ratio": "y_ratio",
    "width_ratio": "width_ratio",
    "width-ratio": "width_ratio",
    "height_ratio": "height_ratio",
    "height-ratio": "height_ratio",
    "font_size_ratio": "font_size_ratio",
    "font-size-ratio": "font_size_ratio",
    "emote_height": "emote_height",
    "emote-height": "emote_height",
    "max_message_lines": "max_message_lines",
    "max-message-lines": "max_message_lines",
    "min_visible_seconds": "min_visible_seconds",
    "min-visible-seconds": "min_visible_seconds",
    "arrival_interval": "arrival_interval",
    "arrival-interval": "arrival_interval",
    "force_export": "force_export",
    "force-export": "force_export",
    "strict_import": "strict_import",
    "strict-import": "strict_import",
}

# Paths resolved relative to the job file's directory when relative.
PATH_FIELDS = {
    "video",
    "chat_html",
    "output",
    "workdir",
    "translation_json",
    "layout_preset",
    "render_preset",
    "profile",
    "rules",
    "preview_image",
    "review_tsv",
    "review_xlsx",
    "lint_report",
    "font_path",
    "font_bold_path",
}

# Existing job-local files take priority. Unresolved names stay relative so the
# pipeline can search source and installed public resources.
PUBLIC_RESOURCE_FIELDS = {"profile", "rules"}

BOOL_FIELDS = {
    "render_original",
    "reuse_translation",
    "preview_dense",
    "keep_temp",
    "skip_translate",
    "manual_translation",
    "review",
    "review_done",
    "no_backup_prev",
    "no_reuse_static_frames",
    "no_skip_blank_frames",
    "lazy_message_images",
    "force_export",
    "strict_import",
}

# Fields that may be a bare bool flag OR a path string (lint-translation).
OPTIONAL_PATH_OR_BOOL = {"lint_translation"}


def _require_yaml() -> None:
    if yaml is None:
        raise ValueError("需要 PyYAML 才能加载 job.yaml：pip install PyYAML")


def _norm_key(key: str) -> str | None:
    k = str(key).strip()
    if k in JOB_FIELD_ALIASES:
        return JOB_FIELD_ALIASES[k]
    nk = k.replace("-", "_")
    if nk in JOB_FIELD_ALIASES.values():
        return nk
    return None


def _is_reserved_job_key(key: str) -> bool:
    """Internal keys (``_…``) stay silent; structural ``job:`` is handled separately."""
    return str(key).startswith("_")


def _warn_unknown_job_keys(unknown: list[str], *, source: str | Path | None = None) -> None:
    """Print a non-fatal bilingual warning listing unrecognized job YAML keys.

    Unknown keys are ignored (not applied). This is intentional soft-fail so older
    or slightly wrong configs still load; only the structural wrapper key ``job:``
    is treated as known nesting and never reported as unknown.
    """
    if not unknown:
        return
    # Stable order for tests / logs
    keys = ", ".join(sorted({str(k) for k in unknown}, key=lambda s: s.lower()))
    where = f" @ {source}" if source is not None else ""
    print(
        f"[job] 警告/WARN: 未识别的字段已忽略 (unknown keys ignored){where}: {keys}\n"
        "  提示: 可能是拼写错误，或当前版本不支持该字段 "
        "(typo? not supported? 见 JOB_FIELD_ALIASES / --help)。"
    )


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    raise ValueError(f"job 字段需要布尔值，收到 {value!r}")


def _resolve_path(value: Any, base_dir: Path) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Keep special sentinel / auto markers absolute-as-is.
    if s.lower() in ("auto", "__pipeline__"):
        return s
    p = Path(s)
    if p.is_absolute():
        return str(p)
    return str((base_dir / p).resolve())


def _resolve_job_resource(value: Any, base_dir: Path) -> str | None:
    """Resolve a real job-local file while preserving public resource names."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    p = Path(s).expanduser()
    if p.is_absolute():
        return str(p)
    job_relative = (base_dir / p).resolve()
    return str(job_relative) if job_relative.is_file() else s


_PLACEHOLDER_MARKERS = (
    "path/to/",
    "path\\to\\",
    "your_video",
    "your_chat",
    "example.mp4",
    "example.html",
    "视频.mp4",
    "聊天.html",
)


def is_placeholder_media_path(path: str | Path | None) -> bool:
    """True if path looks like an example/placeholder rather than a real file path."""
    if path is None:
        return True
    s = str(path).strip().replace("\\", "/").lower()
    if not s:
        return True
    for m in _PLACEHOLDER_MARKERS:
        if m.lower().replace("\\", "/") in s:
            return True
    # Generic path/to style
    if "/to/" in s and ("video" in s or "chat" in s or s.endswith(".mp4") or s.endswith(".html")):
        if "path/to" in s:
            return True
    return False


def validate_job_media_paths(
    job: dict[str, Any],
    *,
    require_existing: bool = True,
) -> list[str]:
    """Return human-readable Chinese problems for video/chat_html in a loaded job.

    Empty list means OK. Does not raise — callers decide hard-fail vs warn.
    """
    problems: list[str] = []
    video = job.get("video")
    chat = job.get("chat_html")
    if not video:
        problems.append("缺少 video（源视频路径）")
    elif is_placeholder_media_path(video):
        problems.append(
            f"video 仍是示例占位路径: {video}\n"
            "  请用「新建配置」填写真实视频，或编辑 jobs/*.yaml 后重试。\n"
            f"  提示: {current_cli_invocation()} --init-job"
        )
    elif require_existing and not Path(str(video)).is_file():
        problems.append(
            f"视频文件不存在: {video}\n"
            f"  请检查路径，或重新创建配置: {current_cli_invocation()} --init-job"
        )

    if not chat:
        problems.append("缺少 chat_html（TwitchDownloader 聊天 HTML）")
    elif is_placeholder_media_path(chat):
        problems.append(
            f"chat_html 仍是示例占位路径: {chat}\n"
            "  请用 TwitchDownloader 导出 HTML，写入配置后再运行。\n"
            f"  提示: {current_cli_invocation()} --init-job，或编辑 jobs/example_job.yaml"
        )
    elif require_existing and not Path(str(chat)).is_file():
        problems.append(
            f"聊天 HTML 不存在: {chat}\n"
            "  请确认 TwitchDownloader 导出了 HTML（不是仅 JSON）。"
        )
    return problems


def load_job_file(path: str | Path) -> dict[str, Any]:
    """Load a job YAML into a dict of canonical argparse field names.

    Relative paths are resolved against the job file's parent directory, except
    unresolved public profile/rules names, which remain available for source or
    installed-share lookup.
    Accepts kebab-case and snake_case keys; nested ``job:`` mapping is optional
    structural nesting (not a field — never warned as unknown).

    Keys that do not normalize to a known field (via ``JOB_FIELD_ALIASES`` /
    ``_norm_key``) are ignored with a stdout warning; load still succeeds.
    """
    _require_yaml()
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"job 文件不存在: {path}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"job 根节点必须是 mapping: {p}")
    # Structural wrapper only: ``job:`` holds the field map. Sibling top-level
    # keys are not applied (and are warned as unknown when nested form is used).
    nested = isinstance(data.get("job"), dict)
    raw = data.get("job") if nested else data
    if not isinstance(raw, dict):
        raise ValueError(f"job 内容必须是 mapping: {p}")

    base_dir = p.resolve().parent
    out: dict[str, Any] = {}
    unknown: list[str] = []
    for key, value in raw.items():
        key_s = str(key)
        attr = _norm_key(key_s)
        if attr is None:
            # Internal / reserved prefixes stay silent if ever written by tools.
            if not _is_reserved_job_key(key_s):
                unknown.append(key_s)
            continue
        if value is None:
            continue
        if attr in BOOL_FIELDS:
            out[attr] = _coerce_bool(value)
            continue
        if attr in OPTIONAL_PATH_OR_BOOL:
            if isinstance(value, bool):
                out[attr] = "__PIPELINE__" if value else None
            else:
                s = str(value).strip()
                if not s:
                    out[attr] = None
                elif s.lower() in ("true", "yes", "on", "1", "__pipeline__"):
                    out[attr] = "__PIPELINE__"
                else:
                    out[attr] = _resolve_path(s, base_dir)
            continue
        if attr in PATH_FIELDS:
            # Preset short names (e.g. "compact") are not paths; leave bare names
            # for layout/render resolvers, but resolve path-like values.
            s = str(value).strip()
            if attr in ("layout_preset", "render_preset", "profile") and (
                "/" not in s and "\\" not in s and not Path(s).suffix
            ):
                out[attr] = s
            elif attr in ("font_path", "font_bold_path") and s.lower() == "auto":
                out[attr] = "auto"
            elif attr in PUBLIC_RESOURCE_FIELDS:
                out[attr] = _resolve_job_resource(s, base_dir)
            else:
                out[attr] = _resolve_path(s, base_dir)
            continue
        if attr == "mode":
            mode = str(value).strip().lower()
            if mode not in ("auto", "preview", "translate", "render", "full"):
                raise ValueError(
                    f"job mode 必须是 auto|preview|translate|render|full，收到 {value!r}"
                )
            out[attr] = mode
            continue
        out[attr] = value

    if nested:
        for key in data:
            key_s = str(key)
            if key_s == "job" or _is_reserved_job_key(key_s):
                continue
            unknown.append(key_s)

    _warn_unknown_job_keys(unknown, source=p)
    out["_job_path"] = str(p.resolve())
    out["_job_dir"] = str(base_dir)
    return out


def apply_job_to_namespace(
    args,
    job: dict[str, Any],
    cli_defaults: dict[str, Any] | None = None,
) -> list[str]:
    """Fill argparse namespace from job only where the value is still at CLI default.

    Explicit CLI flags always win (same contract as layout/render presets).
    Returns list of applied field names.
    """
    cli_defaults = dict(cli_defaults or {})
    applied: list[str] = []
    skip_meta = {"_job_path", "_job_dir"}
    for key, value in job.items():
        if key in skip_meta:
            continue
        if not hasattr(args, key):
            continue
        current = getattr(args, key)
        if key in cli_defaults:
            default = cli_defaults[key]
            if current != default:
                continue
        else:
            # Unknown default: only fill when unset / False for bool flags that
            # store_true typically defaults to False.
            if current is not None and current is not False:
                continue
        setattr(args, key, value)
        applied.append(key)
    return applied


def default_jobs_dir(cwd: Path | None = None) -> Path:
    """jobs/ under cwd (preferred) or repo root next to scripts/."""
    base = Path(cwd) if cwd is not None else Path.cwd()
    local = base / "jobs"
    if local.is_dir():
        return local
    repo_jobs = Path(__file__).resolve().parent.parent / "jobs"
    if repo_jobs.is_dir():
        return repo_jobs
    return local


def list_job_files(jobs_dir: str | Path | None = None) -> list[Path]:
    """List *.yaml / *.yml job files (sorted by name), excluding dotfiles."""
    root = Path(jobs_dir) if jobs_dir is not None else default_jobs_dir()
    if not root.is_dir():
        return []
    files = [
        p
        for p in root.iterdir()
        if p.is_file()
        and p.suffix.lower() in (".yaml", ".yml")
        and not p.name.startswith(".")
    ]
    return sorted(files, key=lambda p: p.name.lower())


def summarize_job(path: str | Path) -> str:
    """One-line human summary for menus: mode, flags, video basename.

    Uses a light YAML load (no path resolution) — listing many jobs should stay cheap.
    """
    p = Path(path)
    try:
        _require_yaml()
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            return f"{p.stem}: (格式无效)"
        data = raw.get("job") if isinstance(raw.get("job"), dict) else raw
        if not isinstance(data, dict):
            return f"{p.stem}: (格式无效)"
    except Exception as e:
        return f"{p.stem}: (无法读取: {type(e).__name__})"

    def _get(*keys):
        for k in keys:
            if k in data and data[k] is not None:
                return data[k]
        return None

    # Human-readable one-liner for menus (Chinese labels, less jargon).
    mode = str(_get("mode") or "auto").strip().lower()
    mode_cn = {
        "preview": "预览原文" if _get("render_original", "render-original") else "预览",
        "full": "翻译出片",
        "auto": "完整流程",
        "translate": "只翻译",
        "render": "只渲染",
    }.get(mode, mode)

    if mode == "preview" and _get("render_original", "render-original"):
        mode_cn = "预览原文"
    elif mode == "render" and _get("reuse_translation", "reuse-translation"):
        mode_cn = "复用翻译渲染"
    elif mode == "full":
        mode_cn = "翻译出片"

    bits: list[str] = [mode_cn]
    layout = _get("layout_preset", "layout-preset")
    if layout:
        bits.append(f"布局 {layout}")
    render = _get("render_preset", "render-preset")
    if render:
        bits.append(f"编码 {render}")
    clip = _get("preview_clip", "preview-clip")
    if clip is not None and mode in ("preview", "auto", "full", "render"):
        try:
            bits.append(f"{float(clip):g}秒预览")
        except (TypeError, ValueError):
            bits.append(f"clip={clip}")
    video = _get("video")
    if video:
        bits.append(Path(str(video)).name)
    else:
        bits.append("路径每次询问")
    return f"{p.stem}  —  " + " · ".join(bits)


def resolve_job_arg(name_or_path: str, jobs_dir: str | Path | None = None) -> Path:
    """Resolve a job name or path to an existing YAML file.

    Order: exact path → jobs/<name>.yaml/.yml → jobs/<name>.
    """
    raw = str(name_or_path).strip().strip('"').strip("'")
    if not raw:
        raise ValueError("job 名称为空")
    p = Path(raw)
    if p.is_file():
        return p.resolve()
    root = Path(jobs_dir) if jobs_dir is not None else default_jobs_dir()
    # Bare short names: try the common suffixes first under jobs/.
    if "/" not in raw and "\\" not in raw:
        stem = Path(raw).stem if Path(raw).suffix else raw
        for c in (root / f"{stem}.yaml", root / f"{stem}.yml", root / raw):
            if c.is_file():
                return c.resolve()
    else:
        for c in (root / raw, root / Path(raw).name):
            if c.is_file():
                return c.resolve()
    raise ValueError(f"找不到 job 配置: {name_or_path}（在 {root} 下试过短名/路径）")


def last_job_path(jobs_dir: str | Path | None = None) -> Path | None:
    """Read jobs/.last_job if present and the target still exists."""
    root = Path(jobs_dir) if jobs_dir is not None else default_jobs_dir()
    marker = root / ".last_job"
    if not marker.is_file():
        return None
    try:
        text = marker.read_text(encoding="utf-8").strip().splitlines()
        if not text:
            return None
        target = Path(text[0].strip().strip('"'))
        if not target.is_file():
            # try relative to jobs dir
            alt = root / text[0].strip()
            if alt.is_file():
                return alt.resolve()
            return None
        return target.resolve()
    except OSError:
        return None


def save_last_job(path: str | Path, jobs_dir: str | Path | None = None) -> None:
    """Remember last used job path for one-click reuse."""
    root = Path(jobs_dir) if jobs_dir is not None else default_jobs_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / ".last_job").write_text(str(Path(path).resolve()) + "\n", encoding="utf-8")
    except OSError:
        pass


def _yaml_quote(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    s = str(value)
    if s == "":
        return '""'
    # Quote if special YAML chars / leading special.
    if any(c in s for c in (":", "#", "{", "}", "[", "]", ",", "&", "*", "?", "|", ">", "!", "%", "@", "`", "'", '"')) or s.strip() != s or s.lower() in ("null", "true", "false", "yes", "no"):
        esc = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{esc}"'
    return s


# Per-run media paths: normally NOT pinned in reusable job YAML.
# Comment them out by default; only write active lines when pin_paths=True.
SESSION_PATH_FIELDS = frozenset(
    {
        "video",
        "chat_html",
        "output",
        "translation_json",
        "preview_image",
        "review_tsv",
        "review_xlsx",
        "lint_report",
    }
)


def render_job_yaml(
    fields: dict[str, Any],
    *,
    title: str | None = None,
    pin_paths: bool = False,
) -> str:
    """Render a fully commented job YAML from a field dict (for wizard / templates).

    By default (pin_paths=False), media path keys (video/chat_html/output/…) are
    written only as commented examples so the same job style can be reused for
    many VODs. Uncomment (or pin_paths=True) to bake paths into the file.
    """
    # Order and comments for every supported key users commonly set.
    catalog: list[tuple[str, str]] = [
        ("video", "源视频路径。默认注释=每次运行询问，不写死；取消注释才固定跟配置走"),
        ("chat_html", "TwitchDownloader 聊天 HTML。默认注释=每次询问；取消注释才固定"),
        ("output", "最终成片路径；注释掉则默认 <视频名>_chat.mp4（按本次视频推导）"),
        ("workdir", "工作目录；中间文件进 workdir/temp（可写死或每次自动）"),
        ("translation_json", "翻译 JSON；注释掉则默认 <视频名>_translation.json"),
        ("mode", "场景: auto|preview|translate|render|full"),
        ("render_original", "true=不翻译，直接烧录原始聊天（首次预览推荐）"),
        ("reuse_translation", "true=复用已有 translation_json，跳过 API"),
        ("target_language", "翻译目标语言，如 zh / ja / ko"),
        ("context", "传给翻译模型的背景说明"),
        ("profile", "翻译 profile YAML（术语表/风格）"),
        ("rules", "翻译后按原文精确匹配覆盖译文的 YAML"),
        ("preview_clip", "只渲染 N 秒短片；mode=preview 时常用 10"),
        ("preview_frame", "只导出第 N 秒预览图"),
        ("preview_image", "预览图输出路径"),
        ("preview_dense", "与 preview_clip 联用：选弹幕最密段"),
        ("offset", "聊天时间戳偏移秒数；不填则自动检测，务必预览确认"),
        ("layout_preset", "布局短名或路径: default|compact|mobile"),
        ("render_preset", "编码短名或路径: default|fast|hq"),
        ("x", "弹幕框左上角 X（像素）"),
        ("y", "弹幕框左上角 Y（像素）"),
        ("width", "弹幕框宽度（像素）"),
        ("height", "弹幕框高度（像素）"),
        ("font_size", "字号（像素）"),
        ("font_path", "字体路径；auto=系统 CJK"),
        ("font_bold_path", "粗体字体；auto=自动"),
        ("bg_alpha", "背景不透明度 0-255"),
        ("max_visible", "最多同时显示条数；0=按框高自动"),
        ("msg_lifetime", "消息停留秒数（仅 lanes）"),
        ("stack_mode", "lanes=时间沉积；float=上浮顶出"),
        ("max_message_lines", "单条最多行数；0=不限制"),
        ("min_visible_seconds", "已上屏最短可见秒数（仅 lanes）"),
        ("arrival_interval", "新消息最小入场间隔秒数"),
        ("emote_height", "表情高度像素"),
        ("x_ratio", "相对视频宽的 X；>0 覆盖 x"),
        ("y_ratio", "相对视频高的 Y；>0 覆盖 y"),
        ("width_ratio", "相对视频宽的宽度"),
        ("height_ratio", "相对视频高的高度"),
        ("font_size_ratio", "相对视频高的字号"),
        ("fps", "弹幕层采样帧率（非成片帧率）"),
        ("output_fps", "成片帧率；不填跟随源视频"),
        ("encoder", "x264|nvenc|qsv|amf|auto"),
        ("video_preset", "编码器速度预设"),
        ("crf", "质量 CRF/CQ，越小越好"),
        ("overlay_codec", "弹幕中间层 vp9|png"),
        ("audio_codec", "aac|copy"),
        ("audio_bitrate", "音频码率，如 192k"),
        ("webm_crf", "VP9 CRF"),
        ("webm_cpu_used", "VP9 速度 0-8"),
        ("keep_temp", "true=保留中间临时文件"),
        ("no_backup_prev", "true=不备份旧输出"),
        ("lazy_message_images", "true=长片消息图 LRU 省内存"),
        ("message_image_cache_size", "lazy 缓存上限"),
        ("batch_size", "翻译批大小"),
        ("workers", "翻译并发数"),
    ]
    lines: list[str] = [
        "# =============================================================================",
        f"# 任务配置{(' — ' + title) if title else ''}",
        "# 布局/编码/用途可复用；视频与 HTML 默认不写死（见下方注释掉的 video/chat_html）。",
        "# 运行时若未配置路径，会询问本次文件位置，且不会写回本文件。",
        "# 只有取消注释的路径才会固定跟配置走。",
        f"# 运行: {current_cli_invocation()} --job <本文件>",
        "# =============================================================================",
        "",
    ]
    used = set(fields.keys())
    path_examples = {
        "video": "path/to/video.mp4",
        "chat_html": "path/to/chat.html",
        "output": "path/to/out_chat.mp4",
        "translation_json": "path/to/translations.json",
        "preview_image": "path/to/preview.png",
    }
    for key, comment in catalog:
        val = fields.get(key)
        is_path = key in SESSION_PATH_FIELDS
        # Session paths: only active if pin_paths and value provided
        if is_path and not pin_paths:
            lines.append(f"# {comment}")
            example = path_examples.get(key, "path/to/file")
            # If user provided a real value this session, show it as commented hint only
            if val not in (None, ""):
                lines.append(f"# {key}: {_yaml_quote(val)}  # 本次用过，未写入（取消注释可固定）")
            else:
                lines.append(f"# {key}: {example}")
            lines.append("")
            used.discard(key)
            continue
        if key not in fields or fields[key] is None:
            continue
        if val == "" and key not in ("context",):
            continue
        lines.append(f"# {comment}")
        lines.append(f"{key}: {_yaml_quote(val)}")
        lines.append("")
        used.discard(key)
    # Any extra keys
    for key in sorted(used):
        if key.startswith("_"):
            continue
        if key in SESSION_PATH_FIELDS and not pin_paths:
            lines.append(f"# (路径-默认注释) {key}: {_yaml_quote(fields[key])}")
            lines.append("")
            continue
        lines.append(f"# (自定义) {key}")
        lines.append(f"{key}: {_yaml_quote(fields[key])}")
        lines.append("")
    # Trailing reference comments for unused common knobs
    lines.extend(
        [
            "# --- 以下为常用可选项（保持注释即可；需要时取消注释并修改）---",
            "# layout_preset: compact   # 布局: default|compact|mobile",
            "# render_preset: fast      # 编码: default|fast|hq",
            "# offset: 7264             # 时间偏移秒数；预览确认后再写死",
            "# workdir: work/my_vod     # 可选；不设则用 jobs/<配置名>/",
            "# profile: profiles/default.yaml",
            "# rules: configs/rules.example.yaml",
            "",
        ]
    )
    return "\n".join(lines)


def write_job_file(
    path: str | Path,
    fields: dict[str, Any],
    *,
    title: str | None = None,
    overwrite: bool = False,
    pin_paths: bool = False,
) -> Path:
    """Write annotated job YAML. Raises FileExistsError unless overwrite.

    pin_paths=False (default): video/chat_html/output/… stay commented for reuse.
    pin_paths=True: write those paths as active YAML keys.
    """
    p = Path(path)
    if p.exists() and not overwrite:
        raise FileExistsError(f"job 已存在: {p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    text = render_job_yaml(fields, title=title or p.stem, pin_paths=pin_paths)
    p.write_text(text, encoding="utf-8")
    return p.resolve()
