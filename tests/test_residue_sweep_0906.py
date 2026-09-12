"""D4 (concurrency-4): SIGKILL-era tmp/staging residue must be claimed by --clean.

Ctrl+C 硬杀路径（SIG_DFL re-raise）会跳过 finally，因此原子写者的唯一
tmp 兄弟文件、隐藏 staging 目录、以及 TUI 事件文件可能永久残留。
本文件验证 clean_temp_artifacts 的新 residue 规则与 tui_task 的事件文件清扫。
"""

from __future__ import annotations

import os
from pathlib import Path
import time

import pytest

from process_util import _RESIDUE_MAX_AGE_SEC, clean_temp_artifacts


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


@pytest.fixture()
def out(tmp_path: Path) -> Path:
    d = tmp_path / "out"
    d.mkdir()
    return d


class TestDotTmpSiblings:
    """原子写唯一 tmp 兄弟（mkstemp 与手拼 uuid 两种真实形态）。"""

    def test_old_mkstemp_sibling_removed(self, out: Path) -> None:
        p = out / ".run_meta.json.ab12cd34.tmp"
        p.write_text("residue", encoding="utf-8")
        _age(p, _RESIDUE_MAX_AGE_SEC + 3600)
        count, _ = clean_temp_artifacts(out, clean_all=False)
        assert not p.exists()
        assert count >= 1

    def test_old_pid_uuid_sibling_removed(self, out: Path) -> None:
        # translate_chat_openai: f".{name}.{pid}.{uuid}.tmp"
        p = out / ".request.json.12345.deadbeefcafebabe1234567890abcdef.tmp"
        p.write_text("residue", encoding="utf-8")
        _age(p, _RESIDUE_MAX_AGE_SEC + 3600)
        clean_temp_artifacts(out, clean_all=False)
        assert not p.exists()

    def test_old_dash_uuid_sibling_removed(self, out: Path) -> None:
        # env_bootstrap.save_dotenv_api_config: f".{name}.tmp-{uuid}"
        p = out / ".env.tmp-0f1e2d3c4b5a69788796a5b4c3d2e1f0"
        p.write_text("residue", encoding="utf-8")
        _age(p, _RESIDUE_MAX_AGE_SEC + 3600)
        clean_temp_artifacts(out, clean_all=False)
        assert not p.exists()

    def test_fresh_sibling_kept(self, out: Path) -> None:
        # 新文件可能属于仍在写热的 writer，绝不删除。
        p = out / ".run_meta.json.ab12cd34.tmp"
        p.write_text("live", encoding="utf-8")
        clean_temp_artifacts(out, clean_all=False)
        assert p.exists()

    def test_default_clean_picks_up_dot_tmp(self, out: Path) -> None:
        # 与 partial artifacts 同一伞：默认 --clean（无 clean_all）也要捡走。
        p = out / ".translation.json.9f8e7d6c.tmp"
        p.write_text("x", encoding="utf-8")
        _age(p, _RESIDUE_MAX_AGE_SEC + 60)
        clean_temp_artifacts(out, clean_progress=False, clean_all=False)
        assert not p.exists()

    def test_normal_hidden_file_without_tmp_shape_kept(self, out: Path) -> None:
        p = out / ".some_hidden_config"
        p.write_text("keep", encoding="utf-8")
        _age(p, _RESIDUE_MAX_AGE_SEC + 3600)
        clean_temp_artifacts(out, clean_all=False)
        assert p.exists()

    def test_one_level_down_claimed(self, out: Path) -> None:
        sub = out / "subdir"
        sub.mkdir()
        p = sub / ".data.json.aaaabbbb.tmp"
        p.write_text("x", encoding="utf-8")
        _age(p, _RESIDUE_MAX_AGE_SEC + 60)
        clean_temp_artifacts(out, clean_all=False)
        assert not p.exists()


class TestTranslationContextHandoff:
    """render_cn_chat 硬杀残留的翻译 context 交接文件（可含敏感 glossary）。

    命名互为契约（twin）：写入方 render_cn_chat._prepare_translation_context
    为 f"translation_context_{pid}_{uuid4().hex[:8]}.txt"，
    process_util._is_partial_artifact 按同一命名认领（partial 伞下不限年龄）。
    """

    def test_old_handoff_file_removed(self, out: Path) -> None:
        p = out / f"translation_context_{os.getpid()}_a5236cda.txt"
        p.write_text("sensitive glossary", encoding="utf-8")
        _age(p, _RESIDUE_MAX_AGE_SEC + 3600)
        count, _ = clean_temp_artifacts(out, clean_all=False)
        assert not p.exists()
        assert count >= 1

    def test_name_shape_mismatch_kept(self, out: Path) -> None:
        # 契约严格一致：随机 hex 段不足 8 位不认领，避免误伤同名前缀文件。
        p = out / f"translation_context_{os.getpid()}_abc123.txt"
        p.write_text("keep", encoding="utf-8")
        _age(p, _RESIDUE_MAX_AGE_SEC + 3600)
        clean_temp_artifacts(out, clean_all=False)
        assert p.exists()


class TestDotStagingDirs:
    """make_job_dir / install 的隐藏 staging 目录残留。"""

    def test_old_job_staging_dir_removed(self, out: Path) -> None:
        d = out / ".job_1757123456_1234_abc12345.extra1"
        d.mkdir()
        (d / "run_meta.json").write_text("{}", encoding="utf-8")
        _age(d, _RESIDUE_MAX_AGE_SEC + 3600)
        clean_temp_artifacts(out, clean_all=False)
        assert not d.exists()

    def test_old_batch_staging_dir_removed(self, out: Path) -> None:
        d = out / ".batch_1757123456_1234_abc12345.extra1"
        d.mkdir()
        _age(d, _RESIDUE_MAX_AGE_SEC + 3600)
        clean_temp_artifacts(out, clean_all=False)
        assert not d.exists()

    def test_old_install_staging_dir_removed(self, out: Path) -> None:
        # td_cli_install / env_bootstrap: ".{dest}.install-<rand>"
        d = out / ".twitch-cli.install-9c8b7a6d5e4f"
        d.mkdir()
        (d / "twitch-cli.exe").write_text("x", encoding="utf-8")
        _age(d, _RESIDUE_MAX_AGE_SEC + 3600)
        clean_temp_artifacts(out, clean_all=False)
        assert not d.exists()

    def test_old_ready_staging_dir_removed(self, out: Path) -> None:
        # ".{dest}.ready-<hex>"
        d = out / ".twitch-cli.ready-0123456789abcdef"
        d.mkdir()
        _age(d, _RESIDUE_MAX_AGE_SEC + 3600)
        clean_temp_artifacts(out, clean_all=False)
        assert not d.exists()

    def test_fresh_staging_dir_kept(self, out: Path) -> None:
        d = out / ".job_1757123456_1234_abc12345.fresh0"
        d.mkdir()
        clean_temp_artifacts(out, clean_all=False)
        assert d.exists()

    def test_staging_rule_gated_like_job_dirs(self, out: Path) -> None:
        # only_job_dir 模式只清指定目录，不顺手清 staging 残留（镜像现有纪律）。
        staging = out / ".job_1_x.install-stuff"
        staging.mkdir()
        _age(staging, _RESIDUE_MAX_AGE_SEC + 3600)
        target = out / "job_2_y"
        target.mkdir()
        clean_temp_artifacts(out, only_job_dir=target, clean_all=False)
        assert staging.exists()

    def test_visible_dir_without_dot_not_claimed(self, out: Path) -> None:
        d = out / "install-something-old"
        d.mkdir()
        _age(d, _RESIDUE_MAX_AGE_SEC + 3600)
        clean_temp_artifacts(out, clean_all=False)
        assert d.exists()


class TestTuiEventSweep:
    """TUI 死亡后 outputs/.tui-events 里 task_*.jsonl / *.result.json 的清扫。"""

    def test_sweep_removes_old_event_files(self, tmp_path: Path) -> None:
        import tui_task

        events = tmp_path / "outputs" / ".tui-events"
        events.mkdir(parents=True)
        old_jsonl = events / "task_abc123.jsonl"
        old_jsonl.write_text("{}", encoding="utf-8")
        old_result = events / "task_abc123.result.json"
        old_result.write_text("{}", encoding="utf-8")
        fresh_jsonl = events / "task_def456.jsonl"
        fresh_jsonl.write_text("{}", encoding="utf-8")
        other = events / "unrelated.jsonl"
        other.write_text("{}", encoding="utf-8")
        _age(old_jsonl, _RESIDUE_MAX_AGE_SEC + 3600)
        _age(old_result, _RESIDUE_MAX_AGE_SEC + 3600)
        _age(other, _RESIDUE_MAX_AGE_SEC + 3600)

        tui_task._sweep_stale_event_files(events)

        assert not old_jsonl.exists()
        assert not old_result.exists()
        assert fresh_jsonl.exists()
        assert other.exists()

    def test_sweep_tolerates_missing_dir(self, tmp_path: Path) -> None:
        import tui_task

        tui_task._sweep_stale_event_files(tmp_path / "nope" / ".tui-events")

    def test_sweep_uses_shared_threshold(self) -> None:
        import tui_task

        assert tui_task._RESIDUE_MAX_AGE_SEC == 24 * 3600


def test_threshold_is_24h() -> None:
    assert _RESIDUE_MAX_AGE_SEC == 24 * 3600
