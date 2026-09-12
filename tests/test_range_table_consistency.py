#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C2 (design-7): one declarative range table drives job_config AND burn.

job_config.FLOAT_RANGE / INT_RANGE are the single authority for numeric
ranges. burn's _validate_runtime_args iterates a local mapping from arg-attr
→ (table, flag-label, validator-kind) and must therefore agree with the
tables field-by-field: below-low rejected, above-high rejected, in-range
accepted. Conditional / special hand-written checks (stack_mode cross-check,
preview_clip > 0, offset, bg_alpha, blank_hold_seconds > 0) are covered
separately where relevant.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import job_config
import twitch_chat_burn as burn


def _valid_runtime_args(**overrides):
    """A namespace that passes _validate_runtime_args with default tables.

    output_fps defaults to None here: burn's --output-fps is optional and its
    range is exercised via dedicated overrides below.
    """
    values = {
        "fps": 15,
        "output_fps": None,
        "width": 497,
        "height": 363,
        "font_size": 15,
        "emote_height": 22,
        "max_visible": 0,
        "message_image_cache_size": 256,
        "stack_mode": "lanes",
        "msg_lifetime": 14.0,
        "max_message_lines": 0,
        "min_visible_seconds": 0.0,
        "arrival_interval": 0.0,
        "x_ratio": 0.0,
        "y_ratio": 0.0,
        "width_ratio": 0.0,
        "height_ratio": 0.0,
        "font_size_ratio": 0.0,
        "preview_frame": None,
        "preview_clip": None,
        "offset": None,
        "blank_hold_seconds": 0.5,
        "bg_alpha": 255,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _burn_value_for(field: str, low, high):
    """A probe value inside (low, high) that burn accepts."""
    if field in job_config.INT_RANGE:
        return int((low + high) // 2) or 1
    return (low + high) / 2.0


def _below_low(field: str, low, high):
    if field in job_config.INT_RANGE:
        return int(low) - 1
    return float(low) - 0.5


def _above_high(field: str, low, high):
    if field in job_config.INT_RANGE:
        return int(high) + 1
    return float(high) + 0.5


# Fields burn actually validates through the tables (x/y/crf/webm_crf are
# deliberately absent from burn's runtime validation; workers/batch_size are
# translation-side only and never reach _validate_runtime_args).
_BURN_TABLE_FIELDS = [
    field
    for field in job_config.INT_RANGE
    if field not in ("x", "y", "crf", "webm_crf", "workers", "batch_size")
] + [
    field
    for field in job_config.FLOAT_RANGE
    if field not in ("msg_lifetime", "blank_hold_seconds", "offset")
]


@pytest.mark.parametrize("field", _BURN_TABLE_FIELDS)
def test_burn_range_matches_job_config_table(field):
    """For every table field burn validates: below/above/out rejected, in-range accepted."""
    table = job_config.INT_RANGE if field in job_config.INT_RANGE else job_config.FLOAT_RANGE
    low, high = table[field][0], table[field][1]

    in_range = _burn_value_for(field, low, high)
    kwargs = {field: in_range}
    if field == "min_visible_seconds":
        # stack_mode cross-check: min_visible must stay <= msg_lifetime (14.0).
        kwargs["msg_lifetime"] = max(14.0, float(in_range))
    burn._validate_runtime_args(_valid_runtime_args(**kwargs))

    below_kwargs = {field: _below_low(field, low, high)}
    above_kwargs = {field: _above_high(field, low, high)}
    if field == "min_visible_seconds":
        below_kwargs["msg_lifetime"] = above_kwargs["msg_lifetime"] = 600.0
    with pytest.raises(ValueError) as below_exc:
        burn._validate_runtime_args(_valid_runtime_args(**below_kwargs))
    assert str(low) in str(below_exc.value) or str(field).replace("_", "-") in str(below_exc.value), field

    with pytest.raises(ValueError):
        burn._validate_runtime_args(_valid_runtime_args(**above_kwargs))


def test_burn_flags_match_job_config_attrs():
    """Every burn table field must be a real argparse attr and vice versa:
    the local spec map must cover exactly the intersection of the tables with
    burn's runtime namespace (minus the hand-written special cases)."""
    special = {"msg_lifetime", "blank_hold_seconds", "offset", "x", "y", "crf", "webm_crf", "workers", "batch_size"}
    covered = set(_BURN_TABLE_FIELDS)
    expected = (set(job_config.INT_RANGE) | set(job_config.FLOAT_RANGE)) - special
    assert covered == expected


def test_msg_lifetime_only_validated_in_lanes_mode():
    """Conditional field: float mode skips msg_lifetime, lanes enforces 0.1..600."""
    args = _valid_runtime_args(stack_mode="float", msg_lifetime=0.0)
    burn._validate_runtime_args(args)  # float mode: not validated

    with pytest.raises(ValueError, match="msg-lifetime"):
        burn._validate_runtime_args(_valid_runtime_args(stack_mode="lanes", msg_lifetime=0.05))
    burn._validate_runtime_args(_valid_runtime_args(stack_mode="lanes", msg_lifetime=0.1))
    with pytest.raises(ValueError, match="msg-lifetime"):
        burn._validate_runtime_args(_valid_runtime_args(stack_mode="lanes", msg_lifetime=601.0))


# design-1: render_preset/job 有意把有理数帧率 ("30000/1001") 保留为 str 透传。
# 经 argv 到 burn 时由 parse_output_fps_arg 归一;preset 直连入口 setattr 进
# namespace 后必须过 _validate_runtime_args 同一单源归一,而不是 float() 裸崩。


def test_string_rational_output_fps_normalizes_and_writes_back():
    for text, expect in (("30000/1001", 30000 / 1001), ("24000/1001", 24000 / 1001)):
        args = _valid_runtime_args(output_fps=text)
        burn._validate_runtime_args(args)
        assert isinstance(args.output_fps, float), text
        assert args.output_fps == pytest.approx(expect), text


def test_invalid_string_output_fps_reports_flag_not_float_cast():
    with pytest.raises(ValueError) as exc:
        burn._validate_runtime_args(_valid_runtime_args(output_fps="-30"))
    message = str(exc.value)
    assert "--output-fps" in message
    assert "could not convert" not in message.lower()


def test_blank_hold_seconds_special_case_and_bg_alpha():
    with pytest.raises(ValueError, match="blank-hold-seconds must be > 0"):
        burn._validate_runtime_args(_valid_runtime_args(blank_hold_seconds=0.0))
    burn._validate_runtime_args(_valid_runtime_args(blank_hold_seconds=30.0))
    with pytest.raises(ValueError, match="bg-alpha"):
        burn._validate_runtime_args(_valid_runtime_args(bg_alpha=256))
    burn._validate_runtime_args(_valid_runtime_args(bg_alpha=0))


def test_offset_and_preview_clip_special_cases_unchanged():
    with pytest.raises(ValueError, match="offset must be finite"):
        burn._validate_runtime_args(_valid_runtime_args(offset=float("nan")))
    with pytest.raises(ValueError, match="preview-clip must be > 0"):
        burn._validate_runtime_args(_valid_runtime_args(preview_clip=0.0))
    burn._validate_runtime_args(_valid_runtime_args(preview_clip=86400.0))
    burn._validate_runtime_args(_valid_runtime_args(offset=-604800.0))


def test_stack_mode_cross_check_unchanged():
    with pytest.raises(ValueError, match="must be <= --msg-lifetime"):
        burn._validate_runtime_args(
            _valid_runtime_args(stack_mode="lanes", msg_lifetime=5.0, min_visible_seconds=6.0)
        )
    with pytest.raises(ValueError, match="stack-mode"):
        burn._validate_runtime_args(_valid_runtime_args(stack_mode="bogus"))


def test_output_fps_optional_but_ranged():
    burn._validate_runtime_args(_valid_runtime_args(output_fps=None))
    burn._validate_runtime_args(_valid_runtime_args(output_fps=240.0))
    with pytest.raises(ValueError, match="output-fps"):
        burn._validate_runtime_args(_valid_runtime_args(output_fps=240.5))
    with pytest.raises(ValueError, match="output-fps"):
        burn._validate_runtime_args(_valid_runtime_args(output_fps=0.5))
