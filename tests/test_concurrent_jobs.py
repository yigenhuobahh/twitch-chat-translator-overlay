#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Concurrent job-dir isolation, promote naming, and placeholder job validation."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(SCRIPTS)
    env["_TWITCH_TRANSPARENT_TEST_MODE"] = "1"
    return env


def test_make_job_dir_unique_and_marked(tmp_path: Path):
    from process_util import JOB_DIR_MARKER, is_tool_job_dir, make_job_dir

    a = make_job_dir(tmp_path, prefix="job_")
    b = make_job_dir(tmp_path, prefix="job_")
    assert a != b
    assert (a / JOB_DIR_MARKER).is_file()
    assert (b / JOB_DIR_MARKER).is_file()
    assert is_tool_job_dir(a)
    assert is_tool_job_dir(b)
    # pid embedded
    assert re.match(r"job_\d+_\d+_[0-9a-fA-F]+", a.name)


def test_placeholder_and_validate_job_media_paths(tmp_path: Path):
    from job_config import is_placeholder_media_path, validate_job_media_paths, write_job_file

    assert is_placeholder_media_path("path/to/video.mp4")
    assert is_placeholder_media_path(r"path\to\chat.html")
    assert not is_placeholder_media_path(str(tmp_path / "real.mp4"))

    problems = validate_job_media_paths(
        {"video": "path/to/video.mp4", "chat_html": "path/to/chat.html"},
        require_existing=True,
    )
    assert problems
    assert any("占位" in p or "path/to" in p for p in problems)

    vid = tmp_path / "v.mp4"
    html = tmp_path / "c.html"
    vid.write_bytes(b"not-a-real-mp4")
    html.write_text("<html></html>", encoding="utf-8")
    ok = validate_job_media_paths(
        {"video": str(vid), "chat_html": str(html)},
        require_existing=True,
    )
    assert ok == []

    # example-style job file roundtrip + validation
    job_path = write_job_file(
        tmp_path / "ex.yaml",
        {
            "video": "path/to/video.mp4",
            "chat_html": "path/to/chat.html",
            "mode": "preview",
            "render_original": True,
        },
        title="ex",
        overwrite=True,
    )
    text = job_path.read_text(encoding="utf-8")
    assert "video:" in text


def test_pipeline_rejects_job_without_media_non_tty(tmp_path: Path):
    from job_config import write_job_file

    # Reusable style job: no pinned paths
    job = write_job_file(
        tmp_path / "style.yaml",
        {
            "mode": "preview",
            "render_original": True,
            "preview_clip": 3,
            "layout_preset": "compact",
        },
        title="style",
        overwrite=True,
        pin_paths=False,
    )
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_cn_chat.py"), "--job", str(job)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_env(),
    )
    assert r.returncode != 0
    joined = (r.stdout or "") + (r.stderr or "")
    assert (
        "video" in joined.lower()
        or "chat" in joined.lower()
        or "非交互" in joined
        or "取消注释" in joined
        or "缺少" in joined
    )


def test_pipeline_accepts_job_plus_cli_media(tmp_path: Path, make_test_video):
    from job_config import write_job_file

    html = ROOT / "tests" / "fixtures" / "twitchdownloader_chat.html"
    if not html.is_file():
        pytest.skip("fixture missing")
    video = make_test_video(duration=2.0)
    job = write_job_file(
        tmp_path / "style.yaml",
        {
            "mode": "preview",
            "render_original": True,
            "preview_clip": 2,
            "overlay_codec": "png",
            "layout_preset": "compact",
        },
        title="style",
        overwrite=True,
        pin_paths=False,
    )
    out = tmp_path / "o.mp4"
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "render_cn_chat.py"),
            "--job",
            str(job),
            str(video),
            str(html),
            "--output",
            str(out),
            "--workdir",
            str(tmp_path / "w"),
            "--fps",
            "15",
            "--offset",
            "0",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_env(),
    )
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    assert out.is_file()


@pytest.mark.smoke
def test_promote_to_out_base_uses_job_unique_name_on_collision(tmp_path: Path, make_test_video):
    """End-to-end: two real runs promoting into the same out_base must not
    overwrite each other's published file (second run uses a job-tagged name).

    Exercises the real promote_to_out_base logic inside twitch_chat_burn.main,
    including the Wave 2 publish-guard cleanup, via fast single-frame
    --preview-mode runs instead of mirroring the heuristic in Python.
    """
    video = make_test_video(duration=2.0, fps=10)
    html = ROOT / "tests" / "fixtures" / "twitchdownloader_chat.html"
    if not html.is_file():
        pytest.skip("fixture html missing")
    out_base = tmp_path / "out"
    out_base.mkdir()
    # Job dirs must live under out-dir; each run still promotes its artifact
    # up to out_base where the basename collision happens.
    job_a = out_base / "job_test_a"
    job_b = out_base / "job_test_b"
    job_a.mkdir()
    job_b.mkdir()

    def run_preview(job_dir: Path) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable,
            str(SCRIPTS / "twitch_chat_burn.py"),
            str(video),
            str(html),
            "--preview-frame",
            "1.5",
            "--out-dir",
            str(out_base),
            "--job-dir",
            str(job_dir),
            "--offset",
            "0",
            "--keep-temp",
        ]
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_env(),
        )

    r1 = run_preview(job_a)
    assert r1.returncode == 0, (r1.stdout or "") + (r1.stderr or "")
    default_name = f"{video.stem}_preview_1.5s.png"
    published_a = out_base / default_name
    assert published_a.is_file(), (r1.stdout or "") + (r1.stderr or "")
    first_bytes = published_a.read_bytes()

    r2 = run_preview(job_b)
    assert r2.returncode == 0, (r2.stdout or "") + (r2.stderr or "")
    # Collision: run A keeps the default name; run B publishes job-unique.
    assert published_a.is_file()
    assert published_a.read_bytes() == first_bytes
    alt = out_base / f"{video.stem}_preview_1.5s__{job_b.name}.png"
    assert alt.is_file(), (r2.stdout or "") + (r2.stderr or "")
    assert "[concurrent]" in (r2.stdout or "")
    # Wave 2 guard cleanup: on Windows the publish lock file is removed after
    # a successful publish; on POSIX it is intentionally kept (C-12 ABA) and
    # claimed later by --clean. The forced-platform matrix lives in
    # test_promote_guard_kept_on_posix_deleted_on_windows below; here we only
    # assert the current platform's contract.
    from twitch_chat_burn import _should_unlink_guard

    if _should_unlink_guard():
        assert not list(out_base.glob(".*.publish.guard"))


@pytest.mark.max
def test_concurrent_burns_shared_out_dir_isolated(tmp_path: Path, make_test_video):
    """Two burns, same --out-dir, default job dirs: both succeed with distinct job_*."""
    video = make_test_video(duration=3.0, fps=30)
    html = ROOT / "tests" / "fixtures" / "twitchdownloader_chat.html"
    if not html.is_file():
        pytest.skip("fixture html missing")
    out_dir = tmp_path / "shared"
    out_dir.mkdir()

    def run_one(log_path: Path) -> int:
        cmd = [
            sys.executable,
            str(SCRIPTS / "twitch_chat_burn.py"),
            str(video),
            str(html),
            "--preview-clip",
            "2",
            "--overlay-codec",
            "png",
            "--offset",
            "0",
            "--fps",
            "15",
            "--out-dir",
            str(out_dir),
            "--keep-temp",
        ]
        with open(log_path, "w", encoding="utf-8") as fh:
            p = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, env=_env())
        return p.returncode

    log_a = tmp_path / "a.txt"
    log_b = tmp_path / "b.txt"
    results: list[int] = [None, None]  # type: ignore

    def wrap(i: int, logp: Path):
        results[i] = run_one(logp)

    t1 = threading.Thread(target=wrap, args=(0, log_a))
    t2 = threading.Thread(target=wrap, args=(1, log_b))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert results[0] == 0, log_a.read_text(encoding="utf-8", errors="replace")[-800:]
    assert results[1] == 0, log_b.read_text(encoding="utf-8", errors="replace")[-800:]

    job_dirs = [p for p in out_dir.iterdir() if p.is_dir() and p.name.startswith("job_")]
    assert len(job_dirs) >= 2
    pids = set()
    for d in job_dirs:
        m = re.match(r"job_\d+_(\d+)_[0-9a-fA-F]+", d.name)
        assert m, d.name
        pids.add(m.group(1))
        assert (d / ".twitch_overlay_job").is_file()
    assert len(pids) >= 2

    # Each job should have its own chat mp4; root may have unique-suffixed copies
    job_mp4s = list(out_dir.glob("job_*/" + video.stem + "_chat.mp4"))
    assert len(job_mp4s) >= 2


# ---------------------------------------------------------------------------
# C-12: publish-guard unlink policy per platform
# ---------------------------------------------------------------------------
# POSIX unlinking the guard right after publish is racy (ABA): waiter B holds
# the old inode while newcomer C creates a fresh guard file, so B/C end up with
# distinct "locks". POSIX therefore keeps the guard file (leftovers are claimed
# by --clean's `.*.publish.guard` rule); Windows keeps unlinking.


def test_should_unlink_guard_defaults_to_current_platform():
    from twitch_chat_burn import _GUARD_UNLINK_ON, _should_unlink_guard

    assert _should_unlink_guard() == (os.name in _GUARD_UNLINK_ON)
    # Windows unlinks; POSIX keeps the guard (ABA, see C-12).
    if os.name == "nt":
        assert _should_unlink_guard() is True
    else:
        assert _should_unlink_guard() is False


def test_promote_guard_kept_on_posix_deleted_on_windows(tmp_path: Path, make_test_video):
    """End-to-end promote: guard file survives on POSIX, is removed on Windows.

    promote_to_out_base is a main() closure and cannot be patched directly, so
    the unlink decision is forced in a real child burn process: a tiny driver
    imports the burn module, flips the module-level _GUARD_UNLINK_ON constant,
    then calls main(). Runs two preview jobs into the same out_base so the
    second one goes through the real promote/collision path. Both policies
    ('keep' / 'unlink') are exercised regardless of the host OS.
    """
    video = make_test_video(duration=2.0, fps=10)
    html = ROOT / "tests" / "fixtures" / "twitchdownloader_chat.html"
    if not html.is_file():
        pytest.skip("fixture html missing")

    driver = tmp_path / "guard_policy_driver.py"
    driver.write_text(
        "import sys\n"
        "sys.argv = ['twitch_chat_burn.py'] + sys.argv[1:]\n"
        "import twitch_chat_burn as burn\n"
        # sys.argv[1] is the unlink-policy token. 'unlink' forces
        # _should_unlink_guard() True on ANY host; 'keep' forces it False.
        # (Setting _GUARD_UNLINK_ON to a platform tuple would be a no-op when
        # that platform isn't the runner's own os.name — the original CI
        # failure.) Bind the token BEFORE stripping argv: the real promote
        # closure resolves _should_unlink_guard via its module-global lookup,
        # so this replacement is what main() actually consults.
        "force_unlink = sys.argv[1] == 'unlink'\n"
        "burn._should_unlink_guard = (lambda: force_unlink)\n"
        "sys.argv = [sys.argv[0]] + sys.argv[2:]\n"
        "rc = burn.main()\n"
        "sys.exit(rc if isinstance(rc, int) else 0)\n",
        encoding="utf-8",
    )

    for policy, expect_guard in (("keep", True), ("unlink", False)):
        out_base = tmp_path / f"out_{policy}"
        out_base.mkdir()

        # First run seeds the colliding basename in out_base.
        r1 = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "twitch_chat_burn.py"),
                str(video),
                str(html),
                "--preview-frame",
                "1.5",
                "--out-dir",
                str(out_base),
                "--offset",
                "0",
                "--keep-temp",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_env(),
            cwd=str(ROOT),
        )
        assert r1.returncode == 0, (r1.stdout or "") + (r1.stderr or "")
        published = out_base / f"{video.stem}_preview_1.5s.png"
        assert published.is_file(), (r1.stdout or "") + (r1.stderr or "")

        # Second run with an isolated job dir under out_base: promote must run
        # (out_dir != out_base) with the platform decision forced by the driver.
        job_dir = out_base / f"job_c12_{policy}"
        job_dir.mkdir()
        r2 = subprocess.run(
            [
                sys.executable,
                str(driver),
                policy,
                str(video),
                str(html),
                "--preview-frame",
                "1.5",
                "--out-dir",
                str(out_base),
                "--job-dir",
                str(job_dir),
                "--offset",
                "0",
                "--keep-temp",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_env(),
            cwd=str(ROOT),
        )
        assert r2.returncode == 0, (r2.stdout or "") + (r2.stderr or "")

        if expect_guard:
            # POSIX: guard intentionally left behind for --clean to claim.
            guards = list(out_base.glob(".*.publish.guard"))
            assert guards, (
                f"[{policy}] expected a leftover publish.guard, got none; "
                + (r2.stdout or "") + (r2.stderr or "")
            )
        else:
            # Windows: guard removed after successful publish.
            assert not list(out_base.glob(".*.publish.guard")), (
                f"[{policy}] expected no publish.guard leftovers; "
                + (r2.stdout or "") + (r2.stderr or "")
            )
