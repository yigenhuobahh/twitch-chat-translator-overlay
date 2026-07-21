"""Long-term contracts for user-facing setup, launch, and job workflows."""

from __future__ import annotations

from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_public_issue_forms_protect_credentials_and_ci_exercises_windows_launchers():
    """Support intake must be structured, privacy-aware, and kept runnable."""
    issue_dir = ROOT / ".github" / "ISSUE_TEMPLATE"
    if not issue_dir.is_dir():
        pytest.skip("GitHub issue forms are repository maintenance files, not package data")

    bug = yaml.safe_load((issue_dir / "bug_report.yml").read_text(encoding="utf-8"))
    feature = yaml.safe_load((issue_dir / "feature_request.yml").read_text(encoding="utf-8"))
    config = yaml.safe_load((issue_dir / "config.yml").read_text(encoding="utf-8"))

    assert bug["name"] == "Bug report"
    assert feature["name"] == "Feature request"
    assert config["blank_issues_enabled"] is False
    diagnostic = next(field for field in bug["body"] if field.get("id") == "diagnostic")
    assert "OAuth" in diagnostic["attributes"]["description"]
    fields = {field.get("id"): field for field in bug["body"] if field.get("id")}
    assert {"doctor", "media", "screenshot"}.issubset(fields)
    assert "never paste .env" in fields["doctor"]["attributes"]["description"]
    privacy = next(field for field in bug["body"] if field.get("id") == "privacy")
    assert privacy["attributes"]["options"][0]["required"] is True
    assert any(link["name"] == "First-run guide and issue checklist" for link in config["contact_links"])

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "新人推荐路线" in readme
    assert "反馈与提交 Issue" in readme
    assert "离线演示" in readme

    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "Smoke Windows batch launchers" in ci
    assert "call run.bat --help" in ci
    assert "call run_cli.bat help" in ci


def _stub_job_io(monkeypatch, wizard, job: dict, captured: list[tuple[str, ...]]) -> None:
    monkeypatch.setattr(wizard, "load_job_file", lambda _path: dict(job))
    monkeypatch.setattr(wizard, "summarize_job", lambda _path: "test job")
    monkeypatch.setattr(wizard, "save_last_job", lambda _path: None)
    monkeypatch.setattr(wizard, "_report_run_success", lambda *_args, **_kwargs: None)

    def fake_run(*args: str) -> int:
        captured.append(tuple(args))
        return 0

    monkeypatch.setattr(wizard, "_run_pipeline", fake_run)


def test_pinned_job_eof_accepts_default_confirmation(tmp_path: Path, monkeypatch):
    """EOF at an optional confirmation is equivalent to pressing Enter."""
    import job_wizard as wizard

    media = tmp_path / "media with spaces"
    media.mkdir()
    video = media / "source video.mp4"
    chat = media / "chat export.html"
    video.write_bytes(b"video")
    chat.write_text("<html></html>", encoding="utf-8")
    job_path = tmp_path / "pinned.yaml"
    job_path.write_text("mode: preview\n", encoding="utf-8")
    job = {
        "video": str(video),
        "chat_html": str(chat),
        "mode": "preview",
        "render_original": True,
    }
    captured: list[tuple[str, ...]] = []
    _stub_job_io(monkeypatch, wizard, job, captured)

    def eof(*_args, **_kwargs):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)

    assert wizard._confirm_and_run_job(job_path) == 0
    assert captured == [("--job", str(job_path), str(video), str(chat))]


def test_job_run_forwards_yes_and_path_overrides_exactly(tmp_path: Path, monkeypatch):
    import job_wizard as wizard

    video = tmp_path / "source.mp4"
    chat = tmp_path / "chat.html"
    original_translation = tmp_path / "original.json"
    video.write_bytes(b"video")
    chat.write_text("<html></html>", encoding="utf-8")
    original_translation.write_text("{}", encoding="utf-8")

    job_path = tmp_path / "reuse.yaml"
    job_path.write_text("mode: render\n", encoding="utf-8")
    job = {
        "video": str(video),
        "chat_html": str(chat),
        "output": str(tmp_path / "job-output.mp4"),
        "workdir": str(tmp_path / "job-work"),
        "translation_json": str(original_translation),
        "reuse_translation": True,
        "mode": "render",
    }
    cli_output = tmp_path / "cli output.mp4"
    cli_work = tmp_path / "cli work"
    cli_translation = tmp_path / "cli translation.json"
    cli_work.mkdir()
    cli_translation.write_text("{}", encoding="utf-8")

    captured: list[tuple[str, ...]] = []
    _stub_job_io(monkeypatch, wizard, job, captured)
    monkeypatch.setattr(wizard, "_prompt", lambda _message, _default=None: "")

    extra = [
        "--output",
        str(cli_output),
        f"--workdir={cli_work}",
        "--translation-json",
        str(cli_translation),
        "--yes",
        "--preview-clip",
        "7",
    ]
    assert wizard._confirm_and_run_job(job_path, extra_cli=extra) == 0
    assert captured == [
        (
            "--job",
            str(job_path),
            str(video),
            str(chat),
            "--output",
            str(cli_output),
            "--workdir",
            str(cli_work),
            "--translation-json",
            str(cli_translation),
            "--reuse-translation",
            "--yes",
            "--preview-clip",
            "7",
        )
    ]


def test_noninteractive_job_without_media_fails_before_pipeline(tmp_path: Path, monkeypatch):
    import job_wizard as wizard

    job_path = tmp_path / "style-only.yaml"
    job_path.write_text("mode: preview\n", encoding="utf-8")
    monkeypatch.setattr(
        wizard,
        "load_job_file",
        lambda _path: {"mode": "preview", "render_original": True},
    )
    monkeypatch.setattr(wizard, "summarize_job", lambda _path: "style only")
    monkeypatch.setattr(wizard, "_stdin_is_interactive", lambda: False)
    monkeypatch.setattr(
        wizard,
        "_run_pipeline",
        lambda *_args: pytest.fail("pipeline must not start without media"),
    )

    def eof(*_args, **_kwargs):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert wizard._confirm_and_run_job(job_path) == 1


def test_dragged_video_and_html_start_api_free_preview(tmp_path: Path, monkeypatch):
    import job_wizard as wizard

    video = tmp_path / "source.mp4"
    chat = tmp_path / "chat.html"
    video.write_bytes(b"video")
    chat.write_text("<html></html>", encoding="utf-8")
    captured: list[tuple[str, ...]] = []
    monkeypatch.setattr(wizard, "_run_pipeline", lambda *args: captured.append(args) or 0)

    assert wizard.run_drag_drop([str(video), str(chat)]) == 0
    assert captured == [
        (str(video), str(chat), "--mode", "preview", "--render-original", "--preview-clip", "10", "--yes")
    ]


def test_dragged_job_uses_existing_job_flow(tmp_path: Path, monkeypatch):
    import job_wizard as wizard

    job = tmp_path / "style.yaml"
    job.write_text("mode: preview\n", encoding="utf-8")
    captured: list[tuple[Path, list[str] | None]] = []
    monkeypatch.setattr(
        wizard,
        "_confirm_and_run_job",
        lambda path, extra_cli=None: captured.append((path, extra_cli)) or 0,
    )

    assert wizard.run_drag_drop([str(job), "--yes"]) == 0
    assert captured == [(job, ["--yes"])]


@pytest.mark.parametrize(
    ("guide_result", "cli_available", "expected_code"),
    [
        (False, False, 1),
        (True, False, 1),
        (False, True, 0),
    ],
)
def test_explicit_td_install_exit_code_reflects_actual_cli(
    monkeypatch,
    guide_result: bool,
    cli_available: bool,
    expected_code: int,
):
    import render_cn_chat as pipeline
    import twitch_download as td

    seen: list[bool] = []
    monkeypatch.setattr(pipeline, "install_process_cleanup_handlers", lambda: None)
    monkeypatch.setattr(
        pipeline,
        "offer_td_cli_guide",
        lambda *, assume_yes=False: seen.append(assume_yes) or guide_result,
    )
    monkeypatch.setattr(
        td,
        "find_twitchdownloader_cli",
        lambda: Path("TwitchDownloaderCLI.exe") if cli_available else None,
    )
    monkeypatch.setattr(sys, "argv", ["render_cn_chat.py", "--offer-td-cli", "--yes"])

    with pytest.raises(SystemExit) as exc:
        pipeline.main()

    assert exc.value.code == expected_code
    assert seen == [True]


def test_optional_td_install_prompt_remains_best_effort(monkeypatch):
    import render_cn_chat as pipeline

    seen: list[bool] = []
    monkeypatch.setattr(pipeline, "install_process_cleanup_handlers", lambda: None)
    monkeypatch.setattr(
        pipeline,
        "maybe_prompt_offer_td_cli",
        lambda *, assume_yes=False: seen.append(assume_yes) or False,
    )
    monkeypatch.setattr(sys, "argv", ["render_cn_chat.py", "--install-td-prompt", "--yes"])

    with pytest.raises(SystemExit) as exc:
        pipeline.main()

    assert exc.value.code == 0
    assert seen == [True]


@pytest.mark.parametrize(
    ("name", "entrypoint"),
    [
        ("run_cli.bat", r'"%PY%" scripts\job_wizard.py'),
        ("doctor.bat", r'"%PY%" scripts\render_cn_chat.py --doctor'),
    ],
)
def test_windows_launchers_prefer_and_validate_venv(name: str, entrypoint: str):
    text = (ROOT / name).read_text(encoding="ascii")
    venv_guard = text.index(r'if exist ".venv\Scripts\python.exe"')
    venv_select = text.index(r'set "PY=.venv\Scripts\python.exe"', venv_guard)
    path_fallback = text.index("where python", venv_select)
    version_check = text.index('"%PY%" -c "import sys;', path_fallback)
    launch = text.index(entrypoint, version_check)

    assert venv_guard < venv_select < path_fallback < version_check < launch


def test_run_bat_opens_tui_only_without_arguments_and_keeps_cli_forwarding():
    text = (ROOT / "run.bat").read_text(encoding="ascii")
    assert 'if /I "%~1"==""' in text
    assert "run_tui.bat" in text
    assert 'run_cli.bat" %*' in text


def test_run_cli_preserves_explicit_pipeline_arguments_before_drag_drop_route():
    text = (ROOT / "run_cli.bat").read_text(encoding="ascii")
    pipeline = text.index(":PIPELINE")
    route = text.index(r'"%PY%" scripts\job_wizard.py drop %*')

    assert 'if "%FIRST:~0,1%"=="-" goto PIPELINE' in text
    assert 'if not "%~3"=="" if exist "%~1" goto PIPELINE' in text
    assert 'if /I "%~1"=="--help" goto PIPELINE' in text
    assert pipeline > route


def test_install_bat_propagates_required_step_failures():
    text = (ROOT / "install.bat").read_text(encoding="ascii")

    patterns = [
        r"%PY% -m venv \.venv\s+if errorlevel 1 \(",
        r'"%PY%" -m pip install -U pip\s+if errorlevel 1 \(',
        (
            r'if exist "requirements\.txt" \(\s*'
            r'"%PY%" -m pip install -r requirements\.txt\s*'
            r'\) else \(\s*"%PY%" -m pip install -e \.\s*\)\s*'
            r"if errorlevel 1 \("
        ),
        (
            r'"%PY%" scripts\\render_cn_chat\.py --init\s*'
            r'set "RC=%ERRORLEVEL%"\s*if not "%RC%"=="0" \('
        ),
        (
            r'"%PY%" scripts\\render_cn_chat\.py --doctor\s*'
            r'set "RC=%ERRORLEVEL%"\s*if not "%RC%"=="0" \('
        ),
    ]
    for pattern in patterns:
        assert re.search(pattern, text, flags=re.MULTILINE), pattern

    optional_pos = text.index(r'"%PY%" scripts\render_cn_chat.py --install-td-prompt')
    final_success = text.index("exit /b 0", optional_pos)
    assert optional_pos < final_success


def test_init_scaffolds_files_and_runs_doctor_summary(tmp_path: Path, monkeypatch):
    import common_utils
    import ux_setup

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ux_setup, "find_env_example", lambda: ROOT / ".env.example")
    monkeypatch.setattr(ux_setup, "print_setup_next_steps", lambda **_kwargs: None)
    monkeypatch.setattr(ux_setup, "current_cli_script", lambda: "twitch-chat-overlay")
    monkeypatch.setattr(ux_setup, "_repo_root", lambda: tmp_path / "no-source")
    monkeypatch.setattr(common_utils, "load_dotenv_if_present", lambda: None)
    doctor_args = SimpleNamespace(doctor=True)
    seen: list[object] = []

    def doctor(args) -> int:
        seen.append(args)
        return 1

    assert ux_setup.run_init(create_job=True, run_doctor_fn=doctor, doctor_args=doctor_args) == 0
    assert (tmp_path / ".env").read_text(encoding="utf-8") == (ROOT / ".env.example").read_text(encoding="utf-8")
    example = tmp_path / "jobs" / "example_job.yaml"
    assert example.is_file()
    assert len(example.read_text(encoding="utf-8").splitlines()) >= 60
    assert seen == [doctor_args]


def test_readiness_distinguishes_render_from_translation():
    from env_bootstrap import CheckItem, readiness_levels

    items = [
        CheckItem("python", "Python", True, required_for_render=True),
        CheckItem("ffmpeg", "FFmpeg", False, required_for_render=True),
        CheckItem("pkg:openai", "OpenAI", True, required_for_render=False, required_for_translate=True),
        CheckItem("api", "API", False, required_for_render=False, required_for_translate=True),
    ]

    assert readiness_levels(items) == (False, False)
    items[1].ok = True
    assert readiness_levels(items) == (True, False)
    items[3].ok = True
    assert readiness_levels(items) == (True, True)


def test_post_run_cleanup_stays_inside_workdir_temp(tmp_path: Path, monkeypatch):
    import job_wizard as wizard
    from process_util import make_job_dir

    work = tmp_path / "work"
    temp = work / "temp"
    temp.mkdir(parents=True)
    partial = temp / "render.partial.mp4"
    progress = temp / "render.progress.json"
    ordinary = temp / "notes.txt"
    outside = work / "outside.partial.mp4"
    partial.write_bytes(b"partial")
    progress.write_text("{}", encoding="utf-8")
    ordinary.write_text("keep", encoding="utf-8")
    outside.write_bytes(b"outside")
    finished_job = make_job_dir(temp, prefix="job_")
    (finished_job / "frame.bin").write_bytes(b"frame")

    monkeypatch.setattr(wizard, "_prompt", lambda _message, _default=None: "yes")
    wizard._maybe_clean_temp_after_run({"workdir": str(work)}, {})

    assert not partial.exists()
    assert progress.is_file()
    assert ordinary.is_file()
    assert outside.is_file()
    assert finished_job.is_dir()
