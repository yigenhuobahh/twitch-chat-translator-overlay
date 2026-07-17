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
