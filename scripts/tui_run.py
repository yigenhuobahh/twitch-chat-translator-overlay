#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Full Textual launcher for local Twitch chat-overlay workflows."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time

from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Checkbox, Footer, Header, Input, RichLog, Static, TabbedContent, TabPane

from tui_history import TuiHistoryStore
from tui_models import (
    MODE_FULL_RENDER,
    MODE_ORIGINAL_PREVIEW,
    MODE_REUSE_RENDER,
    MODE_TRANSLATED_PREVIEW,
    TuiDownloadDraft,
    TuiJobDraft,
)
from tui_task import TaskSession, redact_command, sanitize_diagnostic_file


class OverlayTui(App[None]):
    """Beginner-first UI; rendering remains entirely in render_cn_chat.py."""

    CSS = """
    Screen { layout: vertical; }
    #status { height: 3; padding: 1; }
    TabbedContent { height: 1fr; }
    VerticalScroll { padding: 0 1; }
    Input { margin: 1 0; }
    Checkbox { margin: 1 0; }
    RichLog { height: 1fr; min-height: 12; border: round $accent; }
    Horizontal { height: auto; margin: 1 0; }
    Button { margin-right: 1; }
    .hint { color: $text-muted; margin: 1 0; }
    """
    TITLE = "Twitch Chat Overlay"

    def __init__(self) -> None:
        super().__init__()
        self.session: TaskSession | None = None
        self.last_draft: TuiJobDraft | None = None
        self.imported_draft: TuiJobDraft | None = None
        self.result_directory: Path | None = None
        self.completion_message = "任务完成。"
        self.history = TuiHistoryStore(Path(__file__).resolve().parent.parent / "outputs" / ".tui-history" / "history.json")
        self.active_history_id: str | None = None
        self._handled_session: TaskSession | None = None
        self.current_task_kind = "render"
        self.require_result_manifest = False
        self.download_requested_duration_s: float | None = None
        self.download_duration_note = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("选择本地视频和 Twitch 聊天 HTML，然后开始预览或正式渲染。", id="status")
        with TabbedContent(initial="new-task"):
            with TabPane("下载素材", id="download"):
                with VerticalScroll():
                    yield Static("下载使用已安装的 TwitchDownloaderCLI。VOD 为避免误下载整段内容，必须填写至少一个裁切段；Clip 本身可直接下载。多个 VOD 片段用分号分隔。短时间窗可能按 Twitch 的 HLS 分片边界扩展，完成后会提示实际时长。", classes="hint")
                    yield Input(placeholder="公开 Twitch VOD/Clip 链接或数字 ID", id="download-url")
                    yield Input(value="1080p60", placeholder="画质，例如 1080p60 / 720p60", id="download-quality")
                    yield Input(value="decode", placeholder="下载视频检查：decode / fast / off", id="download-media-check")
                    yield Static("默认完整解码每个片段和合并结果；这会花额外时间，但能在开始翻译前拦住损坏视频。", classes="hint")
                    yield Input(placeholder="下载目录（可选；留空自动创建）", id="download-dir")
                    yield Input(placeholder="裁切段：1:00:00-1:00:08; 1:00:20-1:00:28", id="download-segments")
                    yield Input(placeholder="OAuth（订阅限定 VOD；仅本次下载使用）", password=True, id="download-oauth")
                    yield Button("下载并载入新任务", id="download-start", variant="primary")
            with TabPane("新任务", id="new-task"):
                with VerticalScroll():
                    yield Static("选择一个操作。正式渲染可直接启动；首次使用建议先做原文预览。", classes="hint")
                    with Horizontal():
                        yield Button("原文预览", id="original-preview", variant="primary")
                        yield Button("翻译预览", id="translated-preview")
                    with Horizontal():
                        yield Button("正式翻译渲染", id="full-render", variant="success")
                        yield Button("复用翻译渲染", id="reuse-render")
                    yield Static("输入素材", classes="hint")
                    yield Input(placeholder="源视频路径 (.mp4/.mkv/...)", id="video")
                    yield Input(placeholder="TwitchDownloader 聊天 HTML 路径", id="chat")
                    yield Input(placeholder="输出视频路径（可选，默认源视频同目录）", id="output")
                    yield Input(value="10", placeholder="预览时长（秒）", id="preview-clip")
            with TabPane("任务与结果", id="task"):
                with VerticalScroll():
                    yield Static("结构化阶段事件和子进程输出会显示在这里。失败后可导出脱敏诊断。", classes="hint")
                    yield RichLog(id="log", wrap=True, highlight=False, markup=False)
                    with Horizontal():
                        yield Button("环境检查", id="doctor")
                        yield Button("离线演示", id="demo")
                        yield Button("取消任务", id="cancel", variant="warning")
                        yield Button("打开结果目录", id="open-result")
                        yield Button("导出诊断", id="export-diagnostics")
            with TabPane("保存与导入", id="jobs"):
                with VerticalScroll():
                    yield Static("YAML 是可复现任务的高级格式。导入后可在表单中调整并重新保存。", classes="hint")
                    yield Input(placeholder="现有 job.yaml 路径", id="job-path")
                    with Horizontal():
                        yield Button("导入 YAML", id="load-job")
                        yield Button("保存为新 YAML", id="save-job")
                    yield Checkbox("保存时固定本次视频、聊天和输出路径", value=True, id="pin-paths")
                    yield Input(placeholder="翻译 JSON（复用翻译渲染时必填）", id="translation-json")
            with TabPane("高级设置", id="advanced"):
                with VerticalScroll():
                    yield Static("这些选项会映射到现有 YAML/命令行参数；留空即使用项目默认值。", classes="hint")
                    yield Input(value="zh", placeholder="目标语言，例如 zh / ja / ko", id="target-language")
                    yield Input(value="default", placeholder="布局预设：default / compact / mobile", id="layout-preset")
                    yield Input(value="default", placeholder="编码预设：default / fast / hq", id="render-preset")
                    yield Input(placeholder="翻译 profile YAML（可选）", id="profile")
                    yield Input(placeholder="翻译后替换规则 YAML（可选）", id="rules")
                    yield Input(placeholder="编码器：x264 / auto / nvenc / qsv / amf", id="encoder")
                    yield Input(value="decode", placeholder="输入视频检查：decode / fast / off", id="source-media-check")
                    yield Static("默认在翻译和渲染前完整解码源视频。长视频会多花一次读取时间，但能更早发现坏片段。", classes="hint")
                    yield Input(placeholder="CRF/CQ（可选正整数）", id="crf")
                    yield Input(placeholder="翻译并发数（可选正整数）", id="workers")
                    yield Checkbox("保留中间文件，便于排障或续跑", id="keep-temp")
                    yield Checkbox("翻译后导出人工复核表并停止", id="review")
                    yield Checkbox("只导出待翻译内容，供手工翻译", id="manual-translation")
            with TabPane("历史与产物", id="history"):
                with VerticalScroll():
                    yield Static("输入任务短 ID 后可载入、重跑、打开产物或导出诊断。", classes="hint")
                    yield Input(placeholder="任务短 ID", id="history-id")
                    yield RichLog(id="history-log", wrap=True, highlight=False, markup=False)
                    with Horizontal():
                        yield Button("刷新历史", id="history-refresh")
                        yield Button("载入任务", id="history-load")
                        yield Button("重跑任务", id="history-rerun", variant="primary")
                    with Horizontal():
                        yield Button("打开产物", id="history-open")
                        yield Button("导出诊断", id="history-diagnostic")
                        yield Button("清空历史", id="history-clear", variant="warning")
        yield Footer()

    def on_mount(self) -> None:
        interrupted = self.history.recover_interrupted()
        if interrupted:
            self._set_status(f"已标记 {len(interrupted)} 个上次中断的任务。")
        self._refresh_history()
        self.set_interval(0.15, self._poll_session)

    def on_unmount(self) -> None:
        if self.session:
            if self.session.running:
                self.session.cancel()
                self._finish_history("interrupted", None, refresh=False)
            self.session.close()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action = event.button.id
        if action == "original-preview":
            self._start_draft(MODE_ORIGINAL_PREVIEW)
        elif action == "translated-preview":
            self._start_draft(MODE_TRANSLATED_PREVIEW)
        elif action == "full-render":
            self._start_draft(MODE_FULL_RENDER)
        elif action == "reuse-render":
            self._start_draft(MODE_REUSE_RENDER)
        elif action == "download-start":
            self._start_download()
        elif action == "load-job":
            self._load_job()
        elif action == "save-job":
            self._save_job()
        elif action == "doctor":
            self._start_command("环境检查", [sys.executable, str(self._pipeline()), "--doctor"], completion_message="环境检查完成。")
        elif action == "demo":
            self._start_command(
                "离线演示",
                [sys.executable, str(Path(__file__).with_name("quick_demo.py"))],
                result_directory=Path("outputs") / "quick_demo",
                completion_message="离线演示已生成。可打开结果目录查看 demo_overlay.mp4。",
                require_result_manifest=True,
            )
        elif action == "cancel":
            self._cancel_task()
        elif action == "open-result":
            self._open_result_dir()
        elif action == "export-diagnostics":
            self._export_diagnostics()
        elif action == "history-refresh":
            self._refresh_history()
        elif action == "history-load":
            self._load_history_draft()
        elif action == "history-rerun":
            self._rerun_history()
        elif action == "history-open":
            self._open_history_artifacts()
        elif action == "history-diagnostic":
            self._export_history_diagnostic()
        elif action == "history-clear":
            self.history.clear()
            self.active_history_id = None
            self._refresh_history()
            self._set_status("本机任务历史已清空。")

    def _input(self, selector: str) -> str:
        return self.query_one(selector, Input).value

    def _set_input(self, selector: str, value: str) -> None:
        self.query_one(selector, Input).value = value

    def _draft(self, mode: str | None = None) -> TuiJobDraft:
        try:
            preview_clip = float(self._input("#preview-clip") or 10)
        except ValueError:
            preview_clip = 0
        return TuiJobDraft(
            video=self._input("#video"),
            chat_html=self._input("#chat"),
            output=self._input("#output"),
            translation_json=self._input("#translation-json"),
            mode=mode or (self.imported_draft.mode if self.imported_draft else MODE_ORIGINAL_PREVIEW),
            target_language=self._input("#target-language"),
            layout_preset=self._input("#layout-preset"),
            render_preset=self._input("#render-preset"),
            preview_clip=preview_clip,
            profile=self._input("#profile"),
            rules=self._input("#rules"),
            encoder=self._input("#encoder"),
            source_media_check=self._input("#source-media-check"),
            crf=self._input("#crf"),
            workers=self._input("#workers"),
            keep_temp=self.query_one("#keep-temp", Checkbox).value,
            review=self.query_one("#review", Checkbox).value,
            manual_translation=self.query_one("#manual-translation", Checkbox).value,
            source_job=self._input("#job-path"),
            extra_fields=dict(self.imported_draft.extra_fields or {}) if self.imported_draft else None,
        )

    def _download_draft(self) -> TuiDownloadDraft:
        return TuiDownloadDraft(
            source=self._input("#download-url"),
            quality=self._input("#download-quality"),
            media_check=self._input("#download-media-check"),
            download_dir=self._input("#download-dir"),
            segments_text=self._input("#download-segments"),
            oauth=self._input("#download-oauth"),
        )

    def _apply_download_draft(self, draft: TuiDownloadDraft) -> None:
        self._set_input("#download-url", draft.source)
        self._set_input("#download-quality", draft.quality)
        self._set_input("#download-media-check", draft.media_check)
        self._set_input("#download-dir", draft.download_dir)
        self._set_input("#download-segments", draft.segments_text)
        self._set_input("#download-oauth", "")

    def _start_download(self, draft: TuiDownloadDraft | None = None) -> None:
        draft = draft or self._download_draft()
        problems = draft.validate()
        if problems:
            self._set_status("无法开始下载：" + " ".join(problems))
            return
        self.download_requested_duration_s = draft.requested_duration_s()
        self.download_duration_note = ""
        self._start_command(
            "正在下载素材",
            draft.build_command(sys.executable, self._pipeline()),
            completion_message="素材下载完成，已自动填入新任务。",
            draft=draft,
            task_kind="download",
            require_result_manifest=True,
        )

    def _apply_draft(self, draft: TuiJobDraft) -> None:
        values = {
            "#video": draft.video, "#chat": draft.chat_html, "#output": draft.output,
            "#translation-json": draft.translation_json, "#target-language": draft.target_language,
            "#layout-preset": draft.layout_preset, "#render-preset": draft.render_preset,
            "#preview-clip": str(draft.preview_clip), "#profile": draft.profile, "#rules": draft.rules,
            "#encoder": draft.encoder, "#crf": draft.crf, "#workers": draft.workers,
            "#source-media-check": draft.source_media_check,
        }
        for selector, value in values.items():
            self._set_input(selector, value)
        self.query_one("#keep-temp", Checkbox).value = draft.keep_temp
        self.query_one("#review", Checkbox).value = draft.review
        self.query_one("#manual-translation", Checkbox).value = draft.manual_translation

    @staticmethod
    def _pipeline() -> Path:
        return Path(__file__).with_name("render_cn_chat.py")

    def _set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    def _log(self, message: str) -> None:
        self.query_one("#log", RichLog).write(message)

    def _start_draft(self, mode: str) -> None:
        draft = self._draft(mode)
        problems = draft.validate()
        if problems:
            self._set_status("无法开始：" + " ".join(problems))
            return
        self.last_draft = draft
        for warning in draft.warnings():
            self._log("[提示] " + warning)
        result_directory, completion_message = self._result_context(draft)
        self._start_command(
            "任务已启动",
            draft.build_command(sys.executable, self._pipeline()),
            result_directory=result_directory,
            completion_message=completion_message,
            draft=draft,
            require_result_manifest=True,
        )

    @staticmethod
    def _result_context(draft: TuiJobDraft) -> tuple[Path, str]:
        output = Path(draft.output.strip().strip('"')).expanduser() if draft.output.strip() else None
        directory = output.parent if output else Path(draft.video.strip().strip('"')).expanduser().parent
        if draft.manual_translation:
            review_directory = OverlayTui._review_directory(draft)
            return review_directory, "待人工翻译文件已生成。可打开复核目录查看 JSON、XLSX/TSV。"
        if draft.review:
            review_directory = OverlayTui._review_directory(draft)
            return review_directory, "翻译与人工复核文件已生成。可打开复核目录继续复核。"
        if draft.mode in (MODE_ORIGINAL_PREVIEW, MODE_TRANSLATED_PREVIEW):
            return directory.resolve(), "预览任务完成。可打开结果目录检查生成的预览文件。"
        if draft.mode == MODE_REUSE_RENDER:
            return directory.resolve(), "复用翻译渲染完成。可打开结果目录查看成片。"
        return directory.resolve(), "正式翻译渲染完成。可打开结果目录查看成片。"

    @staticmethod
    def _review_directory(draft: TuiJobDraft) -> Path:
        extras = draft.extra_fields or {}
        for key in ("review_xlsx", "review_tsv"):
            value = str(extras.get(key) or "").strip()
            if value:
                return Path(value).expanduser().parent.resolve()
        workdir = str(extras.get("workdir") or "").strip()
        if workdir:
            return Path(workdir).expanduser().resolve()
        return Path(draft.video.strip().strip('"')).expanduser().parent.resolve()

    def _start_command(
        self,
        label: str,
        command: list[str],
        *,
        result_directory: Path | None = None,
        completion_message: str = "任务完成。",
        draft: TuiJobDraft | TuiDownloadDraft | None = None,
        task_kind: str = "render",
        require_result_manifest: bool = False,
    ) -> None:
        if self.session and self.session.running:
            self._set_status("已有任务正在运行；请等待完成或先取消。")
            return
        queued = self.history.start(draft, label=label)
        self.active_history_id = queued["id"]
        self._refresh_history()
        self.session = TaskSession(command, cwd=Path(__file__).resolve().parent.parent)
        self._handled_session = None
        self.current_task_kind = task_kind
        self.require_result_manifest = require_result_manifest
        self.result_directory = result_directory.expanduser().resolve() if result_directory is not None else None
        self.completion_message = completion_message
        try:
            self.session.start()
        except OSError as exc:
            self._set_status(f"无法启动任务：{type(exc).__name__}")
            self.history.finish(self.active_history_id, state="failed", returncode=1, result_path=None)
            self._refresh_history()
            return
        self.history.mark_running(
            self.active_history_id,
            pid=self.session.process.pid if self.session.process else None,
            result_path=None,
        )
        self._refresh_history()
        self._set_status(label)
        self._log("$ " + " ".join(redact_command(command)))

    def _poll_session(self) -> None:
        if not self.session:
            return
        logs, events = self.session.poll()
        for line in events:
            self._log("[阶段] " + line)
        for line in logs:
            self._log(line)
        if self.session.dropped_output:
            self._log(f"[日志] 为保持界面响应，已省略 {self.session.dropped_output} 行过量输出。")
            self.session.dropped_output = 0
        if not self.session.running and self.session.returncode is not None:
            if self._handled_session is self.session:
                return
            final_logs, final_events = self.session.drain_after_exit()
            for line in final_events:
                self._log("[阶段] " + line)
            for line in final_logs:
                self._log(line)
            returncode = self.session.returncode
            if self.session.cancelled:
                self._set_status("任务已取消。")
                self._finish_history("cancelled", returncode)
                self.session.cleanup()
            elif returncode == 0:
                terminal_state = str((self.session.result or {}).get("state") or "succeeded")
                if self.require_result_manifest and not isinstance(self.session.result, dict):
                    self._set_status("任务进程已结束，但未能写入结果清单；无法确认产物，已标记为失败。")
                    self._finish_history("failed", returncode)
                    self._persist_diagnostics()
                    self.session.cleanup(keep_failure=False)
                elif terminal_state == "manual_required":
                    self._apply_result_directory()
                    self._set_status("翻译未完成：已导出人工复核文件，请填写后载入任务并复用翻译渲染。")
                    self._finish_history("manual_required", returncode)
                    self.session.cleanup()
                elif self.current_task_kind == "download":
                    if self._apply_download_result():
                        self._apply_result_directory()
                        self._set_status(self.completion_message + self.download_duration_note)
                        self._finish_history("succeeded", returncode)
                        self.session.cleanup()
                    else:
                        self._set_status("下载进程已结束，但结果清单缺少视频或聊天 HTML；已标记为失败。")
                        self._finish_history("failed", returncode)
                        self._persist_diagnostics()
                        self.session.cleanup(keep_failure=False)
                elif terminal_state != "succeeded":
                    self._set_status("任务结果清单报告失败；已保留脱敏诊断。")
                    self._finish_history("failed", returncode)
                    self._persist_diagnostics()
                    self.session.cleanup(keep_failure=False)
                else:
                    self._apply_result_directory()
                    self._set_status(self.completion_message)
                    self._finish_history("succeeded", returncode)
                    self.session.cleanup()
            else:
                self._set_status(f"任务失败（退出码 {returncode}）。可导出脱敏诊断。")
                self._finish_history("failed", returncode)
                self._persist_diagnostics()
                self.session.cleanup(keep_failure=False)
            self._handled_session = self.session

    def _cancel_task(self) -> None:
        if self.session and self.session.cancel():
            self._set_status("正在取消任务及其子进程…")
        else:
            self._set_status("当前没有可取消的任务。")

    def _finish_history(self, state: str, returncode: int | None, *, refresh: bool = True) -> None:
        if self.active_history_id:
            result_path = None
            if self.session:
                result_path = self.session.retain_result(self.history.manifest_path(self.active_history_id))
            self.history.finish(
                self.active_history_id,
                state=state,
                returncode=returncode,
                result_path=result_path,
            )
            if refresh:
                self._refresh_history()

    def _apply_result_directory(self) -> None:
        if not self.session or not isinstance(self.session.result, dict):
            return
        artifacts = self.session.result.get("artifacts")
        if not isinstance(artifacts, list):
            return
        preferred = ("video", "review_xlsx", "translation_json", "review_tsv", "preview_image")
        for kind in preferred:
            for artifact in artifacts:
                if not isinstance(artifact, dict) or artifact.get("kind") != kind:
                    continue
                raw_path = artifact.get("path")
                if raw_path:
                    self.result_directory = Path(str(raw_path)).expanduser().parent.resolve()
                    return

    def _apply_download_result(self) -> bool:
        if not self.session or not isinstance(self.session.result, dict):
            return False
        artifacts = self.session.result.get("artifacts")
        if not isinstance(artifacts, list):
            return False
        paths = {
            str(item.get("kind")): str(item.get("path"))
            for item in artifacts
            if isinstance(item, dict) and item.get("kind") and item.get("path")
        }
        video, chat_html = paths.get("video"), paths.get("chat_html")
        if not video or not chat_html:
            self._set_status("下载完成，但结果清单缺少视频或聊天 HTML 路径。")
            return False
        self._set_input("#video", video)
        self._set_input("#chat", chat_html)
        self.imported_draft = None
        self.last_draft = TuiJobDraft(video=video, chat_html=chat_html, mode=MODE_ORIGINAL_PREVIEW)
        self.download_duration_note = self._download_duration_note(video)
        self.query_one(TabbedContent).active = "new-task"
        return True

    def _download_duration_note(self, video: str) -> str:
        """Explain a material Twitch crop-boundary expansion without failing a valid download."""
        expected = self.download_requested_duration_s
        if expected is None or expected <= 0:
            return ""
        try:
            from twitch_download import probe_media_duration

            actual = probe_media_duration(Path(video))
        except Exception:
            return ""
        # Allow normal muxing drift, but make a boundary-aligned expansion visible
        # before a user spends time translating more video than they selected.
        if abs(actual - expected) <= max(2.0, expected * 0.25):
            return ""
        return (
            f" 请求时间窗约 {expected:.1f} 秒，实际下载视频为 {actual:.1f} 秒；"
            "Twitch 短片段可能按 HLS 分片边界扩展，请在开始翻译前确认素材范围。"
        )

    def _refresh_history(self) -> None:
        log = self.query_one("#history-log", RichLog)
        log.clear()
        records = self.history.list_records()
        if not records:
            log.write("暂无本机任务历史。")
            return
        for record in records:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(float(record.get("started_at") or 0)))
            result = self.history.result_for(record) or {}
            artifacts = result.get("artifacts") if isinstance(result, dict) else []
            count = len(artifacts) if isinstance(artifacts, list) else 0
            log.write(
                f"{record.get('id')}  {record.get('state')}  {stamp}  "
                f"{record.get('label', 'task')}  产物 {count}"
            )

    def _history_record(self) -> dict | None:
        raw_id = self._input("#history-id").strip()
        if not raw_id and self.active_history_id:
            raw_id = self.active_history_id
        if not raw_id:
            self._set_status("请先输入历史任务短 ID。")
            return None
        matches = [record for record in self.history.list_records() if str(record.get("id", "")).startswith(raw_id)]
        if len(matches) != 1:
            self._set_status("未找到唯一的历史任务；请使用列表中的完整短 ID。")
            return None
        self._set_input("#history-id", str(matches[0]["id"]))
        return matches[0]

    def _load_history_draft(self) -> None:
        record = self._history_record()
        if record is None:
            return
        download = self.history.download_for(record)
        if download is not None:
            self._apply_download_draft(download)
            self.query_one(TabbedContent).active = "download"
            self._set_status("已载入历史下载配置。")
            return
        draft = self.history.draft_for(record)
        if draft is None:
            self._set_status("该记录没有可重用的本地任务配置。")
            return
        self.imported_draft = draft
        self._apply_draft(draft)
        self._set_status("已载入历史任务配置。")

    def _rerun_history(self) -> None:
        record = self._history_record()
        if record is None:
            return
        download = self.history.download_for(record)
        if download is not None:
            self._apply_download_draft(download)
            self._start_download(download)
            return
        draft = self.history.draft_for(record)
        if draft is None:
            self._set_status("该记录不能重跑（没有本地任务配置）。")
            return
        self.imported_draft = draft
        self._apply_draft(draft)
        self._start_draft(draft.mode)

    def _open_history_artifacts(self) -> None:
        record = self._history_record()
        if record is None:
            return
        result = self.history.result_for(record) or {}
        artifacts = result.get("artifacts") if isinstance(result, dict) else []
        if not isinstance(artifacts, list) or not artifacts:
            self._set_status("该历史任务没有可打开的产物。")
            return
        first: dict = {}
        for kind in ("video", "review_xlsx", "translation_json", "review_tsv", "preview_image"):
            first = next(
                (artifact for artifact in artifacts if isinstance(artifact, dict) and artifact.get("kind") == kind),
                {},
            )
            if first:
                break
        raw_path = first.get("path") if isinstance(first, dict) else None
        if not raw_path:
            self._set_status("该历史任务的产物路径无效。")
            return
        self.result_directory = Path(str(raw_path)).expanduser().parent.resolve()
        self._open_result_dir()

    def _export_history_diagnostic(self) -> None:
        record = self._history_record()
        if record is None:
            return
        existing = record.get("diagnostic_path")
        if existing and Path(str(existing)).is_file():
            try:
                path = sanitize_diagnostic_file(existing)
            except OSError:
                self._set_status("无法读取历史诊断文件。")
                return
            self.result_directory = path.parent.resolve()
            self._open_result_dir()
            return
        self._set_status("该历史任务尚无诊断；请在失败任务结束后导出诊断。")

    def _load_job(self) -> None:
        path = self._input("#job-path").strip().strip('"')
        try:
            draft = TuiJobDraft.from_job_file(path)
        except (OSError, ValueError) as exc:
            self._set_status(f"无法导入 YAML：{exc}")
            return
        self._apply_draft(draft)
        self.last_draft = draft
        self.imported_draft = draft
        self._set_status("已导入 YAML；可调整表单后执行或另存。")

    def _save_job(self) -> None:
        path = self._input("#job-path").strip().strip('"')
        if not path:
            self._set_status("请先填写一个新的 job.yaml 路径。")
            return
        draft = self._draft()
        try:
            saved = draft.save_job(path, pin_paths=self.query_one("#pin-paths", Checkbox).value)
        except (OSError, ValueError, FileExistsError) as exc:
            self._set_status(f"无法保存 YAML：{exc}")
            return
        self._set_input("#job-path", str(saved))
        self.imported_draft = draft
        self._set_status("已保存 YAML。")

    def _result_dir(self) -> Path | None:
        if self.result_directory is not None:
            return self.result_directory
        draft = self.last_draft or self._draft()
        if draft.output.strip():
            return Path(draft.output.strip().strip('"')).expanduser().parent
        if draft.video.strip():
            return Path(draft.video.strip().strip('"')).expanduser().parent
        return None

    def _open_result_dir(self) -> None:
        directory = self._result_dir()
        if directory is None or not directory.is_dir():
            self._set_status("尚无可打开的结果目录。")
            return
        try:
            if os.name == "nt":
                os.startfile(directory)  # type: ignore[attr-defined]  # noqa: S606
            else:
                import subprocess

                subprocess.Popen(["xdg-open", str(directory)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self._set_status("无法打开结果目录。")
            return
        self._set_status(f"已打开：{directory}")

    def _persist_diagnostics(self) -> Path | None:
        """Persist redacted diagnostics so a failed task survives an app restart."""
        if not self.session:
            return None
        if self.active_history_id:
            target = self.history.path.parent / "diagnostics" / f"{self.active_history_id}.txt"
        else:
            target = Path("outputs") / "tui_diagnostic.txt"
        try:
            path = self.session.export_diagnostics(target)
        except OSError:
            return None
        if self.active_history_id:
            self.history.set_diagnostic(self.active_history_id, path)
            self._refresh_history()
        return path

    def _export_diagnostics(self) -> None:
        path = self._persist_diagnostics()
        if path is None:
            self._set_status("尚无任务诊断可导出。")
            return
        self._set_status(f"诊断已导出：{path}")


def main() -> int:
    OverlayTui().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
