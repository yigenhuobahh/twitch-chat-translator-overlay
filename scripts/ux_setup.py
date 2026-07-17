#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""First-run setup helpers: --init scaffold for .env and example job.yaml."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys

from common_utils import (
    current_cli_script,
    distribution_share_dirs,
    format_cli_invocation,
    is_console_entry_script,
    quote_cli_arg,
    safe_which,
    source_checkout_root,
    trusted_tools_root,
)

_FALLBACK_EXAMPLE_JOB = """\
# 任务配置示例 — 每个参数旁有注释说明
# 运行: twitch-chat-overlay --job jobs/example_job.yaml
# CLI 显式参数优先于本文件。

# 视频/HTML 默认注释：每次运行询问，不写死（取消注释才固定）
# video: path/to/video.mp4
# chat_html: path/to/chat.html
# output: out_chat.mp4
# workdir: work/my_vod

# 场景: auto|preview|translate|render|full
mode: preview
render_original: true
preview_clip: 10

# layout_preset: compact
# render_preset: fast
# offset: 7264
# reuse_translation: false
# translation_json: path/to/translations.json
"""


def find_example_job() -> Path | None:
    """Locate the full example job in a checkout or installed wheel share."""
    source_root = source_checkout_root(__file__)
    if source_root is not None:
        source_example = source_root / "jobs" / "example_job.yaml"
        if source_example.is_file():
            return source_example
    for share_root in distribution_share_dirs():
        installed_example = share_root / "jobs" / "example_job.yaml"
        if installed_example.is_file():
            return installed_example
    return None


def example_job_yaml_text() -> str:
    """Load the complete example job only when scaffolding (not at import time)."""
    bundled = find_example_job()
    if bundled is not None:
        try:
            return bundled.read_text(encoding="utf-8")
        except OSError:
            pass
    return _FALLBACK_EXAMPLE_JOB


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def find_env_example() -> Path | None:
    """Locate .env.example in cwd, a source checkout, or installed wheel data."""
    cwd_ex = Path.cwd() / ".env.example"
    if cwd_ex.is_file():
        return cwd_ex
    repo_ex = _repo_root() / ".env.example"
    if repo_ex.is_file():
        return repo_ex
    for share_root in distribution_share_dirs():
        installed_ex = share_root / ".env.example"
        if installed_ex.is_file():
            return installed_ex
    return None


def ensure_dotenv(cwd: Path | None = None) -> tuple[Path | None, str]:
    """Create .env from .env.example if missing.

    Prefers writing into *cwd* (default: process cwd). Returns (path, status)
    where status is 'created' | 'exists' | 'missing_example' | 'error'.
    """
    base = Path(cwd) if cwd is not None else Path.cwd()
    env_path = base / ".env"
    if env_path.is_file():
        return env_path, "exists"

    example = find_env_example()
    if example is None:
        return None, "missing_example"

    try:
        shutil.copyfile(example, env_path)
    except OSError:
        return None, "error"
    return env_path, "created"


def ensure_example_job(cwd: Path | None = None) -> tuple[Path | None, str]:
    """Create jobs/example_job.yaml under cwd if missing."""
    base = Path(cwd) if cwd is not None else Path.cwd()
    jobs_dir = base / "jobs"
    job_path = jobs_dir / "example_job.yaml"
    if job_path.is_file():
        return job_path, "exists"
    try:
        jobs_dir.mkdir(parents=True, exist_ok=True)
        job_path.write_text(example_job_yaml_text(), encoding="utf-8")
    except OSError:
        return None, "error"
    return job_path, "created"


def print_setup_next_steps(
    *,
    has_api: bool,
    has_ffmpeg: bool = True,
    video=None,
    chat=None,
    script: str = "scripts/render_cn_chat.py",
) -> None:
    """Print copy-pasteable next commands after doctor / init."""
    command = format_cli_invocation(script)
    source_launchers = not is_console_entry_script(script)

    print("\n======== 推荐下一步 ========")
    if not has_ffmpeg:
        print("先安装 FFmpeg（并保证 ffprobe 在 PATH）:")
        print("  Windows: winget install --id Gyan.FFmpeg -e")
        print("  macOS:   brew install ffmpeg")
        print("  Linux:   sudo apt install ffmpeg fonts-noto-cjk")
        print(f"  或: {command} --doctor --offer-fix")
        print(f"  便携: 解压到 {trusted_tools_root(__file__) / 'tools' / 'ffmpeg'} 使 bin/ffmpeg 存在")
    print("一键运行:")
    if source_launchers:
        print("  Windows: run.bat          # 菜单：新建配置 / 复用配置")
        print("  Linux/macOS: bash run.sh  # 菜单：新建配置 / 复用配置")
    print(f"  {command} --init-job")
    print(f"  {command} --list-jobs")

    v = quote_cli_arg(video) if video else "video.mp4"
    h = quote_cli_arg(chat) if chat else "chat.html"
    if not has_api:
        print("\n翻译 API 未齐全（不阻塞原文渲染）:")
        print(f"  {command} --init")
        print("  # 编辑 .env 填入 OPENAI_COMPAT_BASE_URL / MODEL / API_KEY")
        print("  不翻译可:")
        print(f"  {command} {v} {h} --mode preview --render-original --output preview.mp4")
        if source_launchers:
            print("  或: run.bat example_job / bash run.sh example_job")
    else:
        print("\n翻译 API 已就绪，可全流程出片:")
        print(f"  {command} {v} {h} --output out.mp4")
        if source_launchers:
            print("  或引导配置后: run.bat <配置名> / bash run.sh <配置名>")

    if video and chat:
        print("\n建议先预览确认 offset / 布局:")
        print(f"  {command} {v} {h} --mode preview --render-original --output preview.mp4")
    print(f"\n场景化: {command} --job jobs/example_job.yaml")
    print("============================")


def run_init(*, create_job: bool = True, run_doctor_fn=None, doctor_args=None) -> int:
    """Scaffold .env (+ optional example job) and print next steps.

    Exit 0 if .env created or already exists; non-zero only on hard failure.
    """
    print("# 初始化 / Init")
    env_path, env_status = ensure_dotenv()
    if env_status == "created":
        print(f"[OK] 已创建 .env: {env_path}")
        print("     请编辑填入 OPENAI_COMPAT_*（仅翻译需要；--render-original 可跳过）")
    elif env_status == "exists":
        print(f"[OK] .env 已存在: {env_path}")
    elif env_status == "missing_example":
        print("[WARN] 未找到 .env.example，跳过 .env 创建")
        print(f"      可在仓库根目录复制模板，或手动创建 {Path.cwd() / '.env'}")
    else:
        print("[FAIL] 无法创建 .env（写权限？）", file=sys.stderr)
        return 1

    job_path = None
    if create_job:
        job_path, job_status = ensure_example_job()
        if job_status == "created":
            print(f"[OK] 已创建示例任务: {job_path}")
        elif job_status == "exists":
            print(f"[OK] 示例任务已存在: {job_path}")
        else:
            print("[WARN] 无法创建 jobs/example_job.yaml（可忽略）")

    script = current_cli_script()

    try:
        from common_utils import load_dotenv_if_present

        load_dotenv_if_present()
    except Exception:
        pass

    import os

    has_api = bool(
        os.getenv("OPENAI_COMPAT_BASE_URL")
        and os.getenv("OPENAI_COMPAT_MODEL")
        and os.getenv("OPENAI_COMPAT_API_KEY")
    )
    print_setup_next_steps(
        has_api=has_api,
        has_ffmpeg=bool(safe_which("ffmpeg") and safe_which("ffprobe")),
        script=script,
    )
    if job_path:
        command = format_cli_invocation(script)
        print(f"示例 job: {command} --job {quote_cli_arg(job_path)}")
        if not is_console_entry_script(script):
            print("或: run.bat example_job / bash run.sh example_job")

    repo = _repo_root()
    if (Path.cwd().resolve() != repo.resolve()) and (repo / ".env.example").is_file():
        print(f"\n提示: 仓库根目录也有 .env.example → {repo / '.env.example'}")

    if run_doctor_fn is not None:
        print("\n--- doctor 摘要 ---")
        try:
            code = int(run_doctor_fn(doctor_args))
        except SystemExit as e:
            code = int(e.code or 0)
        except Exception as e:
            print(f"[WARN] doctor 运行失败: {e}")
            code = 0
        return 0 if env_status in ("created", "exists") else code

    return 0 if env_status in ("created", "exists", "missing_example") else 1
