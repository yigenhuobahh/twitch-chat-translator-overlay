from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ux_setup  # noqa: E402

SDIST_CONTRACT_FILES = {
    ".gitattributes",
    ".gitignore",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    "CHANGELOG.md",
    "pytest.ini",
    "run.bat",
    "run.sh",
    "install.bat",
    "install.sh",
    "update.bat",
    "update.sh",
    "doctor.bat",
    "doctor.sh",
    "jobs/example_job.yaml",
}


def _native_update_launcher(tmp_path: Path, *, failing_git: bool = False) -> tuple[subprocess.CompletedProcess, str]:
    env = os.environ.copy()
    env["CI"] = "1"

    if os.name == "nt":
        launcher = tmp_path / "update.bat"
        shutil.copy2(ROOT / "update.bat", launcher)
        if failing_git:
            fake_bin = tmp_path / "fake-bin"
            fake_bin.mkdir()
            (fake_bin / "git.cmd").write_text("@echo off\r\nexit /b 1\r\n", encoding="ascii")
            env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        command = [env.get("COMSPEC", "cmd.exe"), "/d", "/c", r"call .\update.bat"]
    else:
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("bash is required for the native Unix launcher test")
        launcher = tmp_path / "update.sh"
        shutil.copy2(ROOT / "update.sh", launcher)
        if failing_git:
            fake_bin = tmp_path / "fake-bin"
            fake_bin.mkdir()
            fake_git = fake_bin / "git"
            fake_git.write_text("#!/usr/bin/env sh\nexit 1\n", encoding="ascii")
            fake_git.chmod(0o755)
            env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        command = [bash, str(launcher)]

    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    return result, (result.stdout or "") + (result.stderr or "")


def test_manifest_includes_sdist_runtime_and_test_contract():
    entries = {
        line.strip()
        for line in (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("include ")
    }
    included = {line.removeprefix("include ").strip() for line in entries}

    assert included >= SDIST_CONTRACT_FILES
    assert "recursive-include tests *.py" in (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include tests/fixtures *" in (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    for relative in SDIST_CONTRACT_FILES:
        assert (ROOT / relative).exists(), relative


def test_wheel_data_files_include_complete_example_job():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    data_files = config["tool"]["setuptools"]["data-files"]

    assert data_files["share/twitch-chat-translator-overlay/jobs"] == [
        "jobs/example_job.yaml"
    ]


def test_wheel_includes_download_transaction_module_boundary():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    modules = set(config["tool"]["setuptools"]["py-modules"])

    assert {"twitch_download", "twitch_download_transaction", "twitch_download_types"} <= modules


def test_source_example_job_loader_returns_complete_template():
    source = (ROOT / "jobs" / "example_job.yaml").read_text(encoding="utf-8")

    assert len(source.splitlines()) >= 70
    assert ux_setup.find_example_job() == (ROOT / "jobs" / "example_job.yaml").resolve()
    assert ux_setup.example_job_yaml_text() == source


def test_installed_share_scaffold_matches_complete_source_template(tmp_path: Path, monkeypatch):
    source = (ROOT / "jobs" / "example_job.yaml").read_text(encoding="utf-8")
    share = tmp_path / "prefix" / "share" / "twitch-chat-translator-overlay"
    installed = share / "jobs" / "example_job.yaml"
    installed.parent.mkdir(parents=True)
    installed.write_text(source, encoding="utf-8")

    monkeypatch.setattr(ux_setup, "source_checkout_root", lambda _module: None)
    monkeypatch.setattr(ux_setup, "distribution_share_dirs", lambda: [share])

    consumer = tmp_path / "consumer cwd"
    consumer.mkdir()
    created, status = ux_setup.ensure_example_job(consumer)

    assert status == "created"
    assert created == consumer / "jobs" / "example_job.yaml"
    assert created.read_text(encoding="utf-8") == source


def test_native_updater_rejects_non_git_archive_before_dependency_work(tmp_path: Path):
    result, output = _native_update_launcher(tmp_path)

    assert result.returncode != 0
    assert "not a git checkout" in output.lower()
    assert "fresh" in output.lower()
    assert "[2/3]" not in output
    assert "update done" not in output.lower()
    assert "更新完成" not in output


def test_native_updater_pull_failure_requires_fresh_clone(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    result, output = _native_update_launcher(tmp_path, failing_git=True)

    assert result.returncode != 0
    assert "history may have been rewritten" in output.lower()
    assert "back up only" in output.lower()
    assert "fresh clone" in output.lower()
    assert "[2/3]" not in output


def test_updater_guidance_never_combines_or_rewrites_old_history():
    for name in ("update.bat", "update.sh"):
        text = (ROOT / name).read_text(
            encoding="ascii" if name.endswith(".bat") else "utf-8"
        ).lower()
        assert "git merge" not in text
        assert "git reset" not in text
        assert "stash pop" not in text
        assert "fresh clone" in text


def test_ci_has_sdist_and_scheduled_max_gates():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "sdist-smoke:" in workflow
    assert "python -m build --sdist" in workflow
    assert "python -m pip wheel --no-deps" in workflow
    assert 'python -m pytest -q -m "not smoke and not slow"' in workflow
    assert "scheduled-max:" in workflow
    assert "python scripts/run_tests.py --max" in workflow
    assert "schedule:" in workflow
