#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Experimental isolated Textual launcher; does not replace run.bat."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Footer, Header, Input, RichLog, Static

from process_util import kill_process_tree


class OverlayTui(App[None]):
    CSS = """
    Screen { layout: vertical; }
    #status { height: 3; padding: 1; }
    Input { margin: 0 1; }
    RichLog { height: 1fr; border: round $accent; }
    Horizontal { height: 3; }
    Button { margin: 0 1; }
    """
    TITLE = "Twitch Chat Overlay - Experimental TUI"

    def __init__(self) -> None:
        super().__init__()
        self.process: subprocess.Popen[str] | None = None
        self.event_path: Path | None = None
        self.event_offset = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Choose a safe local check. Existing run.bat workflows are unchanged.", id="status")
        yield Input(placeholder="Video path for a local original-chat preview", id="video")
        yield Input(placeholder="Twitch chat HTML path", id="chat")
        yield Input(placeholder="Optional job YAML path for a configured run", id="job")
        yield RichLog(id="log", wrap=True, highlight=False, markup=False)
        with Horizontal():
            yield Button("Offline demo", id="demo", variant="primary")
            yield Button("Environment check", id="doctor")
            yield Button("Preview local files", id="preview")
        with Horizontal():
            yield Button("Run configured job", id="job-run")
            yield Button("Cancel task", id="cancel", variant="warning")
            yield Button("Quit", id="quit", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.2, self._poll_process)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "quit":
            self._cancel_task()
            self.exit()
        elif event.button.id == "demo":
            self._start("Offline demo", [sys.executable, str(Path(__file__).with_name("quick_demo.py"))])
        elif event.button.id == "doctor":
            self._start("Environment check", [sys.executable, str(Path(__file__).with_name("render_cn_chat.py")), "--doctor"])
        elif event.button.id == "preview":
            self._start_preview()
        elif event.button.id == "job-run":
            self._start_job()
        elif event.button.id == "cancel":
            self._cancel_task()

    def _media_paths(self) -> tuple[Path, Path] | None:
        video = Path(self.query_one("#video", Input).value.strip().strip('"')).expanduser()
        chat = Path(self.query_one("#chat", Input).value.strip().strip('"')).expanduser()
        if not video.is_file() or not chat.is_file():
            self.query_one("#status", Static).update("Choose an existing video file and chat HTML file first.")
            return None
        return video, chat

    def _start_preview(self) -> None:
        media = self._media_paths()
        if media is None:
            return
        video, chat = media
        self._start(
            "Original-chat preview",
            [
                sys.executable,
                str(Path(__file__).with_name("render_cn_chat.py")),
                str(video),
                str(chat),
                "--mode",
                "preview",
                "--render-original",
                "--preview-clip",
                "10",
                "--yes",
            ],
        )

    def _start_job(self) -> None:
        media = self._media_paths()
        job = Path(self.query_one("#job", Input).value.strip().strip('"')).expanduser()
        if media is None:
            return
        if not job.is_file() or job.suffix.lower() not in {".yaml", ".yml"}:
            self.query_one("#status", Static).update("Choose an existing job YAML file first.")
            return
        video, chat = media
        self._start(
            "Configured job",
            [
                sys.executable,
                str(Path(__file__).with_name("render_cn_chat.py")),
                "--job",
                str(job),
                str(video),
                str(chat),
                "--yes",
            ],
        )

    def _cancel_task(self) -> None:
        if self.process and self.process.poll() is None:
            kill_process_tree(self.process.pid, force=True)
            self.query_one("#status", Static).update("Cancelling task...")

    def _start(self, label: str, command: list[str]) -> None:
        if self.process and self.process.poll() is None:
            self.query_one("#status", Static).update("A task is already running.")
            return
        events_dir = Path("outputs") / "tui_events"
        events_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="task_", suffix=".jsonl", dir=events_dir, delete=False) as handle:
            self.event_path = Path(handle.name)
        self.event_offset = 0
        env = os.environ.copy()
        env["TWITCH_OVERLAY_EVENT_FILE"] = str(self.event_path.resolve())
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
        )
        self.query_one("#status", Static).update(f"Running: {label}")
        self.query_one("#log", RichLog).write("$ " + " ".join(command))

    def _poll_process(self) -> None:
        log = self.query_one("#log", RichLog)
        if self.process:
            if self.process.poll() is not None:
                self.query_one("#status", Static).update(f"Finished with exit code {self.process.returncode}")
                self.process = None
        if self.event_path and self.event_path.is_file():
            with self.event_path.open("r", encoding="utf-8") as handle:
                handle.seek(self.event_offset)
                for line in handle:
                    log.write(self._format_event(line))
                self.event_offset = handle.tell()

    @staticmethod
    def _format_event(line: str) -> str:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return "event: invalid event record"
        name = str(event.get("event") or "event")
        stage = event.get("stage")
        program = event.get("program")
        if stage:
            return f"{name.replace('_', ' ')}: {stage}"
        if program:
            return f"{name.replace('_', ' ')}: {program}"
        return name.replace("_", " ")


def main() -> int:
    OverlayTui().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
