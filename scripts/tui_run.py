#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Experimental isolated Textual launcher; does not replace run.bat."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Footer, Header, RichLog, Static


class OverlayTui(App[None]):
    CSS = """
    Screen { layout: vertical; }
    #status { height: 3; padding: 1; }
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
        yield RichLog(id="log", wrap=True, highlight=False, markup=False)
        with Horizontal():
            yield Button("Offline demo", id="demo", variant="primary")
            yield Button("Environment check", id="doctor")
            yield Button("Quit", id="quit", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.2, self._poll_process)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "quit":
            self.exit()
        elif event.button.id == "demo":
            self._start("Offline demo", [sys.executable, str(Path(__file__).with_name("quick_demo.py"))])
        elif event.button.id == "doctor":
            self._start("Environment check", [sys.executable, str(Path(__file__).with_name("render_cn_chat.py")), "--doctor"])

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
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        self.query_one("#status", Static).update(f"Running: {label}")
        self.query_one("#log", RichLog).write("$ " + " ".join(command))

    def _poll_process(self) -> None:
        log = self.query_one("#log", RichLog)
        if self.process and self.process.stdout:
            if self.process.poll() is not None:
                output = self.process.stdout.read()
                for line in output.splitlines():
                    log.write(line)
                self.query_one("#status", Static).update(f"Finished with exit code {self.process.returncode}")
                self.process = None
        if self.event_path and self.event_path.is_file():
            with self.event_path.open("r", encoding="utf-8") as handle:
                handle.seek(self.event_offset)
                for line in handle:
                    log.write("event: " + line.rstrip())
                self.event_offset = handle.tell()


def main() -> int:
    OverlayTui().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
