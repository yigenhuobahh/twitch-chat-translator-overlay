#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for same-VOD multi-segment chat merge / time remap."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _seg_html(messages: list[tuple[int, str, str]], *, emote_class: str = "first-1") -> str:
    """Build minimal TD HTML. messages: (stream_seconds, author, text)."""
    style = (
        f'.{emote_class} {{ content:url("data:image/png;base64,{_TINY_PNG_B64}"); }}'
    )
    lines = []
    for sec, author, text in messages:
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        t_q = f"{h}h{m}m{s}s"
        t_d = f"{h}:{m:02d}:{s:02d}"
        lines.append(
            f'<pre class="comment-root">[<a href="https://www.twitch.tv/videos/9?t={t_q}">'
            f'{t_d}</a>] <span class="comment-author">{author}</span>'
            f'<span class="comment-message">: {text} '
            f'<img class="emote-image {emote_class}" title="LUL">'
            f'<span class="text-hide">LUL</span></span></pre>'
        )
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><style>\n"
        + style
        + "\n</style></head><body>\n"
        + "\n".join(lines)
        + "\n</body></html>\n"
    )


def test_parse_td_time_formats():
    from twitch_download import TwitchDownloadError, format_td_t_seconds, parse_td_time

    assert parse_td_time("100") == 100.0
    assert parse_td_time("100s") == 100.0
    assert parse_td_time("1m40s") == 100.0
    assert parse_td_time("0h1m40s") == 100.0
    assert parse_td_time("0:01:40") == 100.0
    assert parse_td_time("1:40") == 100.0
    assert parse_td_time("0:10:00") == 600.0
    with pytest.raises(TwitchDownloadError):
        parse_td_time("")
    with pytest.raises(TwitchDownloadError):
        parse_td_time("not-a-time")
    assert format_td_t_seconds(746) == ("0h12m26s", "0:12:26")
    assert format_td_t_seconds(-1) == ("0h0m0s", "0:00:00")


def test_parse_segment_line_and_validate():
    from twitch_download import (
        CropSegment,
        TwitchDownloadError,
        make_crop_segment,
        parse_segment_line,
        validate_segments,
    )
    assert parse_segment_line("  ") is None
    seg = parse_segment_line("0:10:00 0:12:30")
    assert seg is not None and seg.begin_s == 600 and seg.end_s == 750
    seg2 = parse_segment_line("0:10:00-0:12:30")
    assert seg2 is not None and seg2.begin_s == 600
    with pytest.raises(TwitchDownloadError, match="终点"):
        make_crop_segment("100s", "50s")
    with pytest.raises(TwitchDownloadError, match="未输入"):
        validate_segments([])
    validate_segments(
        [
            CropSegment("0:10:00", "0:12:00", 600, 720),
            CropSegment("0:11:00", "0:13:00", 660, 780),  # overlap → warn only
        ]
    )


def test_normalize_cut_ranges_merges_overlaps_on_original_timeline():
    from twitch_download import normalize_cut_ranges

    cuts = normalize_cut_ranges(
        [(90.0, 95.0), (10.0, 20.0), (15.0, 25.0), (-5.0, 2.0), (120.0, 130.0)],
        total_duration=100.0,
    )
    assert cuts == [(0.0, 2.0), (10.0, 25.0), (90.0, 95.0)]
    assert 100.0 - sum(end - start for start, end in cuts) == 78.0


def test_merge_chat_html_remaps_timestamps(tmp_path: Path):
    from chat_parser import parse_chat_html
    from twitch_download import (
        CropSegment,
        SegmentDownload,
        merge_chat_html,
        validate_chat_html,
    )

    # Seg A: begin=100, D=20 → stream 100,110 → merged 0,10
    html_a = _seg_html([(100, "Alice", "a1"), (110, "Bob", "a2")], emote_class="first-1")
    # Seg B: begin=500, D=30 → stream 500,505 → merged 20,25
    html_b = _seg_html([(500, "Carol", "b1"), (505, "Dave", "b2")], emote_class="first-2")
    path_a = tmp_path / "seg_00.html"
    path_b = tmp_path / "seg_01.html"
    path_a.write_text(html_a, encoding="utf-8")
    path_b.write_text(html_b, encoding="utf-8")
    # Dummy videos not needed for merge_chat_html
    segs = [
        SegmentDownload(
            index=0,
            segment=CropSegment("100s", "120s", 100.0, 120.0),
            video_path=tmp_path / "seg_00.mp4",
            chat_html_path=path_a,
            duration_s=20.0,
        ),
        SegmentDownload(
            index=1,
            segment=CropSegment("500s", "530s", 500.0, 530.0),
            video_path=tmp_path / "seg_01.mp4",
            chat_html_path=path_b,
            duration_s=30.0,
        ),
    ]
    out = tmp_path / "chat.html"
    merge_chat_html(segs, source_id="123456789", out_path=out)
    validate_chat_html(out)
    data = parse_chat_html(str(out), str(tmp_path / "parse_out"))
    stamps = [m["timestamp"] for m in data["messages"]]
    authors = [m["author"] for m in data["messages"]]
    assert stamps == [0, 10, 20, 25]
    assert authors == ["Alice", "Bob", "Carol", "Dave"]
    # Both emote classes should be present
    assert "first-1" in data["emote_map"] or any(
        "first-1" in str(f) for m in data["messages"] for f in m.get("fragments") or []
    )


def test_remap_drops_outliers():
    from twitch_download import remap_comment_block

    block = (
        '<pre class="comment-root">[<a href="https://www.twitch.tv/videos/1?t=0h0m5s">0:00:05</a>] '
        '<span class="comment-author">X</span><span class="comment-message">: hi</span></pre>'
    )
    # begin=100 → rel = 5-100 = -95 → drop
    assert remap_comment_block(block, begin_s=100, cum_s=0, duration_s=20) is None
    # In window
    block2 = block.replace("0h0m5s", "0h1m45s").replace("0:00:05", "0:01:45")  # 105s
    got = remap_comment_block(block2, begin_s=100, cum_s=50, duration_s=20)
    assert got is not None
    new_block, merged = got
    assert merged == 55.0
    assert "t=0h0m55s" in new_block
    assert ">0:00:55</a>" in new_block


def test_download_assets_multi_rejects_clip(monkeypatch, tmp_path: Path):
    import twitch_download as td

    monkeypatch.setattr(td, "parse_twitch_source", lambda s, kind_hint="auto": ("clip", "slug"))
    with pytest.raises(td.TwitchDownloadError, match="仅支持 VOD"):
        td.download_assets_multi(
            "https://clips.twitch.tv/x",
            [("0:00:00", "0:00:10"), ("0:01:00", "0:01:10")],
            out_dir=tmp_path,
        )


def test_download_assets_multi_single_falls_back(monkeypatch, tmp_path: Path):
    import twitch_download as td

    called = {}

    def fake_single(source, **kwargs):
        called["kwargs"] = kwargs
        return td.DownloadResult(
            video_path=tmp_path / "video.mp4",
            chat_html_path=tmp_path / "chat.html",
            kind="vod",
            source_id="1",
            quality="720p",
            begin=kwargs.get("begin"),
            end=kwargs.get("end"),
            out_dir=tmp_path,
        )

    monkeypatch.setattr(td, "download_assets", fake_single)
    (tmp_path / "video.mp4").write_bytes(b"x")
    (tmp_path / "chat.html").write_text("<pre class='comment-root'>x</pre>", encoding="utf-8")
    res = td.download_assets_multi(
        "612942303",
        [("10s", "20s")],
        out_dir=tmp_path,
        quality="720p",
    )
    assert called["kwargs"]["begin"] == "10s"
    assert called["kwargs"]["end"] == "20s"
    assert res.video_path.name == "video.mp4"


def test_download_assets_multi_mocked(monkeypatch, tmp_path: Path):
    import twitch_download as td

    fake_cli = tmp_path / "TwitchDownloaderCLI.exe"
    fake_cli.write_bytes(b"x")
    monkeypatch.setattr(td, "find_twitchdownloader_cli", lambda root=None: fake_cli)
    monkeypatch.setattr(td, "safe_which", lambda n: "ffmpeg" if n == "ffmpeg" else None)

    def fake_run(cmd, **kwargs):
        out = None
        if "-o" in cmd:
            out = Path(cmd[cmd.index("-o") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            if str(out).endswith(".html"):
                # Stream-absolute messages near each begin (parsed from -b if present)
                begin = "0s"
                if "-b" in cmd:
                    begin = cmd[cmd.index("-b") + 1]
                try:
                    bsec = int(td.parse_td_time(begin))
                except Exception:
                    bsec = 0
                out.write_text(
                    _seg_html([(bsec + 1, "A", "hi"), (bsec + 2, "B", "yo")]),
                    encoding="utf-8",
                )
            else:
                out.write_bytes(b"\x00\x00fake")
        class C:
            returncode = 0
        return C()

    monkeypatch.setattr(td, "run_tracked", fake_run)
    # Durations: first 10s, second 15s
    durs = {0: 10.0, 1: 15.0}

    def fake_probe(path: Path) -> float:
        name = path.name
        if name.startswith("seg_"):
            idx = int(name.split("_")[1].split(".")[0])
            return durs[idx]
        if name == "video.mp4":
            return 25.0
        return 10.0

    monkeypatch.setattr(td, "probe_media_duration", fake_probe)
    monkeypatch.setattr(td, "probe_av_fingerprint", lambda p: ("h264", "1280", "720", "yuv420p", "aac", "48000"))

    def fake_concat(paths, out, list_path=None, **kw):
        out.write_bytes(b"merged")
        return "copy"

    monkeypatch.setattr(td, "concat_videos", fake_concat)

    out_dir = tmp_path / "multi"
    res = td.download_assets_multi(
        "612942303",
        [("100s", "110s"), ("500s", "515s")],
        out_dir=out_dir,
        quality="720p",
        media_check="off",
    )
    assert res.video_path.is_file()
    assert res.chat_html_path.is_file()
    assert res.begin is None and res.end is None
    from chat_parser import parse_chat_html

    data = parse_chat_html(str(res.chat_html_path), str(tmp_path / "pout"))
    # Seg0 begin 100: msgs 101,102 → rel 1,2 → merged 1,2
    # Seg1 begin 500, cum=10: msgs 501,502 → rel 1,2 → merged 11,12
    assert [m["timestamp"] for m in data["messages"]] == [1, 2, 11, 12]


def test_multi_merge_failure_preserves_previous_final_pair(monkeypatch, tmp_path: Path):
    import twitch_download as td

    out_dir = tmp_path / "multi"
    out_dir.mkdir()
    final_video = out_dir / "video.mp4"
    final_chat = out_dir / "chat.html"
    final_video.write_bytes(b"old-video")
    final_chat.write_text("old-chat", encoding="utf-8")

    def fake_download_assets(source, **kwargs):
        video = Path(kwargs["out_dir"]) / kwargs["video_name"]
        chat = Path(kwargs["out_dir"]) / kwargs["chat_name"]
        video.write_bytes(b"segment-video")
        chat.write_text("<html>segment-chat</html>", encoding="utf-8")
        return td.DownloadResult(
            video_path=video,
            chat_html_path=chat,
            kind="vod",
            source_id="1234567890",
            quality=kwargs.get("quality"),
            begin=kwargs.get("begin"),
            end=kwargs.get("end"),
            out_dir=Path(kwargs["out_dir"]),
        )

    def fake_concat_videos(paths, out, **kwargs):
        out.write_bytes(b"new-video")
        return "copy"

    def fail_merge_chat_html(*args, **kwargs):
        raise td.TwitchDownloadError("simulated chat merge failure")

    monkeypatch.setattr(td, "download_assets", fake_download_assets)
    monkeypatch.setattr(td, "probe_media_duration", lambda _path: 10.0)
    monkeypatch.setattr(td, "concat_videos", fake_concat_videos)
    monkeypatch.setattr(td, "merge_chat_html", fail_merge_chat_html)

    with pytest.raises(td.TwitchDownloadError, match="simulated chat merge failure"):
        td.download_assets_multi(
            "1234567890",
            [("0s", "10s"), ("20s", "30s")],
            out_dir=out_dir,
            media_check="off",
        )

    assert final_video.read_bytes() == b"old-video"
    assert final_chat.read_text(encoding="utf-8") == "old-chat"
    assert not (out_dir / ".twitch-download-publish.json").exists()
    assert not (out_dir / ".twitch-download-publish.lock").exists()
    assert not list(out_dir.glob(".*.download-*"))


def test_merge_chat_html_removes_and_rebases_ranges(tmp_path: Path):
    import twitch_download as td

    html = _seg_html([(101, "user_a", "hello"), (102, "user_b", "world"), (103, "user_c", "foo"), (104, "user_d", "bar")])
    source = tmp_path / "seg.html"
    source.write_text(html, encoding="utf-8")
    out = tmp_path / "merged.html"
    seg = td.SegmentDownload(
        index=0,
        segment=td.CropSegment("100s", "110s", 100.0, 110.0),
        video_path=tmp_path / "seg.mp4",
        chat_html_path=source,
        duration_s=10.0,
    )
    td.merge_chat_html(
        [seg], source_id="1", out_path=out, remove_ranges=[(2.0, 4.0)]
    )
    from chat_parser import parse_chat_html

    data = parse_chat_html(str(out), str(tmp_path / "parsed"))
    assert [m["timestamp"] for m in data["messages"]] == [1, 2]


def test_merge_chat_html_normalizes_overlapping_cuts(tmp_path: Path):
    import twitch_download as td

    source = tmp_path / "seg.html"
    source.write_text(
        _seg_html([(101, "a", "one"), (102, "b", "two"), (106, "c", "six")]),
        encoding="utf-8",
    )
    seg = td.SegmentDownload(
        index=0,
        segment=td.CropSegment("100s", "110s", 100.0, 110.0),
        video_path=tmp_path / "seg.mp4",
        chat_html_path=source,
        duration_s=10.0,
    )
    out = tmp_path / "merged.html"
    td.merge_chat_html(
        [seg],
        source_id="1",
        out_path=out,
        remove_ranges=[(2.0, 4.0), (3.0, 5.0)],
    )

    from chat_parser import parse_chat_html

    data = parse_chat_html(str(out), str(tmp_path / "parsed"))
    assert [m["timestamp"] for m in data["messages"]] == [1, 3]


def test_concat_videos_single_copy(tmp_path: Path):
    import twitch_download as td

    src = tmp_path / "a.mp4"
    src.write_bytes(b"abc")
    out = tmp_path / "out.mp4"
    mode = td.concat_videos([src], out)
    assert mode == "copy"
    assert out.read_bytes() == b"abc"


def test_concat_videos_accepts_encoder_parameter(tmp_path: Path):
    """concat_videos should accept encoder kwarg without crashing on arg parsing."""
    import twitch_download as td

    src = tmp_path / "a.mp4"
    src.write_bytes(b"abc")
    out = tmp_path / "out.mp4"
    # Single-segment copy path — encoder is irrelevant but must not crash.
    mode = td.concat_videos([src], out, encoder="x264")
    assert mode == "copy"


def test_concat_videos_with_cuts_rejects_uncut_fallback(tmp_path: Path, monkeypatch):
    """A failed filter concat must not publish a fallback video that ignores --cut."""
    import encode_options
    import twitch_download as td

    paths = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    for path in paths:
        path.write_bytes(b"x")
    calls = []

    monkeypatch.setattr(td, "probe_media_duration", lambda _path: 10.0)
    monkeypatch.setattr(td, "get_stream_start_time", lambda _path, _stream: 0.0)
    monkeypatch.setattr(
        td,
        "run_tracked",
        lambda cmd, **kwargs: calls.append(cmd) or type("Result", (), {"returncode": 1})(),
    )
    monkeypatch.setattr(
        encode_options,
        "resolve_encode_options",
        lambda **kwargs: type("Options", (), {"resolved_encoder": "x264"})(),
    )
    monkeypatch.setattr(encode_options, "build_video_encode_args", lambda _opts: ["-c:v", "libx264"])

    with pytest.raises(td.TwitchDownloadError, match="--cut"):
        td.concat_videos(paths, tmp_path / "out.mp4", remove_ranges=[(2.0, 4.0)])

    assert len(calls) == 1
    assert "-filter_complex" in calls[0]


def test_concat_single_video_with_cuts_never_uses_copy_fast_path(tmp_path: Path, monkeypatch):
    """A direct single-input helper call must not silently ignore --cut."""
    import encode_options
    import twitch_download as td

    source = tmp_path / "a.mp4"
    source.write_bytes(b"x")
    calls = []

    monkeypatch.setattr(td, "probe_media_duration", lambda _path: 10.0)
    monkeypatch.setattr(td, "get_stream_start_time", lambda _path, _stream: 0.0)
    monkeypatch.setattr(
        td,
        "run_tracked",
        lambda cmd, **kwargs: calls.append(cmd) or type("Result", (), {"returncode": 1})(),
    )
    monkeypatch.setattr(
        encode_options,
        "resolve_encode_options",
        lambda **kwargs: type("Options", (), {"resolved_encoder": "x264"})(),
    )
    monkeypatch.setattr(encode_options, "build_video_encode_args", lambda _opts: ["-c:v", "libx264"])

    with pytest.raises(td.TwitchDownloadError, match="--cut"):
        td.concat_videos([source], tmp_path / "out.mp4", remove_ranges=[(2.0, 4.0)])

    assert len(calls) == 1
    assert "-filter_complex" in calls[0]


def test_concat_videos_probe_failure_propagates(tmp_path: Path, monkeypatch):
    """A failed duration probe must abort concat instead of recording 0.0 (D-#4).

    The old `except TwitchDownloadError: durations.append(0.0)` silently turned
    the segment into a 1ms trim and surfaced as a misleading duration-mismatch
    error (or dropped the segment for direct callers).
    """
    import twitch_download as td

    paths = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    for path in paths:
        path.write_bytes(b"x")

    def broken_probe(_path: Path) -> float:
        raise td.TwitchDownloadError("无法读取视频时长: boom")

    monkeypatch.setattr(td, "probe_media_duration", broken_probe)

    with pytest.raises(td.TwitchDownloadError, match="无法读取视频时长"):
        td.concat_videos(paths, tmp_path / "out.mp4")


def test_concat_videos_feeds_probed_durations_into_timeline_check(
    tmp_path: Path, monkeypatch
):
    """Probed durations must still reach the cut-timeline consistency check."""
    from cut_timeline import CutTimeline
    import twitch_download as td

    paths = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    for path in paths:
        path.write_bytes(b"x")

    monkeypatch.setattr(td, "probe_media_duration", lambda _path: 10.0)
    # Timeline built against 100s of source, but probes report 10s+10s.
    timeline = CutTimeline.from_ranges([(2.0, 4.0)], 100.0)

    with pytest.raises(td.TwitchDownloadError, match="不一致"):
        td.concat_videos(
            paths,
            tmp_path / "out.mp4",
            remove_ranges=None,
            cut_timeline=timeline,
        )


def test_download_assets_multi_accepts_cut_and_fps(tmp_path: Path, monkeypatch):
    """download_assets_multi should accept remove_ranges + output_fps + encoder
    and forward them to concat_videos / merge_chat_html."""
    import twitch_download as td

    captured: dict = {}

    class FakeResult:
        video_path = tmp_path / "seg_00.mp4"
        chat_html_path = tmp_path / "seg_00.html"

    def fake_download_assets(source, **kw):
        # Create dummy files so probe_media_duration can work later
        FakeResult.video_path.write_bytes(b"dummy")
        FakeResult.chat_html_path.write_text("<html></html>")
        return FakeResult()

    def fake_concat_videos(paths, out, **kw):
        captured.update(kw)
        out.write_bytes(b"concat_result")
        return "reencode"

    def fake_merge_chat_html(segments, **kw):
        captured["merge_remove_ranges"] = kw.get("remove_ranges")
        kw["out_path"].write_text("<html>merged</html>")
        return kw["out_path"]

    def fake_probe_duration(path):
        return 100.0

    monkeypatch.setattr(td, "download_assets", fake_download_assets)
    monkeypatch.setattr(td, "concat_videos", fake_concat_videos)
    monkeypatch.setattr(td, "merge_chat_html", fake_merge_chat_html)
    monkeypatch.setattr(td, "probe_media_duration", fake_probe_duration)

    result = td.download_assets_multi(
        "1234567890",
        [("0:00:00", "0:10:00"), ("0:20:00", "0:30:00")],
        out_dir=tmp_path / "dl",
        remove_ranges=[(60.0, 120.0)],
        output_fps=60.0,
        encoder="x264",
        media_check="off",
    )

    assert captured.get("remove_ranges") == [(60.0, 120.0)]
    assert captured.get("output_fps") == 60.0
    assert captured.get("encoder") == "x264"
    assert captured.get("merge_remove_ranges") == [(60.0, 120.0)]
    assert result.video_path.read_bytes() == b"concat_result"


# ---------------------------------------------------------------------------
# Fix 9: concat 回退命令带 -r output_fps + 非 x264 encoder WARN
# ---------------------------------------------------------------------------

def test_concat_fallback_command_includes_output_fps_and_encoder_warn(
    tmp_path: Path, monkeypatch, capsys
):
    """filter_complex 主流程失败后，回退命令含 -r <output_fps>；指定
    nvenc 等 encoder 时打印"回退编码固定 libx264"WARN。"""
    import encode_options
    import twitch_download as td

    paths = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    for path in paths:
        path.write_bytes(b"x")
    calls: list[list[str]] = []

    monkeypatch.setattr(td, "probe_media_duration", lambda _path: 10.0)
    monkeypatch.setattr(td, "get_stream_start_time", lambda _path, _stream: 0.0)

    def fake_run_tracked(cmd, **kwargs):
        calls.append(list(cmd))
        # 第一次（filter_complex 主流程）失败；第二次（回退）也失败——
        # 我们只关心回退命令的形态，最后断言抛错。
        return type("Result", (), {"returncode": 1})()

    monkeypatch.setattr(td, "run_tracked", fake_run_tracked)
    monkeypatch.setattr(td.require_executable, "__call__", td.require_executable)
    monkeypatch.setattr(
        encode_options,
        "resolve_encode_options",
        lambda **kwargs: type("Options", (), {"resolved_encoder": "x264"})(),
    )
    monkeypatch.setattr(encode_options, "build_video_encode_args", lambda _opts: ["-c:v", "libx264"])

    with pytest.raises(td.TwitchDownloadError):
        td.concat_videos(paths, tmp_path / "out.mp4", output_fps=60.0, encoder="nvenc")

    assert len(calls) == 2
    fallback = calls[1]
    assert "-r" in fallback
    assert "60.0" in fallback
    out = capsys.readouterr().out
    assert "回退编码固定 libx264" in out
    assert "nvenc" in out


# ---------------------------------------------------------------------------
# K-3: --download-output-fps 支持精确分数（如 30000/1001）
# ---------------------------------------------------------------------------

def test_download_output_fps_argparse_accepts_float_and_fraction():
    """argparse type: float 直接解析；分数原样透传；非法分数报错。"""
    from cli_spec import _download_output_fps

    assert _download_output_fps("60") == 60.0
    assert _download_output_fps("29.97") == 29.97
    assert _download_output_fps("30000/1001") == "30000/1001"
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        _download_output_fps("abc")
    with pytest.raises(argparse.ArgumentTypeError):
        _download_output_fps("30000/")  # 缺分母
    with pytest.raises(argparse.ArgumentTypeError):
        _download_output_fps("/1001")  # 缺分子
    with pytest.raises(argparse.ArgumentTypeError):
        _download_output_fps("30000/0")  # 分母为 0
    with pytest.raises(argparse.ArgumentTypeError):
        _download_output_fps("0")  # 非正数
    with pytest.raises(argparse.ArgumentTypeError):
        _download_output_fps("-5")


def test_build_arg_parser_parses_fraction_output_fps():
    """端到端：--download-output-fps 30000/1001 解析为原样字符串。"""
    from cli_spec import build_arg_parser

    parser = build_arg_parser()
    args = parser.parse_args(["--download-output-fps", "30000/1001"])
    assert args.download_output_fps == "30000/1001"
    args2 = parser.parse_args(["--download-output-fps", "60"])
    assert args2.download_output_fps == 60.0
    # 默认仍是 None（PIPELINE_CLI_DEFAULTS 单源）
    args3 = parser.parse_args([])
    assert args3.download_output_fps is None


def test_concat_fraction_fps_filter_expression(tmp_path: Path, monkeypatch):
    """分数 output_fps 走 fps=num/den 精确表达式；float 保持 .6f。"""
    import encode_options
    import twitch_download as td

    paths = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    for path in paths:
        path.write_bytes(b"x")
    calls: list[list[str]] = []

    monkeypatch.setattr(td, "probe_media_duration", lambda _path: 10.0)
    monkeypatch.setattr(td, "get_stream_start_time", lambda _path, _stream: 0.0)

    def fake_run_tracked(cmd, **kwargs):
        calls.append(list(cmd))
        return type("Result", (), {"returncode": 1})()  # 失败以触发回退，捕全部命令

    monkeypatch.setattr(td, "run_tracked", fake_run_tracked)
    monkeypatch.setattr(
        encode_options,
        "resolve_encode_options",
        lambda **kwargs: type("Options", (), {"resolved_encoder": "x264"})(),
    )
    monkeypatch.setattr(encode_options, "build_video_encode_args", lambda _opts: ["-c:v", "libx264"])

    with pytest.raises(td.TwitchDownloadError):
        td.concat_videos(paths, tmp_path / "out.mp4", output_fps="30000/1001")

    fc_cmd = calls[0]
    fc_index = fc_cmd.index("-filter_complex")
    fc = fc_cmd[fc_index + 1]
    # 归一后的精确分数表达式（30000/1001 已是最简）
    assert "fps=fps=30000/1001" in fc
    assert "[v_cfr]" in fc
    # 回退命令 -r 保持 str() 透传
    fallback = calls[1]
    assert "-r" in fallback and "30000/1001" in fallback

    # float 路径回归：维持现状 .6f 渲染
    calls.clear()
    with pytest.raises(td.TwitchDownloadError):
        td.concat_videos(paths, tmp_path / "out2.mp4", output_fps=60.0)
    fc2 = calls[0][calls[0].index("-filter_complex") + 1]
    assert "fps=fps=60.000000" in fc2

    # float 形式的 30000/1001 也维持现状（29.970030 近似值不改变行为）
    calls.clear()
    with pytest.raises(td.TwitchDownloadError):
        td.concat_videos(paths, tmp_path / "out3.mp4", output_fps=30000 / 1001)
    fc3 = calls[0][calls[0].index("-filter_complex") + 1]
    assert "fps=fps=29.970030" in fc3


def test_download_assets_multi_forwards_fraction_fps(tmp_path: Path, monkeypatch):
    """download_assets_multi 把分数字符串 output_fps 原样转发给 concat_videos。"""
    import twitch_download as td

    captured: dict = {}

    class FakeResult:
        video_path = tmp_path / "seg_00.mp4"
        chat_html_path = tmp_path / "seg_00.html"

    def fake_download_assets(source, **kw):
        FakeResult.video_path.write_bytes(b"dummy")
        FakeResult.chat_html_path.write_text("<html></html>")
        return FakeResult()

    def fake_concat_videos(paths, out, **kw):
        captured.update(kw)
        out.write_bytes(b"concat_result")
        return "reencode"

    def fake_merge_chat_html(segments, **kw):
        kw["out_path"].write_text("<html>merged</html>")
        return kw["out_path"]

    monkeypatch.setattr(td, "download_assets", fake_download_assets)
    monkeypatch.setattr(td, "concat_videos", fake_concat_videos)
    monkeypatch.setattr(td, "merge_chat_html", fake_merge_chat_html)
    monkeypatch.setattr(td, "probe_media_duration", lambda _path: 100.0)

    td.download_assets_multi(
        "1234567890",
        [("0:00:00", "0:10:00"), ("0:20:00", "0:30:00")],
        out_dir=tmp_path / "dl",
        output_fps="30000/1001",
        media_check="off",
    )
    assert captured.get("output_fps") == "30000/1001"

    # 单段 + 分数 fps 也要被拒绝（不能静默忽略）
    with pytest.raises(td.TwitchDownloadError, match="download-output-fps"):
        td.download_assets_multi(
            "1234567890",
            [("0:00:00", "0:10:00")],
            out_dir=tmp_path / "dl2",
            output_fps="30000/1001",
            media_check="off",
        )


# ---------------------------------------------------------------------------
# T-2: concat demuxer 路径转义 golden 测试（零产品代码改动）
# ---------------------------------------------------------------------------

def test_ffmpeg_concat_list_line_golden(tmp_path: Path):
    """_ffmpeg_concat_list_line 的精确输出 golden：空格/单引号/反斜杠路径。

    期望语义: resolve → '\\'→'/'，' → '\\''，包在单引号里。
    golden 按现有实际输出写死；若未来转义规则变更需有意识地更新这里。
    """
    from twitch_download import _ffmpeg_concat_list_line

    # 空格路径
    space = tmp_path / "a b.mp4"
    space.write_bytes(b"x")
    expected_space = f"file '{str(space.resolve()).replace(chr(92), '/')}'"
    assert _ffmpeg_concat_list_line(space) == expected_space
    assert " " in _ffmpeg_concat_list_line(space)  # 空格原样保留在引号内

    # 单引号路径 (it's)
    quote = tmp_path / "it's.mp4"
    quote.write_bytes(b"x")
    # 实现: ' → '\''（结束引号 + 转义引号 + 重开引号，ffmpeg concat demuxer
    # 规范转义），与 shell quoting 习惯一致。golden 按现有实际输出写死。
    resolved_quote = str(quote.resolve()).replace("\\", "/")
    golden_quote = "file '" + resolved_quote.replace("'", "'\\''") + "'"
    assert _ffmpeg_concat_list_line(quote) == golden_quote
    # golden 精确断言（含转义序列 '\''）
    assert _ffmpeg_concat_list_line(quote).endswith("it'\\''s.mp4'")

    # Windows 反斜杠路径形态: C:\a b\file.mp4 → 全部正斜杠
    line = _ffmpeg_concat_list_line(Path("C:\\a b\\file.mp4"))
    assert line.startswith("file 'C:/a b/file.mp4'") or line.startswith("file 'c:/a b/file.mp4'")
    assert "\\" not in line

    # 混合: 引号 + 空格 + 反斜杠（golden 按现有实现实际输出）
    # 注意: 转义序列 '\' 自带一个反斜杠，因此这里不能断言"无反斜杠"，
    # 而是精确断言整行：路径分隔符已全部转为 '/'，仅剩转义反斜杠。
    mixed = Path("C:\\a b\\it's file.mp4")
    mixed_line = _ffmpeg_concat_list_line(mixed)
    assert mixed_line == "file 'C:/a b/it'\\''s file.mp4'" or mixed_line == "file 'c:/a b/it'\\''s file.mp4'"


def test_concat_list_file_roundtrip_written_lines(tmp_path: Path, monkeypatch):
    """写盘的 concat_list.txt 每行 = _ffmpeg_concat_list_line 输出，可直接回读。"""
    import encode_options
    import twitch_download as td

    names = ["a b.mp4", "it's.mp4", "plain.mp4"]
    paths = []
    for name in names:
        p = tmp_path / name
        p.write_bytes(b"x")
        paths.append(p)

    # 单段无 cut 会走 copy 短路；给两段并让 ffmpeg 失败前已写盘。
    monkeypatch.setattr(td, "probe_media_duration", lambda _path: 10.0)
    monkeypatch.setattr(td, "get_stream_start_time", lambda _path, _stream: 0.0)
    monkeypatch.setattr(
        td, "run_tracked",
        lambda cmd, **kwargs: type("Result", (), {"returncode": 1})(),
    )
    monkeypatch.setattr(
        encode_options,
        "resolve_encode_options",
        lambda **kwargs: type("Options", (), {"resolved_encoder": "x264"})(),
    )
    monkeypatch.setattr(encode_options, "build_video_encode_args", lambda _opts: ["-c:v", "libx264"])

    list_path = tmp_path / "concat_list.txt"
    with pytest.raises(td.TwitchDownloadError):
        td.concat_videos(paths[:2], tmp_path / "out.mp4", list_path=list_path)

    written = list_path.read_text(encoding="utf-8").splitlines()
    assert len(written) == 2
    for path, line in zip(paths[:2], written):
        assert line == td._ffmpeg_concat_list_line(path)
    # 引号路径的转义写盘后保持 golden（' → '\''）
    assert written[1].endswith("it'\\''s.mp4'")
