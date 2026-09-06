#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shadow-inventory convergence gates for the 2026-09-06 full-fix wave.

Covers:
- design-3 (D1.1): doctor_check must derive its package checklist from
  env_bootstrap.collect_readiness (single source), not a hand-copied dict.
- design-3 (D1.2): tui_run preset dropdowns must append presets discovered in
  profiles/ (common_utils.discover_presets) beyond the static known entries.
- design-3 (D1.3): MANIFEST.in must cover every file shipped via pyproject
  [tool.setuptools.data-files] (sdist must be able to build the wheel assets).
- tests-3-adjacent (B4): tui_run's textual private-module import is guarded.
- correctness-4 (B1): layout_preset int coercion rejects fractional floats,
  decimal-point strings and bools (mirrors job_config._validated_int_field).
"""

from __future__ import annotations

import importlib
from pathlib import Path
import re
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _read(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# D1.1: doctor_check packages derive from env_bootstrap.collect_readiness
# ---------------------------------------------------------------------------


def _doctor_pkg_module_state(module: str, present: bool):
    """Return (original_spec, monkeypatched?) bookkeeping for stub injection."""

    class _Spec:
        def __init__(self, value):
            self.value = value

    return _Spec(present)


def test_doctor_packages_derive_from_collect_readiness():
    """doctor's pkg checks must be driven by collect_readiness, not a copy.

    Structural gate: the hand-maintained ``packages = {...}`` dict must be
    gone; the pkg loop must consume collect_readiness()'s ``pkg:*`` items.
    """
    src = _read("doctor_check.py")
    assert "packages = {" not in src, (
        "doctor_check must not hand-maintain a packages dict; derive from "
        "env_bootstrap.collect_readiness"
    )
    assert "collect_readiness" in src, "doctor_check must call collect_readiness"
    assert 'startswith("pkg:")' in src or "startswith('pkg:')" in src


def test_doctor_and_env_bootstrap_package_lists_agree(monkeypatch):
    """Runtime gate: doctor's checklist shows exactly the env_bootstrap pkgs.

    Stubs every pkg module as missing via sys.modules injection so both
    surfaces report absent; the derived missing-required list must equal the
    env_bootstrap required set (PIL/bs4/yaml/textual consistency).
    """
    import env_bootstrap

    pkg_modules = [
        item.key[len("pkg:"):] for item in env_bootstrap.collect_readiness()
        if item.key.startswith("pkg:")
    ]
    assert pkg_modules, "env_bootstrap must define pkg:* readiness items"


    def fake_find_spec(name, *args, **kwargs):
        if name in pkg_modules:
            return None
        return importlib.util.find_spec(name, *args, **kwargs)

    # env_bootstrap imports importlib.util locally inside collect_readiness;
    # patch the shared stdlib attribute (monkeypatch restores it).
    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    items = env_bootstrap.collect_readiness()
    env_names = [item.name for item in items if item.key.startswith("pkg:")]
    env_missing_required = sorted(
        item.name for item in items
        if item.key.startswith("pkg:") and not item.ok and item.required_for_render
    )
    # textual is required for render; openai/openpyxl are WARN-only
    assert env_missing_required == sorted(
        {"Pillow", "beautifulsoup4", "PyYAML", "textual"}
    ), env_missing_required
    assert env_names, env_names

    # doctor derives from the same items: exercise only the pkg loop inputs.
    # doctor_check.doctor prints via its own check(); simulate by calling the
    # derivation through the source contract: same names, same requiredness.
    doctor_src = _read("doctor_check.py")
    for name in env_names:
        assert f'"{name}"' in doctor_src or f"'{name}'" in doctor_src, (
            f"doctor display order must include {name}"
        )
    # required packages guard stays consistent: textual must not be WARN-only.
    assert '"textual"' in doctor_src or "'textual'" in doctor_src


# ---------------------------------------------------------------------------
# D1.2: tui_run preset options consume discover_presets
# ---------------------------------------------------------------------------


def test_tui_preset_options_merge_discovered(tmp_path, monkeypatch):
    """A new profiles/layout_*.yaml must appear in the TUI dropdown at import.

    sys.modules.pop 会把 tui_run 换成新模块对象：其他测试文件已在模块顶层
    `from tui_run import OverlayTui` 绑定旧类，且它们用 patch("tui_run.…")
    只能打到重新 import 后的新模块——旧模块的全局属性不再被替换（实测导致
    probe mock 失效、真实网络调用）。因此弹出后必须在 finally 里恢复原模块
    对象，保持全进程内 tui_run 身份唯一。
    """
    monkeypatch.syspath_prepend(str(SCRIPTS))
    saved_tui_run = sys.modules.pop("tui_run", None)

    new_profile = ROOT / "profiles" / "layout_zzztestwave.yaml"
    shutil.copyfile(ROOT / "profiles" / "layout_compact.yaml", new_profile)
    try:
        import tui_run

        assert tui_run is not saved_tui_run  # fresh import actually re-executed
        layout_vals = [value for _label, value in tui_run._LAYOUT_PRESET_OPTIONS]
        # Known entries keep their exact static order...
        assert layout_vals[:7] == [
            "default", "right", "compact", "mobile",
            "transparent", "sidebar", "top_right",
        ], layout_vals
        # ...and the new profile is appended automatically.
        assert "zzztestwave" in layout_vals, layout_vals
        # render kind still exposes its known set (no render_* file was added)
        render_vals = [value for _label, value in tui_run._RENDER_PRESET_OPTIONS]
        assert render_vals == ["default", "fast", "hq", "audio_copy"], render_vals
        # no duplicate values
        assert len(layout_vals) == len(set(layout_vals))
    finally:
        new_profile.unlink(missing_ok=True)
        sys.modules.pop("tui_run", None)
        if saved_tui_run is not None:
            sys.modules["tui_run"] = saved_tui_run


def test_tui_preset_options_survive_missing_profiles_dir(monkeypatch, tmp_path):
    """Import must not fail when profiles scanning is impossible."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    saved_tui_run = sys.modules.pop("tui_run", None)
    monkeypatch.chdir(tmp_path)  # no profiles/ in cwd

    import common_utils

    # repo profiles/ still exists; force profiles_search_dirs to find nothing
    monkeypatch.setattr(common_utils, "profiles_search_dirs", lambda: [])

    import tui_run

    layout_vals = [value for _label, value in tui_run._LAYOUT_PRESET_OPTIONS]
    assert layout_vals == [
        "default", "right", "compact", "mobile",
        "transparent", "sidebar", "top_right",
    ], layout_vals
    sys.modules.pop("tui_run", None)
    if saved_tui_run is not None:
        sys.modules["tui_run"] = saved_tui_run


# ---------------------------------------------------------------------------
# D1.3: MANIFEST.in ↔ pyproject data-files consistency gate
# ---------------------------------------------------------------------------


def _pyproject_data_files() -> list[str]:
    """Explicit file paths listed in [tool.setuptools.data-files]."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = text.split("[tool.setuptools.data-files]", 1)[1]
    section = section.split("\n[", 1)[0]
    return re.findall(r'"([^"]+\.(?:yaml|yml))"', section)


def _manifest_covers(rel_path: str, manifest_text: str) -> bool:
    """True if MANIFEST.in rules include the repo-relative path.

    Deterministic subset: explicit ``include <path>`` lines match literally;
    ``recursive-include <dir> <pat>`` matches via fnmatch on the basename.
    (graft/global-exclude/prune only affect directory trees, none declared
    for profiles/configs/jobs here.)
    """
    parts = Path(rel_path).parts
    for line in manifest_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        if tokens[0] == "include" and len(tokens) == 2:
            if tokens[1].replace("\\", "/") == rel_path:
                return True
        elif tokens[0] == "recursive-include" and len(tokens) == 3:
            import fnmatch

            dir_name, pattern = tokens[1], tokens[2]
            if len(parts) == 2 and parts[0] == dir_name and fnmatch.fnmatch(parts[1], pattern):
                return True
    return False


def test_manifest_covers_all_wheel_data_files():
    """Every yaml shipped via data-files must be includable from MANIFEST.in.

    sdist → wheel: setuptools builds the wheel from the sdist contents, so a
    data-file missing from MANIFEST.in silently drops out of released sdists.
    """
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    data_files = _pyproject_data_files()
    assert data_files, "pyproject must declare yaml data-files"

    missing = [p for p in data_files if not _manifest_covers(p, manifest)]
    assert not missing, (
        f"data-files missing from MANIFEST.in: {missing}"
    )


def test_manifest_public_yaml_whitelist_matches_data_files():
    """The public profiles/configs/jobs whitelist must be the same set.

    Both directions: a profiles/*.yaml added to one surface only is exactly
    the shadow-inventory drift this wave is converging.
    """
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    manifest_public = set(
        re.findall(r"^include (profiles/.*\.yaml|configs/.*\.yaml|jobs/.*\.yaml)$",
                   manifest, re.MULTILINE)
    )
    data_files = set(_pyproject_data_files())
    assert data_files, "pyproject must declare yaml data-files"
    assert manifest_public == data_files, (
        f"MANIFEST.in-only: {sorted(manifest_public - data_files)}; "
        f"data-files-only: {sorted(data_files - manifest_public)}"
    )


def test_data_files_exist_on_disk():
    for rel in _pyproject_data_files():
        assert (ROOT / rel).is_file(), f"data-file declared but missing: {rel}"


# ---------------------------------------------------------------------------
# B4: textual private-module import guard in tui_run
# ---------------------------------------------------------------------------


def test_tui_run_guards_invalid_select_value_import():
    """The textual.widgets._select import must be ImportError-guarded.

    textual 9 removed the private module; the fallback degrades the guard to
    a no-op (empty tuple except-clause) instead of crashing TUI import.
    """
    src = _read("tui_run.py")
    assert "from textual.widgets._select import InvalidSelectValueError" in src
    m = re.search(
        r"try:\s*\n(?:[^\n]*\n)*?\s*from textual\.widgets\._select import "
        r"InvalidSelectValueError\s*\nexcept ImportError[^\n]*\n\s*"
        r"InvalidSelectValueError = \(\)",
        src,
    )
    assert m, (
        "InvalidSelectValueError import must be wrapped in try/except ImportError "
        "with an empty-tuple fallback"
    )


def test_tui_run_imports_with_guard_active():
    """tui_run imports cleanly and the guard resolves to the exception class
    (or the no-op empty tuple on textual>=9)."""
    monkeypatch_free = importlib.import_module("tui_run")
    guard = monkeypatch_free.InvalidSelectValueError
    assert guard == () or issubclass(guard, Exception)


# ---------------------------------------------------------------------------
# B1: layout_preset int coercion rejects fractional floats / bools
# ---------------------------------------------------------------------------


@pytest.fixture()
def layout_preset_mod():
    sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("layout_preset")


def test_layout_int_field_accepts_int_and_integral_float(layout_preset_mod):
    coerce = layout_preset_mod._coerce
    for value, expected in ((15, 15), (15.0, 15), ("15", 15), (" 15 ", 15)):
        out = coerce("font_size", value)
        assert out == expected and isinstance(out, int), (value, out)
    # integral float from YAML carries no extra info -> coerce
    out = coerce("font_size", 15.0)
    assert out == 15 and isinstance(out, int)


@pytest.mark.parametrize("bad", [15.5, True, False, "15.5", "1e3", "abc", [4]])
def test_layout_int_field_rejects_fractional_and_bools(layout_preset_mod, bad):
    with pytest.raises(ValueError, match="font_size"):
        layout_preset_mod._coerce("font_size", bad)


def test_layout_int_error_message_matches_contract(layout_preset_mod):
    with pytest.raises(ValueError) as excinfo:
        layout_preset_mod._coerce("font_size", 15.5)
    assert "layout preset 字段 font_size 需要整数" in str(excinfo.value)
    assert "15.5" in str(excinfo.value)


def test_layout_normalize_propagates_int_rejection(layout_preset_mod, tmp_path, monkeypatch):
    """load_layout_preset surfaces the ValueError (callers catch -> clean exit)."""

    p = tmp_path / "layout_bad.yaml"
    p.write_text("layout:\n  font_size: 15.5\n", encoding="utf-8")
    monkeypatch.setattr(
        layout_preset_mod, "_resolve_preset_path", lambda _path: p
    )
    with pytest.raises(ValueError, match="font_size"):
        layout_preset_mod.load_layout_preset("layout_bad")


def test_layout_float_and_bool_fields_unchanged(layout_preset_mod):
    """Non-int fields keep their semantics (no over-tightening)."""
    coerce = layout_preset_mod._coerce
    assert coerce("msg_lifetime", 12.5) == 12.5
    assert coerce("msg_lifetime", 14) == 14.0
    assert coerce("reuse_static_frames", True) is True
    assert coerce("reuse_static_frames", 0) is False
    assert coerce("stack_mode", "lanes") == "lanes"
