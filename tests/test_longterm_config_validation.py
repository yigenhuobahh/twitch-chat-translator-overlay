from __future__ import annotations

from pathlib import Path
import re

import pytest

import render_cn_chat as pipeline


def _yaml(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("- not-a-mapping\n", "root must be a mapping"),
        ("normalizations: null\n", "normalizations must be a list"),
        ("normalizations:\n  - bad-item\n", "normalizations[0] must be a mapping"),
        (
            "normalizations:\n  - match: {nested: value}\n    translation: x\n",
            "normalizations[0].match must be a string or list",
        ),
        ("preserve_patterns: bad\n", "preserve_patterns must be a list"),
        ("preserve_patterns: ['[']\n", "not a valid regex"),
    ],
)
def test_rules_yaml_schema_errors_are_actionable(
    tmp_path: Path,
    payload: str,
    message: str,
) -> None:
    path = _yaml(tmp_path, payload)
    with pytest.raises(pipeline.PipelineError, match=re.escape(message)):
        pipeline.load_yaml_rules(path)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("glossary: []\n", "glossary must be a mapping"),
        ("preserve: {}\n", "preserve must be a list"),
        ("translation_style: []\n", "translation_style must be a mapping"),
    ],
)
def test_profile_yaml_schema_errors_are_actionable(
    tmp_path: Path,
    payload: str,
    message: str,
) -> None:
    path = _yaml(tmp_path, payload)
    with pytest.raises(pipeline.PipelineError, match=re.escape(message)):
        pipeline.load_profile(path)


def test_yaml_syntax_error_is_wrapped_without_raw_parser_exception(tmp_path: Path) -> None:
    path = _yaml(tmp_path, "normalizations: [unterminated\n")
    with pytest.raises(pipeline.PipelineError, match=r"Invalid .* YAML"):
        pipeline.load_yaml_rules(path)


@pytest.mark.parametrize("label", ["规则", "Profile"])
def test_missing_yaml_file_raises_pipeline_error_not_bare_system_exit(
    tmp_path: Path, label: str
) -> None:
    """PARSE-N1: 上层 except PipelineError 必须能接住缺文件（PipelineError 继承
    SystemExit，退出码链保持不变）。"""
    missing = tmp_path / "nope.yaml"
    with pytest.raises(pipeline.PipelineError, match="文件不存在"):
        pipeline.load_yaml_file(missing, label)


def test_valid_rules_yaml_still_loads(tmp_path: Path) -> None:
    path = _yaml(
        tmp_path,
        """
normalizations:
  - name: hello
    match: [hello, hi]
    translation: greeting
preserve_patterns:
  - '^@'
""".lstrip(),
    )
    loaded = pipeline.load_yaml_rules(path)
    assert loaded["normalizations"] == [
        {
            "name": "hello",
            "match": {"hello", "hi"},
            "translation": "greeting",
        }
    ]
    assert loaded["preserve_patterns"][0].search("@user")
