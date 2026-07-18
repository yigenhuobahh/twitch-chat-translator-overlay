from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def test_source_media_gate_uses_requested_mode_without_requiring_audio(monkeypatch, tmp_path: Path):
    from media_health import MediaHealth
    import render_cn_chat as pipeline

    video = tmp_path / "silent.mp4"
    video.write_bytes(b"x")
    calls: list[dict] = []
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        pipeline,
        "validate_media_health",
        lambda path, **kwargs: calls.append({"path": path, **kwargs}) or MediaHealth(path=path, ok=True),
    )
    monkeypatch.setattr(pipeline, "emit_task_event", lambda event, **fields: events.append((event, fields)) or True)

    pipeline.validate_source_media(video, mode="decode")

    assert calls == [{"path": video, "mode": "decode", "require_audio": False}]
    assert [event for event, _ in events] == ["stage_started", "stage_completed"]


def test_source_media_gate_stops_before_pipeline_on_health_failure(monkeypatch, tmp_path: Path):
    from media_health import MediaHealth
    import render_cn_chat as pipeline

    video = tmp_path / "broken.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr(
        pipeline,
        "validate_media_health",
        lambda path, **kwargs: MediaHealth(path=path, ok=False, issues=["corrupt packet"]),
    )

    with pytest.raises(pipeline.PipelineError, match="输入视频健康检查失败"):
        pipeline.validate_source_media(video, mode="decode")


def test_source_media_gate_dry_run_does_not_probe(monkeypatch, tmp_path: Path):
    import render_cn_chat as pipeline

    video = tmp_path / "source.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr(pipeline, "validate_media_health", lambda *args, **kwargs: pytest.fail("must not probe"))

    pipeline.validate_source_media(video, mode="decode", dry_run=True)
