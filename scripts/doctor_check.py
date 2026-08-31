#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境诊断（--doctor）——从 render_cn_chat.py 原样搬出（搬运而非重写）。

只依赖 env_bootstrap / common_utils / ux_setup 的可直连函数，不读渲染编排层
任何模块级可变全局，因此不需要注入。
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import platform
import subprocess
import sys

from common_utils import (
    current_cli_invocation,
    current_cli_script,
    detect_cjk_font,
    require_executable,
    safe_which,
)
from env_bootstrap import (
    maybe_prompt_offer_fixes,
    offer_fixes,
    prepend_tools_ffmpeg_to_path,
    print_readiness_report,
)
from ux_setup import print_setup_next_steps


def doctor(args):
    """检查本机运行环境。"""
    print("# 环境诊断 / Doctor")
    # Prefer the trusted portable FFmpeg directory before PATH probes.
    tools_bin = prepend_tools_ffmpeg_to_path()
    if tools_bin:
        print(f"[info] 使用可信目录中的便携 FFmpeg: {tools_bin}")

    if getattr(args, "offer_fix", False):
        offer_fixes(assume_yes=bool(getattr(args, "yes", False) or getattr(args, "fix_yes", False)))

    ok = True
    fails: list[str] = []
    warns: list[str] = []
    api_ok = False
    offset_diag = None
    video = None
    html = None
    v_dur = 0.0

    def check(name, passed, detail="", fix="", required=True):
        nonlocal ok
        status = "OK" if passed else ("FAIL" if required else "WARN")
        print(f"[{status}] {name}{(': ' + detail) if detail else ''}")
        if not passed:
            if required:
                ok = False
                fails.append(name)
            else:
                warns.append(name)
            if fix:
                print(f"      修复建议: {fix}")

    check("Python", sys.version_info >= (3, 10), sys.version.split()[0], "安装 Python 3.10 或更高版本: https://www.python.org/downloads/")
    for exe in ["ffmpeg", "ffprobe"]:
        path = safe_which(exe)
        fix = "安装 FFmpeg: https://ffmpeg.org/download.html"
        if platform.system() == "Windows":
            fix += (
                "\n      Windows: winget install --id Gyan.FFmpeg -e"
                "\n      或: choco install ffmpeg -y"
                f"\n      或: {current_cli_invocation()} --doctor --offer-fix"
                "\n      便携: 运行 --doctor --offer-fix 可安装到可信工具目录"
            )
        elif platform.system() == "Darwin":
            fix += "\n      macOS: brew install ffmpeg"
        else:
            fix += (
                "\n      Linux: sudo apt install ffmpeg fonts-noto-cjk"
                "\n      或: sudo dnf install ffmpeg"
            )
        check(exe, bool(path), path or "未找到", fix)

    packages = {
        "Pillow": "PIL",
        "beautifulsoup4": "bs4",
        "openai": "openai",
        "openpyxl": "openpyxl",
        "PyYAML": "yaml",
    }
    # WARN-only packages: 与下方「翻译 API 三件套」口径一致——不装也能渲染，
    # 只有对应功能需要（openai: 仅复用翻译；openpyxl: 仅导出 XLSX 复核表）。
    optional_pkgs = {"openai", "openpyxl"}
    optional_pkg_hints = {
        "openai": "仅复用翻译需要；不使用翻译功能可忽略",
        "openpyxl": "仅导出 XLSX 复核表需要",
    }
    missing_required_pkgs: list[str] = []
    for display, module in packages.items():
        try:
            present = importlib.util.find_spec(module) is not None
        except (ValueError, ModuleNotFoundError):
            # Stub modules in tests may set __spec__ = None.
            present = module in sys.modules
        if not present and module in ("PIL", "bs4", "yaml"):
            missing_required_pkgs.append(display)
        fix = f"pip install {display}\n      或: pip install -r requirements.txt"
        if display in optional_pkg_hints:
            fix += f"\n      {optional_pkg_hints[display]}"
        check(
            display,
            present,
            fix=fix,
            required=display not in optional_pkgs,
        )

    # 系统字体探测代价高：detect_cjk_font() 返回 (regular, bold) 元组，只调用一次，
    # 常规/粗体两个 auto 分支按需取用。
    auto_font: tuple[str | None, str | None] | None = None

    def _auto_cjk_font():
        nonlocal auto_font
        if auto_font is None:
            auto_font = detect_cjk_font()
        return auto_font

    font_path = getattr(args, "font_path", None)
    if font_path and font_path != "auto":
        check("常规字体", Path(font_path).is_file(), font_path, "用 --font-path 指定一个可用字体")
    elif getattr(args, "font_path", "auto") == "auto":
        reg, _ = _auto_cjk_font()
        check("常规字体 (auto)", bool(reg), reg or "未检测到 CJK 字体", "用 --font-path 手动指定字体路径")
    font_bold_path = getattr(args, "font_bold_path", None)
    if font_bold_path and font_bold_path != "auto":
        check("粗体字体", Path(font_bold_path).is_file(), font_bold_path, "用 --font-bold-path 手动指定字体路径")
    elif getattr(args, "font_bold_path", "auto") == "auto":
        _, bold = _auto_cjk_font()
        check("粗体字体 (auto)", bool(bold), bold or "未检测到 CJK 字体", "用 --font-bold-path 手动指定字体路径")

    base_url = os.getenv("OPENAI_COMPAT_BASE_URL")
    api_key = os.getenv("OPENAI_COMPAT_API_KEY")
    model = os.getenv("OPENAI_COMPAT_MODEL")
    check(
        "翻译 Base URL",
        bool(base_url),
        base_url or "未设置",
        f"设置 OPENAI_COMPAT_BASE_URL；仅复用翻译可忽略\n      可 {current_cli_invocation()} --init 生成 .env",
        required=False,
    )
    check("翻译 Model", bool(model), model or "未设置", "设置 OPENAI_COMPAT_MODEL；仅复用翻译可忽略", required=False)
    check("翻译 API Key", bool(api_key), "已设置" if api_key else "未设置", "设置 OPENAI_COMPAT_API_KEY；仅复用翻译可忽略", required=False)
    api_ok = bool(base_url and model and api_key)

    if getattr(args, "video", None):
        video = Path(args.video).resolve()
        check("输入视频", video.is_file(), str(video), "检查视频路径")
        if video.is_file() and safe_which("ffprobe"):
            try:
                probe = subprocess.run(
                    [
                        require_executable("ffprobe"),
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "csv=p=0",
                        str(video),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=45,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                check("视频可读取", False, str(exc)[:120], "确认 ffprobe 可用且视频文件未损坏")
            else:
                check(
                    "视频可读取",
                    probe.returncode == 0,
                    ((probe.stdout or "").strip() + (probe.stderr or "").strip())[:120],
                    "确认视频文件未损坏",
                )
                try:
                    v_dur = (
                        float((probe.stdout or "").strip().splitlines()[0])
                        if probe.returncode == 0
                        else 0.0
                    )
                except (ValueError, IndexError):
                    v_dur = 0.0
    if getattr(args, "chat_html", None):
        html = Path(args.chat_html).resolve()
        check("聊天 HTML", html.is_file(), str(html), "检查 HTML 路径")
        # 时间轴对齐诊断：用与主路径相同的 parse_chat_html，而不是只认 Web data-timestamp。
        if html.is_file() and args.video and video is not None and video.is_file() and safe_which("ffprobe"):
            try:
                import tempfile as _tf

                from chat_parser import parse_chat_html as _parse_chat_html
                from chat_window import compute_time_offset, format_offset_diagnosis

                with _tf.TemporaryDirectory(prefix="doctor_chat_") as tmp:
                    chat = _parse_chat_html(str(html), tmp)
                    msgs = chat.get("messages") or []
                    if msgs and v_dur > 0:
                        first_ts = float(msgs[0].get("timestamp", 0) or 0)
                        diag = compute_time_offset(msgs, video_duration=v_dur, manual_offset=getattr(args, "offset", None))
                        offset_diag = diag
                        if diag.get("mode") == "auto":
                            check(
                                "时间轴对齐",
                                True,
                                f"首条 {first_ts:.0f}s / 视频 {v_dur:.0f}s；将自动 offset={diag['offset']:.0f}s",
                                required=False,
                            )
                        elif first_ts > v_dur:
                            check(
                                "时间轴对齐",
                                False,
                                f"首条消息 {first_ts:.0f}s > 视频时长 {v_dur:.0f}s；自动检测未触发",
                                "用 --offset <秒> 手动指定并用 --preview-frame 验证",
                                required=False,
                            )
                        elif diag.get("warnings"):
                            check(
                                "时间轴对齐",
                                False,
                                diag["warnings"][0][:160],
                                "用 --preview-clip / --offset 确认",
                                required=False,
                            )
                        else:
                            check(
                                "时间轴对齐",
                                True,
                                f"首条消息 {first_ts:.0f}s，视频时长 {v_dur:.0f}s，共 {len(msgs)} 条",
                                required=False,
                            )
                        print()
                        print(format_offset_diagnosis(diag))
                    elif not msgs:
                        check("时间轴对齐", False, "解析到 0 条消息，无法诊断偏移", "确认 HTML 为 TwitchDownloader 导出", required=False)
            except Exception as e:
                # doctor 不应因诊断失败而整体失败
                check("时间轴对齐", True, f"跳过详细诊断 ({type(e).__name__})", required=False)

    print("\n诊断结果:", "通过" if ok else "存在问题")

    # ---- 就绪清单（P1 分级）----
    min_ok, full_ok = print_readiness_report()

    # Default UX: if not ready for render, ask to help install FFmpeg (TTY only).
    # install.bat / doctor.bat / run.bat doctor all hit this path.
    offered = bool(getattr(args, "offer_fix", False))
    if not min_ok:
        ran = maybe_prompt_offer_fixes(
            already_offered=offered,
            assume_yes=bool(getattr(args, "yes", False) or getattr(args, "fix_yes", False)),
        )
        if ran or offered:
            print("\n--- 修复后复检 ---")
            if safe_which("ffmpeg"):
                fails = [f for f in fails if f != "ffmpeg"]
            if safe_which("ffprobe"):
                fails = [f for f in fails if f != "ffprobe"]
            ok = len(fails) == 0
            min_ok, full_ok = print_readiness_report()

    # ---- 推荐下一步（可复制命令）----
    script = current_cli_script()
    if fails:
        print("\n先处理 FAIL 项（上方「修复建议」可复制）：")
        for name in fails:
            print(f"  - {name}")
    if missing_required_pkgs:
        print("  pip install -r requirements.txt")
    if offset_diag and offset_diag.get("mode") == "auto":
        print(f"\n# doctor 检测到自动 offset≈{float(offset_diag.get('offset') or 0):.0f}s，请用预览核对")
    elif offset_diag and offset_diag.get("warnings"):
        print("\n# 时间轴有警告，请用 --preview-clip / --offset 确认")
    print_setup_next_steps(
        has_api=api_ok,
        has_ffmpeg=bool(safe_which("ffmpeg") and safe_which("ffprobe")),
        video=video if video and video.is_file() else None,
        chat=html if html and html.is_file() else None,
        script=script,
    )
    # Exit non-zero if classic doctor fails OR minimum render readiness fails.
    return 0 if (ok and min_ok) else 1
