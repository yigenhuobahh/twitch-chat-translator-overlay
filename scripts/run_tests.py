#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-command test runner for this repo.

Examples:
  python scripts/run_tests.py              # unit + smoke if ffmpeg present
  python scripts/run_tests.py --unit-only
  python scripts/run_tests.py --smoke
  python scripts/run_tests.py --max        # comprehensive long-term suite
  python scripts/run_tests.py --max --coverage
  python scripts/run_tests.py --install-dev
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

from common_utils import safe_which

ROOT = Path(__file__).resolve().parents[1]
COVERAGE_FAIL_UNDER = 65
# G-#10 per-module floor: a big module (>= COVERAGE_FLOOR_STATEMENTS statements)
# whose line coverage falls below COVERAGE_FLOOR_PERCENT fails the --coverage
# run even when the global --cov-fail-under average passes, so refactors cannot
# strand large untested code paths behind a healthy total.
COVERAGE_FLOOR_STATEMENTS = 500
COVERAGE_FLOOR_PERCENT = 60.0

# Keep compile-check in sync with pyproject py-modules / critical scripts.
COMPILE_SCRIPTS = [
    "chat_parser.py",
    "chat_schedule.py",
    "chat_text_layout.py",
    "chat_window.py",
    "cli_spec.py",
    "common_utils.py",
    "cut_timeline.py",
    "doctor_check.py",
    "download_flow.py",
    "encode_options.py",
    "env_bootstrap.py",
    "job_config.py",
    "job_wizard.py",
    "twitch_download.py",
    "twitch_download_transaction.py",
    "twitch_download_types.py",
    "layout_preset.py",
    "media_health.py",
    "media_probe.py",
    "overlay_compose.py",
    "overlay_config.py",
    "overlay_render.py",
    "overlay_scene.py",
    "pipeline_plan.py",
    "pipeline_runner.py",
    "process_util.py",
    "render_cn_chat.py",
    "render_perf.py",
    "render_preset.py",
    "review_tables.py",
    "quick_demo.py",
    "run_meta.py",
    "run_tests.py",
    "support_report.py",
    "task_events.py",
    "task_results.py",
    "td_cli_install.py",
    "tui_history.py",
    "tui_models.py",
    "tui_run.py",
    "tui_task.py",
    "translate_chat_openai.py",
    "translation_io.py",
    "translation_support.py",
    "twitch_chat_burn.py",
    "ux_setup.py",
    "vod_merge.py",
]


def run(cmd: list[str]) -> int:
    print("$", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=str(ROOT))


def _print_install_hint(module_name: str) -> None:
    print(
        f"{module_name} 未安装。可先运行:\n"
        "  python scripts/run_tests.py --install-dev\n"
        "或:\n"
        "  pip install -r requirements-dev.txt",
        file=sys.stderr,
    )


def _ensure_importable(module_name: str, install_dev: bool, *, recheck=None) -> bool:
    """Import *module_name*; optionally install dev requirements then retry once.

    ensure_pytest / ensure_pytest_cov / ensure_ruff 共用的样板：缺模块且未指定
    --install-dev 时打印统一安装提示并返回 False；允许安装则先
    ``pip install -r requirements-dev.txt`` 再重试一次。``recheck`` 是可选的
    安装后校验回调（ruff 常以独立二进制形态工作，import 成功与否不代表
    ``python -m ruff`` 可用，此时安装后改用 CLI 探针校验）；缺省用 import。
    """
    try:
        __import__(module_name)
        return True
    except ImportError:
        pass
    if not install_dev:
        _print_install_hint(module_name)
        return False
    if run([sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements-dev.txt")]) != 0:
        return False
    if recheck is not None:
        return recheck()
    try:
        __import__(module_name)
        return True
    except ImportError:
        return False


def ensure_pytest(install_dev: bool) -> bool:
    return _ensure_importable("pytest", install_dev)


def ensure_pytest_cov(install_dev: bool) -> bool:
    return _ensure_importable("pytest_cov", install_dev)


def compile_check() -> int:
    scripts = [ROOT / "scripts" / name for name in COMPILE_SCRIPTS]
    missing = [p for p in scripts if not p.is_file()]
    if missing:
        print("[WARN] compile list missing files:", ", ".join(str(p.name) for p in missing), flush=True)
    present = [str(p) for p in scripts if p.is_file()]
    return run([sys.executable, "-m", "py_compile", *present])


def _ruff_cli_available() -> bool:
    """Probe ``python -m ruff --version``; ruff ships as a standalone binary too,
    so a failing ``import ruff`` does not mean the tool is unusable."""
    probe = subprocess.run(
        [sys.executable, "-m", "ruff", "--version"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


def ensure_ruff(install_dev: bool) -> bool:
    try:
        import ruff  # noqa: F401
        return True
    except ImportError:
        # ruff is often a standalone binary via `python -m ruff`; probe the CLI
        # before treating it as missing.
        if _ruff_cli_available():
            return True
    if not install_dev:
        _print_install_hint("ruff")
        return False
    return _ensure_importable("ruff", True, recheck=_ruff_cli_available)


def lint_check() -> int:
    """Ruff lint gate (config in pyproject.toml)."""
    print("\n[lint] ruff check scripts tests", flush=True)
    return run([sys.executable, "-m", "ruff", "check", "scripts", "tests"])


def coverage_floor_check() -> int:
    """G-#10: per-module coverage floor for --coverage runs.

    Modules with >= COVERAGE_FLOOR_STATEMENTS statements must keep line
    coverage >= COVERAGE_FLOOR_PERCENT. Reads the ``.coverage`` data file that
    pytest-cov just wrote (cwd=ROOT) via ``coverage json`` and prints an
    actionable module/coverage listing on failure. Infrastructure problems
    (missing data file, broken coverage CLI) only warn and pass: the global
    ``--cov-fail-under`` gate already ran inside pytest.
    """
    import json as _json

    data_file = ROOT / ".coverage"
    if not data_file.is_file():
        print("[WARN] 覆盖率下限检查跳过: 未找到 .coverage 数据文件", flush=True)
        return 0
    report_file = ROOT / ".coverage-floor.json"
    try:
        # ``coverage json`` exits 2 when the configured global fail_under
        # ([tool.coverage.report] fail_under=65) is not met by this run — the
        # JSON report is still written and valid, so parse it regardless of rc
        # and only skip when the report itself is unusable.
        proc = subprocess.run(
            [sys.executable, "-m", "coverage", "json", "-o", str(report_file)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        if not report_file.is_file():
            print(
                f"[WARN] 覆盖率下限检查跳过: coverage json 未生成报告 (exit {proc.returncode})\n"
                f"  {(proc.stderr or proc.stdout or '').strip()[-400:]}",
                flush=True,
            )
            return 0
        report = _json.loads(report_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[WARN] 覆盖率下限检查跳过: 无法生成/解析 coverage json: {exc}", flush=True)
        return 0
    finally:
        try:
            report_file.unlink(missing_ok=True)
        except OSError:
            pass

    scripts_root = (ROOT / "scripts").resolve()
    modules: list[tuple[str, int, float]] = []
    for path, payload in (report or {}).get("files", {}).items():
        file_path = Path(path)
        if not file_path.is_absolute():
            file_path = ROOT / file_path
        try:
            file_path = file_path.resolve()
        except OSError:
            continue
        if file_path.parent != scripts_root or file_path.suffix != ".py":
            continue
        summary = (payload or {}).get("summary") or {}
        statements = int(summary.get("num_statements") or 0)
        covered = int(summary.get("covered_lines") or 0)
        if statements <= 0:
            continue
        modules.append((file_path.stem, statements, covered * 100.0 / statements))

    big = [m for m in modules if m[1] >= COVERAGE_FLOOR_STATEMENTS]
    if not big:
        print(
            f"[info] 覆盖率下限: 没有语句数 >= {COVERAGE_FLOOR_STATEMENTS} 的模块，无需分模块下限",
            flush=True,
        )
        return 0

    print(
        f"\n[coverage-floor] 语句数 >= {COVERAGE_FLOOR_STATEMENTS} 的模块行覆盖率"
        f"（下限 {COVERAGE_FLOOR_PERCENT:.0f}%）:",
        flush=True,
    )
    offenders: list[tuple[str, int, float]] = []
    for name, statements, pct in sorted(big, key=lambda item: item[2]):
        marker = ""
        if pct < COVERAGE_FLOOR_PERCENT:
            offenders.append((name, statements, pct))
            marker = "  [FAIL]"
        print(f"  {name}.py: {pct:.1f}% (statements={statements}){marker}", flush=True)

    if not offenders:
        print("[OK] 覆盖率分模块下限通过", flush=True)
        return 0

    print(
        f"\n[FAIL] 覆盖率分模块下限未达标（G-#10）: 以下模块语句数 >= {COVERAGE_FLOOR_STATEMENTS}"
        f" 但行覆盖率 < {COVERAGE_FLOOR_PERCENT:.0f}%:",
        flush=True,
    )
    for name, statements, pct in offenders:
        print(f"  - scripts/{name}.py: {pct:.1f}% (statements={statements})", flush=True)
    print(
        "  可操作: 为上述模块补充 tests/ 用例（先 `python -m coverage report --show-missing`"
        " 看未覆盖行）;\n"
        "  若该模块体积已不合理，优先拆分/瘦身后再达标。",
        flush=True,
    )
    return 1


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
    env = dict(os.environ)
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
    parser.add_argument("--smoke", action="store_true", help="run FFmpeg smoke tests; errors out if ffmpeg/ffprobe missing (default: smoke auto-runs when ffmpeg present)")
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
    parser.add_argument(
        "--coverage",
        action="store_true",
        help=f"with --max, require core scripts coverage >= {COVERAGE_FAIL_UNDER}%% "
        f"and >= {COVERAGE_FLOOR_PERCENT:.0f}%% line coverage for modules with "
        f">= {COVERAGE_FLOOR_STATEMENTS} statements (G-#10)",
    )
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
    if args.coverage and not args.max:
        print("错误: --coverage 只能与 --max 一起使用，避免快速测试产生不稳定基线", file=sys.stderr)
        return 2

    if not ensure_pytest(args.install_dev):
        print("\n[fallback] 使用 tests/test_core.py 自带 runner（无 pytest）", flush=True)
        code = run([sys.executable, str(ROOT / "tests" / "test_core.py")])
        return code
    if args.coverage and not ensure_pytest_cov(args.install_dev):
        return 2

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

    # Portable FFmpeg ships in tools/; surface it before PATH probes so the
    # ffmpeg/ffprobe detection below sees it. run_tests may run from an
    # installed wheel, so import defensively (no hard dependency).
    try:
        from env_bootstrap import prepend_tools_ffmpeg_to_path
    except ImportError:
        prepend_tools_ffmpeg_to_path = None  # type: ignore[assignment]
    try:
        if prepend_tools_ffmpeg_to_path is not None:
            prepend_tools_ffmpeg_to_path()
    except AttributeError:
        pass

    ffmpeg_ok = safe_which("ffmpeg") is not None and safe_which("ffprobe") is not None

    # --smoke 真语义：显式要求跑 FFmpeg smoke 时，缺 ffmpeg/ffprobe 直接报错退出
    # （此前 --smoke 是空操作，用户以为跑了 smoke 实际被静默跳过）。
    # 可改用 --unit-only 跳过 smoke，或安装 FFmpeg 后重试。
    if args.smoke and not ffmpeg_ok:
        print(
            "错误: --smoke 需要 ffmpeg/ffprobe，但未在 PATH（含 tools/）中找到。\n"
            "  可改用 --unit-only 跳过 smoke，或安装 FFmpeg 后重试。",
            file=sys.stderr,
        )
        return 2

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
    if args.coverage:
        pytest_cmd.extend(
            [
                "--cov=scripts",
                "--cov-report=term",
                "--cov-fail-under",
                str(COVERAGE_FAIL_UNDER),
            ]
        )

    code = run(pytest_cmd)
    if code != 0:
        return code

    if args.coverage:
        floor_code = coverage_floor_check()
        if floor_code != 0:
            return floor_code

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
