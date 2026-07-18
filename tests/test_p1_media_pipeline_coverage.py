"""Failure-focused coverage for media gates and pipeline download/output paths."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


def _ffprobe_payload(*, audio: bool = True, extras: list[str] | None = None) -> str:
    streams = [{
        "codec_type": "video",
        "width": 1920,
        "height": 1080,
        "start_time": "0",
        "r_frame_rate": "60/1",
        "avg_frame_rate": "60/1",
    }]
    if audio:
        streams.append({"codec_type": "audio", "start_time": "0"})
    streams.extend({"codec_type": kind} for kind in extras or [])
    return json.dumps({"format": {"duration": "10"}, "streams": streams})


def test_media_health_reports_packet_and_structure_failures(tmp_path: Path, monkeypatch):
    import media_health as health

    source = tmp_path / "broken.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(health, "safe_which", lambda _name: "tool")
    monkeypatch.setattr(health, "require_executable", lambda name: name)

    def run(command, **_kwargs):
        if "-show_packets" in command:
            return SimpleNamespace(returncode=0, stdout="0.30\n0.01\n", stderr="")
        return SimpleNamespace(returncode=0, stdout=_ffprobe_payload(audio=False, extras=["unknown"]), stderr="")

    monkeypatch.setattr(health.subprocess, "run", run)
    result = health.probe_media_health(source, expected_duration=20, tolerance=0.5)

    assert result.ok is False
    assert result.has_video is True
    assert result.has_audio is False
    assert result.abnormal_audio_packets == 0
    assert len(result.issues) >= 3


def test_media_health_can_allow_extra_streams_and_decode_timeout(tmp_path: Path, monkeypatch):
    import media_health as health

    source = tmp_path / "extra.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(health, "safe_which", lambda _name: "tool")
    monkeypatch.setattr(health, "require_executable", lambda name: name)
    monkeypatch.setattr(
        health.subprocess,
        "run",
        lambda command, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="0.021\n" if "-show_packets" in command else _ffprobe_payload(extras=["unknown"]),
            stderr="",
        ),
    )
    assert health.validate_media_health(source, mode="fast", allow_extra_streams=True).ok is True

    monkeypatch.setattr(health, "safe_which", lambda _name: "ffmpeg")
    monkeypatch.setattr(health, "require_executable", lambda _name: "ffmpeg")
    monkeypatch.setattr(
        health.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("ffmpeg", 1)),
    )
    ok, reason = health.decode_check_media(source)
    assert ok is False
    assert "完整解码检查失败" in reason


def test_media_repair_publishes_only_a_healthy_output(tmp_path: Path, monkeypatch):
    import media_health as health
    import process_util

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    monkeypatch.setattr(health, "require_executable", lambda _name: "ffmpeg")
    monkeypatch.setattr(health, "validate_media_health", lambda path, **_kwargs: health.MediaHealth(path=path, ok=True))

    def run(command, **_kwargs):
        Path(command[-1]).write_bytes(b"repaired")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(process_util, "run_tracked", run)
    output = health.repair_media(source)

    assert output.is_file()
    assert output.read_bytes() == b"repaired"
    assert source.read_bytes() == b"source"


def test_media_repair_rejects_an_unhealthy_partial_output(tmp_path: Path, monkeypatch):
    import media_health as health
    import process_util

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    monkeypatch.setattr(health, "require_executable", lambda _name: "ffmpeg")
    monkeypatch.setattr(process_util, "run_tracked", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))

    with pytest.raises(RuntimeError, match="FFmpeg"):
        health.repair_media(source)


def test_download_flow_multi_segment_publishes_result_and_events(tmp_path: Path, monkeypatch):
    import render_cn_chat as pipeline
    import twitch_download as download

    video = tmp_path / "merged.mp4"
    chat = tmp_path / "merged.html"
    video.write_bytes(b"video")
    chat.write_text("<html></html>", encoding="utf-8")
    seen: dict[str, object] = {}
    events: list[str] = []
    args = SimpleNamespace(
        download="2819850140",
        download_dir=str(tmp_path / "downloads"),
        segment=["0:00:00-0:00:05", "0:00:10-0:00:15"],
        cut=["0:00:01-0:00:02"],
        begin=None,
        end=None,
        kind="vod",
        quality="1080p60",
        oauth="token",
        download_output_fps=60.0,
        download_encoder="x264",
        download_trim_mode="Exact",
        media_check="decode",
        media_repair="audio",
        download_only=True,
        yes=True,
    )
    monkeypatch.setattr(
        download,
        "download_assets_multi",
        lambda source, segments, **kwargs: seen.update(source=source, segments=segments, kwargs=kwargs) or SimpleNamespace(video_path=video, chat_html_path=chat),
    )
    monkeypatch.setattr(pipeline, "emit_task_event", lambda event, **_kwargs: events.append(event) or True)
    monkeypatch.setattr(pipeline, "_post_download_next_steps", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(pipeline, "_TASK_RESULT_CONTEXT", {"mode": "unknown", "artifacts": []})

    assert pipeline._run_download_flow(args) == 0

    assert seen["segments"] == [("0:00:00", "0:00:05"), ("0:00:10", "0:00:15")]
    assert seen["kwargs"]["remove_ranges"] == [(1.0, 2.0)]
    assert seen["kwargs"]["media_check"] == "decode"
    assert events == ["stage_started", "stage_completed"]
    assert pipeline._TASK_RESULT_CONTEXT["artifacts"] == [("video", video), ("chat_html", chat)]


def test_download_flow_records_failure_event(tmp_path: Path, monkeypatch):
    import render_cn_chat as pipeline
    import twitch_download as download

    events: list[str] = []
    args = SimpleNamespace(
        download="2819850140", download_dir=str(tmp_path), segment=[], cut=[], begin=None, end=None,
        kind="vod", quality="720p60", oauth=None, download_trim_mode="Safe", media_check="fast",
        media_repair="audio", download_only=True, yes=True,
    )
    monkeypatch.setattr(download, "download_assets", lambda *_args, **_kwargs: (_ for _ in ()).throw(download.TwitchDownloadError("offline")))
    monkeypatch.setattr(pipeline, "emit_task_event", lambda event, **_kwargs: events.append(event) or True)

    assert pipeline._run_download_flow(args) == 2
    assert events == ["stage_started", "stage_failed"]


def test_publish_output_restores_previous_file_when_copy_fails(tmp_path: Path, monkeypatch):
    import render_cn_chat as pipeline

    source = tmp_path / "new.mp4"
    target = tmp_path / "target.mp4"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    monkeypatch.setattr(pipeline.shutil, "copy2", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        pipeline.publish_output(source, target, backup_prev=True)

    assert target.read_bytes() == b"old"
    assert source.read_bytes() == b"new"
