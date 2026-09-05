#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TwitchDownloaderCLI portable installer, split from twitch_download (W3-A1).

Every function body below is copied verbatim from twitch_download.py.
Collaborators that remain in twitch_download (``find_twitchdownloader_cli``,
``prepend_tools_td_to_path``, ``td_exe_names``) are reached through the lazy
forwarders so tests that ``monkeypatch.setattr(twitch_download, ...)`` keep
working when the installer is driven via ``twitch_download.try_portable_td_cli``.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid
import zipfile

from common_utils import trusted_tools_root
from twitch_download_types import TwitchDownloadError

# Same value as twitch_download._TOOLS_ROOT: both modules live in scripts/ and
# trusted_tools_root() derives the root from the module file location, so the
# default root= of try_portable_td_cli resolves to the identical trusted root.
_TOOLS_ROOT = trusted_tools_root(__file__)

# Assets of the most recent successful release lookup. Lets the checksum
# verifier reuse the already-fetched asset list (no extra API round-trip).
# Module-level state: releases are fetched once per install run.
_LAST_RELEASE_ASSETS: list[object] = []


def find_twitchdownloader_cli(root: Path | None = None) -> Path | None:
    """Lazy forwarder: keeps twitch_download.find_twitchdownloader_cli patches effective."""
    from twitch_download import find_twitchdownloader_cli as _find_twitchdownloader_cli

    return _find_twitchdownloader_cli(root)


def prepend_tools_td_to_path(root: Path | None = None) -> str | None:
    """Lazy forwarder: keeps twitch_download.prepend_tools_td_to_path patches effective."""
    from twitch_download import prepend_tools_td_to_path as _prepend_tools_td_to_path

    return _prepend_tools_td_to_path(root)


def td_exe_names() -> list[str]:
    """Lazy forwarder: keeps twitch_download.td_exe_names patches effective."""
    from twitch_download import td_exe_names as _td_exe_names

    return _td_exe_names()



def platform_td_asset_token() -> str | None:
    """Substring that matches lay295 release asset names for this OS/arch."""
    import platform as _platform

    sysname = _platform.system()
    machine = (_platform.machine() or "").lower()
    if sysname == "Windows":
        return "Windows-x64"
    if sysname == "Darwin":
        if machine in ("arm64", "aarch64"):
            return "MacOSArm64"
        return "MacOS-x64"
    if sysname == "Linux":
        if machine in ("aarch64", "arm64"):
            return "LinuxArm64"
        if machine.startswith("arm"):
            return "LinuxArm"
        # Prefer glibc x64 over Alpine build for desktop distros
        return "Linux-x64"
    return None


def pick_td_cli_asset(assets: list[object]) -> dict | None:
    """Pick best TwitchDownloaderCLI zip asset from GitHub release asset list."""
    token = platform_td_asset_token()
    if not token:
        return None
    cli_assets = []
    for a in assets or []:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "")
        if "TwitchDownloaderCLI" not in name:
            continue
        if "GUI" in name:
            continue
        if not name.lower().endswith(".zip"):
            continue
        cli_assets.append(a)
    # Exact platform token match (avoid LinuxArm matching LinuxArm64).
    for a in cli_assets:
        name = str(a.get("name") or "")
        if token == "LinuxArm":
            if "LinuxArm64" in name:
                continue
            if "LinuxArm" in name:
                return a
        elif token in name:
            return a
    return None


def fetch_latest_td_cli_release_asset(
    *,
    timeout: float = 30.0,
) -> tuple[str, str, str]:
    """Return (tag, asset_name, browser_download_url) for this platform.

    Raises TwitchDownloadError on network/API/selection failure.
    """
    import json as _json
    from urllib.error import HTTPError, URLError
    from urllib.parse import urlparse
    from urllib.request import Request, urlopen

    api = "https://api.github.com/repos/lay295/TwitchDownloader/releases/latest"
    req = Request(
        api,
        headers={
            "User-Agent": "twitch-chat-cn-overlay",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        # The endpoint is a fixed HTTPS GitHub API URL, not user input.
        with urlopen(req, timeout=timeout) as resp:  # nosec B310
            raw = resp.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise TwitchDownloadError("GitHub releases 响应超过 2 MiB 上限")
            data = _json.loads(raw.decode("utf-8", errors="replace"))
    except HTTPError as e:
        raise TwitchDownloadError(f"GitHub releases API 失败 HTTP {e.code}: {e.reason}") from e
    except URLError as e:
        raise TwitchDownloadError(f"无法连接 GitHub releases: {e.reason}") from e
    except Exception as e:
        raise TwitchDownloadError(f"读取 releases 失败: {e}") from e

    if not isinstance(data, dict):
        raise TwitchDownloadError("GitHub releases 响应根节点必须是对象")
    tag = str(data.get("tag_name") or "unknown")
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise TwitchDownloadError("releases 响应缺少 assets 列表")
    if not all(isinstance(asset, dict) for asset in assets):
        raise TwitchDownloadError("releases 响应中的 asset 项必须是对象")
    picked = pick_td_cli_asset(assets)
    if not picked:
        token = platform_td_asset_token() or "unknown-platform"
        names = [str(a.get("name") or "") for a in assets if "CLI" in str(a.get("name") or "")]
        raise TwitchDownloadError(
            f"当前平台 ({token}) 在 release {tag} 中无匹配的 TwitchDownloaderCLI zip。\n"
            f"  可用: {', '.join(names) or '(无)'}\n"
            "  请手动从 https://github.com/lay295/TwitchDownloader/releases 下载"
        )
    url = str(picked.get("browser_download_url") or "").strip()
    name = str(picked.get("name") or "").strip()
    if not url or not name:
        raise TwitchDownloadError("选中的 asset 缺少 name 或 browser_download_url")
    parsed = urlparse(url)
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "github.com"
        or not parsed.path.startswith("/lay295/TwitchDownloader/releases/download/")
    ):
        raise TwitchDownloadError("release asset URL 不属于预期的 GitHub 下载路径")
    if parsed.query or parsed.fragment:
        raise TwitchDownloadError("release asset URL 不应携带 query/fragment")
    global _LAST_RELEASE_ASSETS
    _LAST_RELEASE_ASSETS = list(assets)
    return tag, name, url


def _flatten_td_cli_into(dest: Path) -> Path | None:
    """If exe is nested under dest, keep using dest; return path to exe if found."""
    for name in td_exe_names():
        direct = dest / name
        if direct.is_file():
            return direct
    # Nested single top-level folder from zip
    try:
        children = [p for p in dest.iterdir() if p.is_dir() and not p.name.startswith(".")]
    except OSError:
        return None
    for child in children:
        for name in td_exe_names():
            cand = child / name
            if cand.is_file():
                # Move contents up one level for stable tools/TwitchDownloaderCLI/exe layout
                try:
                    for item in child.iterdir():
                        target = dest / item.name
                        if target.exists():
                            continue
                        item.rename(target)
                    try:
                        child.rmdir()
                    except OSError:
                        pass
                except OSError:
                    return None
                flat = dest / name
                return flat if flat.is_file() else None
    return None


def _find_checksum_asset(assets: list[object], asset_name: str) -> dict | None:
    """Find a sibling checksum asset for ``asset_name`` in the release assets.

    Common shapes: checksums.txt, sha256sums.txt, <stem>.sha256. Returns the
    asset dict or None (best-effort: absence simply skips verification).
    """
    lower_name = asset_name.lower()
    stem = lower_name
    if stem.endswith(".zip"):
        stem = stem[:-4]
    for a in assets or []:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "")
        low = name.lower()
        if not (low.endswith(".txt") or low.endswith(".sha256") or low.endswith(".sha256sum")):
            continue
        if "sha256" in low or "checksum" in low or low.startswith(stem):
            return a
    return None


def _parse_checksum_for(text: str, asset_name: str) -> str | None:
    """Parse `<hash>  <filename>` / `<hash> *<filename>` lines for asset_name.

    Returns the hex digest (lowercase) or None when no (case-insensitive) match.
    """
    target = asset_name.lower()
    for line in (text or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        digest, filename = parts[0].strip(), parts[1].strip()
        if filename.startswith("*"):
            filename = filename[1:]
        if filename.lower() == target and re.fullmatch(r"[0-9a-fA-F]{32,128}", digest):
            return digest.lower()
    return None


def _sha256_file(path: Path) -> str:
    """Streaming SHA-256 of a file (hex, lowercase)."""
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_download_checksum(
    *,
    asset_name: str,
    zip_path: Path,
    urlopen,
    request_factory,
    timeout: float,
    max_bytes: int,
    stream_to_path,
) -> None:
    """Best-effort SHA-256 verification against a sibling release checksum asset.

    Only assets already listed in the same release response are considered —
    no extra API round-trips, no hard-coded digests. When the release ships a
    checksum file (name containing sha256/checksum, .txt/.sha256) that maps
    ``asset_name`` to a digest, the downloaded archive must match or it is
    deleted and TwitchDownloadError raised. No checksum asset → silently skip.
    """
    from urllib.error import HTTPError, URLError
    from urllib.parse import urlparse

    checksum_asset = _find_checksum_asset(_LAST_RELEASE_ASSETS, asset_name)
    if checksum_asset is None:
        return
    checksum_url = str(checksum_asset.get("browser_download_url") or "").strip()
    if not checksum_url:
        return
    parsed = urlparse(checksum_url)
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "github.com"
        or not parsed.path.startswith("/lay295/TwitchDownloader/releases/download/")
        or parsed.query
        or parsed.fragment
    ):
        return
    checksum_path = zip_path.parent / "checksums.download"
    try:
        with urlopen(request_factory(checksum_url), timeout=timeout) as resp:  # nosec B310
            stream_to_path(resp, checksum_path, max_bytes=max_bytes)
        expected = _parse_checksum_for(
            checksum_path.read_text(encoding="utf-8", errors="replace"), asset_name
        )
    except (OSError, ValueError, HTTPError, URLError):
        checksum_path.unlink(missing_ok=True)
        return
    checksum_path.unlink(missing_ok=True)
    if not expected:
        return
    actual = _sha256_file(zip_path)
    if actual != expected.lower():
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise TwitchDownloadError(
            f"下载的 {asset_name} checksum mismatch（sha256 不匹配）: "
            f"expected {expected.lower()}, got {actual}。已删除损坏的下载文件。"
        )
    print("  [OK] SHA-256 校验通过", flush=True)


def try_portable_td_cli(
    *,
    root: Path | None = None,
    timeout: float = 120.0,
) -> bool:
    """Download latest TwitchDownloaderCLI through a validated staged install."""
    from urllib.request import Request, urlopen

    from env_bootstrap import (
        MAX_PORTABLE_DOWNLOAD_BYTES,
        atomic_replace_directory,
        safe_extract_zip,
        stream_response_to_path,
    )

    root = root or _TOOLS_ROOT
    if find_twitchdownloader_cli(root):
        return True

    dest = root / "tools" / "TwitchDownloaderCLI"
    print("\n-- 自动安装 TwitchDownloaderCLI（便携）--")
    print(f"  目标目录: {dest}")
    print("  来源: GitHub lay295/TwitchDownloader releases/latest")
    print("  体积约数十 MB，需网络。")

    try:
        tag, asset_name, url = fetch_latest_td_cli_release_asset(timeout=min(30.0, timeout))
    except TwitchDownloadError as exc:
        print(f"  [FAIL] {exc}")
        return False

    print(f"  版本: {tag}")
    print("  注意: 尽力校验 release 提供的 SHA-256 校验文件；未提供时仅校验 GitHub 官方 release 路径与压缩包结构。")
    print(f"  资源: {asset_name}")
    print(f"  URL: {url}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=f".{dest.name}.install-", dir=dest.parent)
    )
    payload = staging_root / "payload"
    zip_path = staging_root / "download.zip"
    ready: Path | None = None
    try:
        print("  下载中…")
        request = Request(url, headers={"User-Agent": "twitch-chat-cn-overlay"})
        # ``url`` was validated above as a GitHub HTTPS release-asset path.
        with urlopen(request, timeout=timeout) as response:  # nosec B310
            stream_response_to_path(
                response,
                zip_path,
                max_bytes=MAX_PORTABLE_DOWNLOAD_BYTES,
            )
        _verify_download_checksum(
            asset_name=asset_name,
            zip_path=zip_path,
            urlopen=urlopen,
            request_factory=lambda target: Request(
                target, headers={"User-Agent": "twitch-chat-cn-overlay"}
            ),
            timeout=timeout,
            max_bytes=MAX_PORTABLE_DOWNLOAD_BYTES,
            stream_to_path=stream_response_to_path,
        )
        print("  解压中…")
        with zipfile.ZipFile(zip_path, "r") as archive:
            safe_extract_zip(archive, payload)
        exe = _flatten_td_cli_into(payload)
        if exe is None or exe.parent != payload:
            raise ValueError(
                "archive does not contain TwitchDownloaderCLI in a supported layout"
            )
        if os.name != "nt":
            exe.chmod(exe.stat().st_mode | 0o111)

        zip_path.unlink(missing_ok=True)
        ready = dest.parent / f".{dest.name}.ready-{uuid.uuid4().hex}"
        payload.rename(ready)
        atomic_replace_directory(ready, dest)
        ready = None
    except Exception as exc:
        print(f"  [FAIL] 下载/解压失败: {exc}")
        print("  请手动从 https://github.com/lay295/TwitchDownloader/releases 下载")
        return False
    finally:
        for leftover in (ready, staging_root):
            if leftover is None:
                continue
            try:
                if leftover.is_symlink() or leftover.is_file():
                    leftover.unlink(missing_ok=True)
                elif leftover.exists():
                    shutil.rmtree(leftover)
            except OSError:
                pass

    prepend_tools_td_to_path(root)
    found = find_twitchdownloader_cli(root)
    if found:
        print(f"  [OK] TwitchDownloaderCLI: {found}")
        return True
    print("  [FAIL] 安装后未找到 TwitchDownloaderCLI 可执行文件，请检查目录结构")
    return False
