"""P2-9/P2-10/P2-11 security-decision fixes: TOFU ffmpeg manifest, TD env gate, dotenv write gate.

Covers the three user-approved decisions from the 0906 review wave:
- P2-9: try_portable_ffmpeg TOFU manifest helpers (_sha256_of_file, manifest I/O, gate decision)
- P2-10: TWITCHDOWNLOADER_CLI env override confirmation gate
- P2-11: save_dotenv_api_config write-side untrusted-cwd confirmation gate
"""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import common_utils
import env_bootstrap
import twitch_download

# ---------------------------------------------------------------------------
# P2-9: TOFU manifest helpers
# ---------------------------------------------------------------------------

def test_sha256_of_file_matches_hashlib(tmp_path: Path):
    target = tmp_path / "blob.bin"
    payload = b"gyan-essentials-payload" * 4096
    target.write_bytes(payload)
    import hashlib

    assert env_bootstrap._sha256_of_file(target) == hashlib.sha256(payload).hexdigest()


def test_ffmpeg_tofu_gate_first_install_passes():
    assert env_bootstrap._ffmpeg_tofu_gate(None, "a" * 64, prompt=lambda _m: False) is True


def test_ffmpeg_tofu_gate_same_digest_passes_without_prompt():
    prompted = []
    ok = env_bootstrap._ffmpeg_tofu_gate("a" * 64, "a" * 64, prompt=lambda m: prompted.append(m))
    assert ok is True
    assert prompted == []  # 同 digest 不得打扰用户


def test_ffmpeg_tofu_gate_changed_digest_decline_blocks():
    ok = env_bootstrap._ffmpeg_tofu_gate("a" * 64, "b" * 64, prompt=lambda _m: False)
    assert ok is False


def test_ffmpeg_tofu_gate_changed_digest_confirm_proceeds():
    ok = env_bootstrap._ffmpeg_tofu_gate("a" * 64, "b" * 64, prompt=lambda _m: True)
    assert ok is True


def test_write_and_read_ffmpeg_manifest_roundtrip(tmp_path: Path):
    dest_root = tmp_path / "tools" / "ffmpeg"
    dest_root.mkdir(parents=True)
    env_bootstrap._write_ffmpeg_manifest(
        dest_root, url="https://example/ffmpeg.zip", sha256="c" * 64, size_bytes=12345
    )
    manifest = env_bootstrap._read_ffmpeg_manifest(dest_root)
    assert manifest is not None
    assert manifest["sha256"] == "c" * 64
    assert manifest["size_bytes"] == 12345
    assert manifest["url"] == "https://example/ffmpeg.zip"
    assert "installed_at" in manifest


def test_read_ffmpeg_manifest_missing_returns_none(tmp_path: Path):
    assert env_bootstrap._read_ffmpeg_manifest(tmp_path) is None


def test_read_ffmpeg_manifest_corrupt_returns_none(tmp_path: Path):
    dest_root = tmp_path / "tools" / "ffmpeg"
    dest_root.mkdir(parents=True)
    (dest_root / env_bootstrap.FFMPEG_INSTALL_MANIFEST_NAME).write_text("{broken", encoding="utf-8")
    assert env_bootstrap._read_ffmpeg_manifest(dest_root) is None


# ---------------------------------------------------------------------------
# P2-10: TWITCHDOWNLOADER_CLI env override confirmation gate
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_td_exe(tmp_path: Path) -> Path:
    exe = tmp_path / "evil" / "TwitchDownloaderCLI.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    return exe


def test_env_override_untrusted_blocked_without_confirmation(tmp_path, monkeypatch, fake_td_exe, capsys):
    """非信任路径 + 非交互 stdin（isatty False）→ fail-closed，不走 env 分支。"""
    monkeypatch.setenv("TWITCHDOWNLOADER_CLI", str(fake_td_exe))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    result = twitch_download.find_twitchdownloader_cli(root=tmp_path)
    out = capsys.readouterr().out
    assert result is None or (result is not None and fake_td_exe.resolve() != result)
    assert "已忽略 TWITCHDOWNLOADER_CLI" in out


def test_env_override_untrusted_confirmed_returns_path(tmp_path, monkeypatch, fake_td_exe):
    monkeypatch.setenv("TWITCHDOWNLOADER_CLI", str(fake_td_exe))
    monkeypatch.setattr(
        common_utils, "_confirm_untrusted_executable", lambda p: True
    )
    result = twitch_download.find_twitchdownloader_cli(root=tmp_path)
    assert result == fake_td_exe.resolve()


def test_env_override_untrusted_declined_falls_back(tmp_path, monkeypatch, fake_td_exe):
    """拒绝 env 覆盖后回落链仍可用：tools 目录中的二进制照常解析。"""
    tools_bin = tmp_path / "tools" / "TwitchDownloaderCLI"
    tools_bin.mkdir(parents=True)
    fallback = tools_bin / "TwitchDownloaderCLI.exe"
    fallback.write_bytes(b"MZ")
    monkeypatch.setenv("TWITCHDOWNLOADER_CLI", str(fake_td_exe))
    monkeypatch.setattr(common_utils, "_confirm_untrusted_executable", lambda p: False)
    monkeypatch.setattr(twitch_download, "safe_which", lambda name: None)
    monkeypatch.setattr(
        twitch_download, "tools_td_bin_dirs", lambda root=None: [tools_bin]
    )
    result = twitch_download.find_twitchdownloader_cli(root=tmp_path)
    assert result == fallback.resolve()


def test_env_override_trusted_root_path_passes_without_prompt(tmp_path, monkeypatch, fake_td_exe):
    """信任根内的路径（注册目录）直接放行，不弹确认。"""
    import common_utils as cu

    registered = cu.register_trusted_executable_dir(fake_td_exe.parent)
    try:
        monkeypatch.setenv("TWITCHDOWNLOADER_CLI", str(fake_td_exe))
        prompted = []
        monkeypatch.setattr(
            common_utils,
            "_confirm_untrusted_executable",
            lambda p: prompted.append(p) or False,
        )
        result = twitch_download.find_twitchdownloader_cli(root=tmp_path)
        assert result == fake_td_exe.resolve()
        assert prompted == []
    finally:
        cu._TRUSTED_EXECUTABLE_DIRS.discard(registered)


def test_is_trusted_executable_path_rejects_outside(tmp_path):
    outside = tmp_path / "somewhere" / "tool.exe"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"MZ")
    assert common_utils._is_trusted_executable_path(outside) is False


def test_is_trusted_executable_path_accepts_trusted_root(tmp_path):
    inside = tmp_path / "tool.exe"
    inside.write_bytes(b"MZ")
    import common_utils as cu

    registered = cu.register_trusted_executable_dir(tmp_path)
    try:
        assert common_utils._is_trusted_executable_path(inside) is True
    finally:
        cu._TRUSTED_EXECUTABLE_DIRS.discard(registered)


# ---------------------------------------------------------------------------
# P2-11: save_dotenv_api_config write-side gate
# ---------------------------------------------------------------------------

@pytest.fixture()
def _clean_api_env(monkeypatch):
    for key in ("OPENAI_COMPAT_BASE_URL", "OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_MODEL"):
        monkeypatch.delenv(key, raising=False)


def test_save_dotenv_explicit_path_bypasses_gate(tmp_path, monkeypatch, _clean_api_env):
    """显式 env_path 视为用户已知情：非交互 stdin 也不拦。"""
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    ok, msg = env_bootstrap.save_dotenv_api_config(
        "https://api.example/v1", "sk-x", "m", env_path=tmp_path / ".env"
    )
    assert ok is True and (tmp_path / ".env").is_file()


def test_save_dotenv_repo_root_auto_target_passes(tmp_path, monkeypatch, _clean_api_env):
    """cwd == repo root（信任目录）→ 自动目标放行。"""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(env_bootstrap, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    ok, msg = env_bootstrap.save_dotenv_api_config(
        "https://api.example/v1", "sk-x", "m"
    )
    assert ok is True
    assert (tmp_path / ".env").is_file()


def test_save_dotenv_untrusted_cwd_noninteractive_cancelled(tmp_path, monkeypatch, _clean_api_env):
    """cwd 在信任根之外 + 非交互 stdin → fail-closed 取消，不落盘。"""
    outside = tmp_path / "bundle"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setattr(env_bootstrap, "_repo_root", lambda: tmp_path / "repo")
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    ok, msg = env_bootstrap.save_dotenv_api_config(
        "https://api.example/v1", "sk-secret", "m"
    )
    assert ok is False
    assert "非信任" in msg
    assert not (outside / ".env").exists()


def test_save_dotenv_untrusted_cwd_confirmed_writes(tmp_path, monkeypatch, _clean_api_env):
    outside = tmp_path / "bundle"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setattr(env_bootstrap, "_repo_root", lambda: tmp_path / "repo")
    monkeypatch.setattr(
        common_utils, "_confirm_untrusted_dotenv", lambda: True
    )
    ok, msg = env_bootstrap.save_dotenv_api_config(
        "https://api.example/v1", "sk-secret", "m"
    )
    assert ok is True
    assert (outside / ".env").is_file()
