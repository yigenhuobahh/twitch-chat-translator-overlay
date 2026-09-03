#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for job.yaml load/apply (CLI wins)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import load_module


@pytest.fixture(scope="module")
def job_mod():
    return load_module("job_config", "job_config.py")


def test_load_job_resolves_relative_paths(tmp_path: Path, job_mod):
    video = tmp_path / "v.mp4"
    chat = tmp_path / "c.html"
    video.write_bytes(b"x")
    chat.write_text("<html></html>", encoding="utf-8")
    job_file = tmp_path / "jobs" / "j.yaml"
    job_file.parent.mkdir(parents=True)
    job_file.write_text(
        "\n".join(
            [
                "video: ../v.mp4",
                "chat: ../c.html",
                "output: out/chat.mp4",
                "mode: preview",
                "preview-clip: 12",
                "render_original: true",
                "layout_preset: compact",
            ]
        ),
        encoding="utf-8",
    )
    data = job_mod.load_job_file(job_file)
    assert Path(data["video"]).resolve() == video.resolve()
    assert Path(data["chat_html"]).resolve() == chat.resolve()
    assert Path(data["output"]).is_absolute()
    assert Path(data["output"]).name == "chat.mp4"
    assert data["mode"] == "preview"
    assert data["preview_clip"] == 12
    assert data["render_original"] is True
    assert data["layout_preset"] == "compact"  # short name kept


def test_apply_job_cli_wins(job_mod):
    args = SimpleNamespace(
        video=None,
        chat_html=None,
        output=None,
        mode="auto",
        preview_clip=None,
        render_original=False,
        overlay_codec="vp9",
    )
    job = {
        "video": "/a.mp4",
        "chat_html": "/a.html",
        "output": "/out.mp4",
        "mode": "preview",
        "preview_clip": 10,
        "render_original": True,
        "overlay_codec": "png",
    }
    defaults = {
        "video": None,
        "chat_html": None,
        "output": None,
        "mode": "auto",
        "preview_clip": None,
        "render_original": False,
        "overlay_codec": "vp9",
    }
    # CLI already set output and mode
    args.output = "/cli_out.mp4"
    args.mode = "full"
    applied = job_mod.apply_job_to_namespace(args, job, cli_defaults=defaults)
    assert args.output == "/cli_out.mp4"
    assert args.mode == "full"
    assert args.video == "/a.mp4"
    assert args.render_original is True
    assert "output" not in applied
    assert "mode" not in applied
    assert "video" in applied


def test_apply_job_fills_force_export_and_strict_import(job_mod):
    """New pipeline bools must be job-fillable (aliases + BOOL_FIELDS)."""
    import render_cn_chat as pipe

    args = SimpleNamespace(
        force_export=False,
        strict_import=False,
        mode="auto",
    )
    job = {"force_export": True, "strict_import": True, "mode": "preview"}
    applied = job_mod.apply_job_to_namespace(
        args, job, cli_defaults=pipe.PIPELINE_CLI_DEFAULTS
    )
    assert args.force_export is True
    assert args.strict_import is True
    assert "force_export" in applied and "strict_import" in applied


def test_load_job_nested_job_key(tmp_path: Path, job_mod):
    p = tmp_path / "job.yaml"
    p.write_text("job:\n  video: x.mp4\n  chat_html: y.html\n  mode: translate\n", encoding="utf-8")
    data = job_mod.load_job_file(p)
    assert data["mode"] == "translate"
    assert Path(data["video"]).name == "x.mp4"


def test_load_job_rejects_bad_mode(tmp_path: Path, job_mod):
    p = tmp_path / "bad.yaml"
    p.write_text("mode: banana\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mode"):
        job_mod.load_job_file(p)


def test_list_resolve_write_and_last_job(tmp_path: Path, job_mod):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    fields = {
        "video": str(tmp_path / "a.mp4"),
        "chat_html": str(tmp_path / "a.html"),
        "mode": "preview",
        "render_original": True,
        "preview_clip": 10,
        "layout_preset": "compact",
    }
    path = job_mod.write_job_file(jobs / "demo.yaml", fields, title="demo")
    text = path.read_text(encoding="utf-8")
    assert "源视频路径" in text or "video:" in text
    assert "render_original:" in text
    assert "逐项" in text or "注释" in text or "CLI" in text

    listed = job_mod.list_job_files(jobs)
    assert path in listed or any(p.name == "demo.yaml" for p in listed)

    resolved = job_mod.resolve_job_arg("demo", jobs)
    assert resolved == path.resolve()

    job_mod.save_last_job(path, jobs)
    assert job_mod.last_job_path(jobs) == path.resolve()

    summary = job_mod.summarize_job(path)
    assert "demo" in summary
    # Human-readable Chinese summary (or legacy English mode token)
    assert ("预览" in summary) or ("preview" in summary.lower())


def test_render_job_yaml_comments_every_set_field(job_mod):
    text = job_mod.render_job_yaml(
        {
            "video": "v.mp4",
            "chat_html": "c.html",
            "mode": "full",
            "offset": 12.5,
            "encoder": "x264",
        },
        title="t",
        pin_paths=False,
    )
    assert "源视频" in text or "video" in text
    # paths default to commented (reusable job)
    assert "mode: full" in text or "mode:" in text
    assert "12.5" in text
    assert "encoder: x264" in text
    # video not active unless pin_paths
    assert "\nvideo: " not in text or text.count("# video:") >= 1 or "# video:" in text


def test_render_job_yaml_pin_paths_writes_active_video(job_mod):
    text = job_mod.render_job_yaml(
        {"video": "v.mp4", "chat_html": "c.html", "mode": "preview"},
        pin_paths=True,
    )
    assert "video: v.mp4" in text or 'video: "v.mp4"' in text
    assert "chat_html:" in text


def test_load_job_known_keys_no_unknown_warning(tmp_path: Path, job_mod, capsys):
    """Known fields (incl. aliases) load silently — no unknown-key WARN."""
    p = tmp_path / "known.yaml"
    p.write_text(
        "\n".join(
            [
                "video: v.mp4",
                "chat: c.html",  # alias → chat_html
                "preview-clip: 10",  # kebab alias
                "render_original: true",
                "mode: preview",
                "force-export: true",
                "strict_import: yes",
            ]
        ),
        encoding="utf-8",
    )
    data = job_mod.load_job_file(p)
    assert data["preview_clip"] == 10
    assert data["chat_html"]
    assert data["render_original"] is True
    assert data["force_export"] is True
    assert data["strict_import"] is True
    captured = capsys.readouterr()
    assert "未识别" not in captured.out
    assert "unknown keys" not in captured.out.lower()
    assert "WARN" not in captured.out


def test_load_job_unknown_key_warns_but_loads(tmp_path: Path, job_mod, capsys):
    """Unknown keys emit a bilingual warning and are not applied; load still succeeds."""
    p = tmp_path / "weird.yaml"
    p.write_text(
        "\n".join(
            [
                "video: v.mp4",
                "chat_html: c.html",
                "mode: preview",
                "typo_mode: full",
                "not_a_real_flag: true",
            ]
        ),
        encoding="utf-8",
    )
    data = job_mod.load_job_file(p)
    assert data["mode"] == "preview"
    assert "typo_mode" not in data
    assert "not_a_real_flag" not in data
    out = capsys.readouterr().out
    assert "未识别" in out or "WARN" in out
    assert "typo_mode" in out
    assert "not_a_real_flag" in out
    assert "typo" in out.lower() or "拼写" in out


def test_load_job_aliases_still_work_with_warning_path(tmp_path: Path, job_mod, capsys):
    """Aliases normalize; unknown siblings do not break alias application."""
    p = tmp_path / "alias.yaml"
    p.write_text(
        "\n".join(
            [
                "job:",
                "  chat-html: nested.html",
                "  work-dir: ./wd",
                "  layout-preset: compact",
                "  mystery_knob: 1",
                "orphan_top: ignored",
            ]
        ),
        encoding="utf-8",
    )
    data = job_mod.load_job_file(p)
    assert Path(data["chat_html"]).name == "nested.html"
    assert data["layout_preset"] == "compact"
    assert "workdir" in data
    assert "mystery_knob" not in data
    assert "orphan_top" not in data
    out = capsys.readouterr().out
    assert "mystery_knob" in out
    assert "orphan_top" in out
    # Structural wrapper key itself must not be listed as unknown
    assert "ignored) @ " in out or "unknown keys ignored" in out
    # Warning lists field names after the colon on the first line; "job" is not among them.
    warn_line = next(line for line in out.splitlines() if "unknown keys" in line or "未识别" in line)
    listed = warn_line.split(":", 2)[-1]
    assert "mystery_knob" in listed
    assert "orphan_top" in listed
    assert " job" not in listed and not listed.strip().startswith("job")

def test_load_job_public_resources_fall_back_to_installed_share(
    tmp_path: Path, job_mod, monkeypatch
):
    import common_utils

    share = tmp_path / "prefix" / "share" / "twitch-chat-translator-overlay"
    profile = share / "profiles" / "installed_profile.yaml"
    rules = share / "configs" / "installed_rules.yaml"
    profile.parent.mkdir(parents=True)
    rules.parent.mkdir(parents=True)
    profile.write_text("label: installed\n", encoding="utf-8")
    rules.write_text("normalizations: []\n", encoding="utf-8")

    job = tmp_path / "project" / "jobs" / "installed.yaml"
    job.parent.mkdir(parents=True)
    job.write_text(
        "profile: profiles/installed_profile.yaml\n"
        "rules: configs/installed_rules.yaml\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(common_utils, "source_checkout_root", lambda _path: None)
    monkeypatch.setattr(common_utils, "distribution_share_dirs", lambda: [share])

    loaded = job_mod.load_job_file(job)

    assert loaded["profile"] == "profiles/installed_profile.yaml"
    assert loaded["rules"] == "configs/installed_rules.yaml"
    assert common_utils.resolve_public_resource(
        loaded["profile"], subdir="profiles"
    ) == profile.resolve()
    assert common_utils.resolve_public_resource(
        loaded["rules"], subdir="configs"
    ) == rules.resolve()


def test_load_job_prefers_existing_job_local_public_resources(tmp_path: Path, job_mod):
    job_dir = tmp_path / "jobs"
    profile = job_dir / "profiles" / "custom.yaml"
    rules = job_dir / "configs" / "custom.yaml"
    profile.parent.mkdir(parents=True)
    rules.parent.mkdir(parents=True)
    profile.write_text("label: local\n", encoding="utf-8")
    rules.write_text("normalizations: []\n", encoding="utf-8")
    job = job_dir / "local.yaml"
    job.write_text(
        "profile: profiles/custom.yaml\nrules: configs/custom.yaml\n",
        encoding="utf-8",
    )

    loaded = job_mod.load_job_file(job)

    assert Path(loaded["profile"]) == profile.resolve()
    assert Path(loaded["rules"]) == rules.resolve()


def test_generated_job_rules_comment_matches_post_translation_behavior(job_mod):
    text = job_mod.render_job_yaml({"rules": "configs/rules.example.yaml"})
    assert "翻译后" in text
    assert "翻译前规则" not in text


def test_load_job_malformed_yaml_raises_value_error(tmp_path: Path, job_mod):
    """yaml.YAMLError must surface as ValueError (callers only catch OSError/ValueError)."""
    p = tmp_path / "broken.yaml"
    p.write_text("a: [unclosed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="语法错误"):
        job_mod.load_job_file(p)


def test_write_load_roundtrip_newline_and_dash_prefix(tmp_path: Path, job_mod):
    """_yaml_quote must escape newlines / "- " prefixes so the file round-trips.

    Regression: `context: - note` was written bare and yaml.safe_load then
    raised ScannerError (block sequence instead of a scalar).
    """
    fields = {
        "mode": "translate",
        "context": "第一行\n第二行",
        "target_language": "- note",
    }
    path = job_mod.write_job_file(tmp_path / "roundtrip.yaml", fields, title="rt")
    # Atomic write: no tmp leftovers in the job dir.
    assert not list(tmp_path.glob("*.tmp"))
    text = path.read_text(encoding="utf-8")
    assert '"- note"' in text
    assert "\\n" in text
    loaded = job_mod.load_job_file(path)
    assert loaded["context"] == "第一行\n第二行"
    assert loaded["target_language"] == "- note"


def test_yaml_quote_plain_values_unchanged(job_mod):
    """Existing plain path/number/bool quoting must stay byte-identical."""
    assert job_mod._yaml_quote("path/to/video.mp4") == "path/to/video.mp4"
    assert job_mod._yaml_quote("C:\\a b\\c.mp4") == '"C:\\\\a b\\\\c.mp4"'
    assert job_mod._yaml_quote(10) == "10"
    assert job_mod._yaml_quote(12.5) == "12.5"
    assert job_mod._yaml_quote(True) == "true"
    assert job_mod._yaml_quote("") == '""'
    # Newlines and "- " prefixes must be quoted with escapes.
    assert job_mod._yaml_quote("a\nb") == '"a\\nb"'
    assert job_mod._yaml_quote("- note") == '"- note"'
    assert job_mod._yaml_quote("-note") == "-note"  # no space: plain scalar stays


def test_load_job_rejects_wrong_numeric_types(tmp_path: Path, job_mod):
    """D-#7: fps list / crf string must fail at load with the field name,
    not deep inside a burn subprocess argparse."""
    p_fps = tmp_path / "bad_fps.yaml"
    p_fps.write_text("fps:\n  - 1\n  - 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="fps"):
        job_mod.load_job_file(p_fps)

    p_crf = tmp_path / "bad_crf.yaml"
    p_crf.write_text('crf: "high"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="crf"):
        job_mod.load_job_file(p_crf)

    # bool is not a legal integer value either.
    p_bool = tmp_path / "bad_bool.yaml"
    p_bool.write_text("max_visible: true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="max_visible"):
        job_mod.load_job_file(p_bool)

    # Non-numeric offset string.
    p_offset = tmp_path / "bad_offset.yaml"
    p_offset.write_text("offset: later\n", encoding="utf-8")
    with pytest.raises(ValueError, match="offset"):
        job_mod.load_job_file(p_offset)


def test_load_job_numeric_values_passthrough(tmp_path: Path, job_mod):
    """D-#7: legal int/float values keep their types and values unchanged."""
    p = tmp_path / "nums.yaml"
    p.write_text(
        "\n".join(
            [
                "crf: 18",
                "workers: 4",
                "offset: 12.5",
                "preview_clip: 12",
                "msg_lifetime: 14",
                "output_fps: 59.94",
            ]
        ),
        encoding="utf-8",
    )
    data = job_mod.load_job_file(p)
    assert data["crf"] == 18 and isinstance(data["crf"], int)
    assert data["workers"] == 4
    assert data["offset"] == 12.5
    # int for a float field stays an int (no silent rewrite).
    assert data["preview_clip"] == 12 and isinstance(data["preview_clip"], int)
    assert data["msg_lifetime"] == 14
    assert data["output_fps"] == 59.94


def test_load_job_numeric_string_and_rational_fps(tmp_path: Path, job_mod):
    """Numeric strings coerce like argparse type= would; output_fps keeps
    exact rational strings ("30000/1001") for the burn CLI parser."""
    p = tmp_path / "strs.yaml"
    p.write_text(
        "\n".join(
            [
                'crf: "18"',
                'offset: "7264"',
                "output_fps: 30000/1001",
            ]
        ),
        encoding="utf-8",
    )
    data = job_mod.load_job_file(p)
    assert data["crf"] == 18 and isinstance(data["crf"], int)
    assert data["offset"] == 7264.0
    assert data["output_fps"] == "30000/1001"

    # Broken rational fps must still fail at load time.
    p_bad = tmp_path / "bad_rational.yaml"
    p_bad.write_text("output_fps: 30000/zero\n", encoding="utf-8")
    with pytest.raises(ValueError, match="output_fps"):
        job_mod.load_job_file(p_bad)


def test_load_job_integral_float_int_field_coerces_fractional_rejected(
    tmp_path: Path, job_mod
):
    """Integral float for an int field coerces (18.0 -> 18); fractional raises.

    argparse only ever sees strings, so a float value can only arrive from
    YAML: 18.0 carries no extra information and is accepted, 18.5 and the
    decimal-point string "18.0" (which int() would reject) fail at load time.
    """
    p_ok = tmp_path / "float_int.yaml"
    p_ok.write_text("crf: 18.0\n", encoding="utf-8")
    data = job_mod.load_job_file(p_ok)
    assert data["crf"] == 18 and isinstance(data["crf"], int)

    p_frac = tmp_path / "frac_int.yaml"
    p_frac.write_text("crf: 18.5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="crf"):
        job_mod.load_job_file(p_frac)

    p_str = tmp_path / "str_int.yaml"
    p_str.write_text('crf: "18.0"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="crf"):
        job_mod.load_job_file(p_str)
