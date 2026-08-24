from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from overlay_config import OverlayConfig
from overlay_scene import AUTO_LAZY_MESSAGE_THRESHOLD, OverlayScenePlan


def test_scene_plan_resolves_preview_float_budget_and_automatic_cache():
    config = OverlayConfig(
        fps=30,
        height=100,
        font_size=16,
        max_visible=20,
        stack_mode="float",
        preview_clip=8,
        preview_clip_start=30,
        preview_frame=99,
        message_image_cache_size=4,
    )

    plan = OverlayScenePlan.from_config(
        source_duration=12,
        config=config,
        message_count=AUTO_LAZY_MESSAGE_THRESHOLD,
    )

    assert plan.duration == 8
    assert plan.preview_time == 8
    assert plan.total_frames == 1
    assert plan.stack_mode == "float"
    assert plan.max_visible == plan.auto_capacity
    assert plan.budget_warning is not None
    assert plan.float_throttle_from == 0
    assert plan.lazy_message_images is True
    assert plan.message_image_cache_size == 8
    assert plan.auto_lazy_message_images is True


def test_scene_plan_keeps_full_frame_budget_without_preview_mode():
    config = OverlayConfig(fps=15, height=363, font_size=15, stack_mode="unexpected")

    plan = OverlayScenePlan.from_config(source_duration=3.01, config=config, message_count=5)

    assert plan.stack_mode == "lanes"
    assert plan.preview_frame_time is None
    assert plan.total_frames == 46
    assert plan.lazy_message_images is False
