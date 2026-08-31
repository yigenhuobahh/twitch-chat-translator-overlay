#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end translate pipeline tests.

1. In-process --workers 2 run where one batch succeeds while the other hits a
   retryable server error: progress.failed, the final JSON translation fields,
   and the exit code must all agree.
2. Subprocess E2E against a local stub OpenAI-compatible HTTP API
   (127.0.0.1 only): export -> translate -> import, proving env vars and CLI
   flags really travel into the child process.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


@pytest.fixture(scope="module")
def tr():
    from helpers import load_module

    return load_module("translate_chat_openai_e2e", "translate_chat_openai.py")


def test_workers_two_partial_failure_keeps_progress_json_exit_consistent(
    tr,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """--workers 2: one batch succeeds while the other gets a retryable 500.

    The three failure views must stay consistent:
    progress.failed == JSON rows left at original text == rows behind exit 1.
    """
    messages = [
        {"index": 0, "author": "alice", "original": "hello world", "translation": ""},
        {"index": 1, "author": "bob", "original": "goodbye world", "translation": ""},
    ]
    json_path = tmp_path / "translate.json"
    json_path.write_text(
        json.dumps({"messages": messages}, ensure_ascii=False),
        encoding="utf-8",
    )

    class ServerError(Exception):
        pass

    calls = {"ok": 0, "server_error": 0}
    lock = threading.Lock()
    failing_batch_started = threading.Event()
    observed_concurrency = {}

    class Completions:
        def create(self, **kwargs):
            prompt = kwargs["messages"][0]["content"]
            with lock:
                if "goodbye world" in prompt:
                    calls["server_error"] += 1
                    failing_batch_started.set()
                    error = ServerError("internal server error")
                    error.status_code = 500  # retryable SERVER kind
                    raise error
                calls["ok"] += 1
            # With --workers 2 the success batch should overlap the failing one;
            # a serialized pool would block here until the 10s timeout.
            observed_concurrency["overlapped"] = failing_batch_started.wait(timeout=10.0)
            payload = json.dumps(
                {"translations": [{"index": 0, "translation": "你好世界"}]},
                ensure_ascii=False,
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=payload))]
            )

    class Client:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=Completions())

    monkeypatch.setattr(tr, "OpenAI", Client)
    monkeypatch.setattr(tr, "BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setattr(tr, "API_KEY", "stub-key")
    monkeypatch.setattr(tr, "MODEL", "stub-model")
    # Keep retry backoff instantaneous; the retry path itself must still run.
    monkeypatch.setattr(tr, "backoff_seconds", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(
        tr.sys,
        "argv",
        [
            "translate_chat_openai.py",
            str(json_path),
            "--workers",
            "2",
            "--batch-size",
            "1",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        tr.main()

    assert exc_info.value.code == 1
    assert observed_concurrency.get("overlapped") is True
    # Retryable server error: 3 attempts in the main pass + 3 in the final
    # missing-translation retry pass. The success batch is called exactly once.
    assert calls["server_error"] == 6
    assert calls["ok"] == 1

    updated = json.loads(json_path.read_text(encoding="utf-8"))
    progress = tr.load_progress(tr.progress_path_for(json_path))
    translations = [m["translation"] for m in updated["messages"]]
    assert translations == ["你好世界", "goodbye world"]
    assert progress["failed"] == [1]
    assert progress["translations"] == {"0": "你好世界"}
    # Rows written back with their original text are exactly the failed rows.
    fallback_rows = [
        m["index"] for m in updated["messages"] if m["translation"] == m["original"]
    ]
    assert fallback_rows == progress["failed"]


class _StubAPIHandler(BaseHTTPRequestHandler):
    """OpenAI-compatible /chat/completions stub for 127.0.0.1 testing only."""

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        record = self.server.record  # type: ignore[attr-defined]
        prompt = ""
        messages = body.get("messages") or []
        if messages and isinstance(messages[0], dict):
            prompt = str(messages[0].get("content", ""))
        rows = [
            {"index": int(m.group(1)), "translation": f"译{m.group(2).strip()}"}
            for m in re.finditer(r"^\[(\d+)\]\s+(.+)$", prompt, flags=re.MULTILINE)
        ]
        content = json.dumps({"translations": rows}, ensure_ascii=False)
        payload = json.dumps(
            {
                "id": "chatcmpl-stub-1",
                "object": "chat.completion",
                "created": 1,
                "model": body.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode("utf-8")
        with record["lock"]:
            record["requests"].append(
                {
                    "path": self.path,
                    "model": body.get("model"),
                    "auth": self.headers.get("Authorization"),
                    "prompt": prompt,
                }
            )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):  # silence request logging
        pass


@pytest.mark.smoke
def test_subprocess_translate_pipeline_against_stub_http_api(
    tmp_path: Path,
    make_test_video,
):
    """export -> translate (subprocess + stub API) -> import must round-trip.

    Validates that OPENAI_COMPAT_* env vars and CLI flags genuinely reach the
    child process, and that the burned output is produced without a live API.
    """
    video = make_test_video(duration=2.0, width=320, height=180, fps=10)
    html = Path(__file__).resolve().parent / "fixtures" / "twitchdownloader_chat.html"
    if not html.is_file():
        pytest.skip("fixture html missing")

    # --- Step 1: export via the burn CLI (video dir becomes out_base). ---
    export_json = tmp_path / "export.json"
    r = subprocess.run(
        [
            sys.executable, str(SCRIPTS_DIR / "twitch_chat_burn.py"),
            str(video), str(html),
            "--export-translation", str(export_json),
            "--offset", "0",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        timeout=120,
    )
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    exported = json.loads(export_json.read_text(encoding="utf-8"))
    assert len(exported["messages"]) == 3

    # --- Step 2: translate via subprocess against the local stub API. ---
    record = {"requests": [], "lock": threading.Lock()}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubAPIHandler)
    server.record = record  # type: ignore[attr-defined]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        port = server.server_address[1]
        # PARSE-O5: glossary-sized context must reach the API through
        # --context-file (argv would blow the Windows 32k-char limit).
        context_file = tmp_path / "translation_context.txt"
        context_marker = "游戏直播术语表标记E2E"
        context_file.write_text((context_marker + "\n") + ("词条说明。" * 5000), encoding="utf-8")
        child_env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "OPENAI_COMPAT_BASE_URL": f"http://127.0.0.1:{port}/v1",
            "OPENAI_COMPAT_API_KEY": "stub-key",
            "OPENAI_COMPAT_MODEL": "stub-model",
        }
        r = subprocess.run(
            [
                sys.executable, str(SCRIPTS_DIR / "translate_chat_openai.py"),
                str(export_json),
                "--context-file", str(context_file),
                "--max-batch-chars", "200000",
                "--workers", "2",
                "--batch-size", "1",
                "--target-language", "zh",
                "--request-timeout", "30",
                "--no-cache",
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=child_env, timeout=120,
        )
        assert r.returncode == 0, (r.stdout or "") + "\n" + (r.stderr or "")
        out = (r.stdout or "") + (r.stderr or "")
        # 2 of 3 rows are translatable (the pure-emote row is preserved).
        assert "更新 2/3" in out, out
    finally:
        server.shutdown()
        server.server_close()

    with record["lock"]:
        requests = list(record["requests"])
    # --batch-size 1 -> one request per translatable message; the pure-emote
    # row "[Hey]" is preserved locally and must never hit the API.
    assert len(requests) == 2, requests
    assert all(req["path"] == "/v1/chat/completions" for req in requests)
    # env propagation: model + api key came from OPENAI_COMPAT_*.
    assert all(req["model"] == "stub-model" for req in requests)
    assert all(req["auth"] == "Bearer stub-key" for req in requests)
    # flag propagation: --target-language zh is part of the prompt.
    assert all("zh" in req["prompt"] for req in requests)
    # context propagation via --context-file: glossary marker reaches the API prompt.
    assert all(context_marker in req["prompt"] for req in requests)

    translated = json.loads(export_json.read_text(encoding="utf-8"))
    rows = {m["index"]: m for m in translated["messages"]}
    assert rows[0]["translation"].startswith("译")
    assert rows[2]["translation"].startswith("译")
    assert rows[1]["translation"] == rows[1]["original"] == "[Hey]"

    # --- Step 3: import the translated JSON back through the burn CLI. ---
    out_dir = tmp_path / "final_render"
    out_dir.mkdir()
    r = subprocess.run(
        [
            sys.executable, str(SCRIPTS_DIR / "twitch_chat_burn.py"),
            str(video), str(html),
            "--import-translation", str(export_json),
            "--offset", "0",
            "--preview-clip", "2",
            "--fps", "10",
            "--x", "8", "--y", "8", "--w", "240", "--h", "130",
            "--out-dir", str(out_dir),
            "--job-dir", str(out_dir),
            "--keep-temp",
            "--overlay-codec", "png",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        timeout=180,
    )
    assert r.returncode == 0, (r.stdout or "") + "\n" + (r.stderr or "")
    final = out_dir / f"{video.stem}_chat.mp4"
    assert final.is_file(), (r.stdout or "") + (r.stderr or "")
