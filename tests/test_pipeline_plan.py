from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from pipeline_plan import PipelinePlan


def test_plan_projects_tui_fields_to_the_existing_pipeline_contract(tmp_path: Path):
    video = tmp_path / "source.mp4"
    chat = tmp_path / "chat.html"
    video.write_bytes(b"video")
    chat.write_text("<html></html>", encoding="utf-8")
    fields = {
        "video": str(video),
        "chat_html": str(chat),
        "mode": "full",
        "output": str(tmp_path / "output.mp4"),
        "translation_json": str(tmp_path / "translation.json"),
        "preview_clip": 8.5,
        "encoder": "qsv",
        "crf": 20,
        "workers": 4,
        "source_media_check": "decode",
        "render_original": True,
        "keep_temp": True,
    }

    command = PipelinePlan(fields).build_command("python", "render_cn_chat.py")

    assert command == [
        "python",
        "render_cn_chat.py",
        str(video),
        str(chat),
        "--yes",
        "--mode",
        "full",
        "--output",
        str(tmp_path / "output.mp4"),
        "--translation-json",
        str(tmp_path / "translation.json"),
        "--encoder",
        "qsv",
        "--crf",
        "20",
        "--workers",
        "4",
        "--source-media-check",
        "decode",
        "--preview-clip",
        "8.5",
        "--render-original",
        "--keep-temp",
    ]


def test_plan_uses_an_existing_job_as_a_base_then_keeps_explicit_fields(tmp_path: Path):
    job = tmp_path / "advanced.yaml"
    job.write_text("mode: render\n", encoding="utf-8")
    fields = {"video": "source.mp4", "chat_html": "chat.html", "mode": "render"}

    command = PipelinePlan(fields, source_job=str(job)).build_command("python", "render_cn_chat.py")

    assert command[:5] == ["python", "render_cn_chat.py", "--job", str(job), "source.mp4"]
    assert command[-4:] == ["source.mp4", "chat.html", "--yes", "--mode", "render"][-4:]


def test_plan_requires_the_pipeline_input_triad():
    try:
        PipelinePlan({"video": "source.mp4", "chat_html": "chat.html"}).build_command("python", "pipeline.py")
    except ValueError as error:
        assert "video, chat_html, and mode" in str(error)
    else:
        raise AssertionError("incomplete pipeline plan was accepted")
