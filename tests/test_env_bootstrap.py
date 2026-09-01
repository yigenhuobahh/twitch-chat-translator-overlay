#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1–P4 env bootstrap: readiness report and tools/ffmpeg PATH inject."""

from __future__ import annotations

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_collect_readiness_has_core_keys():
    from env_bootstrap import collect_readiness, readiness_levels

    items = collect_readiness()
    keys = {i.key for i in items}
    assert "python" in keys
    assert "ffmpeg" in keys
    assert "ffprobe" in keys
    assert "font" in keys
    assert "api" in keys
    min_ok, full_ok = readiness_levels(items)
    assert isinstance(min_ok, bool) and isinstance(full_ok, bool)


def test_print_readiness_report_runs(capsys):
    from env_bootstrap import print_readiness_report

    min_ok, full_ok = print_readiness_report()
    out = capsys.readouterr().out
    assert "就绪清单" in out or "Readiness" in out
    assert "最小可用" in out
    assert "完整可用" in out
    assert isinstance(min_ok, bool)


def test_prepend_tools_ffmpeg_to_path(tmp_path, monkeypatch):
    from env_bootstrap import prepend_tools_ffmpeg_to_path

    # Fake tools/ffmpeg/bin with dummy executables
    root = tmp_path / "repo"
    bin_dir = root / "tools" / "ffmpeg" / "bin"
    bin_dir.mkdir(parents=True)
    if os.name == "nt":
        (bin_dir / "ffmpeg.exe").write_bytes(b"")
        (bin_dir / "ffprobe.exe").write_bytes(b"")
    else:
        (bin_dir / "ffmpeg").write_bytes(b"")
        (bin_dir / "ffprobe").write_bytes(b"")
        os.chmod(bin_dir / "ffmpeg", 0o755)
        os.chmod(bin_dir / "ffprobe", 0o755)

    monkeypatch.setenv("PATH", "")
    found = prepend_tools_ffmpeg_to_path(root)
    assert found is not None
    assert Path(found) == bin_dir.resolve()
    assert str(bin_dir.resolve()) in os.environ.get("PATH", "")
    from common_utils import safe_which

    assert safe_which("ffmpeg") == str((bin_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")).resolve())


def test_doctor_help_lists_offer_fix():
    import subprocess

    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_cn_chat.py"), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONPATH": str(SCRIPTS), "PYTHONUTF8": "1"},
    )
    assert r.returncode == 0
    text = (r.stdout or "") + (r.stderr or "")
    assert "--offer-fix" in text
    assert "--fix-yes" in text


def test_doctor_prints_readiness(capsys, monkeypatch):
    """doctor() should include readiness section."""
    from types import SimpleNamespace

    import env_bootstrap as eb
    import render_cn_chat as pipe

    # Non-interactive: do not block on install prompt
    monkeypatch.setattr(eb, "can_prompt_interactive", lambda: False)
    monkeypatch.setattr(eb, "maybe_prompt_offer_fixes", lambda **k: False)

    args = SimpleNamespace(
        offer_fix=False,
        yes=False,
        fix_yes=False,
        font_path="auto",
        font_bold_path="auto",
        video=None,
        chat_html=None,
        offset=None,
    )
    code = pipe.doctor(args)
    out = capsys.readouterr().out
    assert "就绪清单" in out or "Readiness" in out
    assert "诊断结果" in out
    assert code in (0, 1)


def test_maybe_prompt_offer_fixes_default_yes_calls_offer(monkeypatch):
    import env_bootstrap as eb

    monkeypatch.setattr(eb, "can_prompt_interactive", lambda: True)
    monkeypatch.setattr(eb, "readiness_levels", lambda items: (False, False))
    monkeypatch.setattr(
        eb,
        "collect_readiness",
        lambda **k: [
            eb.CheckItem("ffmpeg", "ffmpeg", False, True, detail="未找到"),
            eb.CheckItem("ffprobe", "ffprobe", False, True, detail="未找到"),
        ],
    )
    called = {"n": 0}

    def fake_offer(**kwargs):
        called["n"] += 1

    monkeypatch.setattr(eb, "offer_fixes", fake_offer)
    # User presses Enter → default True
    monkeypatch.setattr(eb, "_prompt_yes", lambda *a, **k: True)
    assert eb.maybe_prompt_offer_fixes(already_offered=False) is True
    assert called["n"] == 1


def test_probe_translate_api_missing_env(monkeypatch):
    import env_bootstrap as eb

    for k in (
        "OPENAI_COMPAT_BASE_URL",
        "OPENAI_COMPAT_API_KEY",
        "OPENAI_COMPAT_MODEL",
        "AGNES_BASE_URL",
        "AGNES_API_KEY",
        "AGNES_MODEL",
    ):
        monkeypatch.delenv(k, raising=False)
    ok, msg = eb.probe_translate_api()
    assert ok is False
    assert "未配置" in msg


def test_ensure_translate_api_fallback_manual(monkeypatch, tmp_path):
    """Missing API → interactive C → manual tables, no hard fail."""
    import render_cn_chat as pipe

    monkeypatch.setattr(pipe, "probe_translate_api", lambda **k: (False, "未配置: OPENAI_COMPAT_API_KEY"))
    monkeypatch.setattr(pipe, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "c")
    exported = {"n": 0}

    def fake_export(json_path, out_path):
        exported["n"] += 1
        Path(out_path).write_text("x", encoding="utf-8")

    monkeypatch.setattr(pipe, "export_review_tsv", fake_export)
    monkeypatch.setattr(pipe, "export_review_xlsx", fake_export)

    video = tmp_path / "v.mp4"
    html = tmp_path / "c.html"
    tj = tmp_path / "t.json"
    video.write_bytes(b"0")
    html.write_text("<html></html>", encoding="utf-8")
    tj.write_text('{"messages":[]}', encoding="utf-8")
    mode = pipe.ensure_translate_api_or_fallback(
        video=video,
        chat_html=html,
        trans_json=tj,
        review_tsv=tmp_path / "r.tsv",
        review_xlsx=tmp_path / "r.xlsx",
        workdir=None,
        final_output=tmp_path / "out.mp4",
        yes=False,
    )
    assert mode == "manual"
    assert exported["n"] == 2


def test_ensure_translate_api_fallback_marks_manual_required_for_tui_result(monkeypatch, tmp_path):
    import render_cn_chat as pipe

    monkeypatch.setattr(pipe, "probe_translate_api", lambda **_kwargs: (False, "offline"))
    monkeypatch.setattr(pipe, "export_review_tsv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipe, "export_review_xlsx", lambda *_args, **_kwargs: None)
    translation = tmp_path / "translation.json"
    translation.write_text('{"messages": []}', encoding="utf-8")

    runner = pipe.PipelineRunner(mode="full")
    with pipe.activate_runner(runner):
        mode = pipe.ensure_translate_api_or_fallback(
            video=tmp_path / "video.mp4",
            chat_html=tmp_path / "chat.html",
            trans_json=translation,
            review_tsv=tmp_path / "review.tsv",
            review_xlsx=tmp_path / "review.xlsx",
            workdir=None,
            final_output=tmp_path / "output.mp4",
            yes=True,
        )

    assert mode == "manual"
    assert runner.terminal_state == "manual_required"


def test_ensure_translate_api_retry_then_ok(monkeypatch, tmp_path):
    import render_cn_chat as pipe

    calls = {"n": 0}

    def probe(**k):
        calls["n"] += 1
        if calls["n"] < 2:
            return False, "timeout"
        return True, "ok"

    monkeypatch.setattr(pipe, "probe_translate_api", probe)
    monkeypatch.setattr(pipe, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "r")
    monkeypatch.setattr(pipe, "load_dotenv_if_present", lambda: None)

    mode = pipe.ensure_translate_api_or_fallback(
        video=tmp_path / "v.mp4",
        chat_html=tmp_path / "c.html",
        trans_json=tmp_path / "t.json",
        review_tsv=tmp_path / "r.tsv",
        review_xlsx=tmp_path / "r.xlsx",
        workdir=None,
        final_output=tmp_path / "out.mp4",
        yes=False,
    )
    assert mode == "api"
    assert calls["n"] >= 2


def _mid_fail_paths(tmp_path: Path):
    video = tmp_path / "v.mp4"
    html = tmp_path / "c.html"
    tj = tmp_path / "t.json"
    video.write_bytes(b"0")
    html.write_text("<html></html>", encoding="utf-8")
    tj.write_text('{"messages":[]}', encoding="utf-8")
    return {
        "video": video,
        "chat_html": html,
        "trans_json": tj,
        "review_tsv": tmp_path / "r.tsv",
        "review_xlsx": tmp_path / "r.xlsx",
        "workdir": None,
        "final_output": tmp_path / "out.mp4",
        "translation_context": "ctx",
        "target_language": "zh",
        "batch_size": 20,
        "workers": 2,
        "translator": tmp_path / "fake_translator.py",
    }


def test_handle_translate_run_failure_manual_interactive(monkeypatch, tmp_path):
    """Mid-run API failure + C → export hand tables, stop (manual)."""
    import render_cn_chat as pipe

    monkeypatch.setattr(pipe, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "c")
    exported = {"n": 0}

    def fake_export(json_path, out_path):
        exported["n"] += 1
        Path(out_path).write_text("x", encoding="utf-8")

    monkeypatch.setattr(pipe, "export_review_tsv", fake_export)
    monkeypatch.setattr(pipe, "export_review_xlsx", fake_export)

    mode = pipe.handle_translate_run_failure(
        pipe.PipelineError("translator exit 1"),
        yes=False,
        **_mid_fail_paths(tmp_path),
    )
    assert mode == "manual"
    assert exported["n"] == 2


def test_handle_translate_run_failure_retry_then_api(monkeypatch, tmp_path):
    """Mid-run failure + R → retry run() once, continue as api."""
    import render_cn_chat as pipe

    monkeypatch.setattr(pipe, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "r")
    runs = {"n": 0}

    def fake_run(cmd, **kwargs):
        runs["n"] += 1

    monkeypatch.setattr(pipe, "run", fake_run)

    mode = pipe.handle_translate_run_failure(
        pipe.PipelineError("timeout"),
        yes=False,
        **_mid_fail_paths(tmp_path),
    )
    assert mode == "api"
    assert runs["n"] == 1


def test_handle_translate_run_failure_quit_reraises(monkeypatch, tmp_path):
    import pytest

    import render_cn_chat as pipe

    monkeypatch.setattr(pipe, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "q")
    err = pipe.PipelineError("boom")
    with pytest.raises(pipe.PipelineError, match="boom"):
        pipe.handle_translate_run_failure(
            err,
            yes=False,
            **_mid_fail_paths(tmp_path),
        )


def test_handle_translate_run_failure_yes_noninteractive_manual(monkeypatch, tmp_path):
    """--yes / non-interactive mid-fail → manual tables without prompt."""
    import render_cn_chat as pipe

    monkeypatch.setattr(pipe, "_stdin_is_interactive", lambda: False)
    exported = {"n": 0}

    def fake_export(json_path, out_path):
        exported["n"] += 1
        Path(out_path).write_text("x", encoding="utf-8")

    monkeypatch.setattr(pipe, "export_review_tsv", fake_export)
    monkeypatch.setattr(pipe, "export_review_xlsx", fake_export)

    mode = pipe.handle_translate_run_failure(
        pipe.PipelineError("down"),
        yes=True,
        **_mid_fail_paths(tmp_path),
    )
    assert mode == "manual"
    assert exported["n"] == 2


def test_review_issue_map_warns_on_bad_json(tmp_path, capsys):
    import render_cn_chat as pipe

    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    out = pipe._review_issue_map(bad)
    assert out == {}
    captured = capsys.readouterr().out
    assert "lint 跳过" in captured or "无法解析" in captured

def test_installed_ffmpeg_lookup_ignores_untrusted_cwd_tools(tmp_path, monkeypatch):
    import env_bootstrap as eb

    cwd = tmp_path / "untrusted media"
    trusted = tmp_path / "trusted app data"
    fake_bin = cwd / "tools" / "ffmpeg" / "bin"
    fake_bin.mkdir(parents=True)
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    probe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    (fake_bin / exe).write_bytes(b"not executable")
    (fake_bin / probe).write_bytes(b"not executable")
    (cwd / exe).write_bytes(b"untrusted cwd executable")
    (cwd / probe).write_bytes(b"untrusted cwd executable")

    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(eb, "trusted_tools_root", lambda _module: trusted)

    assert eb.prepend_tools_ffmpeg_to_path() is None
    assert eb.safe_which("ffmpeg") is None
    assert eb.safe_which("ffprobe") is None
    assert os.environ["PATH"] == ""


# ---------------------------------------------------------------------------
# C-O7: one sunk .env API-triple reader shared by all three call sites
# ---------------------------------------------------------------------------

_API_ENV_KEYS = (
    "OPENAI_COMPAT_BASE_URL",
    "OPENAI_COMPAT_API_KEY",
    "OPENAI_COMPAT_MODEL",
    "AGNES_BASE_URL",
    "AGNES_API_KEY",
    "AGNES_MODEL",
)


def test_get_translate_api_config_delegates_to_common_utils(monkeypatch):
    """env_bootstrap.get_translate_api_config 与 common_utils 下沉函数逐项等价。"""
    import common_utils as cu
    import env_bootstrap as eb

    for key in _API_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    # 未配置:两项实现一致地返回 None 三元组。
    assert eb.get_translate_api_config() == {
        "base_url": None,
        "api_key": None,
        "model": None,
    }
    assert cu.translate_api_env_config() == eb.get_translate_api_config()

    # OPENAI_COMPAT_* 优先。
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://compat.invalid/v1")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "compat-key")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "compat-model")
    cfg = eb.get_translate_api_config()
    assert cfg == {
        "base_url": "https://compat.invalid/v1",
        "api_key": "compat-key",
        "model": "compat-model",
    }

    # AGNES_* 逐项回退(缺哪个补哪个)。
    monkeypatch.delenv("OPENAI_COMPAT_MODEL", raising=False)
    monkeypatch.setenv("AGNES_BASE_URL", "https://agnes.invalid/v1")
    monkeypatch.setenv("AGNES_API_KEY", "agnes-key")
    monkeypatch.setenv("AGNES_MODEL", "agnes-model")
    cfg = eb.get_translate_api_config()
    assert cfg == {
        "base_url": "https://compat.invalid/v1",
        "api_key": "compat-key",
        "model": "agnes-model",
    }
    assert cfg == cu.translate_api_env_config()


def test_collect_readiness_api_item_uses_shared_fallback(monkeypatch):
    """collect_readiness 的 api 检查项复用同一回退实现(AGNES_* 也算已配置)。"""
    import env_bootstrap as eb

    for key in _API_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGNES_BASE_URL", "https://agnes.invalid/v1")
    monkeypatch.setenv("AGNES_API_KEY", "agnes-key")
    monkeypatch.setenv("AGNES_MODEL", "agnes-model")
    assert next(i for i in eb.collect_readiness() if i.key == "api").ok is True

    monkeypatch.delenv("AGNES_API_KEY", raising=False)
    assert next(i for i in eb.collect_readiness() if i.key == "api").ok is False


def test_translate_module_top_level_fallback_uses_shared_reader(monkeypatch):
    """C-O7:translate 模块顶部的回退读取与 common_utils 下沉函数等价。"""
    import common_utils as cu
    from helpers import load_module

    for key in _API_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGNES_BASE_URL", "https://agnes.invalid/v1")
    monkeypatch.setenv("AGNES_API_KEY", "agnes-key")
    monkeypatch.setenv("AGNES_MODEL", "agnes-model")

    tr = load_module("translate_chat_openai", "translate_chat_openai.py")
    cfg = cu.translate_api_env_config()

    assert tr.BASE_URL == cfg["base_url"] == "https://agnes.invalid/v1"
    assert tr.API_KEY == cfg["api_key"] == "agnes-key"
    assert tr.MODEL == cfg["model"] == "agnes-model"


def test_translate_main_rereads_shared_api_config(monkeypatch, tmp_path):
    """C-O7:main() 运行时重读走同一共享函数(导入后出现的 AGNES_* 可达)。"""
    import json
    from types import SimpleNamespace

    import common_utils as cu
    from helpers import load_module

    for key in _API_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    tr = load_module("translate_chat_openai", "translate_chat_openai.py")
    # 模块导入时 env 为空(BASE_URL 等为 None);配置随后才出现,
    # main() 必须经共享函数重读 env 拿到 AGNES_* 回退。
    monkeypatch.setenv("AGNES_BASE_URL", "https://agnes.invalid/v1")
    monkeypatch.setenv("AGNES_API_KEY", "agnes-key")
    monkeypatch.setenv("AGNES_MODEL", "agnes-model")

    json_path = tmp_path / "cfg.json"
    json_path.write_text(
        json.dumps(
            {"messages": [{"index": 0, "author": "a", "original": "hi", "translation": ""}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    record: dict = {}

    class Completions:
        def create(self, **kwargs):
            record["create_kwargs"] = kwargs
            payload = json.dumps(
                {"translations": [{"index": 0, "translation": "译"}]},
                ensure_ascii=False,
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=payload))]
            )

    class Client:
        def __init__(self, **kwargs):
            record["client_kwargs"] = kwargs
            self.chat = SimpleNamespace(completions=Completions())

    monkeypatch.setattr(tr, "OpenAI", Client)
    monkeypatch.setattr(
        tr.sys, "argv", ["translate_chat_openai.py", str(json_path), "--workers", "1"]
    )

    tr.main()

    cfg = cu.translate_api_env_config()
    assert (
        record["client_kwargs"]["base_url"]
        == cfg["base_url"]
        == "https://agnes.invalid/v1"
    )
    assert record["create_kwargs"]["model"] == cfg["model"] == "agnes-model"
