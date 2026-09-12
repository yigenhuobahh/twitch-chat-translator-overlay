#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C1 (design-2): rational-fps parsing converged onto ONE helper.

media_probe.parse_rational_fps_text is the single authority; the four former
parallel parsers (cli_spec._download_output_fps, twitch_chat_burn.
parse_output_fps_arg, render_preset._coerce output_fps branch,
job_config._validated_float_field output_fps branch) are thin wrappers that
preserve only their return contracts. Pinned here:

- helper semantics: fractions / plain decimals accepted; negatives (either
  side of a fraction), denominator 0, non-finite and garbage all ValueError.
- semantic tightening (user-approved): "-30000/1001" is now rejected by the
  argparse types too — cli_spec._download_output_fps previously ACCEPTED
  negative fractions.
- each wrapper keeps its return contract (str passthrough for fractions in
  cli_spec / render_preset / job_config; float everywhere in burn).
"""

from __future__ import annotations

import argparse

import pytest

import media_probe

# ---------------------------------------------------------------------------
# Shared helper: media_probe.parse_rational_fps_text
# ---------------------------------------------------------------------------


def test_helper_accepts_plain_and_fraction_forms():
    text, value = media_probe.parse_rational_fps_text("30000/1001")
    assert text == "30000/1001"
    assert value == pytest.approx(30000 / 1001, abs=1e-12)

    text, value = media_probe.parse_rational_fps_text(" 29.97 ")
    assert text == "29.97"
    assert value == pytest.approx(29.97)

    text, value = media_probe.parse_rational_fps_text("60")
    assert value == 60.0

    # Decimal fraction sides are allowed (a, b may be decimals).
    _, value = media_probe.parse_rational_fps_text("59.94/2")
    assert value == pytest.approx(29.97)


@pytest.mark.parametrize(
    "bad",
    [
        "-30000/1001",  # negative numerator
        "30000/-1001",  # negative denominator
        "-29.97",  # negative plain
        "0",  # non-positive plain
        "30000/0",  # zero denominator
        "abc",
        "30000/",
        "/1001",
        "1/2/3",
        "30000/1001x",
        "",
        "nan",
        "inf",
        "-inf",
        "nan/1",
    ],
)
def test_helper_rejects_invalid_values(bad):
    with pytest.raises(ValueError, match="无效帧率"):
        media_probe.parse_rational_fps_text(bad)


# ---------------------------------------------------------------------------
# cli_spec._download_output_fps: plain → float, fraction → str passthrough
# ---------------------------------------------------------------------------


def test_download_output_fps_contracts_preserved():
    import cli_spec

    assert cli_spec._download_output_fps("60") == 60.0
    assert cli_spec._download_output_fps("29.97") == 29.97
    assert cli_spec._download_output_fps("30000/1001") == "30000/1001"

    with pytest.raises(argparse.ArgumentTypeError):
        cli_spec._download_output_fps("abc")
    with pytest.raises(argparse.ArgumentTypeError):
        cli_spec._download_output_fps("30000/0")


def test_download_output_fps_rejects_negative_fraction():
    """Semantic tightening: negative fractions are now rejected everywhere."""
    import cli_spec

    with pytest.raises(argparse.ArgumentTypeError):
        cli_spec._download_output_fps("-30000/1001")
    with pytest.raises(argparse.ArgumentTypeError):
        cli_spec._download_output_fps("30000/-1001")


# ---------------------------------------------------------------------------
# twitch_chat_burn.parse_output_fps_arg: always float
# ---------------------------------------------------------------------------


def test_parse_output_fps_arg_always_float():
    import twitch_chat_burn as burn

    assert burn.parse_output_fps_arg("30000/1001") == pytest.approx(30000 / 1001, abs=1e-12)
    assert burn.parse_output_fps_arg(" 59.94 ") == pytest.approx(59.94)

    with pytest.raises(argparse.ArgumentTypeError):
        burn.parse_output_fps_arg("30000/0")
    with pytest.raises(argparse.ArgumentTypeError):
        burn.parse_output_fps_arg("abc")


def test_parse_output_fps_arg_rejects_negative_fraction():
    import twitch_chat_burn as burn

    with pytest.raises(argparse.ArgumentTypeError):
        burn.parse_output_fps_arg("-30000/1001")


# ---------------------------------------------------------------------------
# render_preset._coerce: fraction stays str, plain stays float
# ---------------------------------------------------------------------------


def test_render_preset_output_fps_contracts_preserved():
    import render_preset

    assert render_preset._coerce("output_fps", "30000/1001") == "30000/1001"
    assert render_preset._coerce("output_fps", "29.97") == 29.97
    assert render_preset._coerce("output_fps", None) is None
    assert render_preset._coerce("output_fps", "") is None
    assert render_preset._coerce("output_fps", 60) == 60.0

    with pytest.raises(ValueError, match="有理数帧率无效"):
        render_preset._coerce("output_fps", "30000/0")


def test_render_preset_output_fps_rejects_negative_fraction():
    import render_preset

    with pytest.raises(ValueError):
        render_preset._coerce("output_fps", "-30000/1001")


def test_render_preset_output_fps_rejects_nonpositive_numeric():
    """Non-str (YAML int/float) values take the same positive/finite check as
    the str branch (regression: correctness-5 — the bare `float(value)` branch
    silently accepted -29.97 / 0 / inf / nan)."""
    import render_preset

    for bad in (-29.97, 0, 0.0, -1, float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValueError, match="output_fps"):
            render_preset._coerce("output_fps", bad)
    # 正常数字形态不受影响。
    assert render_preset._coerce("output_fps", 60) == 60.0
    assert render_preset._coerce("output_fps", 29.97) == 29.97


# ---------------------------------------------------------------------------
# job_config._validated_float_field: fraction stays str + range check
# ---------------------------------------------------------------------------


def test_job_config_output_fps_contracts_preserved():
    import job_config

    assert job_config._validated_float_field("output_fps", "30000/1001") == "30000/1001"
    assert job_config._validated_float_field("output_fps", "59.94") == 59.94
    assert job_config._validated_float_field("offset", "12.5") == 12.5

    with pytest.raises(ValueError, match="output_fps"):
        job_config._validated_float_field("output_fps", "30000/zero")
    with pytest.raises(ValueError, match="范围"):
        job_config._validated_float_field("output_fps", "999/1")
    with pytest.raises(ValueError, match="范围"):
        job_config._validated_float_field("output_fps", "0.1")


def test_job_config_output_fps_rejects_negative_fraction():
    import job_config

    with pytest.raises(ValueError):
        job_config._validated_float_field("output_fps", "-30000/1001")


# ---------------------------------------------------------------------------
# Cross-wrapper consistency: the four consumers agree on every input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["60", "29.97", "30000/1001", "24000/1001", " 59.94 ", "60/2"],
)
def test_all_consumers_agree_on_valid_inputs(text):
    import cli_spec
    import job_config
    import render_preset
    import twitch_chat_burn as burn

    _, expected = media_probe.parse_rational_fps_text(text)

    cli_value = cli_spec._download_output_fps(text)
    if isinstance(cli_value, str):
        # fraction passthrough: helper must reproduce the same float
        assert media_probe.parse_rational_fps_text(cli_value)[1] == pytest.approx(expected)
    else:
        assert cli_value == pytest.approx(expected)

    assert burn.parse_output_fps_arg(text) == pytest.approx(expected)

    preset_value = render_preset._coerce("output_fps", text)
    if isinstance(preset_value, str):
        assert media_probe.parse_rational_fps_text(preset_value)[1] == pytest.approx(expected)
    else:
        assert preset_value == pytest.approx(expected)

    job_value = job_config._validated_float_field("output_fps", text)
    if isinstance(job_value, str):
        assert media_probe.parse_rational_fps_text(job_value)[1] == pytest.approx(expected)
    else:
        assert job_value == pytest.approx(expected)


@pytest.mark.parametrize(
    "text",
    ["-30000/1001", "30000/0", "abc", "-29.97"],
)
def test_all_consumers_reject_invalid_inputs(text):
    import cli_spec
    import job_config
    import render_preset
    import twitch_chat_burn as burn

    with pytest.raises(argparse.ArgumentTypeError):
        cli_spec._download_output_fps(text)
    with pytest.raises(argparse.ArgumentTypeError):
        burn.parse_output_fps_arg(text)
    with pytest.raises(ValueError):
        render_preset._coerce("output_fps", text)
    with pytest.raises(ValueError):
        job_config._validated_float_field("output_fps", text)


@pytest.mark.parametrize(
    "text",
    ["", "nan", "inf", "0", "999/1"],
)
def test_job_config_rejects_invalid_or_out_of_range_like_helper_or_range(text):
    """job_config's contract: garbage/non-finite → type error; in-syntax but
    out-of-range (0 plain, 999/1) → range error. Either way, ValueError."""
    import job_config

    with pytest.raises(ValueError):
        job_config._validated_float_field("output_fps", text)


def test_zero_quotient_fraction_is_out_of_range_everywhere():
    """0/1 parses as 0.0 at the helper level (valid syntax); every consumer
    must still reject it via the plain-positive or range checks."""
    import cli_spec
    import job_config
    import render_preset
    import twitch_chat_burn as burn

    _, value = media_probe.parse_rational_fps_text("0/1")
    assert value == 0.0

    with pytest.raises(argparse.ArgumentTypeError):
        cli_spec._download_output_fps("0/1")
    with pytest.raises(argparse.ArgumentTypeError):
        burn.parse_output_fps_arg("0/1")
    with pytest.raises(ValueError):
        render_preset._coerce("output_fps", "0/1")
    with pytest.raises(ValueError):
        job_config._validated_float_field("output_fps", "0/1")


def test_single_helper_is_the_only_rational_fps_parser():
    """Drift guard: the duplicated _RATIONAL_FPS_RE copies are gone; wrappers
    must route through media_probe.parse_rational_fps_text."""
    import inspect

    import cli_spec
    import job_config
    import render_preset
    import twitch_chat_burn as burn

    for module in (cli_spec, render_preset, job_config):
        assert not hasattr(module, "_RATIONAL_FPS_RE"), module.__name__

    for fn in (
        cli_spec._download_output_fps,
        burn.parse_output_fps_arg,
        render_preset._coerce,
        job_config._validated_float_field,
    ):
        assert "parse_rational_fps_text" in inspect.getsource(fn), fn.__name__


def test_helper_message_mentions_valid_forms():
    with pytest.raises(ValueError) as exc_info:
        media_probe.parse_rational_fps_text("garbage")
    assert "60 / 29.97" in str(exc_info.value)
    assert "30000/1001" in str(exc_info.value)
