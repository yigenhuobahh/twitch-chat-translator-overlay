#!/usr/bin/env python3
"""UI-neutral task draft and validation for the Textual launcher."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from common_utils import detect_cjk_font, load_dotenv_if_present, safe_which
from env_bootstrap import prepend_tools_ffmpeg_to_path
from job_config import load_job_file, write_job_file
from pipeline_plan import PipelinePlan

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
CHAT_EXTENSIONS = {".html", ".htm"}


# 3 Core Operational Paths (Primary Task Modes)
MODE_QUICK_PREVIEW_ORIGINAL = "quick_preview_original"
MODE_QUICK_PREVIEW_TRANSLATED = "quick_preview_translated"
MODE_FULL_PRODUCTION = "full_production"
MODE_ORIGINAL_PRODUCTION = "original_production"
MODE_STEP_EXPORT_MANUAL = "step_export_manual"
MODE_STEP_API_AND_REVIEW = "step_api_and_review"
MODE_STEP_RESUME_RENDER = "step_resume_render"

# Legacy Mode Constants (for 100% backward compatibility)
MODE_ORIGINAL_PREVIEW = "original_preview"
MODE_TRANSLATED_PREVIEW = "translated_preview"
MODE_FULL_RENDER = "full_render"
MODE_REUSE_RENDER = "reuse_render"
MODE_TRANSLATE_ONLY = "translate_only"
MODE_RENDER_ONLY = "render_only"
MODE_AUTO = "auto"
MODE_RENDER_ORIGINAL = "render_original"

CORE_MODES = (
    MODE_QUICK_PREVIEW_ORIGINAL,
    MODE_QUICK_PREVIEW_TRANSLATED,
    MODE_FULL_PRODUCTION,
    MODE_ORIGINAL_PRODUCTION,
    MODE_STEP_EXPORT_MANUAL,
    MODE_STEP_API_AND_REVIEW,
    MODE_STEP_RESUME_RENDER,
)

LEGACY_MODES = (
    MODE_ORIGINAL_PREVIEW,
    MODE_TRANSLATED_PREVIEW,
    MODE_FULL_RENDER,
    MODE_REUSE_RENDER,
    MODE_TRANSLATE_ONLY,
    MODE_RENDER_ONLY,
    MODE_AUTO,
    MODE_RENDER_ORIGINAL,
)

MODES = (
    *CORE_MODES,
    *LEGACY_MODES,
)

# Canonical job-field projection per task mode.  This is the single source of
# truth for to_job_fields so the mode→flags mapping cannot drift between the
# YAML projection and the translation requirement below.
_MODE_JOB_FIELDS: dict[str, dict[str, Any]] = {
    MODE_QUICK_PREVIEW_ORIGINAL: {"mode": "preview", "render_original": True},
    MODE_QUICK_PREVIEW_TRANSLATED: {"mode": "preview"},
    MODE_FULL_PRODUCTION: {"mode": "full"},
    MODE_ORIGINAL_PRODUCTION: {"mode": "render", "render_original": True},
    MODE_STEP_EXPORT_MANUAL: {"mode": "translate", "manual_translation": True},
    MODE_STEP_API_AND_REVIEW: {"mode": "full", "review": True},
    MODE_STEP_RESUME_RENDER: {"mode": "render", "reuse_translation": True},
    MODE_ORIGINAL_PREVIEW: {"mode": "preview", "render_original": True},
    MODE_TRANSLATED_PREVIEW: {"mode": "preview"},
    MODE_FULL_RENDER: {"mode": "full"},
    MODE_REUSE_RENDER: {"mode": "render", "reuse_translation": True},
    MODE_TRANSLATE_ONLY: {"mode": "translate"},
    MODE_RENDER_ONLY: {"mode": "render"},
    MODE_AUTO: {"mode": "auto"},
    MODE_RENDER_ORIGINAL: {"mode": "render", "render_original": True},
}
_MODE_JOB_FIELDS_DEFAULT = {"mode": "full"}

# Preview modes additionally carry the requested preview window.
_PREVIEW_MODES = frozenset({
    MODE_QUICK_PREVIEW_ORIGINAL,
    MODE_QUICK_PREVIEW_TRANSLATED,
    MODE_ORIGINAL_PREVIEW,
    MODE_TRANSLATED_PREVIEW,
})

# Modes that call the translation API unless a manual/render/reuse override
# short-circuits it first (see TuiJobDraft.requires_translation).
_TRANSLATION_REQUIRED_MODES = frozenset({
    MODE_QUICK_PREVIEW_TRANSLATED,
    MODE_TRANSLATED_PREVIEW,
    MODE_FULL_PRODUCTION,
    MODE_FULL_RENDER,
    MODE_STEP_API_AND_REVIEW,
    MODE_TRANSLATE_ONLY,
    MODE_AUTO,
})

_FORM_FIELDS = {
    "video", "chat_html", "output", "translation_json", "mode", "render_original", "reuse_translation",
    "target_language", "layout_preset", "render_preset", "preview_clip", "profile", "rules", "encoder",
    "crf", "workers", "source_media_check", "keep_temp", "review", "manual_translation", "offset",
}
_SENSITIVE_FIELD_PARTS = ("apikey", "token", "password", "authorization", "secret", "oauth")


def _is_sensitive_field(name: object) -> bool:
    normalized = "".join(character for character in str(name).lower() if character.isalnum())
    return any(part in normalized for part in _SENSITIVE_FIELD_PARTS)


def _clean_path(value: str) -> str:
    return value.strip().strip('"').strip("'")


def sanitize_download_source_for_history(value: object) -> str:
    """Keep a reusable Twitch source while dropping URL credentials and fragments."""
    source = str(value or "").strip()
    if not source:
        return ""
    try:
        # Known Twitch VOD and Clip URLs become their stable public ID/slug.
        # This is both more portable and avoids persisting query-string tokens.
        from twitch_download import parse_twitch_source

        _kind, normalized = parse_twitch_source(source, kind_hint="auto")
        source = str(normalized).strip()
    except Exception:
        pass
    parsed = urlsplit(source)
    if parsed.scheme and parsed.netloc:
        # Strip userinfo (user:pass@) before persisting; the credential part of
        # a host is never needed to re-run a download and must not survive.
        host = parsed.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    return source.split("#", 1)[0]


def _mode_from_fields(fields: dict[str, Any]) -> str:
    mode = str(fields.get("mode", "")).lower()
    # Direct match on core task modes
    if mode in (MODE_QUICK_PREVIEW_ORIGINAL, "quick_preview_original"):
        return MODE_QUICK_PREVIEW_ORIGINAL
    if mode in (MODE_QUICK_PREVIEW_TRANSLATED, "quick_preview_translated"):
        return MODE_QUICK_PREVIEW_TRANSLATED
    if mode in (MODE_FULL_PRODUCTION, "full_production"):
        return MODE_FULL_PRODUCTION
    if mode in (MODE_ORIGINAL_PRODUCTION, "original_production"):
        return MODE_ORIGINAL_PRODUCTION
    if mode in (MODE_STEP_EXPORT_MANUAL, "step_export_manual"):
        return MODE_STEP_EXPORT_MANUAL
    if mode in (MODE_STEP_API_AND_REVIEW, "step_api_and_review"):
        return MODE_STEP_API_AND_REVIEW
    if mode in (MODE_STEP_RESUME_RENDER, "step_resume_render"):
        return MODE_STEP_RESUME_RENDER

    if mode == "preview":
        return MODE_ORIGINAL_PREVIEW if fields.get("render_original") else MODE_TRANSLATED_PREVIEW
    if mode == "translate":
        if fields.get("manual_translation"):
            return MODE_STEP_EXPORT_MANUAL
        return MODE_TRANSLATE_ONLY
    if mode == "render":
        return MODE_REUSE_RENDER if fields.get("reuse_translation") else MODE_RENDER_ONLY
    if mode == "full":
        return MODE_FULL_RENDER
    if mode == "auto":
        return MODE_AUTO
    if fields.get("render_original"):
        return MODE_ORIGINAL_PREVIEW
    if fields.get("reuse_translation"):
        return MODE_REUSE_RENDER
    return MODE_FULL_RENDER


@dataclass
class TuiJobDraft:
    """A small, explicit view of the existing job.yaml contract.

    The draft deliberately owns no rendering behavior.  It only converts form
    values to the flags and YAML fields already understood by the pipeline.
    """

    video: str = ""
    chat_html: str = ""
    mode: str = MODE_ORIGINAL_PREVIEW
    output: str = ""
    translation_json: str = ""
    target_language: str = "zh"
    layout_preset: str = "default"
    render_preset: str = "default"
    preview_clip: float = 10.0
    profile: str = ""
    rules: str = ""
    encoder: str = ""
    crf: str = ""
    workers: str = ""
    source_media_check: str = "decode"
    keep_temp: bool = False
    review: bool = False
    manual_translation: bool = False
    render_original: bool = False
    reuse_translation: bool = False
    offset: str = ""
    source_job: str = ""
    extra_fields: dict[str, Any] | None = None

    @classmethod
    def from_job_file(cls, path: str | Path) -> TuiJobDraft:
        fields = load_job_file(path)
        return cls.from_fields(fields, source_job=str(Path(path).resolve()))

    @classmethod
    def from_fields(cls, fields: dict[str, Any], *, source_job: str = "") -> TuiJobDraft:
        def text(name: str, default: str = "") -> str:
            value = fields.get(name, default)
            return "" if value is None else str(value)

        raw_preview_clip = fields.get("preview_clip", 10)
        if raw_preview_clip in (None, ""):
            raw_preview_clip = 10
        try:
            preview_clip = float(raw_preview_clip)
        except (TypeError, ValueError) as exc:
            raise ValueError("preview_clip must be a number") from exc

        offset_val = fields.get("offset")
        offset_str = "" if offset_val is None else str(offset_val)

        return cls(
            video=text("video"),
            chat_html=text("chat_html"),
            mode=_mode_from_fields(fields),
            output=text("output"),
            translation_json=text("translation_json"),
            target_language=text("target_language", "zh"),
            layout_preset=text("layout_preset", "default"),
            render_preset=text("render_preset", "default"),
            preview_clip=preview_clip,
            profile=text("profile"),
            rules=text("rules"),
            encoder=text("encoder"),
            crf=text("crf"),
            workers=text("workers"),
            source_media_check=text("source_media_check", "decode").strip().lower() or "decode",
            keep_temp=bool(fields.get("keep_temp", False)),
            review=bool(fields.get("review", False)),
            manual_translation=bool(fields.get("manual_translation", False)),
            render_original=bool(fields.get("render_original", False)),
            reuse_translation=bool(fields.get("reuse_translation", False)),
            offset=offset_str,
            source_job=source_job or text("_job_path"),
            extra_fields={
                key: value
                for key, value in fields.items()
                if not key.startswith("_") and key not in _FORM_FIELDS and not _is_sensitive_field(key)
            },
        )

    def to_job_fields(self) -> dict[str, Any]:
        """Return canonical keys suitable for ``write_job_file``."""
        fields: dict[str, Any] = {
            key: value for key, value in (self.extra_fields or {}).items() if not _is_sensitive_field(key)
        }
        fields.update({
            "video": _clean_path(self.video),
            "chat_html": _clean_path(self.chat_html),
            "output": _clean_path(self.output),
            "translation_json": _clean_path(self.translation_json),
            "target_language": self.target_language.strip() or "zh",
            "layout_preset": self.layout_preset.strip() or "default",
            "render_preset": self.render_preset.strip() or "default",
            "profile": _clean_path(self.profile),
            "rules": _clean_path(self.rules),
            "source_media_check": self.source_media_check.strip().lower() or "decode",
            "keep_temp": self.keep_temp,
            "review": self.review,
            "manual_translation": self.manual_translation,
        })
        fields.update(dict(_MODE_JOB_FIELDS.get(self.mode, _MODE_JOB_FIELDS_DEFAULT)))
        if self.mode in _PREVIEW_MODES:
            fields["preview_clip"] = self.preview_clip
        if self.render_original:
            fields["render_original"] = True
        if self.reuse_translation:
            fields["reuse_translation"] = True
        if self.encoder.strip():
            fields["encoder"] = self.encoder.strip()
        if self.crf.strip():
            fields["crf"] = int(self.crf)
        if self.workers.strip():
            fields["workers"] = int(self.workers)
        if self.offset is not None and str(self.offset).strip():
            try:
                fields["offset"] = float(str(self.offset).strip())
            except (TypeError, ValueError):
                pass
        return {key: value for key, value in fields.items() if value not in (None, "")}

    def requires_translation(self) -> bool:
        if self.manual_translation or self.render_original or self.reuse_translation:
            return False
        return self.mode in _TRANSLATION_REQUIRED_MODES

    def validate(self, *, check_api: bool = True, check_environment: bool = True) -> list[str]:
        """Return user-facing validation errors without changing files."""
        problems: list[str] = []
        video = Path(_clean_path(self.video)).expanduser()
        chat = Path(_clean_path(self.chat_html)).expanduser()
        if not self.video.strip() or not video.is_file():
            problems.append("请选择存在的源视频文件。")
        elif video.suffix.lower() not in VIDEO_EXTENSIONS:
            problems.append("源视频格式不受支持；请选择常见视频文件。")
        if not self.chat_html.strip() or not chat.is_file():
            problems.append("请选择存在的 Twitch 聊天 HTML 文件。")
        elif chat.suffix.lower() not in CHAT_EXTENSIONS:
            problems.append("聊天文件必须是 .html 或 .htm。")
        if self.mode not in MODES:
            problems.append("请选择一个受支持的任务模式。")
        try:
            if float(self.preview_clip) <= 0:
                problems.append("预览时长必须大于 0。")
        except (TypeError, ValueError):
            problems.append("预览时长必须是数字。")
        for name, value in (("CRF", self.crf), ("翻译并发", self.workers)):
            if value.strip():
                try:
                    if int(value) <= 0:
                        problems.append(f"{name} 必须是正整数。")
                except ValueError:
                    problems.append(f"{name} 必须是整数。")
        if self.offset is not None and str(self.offset).strip():
            try:
                val = float(str(self.offset).strip())
                if not math.isfinite(val) or abs(val) > 604800.0:
                    problems.append("时间偏移秒数绝对值必须在 7 天以内（<= 604800 秒）。")
            except (TypeError, ValueError):
                problems.append("时间偏移必须是数字（支持正负小数与整数）。")
        if self.output.strip():
            output = Path(_clean_path(self.output)).expanduser()
            if output.exists() and output.is_dir():
                problems.append("输出路径不能是文件夹。")
            elif output.suffix.lower() not in {".mp4", ".mkv", ".mov"}:
                problems.append("输出文件建议使用 .mp4、.mkv 或 .mov 后缀。")
            elif self.video.strip() and output.resolve() == video.resolve():
                problems.append("输出文件不能与源视频相同；请选择新的文件名，避免覆盖原片。")
        if (self.mode in (MODE_REUSE_RENDER, MODE_STEP_RESUME_RENDER) or self.reuse_translation) and not Path(
            _clean_path(self.translation_json)
        ).expanduser().is_file():
            problems.append("复用翻译渲染需要选择已存在的翻译 JSON。")
        if (self.render_original or self.mode in (MODE_ORIGINAL_PREVIEW, MODE_QUICK_PREVIEW_ORIGINAL, MODE_ORIGINAL_PRODUCTION, MODE_RENDER_ORIGINAL)) and (self.reuse_translation or self.mode in (MODE_REUSE_RENDER, MODE_STEP_RESUME_RENDER)):
            problems.append("原文渲染不能同时复用翻译；请只选择一种渲染来源。")
        if (self.review or self.mode == MODE_STEP_API_AND_REVIEW) and (self.manual_translation or self.mode == MODE_STEP_EXPORT_MANUAL):
            problems.append("请选择人工复核或手工翻译其中一种，不要同时启用。")
        if self.mode in (MODE_ORIGINAL_PREVIEW, MODE_QUICK_PREVIEW_ORIGINAL) and (self.profile.strip() or self.rules.strip() or self.review or self.manual_translation):
            problems.append("原文预览不能同时使用翻译 profile、规则或人工翻译/复核。")
        if self.source_media_check.strip().lower() not in {"off", "fast", "decode"}:
            problems.append("输入视频检查只能选 off、fast 或 decode。")
        if self.manual_translation and self.mode in (MODE_ORIGINAL_PREVIEW, MODE_QUICK_PREVIEW_ORIGINAL, MODE_REUSE_RENDER, MODE_STEP_RESUME_RENDER):
            problems.append("手工翻译只能从翻译流程开始，不能与原文或复用翻译渲染一起使用。")
        if check_environment:
            prepend_tools_ffmpeg_to_path()
            if not safe_which("ffmpeg") or not safe_which("ffprobe"):
                problems.append("未找到 FFmpeg/ffprobe；请先运行环境检查。")
            regular_font, _ = detect_cjk_font()
            if not regular_font:
                problems.append("未找到可用 CJK 字体；请先运行环境检查。")
        if check_api and self.requires_translation():
            load_dotenv_if_present()
            modern = ("OPENAI_COMPAT_BASE_URL", "OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_MODEL")
            legacy = ("AGNES_BASE_URL", "AGNES_API_KEY", "AGNES_MODEL")
            if not (all(os.environ.get(name, "").strip() for name in modern) or all(os.environ.get(name, "").strip() for name in legacy)):
                problems.append("翻译模式需要在 .env 中配置翻译服务；可先运行环境检查。")
        return problems

    def warnings(self) -> list[str]:
        """Non-blocking explanations shown before a task is launched."""
        if self.output.strip() and Path(_clean_path(self.output)).expanduser().is_file():
            return ["输出文件已存在；pipeline 会保留备份并避免直接覆盖。"]
        return []

    def build_command(
        self,
        python: str,
        pipeline: str | Path,
        *,
        job_path: str | Path | None = None,
    ) -> list[str]:
        """Build a pipeline command from the canonical existing CLI options."""
        return PipelinePlan(self.to_job_fields(), source_job=self.source_job).build_command(
            python,
            pipeline,
            job_path=job_path,
        )

    def save_job(self, path: str | Path, *, pin_paths: bool = True, overwrite: bool = False) -> Path:
        return write_job_file(path, self.to_job_fields(), title=Path(path).stem, pin_paths=pin_paths, overwrite=overwrite)


@dataclass
class TuiDownloadDraft:
    """Small UI adapter for the existing TwitchDownloaderCLI-backed flow."""

    source: str = ""
    download_dir: str = ""
    quality: str = "1080p60"
    segments_text: str = ""
    media_check: str = "decode"
    oauth: str = ""
    authentication_required: bool = False

    def segments(self) -> list[str]:
        return [item.strip() for item in self.segments_text.replace(";", "\n").splitlines() if item.strip()]

    def requested_duration_s(self) -> float | None:
        """Return the requested VOD-window duration when segments are valid.

        TwitchDownloaderCLI may expand a short request to HLS segment boundaries,
        so callers must treat this as the user's requested duration rather than
        a promise about the downloaded file's duration.
        """
        if not self.segments():
            return None
        try:
            from twitch_download import parse_segment_line

            parsed = [parse_segment_line(item) for item in self.segments()]
            if any(segment is None for segment in parsed):
                return None
            return sum(float(segment.end_s - segment.begin_s) for segment in parsed if segment is not None)
        except Exception:
            return None

    def validate(self) -> list[str]:
        problems: list[str] = []
        source_kind: str | None = None
        if not self.source.strip():
            problems.append("请输入公开 Twitch VOD/Clip 链接或数字 ID。")
        else:
            try:
                from twitch_download import parse_twitch_source

                source_kind, _ = parse_twitch_source(self.source.strip(), kind_hint="auto")
                if source_kind != "vod" and self.segments():
                    problems.append("多段裁切只支持 VOD，不支持 Clip。")
            except Exception:
                problems.append("Twitch 链接或 ID 无法识别。")
        if not self.quality.strip():
            problems.append("请选择下载画质，例如 1080p60 或 720p60。")
        if self.media_check.strip().lower() not in {"off", "fast", "decode"}:
            problems.append("下载视频检查只能选 off、fast 或 decode。")
        if self.segments():
            try:
                from twitch_download import parse_segment_line, validate_segments

                parsed = [parse_segment_line(item) for item in self.segments()]
                if any(item is None for item in parsed):
                    raise ValueError("invalid segment")
                validate_segments([item for item in parsed if item is not None])
            except Exception:
                problems.append("裁切段格式无效；每行使用 1:00:00-1:00:08。")
        if self.download_dir.strip():
            target = Path(_clean_path(self.download_dir)).expanduser()
            if target.exists() and not target.is_dir():
                problems.append("下载目录不能是已有文件。")
        return problems

    def build_command(self, python: str, pipeline: str | Path) -> list[str]:
        command = [
            python,
            str(pipeline),
            "--download",
            self.source.strip(),
            "--download-only",
            "--quality",
            self.quality.strip(),
            "--media-check",
            self.media_check.strip().lower() or "decode",
            "--yes",
        ]
        if self.download_dir.strip():
            command.extend(["--download-dir", _clean_path(self.download_dir)])
        if self.oauth.strip():
            command.extend(["--oauth", self.oauth.strip()])
        for segment in self.segments():
            command.extend(["--segment", segment])
        return command

    def to_history_fields(self) -> dict[str, Any]:
        return {
            "_tui_task_type": "download",
            "download": sanitize_download_source_for_history(self.source),
            "download_dir": _clean_path(self.download_dir),
            "quality": self.quality.strip(),
            "segments": self.segments_text.strip(),
            "media_check": self.media_check.strip().lower() or "decode",
            "authentication_required": bool(self.oauth.strip() or self.authentication_required),
        }

    @classmethod
    def from_history_fields(cls, fields: dict[str, Any]) -> TuiDownloadDraft | None:
        if fields.get("_tui_task_type") != "download":
            return None
        return cls(
            source=str(fields.get("download") or ""),
            download_dir=str(fields.get("download_dir") or ""),
            quality=str(fields.get("quality") or "1080p60"),
            segments_text=str(fields.get("segments") or ""),
            media_check=str(fields.get("media_check") or "decode"),
            authentication_required=bool(fields.get("authentication_required", False)),
        )
