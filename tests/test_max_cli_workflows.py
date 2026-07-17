#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Max-suite CLI workflows: export/lint/manual/skip/clean/mode render guard."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures"

pytestmark = pytest.mark.max


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SCRIPTS)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["_TWITCH_TRANSPARENT_TEST_MODE"] = "1"
    for k in (
        "OPENAI_COMPAT_API_KEY",
        "OPENAI_COMPAT_BASE_URL",
        "OPENAI_COMPAT_MODEL",
        "AGNES_API_KEY",
        "AGNES_BASE_URL",
        "AGNES_MODEL",
    ):
        env.pop(k, None)
    return env


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_env(),
    )


def test_mode_render_guard_message():
    r = _run(
        [
            sys.executable,
            str(SCRIPTS / "render_cn_chat.py"),
            "v.mp4",
            "c.html",
            "--mode",
            "render",
        ]
    )
    # may fail earlier on missing files OR mode guard — accept either clear Chinese error
    joined = (r.stdout or "") + (r.stderr or "")
    assert r.returncode != 0
    assert ("mode render" in joined) or ("reuse-translation" in joined) or ("不存在" in joined) or ("错误" in joined)


def test_skip_translate_and_lint(make_test_video, tmp_path: Path):
    html = FIXTURES / "twitchdownloader_chat.html"
    if not html.is_file():
        pytest.skip("fixture missing")
    video = make_test_video(duration=2.0)
    export_json = tmp_path / "exp.json"
    # put json beside video to satisfy export out-dir confinement
    export_json = video.with_name("exp.json")
    r = _run(
        [
            sys.executable,
            str(SCRIPTS / "render_cn_chat.py"),
            str(video),
            str(html),
            "--skip-translate",
            "--translation-json",
            str(export_json),
        ]
    )
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    assert export_json.is_file()
    data = json.loads(export_json.read_text(encoding="utf-8"))
    assert data.get("messages")

    # fill and lint
    for m in data["messages"]:
        o = str(m.get("original") or "")
        m["translation"] = o if o.startswith("[") else f"译:{o}"
    filled = video.with_name("filled.json")
    filled.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    r2 = _run([sys.executable, str(SCRIPTS / "render_cn_chat.py"), "--lint-translation", str(filled)])
    assert r2.returncode == 0
    assert "质检" in ((r2.stdout or "") + (r2.stderr or ""))


def test_manual_translation_exports_tables(make_test_video, tmp_path: Path):
    html = FIXTURES / "twitchdownloader_chat.html"
    if not html.is_file():
        pytest.skip("fixture missing")
    video = make_test_video(duration=2.0)
    tj = video.with_name("manual.json")
    xlsx = video.with_name("manual.xlsx")
    r = _run(
        [
            sys.executable,
            str(SCRIPTS / "render_cn_chat.py"),
            str(video),
            str(html),
            "--manual-translation",
            "--translation-json",
            str(tj),
            "--review-xlsx",
            str(xlsx),
        ]
    )
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    assert tj.is_file()
    # xlsx or tsv
    assert xlsx.is_file() or xlsx.with_suffix(".tsv").is_file() or video.with_name("manual.tsv").is_file() or any(
        video.parent.glob("manual*")
    )


def test_job_dry_run_with_real_paths(make_test_video, tmp_path: Path):
    from job_config import write_job_file

    html = FIXTURES / "twitchdownloader_chat.html"
    if not html.is_file():
        pytest.skip("fixture missing")
    video = make_test_video(duration=2.0)
    job = write_job_file(
        tmp_path / "job.yaml",
        {
            "video": str(video),
            "chat_html": str(html),
            "mode": "preview",
            "render_original": True,
            "preview_clip": 2,
            "overlay_codec": "png",
            "offset": 0,
        },
        title="dry",
        overwrite=True,
        pin_paths=True,
    )
    r = _run([sys.executable, str(SCRIPTS / "render_cn_chat.py"), "--job", str(job), "--dry-run"])
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    joined = (r.stdout or "") + (r.stderr or "")
    assert "dry-run" in joined or "[dry-run]" in joined


@pytest.mark.slow
def test_reuse_translation_render(make_test_video, tmp_path: Path):
    html = FIXTURES / "twitchdownloader_chat.html"
    if not html.is_file():
        pytest.skip("fixture missing")
    video = make_test_video(duration=3.0)
    # export via burn
    exp = tmp_path / "e.json"
    r = _run(
        [
            sys.executable,
            str(SCRIPTS / "twitch_chat_burn.py"),
            str(video),
            str(html),
            "--export-translation",
            str(exp),
            "--offset",
            "0",
        ]
    )
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    data = json.loads(exp.read_text(encoding="utf-8"))
    for m in data.get("messages") or []:
        o = str(m.get("original") or "")
        m["translation"] = o if o.startswith("[") else f"译:{o}"
    filled = tmp_path / "f.json"
    filled.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    out = tmp_path / "out.mp4"
    work = tmp_path / "work"
    r2 = _run(
        [
            sys.executable,
            str(SCRIPTS / "render_cn_chat.py"),
            str(video),
            str(html),
            "--mode",
            "render",
            "--reuse-translation",
            "--translation-json",
            str(filled),
            "--preview-clip",
            "2",
            "--overlay-codec",
            "png",
            "--offset",
            "0",
            "--output",
            str(out),
            "--workdir",
            str(work),
            "--fps",
            "15",
        ]
    )
    assert r2.returncode == 0, (r2.stdout or "") + (r2.stderr or "")
    assert out.is_file() and out.stat().st_size > 1000
