#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit fixes: --clean safety, job_dir confinement, translate index remap, review empty cells."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

from helpers import ROOT, SCRIPTS_DIR, load_module

sys.path.insert(0, str(SCRIPTS_DIR))


def _run(cmd, cwd=None, env=None):
    return subprocess.run(
        cmd,
        cwd=cwd or str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def test_clean_temp_artifacts_skips_progress_by_default(tmp_path: Path):
    from process_util import clean_temp_artifacts, make_job_dir

    job = make_job_dir(tmp_path, prefix="job_")
    (job / "frame.png").write_bytes(b"x" * 100)
    # Marked batch_* is cleaned with --clean-all; bare batch_exports must survive.
    batch = make_job_dir(tmp_path, prefix="batch_")
    (batch / "t.txt").write_text("b", encoding="utf-8")
    bare_batch = tmp_path / "batch_exports"
    bare_batch.mkdir()
    (bare_batch / "keep.txt").write_text("keep", encoding="utf-8")
    partial = tmp_path / "out.partial.mp4"
    partial.write_bytes(b"p" * 50)
    mp4_partial = tmp_path / "clip.mp4.partial"
    mp4_partial.write_bytes(b"q" * 30)
    bare_partial = tmp_path / "notes.partial"
    bare_partial.write_text("keep me", encoding="utf-8")
    progress = tmp_path / "msg.progress.json"
    progress.write_text("{}", encoding="utf-8")
    keep = tmp_path / "final.mp4"
    keep.write_bytes(b"keep")
    loose_job = tmp_path / "job_backup_final"
    loose_job.mkdir()
    (loose_job / "y").write_text("keep", encoding="utf-8")

    # Default --clean: only partials, keep all job_/batch_ dirs.
    count0, freed0 = clean_temp_artifacts(tmp_path, clean_progress=False, clean_all=False)
    assert count0 >= 2
    assert job.is_dir(), "default clean must keep job dirs"
    assert batch.is_dir(), "default clean must keep batch dirs"
    assert not partial.exists()
    assert not mp4_partial.exists()
    assert bare_partial.is_file(), "bare .partial should not be deleted by default"
    assert progress.is_file(), "progress.json must survive default clean"
    assert keep.is_file()
    assert freed0 > 0

    # Recreate partials for clean_all path checks.
    partial.write_bytes(b"p" * 50)
    mp4_partial.write_bytes(b"q" * 30)

    count, freed = clean_temp_artifacts(tmp_path, clean_progress=False, clean_all=True)
    assert count >= 3
    assert not job.exists()
    assert not batch.exists()
    assert bare_batch.is_dir(), "unmarked batch_* must survive clean"
    assert loose_job.is_dir(), "loose job_backup_final must survive clean"
    assert not partial.exists()
    assert not mp4_partial.exists()
    assert bare_partial.is_file(), "bare .partial should not be deleted by default"
    assert progress.is_file(), "progress.json must survive default clean"
    assert keep.is_file()
    assert freed > 0

    count2, _ = clean_temp_artifacts(tmp_path, clean_progress=True, clean_all=False)
    assert count2 >= 1
    assert not progress.exists()


def test_clean_temp_artifacts_only_job_dir(tmp_path: Path):
    from process_util import clean_temp_artifacts, make_job_dir

    job_a = make_job_dir(tmp_path, prefix="job_")
    (job_a / "a.bin").write_bytes(b"a")
    job_b = make_job_dir(tmp_path, prefix="job_")
    (job_b / "b.bin").write_bytes(b"b")
    partial = tmp_path / "x.partial.mp4"
    partial.write_bytes(b"zz")

    count, _ = clean_temp_artifacts(tmp_path, only_job_dir=job_a, clean_all=False)
    assert not job_a.exists()
    assert job_b.is_dir(), "only_job_dir must not delete sibling jobs"
    assert not partial.exists()
    assert count >= 2


def test_clean_temp_artifacts_refuses_only_job_outside_out_base(tmp_path: Path):
    from process_util import clean_temp_artifacts, make_job_dir

    out = tmp_path / "out"
    out.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    job = make_job_dir(outside, prefix="job_")
    (job / "x").write_text("x", encoding="utf-8")
    count, _ = clean_temp_artifacts(out, only_job_dir=job, clean_all=False)
    assert job.is_dir()
    assert count == 0


def test_is_dangerous_publish_path_cross_platform():
    from process_util import is_dangerous_publish_path

    assert is_dangerous_publish_path(r"C:\Windows\System32\drivers")
    assert is_dangerous_publish_path(r"C:\System32\preview.png")
    # Win32 extended device prefix (\\?\C:\...)
    assert is_dangerous_publish_path("\\\\?\\C:\\Windows\\Temp\\x.png")
    assert is_dangerous_publish_path(r"C:\Program Files\foo")
    assert is_dangerous_publish_path(r"C:\Users\Public\Desktop\x.png")
    assert is_dangerous_publish_path("/etc/passwd")
    assert is_dangerous_publish_path("/usr/bin/ffmpeg")
    assert is_dangerous_publish_path("/System/Library/Fonts")
    # Normal user/workspace paths are fine.
    assert not is_dangerous_publish_path(str(Path.cwd() / "preview.png"))
    assert not is_dangerous_publish_path(r"D:\videos\out\preview.png")


def test_burn_clean_cli_real_newline_and_no_progress(tmp_path: Path):
    from process_util import make_job_dir

    out = tmp_path / "out"
    out.mkdir()
    job = make_job_dir(out, prefix="job_")
    (job / "a.bin").write_bytes(b"1234")
    progress = out / "x.progress.json"
    progress.write_text("{}", encoding="utf-8")
    partial = out / "y.partial.mp4"
    partial.write_bytes(b"zz")

    dummy_video = tmp_path / "v.mp4"
    dummy_video.write_bytes(b"\x00")
    dummy_html = tmp_path / "c.html"
    dummy_html.write_text("<html></html>", encoding="utf-8")

    # Default --clean keeps job dirs; only partials go away.
    r = _run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "twitch_chat_burn.py"),
            str(dummy_video),
            str(dummy_html),
            "--clean",
            "--out-dir",
            str(out),
        ]
    )
    assert r.returncode == 0, r.stdout + r.stderr
    joined = (r.stdout or "") + (r.stderr or "")
    # Must not print the literal two-char sequence backslash-n before summary.
    assert "\\n" not in joined
    assert "[clean]" in joined
    assert job.is_dir(), "default --clean must keep job dirs"
    assert not partial.exists()
    assert progress.is_file()

    # --clean-all removes finished tool jobs.
    r2 = _run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "twitch_chat_burn.py"),
            str(dummy_video),
            str(dummy_html),
            "--clean",
            "--clean-all",
            "--out-dir",
            str(out),
        ]
    )
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert not job.exists()
    assert progress.is_file()


def test_burn_clean_only_job_dir_cli(tmp_path: Path):
    from process_util import make_job_dir

    out = tmp_path / "out"
    out.mkdir()
    job_a = make_job_dir(out, prefix="job_")
    (job_a / "a.bin").write_bytes(b"a")
    job_b = make_job_dir(out, prefix="job_")
    (job_b / "b.bin").write_bytes(b"b")
    dummy_video = tmp_path / "v.mp4"
    dummy_video.write_bytes(b"\x00")
    dummy_html = tmp_path / "c.html"
    dummy_html.write_text("<html></html>", encoding="utf-8")

    r = _run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "twitch_chat_burn.py"),
            str(dummy_video),
            str(dummy_html),
            "--clean",
            "--out-dir",
            str(out),
            "--job-dir",
            str(job_a),
        ]
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert not job_a.exists()
    assert job_b.is_dir()


def test_burn_clean_only_job_dir_outside_fails(tmp_path: Path):
    """CLI must fail when --job-dir is outside --out-dir (not silent success)."""
    out = tmp_path / "out"
    out.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    dummy_video = tmp_path / "v.mp4"
    dummy_video.write_bytes(b"\x00")
    dummy_html = tmp_path / "c.html"
    dummy_html.write_text("<html></html>", encoding="utf-8")

    r = _run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "twitch_chat_burn.py"),
            str(dummy_video),
            str(dummy_html),
            "--clean",
            "--out-dir",
            str(out),
            "--job-dir",
            str(outside),
        ]
    )
    assert r.returncode != 0
    joined = ((r.stdout or "") + (r.stderr or "")).lower()
    assert "job-dir" in joined or "out-dir" in joined or "之下" in joined


def test_render_cn_chat_clean_early_exit_no_pipeline(tmp_path: Path):
    from process_util import make_job_dir

    work = tmp_path / "wd"
    temp = work / "temp"
    temp.mkdir(parents=True)
    job = make_job_dir(temp, prefix="job_")
    (job / "junk").write_text("x", encoding="utf-8")
    progress = temp / "t.progress.json"
    progress.write_text("{}", encoding="utf-8")
    partial = temp / "z.partial.mp4"
    partial.write_bytes(b"zz")

    r = _run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "render_cn_chat.py"),
            "--clean",
            "--workdir",
            str(work),
        ]
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert job.is_dir(), "pipeline default --clean keeps job dirs"
    assert not partial.exists()
    assert progress.is_file()
    assert "清理完成" in ((r.stdout or "") + (r.stderr or ""))

    r2 = _run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "render_cn_chat.py"),
            "--clean",
            "--clean-all",
            "--workdir",
            str(work),
        ]
    )
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert not job.exists()
    assert progress.is_file()


def test_clean_all_alone_is_error(tmp_path: Path):
    r = _run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "render_cn_chat.py"),
            "--clean-all",
            "--workdir",
            str(tmp_path),
        ]
    )
    assert r.returncode != 0
    joined = (r.stdout or "") + (r.stderr or "")
    assert "clean-all" in joined.lower() or "--clean" in joined

    dummy_video = tmp_path / "v.mp4"
    dummy_html = tmp_path / "c.html"
    dummy_video.write_bytes(b"\x00")
    dummy_html.write_text("<html></html>", encoding="utf-8")
    r2 = _run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "twitch_chat_burn.py"),
            str(dummy_video),
            str(dummy_html),
            "--clean-all",
            "--out-dir",
            str(tmp_path),
        ]
    )
    assert r2.returncode != 0


def test_clean_all_skips_live_batch(tmp_path: Path):
    import json

    from process_util import clean_temp_artifacts, make_job_dir

    live = make_job_dir(tmp_path, prefix="batch_")
    (live / "run_meta.json").write_text(json.dumps({"status": "running"}), encoding="utf-8")
    (live / "keep.bin").write_bytes(b"x")
    done = make_job_dir(tmp_path, prefix="batch_")
    (done / "run_meta.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    (done / "gone.bin").write_bytes(b"y")
    corrupt = make_job_dir(tmp_path, prefix="job_")
    (corrupt / "run_meta.json").write_text("{not-json", encoding="utf-8")
    (corrupt / "maybe_live.bin").write_bytes(b"z")

    count, _ = clean_temp_artifacts(tmp_path, clean_all=True)
    assert live.is_dir(), "running batch must remain"
    assert not done.is_dir(), "finished batch should be cleaned"
    assert corrupt.is_dir(), "corrupt run_meta must fail-closed (treat as live)"
    assert count >= 1


def test_pause_preview_available_without_workdir(tmp_path: Path, monkeypatch):
    """P preview must work even when pipeline has no --workdir."""
    from types import SimpleNamespace

    import render_cn_chat as pipeline

    trans = tmp_path / "t.json"
    trans.write_text("[]", encoding="utf-8")
    xlsx = tmp_path / "r.xlsx"
    tsv = tmp_path / "r.tsv"
    video = tmp_path / "v.mp4"
    html = tmp_path / "c.html"
    video.write_bytes(b"\x00")
    html.write_text("<html></html>", encoding="utf-8")
    burn = tmp_path / "burn.py"
    burn.write_text("# stub\n", encoding="utf-8")

    monkeypatch.setattr(pipeline, "export_review_tsv", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "export_review_xlsx", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(pipeline, "DRY_RUN", False)

    calls = {"n": 0, "workdir": "unset"}

    def fake_preview(**kwargs):
        calls["n"] += 1
        calls["workdir"] = kwargs.get("workdir")
        out = tmp_path / "preview.mp4"
        out.write_bytes(b"mp4")
        return out

    monkeypatch.setattr(pipeline, "_render_preview_clip", fake_preview)
    answers = iter(["p", ""])  # P then Enter continue
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))

    args = SimpleNamespace(
        x=0, y=0, width=100, height=100, font_size=15,
        font_path="auto", font_bold_path="auto", bg_alpha=255, offset=None,
        preview_dense=False,
    )
    action = pipeline.pause_after_translation_for_review(
        trans_json=trans,
        review_xlsx=xlsx,
        review_tsv=tsv,
        auto_continue=False,
        video=video,
        chat_html=html,
        args=args,
        workdir=None,
        burn=burn,
    )
    assert action == "continue"
    assert calls["n"] == 1
    assert calls["workdir"] is None


def test_render_clean_missing_video_does_not_use_cwd(tmp_path: Path):
    r = _run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "render_cn_chat.py"),
            str(tmp_path / "nope.mp4"),
            str(tmp_path / "nope.html"),
            "--clean",
        ]
    )
    assert r.returncode != 0
    joined = (r.stdout or "") + (r.stderr or "")
    assert "不存在" in joined or "拒绝" in joined or "workdir" in joined.lower()


def test_preview_clip_output_name_is_chat_mp4_not_preview_glob(tmp_path: Path):
    """Document burn naming so pause P-preview can find the file."""
    # Source-level contract: compose writes <stem>_chat.mp4
    src = (SCRIPTS_DIR / "twitch_chat_burn.py").read_text(encoding="utf-8")
    assert 'stem + "_chat.mp4"' in src or 'stem + \'_chat.mp4\'' in src
    # And render_cn_chat looks for that name first.
    pipe = (SCRIPTS_DIR / "render_cn_chat.py").read_text(encoding="utf-8")
    assert "_chat.mp4" in pipe
    assert "preview_*s.mp4" in pipe or "_preview_" in pipe


def test_job_dir_must_be_under_out_base(tmp_path: Path):
    video = tmp_path / "v.mp4"
    html = tmp_path / "c.html"
    video.write_bytes(b"\x00")
    html.write_text("<html></html>", encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    outside = tmp_path / "outside_job"
    outside.mkdir()

    r = _run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "twitch_chat_burn.py"),
            str(video),
            str(html),
            "--out-dir",
            str(out_dir),
            "--job-dir",
            str(outside),
            "--export-translation",
            str(tmp_path / "t.json"),
        ]
    )
    assert r.returncode != 0
    joined = (r.stdout or "") + (r.stderr or "")
    assert "job-dir" in joined.lower() or "out-dir" in joined.lower() or "之下" in joined


def test_render_preset_none_default_does_not_clobber_explicit_cli():
    from render_preset import apply_render_preset_to_namespace

    preset = {
        "video_preset": "slow",
        "video_bitrate": "8M",
        "maxrate": "12M",
        "bufsize": "16M",
        "output_fps": 60,
        "encoder": "nvenc",
    }
    args = SimpleNamespace(
        encoder="x264",
        video_preset="ultrafast",  # explicit CLI (default is None)
        video_bitrate="4M",
        maxrate="6M",
        bufsize="8M",
        output_fps=24,
        crf=18,
        audio_codec="aac",
        audio_bitrate="192k",
        overlay_codec="vp9",
        webm_crf=30,
        webm_cpu_used=4,
        fps=30,
        blank_hold_seconds=0.5,
        message_image_cache_size=256,
        lazy_message_images=False,
        no_reuse_static_frames=False,
        no_skip_blank_frames=False,
    )
    applied = apply_render_preset_to_namespace(
        args,
        preset,
        cli_defaults={
            "encoder": "x264",
            "video_preset": None,
            "video_bitrate": None,
            "maxrate": None,
            "bufsize": None,
            "output_fps": None,
            "crf": 18,
            "overlay_codec": "vp9",
        },
    )
    assert args.video_preset == "ultrafast"
    assert args.video_bitrate == "4M"
    assert args.maxrate == "6M"
    assert args.bufsize == "8M"
    assert args.output_fps == 24
    assert args.encoder == "nvenc"  # still at default x264 -> applied
    assert "video_preset" not in applied
    assert "encoder" in applied


def test_layout_preset_none_default_does_not_always_apply():
    from layout_preset import apply_layout_preset_to_namespace

    args = SimpleNamespace(x=99, y=1, width=497)
    applied = apply_layout_preset_to_namespace(
        args,
        {"x": 15, "y": 50, "width": 420},
        cli_defaults={"x": 15, "y": None, "width": 497},
    )
    # x is non-default -> keep; y default is None but current is 1 -> keep; width default -> apply
    assert args.x == 99
    assert args.y == 1
    assert args.width == 420
    assert "y" not in applied
    assert "width" in applied


def test_burn_render_preset_cli_defaults_encoder_is_x264():
    """Source-level: burn cli_defaults encoder must match argparse default x264."""
    text = (SCRIPTS_DIR / "twitch_chat_burn.py").read_text(encoding="utf-8")
    assert '"encoder": "x264"' in text or "'encoder': 'x264'" in text
    # The historical bug used "auto" in render-preset cli_defaults only.
    # Ensure the apply_render_preset block no longer uses encoder auto.
    # Rough check: after render-preset section, encoder default is x264.
    idx = text.find("apply_render_preset_to_namespace")
    assert idx > 0
    chunk = text[idx : idx + 800]
    assert '"encoder": "auto"' not in chunk


def _fake_client_with_payload(payload: dict):
    client = MagicMock()
    content = json.dumps(payload, ensure_ascii=False)
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )
    return client


def test_translate_batch_local_index_remap():
    tr = load_module("translate_chat_openai", "translate_chat_openai.py")
    tr.MODEL = "test-model"
    batch = [
        {"index": 10, "original": "hello", "author": "a"},
        {"index": 11, "original": "world", "author": "b"},
    ]
    payload = {
        "translations": [
            {"index": 0, "translation": "你好"},
            {"index": 1, "translation": "世界"},
        ]
    }
    client = _fake_client_with_payload(payload)
    out = tr.translate_batch(client, batch, 1, "ctx", "zh", cache=tr.TranslationCache(None))
    assert out is not None
    by_idx = {item["index"]: item["translation"] for item in out}
    assert by_idx[10] == "你好"
    assert by_idx[11] == "世界"


def test_translate_shuffled_global_indexes_kept():
    tr = load_module("translate_chat_openai", "translate_chat_openai.py")
    tr.MODEL = "test-model"
    batch = [
        {"index": 10, "original": "hello", "author": "a"},
        {"index": 11, "original": "world", "author": "b"},
        {"index": 12, "original": "!", "author": "c"},
    ]
    # Correct globals, shuffled order — must NOT zip-order remap.
    payload = {
        "translations": [
            {"index": 12, "translation": "叹"},
            {"index": 10, "translation": "你好"},
            {"index": 11, "translation": "世界"},
        ]
    }
    client = _fake_client_with_payload(payload)
    out = tr.translate_batch(client, batch, 1, "ctx", "zh", cache=tr.TranslationCache(None))
    assert out is not None
    by_idx = {item["index"]: item["translation"] for item in out}
    assert by_idx[10] == "你好"
    assert by_idx[11] == "世界"
    assert by_idx[12] == "叹"


def test_translate_duplicate_indexes_rejected(monkeypatch):
    tr = load_module("translate_chat_openai", "translate_chat_openai.py")
    tr.MODEL = "test-model"
    monkeypatch.setattr(tr.time, "sleep", lambda *_a, **_k: None)
    batch = [
        {"index": 10, "original": "hello", "author": "a"},
        {"index": 11, "original": "world", "author": "b"},
    ]
    payload = {
        "translations": [
            {"index": 10, "translation": "A"},
            {"index": 10, "translation": "B"},
        ]
    }
    client = _fake_client_with_payload(payload)
    # Exhaust retries -> None (or cached empty)
    out = tr.translate_batch(client, batch, 1, "ctx", "zh", cache=tr.TranslationCache(None))
    assert out is None or out == []
    # All attempts should have failed on duplicate index
    assert client.chat.completions.create.call_count >= 1


def test_translate_wrong_global_indexes_not_zip_remapped(monkeypatch):
    tr = load_module("translate_chat_openai", "translate_chat_openai.py")
    tr.MODEL = "test-model"
    monkeypatch.setattr(tr.time, "sleep", lambda *_a, **_k: None)
    batch = [
        {"index": 10, "original": "hello", "author": "a"},
        {"index": 11, "original": "world", "author": "b"},
    ]
    # Same count, wrong globals (not batch-local 0..1) — must fail, not zip remap.
    payload = {
        "translations": [
            {"index": 99, "translation": "X"},
            {"index": 100, "translation": "Y"},
        ]
    }
    client = _fake_client_with_payload(payload)
    out = tr.translate_batch(client, batch, 1, "ctx", "zh", cache=tr.TranslationCache(None))
    assert out is None or out == []


def test_review_xlsx_empty_cell_does_not_wipe(tmp_path: Path):
    from openpyxl import Workbook

    import render_cn_chat as pipeline

    src = {
        "messages": [
            {"index": 0, "timestamp": 1.0, "author": "a", "original": "hi", "translation": "你好"},
            {"index": 1, "timestamp": 2.0, "author": "b", "original": "yo", "translation": "哟"},
        ]
    }
    json_path = tmp_path / "t.json"
    json_path.write_text(json.dumps(src, ensure_ascii=False, indent=2), encoding="utf-8")
    xlsx = tmp_path / "r.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["index", "timestamp", "author", "original", "translation"])
    ws.append([0, 1.0, "a", "hi", ""])  # empty wipe attempt
    ws.append([1, 2.0, "b", "yo", "人工哟"])
    wb.save(xlsx)

    pipeline.import_review_xlsx(json_path, xlsx)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["messages"][0]["translation"] == "你好"
    assert data["messages"][1]["translation"] == "人工哟"


def test_review_tsv_empty_cell_does_not_wipe(tmp_path: Path):
    import render_cn_chat as pipeline

    src = {
        "messages": [
            {"index": 0, "timestamp": 1.0, "author": "a", "original": "hi", "translation": "你好"},
        ]
    }
    json_path = tmp_path / "t.json"
    json_path.write_text(json.dumps(src, ensure_ascii=False, indent=2), encoding="utf-8")
    tsv = tmp_path / "r.tsv"
    tsv.write_text(
        "index\ttimestamp\tauthor\toriginal\ttranslation\n"
        "0\t1.0\ta\thi\t\n",
        encoding="utf-8-sig",
    )
    pipeline.import_review_tsv(json_path, tsv)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["messages"][0]["translation"] == "你好"


def test_make_job_dir_writes_marker(tmp_path: Path):
    from process_util import JOB_DIR_MARKER, is_tool_job_dir, make_job_dir

    job = make_job_dir(tmp_path, prefix="job_")
    assert (job / JOB_DIR_MARKER).is_file()
    assert is_tool_job_dir(job)

def test_pause_stop_hint_includes_installed_media_paths(tmp_path: Path, monkeypatch, capsys):
    import render_cn_chat as pipeline

    entry = tmp_path / "Program Files" / "twitch-chat-overlay.exe"
    video = tmp_path / "media & clips" / "video one.mp4"
    chat = tmp_path / "media & clips" / "chat one.html"
    trans = tmp_path / "review files" / "translations.json"
    review_xlsx = tmp_path / "review files" / "review sheet.xlsx"
    review_tsv = tmp_path / "review files" / "review.tsv"
    workdir = tmp_path / "work files"
    output = tmp_path / "final output.mp4"

    monkeypatch.setattr(sys, "argv", [str(entry)])
    monkeypatch.setattr(pipeline, "export_review_tsv", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "export_review_xlsx", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(pipeline, "DRY_RUN", False)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "s")

    action = pipeline.pause_after_translation_for_review(
        trans_json=trans,
        review_xlsx=review_xlsx,
        review_tsv=review_tsv,
        video=video,
        chat_html=chat,
        args=SimpleNamespace(output=str(output)),
        workdir=workdir,
    )

    out = capsys.readouterr().out
    expected_prefix = (
        f"{pipeline.current_cli_invocation()} {pipeline.quote_cli_arg(video)} "
        f"{pipeline.quote_cli_arg(chat)}"
    )
    assert action == "stop"
    assert expected_prefix in out
    assert f"--workdir {pipeline.quote_cli_arg(workdir)}" in out
    assert f"--output {pipeline.quote_cli_arg(output)}" in out


def test_noninteractive_job_hint_quotes_job_path(tmp_path: Path):
    from common_utils import quote_cli_arg

    job = tmp_path / "jobs with spaces" / "style one.yaml"
    job.parent.mkdir(parents=True)
    job.write_text("mode: preview\nrender_original: true\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "render_cn_chat.py"), "--job", str(job)],
        cwd=str(ROOT),
        input="",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    output = (result.stdout or "") + (result.stderr or "")
    assert result.returncode != 0
    assert f"--job {quote_cli_arg(job.resolve())}" in output
