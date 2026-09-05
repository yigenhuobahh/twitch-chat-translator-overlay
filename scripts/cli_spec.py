#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""render_cn_chat CLI 的单一定义源：argparse 构造 + 默认值字典 + 显式 flag 探测。

从 render_cn_chat._main 原样抽出，供 pipeline / job / layout / render preset 的
"CLI wins" 逻辑与 TUI 适配层共用：

- build_arg_parser(): render_cn_chat 的完整 argparse 定义。
- PIPELINE_CLI_DEFAULTS: argparse 默认值字典，是 apply_job_to_namespace /
  apply_layout_preset_to_namespace / apply_render_preset_to_namespace /
  apply_preview_first_defaults 里 "still at CLI default" 判断的唯一来源；
  与 parser 逐项 default 的同步由 tests/test_cli_flag_forward.py 的同步测试
  拦截（历史上曾因手工复刻漏同步而静默丢过 job YAML 字段）。
- _cli_flag_present(): 判断 sys.argv 里是否显式出现某 flag（显式性判断）。

render_cn_chat 顶部 re-export 这三个名字，旧的
``from render_cn_chat import PIPELINE_CLI_DEFAULTS`` 消费者不受影响。
"""

import argparse
from pathlib import Path
import sys

# Allow sibling imports when loaded as a script or via importlib from tests.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from common_utils import positive_float_arg


def _download_output_fps(value: str):
    """argparse type for --download-output-fps.

    Accepts a plain float ("60", "29.97") → float, or an exact fraction like
    "30000/1001" → the original string passed through (validated as a/b with
    b != 0; ffmpeg fps filter and -r both accept fractional expressions).
    """
    text = str(value).strip()
    if "/" in text:
        num, sep, den = text.partition("/")
        if not sep or not num.strip() or not den.strip():
            raise argparse.ArgumentTypeError(
                f"无效帧率分数: {value!r}（需要 形如 30000/1001 的 a/b 分数）"
            )
        try:
            numerator = float(num)
            denominator = float(den)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"无效帧率分数: {value!r}（分子/分母必须是数字）"
            ) from exc
        if not all(
            __import__("math").isfinite(x) for x in (numerator, denominator)
        ) or denominator == 0:
            raise argparse.ArgumentTypeError(
                f"无效帧率分数: {value!r}（分母不能为 0，且必须是有限数值）"
            )
        return text
    try:
        parsed = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"无效帧率: {value!r}（可用 60 / 29.97 或分数 30000/1001）"
        ) from exc
    if not __import__("math").isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError(f"帧率必须是正数: {value!r}")
    return parsed

# argparse defaults used by job/layout/render “CLI wins” application.
PIPELINE_CLI_DEFAULTS = {
    "video": None,
    "chat_html": None,
    "context": "livestream chat",
    "target_language": "zh",
    "profile": None,
    "layout_preset": None,
    "render_preset": None,
    "lazy_message_images": False,
    "message_image_cache_size": 256,
    "max_visible": 0,
    "msg_lifetime": 14.0,
    "max_message_lines": 0,
    "min_visible_seconds": 0.0,
    "arrival_interval": 0.0,
    "stack_mode": "lanes",
    "x_ratio": 0.0,
    "y_ratio": 0.0,
    "width_ratio": 0.0,
    "height_ratio": 0.0,
    "font_size_ratio": 0.0,
    "emote_height": 22,
    "translation_json": None,
    "reuse_translation": False,
    "force_export": False,
    "strict_import": False,
    "skip_translate": False,
    "manual_translation": False,
    "render_original": False,
    "review": False,
    "review_done": False,
    "review_tsv": None,
    "review_xlsx": None,
    "lint_translation": None,
    "lint_report": None,
    "lint_max_chars": 90,
    "rules": None,
    "output": None,
    "doctor": False,
    "init": False,
    "init_job": False,
    "list_jobs": False,
    "job": None,
    "mode": "auto",
    # 与 argparse 的 --source-media-check 默认值一致；缺了这条，job YAML 的
    # source_media_check 会落进 apply_job_to_namespace 的 unknown-default 分支
    # （非 None/False 即跳过）而被静默丢弃。
    "source_media_check": "fast",
    "workdir": None,
    "dry_run": False,
    "quiet": False,
    "verbose": False,
    "x": 15,
    "y": 327,
    "width": 497,
    "height": 363,
    "font_size": 15,
    "font_path": "auto",
    "font_bold_path": "auto",
    "fps": 15,
    "output_fps": None,
    "bg_alpha": 255,
    "keep_temp": False,
    "no_backup_prev": False,
    "offset": None,
    "clean": False,
    "clean_progress": False,
    "preview_frame": None,
    "preview_image": None,
    "preview_clip": None,
    "preview_dense": False,
    "yes": False,
    "batch_size": 10,
    "workers": 4,
    "encoder": "x264",
    "video_preset": None,
    "crf": 18,
    "video_bitrate": None,
    "maxrate": None,
    "bufsize": None,
    "audio_codec": "aac",
    "audio_bitrate": "192k",
    "overlay_codec": "vp9",
    "webm_crf": 30,
    "webm_cpu_used": 4,
    "no_reuse_static_frames": False,
    "no_skip_blank_frames": False,
    "blank_hold_seconds": 0.5,
    # 下载 / doctor 引导 / --clean 等非 preset 字段也逐项登记：同步测试
    # (tests/test_cli_flag_forward.py) 要求字典与 argparse 逐项默认值完全一致，
    # 漏登记曾让 job/layout 的 "still at default" 判断失真（如 source_media_check）。
    "offer_fix": False,
    "fix_yes": False,
    "offer_td_cli": False,
    "download": None,
    "download_dir": None,
    "download_only": False,
    "quality": "1080p60",
    "begin": None,
    "end": None,
    "segment": None,
    "cut": None,
    "download_output_fps": None,
    "download_encoder": "auto",
    "download_trim_mode": "Safe",
    "media_check": "fast",
    "media_repair": "audio",
    "kind": "auto",
    "oauth": None,
    "install_td_prompt": False,
    "clean_all": False,
}


def _cli_flag_present(*flags: str) -> bool:
    """True if any of the given CLI flags appear in sys.argv (explicit user intent).

    已知限制: 不识别 argparse 的无歧义前缀缩写（如 ``--overlay-cod`` 代表
    ``--overlay-codec``）。缩写形式会被判为"未显式传入"而走默认值分支——
    只影响这里的显式性判断（例如 preview 降级检查），不影响 argparse 实际解析。
    """
    argv = sys.argv[1:]
    for flag in flags:
        if flag in argv:
            return True
        # also match --flag=value form
        prefix = flag + "="
        if any(a.startswith(prefix) for a in argv):
            return True
    return False


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the render_cn_chat argument parser.

    定义原样取自原 _main 内联块（W1-A1 抽出）；末尾以
    ``set_defaults(**PIPELINE_CLI_DEFAULTS)`` 注入字典默认值，
    使 PIPELINE_CLI_DEFAULTS 成为默认值的单一权威来源。
    """
    parser = argparse.ArgumentParser(description="Generate translated chat overlay video from Twitch HTML")
    parser.add_argument("video", nargs="?", help="Source video path, e.g. video.mp4")
    parser.add_argument("chat_html", nargs="?", help="Twitch chat HTML path, e.g. chat.html")
    parser.add_argument("--context", default="livestream chat", help="Background context passed to the translator")
    parser.add_argument("--target-language", default="zh", help="Target language for translation (e.g. zh, ja, ko, en). Default: zh")
    parser.add_argument("--profile", default=None, help="翻译 profile YAML，例如 profiles/default.yaml；会合并 context、glossary、preserve 和 style")
    parser.add_argument(
        "--layout-preset",
        default=None,
        help="渲染布局 YAML 或短名，例如 profiles/layout_default.yaml 或 compact；命令行布局参数优先覆盖",
    )
    parser.add_argument(
        "--render-preset",
        default=None,
        help="编码/性能 YAML 或短名，例如 profiles/render_default.yaml 或 fast；命令行参数优先覆盖",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="首次脚手架：创建 .env（从 .env.example）与 jobs/example_job.yaml，打印推荐命令",
    )
    parser.add_argument(
        "--offer-fix",
        action="store_true",
        help="doctor 时直接进入修复流程（不先问总开关）；默认在缺 FFmpeg 时也会询问是否帮忙安装",
    )
    parser.add_argument(
        "--fix-yes",
        action="store_true",
        help="与 --offer-fix 联用：非交互默认同意修复步骤（CI/脚本用）",
    )
    parser.add_argument(
        "--offer-td-cli",
        action="store_true",
        help="可选增强: 自动下载/引导安装 TwitchDownloaderCLI 到 tools/（需确认；--yes 直接下载）",
    )
    parser.add_argument(
        "--download",
        default=None,
        metavar="URL_OR_ID",
        help="用 TwitchDownloaderCLI 下载 VOD/Clip 视频 + 嵌入表情的聊天 HTML（可选增强）",
    )
    parser.add_argument(
        "--download-dir",
        default=None,
        help="--download 输出目录（默认 downloads/<id>_<时间>/）",
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="与 --download 联用：只下载并打印路径后退出（不进入下一步菜单）",
    )
    parser.add_argument(
        "--quality",
        default="1080p60",
        help="--download 视频画质（默认 1080p60；不可用时 CLI 会回退）",
    )
    parser.add_argument(
        "--begin",
        default=None,
        help="--download 裁切起点（仅 VOD；如 0:01:40 或 100s）",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="--download 裁切终点（仅 VOD）",
    )
    parser.add_argument(
        "--segment",
        action="append",
        default=None,
        metavar="BEGIN-END",
        help=(
            "--download 多段裁切（可重复；同一 VOD）。"
            "例: --segment 0:10:00-0:12:30 --segment 0:40:00-0:43:00；"
            "与 --begin/--end 同时出现时以 --segment 为准"
        ),
    )
    parser.add_argument(
        "--cut",
        action="append",
        default=None,
        metavar="START-END",
        help=(
            "合并后切除时间段（可重复）。"
            "例: --cut 21:01-22:59 删除合并视频的第 21 分 01 秒到 22 分 59 秒。"
            "时间轴自动前移，聊天同步裁剪。仅与 --segment 多段下载联用。"
        ),
    )
    parser.add_argument(
        "--download-output-fps",
        type=_download_output_fps,
        default=None,
        help="合并视频强制 CFR 帧率（如 60；也支持精确分数 30000/1001）。不指定则保持源帧率。",
    )
    parser.add_argument(
        "--download-encoder",
        default="auto",
        choices=["auto", "x264", "nvenc", "qsv", "amf"],
        help="合并视频编码器（默认 auto 自动探测硬件编码器）",
    )
    parser.add_argument(
        "--download-trim-mode",
        default="Safe",
        choices=["Safe", "Exact"],
        help="VOD 裁切模式；Safe（默认、推荐）避免 Exact 的时间戳偏移，Exact 仅用于明确需要精确裁切时。",
    )
    parser.add_argument(
        "--media-check",
        default="fast",
        choices=["off", "fast", "decode"],
        help="媒体健康门禁：fast=流/时长/AAC包检查（默认）；decode=额外完整解码；off=不建议，仅跳过检查。",
    )
    parser.add_argument(
        "--source-media-check",
        default="fast",
        choices=["off", "fast", "decode"],
        help="本地输入视频门禁：fast=快速检查（默认）；decode=翻译/渲染前完整解码；off=仅用于排障。",
    )
    parser.add_argument(
        "--media-repair",
        default="audio",
        choices=["off", "audio"],
        help="健康失败时自动尝试非破坏性音频时间轴修复（默认 audio）；输出 *.repaired.mp4，原下载不覆盖；off 可禁用。",
    )
    parser.add_argument(
        "--kind",
        default="auto",
        choices=["auto", "vod", "clip"],
        help="--download 源类型（默认 auto 识别）",
    )
    parser.add_argument(
        "--oauth",
        default=None,
        help="TwitchDownloaderCLI --oauth（订阅限定 VOD；勿提交到 git）",
    )
    parser.add_argument(
        "--install-td-prompt",
        action="store_true",
        help="安装脚本用：交互询问是否配置可选 TwitchDownloaderCLI（默认 No）",
    )
    parser.add_argument(
        "--init-job",
        action="store_true",
        help="引导式创建带注释的 jobs/<name>.yaml（交互问答）",
    )
    parser.add_argument(
        "--list-jobs",
        action="store_true",
        help="列出 jobs/ 下的任务配置",
    )
    parser.add_argument(
        "--job",
        default=None,
        help="从 job.yaml 加载 video/chat/output/presets 等；显式 CLI 仍优先；也可用短名",
    )
    parser.add_argument(
        "--mode",
        default="auto",
        choices=["auto", "preview", "translate", "render", "full"],
        help="场景模式: auto/full=完整流程; preview=默认10s预览; translate=只翻译; render=只渲染(需 reuse/original)",
    )
    parser.add_argument("--lazy-message-images", action="store_true", help="长片省内存：转发给 burn 的消息图 LRU 缓存模式")
    parser.add_argument("--message-image-cache-size", type=int, default=256, help="lazy 消息图缓存上限，默认 256")
    parser.add_argument(
        "--max-visible",
        type=int,
        default=0,
        help=(
            "最大同时可见消息数；默认 0=按框高/字号自动填满；"
            "显式 N 固定条数；若 N 大于框高可容纳行数会自动钳制并告警，避免弹幕叠在顶部"
        ),
    )
    parser.add_argument(
        "--msg-lifetime",
        type=positive_float_arg,
        default=14.0,
        help="消息停留秒数（仅 stack_mode=lanes；float 上浮模式忽略；必须 > 0）",
    )
    parser.add_argument("--max-message-lines", type=int, default=0, help="单条消息最多显示行数；0 表示不额外限制")
    parser.add_argument(
        "--min-visible-seconds",
        type=float,
        default=0.0,
        help="已上屏消息最短可见秒数（仅 stack_mode=lanes；float 忽略）；0 表示允许立即被顶替",
    )
    parser.add_argument("--arrival-interval", type=float, default=0.0, help="新消息最小入场间隔秒数；0 表示不限流")
    parser.add_argument(
        "--stack-mode",
        choices=("float", "lanes"),
        default="lanes",
        help="聊天堆叠: lanes=lifetime lane沉积(默认), float=Twitch上浮(仅容量顶出)",
    )
    parser.add_argument("--x-ratio", type=float, default=0.0, help="相对源视频宽度的 X 坐标；0 使用 --x")
    parser.add_argument("--y-ratio", type=float, default=0.0, help="相对源视频高度的 Y 坐标；0 使用 --y")
    parser.add_argument("--width-ratio", type=float, default=0.0, help="相对源视频宽度的 overlay 宽度；0 使用 --width")
    parser.add_argument("--height-ratio", type=float, default=0.0, help="相对源视频高度的 overlay 高度；0 使用 --height")
    parser.add_argument("--font-size-ratio", type=float, default=0.0, help="相对源视频高度的字号；0 使用 --font-size")
    parser.add_argument("--emote-height", type=int, default=22, help="emote 高度像素")
    parser.add_argument("--translation-json", default=None, help="翻译 JSON 路径，默认 <视频名>_translation.json")
    parser.add_argument("--reuse-translation", action="store_true", help="如果翻译 JSON 已存在，跳过导出和翻译，直接渲染")
    parser.add_argument(
        "--force-export",
        action="store_true",
        help="允许覆盖已有非空 translation 的 JSON（默认拒绝；防丢译）。仅影响导出步骤",
    )
    parser.add_argument(
        "--strict-import",
        action="store_true",
        help="导入翻译渲染时：author/timestamp/original 不一致则硬失败（转发给 burn；默认跳过错配）",
    )
    parser.add_argument("--skip-translate", action="store_true", help="只导出翻译 JSON，不调用翻译和渲染")
    parser.add_argument("--manual-translation", action="store_true", help="不调用 LLM；导出待翻译 JSON 和人工复核 XLSX/TSV 后停止")
    parser.add_argument("--render-original", action="store_true", help="不导出、不翻译，直接将原始聊天文本和已有 emote 渲染到视频")
    parser.add_argument("--review", action="store_true", help="LLM 翻译后导出中英对照 TSV 并停止，等待人工复核")
    parser.add_argument("--review-done", action="store_true", help="从人工复核 TSV 回写翻译后再渲染")
    parser.add_argument("--review-tsv", default=None, help="人工复核 TSV 路径，默认 <视频名>_translation_review.tsv")
    parser.add_argument("--review-xlsx", default=None, help="人工复核 XLSX 路径，默认 <视频名>_translation_review.xlsx")
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="非交互：翻译完成后不暂停等回车，直接渲染（默认交互会导出 Excel 并等待确认）",
    )
    parser.add_argument("--lint-translation", nargs="?", const="__PIPELINE__", default=None, help="检查翻译 JSON；可单独传 JSON 路径，或在 pipeline 中不带值使用")
    parser.add_argument("--lint-report", default=None, help="导出翻译质检 TSV 报告路径")
    parser.add_argument("--lint-max-chars", type=int, default=90, help="翻译长度告警阈值，默认 90 字")
    parser.add_argument("--rules", default=None, help="YAML 规则文件路径，例如 configs/rules.example.yaml")
    parser.add_argument("--output", default=None, help="最终输出路径；默认使用 twitch_chat_burn.py 的 <视频名>_chat.mp4")
    parser.add_argument("--doctor", action="store_true", help="检查 Python、依赖、FFmpeg、字体和翻译环境变量")
    parser.add_argument("--workdir", default=None, help="独立工作目录，所有中间文件和输出将归档到此目录")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划执行步骤，不实际运行")
    parser.add_argument("--quiet", action="store_true", help="减少输出")
    parser.add_argument("--verbose", action="store_true", help="显示详细输出")

    parser.add_argument("--x", type=int, default=15)
    parser.add_argument("--y", type=int, default=327)
    parser.add_argument("--w", "--width", dest="width", type=int, default=497)
    parser.add_argument("--h", "--height", dest="height", type=int, default=363)
    parser.add_argument("--font-size", type=int, default=15)
    parser.add_argument("--font-path", default="auto", help="字体文件路径；auto 为自动检测 CJK 字体")
    parser.add_argument("--font-bold-path", default="auto", help="粗体字体路径；auto 为自动检测")
    parser.add_argument("--fps", type=int, default=15, help="弹幕 overlay 渲染帧率（默认 15；不强制成片帧率）")
    parser.add_argument(
        "--output-fps", type=float, default=None,
        help="最终成片视频帧率（可用 29.97 等分数帧率）；默认跟随源视频",
    )
    parser.add_argument("--bg-alpha", type=int, default=255, help="聊天背景透明度 0-255；255 为不透明黑底（默认），170 为半透明")
    parser.add_argument("--keep-temp", action="store_true", help="保留底层渲染中间文件，方便失败后排查/续跑")

    parser.add_argument("--no-backup-prev", action="store_true", help="不备份旧输出文件（默认自动备份为 .bak）")
    parser.add_argument("--offset", type=float, default=None, help="时间偏移修正秒数；默认交给 twitch_chat_burn.py 自动判断")
    parser.add_argument(
        "--clean",
        action="store_true",
        help="清理 --workdir 下临时文件后退出（无 workdir 时用视频目录/当前目录）：默认只删 *.partial.mp4；加 --clean-all 才删全部已结束 job_/batch_；默认不删 *.progress.json",
    )
    parser.add_argument(
        "--clean-all",
        action="store_true",
        help="与 --clean 联用：删除 workdir/out 下全部已结束的工具 job_/batch_ 目录（仍跳过 running）",
    )
    parser.add_argument(
        "--clean-progress",
        action="store_true",
        help="与 --clean 联用：同时删除 *.progress.json 进度文件",
    )
    parser.add_argument("--preview-frame", type=float, default=None, help="只导出指定秒数的一张预览图，不渲染整片")
    parser.add_argument("--preview-image", default=None, help="预览图输出路径；默认 <视频名>_preview_<秒数>s.png")
    parser.add_argument(
        "--preview-clip",
        type=float,
        default=None,
        help="只渲染 N 秒短片；默认真从 0 秒开始，可用 --preview-dense 选弹幕最密段；仅预览模式使用，正式出片请勿依赖它截断时长",
    )
    parser.add_argument(
        "--preview-dense",
        action="store_true",
        help="与 --preview-clip 联用：自动选弹幕最密时间窗",
    )
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--workers", type=int, default=4)
    # Performance / encode (forwarded to twitch_chat_burn.py)
    parser.add_argument("--encoder", default="x264", choices=["auto", "x264", "nvenc", "qsv", "amf"],
                        help="最终视频编码器；auto 优先硬件，默认 x264 最稳")
    parser.add_argument("--video-preset", default=None, help="编码预设（x264/nvenc/qsv/amf 各自体系）")
    parser.add_argument("--crf", type=int, default=18, help="质量 CRF/CQ，默认 18")
    parser.add_argument("--video-bitrate", default=None, help="视频码率，如 8M")
    parser.add_argument("--maxrate", default=None, help="最大码率")
    parser.add_argument("--bufsize", default=None, help="码率缓冲")
    parser.add_argument("--audio-codec", default="aac", choices=["aac", "copy"])
    parser.add_argument("--audio-bitrate", default="192k")
    parser.add_argument("--overlay-codec", default="vp9", choices=["vp9", "png"],
                        help="聊天层：vp9 中间 WebM 或 png 直接叠加")
    parser.add_argument("--webm-crf", type=int, default=30)
    parser.add_argument("--webm-cpu-used", type=int, default=4, help="VP9 速度 0-8，默认 4")
    parser.add_argument("--no-reuse-static-frames", action="store_true")
    parser.add_argument("--no-skip-blank-frames", action="store_true")
    parser.add_argument("--blank-hold-seconds", type=float, default=0.5)
    # 单源化：字典是默认值的唯一权威来源，末尾统一注入；逐项 default 与字典
    # 的同步（两侧都不许漂移）由 tests/test_cli_flag_forward.py 的同步测试守护。
    parser.set_defaults(**PIPELINE_CLI_DEFAULTS)
    return parser
