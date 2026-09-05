# -*- coding: utf-8 -*-
"""Regression tests for translate_chat_openai interrupt handling and prompt sizing.

Part (a): driving the real executor loop structure from
``translate_chat_openai.main`` (scripts/translate_chat_openai.py:1012-1092).
The loop is inlined in ``main`` and cannot be invoked without a full CLI run,
so the test replicates its verbatim control flow — the same
submit-all / as_completed / ``except BaseException`` shape — while calling the
REAL module pieces that the fix touches (``threading.Event`` abort semantics
plus the cancel-on-interrupt sequence: ``future.cancel()`` for every pending
future and ``executor.shutdown(wait=False, cancel_futures=True)``).  The
equivalence of that shape to the source is asserted by
``test_executor_loop_structure_matches_source``, which greps the source for the
load-bearing statements so drift breaks this test.

Part (b): the incremental size accounting in ``build_translation_batches``
must stay byte-for-byte equivalent to formatting the full prompt.
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path
import threading
import time

import pytest

from translate_chat_openai import (
    TRANSLATE_PROMPT,
    build_translation_batches,
    prepare_messages_for_llm,
)

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
SOURCE = (SCRIPTS_DIR / "translate_chat_openai.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Part (a): interrupt must not drain the pre-submitted executor queue
# ---------------------------------------------------------------------------


def test_executor_loop_structure_matches_source():
    """Guard: the replicated loop below stays equivalent to the real source.

    If any of these load-bearing statements drifts, this test fails and the
    replicated-structure test below must be revisited.
    """
    # abort fuse checked in the submit loop and the as_completed consumer
    assert "if abort_event.is_set():" in SOURCE
    # on fuse: cancel every pending future, then non-waiting shutdown
    assert "for pending in futures:" in SOURCE
    assert "executor.shutdown(wait=False, cancel_futures=True)" in SOURCE
    # inner handler inside the with-block: cancel BEFORE __exit__ waits
    inner = SOURCE.split("with concurrent.futures.ThreadPoolExecutor", 1)[1]
    inner = inner.split("except BaseException:", 1)[1]
    assert "pending.cancel()" in inner
    assert "executor.shutdown(wait=False, cancel_futures=True)" in inner
    # outer handler after the with-block also re-cancels idempotently
    outer = SOURCE.rsplit("except BaseException:", 1)[1]
    assert "pending.cancel()" in outer
    # the executor is still a ThreadPoolExecutor driven by as_completed
    assert "concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)" in SOURCE
    assert "concurrent.futures.as_completed(futures)" in SOURCE


class _Interrupt(Exception):  # noqa: N818 - KeyboardInterrupt stand-in, not an Error
    """BaseException-family stand-in for KeyboardInterrupt (same except path)."""


def _run_executor_loop(batches, worker_fn, *, interrupt_after=None, abort_event=None):
    """Verbatim structural replica of translate_chat_openai.main's executor block.

    Same statements as the fixed source (see
    test_executor_loop_structure_matches_source): pre-submit every batch,
    consume as_completed, an inner ``except BaseException`` *inside* the
    with-block that cancels all pending futures and shuts down without
    waiting, and an outer ``except BaseException`` after the with-block that
    re-cancels idempotently (persist step elided — not under test here).
    """
    if abort_event is None:
        abort_event = threading.Event()
    futures = {}
    started = time.monotonic()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            try:
                for batch in batches:
                    if abort_event.is_set():
                        break
                    future = executor.submit(worker_fn, batch)
                    futures[future] = batch

                first_done = {"flag": False}
                for future in concurrent.futures.as_completed(futures):
                    if abort_event.is_set():
                        for pending in futures:
                            pending.cancel()
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    futures[future]
                    try:
                        future.result()
                        if interrupt_after is not None and not first_done["flag"]:
                            first_done["flag"] = True
                            interrupt_after -= 1
                            if interrupt_after == 0:
                                raise _Interrupt("simulated Ctrl+C")
                    except _Interrupt:
                        raise
            except BaseException:
                # fix under test: cancel not-yet-started batches and stop
                # waiting BEFORE the with-block's shutdown(wait=True) runs.
                for pending in futures:
                    pending.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                raise
    except BaseException:
        # outer handler: idempotent re-cancel (persist elided), re-raise.
        for pending in futures:
            pending.cancel()
        raise
    return time.monotonic() - started


def test_interrupt_cancels_pending_batches():
    calls = []
    lock = threading.Lock()

    def fake_translate_batch(batch):
        with lock:
            calls.append(batch)
        time.sleep(0.3)
        return [{"index": 0, "translation": "x"}]

    batches = [f"batch_{i}" for i in range(10)]
    with pytest.raises(_Interrupt):
        _run_executor_loop(batches, fake_translate_batch, interrupt_after=1)

    # Ctrl+C after the first completion: batches 3..10 must be cancelled,
    # not silently drained by shutdown(wait=True).
    assert len(calls) < 10, (
        f"expected pending batches to be cancelled after interrupt, "
        f"but {len(calls)}/10 ran"
    )
    assert calls, "at least the in-flight batch should have run"


def test_interrupt_cancels_fast_enough():
    """The with-block must exit promptly instead of draining the queue."""
    def fake_translate_batch(batch):
        time.sleep(0.5)
        return [{"index": 0, "translation": "x"}]

    batches = [f"batch_{i}" for i in range(10)]
    started = time.perf_counter()
    with pytest.raises(_Interrupt):
        _run_executor_loop(batches, fake_translate_batch, interrupt_after=1)
    elapsed = time.perf_counter() - started
    # 10 batches x 0.5s / 2 workers would be ~2.5s if fully drained; with 2
    # in-flight batches finishing it is well under 1.5s.
    assert elapsed < 1.5, f"interrupt drained the queue: {elapsed:.2f}s"


def test_abort_fuse_cancels_pending_batches():
    """The AUTH/CLIENT abort fuse must also stop draining the queue."""
    calls = []
    lock = threading.Lock()
    abort_event = threading.Event()

    def fake_translate_batch(batch):
        with lock:
            calls.append(batch)
        if batch == "batch_1":
            # simulate bump_error() setting the global abort fuse
            abort_event.set()
        time.sleep(0.3)
        return None

    batches = [f"batch_{i}" for i in range(10)]
    _run_executor_loop(batches, fake_translate_batch, abort_event=abort_event)

    assert len(calls) < 10, (
        f"abort fuse should cancel pending batches, but {len(calls)}/10 ran"
    )


def test_without_cancel_queue_drains_red_control():
    """Red control: the pre-fix shape (plain break, no cancel) drains all 10.

    The interrupt escapes the with-block where the pre-fix code had no cancel
    step, so shutdown(wait=True) waits for every submitted batch.
    """
    calls = []
    lock = threading.Lock()

    def fake_translate_batch(batch):
        with lock:
            calls.append(batch)
        time.sleep(0.3)
        return [{"index": 0, "translation": "x"}]

    batches = [f"batch_{i}" for i in range(10)]
    abort_event = threading.Event()  # never set: KeyboardInterrupt path
    futures = {}
    with pytest.raises(_Interrupt):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            for batch in batches:
                if abort_event.is_set():
                    break
                futures[executor.submit(fake_translate_batch, batch)] = batch
            first = {"flag": False}
            for future in concurrent.futures.as_completed(futures):
                if abort_event.is_set():
                    break
                try:
                    future.result()
                    if not first["flag"]:
                        first["flag"] = True
                        raise _Interrupt("simulated Ctrl+C")
                except _Interrupt:
                    raise
        # pre-fix: no cancel, no cancel_futures shutdown — __exit__ drained
        # the whole queue before the interrupt surfaced.

    assert len(calls) == 10, "pre-fix shape should drain the whole queue (red control)"


# ---------------------------------------------------------------------------
# Part (b): incremental sizing == full-format prompt sizing
# ---------------------------------------------------------------------------


def _original_prompt_size(items, context, target_language):
    """Pre-fix prompt_size: full re-format per candidate (verbatim)."""
    messages_text = prepare_messages_for_llm(items)
    prompt = TRANSLATE_PROMPT.format(
        context=context,
        messages=messages_text,
        target_language=target_language,
    )
    return len(prompt)


def _original_build(messages, *, max_messages, max_prompt_chars, context, target_language):
    """Pre-fix build_translation_batches (verbatim reference)."""
    batches = []
    current = []
    for msg in messages:
        if current and len(current) >= max_messages:
            batches.append(current)
            current = []
        candidate = [*current, msg]
        if _original_prompt_size(candidate, context, target_language) <= max_prompt_chars:
            current = candidate
            continue
        if current:
            batches.append(current)
            current = [msg]
        else:
            current = candidate
        if _original_prompt_size(current, context, target_language) > max_prompt_chars:
            raise ValueError(
                f"消息 {msg.get('index')} 单条提示超过 --max-batch-chars="
                f"{max_prompt_chars}；请缩短 context 或提高上限"
            )
    if current:
        batches.append(current)
    return batches


@pytest.mark.parametrize(
    "context,target_language",
    [
        ("", "zh"),
        ("livestream chat", "zh"),
        ("词表: Pog => 狂欢\nKappa => 讽刺\n" * 20, "ja"),
        ("x" * 5000, "en-US"),
    ],
)
def test_incremental_sizing_matches_full_format(context, target_language):
    """Incremental accounting must equal a full TRANSLATE_PROMPT.format byte-for-byte."""
    msgs = [
        {"index": i, "original": text}
        for i, text in enumerate(
            ["hello world", "", "   ", "[Pog] [Kappa]", "GG wp 不愧是你",
             "emoji 🎉 混排", "@user hi", "12 34", "long " * 100]
        )
    ]
    batches = build_translation_batches(
        msgs,
        max_messages=3,
        max_prompt_chars=16_000,
        context=context,
        target_language=target_language,
    )
    shell = len(
        TRANSLATE_PROMPT.format(context=context, messages="", target_language=target_language)
    )
    for batch in batches:
        incremental = shell + len(prepare_messages_for_llm(batch))
        full = _original_prompt_size(batch, context, target_language)
        assert incremental == full


@pytest.mark.parametrize(
    "max_messages,max_prompt_chars",
    [(1, 3000), (3, 16000), (10, 200000), (50, 9999999)],
)
def test_batch_split_identical_to_original_algorithm(max_messages, max_prompt_chars):
    """The new implementation must produce the exact same batch split.

    Both algorithms may legitimately raise the ValueError for a single message
    over the cap; when they do, the message text must match verbatim.
    """
    context = "词表: Pog => 狂欢\n" * 100
    msgs = [
        {"index": i, "original": text}
        for i, text in enumerate(
            ["hello world", "", "   ", "[Pog] [Kappa]", "GG wp 23333",
             "long " * 300, "@user hi", "x" * 2000, "short"]
        )
    ]
    try:
        new_batches = build_translation_batches(
            msgs,
            max_messages=max_messages,
            max_prompt_chars=max_prompt_chars,
            context=context,
            target_language="zh",
        )
        new_exc = None
    except ValueError as exc:
        new_batches = None
        new_exc = str(exc)
    try:
        old_batches = _original_build(
            msgs,
            max_messages=max_messages,
            max_prompt_chars=max_prompt_chars,
            context=context,
            target_language="zh",
        )
        old_exc = None
    except ValueError as exc:
        old_batches = None
        old_exc = str(exc)
    assert (new_batches is None) == (old_batches is None)
    if new_exc is not None:
        assert new_exc == old_exc
        return
    assert new_batches == old_batches


def test_single_message_overflow_valueerror_unchanged():
    """A single message over the cap still raises the exact same ValueError."""
    context = ""
    huge = {"index": 7, "original": "y" * 5000}
    with pytest.raises(ValueError) as new_exc:
        build_translation_batches(
            [huge],
            max_messages=10,
            max_prompt_chars=1000,
            context=context,
            target_language="zh",
        )
    with pytest.raises(ValueError) as old_exc:
        _original_build(
            [huge],
            max_messages=10,
            max_prompt_chars=1000,
            context=context,
            target_language="zh",
        )
    assert str(new_exc.value) == str(old_exc.value)
    assert "单条提示超过" in str(new_exc.value)
    assert "7" in str(new_exc.value)
