#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键生成翻译后的 Twitch 聊天覆盖视频
================================

输入：源视频 + Twitch 原始/导出的聊天 HTML
输出：带翻译后 chat overlay 的视频（默认 <视频名>_chat.mp4）

流程：
1. twitch_chat_burn.py --export-translation 导出待翻译 JSON
2. translate_chat_openai.py 使用 OpenAI-compatible 接口并发翻译 JSON
3. 可选 YAML 规则清洗（例如频道梗、术语替换）
4. twitch_chat_burn.py --import-translation 渲染并合成视频

示例：
  python render_cn_chat.py --init
  python render_cn_chat.py --doctor
  python render_cn_chat.py --job jobs/example_job.yaml
  python render_cn_chat.py video.mp4 chat.html --mode preview --render-original
  python render_cn_chat.py video.mp4 chat.html --reuse-translation --rules configs/rules.example.yaml
  python render_cn_chat.py video.mp4 chat.html --profile profiles/default.yaml
  python render_cn_chat.py video.mp4 chat.html --preview-frame 60 --preview-image preview.png
"""

import glob
import json
import os
from pathlib import Path
import shutil  # noqa: F401 - 测试按模块属性 patch pipeline.shutil.copy2；publish_output 实现居 review_tables，双方共享同一 shutil 模块对象，patch 仍然生效
import sys
import uuid

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - PyYAML 是必需依赖，缺纸上 --job/--preset 已有兜底
    yaml = None  # type: ignore

# Allow sibling imports when loaded as a script or via importlib from tests.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from chat_window import apply_preview_first_defaults
from common_utils import (
    atomic_write_json,  # noqa: F401 - 门面 re-export（tests 经 pipe.atomic_write_json 消费；实现单源在 common_utils，review_tables 直接引用）
    current_cli_invocation,
    ensure_utf8_stdio,
    load_dotenv_if_present,
    quote_cli_arg,
    resolve_font_paths,
    resolve_public_resource,
    validate_positive_float,
)
from common_utils import (
    stdin_is_interactive as _stdin_is_interactive,
)
from env_bootstrap import (
    maybe_prompt_offer_td_cli,
    offer_td_cli_guide,
    prepend_tools_ffmpeg_to_path,
    probe_translate_api,
)
from job_config import (
    apply_job_to_namespace,
    load_job_file,
    save_last_job,
    validate_job_media_paths,
)
from job_wizard import run_job_wizard, run_list_jobs
from layout_preset import apply_layout_preset_to_namespace, load_layout_preset
from media_health import validate_media_health
import pipeline_plan as _pipeline_plan
from pipeline_runner import PipelineRunner, activate_runner, active_runner, emit_task_event
from process_util import (
    clean_companion_flags_error,
    clean_temp_artifacts,
    install_process_cleanup_handlers,
    is_dangerous_publish_path,
    run_tracked,
)
from render_preset import apply_render_preset_to_namespace, load_render_preset

# 复核表 / 翻译质检（lint）+ YAML/发布/规则清洗簇：实现单源在 review_tables.py
# （刀四/刀五原样搬运）。这里保留同名 re-export，`from render_cn_chat import
# PipelineError / load_yaml_rules / publish_output ...` 等历史消费者不断；
# 依赖 DRY_RUN/log 的函数另留薄包装（见下方"门面"区）。
import review_tables as review_tables
from review_tables import (  # noqa: F401 - re-exported for tests/CLI compatibility
    LINT_BRACKET_TOKEN_RE,
    LINT_MENTION_RE,
    LINT_PURE_EMOTE_RE,
    LINT_URL_RE,
    PipelineError,
    _lint_issue,
    _review_issue_map,
    _review_rows,
    load_profile,
    load_yaml_file,
    load_yaml_rules,
    publish_output,
)

# C-O8：非空译文计数单源在 translation_io.py，经 twitch_chat_burn 门面
# re-export 引入；render_cn_chat 不再保留 _translation_nonempty_count 本地副本。
from twitch_chat_burn import translation_json_nonempty_count
from ux_setup import run_init

ensure_utf8_stdio()

_TASK_STAGE_BY_PROGRAM = {
    "translate_chat_openai.py": "translate",
    "twitch_chat_burn.py": "render",
}
load_dotenv_if_present()


# Compatibility exports for callers that historically imported forwarding
# helpers from this pipeline module.  The canonical definitions now live in
# pipeline_plan.py so the TUI and command-line adapters use the same rules.
BURN_ONLY_FLAGS = _pipeline_plan.BURN_ONLY_FLAGS
FPS_FORWARD_SPECS = _pipeline_plan.FPS_FORWARD_SPECS
LAYOUT_FORWARD_SPECS = _pipeline_plan.LAYOUT_FORWARD_SPECS
PERF_FORWARD_SPECS = _pipeline_plan.PERF_FORWARD_SPECS
SHARED_FORWARD_FLAGS = _pipeline_plan.SHARED_FORWARD_FLAGS
_append_flag_specs = _pipeline_plan.append_flag_specs
append_fps_args = _pipeline_plan.append_fps_args
append_layout_burn_args = _pipeline_plan.append_layout_burn_args
append_perf_encode_args = _pipeline_plan.append_perf_encode_args
append_shared_burn_args = _pipeline_plan.append_shared_burn_args
append_strict_import_arg = _pipeline_plan.append_strict_import_arg
# 烧录命令组装单源：render-original 与 import-translation 两个分支共用。
build_burn_command = _pipeline_plan.build_burn_command

# CLI 定义单源迁至 cli_spec.py（argparse 构造 + PIPELINE_CLI_DEFAULTS +
# _cli_flag_present）；re-export 保持 `from render_cn_chat import
# PIPELINE_CLI_DEFAULTS` 等历史消费者不断（tests/test_cli_flag_forward.py、
# scripts/job_config.py 的注释引用等）。
from cli_spec import PIPELINE_CLI_DEFAULTS, _cli_flag_present, build_arg_parser

DRY_RUN = False
VERBOSE = False
QUIET = False


# PipelineError 单源已迁至 review_tables.py（刀五），文件头 re-export 同一个
# 类对象：`except PipelineError` / `pytest.raises(pipe.PipelineError)` 语义不变。


def mark_manual_translation_required() -> None:
    """Record that a requested translated task stopped for human input."""
    runner = active_runner()
    if runner is not None:
        runner.mark_manual_required()


def validate_source_media(video: Path, *, mode: str, dry_run: bool = False) -> None:
    """Fail before translation when the local input cannot be decoded safely."""
    selected_mode = str(mode or "fast").lower()
    if selected_mode == "off":
        log("[media] 输入视频健康检查已关闭。")
        emit_task_event("stage_skipped", stage="source_media_check", reason="disabled", completed=0, total=1)
        return
    if dry_run:
        log(f"[dry-run] 跳过输入视频健康检查（{selected_mode}）。")
        emit_task_event("stage_skipped", stage="source_media_check", reason="dry_run", completed=0, total=1)
        return

    label = "完整解码" if selected_mode == "decode" else "快速"
    log(f"[media] {label}检查输入视频，发现问题会在翻译/渲染前停止…")
    emit_task_event("stage_started", stage="source_media_check", completed=0, total=1)
    # Local workflows may legitimately use silent video. Validate its video
    # stream and any present audio stream without turning silence into failure.
    health = validate_media_health(video, mode=selected_mode, require_audio=False)
    if not health.ok:
        emit_task_event("stage_failed", stage="source_media_check", completed=0, total=1)
        raise PipelineError(
            "错误: 输入视频健康检查失败，已在翻译或渲染前停止。\n"
            f"  详情: {health.reason()}\n"
            "  建议: 重新下载有问题的片段，或使用 --source-media-check fast 进行快速复查。"
        )
    for warning in health.warnings:
        log(f"[media] 提示: {warning}")
    emit_task_event("stage_completed", stage="source_media_check", completed=1, total=1)


def log(msg, level="info"):
    if QUIET and level == "info":
        return
    if VERBOSE or level != "debug":
        print(msg, flush=True)


def _render_preview_clip(
    *,
    video: Path,
    chat_html: Path,
    trans_json: Path,
    args,
    workdir: Path | None,
    seconds: float,
    burn: Path,
) -> Path | None:
    """Render a short preview clip with translated chat overlay. Returns output path or None on failure."""
    preview_dir = (workdir / "temp") if workdir else Path("outputs") / "_preview"
    if is_dangerous_publish_path(preview_dir):
        print(f"  [FAIL] 预览目录在系统路径下，已拒绝: {preview_dir}", flush=True)
        return None
    preview_dir.mkdir(parents=True, exist_ok=True)

    # 命令组装单源：preview 分支与主管线（render-original / import-translation）
    # 共用 pipeline_plan.build_burn_command，避免三处手搓命令漂移。生成主体
    # （--import-translation + --strict-import + 共享表 + --offset +
    # keep-temp/no-backup-prev/preview-frame/preview-image 转发 + 收尾
    # --preview-dense）与主管线一致；preview 只追加自己特有的
    # --out-dir 与 --preview-clip（秒数来自交互输入而非 args.preview_clip，
    # 追加在最后使 argparse last-wins 语义正确）。
    cmd = build_burn_command(
        args, video, chat_html, burn,
        trans_json=trans_json,
    )
    cmd.extend(["--out-dir", str(preview_dir)])
    cmd.extend(["--preview-clip", str(seconds)])

    log(f"\n[预览] 渲染 {seconds}s 预览片段...")
    try:
        run(cmd, error_hint="预览渲染失败")
    except PipelineError as e:
        print(f"  [FAIL] 预览渲染失败: {e}", flush=True)
        return None

    # burn compose names preview clips as <stem>_chat.mp4 (same as full burns);
    # also accept any *_preview_*s.mp4 if naming changes later.
    # glob.escape: stem 里的 [*?] 等元字符必须按字面匹配，否则带特殊字符的
    # 视频名（如 "clip [x].mp4"）会匹配到错误的候选文件。
    candidates = list(preview_dir.glob(f"{glob.escape(video.stem)}_chat.mp4"))
    if not candidates:
        candidates = list(preview_dir.glob(f"{glob.escape(video.stem)}_preview_*s.mp4"))
    if not candidates:
        # Job-dir layout: out_dir may contain job_*/<stem>_chat.mp4
        candidates = list(preview_dir.glob(f"**/job_*/{glob.escape(video.stem)}_chat.mp4"))
    if candidates:
        # Prefer newest if multiple
        preview_out = max(candidates, key=lambda p: p.stat().st_mtime)
    else:
        print(f"  [WARN] 未找到预览输出文件（期望 {video.stem}_chat.mp4）", flush=True)
        return None
    log(f"[预览] 已生成: {preview_out}")
    # Best-effort open the preview (Windows).
    if os.name == "nt":
        try:
            os.startfile(str(preview_out))
        except OSError:
            pass
    return preview_out


def pause_after_translation_for_review(
    *,
    trans_json: Path,
    review_xlsx: Path,
    review_tsv: Path,
    auto_continue: bool = False,
    # Preview support
    video: Path | None = None,
    chat_html: Path | None = None,
    args=None,
    workdir: Path | None = None,
    burn: Path | None = None,
) -> str:
    """After API/rules translation: export Excel and wait for Enter before render.

    Returns:
      "continue" — proceed to render (optionally after user edited XLSX; caller may re-import)
      "stop" — user chose to stop here (same spirit as --review)
    Non-interactive / --yes / dry-run: prints paths and continues without blocking.
    """
    # Always refresh review tables so user has something to open.
    # JSON 只解析一次、lint 只跑一次，两个导出共用同一份结果。
    try:
        data, issue_map = _prepare_review_export(trans_json)
        export_review_tsv(trans_json, review_tsv, data=data, issue_map=issue_map)
        export_review_xlsx(trans_json, review_xlsx, data=data, issue_map=issue_map)
    except Exception as e:
        log(f"[WARN] 导出复核表失败（仍可继续渲染）: {e}")

    print("\n======== 翻译已完成 · 渲染前确认 ========", flush=True)
    print(f"  翻译 JSON : {trans_json}", flush=True)
    if review_xlsx.is_file():
        print(f"  Excel 复核: {review_xlsx}", flush=True)
        print("  （请打开 XLSX，检查/修改最后一列 translation）", flush=True)
    if review_tsv.is_file():
        print(f"  TSV 备份  : {review_tsv}", flush=True)
    print("----------------------------------------", flush=True)

    if auto_continue or DRY_RUN or not _stdin_is_interactive():
        if not auto_continue and not DRY_RUN:
            print("  （非交互终端：自动继续渲染。交互运行时会在此等待回车。）", flush=True)
        else:
            print("  （--yes / dry-run：不暂停，继续渲染）", flush=True)
        return "continue"

    # Best-effort open Excel for the user (Windows).
    if review_xlsx.is_file() and os.name == "nt":
        try:
            os.startfile(str(review_xlsx))  # type: ignore[attr-defined]
            print("  已尝试用默认程序打开 Excel。", flush=True)
        except OSError:
            pass

    # workdir is optional: without it, _render_preview_clip writes under outputs/_preview.
    can_preview = all(v is not None for v in (video, chat_html, args, burn))
    while True:
        print("  回车 = 继续渲染（若改过 XLSX 会先自动回写）", flush=True)
        if can_preview:
            print("  P    = 先渲染一小段预览片段（默认 10 秒；无 --workdir 时写到 outputs/_preview）", flush=True)
            print("  P 30 = 渲染 30 秒预览片段", flush=True)
        print("  S    = 先停在这里，稍后用 --review-done 再渲染", flush=True)
        try:
            raw = input("请选择 [回车继续" + (" / P 预览" if can_preview else "") + " / S 停止]: ").strip()
        except EOFError:
            # stdin 关闭 = 无人监督（管道/断开的终端）：自动继续渲染数小时不安全，
            # 与 "S" 同义停在翻译完成点（--review-done 可恢复）。
            raw = "s"

        low = raw.lower()

        if low in ("s", "stop", "q", "quit"):
            print("\n[OK] 已暂停。改完 Excel 后可用：", flush=True)
            resume_hint = (
                f"{current_cli_invocation()} "
                f"{quote_cli_arg(video or 'video.mp4')} "
                f"{quote_cli_arg(chat_html or 'chat.html')} "
                f"--reuse-translation --review-done "
                f"--translation-json {quote_cli_arg(trans_json)} "
                f"--review-xlsx {quote_cli_arg(review_xlsx)}"
            )
            if workdir is not None:
                resume_hint += f" --workdir {quote_cli_arg(workdir)}"
            output = getattr(args, "output", None)
            if output:
                resume_hint += f" --output {quote_cli_arg(output)}"
            print(f"  {resume_hint}", flush=True)
            return "stop"

        parts = raw.split(None, 1)
        if can_preview and parts and parts[0].lower() == "p":
            # "P" -> 10s; "P 30" -> 30s
            seconds = 10.0
            if len(parts) == 2:
                try:
                    seconds = validate_positive_float(
                        "preview seconds", float(parts[1]), maximum=3600.0
                    )
                except (TypeError, ValueError):
                    print("  秒数无效（须在 0 到 3600 之间），使用默认 10 秒", flush=True)
                    seconds = 10.0
            preview_path = _render_preview_clip(
                video=video,
                chat_html=chat_html,
                trans_json=trans_json,
                args=args,
                workdir=workdir,
                seconds=seconds,
                burn=burn,
            )
            if preview_path is not None:
                print(f"  预览已生成: {preview_path}", flush=True)
                print("  请检查预览效果，然后回车继续渲染或 S 停止。", flush=True)
            continue

        # Empty / unknown -> continue to render
        return "continue"


def run(cmd, cwd=None, error_hint=""):
    launcher = Path(str(cmd[0])).stem.lower()
    program_arg = cmd[1] if len(cmd) > 1 and launcher.startswith("python") else cmd[0]
    program = Path(str(program_arg)).name
    stage = _TASK_STAGE_BY_PROGRAM.get(program)
    if DRY_RUN:
        log(f"[dry-run] {' '.join(str(c) for c in cmd)}")
        emit_task_event("command_skipped", program=program, reason="dry_run")
        if stage:
            emit_task_event("stage_skipped", stage=stage, reason="dry_run", completed=0, total=1)
        return
    log("\n$ " + " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd))
    emit_task_event("command_started", program=program)
    if stage:
        emit_task_event("stage_started", stage=stage, completed=0, total=1)
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        # Inherit stdio (None) so child progress remains visible, but still track
        # the process tree for Ctrl+C / atexit cleanup.
        p = run_tracked(cmd, cwd=cwd, text=False, env=env, stdout=None, stderr=None)
    except FileNotFoundError as e:
        emit_task_event("command_failed", program=program, reason="not_found")
        if stage:
            emit_task_event("stage_failed", stage=stage, reason="not_found", completed=0, total=1)
        hint = error_hint or "找不到可执行文件，请确认已安装并加入 PATH"
        raise PipelineError(f"错误: {hint}\n  详情: {e}")
    emit_task_event("command_exited", program=program, returncode=p.returncode)
    if stage:
        emit_task_event(
            "stage_completed" if p.returncode == 0 else "stage_failed",
            stage=stage,
            completed=1 if p.returncode == 0 else 0,
            total=1,
        )
    if p.returncode != 0:
        hint = error_hint or "命令执行失败"
        raise PipelineError(f"错误: {hint} (exit code {p.returncode})")


# C-O8：_translation_nonempty_count 本地副本已删除；调用点统一改用
# twitch_chat_burn 门面的 translation_json_nonempty_count（实现居
# translation_io.py，行为一致：缺文件/不可解析均计 0）。


def _post_download_next_steps(video: Path, chat_html: Path, *, download_only: bool, yes: bool) -> int:
    """After assets land: print paths and optionally interactive next-step menu."""
    print("\n======== 下载完成 ========")
    print(f"  视频: {video}")
    print(f"  聊天: {chat_html}")
    print("  下一步示例:")
    print(
        f"    {current_cli_invocation()} {quote_cli_arg(video)} {quote_cli_arg(chat_html)} "
        f"--mode preview --render-original --preview-clip 10"
    )
    print(
        f"    {current_cli_invocation()} {quote_cli_arg(video)} {quote_cli_arg(chat_html)} --manual-translation"
    )
    if download_only or yes or not _stdin_is_interactive():
        return 0
    print("\n请选择下一步:")
    print("  [1] 预览短片（原文 10s）")
    print("  [2] 导出人工翻译表")
    print("  [3] 翻译出片（API 可用则自动译）")
    print("  [0] 结束（仅保留已下载文件）")
    try:
        choice = input("请选择 [0-3] (默认 1): ").strip() or "1"
    except EOFError:
        return 0
    if choice in ("0", "q", "quit"):
        return 0
    if choice == "2":
        return _run_pipeline_with_media(
            video,
            chat_html,
            "--manual-translation",
            "--yes",
        )
    if choice == "3":
        return _run_pipeline_with_media(video, chat_html, "--mode", "full", "--yes")
    # default preview
    return _run_pipeline_with_media(
        video,
        chat_html,
        "--mode",
        "preview",
        "--render-original",
        "--preview-clip",
        "10",
        "--yes",
    )


def _run_pipeline_with_media(video: Path, chat_html: Path, *extra: str) -> int:
    """Re-enter this script with local media (same interpreter)."""
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        str(video),
        str(chat_html),
        *extra,
    ]
    print("\n$ " + " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd), flush=True)
    try:
        p = run_tracked(cmd, stdout=None, stderr=None, text=False)
        return int(p.returncode)
    except Exception as e:
        print(f"[FAIL] 无法继续 pipeline: {e}", flush=True)
        return 1


def _run_download_flow(args, *, runner: PipelineRunner | None = None) -> int:
    """CLI entry for --download（实现见 download_flow.py，原样搬运）。

    emit/_post_download_next_steps 以参数注入：调用时解析本模块全局，
    保持测试按模块属性 monkeypatch 的语义。"""
    return download_flow.run_download_flow(
        args,
        runner=runner,
        emit=emit_task_event,
        next_steps=_post_download_next_steps,
    )


def _export_translation_json(
    *,
    burn: Path,
    video: Path,
    chat_html: Path,
    trans_json: Path,
    force: bool = False,
    offset: float | None = None,
) -> None:
    """Export via burn. Auto-reuse when JSON already has translations (unless force).

    Forward ``offset`` so export_offset metadata matches the pipeline's intended
    timeline diagnosis (identity still uses stream timestamps either way).
    """
    existing_n = translation_json_nonempty_count(trans_json)
    if existing_n > 0 and not force:
        log(
            f"[1/3] 检测到已有 {existing_n} 条非空 translation，跳过导出以免覆盖: {trans_json}\n"
            f"      （继续翻译/渲染；若要强制重导加 --force-export；只渲染用 --reuse-translation）"
        )
        return
    cmd = [
        sys.executable,
        str(burn),
        "--out-dir",
        str(trans_json.parent),
        str(video),
        str(chat_html),
        "--export-translation",
        str(trans_json),
    ]
    if force:
        cmd.append("--force-export")
    if offset is not None:
        cmd.extend(["--offset", str(offset)])
    run(
        cmd,
        error_hint=(
            "导出翻译 JSON 失败。若提示已有译文被拒绝覆盖：改用 --reuse-translation，"
            "或确认后加 --force-export。并检查 HTML 是否为 TwitchDownloader 标准格式"
        ),
    )


def _fallback_manual_after_export(
    *,
    video: Path,
    chat_html: Path,
    trans_json: Path,
    review_tsv: Path,
    review_xlsx: Path,
    workdir: Path | None,
    final_output: Path,
    reason: str,
) -> None:
    """API unavailable: export review tables and stop for hand translation (same as --manual-translation tail)."""
    log(f"\n[翻译 API] {reason}")
    filled = translation_json_nonempty_count(trans_json)
    total = 0
    try:
        # utf-8-sig：手工编辑（记事本等）常在 JSON 头部留下 BOM；utf-8-sig
        # 对无 BOM 文件行为不变，有 BOM 时自动剥离。与 translation_io /
        # translate_chat_openai.load_json 的读取口径一致，避免 total 被算成 0。
        data = json.loads(trans_json.read_text(encoding="utf-8-sig")) if trans_json.is_file() else {}
        total = len((data.get("messages") if isinstance(data, dict) else None) or [])
    except Exception:
        total = 0
    if filled > 0:
        log(
            f"[手翻] 当前 JSON 已有 {filled}/{total or '?'} 条非空译文（可能为中途失败残留）；"
            f"导出复核表时会保留这些行，请只补空行或改错行"
        )
    else:
        log("[手翻] 导出人工复核表（不调用 LLM；translation 列为空，请自行填写）…")
    try:
        # 注意：这里保持两参调用。测试用严格签名 stub 替换导出函数，
        # 预计算参数只在 _main 的导出路径传递。
        export_review_tsv(trans_json, review_tsv)
        export_review_xlsx(trans_json, review_xlsx)
    except Exception as e:
        raise PipelineError(f"错误: 导出人工复核表失败: {e}") from e
    mark_manual_translation_required()
    print("\n[OK] 已改为人工翻译流程。请编辑 XLSX 最后一列 translation：")
    print(f"     {review_xlsx}")
    print(f"     JSON: {trans_json}")
    if filled > 0:
        print(f"     提示: 已有 {filled} 条译文会写进表内，勿整列清空。")
    print("     完成后回写并渲染：")
    _hint = (
        f"{current_cli_invocation()} {quote_cli_arg(video)} {quote_cli_arg(chat_html)} "
        f"--reuse-translation --review-done --translation-json {quote_cli_arg(trans_json)} "
        f"--review-xlsx {quote_cli_arg(review_xlsx)}"
    )
    if workdir:
        _hint += f" --workdir {quote_cli_arg(workdir)}"
    _hint += f" --output {quote_cli_arg(final_output)}"
    print(f"     {_hint}")


def ensure_translate_api_or_fallback(
    *,
    video: Path,
    chat_html: Path,
    trans_json: Path,
    review_tsv: Path,
    review_xlsx: Path,
    workdir: Path | None,
    final_output: Path,
    yes: bool = False,
) -> str:
    """Before calling the translator: probe API; on failure ask continue (manual) or retry.

    Returns:
      "api" — proceed with LLM translate
      "manual" — user chose hand translation (tables already exported by caller path)
    """
    max_rounds = 8
    for _round in range(max_rounds):
        ok, msg = probe_translate_api()
        if ok:
            log(f"[翻译 API] {msg}")
            return "api"

        print(f"\n[!] 翻译 API 不可用: {msg}", flush=True)
        print("  可检查 .env 中 OPENAI_COMPAT_BASE_URL / MODEL / API_KEY，以及网络。", flush=True)

        if yes or not _stdin_is_interactive():
            # Non-interactive: fall through to manual tables so batch jobs can continue.
            print("  （非交互/--yes：改为导出人工翻译表后停止）", flush=True)
            _fallback_manual_after_export(
                video=video,
                chat_html=chat_html,
                trans_json=trans_json,
                review_tsv=review_tsv,
                review_xlsx=review_xlsx,
                workdir=workdir,
                final_output=final_output,
                reason=msg,
            )
            return "manual"

        print("  [C] 继续 → 导出未翻译表格，自行填写后再 --review-done 渲染", flush=True)
        print("  [R] 重试 → 再探测一次 API", flush=True)
        print("  [Q] 退出", flush=True)
        try:
            raw = input("请选择 [C 继续 / R 重试 / Q 退出] (默认 C): ").strip().lower()
        except EOFError:
            raw = "c"
        if not raw:
            raw = "c"
        if raw in ("r", "retry", "重试"):
            load_dotenv_if_present()
            print("  重新探测…", flush=True)
            continue
        if raw in ("q", "quit", "exit", "n", "no"):
            raise PipelineError("已取消：翻译 API 不可用。")
        # continue / c / enter / anything else → manual
        _fallback_manual_after_export(
            video=video,
            chat_html=chat_html,
            trans_json=trans_json,
            review_tsv=review_tsv,
            review_xlsx=review_xlsx,
            workdir=workdir,
            final_output=final_output,
            reason=msg,
        )
        return "manual"

    raise PipelineError("错误: 翻译 API 多次重试仍不可用。")


# Windows CreateProcess caps the *entire* command line at 32,767 chars; a
# profile with a large glossary exceeds it before Python even starts. Above
# this threshold the context travels via --context-file instead of argv
# (8000 chars leaves a 4x margin for interpreter/script/JSON paths and flags).
TRANSLATION_CONTEXT_ARGV_LIMIT = 8000
TRANSLATION_MAX_BATCH_CHARS_CEILING = 200_000


def _prepare_translation_context(context: str, workdir: Path | None) -> list[str]:
    """Return the translator flags carrying *context* to the subprocess.

    Context is part of every batch prompt, so a file-delivered context also
    raises --max-batch-chars by its length — otherwise the translator's own
    per-batch guard rejects the context right after we delivered it.
    """
    if len(context) <= TRANSLATION_CONTEXT_ARGV_LIMIT:
        return ["--context", context]
    base = Path(workdir) if workdir else Path.cwd()
    base.mkdir(parents=True, exist_ok=True)
    # 文件名带 pid+随机后缀：并发/重试不会互相覆盖，也不会以固定文件名
    # 把敏感 glossary 长期留在 --workdir/cwd；写盘成功后登记到
    # _translation_context_files，由调用方在翻译子进程结束后清理。
    path = base / f"translation_context_{os.getpid()}_{uuid.uuid4().hex[:8]}.txt"
    path.write_text(context, encoding="utf-8")
    _translation_context_files.append(path)
    max_batch_chars = min(TRANSLATION_MAX_BATCH_CHARS_CEILING, len(context) + 16_000)
    log(f"[info] 翻译 context 过长（{len(context)} 字符），改用文件传递: {path}")
    return ["--context-file", str(path), "--max-batch-chars", str(max_batch_chars)]


# _prepare_translation_context 已写盘、待清理的 context 交接文件
# （内容可能含敏感 glossary）。同一时刻可能存在多个（首次运行失败后的
# 重试会再写一个）；清理函数删除全部并清空，幂等、失败静默。
_translation_context_files: list[Path] = []


def _cleanup_translation_context_file() -> None:
    """Best-effort remove all pending context files; silent on failure."""
    pending = list(_translation_context_files)
    _translation_context_files.clear()
    for path in pending:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def handle_translate_run_failure(
    err: BaseException,
    *,
    video: Path,
    chat_html: Path,
    trans_json: Path,
    review_tsv: Path,
    review_xlsx: Path,
    workdir: Path | None,
    final_output: Path,
    translation_context: str,
    target_language: str,
    batch_size: int,
    workers: int,
    translator: Path,
    yes: bool = False,
) -> str:
    """After a mid-run translator failure: C=manual tables, R=retry once, Q=re-raise.

    Returns:
      "manual" — stopped for hand translation
      "api" — retry succeeded (caller continues pipeline)
    Raises:
      PipelineError / original err on quit or retry failure.
    """
    print(f"\n[!] 翻译调用失败: {err}", flush=True)
    if yes or not _stdin_is_interactive():
        _fallback_manual_after_export(
            video=video,
            chat_html=chat_html,
            trans_json=trans_json,
            review_tsv=review_tsv,
            review_xlsx=review_xlsx,
            workdir=workdir,
            final_output=final_output,
            reason=str(err),
        )
        return "manual"
    print("  [C] 继续 → 用当前 JSON 导出人工表（自行翻译）", flush=True)
    print("  [R] 重试 → 再调用一次翻译 API", flush=True)
    print("  [Q] 退出", flush=True)
    try:
        choice = input("请选择 [C/R/Q] (默认 C): ").strip().lower() or "c"
    except EOFError:
        choice = "c"
    if choice in ("r", "retry", "重试"):
        try:
            context_args = _prepare_translation_context(translation_context, workdir)
            run(
                [
                    sys.executable,
                    str(translator),
                    str(trans_json),
                    *context_args,
                    "--target-language",
                    target_language,
                    "--batch-size",
                    str(batch_size),
                    "--workers",
                    str(workers),
                ],
                error_hint="翻译重试仍失败。可改用人工表：--manual-translation 或修好 API 后再跑",
            )
        finally:
            # 重试的 context 交接文件（可能含敏感 glossary）用完即清。
            _cleanup_translation_context_file()
        return "api"
    if choice in ("q", "quit", "exit"):
        raise err
    _fallback_manual_after_export(
        video=video,
        chat_html=chat_html,
        trans_json=trans_json,
        review_tsv=review_tsv,
        review_xlsx=review_xlsx,
        workdir=workdir,
        final_output=final_output,
        reason=str(err),
    )
    return "manual"


# ---------------------------------------------------------------------------
# 人工复核表 / 翻译质检（lint）/ YAML / 发布 / 规则清洗 —— 门面（刀四/刀五）。
# 实现已整体搬至 review_tables.py（原样搬运）；review_tables 及纯函数、
# LINT_* 正则、load_yaml_file / load_yaml_rules / load_profile / publish_output
# 已在文件头 import + re-export（tests 未按属性 monkeypatch 这些名字，直接
# 绑定即可）。依赖 DRY_RUN/log 的函数保留下方薄包装：显式传入 dry_run=DRY_RUN
# 与本模块的 log，使"测试按模块属性 monkeypatch DRY_RUN"的历史语义完全不变；
# 对外名字与签名一概不变。
# ---------------------------------------------------------------------------


def normalize_translation(json_path: Path, rules_path: Path | None = None):
    # 规则清洗实现单源在 review_tables.normalize_translation；本模块全局
    # DRY_RUN 是唯一事实源，调用时注入（tests/test_pipeline_scenarios.py 按
    # 模块属性改写 render.DRY_RUN 的历史语义不变）。
    review_tables.normalize_translation(json_path, rules_path=rules_path, dry_run=DRY_RUN)


def _prepare_review_export(json_path, max_chars=90):
    # 传本模块的 lint_translation（测试可能 monkeypatch 它），保持 lint-once 的
    # spy 语义：lint 结果缓存进 issue_map，TSV/XLSX 两个导出不再重复执行。
    return review_tables._prepare_review_export(
        json_path, max_chars=max_chars, lint_fn=lint_translation
    )


def export_review_tsv(json_path, review_path, *, data=None, issue_map=None):
    review_tables.export_review_tsv(
        json_path, review_path, data=data, issue_map=issue_map, dry_run=DRY_RUN, log=log
    )


def export_review_xlsx(json_path, review_path, *, data=None, issue_map=None):
    review_tables.export_review_xlsx(
        json_path, review_path, data=data, issue_map=issue_map, dry_run=DRY_RUN, log=log
    )


def import_review_xlsx(json_path, review_path):
    review_tables.import_review_xlsx(json_path, review_path, dry_run=DRY_RUN)


def import_review_tsv(json_path, review_path):
    review_tables.import_review_tsv(json_path, review_path, dry_run=DRY_RUN)


def lint_translation(
    json_path,
    report_path=None,
    max_chars=90,
    max_ratio=2.8,
    data=None,
):
    return review_tables.lint_translation(
        json_path,
        report_path=report_path,
        max_chars=max_chars,
        max_ratio=max_ratio,
        data=data,
        dry_run=DRY_RUN,
    )


def _lint_only_exit(args, lint_target: Path) -> None:
    """只质检 lint_target 指向的翻译 JSON 后立刻退出（三处 lint-only 分支共用）。

    以质检结果作为退出码：存在 FAIL 级问题为 1，否则 0。
    不导出、不翻译、不渲染；report 路径取 --lint-report（可为空）。
    """
    report_path = Path(args.lint_report).resolve() if args.lint_report else None
    issues = lint_translation(lint_target, report_path=report_path, max_chars=args.lint_max_chars)
    raise SystemExit(1 if any(i["severity"] == "FAIL" for i in issues) else 0)


from doctor_check import doctor  # noqa: E402 - 原样搬运至 doctor_check.py
import download_flow as download_flow


def apply_mode_defaults(args) -> list[str]:
    """Apply --mode scenario defaults without overriding explicit CLI values.

    - preview: via apply_preview_first_defaults (preview_clip=10, overlay png when safe)
    - translate: stop after translate/rules/lint/review export (no burn)
    - render: require reuse-translation / render-original / skip / manual / review-only
    - full/auto: no-op defaults
    Returns list of applied field names for logging.
    """
    mode = str(getattr(args, "mode", "auto") or "auto").strip().lower()
    applied: list[str] = []
    if mode in ("auto", "full"):
        return applied

    if mode == "preview":
        preview_applied = apply_preview_first_defaults(
            args,
            PIPELINE_CLI_DEFAULTS,
            explicit_overlay_codec=_cli_flag_present("--overlay-codec"),
        )
        for name in preview_applied:
            if name == "preview_clip":
                applied.append("preview_clip=10")
            elif name == "overlay_codec":
                applied.append("overlay_codec=png")
            else:
                applied.append(name)
        # 预览只为确认 offset/布局；未显式传 --source-media-check 时不要为
        # 10 秒预览完整解码全片（显式传 decode 则尊重用户选择）。
        if (
            str(getattr(args, "source_media_check", "fast") or "").strip().lower() == "decode"
            and not _cli_flag_present("--source-media-check")
        ):
            args.source_media_check = "fast"
            applied.append("source_media_check=fast")
            log("[preview] 预览模式输入检查降级为 fast（不完整解码全片）；如需完整解码请显式传 --source-media-check decode")
        return applied

    if mode == "translate":
        # Translate path: after API translate, stop before burn (like --review without review table).
        args._mode_stop_after_translate = True  # type: ignore[attr-defined]
        applied.append("stop_after_translate")
        return applied

    if mode == "render":
        # 显式 lint 路径：只质检用户指定的翻译 JSON，不进入渲染流程（见 _main 短路）。
        lint_target = getattr(args, "lint_translation", None)
        if lint_target and lint_target != "__PIPELINE__":
            args._mode_render_lint_only = True  # type: ignore[attr-defined]
            applied.append("render_lint_only")
            return applied
        # Allow paths that do not call the live translation API.
        # 注意：review / lint sentinel 不豁免——它们在不带 --reuse-translation 时
        # 仍会走"导出 JSON -> API probe -> LLM 翻译"的完整路径。review_done 安全：
        # 它被强制要求搭配 --reuse-translation。
        needs_live_api = not (
            bool(getattr(args, "render_original", False))
            or bool(getattr(args, "reuse_translation", False))
            or bool(getattr(args, "skip_translate", False))
            or bool(getattr(args, "manual_translation", False))
            or bool(getattr(args, "review_done", False))
        )
        if needs_live_api:
            raise PipelineError(
                "错误: --mode render 不会调用翻译 API。"
                "请使用 --reuse-translation（已有翻译 JSON）或 --render-original，"
                "或改用 --mode full / --mode auto 做完整翻译出片；"
                "翻译后人工复核请用 --mode full --review。"
            )
        applied.append("render_only_guard")
        return applied

    raise PipelineError(f"错误: 未知 --mode {mode!r}，可选 auto|preview|translate|render|full")


def _main():
    # Activate only the trusted source/user-data portable FFmpeg directory.
    prepend_tools_ffmpeg_to_path()
    parser = build_arg_parser()
    args = parser.parse_args()
    install_process_cleanup_handlers()

    # --init / --list-jobs / --init-job / download / td guide early (no video/html required).
    if getattr(args, "init", False):
        raise SystemExit(run_init(create_job=True, run_doctor_fn=doctor, doctor_args=args))
    if getattr(args, "list_jobs", False):
        raise SystemExit(run_list_jobs())
    if getattr(args, "init_job", False):
        created = run_job_wizard()
        if created is None:
            raise SystemExit(1)
        # If wizard saved a job, offer to continue by loading it as --job when
        # no other action was requested: re-enter via env for shell launchers.
        print(f"\n提示: 运行该配置 → {current_cli_invocation()} --job \"{created}\"")
        raise SystemExit(0)
    if getattr(args, "offer_td_cli", False):
        offer_td_cli_guide(
            assume_yes=bool(getattr(args, "yes", False) or getattr(args, "fix_yes", False))
        )
        try:
            from twitch_download import find_twitchdownloader_cli

            installed = find_twitchdownloader_cli() is not None
        except ImportError:
            installed = False
        if not installed:
            print("  [ERROR] TwitchDownloaderCLI 仍不可用；安装或引导未完成。")
        raise SystemExit(0 if installed else 1)
    if getattr(args, "install_td_prompt", False):
        maybe_prompt_offer_td_cli(
            assume_yes=bool(getattr(args, "yes", False) or getattr(args, "fix_yes", False))
        )
        raise SystemExit(0)
    if getattr(args, "download", None):
        raise SystemExit(_run_download_flow(args, runner=active_runner()))

    # --doctor 是纯诊断：必须在 --job 加载与交互询问之前早退，否则
    # `--doctor --job x.yaml` 会在 job 缺少 video/chat_html 时先进入
    # 交互提问（或直接报错退出），诊断根本跑不到。
    if args.doctor:
        raise SystemExit(doctor(args))

    # --job fills only fields still at CLI default (explicit CLI wins).
    job_applied: list[str] = []
    if getattr(args, "job", None):
        try:
            from job_config import resolve_job_arg

            job_path = resolve_job_arg(args.job)
            args.job = str(job_path)
            job = load_job_file(job_path)
            # Apply style fields first. Media paths may be omitted (commented in YAML)
            # for reusable jobs — then CLI args or interactive ask must supply them.
            job_applied = apply_job_to_namespace(args, job, cli_defaults=PIPELINE_CLI_DEFAULTS)
            if job_applied:
                print(f"[job] 已加载: {job_path} -> {', '.join(job_applied)}")
            else:
                print(f"[job] 已加载: {job_path}（无字段应用，可能均被 CLI 覆盖）")

            # Interactive fill for missing video/chat when stdin is a real TTY and not dry-run.
            need_video = not getattr(args, "video", None)
            need_chat = not getattr(args, "chat_html", None)
            if (need_video or need_chat) and not getattr(args, "dry_run", False):
                from pathlib import Path as _P

                from job_wizard import _guess_chat_html, _prompt_path

                interactive = _stdin_is_interactive()
                if interactive:
                    print("[job] 配置未固定视频/HTML（可复用样式）。请指定本次文件（不会写回配置）：")
                    try:
                        if need_video:
                            args.video = _prompt_path("  源视频", must_exist=True)
                        if need_chat:
                            guess = _guess_chat_html(_P(args.video)) if args.video else None
                            args.chat_html = _prompt_path("  聊天 HTML", guess, must_exist=True)
                    except (EOFError, FileNotFoundError) as e:
                        raise SystemExit(
                            f"错误: 无法取得本次视频/HTML（{e}）。\n"
                            "  请传入: --job style.yaml video.mp4 chat.html\n"
                            "  或在 YAML 取消注释 video/chat_html 后重新运行。\n"
                            f"  重试: {current_cli_invocation()} --job {quote_cli_arg(job_path)}"
                        ) from e
                else:
                    raise SystemExit(
                        "错误: job 未包含 video/chat_html，且当前非交互终端。\n"
                        "  请在命令行传入: --job style.yaml video.mp4 chat.html\n"
                        "  或在 YAML 中取消注释 video/chat_html 以固定路径。\n"
                        f"  或在交互终端重新运行: {current_cli_invocation()} "
                        f"--job {quote_cli_arg(job_path)}"
                    )

            merged = {
                "video": getattr(args, "video", None),
                "chat_html": getattr(args, "chat_html", None),
            }
            media_problems = validate_job_media_paths(merged, require_existing=True)
            # Missing keys after interactive attempt
            if not merged.get("video"):
                media_problems = list(media_problems) + ["缺少 video（请传参或取消注释配置）"]
            if not merged.get("chat_html"):
                media_problems = list(media_problems) + ["缺少 chat_html（请传参或取消注释配置）"]
            if media_problems and not getattr(args, "dry_run", False):
                msg = "错误: job 输入路径不可用\n" + "\n".join(f"  - {p}" for p in media_problems)
                raise SystemExit(msg)
            if media_problems and getattr(args, "dry_run", False):
                print("[job] 警告: " + " | ".join(str(p).splitlines()[0] for p in media_problems[:2]))
            if getattr(args, "dry_run", False):
                # dry-run 不落盘：jobs/.last_job 也是真实写出，统一在写侧拦截。
                print("[dry-run] 跳过记录 jobs/.last_job。")
            else:
                save_last_job(job_path)
        except SystemExit:
            # Preserve intentional exits (missing media / non-interactive).
            raise
        except (OSError, ValueError) as e:
            raise SystemExit(f"错误: {e}")

    if args.layout_preset:
        try:
            preset = load_layout_preset(args.layout_preset)
            applied = apply_layout_preset_to_namespace(
                args, preset, cli_defaults=PIPELINE_CLI_DEFAULTS
            )
            if applied:
                print(f"[layout-preset] 已加载: {args.layout_preset} -> {', '.join(applied)}")
        except (OSError, ValueError) as e:
            raise SystemExit(f"错误: {e}")

    if getattr(args, "render_preset", None):
        try:
            rpreset = load_render_preset(args.render_preset)
            rapplied = apply_render_preset_to_namespace(
                args, rpreset, cli_defaults=PIPELINE_CLI_DEFAULTS
            )
            if rapplied:
                print(f"[render-preset] 已加载: {args.render_preset} -> {', '.join(rapplied)}")
        except (OSError, ValueError) as e:
            # 只兜预设文件读取/校验错误（与 layout 分支同款），不吞编程错误。
            raise SystemExit(f"错误: {e}")
        except yaml.YAMLError as e:
            # PyYAML 错误的 MRO 不经过 ValueError：坏 YAML 预设要像坏 job YAML
            # 一样给出"错误:"退出（exit 1），而不是裸 traceback。
            raise SystemExit(f"错误: render preset YAML 无法解析: {e}")

    # Clean early exit before mode guards; doctor 早退已前移至 --job 处理之前。

    companion_err = clean_companion_flags_error(args)
    if companion_err:
        print(companion_err)
        raise SystemExit(2)

    # --clean early exit: resolve out dir from --workdir (or default), no export/translate/burn.
    if getattr(args, "clean", False):
        if args.workdir:
            out_base = Path(args.workdir).resolve()
            clean_root = out_base / "temp"
            if not clean_root.is_dir():
                clean_root = out_base
        elif args.video:
            video_path = Path(args.video).expanduser()
            if not video_path.is_file():
                print(f"--clean: 视频不存在，拒绝回退到当前目录: {video_path}")
                raise SystemExit(1)
            clean_root = video_path.resolve().parent
        else:
            print("--clean: 请指定 --workdir，或提供存在的 video 路径（避免误清 cwd）")
            raise SystemExit(1)
        clean_root = Path(os.path.abspath(str(clean_root)))
        if not clean_root.is_dir():
            print(f"--clean: 目录不存在: {clean_root}")
            raise SystemExit(1)
        if is_dangerous_publish_path(clean_root):
            print(f"--clean: 拒绝在系统目录下清理: {clean_root}")
            raise SystemExit(2)
        count, freed = clean_temp_artifacts(
            clean_root,
            clean_progress=bool(getattr(args, "clean_progress", False)),
            clean_all=bool(getattr(args, "clean_all", False)),
        )
        print(f"\n清理完成: {count} 项, 释放 {freed / (1024 * 1024):.1f} MB")
        raise SystemExit(0)

    # Mode defaults after job/presets so "still at default" checks remain valid for preview overlay.
    try:
        mode_applied = apply_mode_defaults(args)
        if mode_applied and not getattr(args, "quiet", False):
            print(f"[mode={getattr(args, 'mode', 'auto')}] {', '.join(mode_applied)}")
    except PipelineError:
        raise

    try:
        args.font_path, args.font_bold_path = resolve_font_paths(args.font_path, args.font_bold_path)
    except FileNotFoundError as e:
        raise PipelineError(f"错误: {e}")

    global DRY_RUN, VERBOSE, QUIET
    DRY_RUN = args.dry_run
    VERBOSE = args.verbose
    QUIET = args.quiet
    if getattr(args, "_mode_render_lint_only", False):
        # --mode render --lint-translation <路径>：只质检用户指定的翻译 JSON，
        # 不导出、不翻译、不渲染；以质检结果（是否有 FAIL）作为退出码。
        lint_target = Path(args.lint_translation).resolve()
        log(f"[mode=render] 仅质检翻译 JSON，不渲染视频: {lint_target}")
        _lint_only_exit(args, lint_target)
    if args.lint_translation and args.lint_translation != "__PIPELINE__" and not args.video and not args.chat_html:
        _lint_only_exit(args, Path(args.lint_translation).resolve())
    if args.lint_translation == "__PIPELINE__" and args.video and not args.chat_html:
        # sentinel（--lint-translation 不带值）+ video：用户意图是"质检本片翻译"，
        # 但管线还没有 translation_json 可检；直接 parser.error 比让它落到
        # "需要 chat_html" 的通用报错更能说明问题。
        parser.error(
            "--lint-translation 不带值时不能同时提供 video 参数: "
            "请给 --lint-translation 传翻译 JSON 路径，或去掉 video 只跑质检"
        )
    if not args.video or not args.chat_html:
        parser.error(
            "需要提供 video 和 chat_html；"
            "仅 --init / --doctor / --job / 单独 --lint-translation 可省略输入文件"
        )
    if args.render_original and (args.reuse_translation or args.skip_translate or args.manual_translation or args.review or args.review_done or args.lint_translation or args.rules or args.profile):
        raise PipelineError("错误: --render-original 不能与翻译、复核、规则或 profile 参数同时使用。请只保留渲染布局参数和 --output。")
    if args.manual_translation and (args.reuse_translation or args.skip_translate or args.render_original or args.review_done):
        raise PipelineError("错误: --manual-translation 只负责导出人工翻译文件，不能与复用翻译、仅导出、原文渲染或回写复核同时使用。")
    if args.review_done and not args.reuse_translation:
        raise PipelineError(
            "错误: --review-done 必须配合 --reuse-translation 使用，"
            "避免重新导出/翻译冲掉已有 JSON。请先保留翻译文件再回写复核表。"
        )
    base_dir = Path(__file__).resolve().parent
    burn = base_dir / "twitch_chat_burn.py"
    translator = base_dir / "translate_chat_openai.py"

    video = Path(args.video).resolve()
    chat_html = Path(args.chat_html).resolve()
    if not video.is_file():
        raise PipelineError(f"错误: 视频文件不存在: {video}\n  请确认路径正确，或用 TwitchDownloader 下载视频后重试。")
    if not chat_html.is_file():
        raise PipelineError(f"错误: 聊天 HTML 文件不存在: {chat_html}\n  请用 TwitchDownloader 导出聊天 HTML，确保选择 HTML 格式。")
    explicit_output = Path(args.output).resolve() if args.output else None
    if explicit_output == video:
        raise PipelineError(
            "错误: --output 不能与源视频指向同一文件；请选择新的输出文件名，避免覆盖原片。"
        )
    if not burn.is_file():
        raise PipelineError(f"错误: 找不到核心脚本: {burn}\n  请确认从项目根目录运行，或检查 scripts/ 目录完整性。")
    if not translator.is_file() and not args.skip_translate and not args.reuse_translation and not args.manual_translation and not args.render_original:
        raise PipelineError(f"错误: 找不到翻译脚本: {translator}\n  请确认 scripts/ 目录完整性。")

    validate_source_media(video, mode=args.source_media_check, dry_run=args.dry_run)

    workdir = None
    if args.workdir:
        workdir = Path(args.workdir).resolve()
        if is_dangerous_publish_path(workdir):
            raise PipelineError(f"错误: --workdir 不能是系统目录: {workdir}")
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "temp").mkdir(exist_ok=True)
        log(f"[workdir] 使用工作目录: {workdir}")

    def wd(default_path, filename=None):
        if workdir:
            return workdir / (filename or default_path.name)
        return default_path

    # Explicit paths always win. --workdir only relocates implicit defaults.
    # 三个显式路径与 --output 同款系统目录守卫：写进系统目录的翻译/复核表
    # 同样不可接受（fail closed）。
    for _label, _explicit in (
        ("--translation-json", args.translation_json),
        ("--review-tsv", args.review_tsv),
        ("--review-xlsx", args.review_xlsx),
    ):
        if _explicit and is_dangerous_publish_path(Path(str(_explicit)).resolve()):
            raise PipelineError(f"错误: {_label} 不能写到系统目录: {_explicit}")
    trans_json = Path(args.translation_json).resolve() if args.translation_json else wd(video.with_name(video.stem + "_translation.json"))
    review_tsv = Path(args.review_tsv).resolve() if args.review_tsv else wd(video.with_name(video.stem + "_translation_review.tsv"))
    review_xlsx = Path(args.review_xlsx).resolve() if args.review_xlsx else review_tsv.with_suffix(".xlsx")
    output_default = video.with_name(video.stem + "_chat.mp4")
    if explicit_output is not None:
        final_output = explicit_output
    elif workdir:
        final_output = workdir / (video.stem + "_chat.mp4")
    else:
        final_output = output_default
    if is_dangerous_publish_path(final_output) or is_dangerous_publish_path(final_output.parent):
        raise PipelineError(f"错误: --output 不能写到系统目录: {final_output}")
    # final_output 只可能是 explicit_output（上面已拦 ==video）或由 video.stem
    # 派生的 <stem>_chat.mp4 / workdir 派生名，不可能与源视频同名，无需重复守卫。

    runner = active_runner()
    if runner is not None:
        runner.configure(
            mode=getattr(args, "mode", "auto"),
            artifacts=[
                ("video", final_output),
                ("translation_json", trans_json),
                ("review_xlsx", review_xlsx),
                ("review_tsv", review_tsv),
                ("preview_image", getattr(args, "preview_image", None)),
            ],
        )

    if args.render_original:
        log("[1/1] 不使用 LLM，直接渲染原始聊天文本和 HTML 中已有 emote")
        # 命令组装单源走 pipeline_plan.build_burn_command；原路径不传 trans_json
        # → 不含 --import-translation，也不发明 --strict-import。
        cmd = build_burn_command(
            args, video, chat_html, burn,
            out_dir=(workdir / "temp") if workdir else None,
        )
        run(cmd, error_hint="渲染失败，请检查视频文件、FFmpeg 和字体路径是否正确")
        if DRY_RUN:
            return
        if args.preview_frame is not None:
            log("\n[OK] 原始聊天预览图已生成。")
            return
        rendered_output = (workdir / "temp" / (video.stem + "_chat.mp4")) if workdir else output_default
        if final_output != rendered_output:
            publish_output(
                rendered_output,
                final_output,
                backup_prev=not bool(getattr(args, "no_backup_prev", False)),
            )
        log(f"\n[OK] 原始聊天 overlay 已输出到: {final_output}")
        if (
            args.preview_frame is None
            and args.preview_clip is None
            and str(getattr(args, "mode", "auto") or "auto") not in ("preview",)
        ):
            log("提示: 下次可先 --preview-clip 10 或 --mode preview 确认 offset/布局，再出长片")
        return

    if args.manual_translation:
        log(f"[1/2] 导出待人工翻译 JSON: {trans_json}")
        _export_translation_json(
            burn=burn,
            video=video,
            chat_html=chat_html,
            trans_json=trans_json,
            force=bool(getattr(args, "force_export", False)),
            offset=getattr(args, "offset", None),
        )
        if DRY_RUN:
            log("\n[dry-run] 跳过复核表导出和后续步骤。")
            return
        log("\n[2/2] 导出人工复核表（无需 LLM）")
        data, issue_map = _prepare_review_export(trans_json)
        export_review_tsv(trans_json, review_tsv, data=data, issue_map=issue_map)
        export_review_xlsx(trans_json, review_xlsx, data=data, issue_map=issue_map)
        mark_manual_translation_required()
        print("\n[OK] 请优先编辑 XLSX 最后一列 translation：")
        print(f"     {review_xlsx}")
        print("     完成后使用以下命令回写并渲染：")
        _hint = (
            f"{current_cli_invocation()} {quote_cli_arg(video)} {quote_cli_arg(chat_html)} "
            f"--reuse-translation --review-done --translation-json {quote_cli_arg(trans_json)} "
            f"--review-xlsx {quote_cli_arg(review_xlsx)}"
        )
        if workdir:
            _hint += f" --workdir {quote_cli_arg(workdir)}"
        _hint += f" --output {quote_cli_arg(final_output)}"
        print(f"     {_hint}")
        return

    profile_context = ""
    if args.profile:
        profile_path = resolve_public_resource(args.profile, subdir="profiles")
        profile_context, profile_data = load_profile(profile_path)
        print(f"[profile] 已加载: {profile_path} ({profile_data.get('label') or profile_data.get('name') or 'unnamed'})")
    translation_context = "\n\n".join(part for part in [args.context, profile_context] if part)

    if args.reuse_translation:
        if not trans_json.is_file():
            raise PipelineError(f"错误: --reuse-translation 指定但翻译 JSON 不存在: {trans_json}\n  请先运行不带 --reuse-translation 的命令生成翻译，或用 --manual-translation 导出后人工填写。")
        log(f"[1/3] 复用翻译 JSON: {trans_json}")
    else:
        log(f"[1/3] 导出待翻译 JSON: {trans_json}")
        _export_translation_json(
            burn=burn,
            video=video,
            chat_html=chat_html,
            trans_json=trans_json,
            force=bool(getattr(args, "force_export", False)),
            offset=getattr(args, "offset", None),
        )
        if DRY_RUN:
            log("\n[dry-run] 跳过翻译和渲染步骤。")
            return

        if args.skip_translate:
            print(f"\n[OK] 已导出待翻译 JSON，未继续翻译/渲染: {trans_json}")
            return

        # Allow choosing translate mode even with bad/missing API: probe first,
        # then continue with hand-translation tables or retry.
        api_mode = ensure_translate_api_or_fallback(
            video=video,
            chat_html=chat_html,
            trans_json=trans_json,
            review_tsv=review_tsv,
            review_xlsx=review_xlsx,
            workdir=workdir,
            final_output=final_output,
            yes=bool(getattr(args, "yes", False)),
        )
        if api_mode == "manual":
            return

        log(f"\n[2/3] 调用 OpenAI-compatible 翻译器: {trans_json}")
        try:
            context_args = _prepare_translation_context(translation_context, workdir)
            run([
                sys.executable, str(translator), str(trans_json),
                *context_args,
                "--target-language", args.target_language,
                "--batch-size", str(args.batch_size),
                "--workers", str(args.workers),
            ], error_hint="翻译失败，请检查 OPENAI_COMPAT_* 环境变量是否正确设置，网络是否可达")
        except PipelineError as e:
            # Mid-run API failure: same C/R/Q contract as pre-flight probe.
            mid = handle_translate_run_failure(
                e,
                video=video,
                chat_html=chat_html,
                trans_json=trans_json,
                review_tsv=review_tsv,
                review_xlsx=review_xlsx,
                workdir=workdir,
                final_output=final_output,
                translation_context=translation_context,
                target_language=args.target_language,
                batch_size=args.batch_size,
                workers=args.workers,
                translator=translator,
                yes=bool(getattr(args, "yes", False)),
            )
            if mid == "manual":
                return
        finally:
            # context 交接文件（可能含敏感 glossary）只在翻译子进程运行期间
            # 需要存在；成功、失败、重试返回后一律清理（重试的文件由
            # handle_translate_run_failure 内部的 finally 清理，此处幂等）。
            _cleanup_translation_context_file()
        if DRY_RUN:
            log("\n[dry-run] 跳过渲染步骤。")
            return

    rules_path = resolve_public_resource(args.rules, subdir="configs") if args.rules else None
    normalize_translation(trans_json, rules_path=rules_path)

    if args.review_done:
        if review_xlsx.is_file():
            import_review_xlsx(trans_json, review_xlsx)
        else:
            import_review_tsv(trans_json, review_tsv)

    if args.lint_translation:
        # sentinel 表示"检查本次流水线产出的 trans_json"；显式路径则尊重用户指定，
        # 不得忽略（否则用户拿到的质检报告对不上自己传的文件）。
        lint_target = trans_json if args.lint_translation == "__PIPELINE__" else Path(args.lint_translation).resolve()
        report_path = Path(args.lint_report).resolve() if args.lint_report else None
        issues = lint_translation(lint_target, report_path=report_path, max_chars=args.lint_max_chars)
        if any(issue["severity"] == "FAIL" for issue in issues):
            raise PipelineError("错误: 翻译质检存在 FAIL，请修复后再渲染；如需只查看报告，可单独运行 --lint-translation。")

    if args.review:
        if DRY_RUN:
            # dry-run 不落盘：复核表导出统一在写侧拦截（导出函数内部同样有守卫）。
            log("\n[dry-run] 跳过复核表导出（TSV/XLSX 不会写出）。")
        else:
            data, issue_map = _prepare_review_export(trans_json)
            export_review_tsv(trans_json, review_tsv, data=data, issue_map=issue_map)
            export_review_xlsx(trans_json, review_xlsx, data=data, issue_map=issue_map)
        mark_manual_translation_required()
        print("  请优先编辑 XLSX 的最后一列 translation；TSV 仅作为兼容备份。")
        print("\n[OK] 已停在人工复核环节，尚未渲染视频。")
        print("     修改 XLSX 后运行同一命令并把 --review 换成 --review-done。")
        return

    if getattr(args, "_mode_stop_after_translate", False):
        if args.reuse_translation:
            log(f"\n[OK] --mode translate + --reuse-translation：已完成规则/质检，未渲染。JSON: {trans_json}")
        else:
            log(f"\n[OK] --mode translate：翻译已完成，未渲染。JSON: {trans_json}")
        log(
            f"     下一步渲染: {current_cli_invocation()} {quote_cli_arg(video)} {quote_cli_arg(chat_html)} "
            f"--mode render --reuse-translation --translation-json {quote_cli_arg(trans_json)} "
            f"--output {quote_cli_arg(final_output)}"
        )
        return

    # Default UX: after a *fresh* API translation, export Excel and wait for Enter
    # so the user can skim before a long render. Skip pause when:
    #   --yes / -y, dry-run, non-TTY, --reuse-translation (already translated),
    #   or --review-done (user already came back from Excel).
    did_fresh_translate = (
        not args.render_original
        and not args.reuse_translation
        and not args.review_done
        and not args.skip_translate
    )
    if did_fresh_translate:
        action = pause_after_translation_for_review(
            trans_json=trans_json,
            review_xlsx=review_xlsx,
            review_tsv=review_tsv,
            auto_continue=bool(getattr(args, "yes", False)),
            video=video,
            chat_html=chat_html,
            args=args,
            workdir=workdir,
            burn=burn,
        )
        if action == "stop":
            # 与 --review 相同契约：停在人工环节必须发布 manual_required 终态，
            # 否则结果清单会误报 succeeded（看起来像出片成功）。
            mark_manual_translation_required()
            return
        # If user edited XLSX during the pause, pull changes back into JSON.
        if review_xlsx.is_file() and not getattr(args, "yes", False) and _stdin_is_interactive():
            try:
                import_review_xlsx(trans_json, review_xlsx)
                log(f"[复核] 已从 Excel 回写翻译: {review_xlsx}")
            except Exception as e:
                log(f"[WARN] 回写 Excel 失败（使用当前 JSON 继续）: {e}")

    log("\n[3/3] 渲染并合成翻译后的 chat overlay 视频")
    cmd = build_burn_command(
        args, video, chat_html, burn,
        trans_json=trans_json,
        out_dir=(workdir / "temp") if workdir else None,
    )
    run(cmd, error_hint="渲染失败，请检查视频文件、FFmpeg 和字体路径是否正确")

    if args.preview_frame is not None:
        log("\n[OK] 预览图已生成。")
        return

    rendered_output = (workdir / "temp" / (video.stem + "_chat.mp4")) if workdir else output_default
    if final_output != rendered_output:
        publish_output(
            rendered_output,
            final_output,
            backup_prev=not bool(getattr(args, "no_backup_prev", False)),
        )
        log(f"\n[OK] 已输出到: {final_output}")
    else:
        log(f"\n[OK] 输出: {final_output}")
    if (
        args.preview_frame is None
        and args.preview_clip is None
        and str(getattr(args, "mode", "auto") or "auto") not in ("preview",)
    ):
        log("提示: 下次可先 --preview-clip 10 或 --mode preview 确认 offset/布局，再出长片")


def _terminal_exit_code(value: object) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 1


def main():
    """Run the pipeline and publish an optional narrow terminal result manifest."""
    runner = PipelineRunner()
    with activate_runner(runner):
        try:
            result = _main()
        except SystemExit as exc:
            code = _terminal_exit_code(exc.code)
            runner.publish_terminal_result("succeeded" if code == 0 else "failed", code)
            raise
        except BaseException:
            runner.publish_terminal_result("failed", 1)
            raise
        code = _terminal_exit_code(result)
        runner.publish_terminal_result("succeeded" if code == 0 else "failed", code)
        return result


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass
    try:
        main()
    except PipelineError as e:
        print(f"\n{e}", file=sys.stderr)
        sys.exit(1)
