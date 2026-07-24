#!/usr/bin/env python3
"""Create an opt-in, shareable environment summary for bug reports.

The report deliberately runs ``--doctor`` without media inputs.  It is useful
for support, but it is not a replacement for reviewing a report before upload.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import platform
import re
import subprocess
import sys

from task_results import write_task_result
from tui_task import redact_text

_WINDOWS_PATH = re.compile(r"(?i)(?:[a-z]:\\|\\\\)[^\r\n]+")
_HOME_PATH = re.compile(r"(?<!https:)(?<!http:)(?<!file:)/(?:Users|home)/[^\s\r\n]+", re.IGNORECASE)
def redact_for_sharing(value: str) -> str:
    """Remove credentials and common private absolute-path forms from text."""
    value = redact_text(value)
    value = _WINDOWS_PATH.sub("[local path]", value)
    return _HOME_PATH.sub("[local path]", value)


def installed_version() -> str:
    """Return a useful version for both editable checkouts and installed wheels."""
    try:
        return version("twitch-chat-translator-overlay")
    except PackageNotFoundError:
        return "source checkout"


def run_doctor(*, python: str, pipeline: str | Path, timeout: float) -> tuple[int, str]:
    """Run the existing non-interactive doctor command and retain its result."""
    try:
        completed = subprocess.run(
            [python, str(pipeline), "--doctor"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, float(timeout)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return 124, str(output) + "\n[doctor timed out before completion]"
    except OSError as exc:
        return 125, f"[doctor could not start: {type(exc).__name__}]"
    return int(completed.returncode), completed.stdout or ""


def build_summary(*, doctor_returncode: int, doctor_output: str, generated_at: datetime | None = None) -> str:
    """Format a compact report which is safe to review and paste into an Issue."""
    timestamp = generated_at or datetime.now().astimezone()
    lines = [
        "Twitch Chat Overlay support summary",
        f"generated: {timestamp.isoformat(timespec='seconds')}",
        f"project version: {installed_version()}",
        f"system: {platform.system()} {platform.release()} ({platform.machine()})",
        f"python: {platform.python_version()}",
        f"doctor exit code: {doctor_returncode}",
        "",
        "doctor output (credentials and common absolute paths were removed):",
    ]
    output = redact_for_sharing(doctor_output).strip()
    lines.append(output or "[doctor produced no output]")
    lines.extend(
        [
            "",
            "Before sharing: review this file and remove any remaining private information.",
            "Do not attach API keys, OAuth values, .env files, videos, or private chat HTML.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_summary(path: str | Path, content: str) -> Path:
    """Atomically write a user-chosen support summary."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(target)
    return target


def default_output_path(root: str | Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(root) / "outputs" / "support-reports" / f"issue-summary-{stamp}.txt"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a reviewable support summary from --doctor.")
    parser.add_argument("--output", help="Report path; defaults under outputs/support-reports/")
    parser.add_argument("--timeout", type=float, default=120.0, help="Doctor timeout in seconds (default: 120)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    output = Path(args.output).expanduser() if args.output else default_output_path(root)
    doctor_returncode, doctor_output = run_doctor(
        python=sys.executable,
        pipeline=root / "scripts" / "render_cn_chat.py",
        timeout=args.timeout,
    )
    try:
        path = write_summary(output, build_summary(doctor_returncode=doctor_returncode, doctor_output=doctor_output))
    except OSError as exc:
        print(f"Could not write support summary: {type(exc).__name__}")
        return 2
    write_task_result(
        state="succeeded",
        mode="support-summary",
        returncode=0,
        artifacts=[("support_summary", path)],
    )
    print(f"Support summary written: {path}")
    print("Review it before sharing; it is not safe to upload blindly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
