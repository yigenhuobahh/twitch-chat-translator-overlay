#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hardening regressions for the translate/parser stack (review wave 2).

Covers: CJK range coverage for ja/ko, string-index model responses, author
color vs background-color, HTML-comment message stripping, extract_json
chatter tolerance, drive-prefix guard, progress value type validation, and
dotenv export-prefix / inline-comment parsing plus the cwd .env notice.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from helpers import load_module

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


def _client_returning(payloads: list[str], record: dict):
    """Stub OpenAI client whose completions.create returns payloads in order."""

    class Completions:
        def create(self, **_kwargs):
            payload = payloads[min(record["calls"], len(payloads) - 1)]
            record["calls"] += 1
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=payload))]
            )

    class Client:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=Completions())

    return Client


def _parse_html(tmp_path: Path, body: str) -> dict:
    parser = load_module("chat_parser", "chat_parser.py")
    html = (
        "<!DOCTYPE html><html><head><title>t</title></head><body>"
        + body
        + "</body></html>"
    )
    html_path = tmp_path / "chat.html"
    html_path.write_text(html, encoding="utf-8")
    return parser.parse_chat_html(str(html_path), str(tmp_path / "out"))


def _td_message(
    timestamp: int,
    author: str,
    style_attr: str,
    text: str,
) -> str:
    style = f' style="{style_attr}"' if style_attr else ""
    return (
        f'<pre class="comment-root">'
        f'[<a href="https://www.twitch.tv/videos/1?t=0h0m{timestamp}s">0:00:{timestamp:02d}</a>] '
        f'<span class="comment-author"{style}>{author}</span>'
        f'<span class="comment-message">: {text}</span></pre>'
    )


# ---------------------------------------------------------------------------
# PARSE-O1: CJK range coverage (kana / hangul / Extension A)
# ---------------------------------------------------------------------------


def test_username_echo_stripped_for_kana_hangul_and_extension_a():
    import translation_support as support

    assert support.clean_translation_text("Tanaka: マジで") == "マジで"
    assert support.clean_translation_text("Kim: 안녕하세요") == "안녕하세요"
    assert support.clean_translation_text("alice: 㐀㐁は拡張A") == "㐀㐁は拡張A"


def test_dual_candidate_folding_accepts_kana_and_hangul():
    import translation_support as support

    assert support.clean_translation_text("マジで/本当だよ") == "マジで"
    assert support.clean_translation_text("안녕하세요/반갑습니다") == "안녕하세요"


def test_clean_translation_guards_survive_cjk_expansion():
    import translation_support as support

    # Non-CJK remainders, times, labels, URLs and paths stay intact.
    assert support.clean_translation_text("alice: hello") == "alice: hello"
    assert support.clean_translation_text("12:30") == "12:30"
    assert support.clean_translation_text("Score: 5-0") == "Score: 5-0"
    assert (
        support.clean_translation_text("看这里 https://twitch.tv/foo")
        == "看这里 https://twitch.tv/foo"
    )
    assert support.clean_translation_text("and/or") == "and/or"
    assert support.clean_translation_text("A/B测试") == "A/B测试"


# ---------------------------------------------------------------------------
# PARSE-N2: drive prefix with a space after the colon
# ---------------------------------------------------------------------------


def test_drive_prefix_guard_accepts_space_after_colon():
    import translation_support as support

    assert support._DRIVE_PREFIX_RE.match("C: /盘里/x")
    assert support._DRIVE_PREFIX_RE.match("C:\\path")
    assert support._DRIVE_PREFIX_RE.match("D: \\\\srv\\share")


def test_drive_prefixed_text_is_never_username_stripped():
    import translation_support as support

    assert support.clean_translation_text("C: 盘里") == "C: 盘里"
    assert support.clean_translation_text("C: /盘里/字幕.ass") == "C: /盘里/字幕.ass"
    assert support.clean_translation_text("C:\\盘里\\file") == "C:\\盘里\\file"
    assert support.clean_translation_text("C:\\Users\\foo/bar") == "C:\\Users\\foo/bar"


# ---------------------------------------------------------------------------
# PARSE-O3: author color must not capture background-color
# ---------------------------------------------------------------------------


def test_author_color_ignores_background_color(tmp_path: Path):
    chat = _parse_html(
        tmp_path,
        _td_message(1, "Alice", "background-color: #FF0000; color: #00FF00", "hi"),
    )
    assert chat["messages"][0]["color"] == "#00FF00"


def test_author_color_empty_when_only_background_color(tmp_path: Path):
    chat = _parse_html(
        tmp_path,
        _td_message(1, "Bob", "background-color: #FF0000", "yo"),
    )
    assert chat["messages"][0]["color"] == ""


# ---------------------------------------------------------------------------
# PARSE-O4: HTML comments never produce messages
# ---------------------------------------------------------------------------


def test_commented_out_message_is_not_parsed(tmp_path: Path):
    real = _td_message(1, "Alice", "color: #FF0000", "hi")
    fake = _td_message(9, "FakeGuy", "color: #00FF00", "fake msg")
    chat = _parse_html(tmp_path, f"<!-- {fake} -->" + real)

    assert len(chat["messages"]) == 1
    assert chat["messages"][0]["author"] == "Alice"
    assert chat["messages"][0]["timestamp"] == 1.0


def test_fully_commented_body_yields_no_messages(tmp_path: Path):
    fake = _td_message(9, "FakeGuy", "color: #00FF00", "fake msg")
    chat = _parse_html(tmp_path, f"<!-- {fake} -->")

    assert chat["messages"] == []


def test_comments_between_messages_keep_surrounding_messages(tmp_path: Path):
    first = _td_message(1, "Alice", "color: #FF0000", "hi")
    fake = _td_message(5, "FakeGuy", "color: #00FF00", "fake msg")
    second = _td_message(2, "Carol", "color: #0000FF", "yo")
    chat = _parse_html(tmp_path, first + f"<!-- {fake} -->" + second)

    assert [m["author"] for m in chat["messages"]] == ["Alice", "Carol"]
    assert [m["timestamp"] for m in chat["messages"]] == [1.0, 2.0]


# ---------------------------------------------------------------------------
# PARSE-O6: extract_json fence / chatter tolerance
# ---------------------------------------------------------------------------


def test_extract_json_strips_leading_code_fence():
    import translate_chat_openai as tr

    assert tr.extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert tr.extract_json('```\n{"a": 1}\n```') == '{"a": 1}'


def test_extract_json_slices_bare_json_with_leading_chatter():
    import translate_chat_openai as tr

    assert tr.extract_json('Here you go: {"a": 1}') == '{"a": 1}'
    assert tr.extract_json('{"a": 1} hope this helps') == '{"a": 1}'
    assert (
        tr.extract_json('Sure!\n```json\n{"a": 1}\n```')
        == '{"a": 1}'
    )


def test_extract_json_keeps_json_string_containing_fence_markers():
    import translate_chat_openai as tr

    text = '{"a": "has ``` inside"}'
    assert tr.extract_json(text) == text


def test_extract_json_without_json_keeps_bad_json_failure_path():
    import translate_chat_openai as tr

    junk = "抱歉，无法翻译"
    assert tr.extract_json(junk) == junk
    with pytest.raises(json.JSONDecodeError):
        json.loads(tr.extract_json(junk))


# ---------------------------------------------------------------------------
# PARSE-O2: string indexes from the model must not fail the batch
# ---------------------------------------------------------------------------


def test_translate_batch_remaps_string_batch_local_indexes(monkeypatch):
    import translate_chat_openai as tr

    monkeypatch.setattr(tr.time, "sleep", lambda _s: None)
    payload = json.dumps(
        {"translations": [
            {"index": "0", "translation": "译A"},
            {"index": "1", "translation": "译B"},
        ]},
        ensure_ascii=False,
    )
    record = {"calls": 0}
    client = _client_returning([payload], record)()

    result = tr.translate_batch(
        client,
        [{"index": 5, "original": "a"}, {"index": 9, "original": "b"}],
        1,
        "ctx",
        "zh",
    )

    assert record["calls"] == 1
    assert result == [
        {"index": 5, "translation": "译A"},
        {"index": 9, "translation": "译B"},
    ]


def test_translate_batch_accepts_string_global_indexes(monkeypatch):
    import translate_chat_openai as tr

    monkeypatch.setattr(tr.time, "sleep", lambda _s: None)
    payload = json.dumps(
        {"translations": [
            {"index": "5", "translation": "译A"},
            {"index": "9", "translation": "译B"},
        ]},
        ensure_ascii=False,
    )
    record = {"calls": 0}
    client = _client_returning([payload], record)()

    result = tr.translate_batch(
        client,
        [{"index": 5, "original": "a"}, {"index": 9, "original": "b"}],
        1,
        "ctx",
        "zh",
    )

    assert record["calls"] == 1
    assert result == [
        {"index": 5, "translation": "译A"},
        {"index": 9, "translation": "译B"},
    ]


def test_translate_batch_drops_row_with_non_numeric_index(monkeypatch):
    import translate_chat_openai as tr

    monkeypatch.setattr(tr.time, "sleep", lambda _s: None)
    payload = json.dumps(
        {"translations": [
            {"index": "5", "translation": "译A"},
            {"index": "oops", "translation": "译B"},
        ]},
        ensure_ascii=False,
    )
    record = {"calls": 0}
    client = _client_returning([payload], record)()
    error_counts: dict[str, int] = {}

    result = tr.translate_batch(
        client,
        [{"index": 5, "original": "a"}, {"index": 9, "original": "b"}],
        1,
        "ctx",
        "zh",
        error_counts=error_counts,
    )

    # One invalid row is dropped without failing/retrying the whole batch.
    assert record["calls"] == 1
    assert result == [{"index": 5, "translation": "译A"}]
    assert error_counts.get("bad_json") == 1


def test_translate_batch_still_rejects_duplicate_string_indexes(monkeypatch):
    import translate_chat_openai as tr

    monkeypatch.setattr(tr.time, "sleep", lambda _s: None)
    payload = json.dumps(
        {"translations": [
            {"index": "0", "translation": "译A"},
            {"index": "0", "translation": "译B"},
        ]},
        ensure_ascii=False,
    )
    record = {"calls": 0}
    client = _client_returning([payload], record)()

    result = tr.translate_batch(
        client,
        [{"index": 5, "original": "a"}, {"index": 9, "original": "b"}],
        1,
        "ctx",
        "zh",
    )

    # Duplicates (even coerced from strings) must keep the retry + failure path.
    assert record["calls"] == 3
    assert result is None


def test_translate_batch_still_retries_when_model_omits_a_row(monkeypatch):
    import translate_chat_openai as tr

    monkeypatch.setattr(tr.time, "sleep", lambda _s: None)
    payload = json.dumps(
        {"translations": [{"index": "5", "translation": "译A"}]},
        ensure_ascii=False,
    )
    record = {"calls": 0}
    client = _client_returning([payload], record)()

    result = tr.translate_batch(
        client,
        [{"index": 5, "original": "a"}, {"index": 9, "original": "b"}],
        1,
        "ctx",
        "zh",
    )

    assert record["calls"] == 3
    assert result is None


# ---------------------------------------------------------------------------
# R1: 合法 JSON 但 translations 为空数组时必须走重试，不得静默返回 []
# ---------------------------------------------------------------------------


def test_translate_batch_retries_on_valid_empty_translations(monkeypatch):
    import translate_chat_openai as tr

    monkeypatch.setattr(tr.time, "sleep", lambda _s: None)
    # 恒返回合法 JSON 但空数组：历史实现会跳过修复/重试分支直接返回 []，
    # 让"批成功但零译文"骗过 main() 的失败标记。
    payload = json.dumps({"translations": []}, ensure_ascii=False)
    record = {"calls": 0}
    client = _client_returning([payload], record)()
    error_counts: dict[str, int] = {}

    result = tr.translate_batch(
        client,
        [{"index": 5, "original": "a"}, {"index": 9, "original": "b"}],
        1,
        "ctx",
        "zh",
        cache=tr.TranslationCache(None),
        error_counts=error_counts,
    )

    # 3 次重试全部耗尽后必须返回 None（缓存项也没有），且确实调了 3 次 API。
    assert record["calls"] == 3
    assert result is None
    assert error_counts.get("unknown") == 3


# ---------------------------------------------------------------------------
# PARSE-N4: non-string progress translation values must not be reused
# ---------------------------------------------------------------------------


def test_progress_translation_with_dict_value_is_retranslated(
    tmp_path: Path,
    monkeypatch,
):
    import translate_chat_openai as tr

    message = {"index": 0, "author": "alice", "original": "hello", "translation": ""}
    json_path = tmp_path / "poison.json"
    json_path.write_text(
        json.dumps({"messages": [message]}, ensure_ascii=False),
        encoding="utf-8",
    )
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
            "translations": {"0": {"nested": "poison"}},
            "fingerprints": {"0": tr.fingerprint_message(message)},
            "json_translation_fingerprints": {"0": tr.fingerprint_translation("")},
            "failed": [],
        },
    )

    payload = json.dumps(
        {"translations": [{"index": 0, "translation": "新译文"}]},
        ensure_ascii=False,
    )
    record = {"calls": 0}
    monkeypatch.setattr(tr, "OpenAI", _client_returning([payload], record))
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
    # The dict "translation" must not be str()-ed into a reused value.
    assert record["calls"] == 1
    assert updated["messages"][0]["translation"] == "新译文"


# ---------------------------------------------------------------------------
# Context auto-raise: file-delivered context must not trip the per-batch cap
# ---------------------------------------------------------------------------


def test_main_auto_raises_max_batch_chars_for_long_context_file(
    tmp_path: Path,
    monkeypatch,
):
    """超长 context（--context-file）且未传 --max-batch-chars 时 main() 自动抬高上限。

    默认 16000 对 4 万字符 context 会在 build_translation_batches 对第一条
    消息抛"单条提示超过上限"；main() 必须按与管道侧 render_cn_chat 相同的
    公式 min(200_000, len(context) + 16_000) 抬高后正常完成翻译。
    """
    import translate_chat_openai as tr

    json_path = tmp_path / "long-ctx.json"
    json_path.write_text(
        json.dumps(
            {
                "messages": [
                    {"index": 0, "author": "alice", "original": "hello", "translation": ""}
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx_file = tmp_path / "glossary.txt"
    ctx_file.write_text("g" * 40_000, encoding="utf-8")
    payload = json.dumps(
        {"translations": [{"index": 0, "translation": "译"}]},
        ensure_ascii=False,
    )
    record = {"calls": 0}
    monkeypatch.setattr(tr, "OpenAI", _client_returning([payload], record))
    monkeypatch.setattr(tr, "BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setattr(tr, "API_KEY", "stub-key")
    monkeypatch.setattr(tr, "MODEL", "stub-model")
    monkeypatch.setattr(
        tr.sys,
        "argv",
        [
            "translate_chat_openai.py",
            str(json_path),
            "--context-file",
            str(ctx_file),
            "--workers",
            "1",
        ],
    )

    tr.main()

    assert record["calls"] == 1
    updated = json.loads(json_path.read_text(encoding="utf-8"))
    assert updated["messages"][0]["translation"] == "译"


def test_prepare_translation_context_unique_filename_and_cleanup(tmp_path):
    """context 交接文件带 pid+随机后缀（并发/重试不覆盖），用后即清（静默）。"""
    pipe = load_module("render_cn_chat", "render_cn_chat.py")
    context = "词" * 9000  # > argv 阈值 → 走文件传递

    args_a = pipe._prepare_translation_context(context, tmp_path)
    args_b = pipe._prepare_translation_context(context, tmp_path)

    path_a, path_b = Path(args_a[1]), Path(args_b[1])
    try:
        assert path_a.parent.resolve() == tmp_path.resolve()
        assert path_b.parent.resolve() == tmp_path.resolve()
        assert path_a != path_b
        assert path_a.is_file() and path_b.is_file()
        assert path_a.name.startswith(f"translation_context_{os.getpid()}_")

        # 清理：登记的全部文件被删除，注册表清空，重复调用幂等。
        pipe._cleanup_translation_context_file()
        assert not path_a.exists() and not path_b.exists()
        assert pipe._translation_context_files == []
        pipe._cleanup_translation_context_file()  # 幂等
    finally:
        pipe._cleanup_translation_context_file()


def test_prepare_translation_context_cleanup_silences_oserror(tmp_path, monkeypatch):
    """清理失败（如文件被占用）必须静默，不能打断翻译流水线。"""
    pipe = load_module("render_cn_chat", "render_cn_chat.py")
    context = "词" * 9000
    args = pipe._prepare_translation_context(context, tmp_path)
    path = Path(args[1])

    def raise_oserror(self, missing_ok=False):
        raise OSError("file in use")

    monkeypatch.setattr(Path, "unlink", raise_oserror)
    pipe._cleanup_translation_context_file()  # 不得抛异常
    monkeypatch.undo()
    assert pipe._translation_context_files == []
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# PARSE-N5: dotenv export prefix + inline comments
# ---------------------------------------------------------------------------


def test_dotenv_parses_export_prefix_and_inline_comments(
    tmp_path: Path,
    monkeypatch,
):
    import common_utils as cu

    _clear_translation_env(monkeypatch)
    monkeypatch.delenv("_TWITCH_TRANSPARENT_TEST_MODE", raising=False)
    monkeypatch.setattr(cu, "_DOTENV_LOADED_KEYS", set())
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "# full line comment",
                "OPENAI_COMPAT_API_KEY=abc # inline comment",
                'OPENAI_COMPAT_BASE_URL="https://provider.invalid/v1" # trailing',
                "AGNES_MODEL=m#del",
                "export AGNES_API_KEY=exp-key",
                "OPENAI_COMPAT_MODEL=#FF0000",
                "AGNES_BASE_URL='quo#ted'",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    # R-3: cwd .env 同时提供端点+密钥时需交互确认;此处模拟用户确认(y),
    # 使本解析语义测试继续覆盖 export 前缀/行内注释的处理。
    monkeypatch.setattr(cu, "_confirm_untrusted_dotenv", lambda: True)

    cu.load_dotenv_if_present()

    assert os.environ["OPENAI_COMPAT_API_KEY"] == "abc"
    assert os.environ["OPENAI_COMPAT_BASE_URL"] == "https://provider.invalid/v1"
    assert os.environ["AGNES_MODEL"] == "m#del"
    assert os.environ["AGNES_API_KEY"] == "exp-key"
    assert os.environ["OPENAI_COMPAT_MODEL"] == "#FF0000"
    assert os.environ["AGNES_BASE_URL"] == "quo#ted"

    # load_dotenv_if_present writes os.environ directly, outside monkeypatch undo.
    for key in _TRANSLATION_ENV_KEYS:
        os.environ.pop(key, None)


# ---------------------------------------------------------------------------
# SEC-O2: foreign cwd .env load is announced
# ---------------------------------------------------------------------------


def test_foreign_cwd_dotenv_load_prints_notice(tmp_path: Path, monkeypatch, capsys):
    import common_utils as cu

    _clear_translation_env(monkeypatch)
    monkeypatch.delenv("_TWITCH_TRANSPARENT_TEST_MODE", raising=False)
    monkeypatch.setattr(cu, "_DOTENV_LOADED_KEYS", set())
    (tmp_path / ".env").write_text("OPENAI_COMPAT_API_KEY=foreign-key\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cu.load_dotenv_if_present()

    out = capsys.readouterr().out
    assert "已从当前目录加载 .env" in out
    assert str(tmp_path) in out
    assert os.environ["OPENAI_COMPAT_API_KEY"] == "foreign-key"

    for key in _TRANSLATION_ENV_KEYS:
        os.environ.pop(key, None)


def test_repo_root_dotenv_load_prints_no_notice(tmp_path: Path, monkeypatch, capsys):
    import common_utils as cu

    _clear_translation_env(monkeypatch)
    monkeypatch.delenv("_TWITCH_TRANSPARENT_TEST_MODE", raising=False)
    monkeypatch.setattr(cu, "_DOTENV_LOADED_KEYS", set())
    # Fake a source-checkout layout; cwd .env is the repo root .env itself.
    fake_repo = tmp_path / "repo"
    (fake_repo / "scripts").mkdir(parents=True)
    (fake_repo / "scripts" / "common_utils.py").write_text("", encoding="utf-8")
    (fake_repo / ".env").write_text("OPENAI_COMPAT_API_KEY=repo-key\n", encoding="utf-8")
    monkeypatch.setattr(cu, "__file__", str(fake_repo / "scripts" / "common_utils.py"))
    monkeypatch.chdir(fake_repo)

    cu.load_dotenv_if_present()

    assert "已从当前目录加载" not in capsys.readouterr().out
    assert os.environ["OPENAI_COMPAT_API_KEY"] == "repo-key"

    for key in _TRANSLATION_ENV_KEYS:
        os.environ.pop(key, None)


# ---------------------------------------------------------------------------
# C-O9: exponential backoff carries bounded jitter; Retry-After stays exact
# ---------------------------------------------------------------------------


def test_backoff_exponential_branch_adds_bounded_jitter(monkeypatch):
    import translation_support as support

    kind = support.TranslationErrorKind.SERVER  # base 10.0
    # Max jitter (base * 0.3): attempt 1 -> 10*2 + 10*0.3 = 23.0
    monkeypatch.setattr(support.random, "uniform", lambda lo, hi: hi)
    assert support.backoff_seconds(kind, 1) == pytest.approx(23.0)
    # Zero jitter keeps the historical pure-exponential value.
    monkeypatch.setattr(support.random, "uniform", lambda lo, hi: 0.0)
    assert support.backoff_seconds(kind, 1) == pytest.approx(20.0)

    # Unpatched: value stays within [pure, pure + base*0.3] and caps at 120.
    monkeypatch.undo()
    for _ in range(50):
        value = support.backoff_seconds(kind, 1)
        assert 20.0 <= value <= 23.0
    assert support.backoff_seconds(kind, 8) == 120.0


def test_backoff_retry_after_branch_ignores_jitter(monkeypatch):
    import translation_support as support

    class RateLimitedError(Exception):
        status_code = 429
        response = type("R", (), {"headers": {"Retry-After": "7"}})()

    # Even with maximal jitter, the server-provided Retry-After value is exact.
    monkeypatch.setattr(support.random, "uniform", lambda lo, hi: hi)
    assert (
        support.backoff_seconds(
            support.TranslationErrorKind.RATE_LIMIT, 0, RateLimitedError()
        )
        == 7.0
    )
    # Zero-base kinds (AUTH/CLIENT) stay 0 regardless of jitter.
    assert support.backoff_seconds(support.TranslationErrorKind.AUTH, 0) == 0.0


# ---------------------------------------------------------------------------
# C-O6: success finalization keeps a failed_last_run audit trail
# ---------------------------------------------------------------------------


def test_success_run_keeps_failed_last_run_audit_and_clears_failed(
    tmp_path: Path,
    monkeypatch,
):
    """批次失败进入 progress 后被最终重试救回:收尾 failed 清空,
    failed_last_run 保留本轮失败明细供事后审计。"""
    import translate_chat_openai as tr

    messages = [
        {"index": 0, "author": "alice", "original": "flaky", "translation": ""},
        {"index": 1, "author": "bob", "original": "stable", "translation": ""},
    ]
    json_path = tmp_path / "audit.json"
    json_path.write_text(
        json.dumps({"messages": messages}, ensure_ascii=False),
        encoding="utf-8",
    )

    class FlakyError(Exception):
        status_code = 500  # retryable SERVER kind

    calls = {"flaky": 0, "stable": 0}

    class Completions:
        def create(self, **kwargs):
            prompt = kwargs["messages"][0]["content"]
            if "flaky" in prompt:
                calls["flaky"] += 1
                if calls["flaky"] <= 3:
                    # Main pass: all 3 in-batch attempts fail -> batch failure.
                    raise FlakyError("internal server error")
                payload = json.dumps(
                    {"translations": [{"index": 0, "translation": "恢复了"}]},
                    ensure_ascii=False,
                )
            else:
                calls["stable"] += 1
                payload = json.dumps(
                    {"translations": [{"index": 1, "translation": "稳定"}]},
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
    monkeypatch.setattr(tr.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        tr.sys,
        "argv",
        [
            "translate_chat_openai.py",
            str(json_path),
            "--workers",
            "1",
            "--batch-size",
            "1",
        ],
    )

    tr.main()

    assert calls["flaky"] == 4  # 3 failed attempts + 1 rescued retry-pass call
    progress = tr.load_progress(tr.progress_path_for(json_path))
    assert progress["failed"] == []
    assert progress["failed_last_run"] == [0]
    updated = json.loads(json_path.read_text(encoding="utf-8"))
    assert updated["messages"][0]["translation"] == "恢复了"


# ---------------------------------------------------------------------------
# C-O10: interrupt during the execution phase still persists finished batches
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_forces_progress_persist_of_completed_batches(
    tmp_path: Path,
    monkeypatch,
):
    """C-O10:worker 抛 KeyboardInterrupt 时,已完成批次必须被强制落盘。

    批次 1 完成后首次落盘(节流窗口开启);批次 2 完成后的非强制落盘被
    30 秒节流跳过;批次 3 抛 KeyboardInterrupt。收尾强制落盘必须把批次 2
    的译文补写进 progress,否则它会停留在内存里丢失。
    """
    import translate_chat_openai as tr

    messages = [
        {"index": i, "author": "a", "original": f"m{i}", "translation": ""}
        for i in range(3)
    ]
    json_path = tmp_path / "interrupt.json"
    json_path.write_text(
        json.dumps({"messages": messages}, ensure_ascii=False),
        encoding="utf-8",
    )

    def fake_translate_batch(_client, batch, batch_num, *_args, **_kwargs):
        if batch_num == 3:
            raise KeyboardInterrupt("simulated ctrl+c")
        return [
            {"index": batch[0]["index"], "translation": f"译{batch[0]['index']}"}
        ]

    class StubClient:
        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr(tr, "translate_batch", fake_translate_batch)
    monkeypatch.setattr(tr, "OpenAI", StubClient)
    monkeypatch.setattr(tr, "BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setattr(tr, "API_KEY", "stub-key")
    monkeypatch.setattr(tr, "MODEL", "stub-model")
    monkeypatch.setattr(
        tr.sys,
        "argv",
        [
            "translate_chat_openai.py",
            str(json_path),
            "--workers",
            "1",
            "--batch-size",
            "1",
        ],
    )

    with pytest.raises(KeyboardInterrupt):
        tr.main()

    progress = tr.load_progress(tr.progress_path_for(json_path))
    assert progress["translations"] == {"0": "译0", "1": "译1"}
    assert progress["failed"] == []
    # 中断发生在收尾之前:JSON 快照未写回,译文只在 progress 中。
    updated = json.loads(json_path.read_text(encoding="utf-8"))
    assert [m["translation"] for m in updated["messages"]] == ["", "", ""]


# ---------------------------------------------------------------------------
# O·AUTH/CLIENT 全局熔断: 配置类错误不得让每个 worker 把全部批次打一遍 API
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# P-8: TranslationCache 并发读写(写写互斥 + 无锁读路径)
# ---------------------------------------------------------------------------


def test_translation_cache_concurrent_writes_keep_every_key(tmp_path: Path):
    """8 线程 × 各写不同 key × 200 轮:写写互斥下文件不丢、JSON 完整可解析。"""
    from translation_support import TranslationCache

    cache = TranslationCache(tmp_path / "cache")
    workers = 8
    rounds = 200
    barrier = threading.Barrier(workers)

    def worker(w: int):
        barrier.wait()
        for r in range(rounds):
            cache.put(f"msg{w}", "zh", "m1", "ctx", f"译文{w}-{r}")
            cache.get(f"msg{w}", "zh", "m1", "ctx")

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    files = list((tmp_path / "cache").glob("*.json"))
    assert len(files) == workers  # 每个 key 一个文件,无临时文件残留
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["translation"].startswith("译文")
    # 每个 key 都能 get 回一个非空译文(不得丢 key / 丢更新到空)。
    for w in range(workers):
        assert (cache.get(f"msg{w}", "zh", "m1", "ctx") or "").startswith("译文")


def test_translation_cache_get_survives_concurrent_atomic_replace(tmp_path: Path):
    """读路径不持全局锁:并发 get 与 put(原子替换)交错时 get 返回值合法或不命中。"""
    from translation_support import TranslationCache

    cache = TranslationCache(tmp_path / "cache")
    cache.put("seed", "zh", "m1", "ctx", "种子")
    barrier = threading.Barrier(4)
    stop = threading.Event()
    errors: list[BaseException] = []

    def reader():
        barrier.wait()
        try:
            for _ in range(400):
                value = cache.get("seed", "zh", "m1", "ctx")
                # 原子替换语义:要么见旧/新完整内容,要么(极端时序)未命中。
                assert value is None or value == "种子" or value.startswith("更新")
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def writer():
        barrier.wait()
        for i in range(400):
            cache.put("seed", "zh", "m1", "ctx", f"更新{i}")
        stop.set()

    threads = [threading.Thread(target=reader) for _ in range(3)] + [
        threading.Thread(target=writer)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    # 最终文件仍可解析且非空。
    final = cache.get("seed", "zh", "m1", "ctx")
    assert final is not None and final.startswith("更新")


def test_translation_cache_get_returns_none_on_corrupt_json(tmp_path: Path):
    """损坏 JSON 文件的 get 返回 None,与未命中语义一致(不抛异常)。"""
    from translation_support import TranslationCache

    cache = TranslationCache(tmp_path / "cache")
    cache.put("good", "zh", "m1", "ctx", "好")
    good_path = tmp_path / "cache"
    key_file = next(good_path.glob("*.json"))
    key_file.write_text("{not valid json", encoding="utf-8")

    assert cache.get("good", "zh", "m1", "ctx") is None
    # 未命中路径同样返回 None。
    assert cache.get("missing", "zh", "m1", "ctx") is None


def test_main_aborts_remaining_batches_on_auth_error(tmp_path: Path, monkeypatch):
    """首批返回 401 后：所有批次都标记失败，但 API 实际调用远小于批次数。"""
    import translate_chat_openai as tr

    messages = [
        {"index": i, "author": "a", "original": f"m{i}", "translation": ""}
        for i in range(8)
    ]
    json_path = tmp_path / "auth_abort.json"
    json_path.write_text(
        json.dumps({"messages": messages}, ensure_ascii=False),
        encoding="utf-8",
    )

    class AuthError(Exception):
        status_code = 401

    class Completions:
        calls = 0

        def create(self, **_kwargs):
            Completions.calls += 1
            raise AuthError("401 unauthorized: invalid api key")

    class Client:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=Completions())

    monkeypatch.setattr(tr.time, "sleep", lambda _s: None)
    monkeypatch.setattr(tr, "OpenAI", Client)
    monkeypatch.setattr(tr, "BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setattr(tr, "API_KEY", "stub-key")
    monkeypatch.setattr(tr, "MODEL", "stub-model")
    monkeypatch.setattr(
        tr.sys,
        "argv",
        [
            "translate_chat_openai.py",
            str(json_path),
            "--workers",
            "4",
            "--batch-size",
            "1",
        ],
    )

    # 缺失译文时 main() 以 SystemExit(1) 退出
    with pytest.raises(SystemExit) as exc_info:
        tr.main()

    assert exc_info.value.code == 1

    # 全部 8 批（8 条非保留消息）都失败
    progress = tr.load_progress(tr.progress_path_for(json_path))
    assert sorted(progress["failed"]) == list(range(8))
    # 熔断生效：8 批 × workers 4 本可放大到 ~8+ 次 API 调用；熔断后 1-2 次。
    assert Completions.calls < 8, Completions.calls
    updated = json.loads(json_path.read_text(encoding="utf-8"))
    assert all(m["translation"] == m["original"] for m in updated["messages"])
