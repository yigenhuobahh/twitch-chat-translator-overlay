#!/usr/bin/env python3
"""Execution-scoped task lifecycle for the render pipeline."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from task_events import emit_task_event as _write_task_event
from task_results import write_task_result

ArtifactCandidate = tuple[str, str | Path | None]
_ACTIVE_RUNNER: ContextVar[PipelineRunner | None] = ContextVar("twitch_overlay_pipeline_runner", default=None)


@dataclass
class PipelineRunner:
    """Own one pipeline invocation's observable state and terminal result."""

    mode: str = "unknown"
    artifacts: list[ArtifactCandidate] = field(default_factory=list)
    terminal_state: str | None = None

    def configure(self, *, mode: object, artifacts: list[ArtifactCandidate]) -> None:
        self.mode = str(mode or "unknown")
        self.artifacts = list(artifacts)

    def mark_manual_required(self) -> None:
        self.terminal_state = "manual_required"

    def emit_event(self, kind: str, **fields: Any) -> bool:
        """Write a non-fatal progress event through the existing JSONL adapter."""
        return _write_task_event(kind, **fields)

    def publish_terminal_result(self, state: str, returncode: int) -> bool:
        """Publish the one terminal manifest, preserving a manual handoff."""
        terminal_state = self.terminal_state or state
        return write_task_result(
            state=terminal_state,
            mode=self.mode,
            returncode=returncode,
            artifacts=self.artifacts,
        )


@contextmanager
def activate_runner(runner: PipelineRunner) -> Iterator[PipelineRunner]:
    """Bind a runner only for the current invocation and restore nesting safely."""
    token = _ACTIVE_RUNNER.set(runner)
    try:
        yield runner
    finally:
        _ACTIVE_RUNNER.reset(token)


def active_runner() -> PipelineRunner | None:
    """Return the invocation-scoped runner, if the caller is inside main()."""
    return _ACTIVE_RUNNER.get()


def emit_task_event(kind: str, **fields: Any) -> bool:
    """Keep legacy event call sites while routing active pipeline events centrally."""
    runner = active_runner()
    if runner is not None:
        return runner.emit_event(kind, **fields)
    return _write_task_event(kind, **fields)
