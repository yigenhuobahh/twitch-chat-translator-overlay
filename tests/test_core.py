#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for twitch_chat_cn_overlay scripts."""

import json
from pathlib import Path
import sys

# Add scripts dir to path
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class TestHTMLParsing:
    """Test HTML parsing via twitch_chat_burn imports."""

    def test_fixtures_exist(self):
        assert (FIXTURES_DIR / "test_chat.html").is_file()
        assert (FIXTURES_DIR / "test_translation.json").is_file()

    def test_html_has_comments(self):
        from bs4 import BeautifulSoup
        html = (FIXTURES_DIR / "test_chat.html").read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "html.parser")
        comments = soup.find_all("div", class_="comment")
        assert len(comments) == 5

    def test_html_has_emotes(self):
        from bs4 import BeautifulSoup
        html = (FIXTURES_DIR / "test_chat.html").read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "html.parser")
        emotes = soup.find_all("img", class_="emote-image")
        assert len(emotes) == 3
        titles = [e.get("title", "") for e in emotes]
        assert "LUL" in titles
        assert "Hey" in titles
        assert "xdx" in titles


class TestTranslationJSON:
    """Test translation JSON schema and lint."""

    def test_load_json(self):
        data = json.loads((FIXTURES_DIR / "test_translation.json").read_text(encoding="utf-8"))
        assert "messages" in data
        assert len(data["messages"]) == 5

    def test_message_fields(self):
        data = json.loads((FIXTURES_DIR / "test_translation.json").read_text(encoding="utf-8"))
        for msg in data["messages"]:
            assert "index" in msg
            assert "timestamp" in msg
            assert "author" in msg
            assert "original" in msg
            assert "translation" in msg

    def test_pure_emote_preserved(self):
        data = json.loads((FIXTURES_DIR / "test_translation.json").read_text(encoding="utf-8"))
        # Message index 2 is pure emote [Hey] [xdx]
        msg = data["messages"][2]
        assert msg["translation"] == msg["original"]

    def test_mention_preserved(self):
        data = json.loads((FIXTURES_DIR / "test_translation.json").read_text(encoding="utf-8"))
        msg = data["messages"][3]
        assert "@TestUser1" in msg["translation"]

    def test_url_preserved(self):
        data = json.loads((FIXTURES_DIR / "test_translation.json").read_text(encoding="utf-8"))
        msg = data["messages"][4]
        assert "https://example.com/link" in msg["translation"]


class TestLintTranslation:
    """Test lint_translation function from render_cn_chat."""

    def test_lint_clean_json(self):
        # Import lint_translation from render_cn_chat
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "render_cn_chat",
            str(SCRIPTS_DIR / "render_cn_chat.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        json_path = FIXTURES_DIR / "test_translation.json"
        issues = mod.lint_translation(json_path, max_chars=90)
        # Should have zero FAIL issues
        fail_count = sum(1 for i in issues if i["severity"] == "FAIL")
        assert fail_count == 0, f"Expected 0 FAIL issues, got {fail_count}: {[i for i in issues if i['severity'] == 'FAIL']}"

    def test_lint_empty_translation(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "render_cn_chat",
            str(SCRIPTS_DIR / "render_cn_chat.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Create a temp JSON with empty translation
        import tempfile
        data = {"messages": [{"index": 0, "timestamp": 0.0, "author": "Test", "original": "hello", "translation": ""}]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(data, f)
            tmp_path = Path(f.name)
        try:
            issues = mod.lint_translation(tmp_path, max_chars=90)
            fail_count = sum(1 for i in issues if i["severity"] == "FAIL")
            assert fail_count >= 1, "Expected at least 1 FAIL for empty translation"
        finally:
            tmp_path.unlink(missing_ok=True)


class TestReviewRoundtrip:
    """Test TSV export/import roundtrip."""

    def test_tsv_roundtrip(self):
        import importlib.util
        import tempfile
        spec = importlib.util.spec_from_file_location(
            "render_cn_chat",
            str(SCRIPTS_DIR / "render_cn_chat.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        json_path = FIXTURES_DIR / "test_translation.json"
        with tempfile.TemporaryDirectory() as tmpdir:
            tsv_path = Path(tmpdir) / "review.tsv"
            mod.export_review_tsv(json_path, tsv_path)
            assert tsv_path.is_file()

            # Modify a translation in JSON, then import from TSV should restore it
            data = json.loads(json_path.read_text(encoding="utf-8"))
            data["messages"][0]["translation"] = "MODIFIED"
            tmp_json = Path(tmpdir) / "modified.json"
            tmp_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

            mod.import_review_tsv(tmp_json, tsv_path)
            restored = json.loads(tmp_json.read_text(encoding="utf-8"))
            assert restored["messages"][0]["translation"] == "[LUL] 大家好！"


class TestEmotePlaceholderRemoval:
    """Test that [emote] placeholders are removed when matching images exist."""

    def test_placeholder_removal_logic(self):
        # The logic: given a translation text with [LUL] and the original message
        # has an <img title="LUL">, the [LUL] should be removed from translation.
        # We test the regex logic used in twitch_chat_burn.py
        import re

        # Simulate: original has emotes with titles ["LUL", "Hey", "xdx"]
        emote_titles = {"LUL", "Hey", "xdx"}

        # Case 1: translation "[LUL] Hello world!" -> "Hello world!"
        text = "[LUL] Hello world!"
        for title in emote_titles:
            text = re.sub(rf"\[{re.escape(title)}\]\s*", "", text)
        assert text == "Hello world!", f"Got: {text!r}"

        # Case 2: translation "游戏不错 [LUL]" -> "游戏不错 "
        text2 = "游戏不错 [LUL]"
        for title in emote_titles:
            text2 = re.sub(rf"\[{re.escape(title)}\]\s*", "", text2)
        assert text2.strip() == "游戏不错", f"Got: {text2!r}"

        # Case 3: non-emote bracket [together] should remain
        text3 = "[together] something"
        for title in emote_titles:
            text3 = re.sub(rf"\[{re.escape(title)}\]\s*", "", text3)
        assert text3 == "[together] something", f"Got: {text3!r}"


class TestRegressionGuards:
    """Regression guards for imported translations and frame sequences."""

    def test_clean_imported_translation_strips_model_metadata(self):
        import twitch_chat_burn as burn

        assert burn.clean_imported_translation("<example_user> 幸好我卡点到了……", "example_user") == "幸好我卡点到了……"
        assert burn.clean_imported_translation("[12] hello", "user") == "hello"
        assert burn.clean_imported_translation("example_user: hello", "example_user") == "hello"
        assert burn.clean_imported_translation("example_user：hello", "example_user") == "hello"
        assert burn.clean_imported_translation("普通译文", "example_user") == "普通译文"

    def test_detect_frame_start_number_handles_nonzero_sequence(self):
        import tempfile

        import twitch_chat_burn as burn

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "frame_06586.png").write_bytes(b"")
            (tmp / "frame_06587.png").write_bytes(b"")
            assert burn.detect_frame_start_number(tmp) == 6586

    def test_hex_to_rgb_accepts_short_and_invalid_colors(self):
        import twitch_chat_burn as burn

        assert burn.hex_to_rgb("#abc") == (170, 187, 204)
        assert burn.hex_to_rgb("#FF0000") == (255, 0, 0)
        assert burn.hex_to_rgb("not-a-color") == (255, 255, 255)
        assert burn.hex_to_rgb("") == (255, 255, 255)

    def test_global_frame_index_helpers_do_not_inflate_count(self):
        import twitch_chat_burn as burn

        duration = 10.0
        fps = 30
        total = burn.expected_overlay_frame_count(duration, fps)
        assert total == 300

        # Reproduce the old failure mode: many short segments with ceil()
        # would overshoot. Global index mapping must cover exactly 0..total.
        change_points = [0.0, 1.0 / 3, 2.0 / 3, 1.0, 2.5, 2.51, 10.0]
        covered = []
        for i in range(len(change_points) - 1):
            start_i, end_i = burn.frame_index_range(change_points[i], change_points[i + 1], fps, total)
            covered.extend(range(start_i, end_i))
        assert covered == list(range(total))
        assert len(covered) == total


class TestTwitchDownloaderParsing:
    """Parse TwitchDownloader-style HTML, including emotesv2 class names."""

    def test_parses_emotesv2_class_names(self):
        import tempfile

        import twitch_chat_burn as burn

        html_path = FIXTURES_DIR / "twitchdownloader_chat.html"
        with tempfile.TemporaryDirectory() as tmpdir:
            data = burn.parse_chat_html(str(html_path), tmpdir)
            assert len(data["messages"]) == 3

            classes = []
            for msg in data["messages"]:
                for frag in msg["fragments"]:
                    if frag["type"] == "emote":
                        classes.append(frag["class"])

            assert "first-508650" in classes
            assert "first-emotesv2_4e1c5651219a462894aefa8b6720efc5" in classes
            assert "third-01F63B8GJR000AE7CZT484KXF9" in classes
            assert "first-emotesv2_4e1c5651219a462894aefa8b6720efc5" in data["emote_map"]

            # Second message is pure emote and must not degrade into missing fragment.
            pure = data["messages"][1]
            assert pure["fragments"][0]["type"] == "emote"
            assert pure["fragments"][0]["title"] == "Hey"


class TestTranslationCleaning:
    """Guard translate_chat_openai text cleanup against URL/path damage."""

    def _load_translator(self):
        import importlib.util
        import types

        # Avoid hard dependency on the real openai package during unit tests.
        if "openai" not in sys.modules:
            fake = types.ModuleType("openai")

            class _OpenAI:
                def __init__(self, *args, **kwargs):
                    pass

            fake.OpenAI = _OpenAI
            sys.modules["openai"] = fake

        spec = importlib.util.spec_from_file_location(
            "translate_chat_openai",
            str(SCRIPTS_DIR / "translate_chat_openai.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_clean_translation_text_keeps_urls_and_paths(self):
        mod = self._load_translator()
        assert mod.clean_translation_text("https://example.com/a/b") == "https://example.com/a/b"
        assert mod.clean_translation_text("\u770b\u8fd9\u91cc https://twitch.tv/foo") == "\u770b\u8fd9\u91cc https://twitch.tv/foo"
        assert mod.clean_translation_text(r"C:\Users\foo/bar") == r"C:\Users\foo/bar"
        assert mod.clean_translation_text("and/or") == "and/or"
        # Mixed Latin/CJK with slash is not dual-candidate output.
        assert mod.clean_translation_text("A/B\u6d4b\u8bd5") == "A/B\u6d4b\u8bd5"

    def test_clean_translation_text_keeps_first_of_short_alternatives(self):
        mod = self._load_translator()
        assert mod.clean_translation_text("太强了/真厉害") == "太强了"
        assert mod.clean_translation_text("译文A / 译文B") == "译文A"


class TestOutputPublish:
    """Pipeline final-path publishing should create parents and replace atomically."""

    def test_publish_output_creates_parent_and_moves(self):
        import importlib.util
        import tempfile

        spec = importlib.util.spec_from_file_location(
            "render_cn_chat",
            str(SCRIPTS_DIR / "render_cn_chat.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "src.mp4"
            dst = Path(tmpdir) / "nested" / "out" / "final.mp4"
            src.write_bytes(b"fake-mp4-bytes")
            out = mod.publish_output(src, dst)
            assert out == dst
            assert dst.is_file()
            assert dst.read_bytes() == b"fake-mp4-bytes"
            assert not src.exists()


if __name__ == "__main__":
    # Allow running without pytest
    import traceback
    tests = [
        TestHTMLParsing,
        TestTranslationJSON,
        TestLintTranslation,
        TestReviewRoundtrip,
        TestEmotePlaceholderRemoval,
        TestRegressionGuards,
        TestTwitchDownloaderParsing,
        TestTranslationCleaning,
        TestOutputPublish,
    ]
    passed = 0
    failed = 0
    for test_class in tests:
        for method_name in dir(test_class):
            if method_name.startswith("test_"):
                try:
                    instance = test_class()
                    getattr(instance, method_name)()
                    print(f"  [PASS] {test_class.__name__}.{method_name}")
                    passed += 1
                except Exception as e:
                    print(f"  [FAIL] {test_class.__name__}.{method_name}: {e}")
                    traceback.print_exc()
                    failed += 1
    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
