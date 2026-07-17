#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Behavior-level regressions for long-term runtime safety."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

_TRANSLATION_ENV_KEYS = (
    "OPENAI_COMPAT_BASE_URL",
    "OPENAI_COMPAT_MODEL",
    "OPENAI_COMPAT_API_KEY",
    "AGNES_BASE_URL",
    "AGNES_MODEL",
    "AGNES_API_KEY",
)


def _clear_translation_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _TRANSLATION_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_dotenv_does_not_mix_with_process_translation_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import common_utils as cu

    _clear_translation_env(monkeypatch)
    monkeypatch.delenv("_TWITCH_TRANSPARENT_TEST_MODE", raising=False)
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "process-only-key")
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "OPENAI_COMPAT_API_KEY=file-key",
                "OPENAI_COMPAT_BASE_URL=https://untrusted.invalid/v1",
                "OPENAI_COMPAT_MODEL=untrusted-model",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cu, "_DOTENV_LOADED_KEYS", set())

    cu.load_dotenv_if_present()

    assert os.environ["OPENAI_COMPAT_API_KEY"] == "process-only-key"
    assert "OPENAI_COMPAT_BASE_URL" not in os.environ
    assert "OPENAI_COMPAT_MODEL" not in os.environ
    assert set() == cu._DOTENV_LOADED_KEYS


def test_dotenv_still_loads_complete_config_when_process_has_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import common_utils as cu

    _clear_translation_env(monkeypatch)
    monkeypatch.delenv("_TWITCH_TRANSPARENT_TEST_MODE", raising=False)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "OPENAI_COMPAT_API_KEY=file-key",
                "OPENAI_COMPAT_BASE_URL=https://provider.invalid/v1",
                "OPENAI_COMPAT_MODEL=test-model",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cu, "_DOTENV_LOADED_KEYS", set())

    cu.load_dotenv_if_present()

    assert os.environ["OPENAI_COMPAT_API_KEY"] == "file-key"
    assert os.environ["OPENAI_COMPAT_BASE_URL"] == "https://provider.invalid/v1"
    assert os.environ["OPENAI_COMPAT_MODEL"] == "test-model"

    # load_dotenv_if_present writes os.environ directly, outside monkeypatch undo.
    for key in _TRANSLATION_ENV_KEYS:
        os.environ.pop(key, None)


@pytest.mark.parametrize(
    ("platform_name", "env_key", "relative_value", "fallback_parts"),
    [
        ("win32", "LOCALAPPDATA", "relative-local", ("AppData", "Local")),
        ("linux", "XDG_DATA_HOME", "relative-xdg", (".local", "share")),
    ],
)
def test_trusted_tools_root_rejects_relative_environment_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    env_key: str,
    relative_value: str,
    fallback_parts: tuple[str, ...],
):
    import common_utils as cu

    home = tmp_path / "home"
    fake_os = SimpleNamespace(
        name="nt" if platform_name == "win32" else "posix",
        environ=os.environ,
    )
    fake_sys = SimpleNamespace(platform=platform_name)
    monkeypatch.setattr(cu, "source_checkout_root", lambda _module: None)
    monkeypatch.setattr(cu, "os", fake_os)
    monkeypatch.setattr(cu, "sys", fake_sys)
    monkeypatch.setattr(cu.Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(cu, "_DOTENV_LOADED_KEYS", set())
    monkeypatch.setenv(env_key, relative_value)
    if platform_name == "win32":
        monkeypatch.setenv("APPDATA", "also-relative")

    actual = cu.trusted_tools_root(tmp_path / "installed" / "common_utils.py")

    expected = home.joinpath(*fallback_parts, "twitch-chat-translator-overlay").resolve()
    assert actual == expected
    assert not str(actual).startswith(str((tmp_path / relative_value).resolve()))


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_numeric_validators_reject_non_finite_values(value: float):
    import common_utils as cu

    with pytest.raises(ValueError, match="finite"):
        cu.validate_non_negative_float("offset", value)
    with pytest.raises(ValueError, match="finite"):
        cu.validate_positive_float("fps", value)
    with pytest.raises(argparse.ArgumentTypeError, match="finite"):
        cu.positive_float_arg(str(value))


def test_clean_scan_does_not_follow_directory_symlink(tmp_path: Path):
    import process_util as pu

    out_dir = tmp_path / "output"
    outside = tmp_path / "outside"
    out_dir.mkdir()
    outside.mkdir()
    artifact = outside / "stale.partial.mp4"
    artifact.write_bytes(b"keep")
    link = out_dir / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    count, freed = pu.clean_temp_artifacts(out_dir, scan_one_level=True)

    assert count == 0
    assert freed == 0
    assert artifact.read_bytes() == b"keep"


def test_windows_reparse_attribute_is_treated_as_indirection(
    monkeypatch: pytest.MonkeyPatch,
):
    import process_util as pu

    fake_stat = SimpleNamespace(
        st_mode=stat.S_IFDIR,
        st_file_attributes=pu._WINDOWS_REPARSE_POINT,
    )
    monkeypatch.setattr(pu.os, "lstat", lambda *_args, **_kwargs: fake_stat)

    assert pu._is_link_or_reparse_point("junction") is True


def test_live_pid_takes_precedence_over_old_metadata(monkeypatch: pytest.MonkeyPatch):
    import run_meta as rm

    monkeypatch.setattr(rm, "pid_is_alive", lambda _pid: True)
    assert rm.is_live_run_meta(
        {
            "status": "running",
            "pid": 123,
            "updated_at": "2000-01-01T00:00:00",
        },
        stale_after_sec=1,
        now=2_000_000_000,
    )


def test_translation_cache_identity_and_unique_temp_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import translation_support as support

    observed_temp_names: list[str] = []
    real_replace = support.os.replace

    def tracking_replace(src, dst):
        observed_temp_names.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(support.os, "replace", tracking_replace)
    cache = support.TranslationCache(tmp_path / "cache")
    identity = {
        "provider": "openai-compatible",
        "base_url": "https://one.invalid/v1",
        "prompt_version": "1",
    }

    assert cache.put("hello", "zh", "m", "ctx", "甲", **identity) is True
    assert cache.put("hello", "zh", "m", "ctx", "乙", **identity) is True
    assert len(observed_temp_names) == 2
    assert len(set(observed_temp_names)) == 2
    assert cache.get("hello", "zh", "m", "ctx", **identity) == "乙"
    assert (
        cache.get(
            "hello",
            "zh",
            "m",
            "ctx",
            provider="openai-compatible",
            base_url="https://two.invalid/v1",
            prompt_version="1",
        )
        is None
    )
    assert not list((tmp_path / "cache").glob("*.tmp"))


def test_cache_write_failure_does_not_discard_model_result():
    import translate_chat_openai as tr

    class BrokenCache:
        def get(self, *_args, **_kwargs):
            return None

        def put(self, *_args, **_kwargs):
            raise OSError("disk unavailable")

    payload = json.dumps(
        {"translations": [{"index": 7, "translation": "译文"}]},
        ensure_ascii=False,
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=payload))]
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kwargs: response)
        )
    )
    tr.MODEL = "test-model"
    tr.BASE_URL = "https://provider.invalid/v1"

    result = tr.translate_batch(
        client,
        [{"index": 7, "original": "hello"}],
        1,
        "ctx",
        "zh",
        cache=BrokenCache(),
    )

    assert result == [{"index": 7, "translation": "译文"}]


def test_load_progress_sanitizes_malformed_collection_shapes(tmp_path: Path):
    import translate_chat_openai as tr

    path = tmp_path / "bad.progress.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": tr.PROGRESS_SCHEMA_VERSION,
                "translations": ["not", "a", "mapping"],
                "fingerprints": "not-a-mapping",
                "failed": {"0": True},
            }
        ),
        encoding="utf-8",
    )

    loaded = tr.load_progress(path)

    assert loaded["translations"] == {}
    assert loaded["fingerprints"] == {}
    assert loaded["failed"] == []


def test_progress_compatibility_requires_all_translation_identity_fields():
    import translate_chat_openai as tr

    progress = tr.empty_progress()
    errors = tr.progress_compatibility_errors(
        progress,
        target_language="zh",
        context="ctx",
        provider=tr.TRANSLATION_PROVIDER,
        base_url="https://provider.invalid/v1",
        model="m",
        prompt_version=tr.PROMPT_VERSION,
    )
    assert {
        "target_language",
        "context",
        "provider",
        "base_url_fingerprint",
        "model",
        "prompt_version",
    } <= set(errors)


def _success_openai(record: dict, translation: str):
    class Completions:
        def __init__(self):
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            payload = json.dumps(
                {"translations": [{"index": 0, "translation": translation}]},
                ensure_ascii=False,
            )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=payload),
                    )
                ]
            )

    class Client:
        def __init__(self, **kwargs):
            record["kwargs"] = kwargs
            self.chat = SimpleNamespace(completions=Completions())
            record["completions"] = self.chat.completions

    return Client


def test_main_ignores_filled_json_without_compatible_progress_and_disables_sdk_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import translate_chat_openai as tr

    path = tmp_path / "translate.json"
    path.write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "index": 0,
                        "author": "alice",
                        "original": "hello",
                        "translation": "old-language",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    record: dict = {}
    monkeypatch.setattr(tr, "OpenAI", _success_openai(record, "新译文"))
    monkeypatch.setattr(tr, "BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setattr(tr, "API_KEY", "stub-key")
    monkeypatch.setattr(tr, "MODEL", "stub-model")
    monkeypatch.setattr(
        tr.sys,
        "argv",
        [
            "translate_chat_openai.py",
            str(path),
            "--workers",
            "1",
            "--request-timeout",
            "12.5",
        ],
    )

    tr.main()

    updated = json.loads(path.read_text(encoding="utf-8"))
    progress = tr.load_progress(tr.progress_path_for(path))
    assert updated["messages"][0]["translation"] == "新译文"
    assert record["completions"].calls == 1
    assert record["kwargs"]["max_retries"] == 0
    assert record["kwargs"]["timeout"] == 12.5
    assert progress["provider"] == tr.TRANSLATION_PROVIDER
    assert progress["base_url_fingerprint"] == tr.base_url_fingerprint(tr.BASE_URL)
    assert progress["model"] == tr.MODEL
    assert progress["prompt_version"] == tr.PROMPT_VERSION


@pytest.mark.parametrize(
    ("status_code", "message"),
    [(401, "unauthorized"), (404, "model not found")],
)
def test_terminal_client_error_is_not_retried_by_final_missing_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    message: str,
):
    import translate_chat_openai as tr

    class TerminalError(Exception):
        pass

    record = {"calls": 0}

    class Completions:
        def create(self, **_kwargs):
            record["calls"] += 1
            error = TerminalError(message)
            error.status_code = status_code
            raise error

    class Client:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=Completions())

    path = tmp_path / "terminal.json"
    path.write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "index": 0,
                        "author": "alice",
                        "original": "hello",
                        "translation": "stale",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tr, "OpenAI", Client)
    monkeypatch.setattr(tr, "BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setattr(tr, "API_KEY", "stub-key")
    monkeypatch.setattr(tr, "MODEL", "stub-model")
    monkeypatch.setattr(
        tr.sys,
        "argv",
        ["translate_chat_openai.py", str(path), "--workers", "1"],
    )

    with pytest.raises(SystemExit) as exc_info:
        tr.main()

    assert exc_info.value.code == 1
    assert record["calls"] == 1
    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["messages"][0]["translation"] == "hello"

def test_batch_builder_enforces_count_and_complete_prompt_budget():
    import translate_chat_openai as tr

    messages = [
        {"index": index, "original": "x" * 180}
        for index in range(6)
    ]
    one_prompt = tr.TRANSLATE_PROMPT.format(
        context="ctx",
        messages=tr.prepare_messages_for_llm(messages[:1]),
        target_language="zh",
    )
    budget = len(one_prompt) + 220

    batches = tr.build_translation_batches(
        messages,
        max_messages=4,
        max_prompt_chars=budget,
        context="ctx",
        target_language="zh",
    )

    assert len(batches) > 1
    assert [item["index"] for batch in batches for item in batch] == list(range(6))
    for batch in batches:
        prompt = tr.TRANSLATE_PROMPT.format(
            context="ctx",
            messages=tr.prepare_messages_for_llm(batch),
            target_language="zh",
        )
        assert len(batch) <= 4
        assert len(prompt) <= budget

    with pytest.raises(ValueError, match="max-batch-chars"):
        tr.build_translation_batches(
            [{"index": 99, "original": "hello"}],
            max_messages=4,
            max_prompt_chars=2_000,
            context="c" * 5_000,
            target_language="zh",
        )

def test_compatible_progress_preserves_later_human_review_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import translate_chat_openai as tr

    message = {
        "index": 0,
        "author": "alice",
        "original": "hello",
        "translation": "人工复核译文",
    }
    json_path = tmp_path / "reviewed.json"
    json_path.write_text(
        json.dumps({"messages": [message]}, ensure_ascii=False),
        encoding="utf-8",
    )
    progress_path = tr.progress_path_for(json_path)
    tr.save_progress(
        progress_path,
        {
            "schema_version": tr.PROGRESS_SCHEMA_VERSION,
            "provider": tr.TRANSLATION_PROVIDER,
            "base_url_fingerprint": tr.base_url_fingerprint(
                "https://provider.invalid/v1"
            ),
            "model": "stub-model",
            "prompt_version": tr.PROMPT_VERSION,
            "target_language": "zh",
            "context": "livestream chat",
            "translations": {"0": "旧机器译文"},
            "fingerprints": {"0": tr.fingerprint_message(message)},
            "failed": [],
        },
    )

    record: dict = {}
    monkeypatch.setattr(tr, "OpenAI", _success_openai(record, "不应调用"))
    monkeypatch.setattr(tr, "BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setattr(tr, "API_KEY", "stub-key")
    monkeypatch.setattr(tr, "MODEL", "stub-model")
    monkeypatch.setattr(
        tr.sys,
        "argv",
        ["translate_chat_openai.py", str(json_path), "--workers", "1"],
    )

    tr.main()

    updated = json.loads(json_path.read_text(encoding="utf-8"))
    progress = tr.load_progress(progress_path)
    assert record["completions"].calls == 0
    assert updated["messages"][0]["translation"] == "人工复核译文"
    assert progress["translations"]["0"] == "人工复核译文"


@pytest.mark.parametrize(
    "status_code",
    [400, 402, 404, 406, 409, 410, 411, 414, 415, 418, 421, 423, 425, 451],
)
def test_other_http_4xx_are_terminal_client_errors(status_code: int):
    import translation_support as support

    class HTTPError(Exception):
        def __init__(self, status: int):
            super().__init__(f"HTTP {status}")
            self.status_code = status

    assert (
        support.classify_api_error(HTTPError(status_code))
        == support.TranslationErrorKind.CLIENT
    )
    assert support.classify_api_error(HTTPError(408)) == support.TranslationErrorKind.TIMEOUT
    assert (
        support.classify_api_error(HTTPError(429))
        == support.TranslationErrorKind.RATE_LIMIT
    )
    assert support.classify_api_error(HTTPError(401)) == support.TranslationErrorKind.AUTH


def test_progress_file_alias_is_rejected_without_mutating_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import translate_chat_openai as tr

    json_path = tmp_path / "translation.json"
    json_path.write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "index": 0,
                        "author": "alice",
                        "original": "hello",
                        "translation": "",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    before = json_path.read_bytes()
    aliases = [json_path]
    hardlink = tmp_path / "translation-progress-hardlink.json"
    try:
        os.link(json_path, hardlink)
    except OSError:
        pass
    else:
        aliases.append(hardlink)
        assert tr.paths_refer_to_same_file(json_path, hardlink)

    monkeypatch.setattr(tr, "OpenAI", object)
    monkeypatch.setattr(tr, "BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setattr(tr, "API_KEY", "stub-key")
    monkeypatch.setattr(tr, "MODEL", "stub-model")
    for alias in aliases:
        monkeypatch.setattr(
            tr.sys,
            "argv",
            [
                "translate_chat_openai.py",
                str(json_path),
                "--progress-file",
                str(alias),
            ],
        )
        with pytest.raises(SystemExit) as exc_info:
            tr.main()
        assert exc_info.value.code == 2
        assert json_path.read_bytes() == before


def test_cleaned_empty_model_output_is_failed_and_not_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import translate_chat_openai as tr

    json_path = tmp_path / "clean-empty.json"
    message = {
        "index": 0,
        "author": "alice",
        "original": "hello",
        "translation": "",
    }
    json_path.write_text(
        json.dumps({"messages": [message]}, ensure_ascii=False),
        encoding="utf-8",
    )
    record: dict = {}
    monkeypatch.setattr(tr, "OpenAI", _success_openai(record, "<alice>"))
    monkeypatch.setattr(tr, "BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setattr(tr, "API_KEY", "stub-key")
    monkeypatch.setattr(tr, "MODEL", "stub-model")
    monkeypatch.setattr(
        tr.sys,
        "argv",
        ["translate_chat_openai.py", str(json_path), "--workers", "1"],
    )

    with pytest.raises(SystemExit) as exc_info:
        tr.main()

    assert exc_info.value.code == 1
    assert record["completions"].calls == 2
    updated = json.loads(json_path.read_text(encoding="utf-8"))
    progress = tr.load_progress(tr.progress_path_for(json_path))
    assert updated["messages"][0]["translation"] == "hello"
    assert progress["translations"] == {}
    assert progress["failed"] == [0]
    assert progress["fingerprints"]["0"] == tr.fingerprint_message(message)
    assert progress["json_translation_fingerprints"]["0"] == (
        tr.fingerprint_translation("hello")
    )


def _write_compatible_failed_progress(tr, json_path: Path, message: dict) -> None:
    tr.save_progress(
        tr.progress_path_for(json_path),
        {
            "schema_version": tr.PROGRESS_SCHEMA_VERSION,
            "provider": tr.TRANSLATION_PROVIDER,
            "base_url_fingerprint": tr.base_url_fingerprint(
                "https://provider.invalid/v1"
            ),
            "model": "stub-model",
            "prompt_version": tr.PROMPT_VERSION,
            "target_language": "zh",
            "context": "livestream chat",
            "translations": {},
            "fingerprints": {"0": tr.fingerprint_message(message)},
            "json_translation_fingerprints": {
                "0": tr.fingerprint_translation(message["original"])
            },
            "failed": [0],
        },
    )


def test_default_resume_preserves_human_edit_of_failed_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import translate_chat_openai as tr

    message = {
        "index": 0,
        "author": "alice",
        "original": "hello",
        "translation": "人工补译",
    }
    json_path = tmp_path / "human-recovery.json"
    json_path.write_text(
        json.dumps({"messages": [message]}, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_compatible_failed_progress(tr, json_path, message)
    record: dict = {}
    monkeypatch.setattr(tr, "OpenAI", _success_openai(record, "不应调用"))
    monkeypatch.setattr(tr, "BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setattr(tr, "API_KEY", "stub-key")
    monkeypatch.setattr(tr, "MODEL", "stub-model")
    monkeypatch.setattr(
        tr.sys,
        "argv",
        ["translate_chat_openai.py", str(json_path), "--workers", "1"],
    )

    tr.main()

    updated = json.loads(json_path.read_text(encoding="utf-8"))
    progress = tr.load_progress(tr.progress_path_for(json_path))
    assert record["completions"].calls == 0
    assert updated["messages"][0]["translation"] == "人工补译"
    assert progress["translations"]["0"] == "人工补译"
    assert progress["failed"] == []
    assert progress["json_translation_fingerprints"]["0"] == (
        tr.fingerprint_translation("人工补译")
    )


def test_default_resume_retries_unchanged_original_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import translate_chat_openai as tr

    message = {
        "index": 0,
        "author": "alice",
        "original": "hello",
        "translation": "hello",
    }
    json_path = tmp_path / "untouched-fallback.json"
    json_path.write_text(
        json.dumps({"messages": [message]}, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_compatible_failed_progress(tr, json_path, message)
    record: dict = {}
    monkeypatch.setattr(tr, "OpenAI", _success_openai(record, "新译文"))
    monkeypatch.setattr(tr, "BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setattr(tr, "API_KEY", "stub-key")
    monkeypatch.setattr(tr, "MODEL", "stub-model")
    monkeypatch.setattr(
        tr.sys,
        "argv",
        ["translate_chat_openai.py", str(json_path), "--workers", "1"],
    )

    tr.main()

    updated = json.loads(json_path.read_text(encoding="utf-8"))
    progress = tr.load_progress(tr.progress_path_for(json_path))
    assert record["completions"].calls == 1
    assert updated["messages"][0]["translation"] == "新译文"
    assert progress["translations"]["0"] == "新译文"
    assert progress["failed"] == []
