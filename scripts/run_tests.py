#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-command test runner for this repo.

Examples:
  python scripts/run_tests.py              # unit + smoke if ffmpeg present
  python scripts/run_tests.py --unit-only
  python scripts/run_tests.py --smoke
  python scripts/run_tests.py --max        # comprehensive long-term suite
  python scripts/run_tests.py --install-dev
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

from common_utils import safe_which

ROOT = Path(__file__).resolve().parents[1]

# Keep compile-check in sync with pyproject py-modules / critical scripts.
COMPILE_SCRIPTS = [
    "chat_parser.py",
    "chat_window.py",
    "common_utils.py",
    "encode_options.py",
    "env_bootstrap.py",
    "job_config.py",
    "job_wizard.py",
    "twitch_download.py",
    "layout_preset.py",
    "media_health.py",
    "overlay_config.py",
    "process_util.py",
    "render_cn_chat.py",
    "render_perf.py",
    "render_preset.py",
    "run_meta.py",
    "run_tests.py",
    "translate_chat_openai.py",
    "translation_support.py",
    "twitch_chat_burn.py",
    "ux_setup.py",
]


def run(cmd: list[str]) -> int:
    print("$", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=str(ROOT))


def ensure_pytest(install_dev: bool) -> bool:
    try:
        import pytest  # noqa: F401
        return True
    except ImportError:
        if not install_dev:
            print(
                "pytest 未安装。可先运行:\n"
                "  python scripts/run_tests.py --install-dev\n"
                "或:\n"
                "  pip install -r requirements-dev.txt",
                file=sys.stderr,
            )
            return False
        code = run([sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements-dev.txt")])
        if code != 0:
            return False
        try:
            import pytest  # noqa: F401
            return True
        except ImportError:
            return False


def compile_check() -> int:
    scripts = [ROOT / "scripts" / name for name in COMPILE_SCRIPTS]
    missing = [p for p in scripts if not p.is_file()]
    if missing:
        print("[WARN] compile list missing files:", ", ".join(str(p.name) for p in missing), flush=True)
    present = [str(p) for p in scripts if p.is_file()]
    return run([sys.executable, "-m", "py_compile", *present])


def ensure_ruff(install_dev: bool) -> bool:
    try:
        import ruff  # noqa: F401
        return True
    except ImportError:
        # ruff is often a standalone binary via `python -m ruff`
        pass
    # Prefer invoking as module; if missing, optionally install dev deps.
    probe = subprocess.run(
        [sys.executable, "-m", "ruff", "--version"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0:
        return True
    if not install_dev:
        print(
            "ruff 未安装。可先运行:\n"
            "  python scripts/run_tests.py --install-dev\n"
            "或:\n"
            "  pip install -r requirements-dev.txt",
            file=sys.stderr,
        )
        return False
    code = run([sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements-dev.txt")])
    if code != 0:
        return False
    probe2 = subprocess.run(
        [sys.executable, "-m", "ruff", "--version"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    return probe2.returncode == 0


def lint_check() -> int:
    """Ruff lint gate (config in pyproject.toml)."""
    print("\n[lint] ruff check scripts tests", flush=True)
    return run([sys.executable, "-m", "ruff", "check", "scripts", "tests"])


def packaging_smoke() -> int:
    """Lightweight packaging / entrypoint checks for --max (no network)."""
    print("\n[max] packaging / entrypoint smoke", flush=True)
    checks: list[tuple[str, list[str]]] = [
        ("help-pipeline", [sys.executable, str(ROOT / "scripts" / "render_cn_chat.py"), "--help"]),
        ("help-burn", [sys.executable, str(ROOT / "scripts" / "twitch_chat_burn.py"), "--help"]),
        ("list-jobs", [sys.executable, str(ROOT / "scripts" / "render_cn_chat.py"), "--list-jobs"]),
        ("doctor", [sys.executable, str(ROOT / "scripts" / "render_cn_chat.py"), "--doctor"]),
        ("wizard-help", [sys.executable, str(ROOT / "scripts" / "job_wizard.py"), "help"]),
    ]
    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    env["PYTHONPATH"] = str(ROOT / "scripts")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["_TWITCH_TRANSPARENT_TEST_MODE"] = "1"
    failed = 0
    for name, cmd in checks:
        print(f"  - {name}: {' '.join(cmd[-2:])}", flush=True)
        r = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0 and name != "doctor":
            # doctor may return 1 if env incomplete; still require it to run
            print(f"    [FAIL] rc={r.returncode}", flush=True)
            print((r.stdout or "")[-400:], flush=True)
            print((r.stderr or "")[-400:], flush=True)
            failed += 1
        elif name == "doctor" and "诊断结果" not in ((r.stdout or "") + (r.stderr or "")):
            print("    [FAIL] doctor produced no 诊断结果", flush=True)
            failed += 1
        else:
            print(f"    [OK] rc={r.returncode}", flush=True)
    # Import the packaged module surface; scripts/ also contains deliberate
    # process-only shims such as deprecated commands that exit on import.
    print("  - import-packaged-modules", flush=True)
    mods = [Path(name).stem for name in COMPILE_SCRIPTS]
    code = "\n".join(
        [
            "import importlib, sys",
            f"sys.path.insert(0, {str(ROOT / 'scripts')!r})",
            f"mods = {mods!r}",
            "errs = []",
            "for m in mods:",
            "    try:",
            "        importlib.import_module(m)",
            "    except Exception as e:",
            "        errs.append(f'{m}: {type(e).__name__}: {e}')",
            "print('imported', len(mods) - len(errs), '/', len(mods))",
            "print('\\n'.join(errs))",
            "raise SystemExit(1 if errs else 0)",
        ]
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print(r.stdout or "", flush=True)
    if r.returncode != 0:
        print(r.stderr or "", flush=True)
        failed += 1
    else:
        print("    [OK] all scripts/*.py importable", flush=True)
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run unit/smoke/max tests for twitch-chat-translator-overlay")
    parser.add_argument("--install-dev", action="store_true", help="pip install requirements-dev.txt first")
    parser.add_argument("--smoke", action="store_true", help="include FFmpeg smoke tests (default: yes if ffmpeg present)")
    parser.add_argument("--unit-only", action="store_true", help="skip smoke tests even if ffmpeg is present")
    parser.add_argument(
        "--max",
        action="store_true",
        help="comprehensive suite: compile + lint + all tests (incl. max/slow when ffmpeg) + packaging smoke",
    )
    parser.add_argument("--no-compile", action="store_true", help="skip py_compile check")
    parser.add_argument(
        "--lint",
        action="store_true",
        help="run ruff lint (also on by default with --max; config in pyproject.toml)",
    )
    parser.add_argument("--no-lint", action="store_true", help="skip ruff even when --max/--lint")
    parser.add_argument("-k", dest="keyword", default=None, help="pytest -k expression")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("--strict", action="store_true", help="fail on pytest warnings (useful for CI hardening)")
    parser.add_argument("--maxfail", type=int, default=None, help="pytest --maxfail N")
    args = parser.parse_args()

    if args.max and args.unit_only:
        print("错误: --max 与 --unit-only 不能同时使用", file=sys.stderr)
        return 2
    if args.lint and args.no_lint:
        print("错误: --lint 与 --no-lint 不能同时使用", file=sys.stderr)
        return 2

    if not ensure_pytest(args.install_dev):
        print("\n[fallback] 使用 tests/test_core.py 自带 runner（无 pytest）", flush=True)
        code = run([sys.executable, str(ROOT / "tests" / "test_core.py")])
        return code

    if not args.no_compile:
        code = compile_check()
        if code != 0:
            return code

    do_lint = (args.lint or args.max) and not args.no_lint
    if do_lint:
        if not ensure_ruff(args.install_dev):
            print("[FAIL] ruff 不可用，无法执行 --lint/--max lint 门禁", file=sys.stderr)
            return 2
        code = lint_check()
        if code != 0:
            print(
                "\n[FAIL] ruff 未通过。可先自动修部分问题:\n"
                "  python -m ruff check scripts tests --fix\n"
                "再查看剩余:\n"
                "  python -m ruff check scripts tests",
                file=sys.stderr,
            )
            return code

    ffmpeg_ok = safe_which("ffmpeg") is not None and safe_which("ffprobe") is not None

    # Marker selection
    # - unit-only: not smoke and not max (fast)
    # - default: all non-max; include smoke tests if ffmpeg (pytest still collects smoke;
    #   smoke tests self-skip without ffmpeg via fixtures)
    # - max: no marker filter (everything), plus packaging smoke
    pytest_cmd = [sys.executable, "-m", "pytest", "tests/"]
    if args.quiet:
        pytest_cmd.append("-q")
    else:
        pytest_cmd.append("-v")

    if args.unit_only:
        pytest_cmd.extend(["-m", "not smoke and not max and not slow"])
        print("[info] unit-only: 跳过 smoke/max/slow", flush=True)
    elif args.max:
        print("[info] max: 全量用例（含 max/slow；无 FFmpeg 时相关用例会 skip）", flush=True)
        if not ffmpeg_ok:
            print("[warn] 未检测到 ffmpeg/ffprobe，部分 smoke/max 会 skip", flush=True)
    else:
        # Default day-to-day: exclude slow/max layers so PR loops stay fast.
        # Smoke still runs when present (and ffmpeg available via fixtures).
        pytest_cmd.extend(["-m", "not max and not slow"])
        if not ffmpeg_ok:
            print("[info] 未检测到 ffmpeg/ffprobe，依赖 FFmpeg 的 smoke 会 skip", flush=True)
        elif args.smoke:
            print("[info] 包含 smoke（FFmpeg 短片）", flush=True)

    if args.keyword:
        pytest_cmd.extend(["-k", args.keyword])
    if args.strict:
        pytest_cmd.extend(["-W", "error::pytest.PytestUnhandledThreadExceptionWarning"])
    if args.maxfail is not None:
        pytest_cmd.extend(["--maxfail", str(args.maxfail)])
    # Max suite: show skip reasons and durations for long-term signal
    if args.max:
        pytest_cmd.extend(["-ra", "--durations=25"])

    code = run(pytest_cmd)
    if code != 0:
        return code

    if args.max:
        pack_code = packaging_smoke()
        if pack_code != 0:
            return pack_code

    print("\n[OK] 测试通过", flush=True)
    if args.max:
        print("[OK] max 套件完成（pytest 全量 + packaging smoke）", flush=True)
    elif args.unit_only:
        print("[OK] 仅单元测试（未跑 smoke/max）", flush=True)
    else:
        print("[OK] 默认套件（不含 max/slow 层）", flush=True)
    # Default/day loop skips ruff; remind before push so CI lint is not a surprise.
    if not do_lint and not args.no_lint:
        print(
            "[hint] 提交前建议再跑: python scripts/run_tests.py --lint",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
