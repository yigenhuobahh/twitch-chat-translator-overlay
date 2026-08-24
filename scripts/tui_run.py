#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Full Textual launcher for local Twitch chat-overlay workflows."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time
import webbrowser

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.option_list import Option

from env_bootstrap import (
    get_translate_api_config,
    probe_translate_api,
    save_dotenv_api_config,
)
from tui_history import TuiHistoryStore
from tui_models import (
    MODE_AUTO,
    MODE_FULL_PRODUCTION,
    MODE_FULL_RENDER,
    MODE_ORIGINAL_PREVIEW,
    MODE_ORIGINAL_PRODUCTION,
    MODE_QUICK_PREVIEW_ORIGINAL,
    MODE_QUICK_PREVIEW_TRANSLATED,
    MODE_RENDER_ONLY,
    MODE_RENDER_ORIGINAL,
    MODE_REUSE_RENDER,
    MODE_STEP_API_AND_REVIEW,
    MODE_STEP_EXPORT_MANUAL,
    MODE_STEP_RESUME_RENDER,
    MODE_TRANSLATE_ONLY,
    MODE_TRANSLATED_PREVIEW,
    TuiDownloadDraft,
    TuiJobDraft,
)
from tui_task import TaskSession, redact_command, sanitize_diagnostic_file

_UI_MODE_RENDER_ORIGINAL = "render_original"

# 3 Core Operational Paths (Primary UI Options)
_CORE_TASK_MODE_OPTIONS = (
    ("【快速预览】1. 原文短片预览（不调用 API，10 秒快速看弹幕与排版）", MODE_QUICK_PREVIEW_ORIGINAL),
    ("【快速预览】2. 翻译小样预览（调用 API，10 秒快速看翻译效果）", MODE_QUICK_PREVIEW_TRANSLATED),
    ("【一键出片】1. 全自动翻译压制（提取 + API 翻译 + 规则清洗 + 压制成片）", MODE_FULL_PRODUCTION),
    ("【一键出片】2. 纯原文弹幕压制（不调用 API，直接压制全片）", MODE_ORIGINAL_PRODUCTION),
    ("【分步复核】1. 导出待翻译表单（提取弹幕并导出待翻译文件，暂停）", MODE_STEP_EXPORT_MANUAL),
    ("【分步复核】2. 自动翻译并导出复核表（API 翻译后导出 Excel 供人工核对）", MODE_STEP_API_AND_REVIEW),
    ("【分步复核】3. 载入复核表并压制（使用已核对的 JSON/Excel 恢复压制成片）", MODE_STEP_RESUME_RENDER),
)

# Legacy Task Modes (Maintained for full backward compatibility)
_LEGACY_TASK_MODE_OPTIONS = (
    ("原文预览 (Legacy)", MODE_ORIGINAL_PREVIEW),
    ("翻译预览 (Legacy)", MODE_TRANSLATED_PREVIEW),
    ("正式翻译渲染 (Legacy)", MODE_FULL_RENDER),
    ("复用已有翻译渲染 (Legacy)", MODE_REUSE_RENDER),
    ("仅渲染原文 (Legacy)", _UI_MODE_RENDER_ORIGINAL),
    ("仅翻译并导出 JSON (Legacy)", MODE_TRANSLATE_ONLY),
    ("自动模式 (Legacy)", MODE_AUTO),
)

_TASK_MODE_OPTIONS = (
    *_CORE_TASK_MODE_OPTIONS,
    *_LEGACY_TASK_MODE_OPTIONS,
)

_ADVANCED_RENDER_MODE_OPTION = ("仅渲染（导入 YAML 的高级流程）", MODE_RENDER_ONLY)

_LAYOUT_PRESET_OPTIONS = (
    ("左下标准 (Default - 1080p 标准左下角黑底弹幕框)", "default"),
    ("右侧避让 (Right - 1080p 右侧弹幕框，避开左侧游戏UI与头像)", "right"),
    ("紧凑小框 (Compact - 缩小版弹幕框，适合小窗或密集聊天)", "compact"),
    ("移动上浮 (Mobile - 上浮堆叠模式，自动填满高度，适合手机观看)", "mobile"),
    ("半透明悬浮 (Transparent - 半透明淡底，弱化黑框不遮挡游戏画面)", "transparent"),
    ("全高侧边栏 (Sidebar - 纵向通顶长条侧边栏，适合大体量弹幕)", "sidebar"),
    ("右上小窗 (Top-Right - 右上角小窗，避开底部技能与剧情字幕)", "top_right"),
)

_RENDER_PRESET_OPTIONS = (
    ("自动智能 (Default - 优先显卡加速，CRF 18 平衡出片)", "default"),
    ("极速草稿 (Fast - 优先显卡加速 + PNG 直接叠加，秒级出样)", "fast"),
    ("母带高清 (HQ - 优先显卡高质量模式/CRF 16 + 256k 音频)", "hq"),
    ("无损音轨 (Audio Copy - 音轨直通不重采样，保留100%音质)", "audio_copy"),
)

_ENCODER_OPTIONS = (
    ("智能识别 (Auto - 优先独显 NVENC/AMF -> QSV -> 回退 x264)", "auto"),
    ("NVIDIA 硬件加速 (NVENC - 适用于 GeForce/RTX/Quadro 显卡)", "nvenc"),
    ("AMD 硬件加速 (AMF - 适用于 Radeon RX 独显与 Ryzen APU)", "amf"),
    ("Intel 硬件加速 (QSV - 适用于 Core 核显与 Arc 独显)", "qsv"),
    ("CPU 软件编码 (x264 - 通用稳定无显卡依赖)", "x264"),
)

_MEDIA_CHECK_OPTIONS = (
    ("完整检查：逐帧解码验证，推荐正式任务使用", "decode"),
    ("快速检查：仅检查封装结构、时间戳与流信息", "fast"),
    ("关闭检查：跳过检查（仅供排障调试）", "off"),
)


class OverlayTui(App[None]):
    """Beginner-first UI; rendering remains entirely in render_cn_chat.py."""

    CSS = """
    Screen { layout: vertical; }
    #status { height: 3; padding: 1; }
    TabbedContent { height: 1fr; }
    VerticalScroll { padding: 0 1; }
    Input { margin: 0 0 1 0; }
    Select { margin: 0 0 1 0; }
    Checkbox { margin: 1 0; }
    RichLog { height: 1fr; min-height: 12; border: round $accent; }
    OptionList { height: 1fr; min-height: 12; border: round $accent; }
    Horizontal { height: auto; margin: 1 0; }
    Button { margin-right: 1; }
    .hint { color: $text-muted; margin: 1 0; }
    .section-title { text-style: bold; color: $accent; margin: 1 0 0 0; }
    .field-row { height: 3; margin: 1 0 0 0; }
    .field-row .field-label { width: 26; color: $text-muted; margin: 1 1 0 0; }
    .field-row Input { width: 1fr; margin: 0; }
    .field-row Select { width: 1fr; margin: 0; }
    #form-validation { height: auto; min-height: 2; max-height: 6; margin: 0 0 1 0; }
    #form-validation.ready { color: $success; }
    #form-validation.invalid { color: $warning; }
    #api-status-feedback { margin: 1 0; }
    """
    TITLE = "Twitch Chat Overlay"
    ISSUE_TEMPLATE_URL = "https://github.com/yigenhuobahh/twitch-chat-translator-overlay/issues/new?template=bug_report.yml"

    def __init__(self) -> None:
        super().__init__()
        self.session: TaskSession | None = None
        self.last_draft: TuiJobDraft | None = None
        self.imported_draft: TuiJobDraft | None = None
        self.result_directory: Path | None = None
        self.completion_message = "任务已顺利完成。"
        self.history = TuiHistoryStore(Path(__file__).resolve().parent.parent / "outputs" / ".tui-history" / "history.json")
        self.active_history_id: str | None = None
        self._handled_session: TaskSession | None = None
        self.current_task_kind = "render"
        self.require_result_manifest = False
        self.download_requested_duration_s: float | None = None
        self.download_duration_note = ""
        self.selected_history_id: str | None = None
        self._history_clear_confirmation_until = 0.0

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("请选择工作模式并载入素材，随后即可开始快速预览、一键出片或分步人工复核。", id="status")
        with TabbedContent(initial="new-task"):
            with TabPane("下载素材", id="download"):
                with VerticalScroll():
                    yield Static(
                        "支持从 Twitch 下载公开或订阅 VOD 与精彩 Clip。\n"
                        "• 留空不填：直接下载整段完整 VOD 或 Clip 内容。\n"
                        "• 多段裁切拼接：用分号或换行分隔多个时间段（如 1:00:00-1:00:08; 1:00:20-1:00:28），下载后将自动按时间顺序裁剪并拼接为一个完整视频与弹幕文件。\n"
                        "提示：Twitch 视频可能按 HLS 分片边界对齐扩展切片时长，下载完成后会自动核对实际时长。",
                        classes="hint",
                    )
                    with Horizontal(classes="field-row"):
                        yield Label("VOD / Clip", classes="field-label")
                        yield Input(placeholder="公开 Twitch VOD/Clip 链接或数字 ID", id="download-url")
                    with Horizontal(classes="field-row"):
                        yield Label("下载画质", classes="field-label")
                        yield Input(value="1080p60", placeholder="画质，例如 1080p60 / 720p60", id="download-quality")
                    with Horizontal(classes="field-row"):
                        yield Label("下载媒体检查", classes="field-label")
                        yield Select(
                            _MEDIA_CHECK_OPTIONS,
                            value="decode",
                            allow_blank=False,
                            prompt="下载媒体检查",
                            id="download-media-check",
                        )
                    yield Static("完整检查会顺序解码每个分片与合并后的文件，适合正式出片；快速检查耗时极短，适合快速确认素材范围。", classes="hint")
                    with Horizontal(classes="field-row"):
                        yield Label("下载目录（可选）", classes="field-label")
                        yield Input(placeholder="下载目录（可选；留空自动创建）", id="download-dir")
                    with Horizontal(classes="field-row"):
                        yield Label("VOD 裁切段（可选）", classes="field-label")
                        yield Input(placeholder="可选：留空下载整段；多段拼接用分号分隔：1:00:00-1:00:08; 1:00:20-1:00:28", id="download-segments")
                    with Horizontal(classes="field-row"):
                        yield Label("OAuth（可选，不会保存）", classes="field-label")
                        yield Input(placeholder="OAuth（订阅限定 VOD；仅本次下载使用）", password=True, id="download-oauth")
                    yield Button("下载并载入新任务", id="download-start", variant="primary")
            with TabPane("新任务", id="new-task"):
                with VerticalScroll():
                    yield Static(
                        "请选择工作模式并指定素材路径。初次使用建议先跑【快速预览】确认弹幕位置；日常使用推荐【一键出片】或【分步人工复核】。",
                        classes="hint",
                    )
                    with Horizontal(classes="field-row"):
                        yield Label("任务模式", classes="field-label")
                        yield Select(
                            _TASK_MODE_OPTIONS,
                            value=MODE_QUICK_PREVIEW_ORIGINAL,
                            allow_blank=False,
                            prompt="任务模式",
                            id="task-mode",
                        )
                    yield Static("输入素材", classes="hint")
                    with Horizontal(classes="field-row"):
                        yield Label("源视频", classes="field-label")
                        yield Input(placeholder="源视频路径 (.mp4/.mkv/...)", id="video")
                    with Horizontal(classes="field-row"):
                        yield Label("Twitch 聊天 HTML", classes="field-label")
                        yield Input(placeholder="TwitchDownloader 聊天 HTML 路径", id="chat")
                    with Horizontal(classes="field-row"):
                        yield Label("输出视频（可选）", classes="field-label")
                        yield Input(placeholder="输出视频路径（可选，默认源视频同目录）", id="output")
                    with Horizontal(classes="field-row"):
                        yield Label("预览时长（秒）", classes="field-label")
                        yield Input(value="10", placeholder="预览时长（秒）", id="preview-clip")
                    with Horizontal(classes="field-row"):
                        yield Label("时间偏移（秒）", classes="field-label")
                        yield Input(value="0.0", placeholder="0.0", id="offset")
                    yield Static("时间偏移用于微调弹幕与视频的时间轴对齐（正数延后，负数提前；例如 0.0、12.5、-3.0；留空或 0 则自动按直播流时间戳对齐）。", classes="hint")
                    yield Static("", id="form-validation", classes="invalid")
                    yield Button("开始所选任务", id="run-mode", variant="primary")
            with TabPane("任务与结果", id="task"):
                with VerticalScroll():
                    yield Static("实时显示任务执行进度、结构化阶段事件与子进程日志。若任务遇到异常，可导出脱敏诊断报告以便排障。", classes="hint")
                    yield RichLog(id="log", wrap=True, highlight=False, markup=False)
                    with Horizontal():
                        yield Button("运行环境检查", id="doctor")
                        yield Button("生成 Issue 自检摘要", id="support-summary")
                        yield Button("打开 Bug 反馈模板", id="open-issue")
                        yield Button("生成离线演示小样", id="demo")
                        yield Button("取消当前任务", id="cancel", variant="warning")
                        yield Button("打开结果输出目录", id="open-result")
                        yield Button("导出脱敏诊断日志", id="export-diagnostics")
            with TabPane("保存与导入", id="jobs"):
                with VerticalScroll():
                    yield Static("任务配置管理：可将当前界面的全部参数保存为 YAML 配置文件，或导入已有的配置文件实现一键复现。", classes="hint")
                    with Horizontal(classes="field-row"):
                        yield Label("Job YAML", classes="field-label")
                        yield Input(placeholder="现有 job.yaml 路径", id="job-path")
                    with Horizontal():
                        yield Button("导入 YAML", id="load-job")
                        yield Button("保存为新 YAML", id="save-job")
                    yield Checkbox("保存时固定本次视频、聊天和输出路径", value=True, id="pin-paths")
                    with Horizontal(classes="field-row"):
                        yield Label("翻译 JSON", classes="field-label")
                        yield Input(placeholder="翻译 JSON（复用翻译渲染时必填）", id="translation-json")
            with TabPane("高级设置", id="advanced"):
                with VerticalScroll():
                    yield Static("这些选项会映射到现有 YAML/命令行参数；留空即使用项目默认值。", classes="hint")

                    yield Static("API 配置与连通性", classes="section-title")
                    yield Static("配置 OpenAI 兼容的翻译服务（如 OpenAI、DeepSeek、Ollama 等）。密钥使用密码掩码输入，保存后将安全写入本地 .env 文件。", classes="hint")
                    with Horizontal(classes="field-row"):
                        yield Label("API 接口地址", classes="field-label")
                        yield Input(placeholder="https://api.openai.com/v1", id="api-base-url")
                    with Horizontal(classes="field-row"):
                        yield Label("API 密钥", classes="field-label")
                        yield Input(placeholder="sk-...", password=True, id="api-key")
                    with Horizontal(classes="field-row"):
                        yield Label("翻译模型", classes="field-label")
                        yield Input(placeholder="gpt-4o-mini / deepseek-chat", id="api-model")
                    with Horizontal():
                        yield Button("测试 API 连通性", id="btn-test-api")
                        yield Button("保存配置到 .env", id="btn-save-api")
                    yield Static("点击【测试 API 连通性】以验证当前接口配置与鉴权状态。", id="api-status-feedback", classes="hint")

                    yield Static("翻译、编码与排版参数", classes="section-title")
                    with Horizontal(classes="field-row"):
                        yield Label("目标语言", classes="field-label")
                        yield Input(value="zh", placeholder="目标语言，例如 zh / ja / ko", id="target-language")
                    with Horizontal(classes="field-row"):
                        yield Label("布局预设", classes="field-label")
                        yield Select(_LAYOUT_PRESET_OPTIONS, value="default", allow_blank=False, id="layout-preset")
                    with Horizontal(classes="field-row"):
                        yield Label("编码预设", classes="field-label")
                        yield Select(_RENDER_PRESET_OPTIONS, value="default", allow_blank=False, id="render-preset")
                    with Horizontal(classes="field-row"):
                        yield Label("视频编码器（可选）", classes="field-label")
                        yield Select(_ENCODER_OPTIONS, value="auto", allow_blank=False, id="encoder")
                    with Horizontal(classes="field-row"):
                        yield Label("输入视频检查", classes="field-label")
                        yield Select(
                            _MEDIA_CHECK_OPTIONS,
                            value="decode",
                            allow_blank=False,
                            prompt="输入视频检查",
                            id="source-media-check",
                        )
                    yield Static("完整检查会在翻译和压制前对源视频执行顺序解码校验；快速检查仅校验元数据与时间戳。", classes="hint")
                    with Horizontal(classes="field-row"):
                        yield Label("CRF / CQ（可选）", classes="field-label")
                        yield Input(placeholder="CRF/CQ（可选正整数）", id="crf")
                    with Horizontal(classes="field-row"):
                        yield Label("翻译并发数（可选）", classes="field-label")
                        yield Input(placeholder="翻译并发数（可选正整数）", id="workers")
                    with Horizontal(classes="field-row"):
                        yield Label("翻译 Profile（可选）", classes="field-label")
                        yield Input(placeholder="翻译 profile YAML（可选）", id="profile")
                    with Horizontal(classes="field-row"):
                        yield Label("替换规则（可选）", classes="field-label")
                        yield Input(placeholder="翻译后替换规则 YAML（可选）", id="rules")
                    yield Checkbox("保留中间文件，便于排障或续跑", id="keep-temp")
                    yield Checkbox("翻译后导出人工复核表并停止", id="review")
                    yield Checkbox("只导出待翻译内容，供手工翻译", id="manual-translation")
            with TabPane("历史与产物", id="history"):
                with VerticalScroll():
                    yield Static("选择一条任务后可载入、重跑、打开产物或导出诊断。", classes="hint")
                    yield OptionList(id="history-list")
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
        self._load_api_config_into_ui()
        self._refresh_form_validation()
        self.set_interval(0.15, self._poll_session)

    def _load_api_config_into_ui(self) -> None:
        try:
            cfg = get_translate_api_config()
            if cfg.get("base_url"):
                self._set_input("#api-base-url", str(cfg["base_url"]))
            if cfg.get("api_key"):
                self._set_input("#api-key", str(cfg["api_key"]))
            if cfg.get("model"):
                self._set_input("#api-model", str(cfg["model"]))
        except Exception:
            pass

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in {
            "video", "chat", "output", "preview-clip", "offset", "translation-json",
            "profile", "rules", "crf", "workers",
        }:
            self._refresh_form_validation()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id in {"task-mode", "source-media-check", "layout-preset", "render-preset", "encoder"}:
            self._refresh_form_validation()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id in {"review", "manual-translation"}:
            self._refresh_form_validation()

    def on_unmount(self) -> None:
        if self.session:
            if self.session.running:
                self.session.cancel()
                self._finish_history("interrupted", None, refresh=False)
            self.session.close()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action = event.button.id
        if action == "run-mode":
            self._start_selected_mode()
        elif action == "download-start":
            self._start_download()
        elif action == "load-job":
            self._load_job()
        elif action == "save-job":
            self._save_job()
        elif action == "btn-save-api":
            self._save_api_config()
        elif action == "btn-test-api":
            self._test_api_connectivity()
        elif action == "doctor":
            self._start_command("环境检查", [sys.executable, str(self._pipeline()), "--doctor"], completion_message="环境检查完成。")
        elif action == "support-summary":
            self._start_support_summary()
        elif action == "open-issue":
            self._open_issue_template()
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
            self._clear_history()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "history-list" and event.option.id:
            self._select_history(str(event.option.id))

    def _input(self, selector: str) -> str:
        return self.query_one(selector, Input).value

    def _set_input(self, selector: str, value: str) -> None:
        self.query_one(selector, Input).value = value

    def _select_value(self, selector: str) -> str:
        value = self.query_one(selector, Select).value
        return value if isinstance(value, str) else ""

    def _set_select(self, selector: str, value: str) -> None:
        self.query_one(selector, Select).value = value

    def _set_select_with_custom(self, selector: str, standard_options: tuple[tuple[str, str], ...], value: str) -> None:
        val = value.strip() if value else ""
        select_widget = self.query_one(selector, Select)
        known_values = {opt_val for _label, opt_val in standard_options}
        if val and val not in known_values:
            custom_option = (f"自定义 ({val})", val)
            select_widget.set_options((*standard_options, custom_option))
            select_widget.value = val
        else:
            select_widget.set_options(standard_options)
            target = val if val in known_values else (standard_options[0][1] if standard_options else "")
            select_widget.value = target

    def _set_task_mode_options(self, *, include_advanced_render: bool = False, value: str | None = None) -> None:
        options = list(_TASK_MODE_OPTIONS)
        if include_advanced_render:
            options.append(_ADVANCED_RENDER_MODE_OPTION)
        allowed = {item_value for _label, item_value in options}

        if value and value not in allowed:
            if value == MODE_RENDER_ONLY:
                options.append(_ADVANCED_RENDER_MODE_OPTION)
            else:
                options.append((f"自定义模式 ({value})", value))
            allowed.add(value)

        target = value if (value and value in allowed) else MODE_QUICK_PREVIEW_ORIGINAL
        task_mode = self.query_one("#task-mode", Select)
        task_mode.set_options(options)
        task_mode.value = target

    def _draft(
        self,
        mode: str | None = None,
        *,
        render_original: bool | None = None,
        reuse_translation: bool | None = None,
    ) -> TuiJobDraft:
        try:
            preview_clip = float(self._input("#preview-clip") or 10)
        except ValueError:
            preview_clip = 0.0
        selected_mode = mode or self._select_value("#task-mode") or (
            self.imported_draft.mode if self.imported_draft else MODE_QUICK_PREVIEW_ORIGINAL
        )
        inherited_render_original = bool(
            self.imported_draft
            and selected_mode == self.imported_draft.mode
            and self.imported_draft.render_original
        )
        inherited_reuse_translation = bool(
            self.imported_draft
            and selected_mode == self.imported_draft.mode
            and self.imported_draft.reuse_translation
        )
        return TuiJobDraft(
            video=self._input("#video"),
            chat_html=self._input("#chat"),
            output=self._input("#output"),
            translation_json=self._input("#translation-json"),
            mode=selected_mode,
            target_language=self._input("#target-language"),
            layout_preset=self._select_value("#layout-preset") or "default",
            render_preset=self._select_value("#render-preset") or "default",
            preview_clip=preview_clip,
            profile=self._input("#profile"),
            rules=self._input("#rules"),
            encoder=self._select_value("#encoder") or "auto",
            source_media_check=self._select_value("#source-media-check") or "decode",
            crf=self._input("#crf"),
            workers=self._input("#workers"),
            keep_temp=self.query_one("#keep-temp", Checkbox).value,
            review=self.query_one("#review", Checkbox).value,
            manual_translation=self.query_one("#manual-translation", Checkbox).value,
            render_original=inherited_render_original if render_original is None else render_original,
            reuse_translation=inherited_reuse_translation if reuse_translation is None else reuse_translation,
            offset=self._input("#offset").strip(),
            source_job=self._input("#job-path"),
            extra_fields=dict(self.imported_draft.extra_fields or {}) if self.imported_draft else None,
        )

    def _read_form_draft(self, mode: str | None = None, **kwargs) -> TuiJobDraft:
        return self._draft(mode, **kwargs)

    def _download_draft(self) -> TuiDownloadDraft:
        return TuiDownloadDraft(
            source=self._input("#download-url"),
            quality=self._input("#download-quality"),
            media_check=self._select_value("#download-media-check") or "decode",
            download_dir=self._input("#download-dir"),
            segments_text=self._input("#download-segments"),
            oauth=self._input("#download-oauth"),
        )

    def _apply_download_draft(self, draft: TuiDownloadDraft) -> None:
        self._set_input("#download-url", draft.source)
        self._set_input("#download-quality", draft.quality)
        self._set_select("#download-media-check", draft.media_check)
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
        self._log("[检查] " + self._media_check_summary(draft.media_check, subject="下载素材"))
        self._start_command(
            "正在下载素材",
            draft.build_command(sys.executable, self._pipeline()),
            completion_message="素材下载完成，已自动填入新任务。",
            draft=draft,
            task_kind="download",
            require_result_manifest=True,
        )

    def _apply_draft(self, draft: TuiJobDraft) -> None:
        offset_val = draft.offset if draft.offset is not None and str(draft.offset).strip() != "" else (draft.extra_fields or {}).get("offset", "")
        values = {
            "#video": draft.video,
            "#chat": draft.chat_html,
            "#output": draft.output,
            "#translation-json": draft.translation_json,
            "#target-language": draft.target_language,
            "#preview-clip": str(draft.preview_clip),
            "#profile": draft.profile,
            "#rules": draft.rules,
            "#crf": draft.crf,
            "#workers": draft.workers,
            "#offset": "" if offset_val is None else str(offset_val),
            "#job-path": draft.source_job,
        }
        for selector, value in values.items():
            self._set_input(selector, value)

        self._set_select_with_custom("#layout-preset", _LAYOUT_PRESET_OPTIONS, draft.layout_preset or "default")
        self._set_select_with_custom("#render-preset", _RENDER_PRESET_OPTIONS, draft.render_preset or "default")
        self._set_select_with_custom("#encoder", _ENCODER_OPTIONS, draft.encoder or "auto")
        self._set_select("#source-media-check", draft.source_media_check or "decode")

        mode_value = _UI_MODE_RENDER_ORIGINAL if draft.mode == MODE_RENDER_ONLY and draft.render_original else draft.mode
        self._set_task_mode_options(
            include_advanced_render=draft.mode == MODE_RENDER_ONLY and not draft.render_original,
            value=mode_value,
        )
        self.query_one("#keep-temp", Checkbox).value = draft.keep_temp
        self.query_one("#review", Checkbox).value = draft.review
        self.query_one("#manual-translation", Checkbox).value = draft.manual_translation

    def _populate_form_from_draft(self, draft: TuiJobDraft) -> None:
        self._apply_draft(draft)

    @staticmethod
    def _pipeline() -> Path:
        return Path(__file__).with_name("render_cn_chat.py")

    def _set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    def _refresh_form_validation(self) -> None:
        validation = self.query_one("#form-validation", Static)
        problems = self._draft().validate(check_api=False, check_environment=False)
        if problems:
            validation.remove_class("ready")
            validation.add_class("invalid")
            validation.update("待处理：" + " ".join(problems))
            return
        validation.remove_class("invalid")
        validation.add_class("ready")
        validation.update("表单检查通过。")

    def _log(self, message: str) -> None:
        self.query_one("#log", RichLog).write(message)

    def _save_api_config(self) -> None:
        base_url = self._input("#api-base-url").strip()
        api_key = self._input("#api-key").strip()
        model = self._input("#api-model").strip()
        ok, msg = save_dotenv_api_config(base_url, api_key, model)
        feedback = self.query_one("#api-status-feedback", Static)
        if ok:
            feedback.update(f"✅ 配置已成功保存至本地 .env 文件（模型: {model or '默认'}）。")
            self._set_status("API 配置已保存至 .env。")
            self._log(f"[提示] 翻译 API 配置已更新并保存至 .env（{base_url or '默认地址'}, {model or '默认模型'}）。")
        else:
            feedback.update(f"❌ 保存 .env 失败：{msg}")
            self._set_status(f"保存 .env 失败：{msg}")

    def _test_api_connectivity(self) -> None:
        base_url = self._input("#api-base-url").strip()
        api_key = self._input("#api-key").strip()
        model = self._input("#api-model").strip()
        self._run_api_probe(base_url, api_key, model)

    @work(thread=True)
    def _run_api_probe(self, base_url: str, api_key: str, model: str) -> None:
        self.app.call_from_thread(
            self._update_api_feedback,
            "⏳ 正在连接翻译 API 接口进行鉴权与连通性测试，请稍候…",
            status="正在测试 API 连通性…",
        )
        start_time = time.perf_counter()
        ok, msg = probe_translate_api(
            base_url=base_url or None,
            api_key=api_key or None,
            model=model or None,
            timeout=12.0,
        )
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        if ok:
            feedback_text = f"✅ API 连通性测试成功！模型: {model or '默认'}，响应延迟: {latency_ms}ms。接口正常可用。"
            status_text = f"API 连通性测试成功（耗时 {latency_ms}ms）。"
        else:
            feedback_text = f"❌ API 连通性测试失败：{msg}（耗时 {latency_ms}ms）"
            status_text = f"API 测试失败：{msg}"
        self.app.call_from_thread(self._update_api_feedback, feedback_text, status=status_text)

    def _update_api_feedback(self, text: str, status: str | None = None) -> None:
        try:
            feedback = self.query_one("#api-status-feedback", Static)
            feedback.update(text)
            if status:
                self._set_status(status)
        except Exception:
            pass

    def _start_selected_mode(self) -> None:
        selected = self._select_value("#task-mode")
        if selected == _UI_MODE_RENDER_ORIGINAL:
            self._start_draft(MODE_RENDER_ONLY, render_original=True, reuse_translation=False)
            return
        if selected == MODE_ORIGINAL_PRODUCTION:
            self._start_draft(MODE_ORIGINAL_PRODUCTION, render_original=True, reuse_translation=False)
            return
        if selected == MODE_RENDER_ONLY and (
            not self.imported_draft or self.imported_draft.mode != MODE_RENDER_ONLY
        ):
            self._set_status("仅渲染高级流程需要先导入包含 render 模式的 YAML。")
            return
        self._start_draft(selected or MODE_QUICK_PREVIEW_ORIGINAL)

    @staticmethod
    def _media_check_summary(mode: str, *, subject: str) -> str:
        normalized = mode.strip().lower()
        if normalized == "decode":
            return f"{subject}将做完整解码检查，会额外顺序读取一次媒体。"
        if normalized == "fast":
            return f"{subject}将做快速结构检查；正式验收仍建议使用完整检查。"
        return f"{subject}已关闭媒体检查，仅建议用于排障。"

    def _start_draft(
        self,
        mode: str,
        *,
        render_original: bool | None = None,
        reuse_translation: bool | None = None,
    ) -> None:
        draft = self._draft(mode, render_original=render_original, reuse_translation=reuse_translation)
        problems = draft.validate()
        if problems:
            self._set_status("无法开始：" + " ".join(problems))
            return
        self.last_draft = draft
        self._log("[检查] " + self._media_check_summary(draft.source_media_check, subject="源视频"))
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
        if draft.manual_translation or draft.mode == MODE_STEP_EXPORT_MANUAL:
            review_directory = OverlayTui._review_directory(draft)
            return review_directory, "待人工翻译文件已生成。可打开复核目录查看 JSON、XLSX/TSV。"
        if draft.review or draft.mode == MODE_STEP_API_AND_REVIEW:
            review_directory = OverlayTui._review_directory(draft)
            return review_directory, "翻译与人工复核文件已生成。可打开复核目录继续复核。"
        if draft.mode in (
            MODE_ORIGINAL_PREVIEW,
            MODE_TRANSLATED_PREVIEW,
            MODE_QUICK_PREVIEW_ORIGINAL,
            MODE_QUICK_PREVIEW_TRANSLATED,
        ):
            return directory.resolve(), "预览任务完成。可打开结果目录检查生成的预览文件。"
        if (draft.render_original or draft.mode == MODE_ORIGINAL_PRODUCTION) and draft.mode not in (
            MODE_ORIGINAL_PREVIEW,
            MODE_QUICK_PREVIEW_ORIGINAL,
        ):
            return directory.resolve(), "原文渲染完成。可打开结果目录查看成片。"
        if draft.mode == MODE_TRANSLATE_ONLY:
            return directory.resolve(), "翻译任务完成。可打开结果目录查看翻译 JSON。"
        if draft.mode in (MODE_REUSE_RENDER, MODE_STEP_RESUME_RENDER) or draft.reuse_translation:
            return directory.resolve(), "复用翻译渲染完成。可打开结果目录查看成片。"
        if draft.mode == MODE_RENDER_ONLY:
            return directory.resolve(), "仅渲染任务完成。可打开结果目录查看成片。"
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
        try:
            queued = self.history.start(draft, label=label)
        except (OSError, ValueError) as exc:
            self._set_status(f"无法保存本地任务历史，任务未启动：{type(exc).__name__}")
            return
        if (
            isinstance(draft, TuiJobDraft)
            and len(command) > 1
            and Path(command[1]).resolve() == self._pipeline().resolve()
        ):
            snapshot = self.history.job_for(queued)
            if snapshot is not None:
                command = draft.build_command(sys.executable, self._pipeline(), job_path=snapshot)
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
        self._set_history_clear_enabled(False)
        self._set_status(label)
        self._log("$ " + " ".join(redact_command(command)))

    def _start_support_summary(self) -> None:
        """Generate a reviewable doctor summary without requiring media or an API."""
        report_directory = Path(__file__).resolve().parent.parent / "outputs" / "support-reports"
        report_path = report_directory / f"issue-summary-{time.time_ns()}.txt"
        self._start_command(
            "生成 Issue 自检摘要",
            [sys.executable, str(Path(__file__).with_name("support_report.py")), "--output", str(report_path)],
            result_directory=report_directory,
            completion_message=(
                "Issue 自检摘要已生成。打开结果目录查看；提交前仍请自行检查并删除私人路径、聊天内容和凭据。"
            ),
            task_kind="support-summary",
            require_result_manifest=True,
        )

    def _open_issue_template(self) -> None:
        """Open the repository's structured Bug report form in the default browser."""
        try:
            opened = webbrowser.open(self.ISSUE_TEMPLATE_URL)
        except webbrowser.Error:
            opened = False
        self._set_status("已打开 Bug 报告模板。" if opened else "无法自动打开浏览器，请使用 README 中的 Issue 链接。")

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
                    review_dir = self.result_directory or OverlayTui._review_directory(self.last_draft or self._draft())
                    guidance = (
                        f"【人工复核已就绪】已导出待校对表单至 {review_dir}。"
                        "请在 Excel/TSV 中完成翻译与核对后，将任务模式切换为【分步复核：载入复核表并压制】继续出片。"
                    )
                    self._set_status(guidance)
                    self._log("[提示] " + guidance)
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
            self._set_history_clear_enabled(True)

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
        preferred = ("video", "review_xlsx", "translation_json", "review_tsv", "preview_image", "support_summary")
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
        self._set_task_mode_options(include_advanced_render=False, value=MODE_QUICK_PREVIEW_ORIGINAL)
        self.last_draft = TuiJobDraft(video=video, chat_html=chat_html, mode=MODE_QUICK_PREVIEW_ORIGINAL)
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

    def _set_history_clear_enabled(self, enabled: bool) -> None:
        self.query_one("#history-clear", Button).disabled = not enabled

    def _has_unfinished_task(self) -> bool:
        """Keep history intact until a completed child has been recorded."""
        return bool(
            self.session
            and (
                self.session.running
                or (self.session.returncode is not None and self._handled_session is not self.session)
            )
        )

    def _refresh_history(self) -> None:
        history_list = self.query_one("#history-list", OptionList)
        history_list.clear_options()
        records = self.history.list_records()
        if not records:
            self.selected_history_id = None
            history_list.add_option(Option("暂无本机任务历史。", disabled=True))
            return
        selected_index: int | None = None
        for index, record in enumerate(records):
            stamp = time.strftime("%m-%d %H:%M", time.localtime(float(record.get("started_at") or 0)))
            result = self.history.result_for(record) or {}
            artifacts = result.get("artifacts") if isinstance(result, dict) else []
            count = len(artifacts) if isinstance(artifacts, list) else 0
            record_id = str(record.get("id") or "")
            diagnostic = "  有诊断" if record.get("diagnostic_path") else ""
            history_list.add_option(
                Option(
                    f"{record.get('state')}  {stamp}  {record.get('label', 'task')}  产物 {count}{diagnostic}",
                    id=record_id,
                )
            )
            if record_id == self.selected_history_id:
                selected_index = index
        if selected_index is not None:
            history_list.highlighted = selected_index

    def _select_history(self, record_id: str) -> None:
        self.selected_history_id = record_id
        record = self.history.get(record_id)
        if record is not None:
            self._set_status(f"已选择历史任务 {record_id}：{record.get('state', 'unknown')}。")

    def _history_record(self) -> dict | None:
        raw_id = self.selected_history_id
        if not raw_id:
            self._set_status("请先从历史列表选择一个任务。")
            return None
        record = self.history.get(raw_id)
        if record is None:
            self._set_status("所选历史任务已不存在；请刷新列表。")
            return None
        return record

    def _clear_history(self) -> None:
        if self._has_unfinished_task() or self.history.has_unfinished_records():
            self._set_status("存在正在运行或收尾中的任务，不能清空历史。")
            return
        if time.monotonic() > self._history_clear_confirmation_until:
            self._history_clear_confirmation_until = time.monotonic() + 10.0
            self._set_status("清空会删除本机任务快照和诊断；请在 10 秒内再次点击确认。")
            return
        self._history_clear_confirmation_until = 0.0
        if not self.history.clear():
            self._history_clear_confirmation_until = 0.0
            self._set_status("存在其他窗口正在运行的任务，历史未清空。")
            return
        self.active_history_id = None
        self.selected_history_id = None
        self._refresh_history()
        self._set_status("本机任务历史已清空。")

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
            if download.authentication_required:
                self.query_one(TabbedContent).active = "download"
                self._set_status("该任务使用过 OAuth；凭据未保存。请重新输入 OAuth 后点击开始下载。")
                return
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
        for kind in ("video", "review_xlsx", "translation_json", "review_tsv", "preview_image", "support_summary"):
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
        except (OSError, ValueError, Exception) as exc:
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
