#!/usr/bin/env python3
"""Bounded subprocess and event handling used by the Textual launcher."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading

from process_util import (  # noqa: F401  (redact_command re-exported; single source in process_util)
    kill_process_tree,
    redact_command,
)
from task_results import read_task_result

EVENT_DIRECTORY = Path("outputs") / ".tui-events"
_BASE_URL_VALUE = re.compile(
    r"(?im)([^\r\n]*(?:(?:OPENAI_COMPAT|AGNES)_BASE_URL|翻译 Base URL|Translation Base URL)\s*[:=]\s*)[^\r\n]*"
)
_URL_USERINFO = re.compile(r"(?i)(https?://)[^\s/@]*[^\s/@]@[^\s/]*/?")
_AUTHORIZATION_SECRET = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer|oauth|basic)\s+)[^\s,;]+"
)
# ``authorization`` is handled after _AUTHORIZATION_SECRET so scheme-prefixed
# values keep their dedicated rule; the named-secret rule catches the bare
# ``authorization: <value>`` form.
_NAMED_SECRET = re.compile(
    r"(?i)((?:api[_ -]?key|token|password|oauth|authorization|(?:client[_ -]?)?secret)\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_JSON_SECRET = re.compile(
    r"(?i)(\"(?:api[_ -]?key|token|password|oauth|(?:client[_ -]?)?secret)\"\s*:\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)
_OAUTH_ARGUMENT = re.compile(r"(?i)(--oauth(?:\s+|=))(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)")
# 只脱敏密钥形态的变量名（后缀白名单）；BASE_URL 等普通名称交给值侧规则，
# 避免“抹名不抹值”式过度脱敏。
_ENVIRONMENT_VARIABLE = re.compile(
    r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_(?:KEY|TOKEN|SECRET|PASSWORD)\b"
)
# 现由 process_util.redact_command / _SECRET_ARGUMENT_FLAGS 单源维护。


def redact_text(value: str) -> str:
    """Remove common secret-shaped log fragments before they reach UI/export."""
    value = _BASE_URL_VALUE.sub(r"\1[redacted]", value)
    value = _URL_USERINFO.sub(r"\1[redacted]/", value)
    value = _OAUTH_ARGUMENT.sub(r"\1[redacted]", value)
    value = _AUTHORIZATION_SECRET.sub(r"\1[redacted]", value)
    value = _JSON_SECRET.sub(r"\1\"[redacted]\"", value)
    value = _NAMED_SECRET.sub(r"\1[redacted]", value)
    return _ENVIRONMENT_VARIABLE.sub("[environment variable]", value)


def _diagnostic_line(value: str) -> str:
    """Keep executable commands out of shareable diagnostic exports."""
    if value.startswith("$ "):
        return "[command omitted for privacy]"
    return redact_text(value)


def _unique_sibling_temp(target: Path) -> Path:
    """Create a unique temp file in ``target``'s directory via mkstemp.

    固定 "<name><suffix>.tmp" 兄弟名在无锁时会让两个并发写者互踩
    （run_meta.py 曾记载同款教训）：一个写者刚写完的 tmp 会被另一个写者的
    write 覆盖，随后两次 replace 让“后完成者获胜”污染最终内容。mkstemp
    保证每个写者拿到独立路径，同目录则保住 os.replace 的原子性语义。
    """
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    os.close(fd)
    return Path(name)


def sanitize_diagnostic_file(path: str | Path) -> Path:
    """Migrate a prior diagnostic export to current privacy guarantees."""
    target = Path(path)
    raw = target.read_text(encoding="utf-8")
    cleaned = "\n".join(_diagnostic_line(line) for line in raw.splitlines()) + "\n"
    if cleaned != raw:
        temporary = _unique_sibling_temp(target)
        try:
            temporary.write_text(cleaned, encoding="utf-8")
            temporary.replace(target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return target


def format_event(record: dict) -> str:
    name = str(record.get("event") or "event").replace("_", " ")
    stage = record.get("stage")
    if stage:
        completed, total = record.get("completed"), record.get("total")
        if completed is not None and total:
            return f"{name}: {stage} ({completed}/{total})"
        return f"{name}: {stage}"
    if record.get("program"):
        return f"{name}: {record['program']}"
    return name


class TaskSession:
    """A single pipeline process with non-blocking output and JSONL events."""

    def __init__(self, command: list[str], *, cwd: str | Path | None = None) -> None:
        self.command = [str(value) for value in command]
        self.cwd = None if cwd is None else str(cwd)
        self.process: subprocess.Popen[str] | None = None
        self.event_path: Path | None = None
        self.result_path: Path | None = None
        self.result: dict | None = None
        self._event_offset = 0
        self._event_tail = ""
        self._output: queue.Queue[str] = queue.Queue(maxsize=500)
        self._event_lines: deque[str] = deque(maxlen=800)
        self._log_lines: deque[str] = deque(maxlen=1200)
        self._reader: threading.Thread | None = None
        self.cancelled = False
        self.dropped_output = 0

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def returncode(self) -> int | None:
        return None if self.process is None else self.process.poll()

    def start(self) -> None:
        if self.running:
            raise RuntimeError("A task is already running")
        directory = (Path(self.cwd) if self.cwd else Path.cwd()) / EVENT_DIRECTORY
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="task_", suffix=".jsonl", dir=directory, delete=False) as handle:
            self.event_path = Path(handle.name)
        with tempfile.NamedTemporaryFile(prefix="task_", suffix=".result.json", dir=directory, delete=False) as handle:
            self.result_path = Path(handle.name)
        self._event_offset = 0
        self._event_tail = ""
        env = os.environ.copy()
        env["TWITCH_OVERLAY_EVENT_FILE"] = str(self.event_path.resolve())
        env["TWITCH_OVERLAY_RESULT_FILE"] = str(self.result_path.resolve())
        popen_options: dict[str, object] = {}
        if os.name != "nt":
            # kill_process_tree uses killpg on POSIX, so the task must not
            # share the Textual launcher's process group.
            popen_options["start_new_session"] = True
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **popen_options,
            )
        except OSError:
            self.cleanup(keep_failure=False)
            raise
        self._reader = threading.Thread(target=self._read_output, name="tui-task-output", daemon=True)
        self._reader.start()

    def _read_output(self) -> None:
        if self.process is None or self.process.stdout is None:
            return
        # Limit each read so one malformed unbroken output line cannot consume
        # unbounded memory before the queue cap takes effect.
        for line in iter(lambda: self.process.stdout.readline(8192), ""):
            clean = redact_text(line.rstrip())
            try:
                self._output.put_nowait(clean)
            except queue.Full:
                # Preserve the newest output; terminal errors are commonly
                # written at the end of a failed child process.
                try:
                    self._output.get_nowait()
                except queue.Empty:
                    pass
                else:
                    try:
                        self._output.put_nowait(clean)
                    except queue.Full:
                        pass
                # 读取线程与 UI 线程并发 +=；最坏情况是并发窗口内丢失一次
                # 计数（界面少提示一行“已省略 N 行”），可接受且无需加锁。
                self.dropped_output += 1
        self.process.stdout.close()

    def drain_after_exit(self, *, timeout: float = 1.0) -> tuple[list[str], list[str]]:
        """Collect buffered final output before diagnostics are persisted."""
        if self._reader:
            self._reader.join(timeout=max(0.0, timeout))
        return self.poll()

    def poll(self) -> tuple[list[str], list[str]]:
        """Return newly received (log lines, formatted event lines)."""
        logs: list[str] = []
        while True:
            try:
                line = self._output.get_nowait()
            except queue.Empty:
                break
            logs.append(line)
            self._log_lines.append(line)
        events: list[str] = []
        if self.event_path and self.event_path.is_file():
            try:
                with self.event_path.open("r", encoding="utf-8") as handle:
                    handle.seek(self._event_offset)
                    appended = handle.read()
                    self._event_offset = handle.tell()
                    complete, separator, tail = (self._event_tail + appended).rpartition("\n")
                    self._event_tail = tail if separator else self._event_tail + appended
                    if not separator:
                        complete = ""
                    for line in complete.splitlines():
                        raw = line.rstrip("\r")
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        rendered = format_event(event)
                        events.append(rendered)
                        self._event_lines.append(rendered)
            except OSError:
                pass
        if self.result_path and self.result is None and self.process and self.process.poll() is not None:
            self.result = read_task_result(self.result_path)
        return logs, events

    def cancel(self) -> bool:
        if not self.running or self.process is None:
            return False
        self.cancelled = True
        kill_process_tree(self.process.pid, force=True)
        return True

    def cleanup(self, *, keep_failure: bool = True) -> None:
        """Remove transient events unless a failed run is retained for export."""
        failed = self.returncode not in (None, 0) and not self.cancelled
        if keep_failure and failed:
            return
        if self.event_path:
            try:
                self.event_path.unlink(missing_ok=True)
            except OSError:
                pass
        if self.result_path:
            try:
                self.result_path.unlink(missing_ok=True)
            except OSError:
                pass

    def retain_result(self, path: str | Path) -> Path | None:
        """Move a valid result manifest into durable TUI history storage."""
        if self.result is None or self.result_path is None or not self.result_path.is_file():
            return None
        target = Path(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # History can be redirected to another drive in tests or by a
            # portable install.  Copy-then-replace remains atomic at target.
            # 唯一临时名：固定 ".tmp" 兄弟名在两个会话并发 retain 同一目标时互踩。
            temporary = _unique_sibling_temp(target)
            shutil.copyfile(self.result_path, temporary)
            os.replace(temporary, target)
            self.result_path.unlink(missing_ok=True)
        except OSError:
            return None
        self.result_path = None
        return target

    def export_diagnostics(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = ["Twitch Chat Overlay TUI diagnostic", f"returncode: {self.returncode}", "", "events:"]
        lines.extend(_diagnostic_line(line) for line in self._event_lines)
        lines.append("")
        lines.append("output:")
        lines.extend(_diagnostic_line(line) for line in self._log_lines)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return target

    def close(self) -> None:
        if self.running:
            self.cancel()
        if self._reader:
            self._reader.join(timeout=1)
        self.cleanup(keep_failure=False)


def drain_session(session: TaskSession, *, limit: int = 100) -> Iterable[str]:
    """Small test/CLI helper returning at most ``limit`` newly observed lines."""
    logs, events = session.poll()
    return (logs + events)[:limit]
