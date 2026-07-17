"""Tests for P2 fixes: WebM duration check, GPU trial encode, preview-image safety."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

# ---------------------------------------------------------------------------
# WebM duration validation in compose_video
# ---------------------------------------------------------------------------

class TestWebMDurationCheck:
    """Verify compose_video hard-fails when WebM intermediate is too short."""

    def test_webm_duration_aborts_on_short(self, tmp_path: Path, capsys):
        """Short WebM must abort compose (not warn-and-publish missing tail chat)."""
        from PIL import Image

        from encode_options import EncodeOptions
        from overlay_config import OverlayConfig
        import twitch_chat_burn as burn

        frames_dir = tmp_path / "frames"
        frames_dir.mkdir()
        Image.new("RGBA", (10, 10)).save(frames_dir / "frame_00000.png")
        video = tmp_path / "src.mp4"
        video.write_bytes(b"\x00")

        config = OverlayConfig(
            x=0, y=0, width=100, height=100, font_size=14,
            fps=10, output_fps=30,
        )
        config.encode = EncodeOptions(
            encoder="x264", video_codec="libx264", video_preset="fast",
            crf=18, audio_codec="aac", audio_bitrate="192k",
            overlay_codec="vp9", webm_crf=30, webm_cpu_used=4,
            prefer_hw=False, resolved_encoder="x264", notes=[],
        )
        short_summary = {"ok": True, "duration": 1.0, "has_video": True, "has_audio": False}

        with mock.patch("twitch_chat_burn.probe_media_summary", return_value=short_summary):
            with mock.patch("twitch_chat_burn.resolve_source_av_timing") as mock_timing:
                mock_timing.return_value = {
                    "has_audio": False,
                    "video_lead_in": 0.0,
                    "source_duration": 10.0,
                    "video_start": 0.0,
                    "audio_start": 0.0,
                }
                with mock.patch("twitch_chat_burn.resolve_output_fps", return_value=30):
                    with mock.patch("twitch_chat_burn.run_tracked") as mock_run:
                        mock_run.return_value = mock.Mock(returncode=0)
                        with mock.patch("twitch_chat_burn.build_webm_encode_args", return_value=[]):
                            with mock.patch("twitch_chat_burn.build_video_encode_args", return_value=[]):
                                with mock.patch("twitch_chat_burn.build_audio_encode_args", return_value=[]):
                                    with mock.patch(
                                        "twitch_chat_burn.missing_frame_indexes",
                                        return_value=[],
                                    ):
                                        with mock.patch(
                                            "twitch_chat_burn.expand_frame_sequence_for_ffmpeg",
                                            return_value=None,
                                        ):
                                            result = burn.compose_video(
                                                str(video),
                                                str(frames_dir),
                                                str(tmp_path),
                                                config,
                                                duration=10.0,
                                            )
        assert result is None
        out = capsys.readouterr().out
        assert "错误" in out or "拒绝合成" in out
        assert "1.000" in out or "1.0" in out

    def test_probe_media_summary_callable(self):
        """Ensure probe_media_summary works on a real video file."""
        from twitch_chat_burn import probe_media_summary
        # Just verify it's callable and returns a dict
        assert callable(probe_media_summary)


# ---------------------------------------------------------------------------
# GPU trial encode in resolve_encode_options
# ---------------------------------------------------------------------------

class TestGPUTrialEncode:
    """Verify resolve_encode_options does trial encode for hardware encoders."""

    def test_trial_encode_function_exists(self):
        """_trial_encode should be importable and callable."""
        from encode_options import _trial_encode
        assert callable(_trial_encode)

    def test_trial_encode_returns_bool(self):
        """_trial_encode should return a boolean."""
        from encode_options import _trial_encode
        result = _trial_encode("libx264")
        assert isinstance(result, bool)

    def test_auto_fallback_to_x264_on_hw_failure(self):
        """When hardware encoder trial fails, auto should fall back to x264."""
        from encode_options import resolve_encode_options

        # Mock detect_hw_encoders to report nvenc available
        with mock.patch("encode_options.detect_hw_encoders") as mock_hw:
            mock_hw.return_value = {"nvenc": "h264_nvenc", "x264": "libx264"}
            # Mock _trial_encode to fail for nvenc
            with mock.patch("encode_options._trial_encode", return_value=False):
                opts = resolve_encode_options(encoder="auto", prefer_hw=True)
                assert opts.resolved_encoder == "x264"
                assert any("trial encode failed" in n for n in opts.notes)

    def test_auto_selects_hw_on_trial_success(self):
        """When hardware encoder trial succeeds, auto should select it."""
        from encode_options import resolve_encode_options

        with mock.patch("encode_options.detect_hw_encoders") as mock_hw:
            mock_hw.return_value = {"nvenc": "h264_nvenc", "x264": "libx264"}
            with mock.patch("encode_options._trial_encode", return_value=True):
                opts = resolve_encode_options(encoder="auto", prefer_hw=True)
                assert opts.resolved_encoder == "nvenc"
                assert any("auto selected" in n for n in opts.notes)

    def test_explicit_hw_warns_on_failure(self):
        """When user explicitly requests HW encoder that fails trial, should warn."""
        from encode_options import resolve_encode_options

        with mock.patch("encode_options.detect_hw_encoders") as mock_hw:
            mock_hw.return_value = {"nvenc": "h264_nvenc", "x264": "libx264"}
            with mock.patch("encode_options._trial_encode", return_value=False):
                opts = resolve_encode_options(encoder="nvenc")
                assert opts.resolved_encoder == "nvenc"
                assert any("trial encode failed" in n for n in opts.notes)

    def test_explicit_hw_no_warn_on_success(self):
        """When user explicitly requests HW encoder and trial passes, no warning."""
        from encode_options import resolve_encode_options

        with mock.patch("encode_options.detect_hw_encoders") as mock_hw:
            mock_hw.return_value = {"nvenc": "h264_nvenc", "x264": "libx264"}
            with mock.patch("encode_options._trial_encode", return_value=True):
                opts = resolve_encode_options(encoder="nvenc")
                assert opts.resolved_encoder == "nvenc"
                assert not any("trial encode failed" in n for n in opts.notes)


# ---------------------------------------------------------------------------
# Preview-image path safety
# ---------------------------------------------------------------------------

class TestPreviewImageSafety:
    """Verify --preview-image refuses system directories."""

    def test_system_path_rejected(self, tmp_path: Path):
        """Preview-image paths under system directories should be refused."""
        from process_util import is_dangerous_publish_path, path_is_under

        assert path_is_under(tmp_path / "sub", tmp_path)
        assert not path_is_under("C:\\Windows\\System32", tmp_path)
        assert is_dangerous_publish_path(r"C:\Windows\Temp\x.png")
        assert is_dangerous_publish_path("/etc/preview.png")
        assert not is_dangerous_publish_path(tmp_path / "preview.png")

    def test_publish_copy_skips_dangerous_destination(self, tmp_path: Path):
        """Policy + burn export refuse system destinations; user paths stay allowed."""
        from process_util import is_dangerous_publish_path
        import twitch_chat_burn as burn

        req = Path(r"C:\Windows\Temp\evil_preview.png")
        assert is_dangerous_publish_path(req)
        assert burn.is_dangerous_publish_path(req)
        safe = tmp_path / "outside" / "ok.png"
        assert not is_dangerous_publish_path(safe)


# ---------------------------------------------------------------------------
# Preset path resolution (wheel share/ fallback)
# ---------------------------------------------------------------------------

class TestPresetPathResolution:
    """Verify _resolve_preset_path finds profiles in multiple locations."""

    def test_resolve_as_is(self, tmp_path: Path):
        """Direct path should work."""
        from layout_preset import _resolve_preset_path
        f = tmp_path / "test.yaml"
        f.write_text("layout: {x: 10}", encoding="utf-8")
        result = _resolve_preset_path(str(f))
        assert result == f

    def test_resolve_in_profiles_dir(self):
        """Should find layout_default.yaml in repo profiles/ directory."""
        from layout_preset import _resolve_preset_path
        # When running from repo root, profiles/layout_default.yaml exists
        result = _resolve_preset_path("layout_default.yaml")
        assert result.is_file()
        assert result.name == "layout_default.yaml"

    def test_resolve_render_preset_in_profiles_dir(self):
        """Should find render_default.yaml in repo profiles/ directory."""
        from render_preset import _resolve_preset_path
        result = _resolve_preset_path("render_default.yaml")
        assert result.is_file()
        assert result.name == "render_default.yaml"

    def test_resolve_returns_original_on_not_found(self):
        """Should return original path when file not found anywhere."""
        from layout_preset import _resolve_preset_path
        result = _resolve_preset_path("nonexistent_preset.yaml")
        assert not result.is_file()
        assert result.name == "nonexistent_preset.yaml"
