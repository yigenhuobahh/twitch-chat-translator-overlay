#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Environment readiness: structured checks, install hints, optional fixes (P1–P4).

P1: collect_readiness + print_readiness_report (shared by doctor / install)
P2: optional Windows winget/choco FFmpeg install (confirm)
P3: macOS brew / Linux package-manager command hints (+ optional brew if confirmed)
P4: honor repo tools/ffmpeg (PATH inject); optional portable download scaffold (Windows)
Optional: TwitchDownloaderCLI detect (WARN only) + install-time offer_td_cli_guide
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.request import Request, urlopen
import uuid
import zipfile

from common_utils import (
    current_cli_invocation,
    current_cli_script,
    is_console_entry_script,
    register_trusted_executable_dir,
    safe_which,
    trusted_tools_root,
)


@dataclass
class CheckItem:
    key: str
    name: str
    ok: bool
    required_for_render: bool
    required_for_translate: bool = False
    detail: str = ""
    fix_cmds: list[str] = field(default_factory=list)
    fix_urls: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


MAX_PORTABLE_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_PORTABLE_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024
MAX_PORTABLE_ARCHIVE_FILES = 20_000


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _archive_member_target(root: Path, info: zipfile.ZipInfo) -> Path:
    raw_name = info.filename
    normalized = raw_name.replace("\\", "/")
    member = PurePosixPath(normalized)
    if (
        not raw_name
        or "\x00" in raw_name
        or normalized.startswith("/")
        or member.is_absolute()
        or not member.parts
        or any(part in ("", ".", "..") for part in member.parts)
        or ":" in member.parts[0]
    ):
        raise ValueError(f"unsafe archive member path: {raw_name!r}")
    target = root.joinpath(*member.parts)
    try:
        target.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"archive member escapes destination: {raw_name!r}") from exc
    return target


def safe_extract_zip(
    archive: zipfile.ZipFile,
    destination: Path,
    *,
    max_files: int = MAX_PORTABLE_ARCHIVE_FILES,
    max_uncompressed_bytes: int = MAX_PORTABLE_EXTRACTED_BYTES,
) -> None:
    """Extract a trusted-tool archive with traversal and resource limits."""
    infos = archive.infolist()
    if len(infos) > max_files:
        raise ValueError(f"archive contains too many entries ({len(infos)} > {max_files})")
    declared_size = sum(max(0, int(info.file_size)) for info in infos)
    if declared_size > max_uncompressed_bytes:
        raise ValueError(
            "archive expands beyond the allowed size "
            f"({declared_size} > {max_uncompressed_bytes} bytes)"
        )

    destination.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    targets: list[tuple[zipfile.ZipInfo, Path, int]] = []
    for info in infos:
        if info.flag_bits & 0x1:
            raise ValueError(f"encrypted archive member is not supported: {info.filename!r}")
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(unix_mode)
        if file_type == stat.S_IFLNK:
            raise ValueError(f"archive symlink is not allowed: {info.filename!r}")
        if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ValueError(f"special archive member is not allowed: {info.filename!r}")
        target = _archive_member_target(destination, info)
        identity = os.path.normcase(str(target.resolve(strict=False)))
        if identity in seen:
            raise ValueError(f"duplicate archive member path: {info.filename!r}")
        seen.add(identity)
        targets.append((info, target, unix_mode))

    extracted_size = 0
    for info, target, unix_mode in targets:
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with archive.open(info, "r") as source, target.open("xb") as output:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                extracted_size += len(chunk)
                if written > int(info.file_size) or extracted_size > max_uncompressed_bytes:
                    raise ValueError(f"archive member exceeded its size budget: {info.filename!r}")
                output.write(chunk)
        if written != int(info.file_size):
            raise ValueError(f"archive member size mismatch: {info.filename!r}")
        if os.name != "nt" and unix_mode:
            target.chmod(unix_mode & 0o777)


def stream_response_to_path(response, path: Path, *, max_bytes: int) -> int:
    """Stream an HTTP response to a new file while enforcing a byte ceiling."""
    headers = getattr(response, "headers", None)
    length_value = headers.get("Content-Length") if headers is not None else None
    if length_value is None and hasattr(response, "getheader"):
        length_value = response.getheader("Content-Length")
    if length_value:
        try:
            content_length = int(length_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Content-Length in download response") from exc
        if content_length < 0 or content_length > max_bytes:
            raise ValueError(
                f"download exceeds the allowed size ({content_length} > {max_bytes} bytes)"
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with path.open("xb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"download exceeded the allowed size ({total} > {max_bytes} bytes)")
            output.write(chunk)
    return total


def atomic_replace_directory(staged: Path, destination: Path) -> None:
    """Replace a tool directory only after a complete staged install validates."""
    if not staged.is_dir():
        raise ValueError(f"staged install is not a directory: {staged}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if staged.parent.resolve() != destination.parent.resolve():
        raise ValueError("staged install must be a sibling of its destination")

    backup: Path | None = None
    if destination.exists() or destination.is_symlink():
        backup = destination.with_name(f".{destination.name}.backup-{uuid.uuid4().hex}")
        os.replace(destination, backup)
    try:
        os.replace(staged, destination)
    except Exception:
        if backup is not None and not destination.exists():
            os.replace(backup, destination)
        raise
    if backup is not None:
        try:
            _remove_path(backup)
        except OSError:
            pass


def _repo_root() -> Path:
    return trusted_tools_root(__file__)


def portable_ffmpeg_dir(root: Path | None = None) -> Path:
    """Trusted portable FFmpeg directory for source or installed execution."""
    return (root or _repo_root()) / "tools" / "ffmpeg"


def tools_ffmpeg_bin_dirs(root: Path | None = None) -> list[Path]:
    """Candidate dirs under the trusted app tools root."""
    base = portable_ffmpeg_dir(root)
    return [
        base / "bin",
        base,
        base / "ffmpeg-master-latest-win64-gpl" / "bin",
        base / "ffmpeg-master-latest-win64-gpl-shared" / "bin",
    ]


def prepend_tools_ffmpeg_to_path(root: Path | None = None) -> str | None:
    """If tools/ffmpeg has ffmpeg(+ffprobe), prepend to PATH. Returns bin dir or None."""
    for d in tools_ffmpeg_bin_dirs(root):
        if not d.is_dir():
            continue
        exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        probe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
        if (d / exe).is_file() and (d / probe).is_file():
            path = str(register_trusted_executable_dir(d))
            cur = os.environ.get("PATH", "")
            if path not in cur.split(os.pathsep):
                os.environ["PATH"] = path + os.pathsep + cur
            return path
    # Also accept nested **/bin after one-level extract
    base = portable_ffmpeg_dir(root)
    if base.is_dir():
        for child in base.iterdir():
            if not child.is_dir():
                continue
            b = child / "bin"
            exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
            probe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
            if b.is_dir() and (b / exe).is_file() and (b / probe).is_file():
                path = str(register_trusted_executable_dir(b))
                cur = os.environ.get("PATH", "")
                if path not in cur.split(os.pathsep):
                    os.environ["PATH"] = path + os.pathsep + cur
                return path
    return None


def _system() -> str:
    return platform.system()


def _ffmpeg_fix_cmds() -> tuple[list[str], list[str]]:
    sysname = _system()
    cli = current_cli_invocation()
    urls = ["https://ffmpeg.org/download.html"]
    if sysname == "Windows":
        cmds = [
            "winget install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements",
            "choco install ffmpeg -y",
            f"{cli} --doctor --offer-fix",
            f"# 或便携: 解压到 {portable_ffmpeg_dir()}，使 bin/ffmpeg.exe 存在",
        ]
        urls.append("https://www.gyan.dev/ffmpeg/builds/")
    elif sysname == "Darwin":
        cmds = [
            "brew install ffmpeg",
            "brew install --cask font-noto-sans-cjk  # optional CJK",
        ]
    else:
        cmds = [
            "sudo apt-get update && sudo apt-get install -y ffmpeg fonts-noto-cjk fonts-wqy-zenhei",
            "sudo dnf install -y ffmpeg google-noto-sans-cjk-fonts",
            "sudo pacman -S ffmpeg noto-fonts-cjk",
        ]
    return cmds, urls


def _font_fix_cmds() -> tuple[list[str], list[str]]:
    sysname = _system()
    urls: list[str] = []
    if sysname == "Windows":
        cmds = [
            "# Windows 通常自带微软雅黑；若缺失: 设置 → 时间和语言 → 语言和区域 → 添加中文",
        ]
    elif sysname == "Darwin":
        cmds = [
            "brew install --cask font-noto-sans-cjk",
            "# 或用 --font-path 指向已有 CJK 字体",
        ]
    else:
        cmds = [
            "sudo apt-get install -y fonts-noto-cjk fonts-wqy-zenhei",
            "sudo dnf install -y google-noto-sans-cjk-fonts",
        ]
    return cmds, urls


def collect_readiness(*, font_path: str | None = "auto", font_bold_path: str | None = "auto") -> list[CheckItem]:
    """Run environment checks; may inject tools/ffmpeg into PATH first."""
    prepend_tools_ffmpeg_to_path()
    try:
        from twitch_download import find_twitchdownloader_cli, td_install_hints
    except ImportError:
        find_twitchdownloader_cli = None  # type: ignore
        td_install_hints = None  # type: ignore
    items: list[CheckItem] = []

    # Python
    py_ok = sys.version_info >= (3, 10)
    items.append(
        CheckItem(
            key="python",
            name="Python >= 3.10",
            ok=py_ok,
            required_for_render=True,
            detail=sys.version.split()[0],
            fix_cmds=["https://www.python.org/downloads/ 安装 3.10+ 并勾选 Add to PATH"],
            fix_urls=["https://www.python.org/downloads/"],
        )
    )

    # FFmpeg / ffprobe
    ff_cmds, ff_urls = _ffmpeg_fix_cmds()
    for exe in ("ffmpeg", "ffprobe"):
        path = safe_which(exe)
        items.append(
            CheckItem(
                key=exe,
                name=exe,
                ok=bool(path),
                required_for_render=True,
                detail=path or "未找到",
                fix_cmds=ff_cmds,
                fix_urls=ff_urls,
            )
        )

    # Packages
    import importlib.util

    packages = {
        "Pillow": "PIL",
        "beautifulsoup4": "bs4",
        "openai": "openai",
        "PyYAML": "yaml",
        "openpyxl": "openpyxl",
    }
    for display, module in packages.items():
        try:
            present = importlib.util.find_spec(module) is not None
        except Exception:
            present = module in sys.modules
        req = module in ("PIL", "bs4", "yaml")  # openai/openpyxl softer for original-only
        items.append(
            CheckItem(
                key=f"pkg:{module}",
                name=display,
                ok=present,
                required_for_render=req,
                required_for_translate=(module == "openai"),
                detail="已安装" if present else "未安装",
                fix_cmds=["pip install -r requirements.txt", f"pip install {display}"],
            )
        )

    # Fonts
    try:
        from common_utils import detect_cjk_font
    except Exception:
        detect_cjk_font = None  # type: ignore

    font_cmds, font_urls = _font_fix_cmds()
    if font_path and font_path != "auto":
        reg_ok = Path(font_path).is_file()
        reg_detail = font_path
    elif detect_cjk_font:
        reg, bold = detect_cjk_font()
        reg_ok = bool(reg)
        reg_detail = reg or "未检测到 CJK 字体"
    else:
        reg_ok, reg_detail = False, "无法检测"
    items.append(
        CheckItem(
            key="font",
            name="CJK 字体",
            ok=reg_ok,
            required_for_render=True,
            detail=str(reg_detail),
            fix_cmds=font_cmds + ["# 或: --font-path /path/to/NotoSansCJK.ttc"],
            fix_urls=font_urls,
        )
    )

    # Translation API (optional for original render)
    base_url = os.getenv("OPENAI_COMPAT_BASE_URL") or os.getenv("AGNES_BASE_URL")
    model = os.getenv("OPENAI_COMPAT_MODEL") or os.getenv("AGNES_MODEL")
    api_key = os.getenv("OPENAI_COMPAT_API_KEY") or os.getenv("AGNES_API_KEY")
    api_ok = bool(base_url and model and api_key)
    items.append(
        CheckItem(
            key="api",
            name="翻译 API (.env)",
            ok=api_ok,
            required_for_render=False,
            required_for_translate=True,
            detail="已配置" if api_ok else "未齐全（--render-original 可跳过）",
            fix_cmds=[
                f"{current_cli_invocation()} --init",
                "# 编辑 .env: OPENAI_COMPAT_BASE_URL / MODEL / API_KEY",
            ],
            notes=["仅自动翻译需要；原文烧录不需要"],
        )
    )

    # Optional: TwitchDownloaderCLI (download enhancement; not required for burn)
    if find_twitchdownloader_cli is not None:
        td_path = find_twitchdownloader_cli()
        td_cmds, td_urls = td_install_hints() if td_install_hints else ([], [])
        items.append(
            CheckItem(
                key="twitchdownloader",
                name="TwitchDownloaderCLI（可选）",
                ok=bool(td_path),
                required_for_render=False,
                required_for_translate=False,
                detail=str(td_path) if td_path else "未找到（仅 --download / 菜单下载需要）",
                fix_cmds=td_cmds,
                fix_urls=td_urls,
                notes=["可选增强：从 VOD/Clip URL 自动下视频+聊天 HTML"],
            )
        )

    return items


def get_translate_api_config() -> dict[str, str | None]:
    """Return current OpenAI-compatible env (OPENAI_COMPAT_* with AGNES_* fallback)."""
    return {
        "base_url": os.getenv("OPENAI_COMPAT_BASE_URL") or os.getenv("AGNES_BASE_URL"),
        "api_key": os.getenv("OPENAI_COMPAT_API_KEY") or os.getenv("AGNES_API_KEY"),
        "model": os.getenv("OPENAI_COMPAT_MODEL") or os.getenv("AGNES_MODEL"),
    }


def translate_api_config_ok(cfg: dict[str, str | None] | None = None) -> bool:
    cfg = cfg or get_translate_api_config()
    return bool(cfg.get("base_url") and cfg.get("api_key") and cfg.get("model"))


def probe_translate_api(*, timeout: float = 12.0) -> tuple[bool, str]:
    """Lightweight connectivity/auth check for the translation API.

    Returns (ok, message). Does not translate chat content.
    """
    cfg = get_translate_api_config()
    base = (cfg.get("base_url") or "").strip()
    key = (cfg.get("api_key") or "").strip()
    model = (cfg.get("model") or "").strip()
    missing = []
    if not base:
        missing.append("OPENAI_COMPAT_BASE_URL")
    if not key:
        missing.append("OPENAI_COMPAT_API_KEY")
    if not model:
        missing.append("OPENAI_COMPAT_MODEL")
    if missing:
        return False, "未配置: " + ", ".join(missing)

    try:
        from openai import OpenAI
    except ImportError:
        return False, "未安装 openai 库（pip install openai）"

    try:
        client = OpenAI(api_key=key, base_url=base, timeout=timeout)
        # Tiny chat completion — works on OpenAI-compatible servers that expose chat/completions.
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        return True, f"API 可达 ({base}, model={model})"
    except Exception as e:
        err = str(e).strip() or type(e).__name__
        if len(err) > 240:
            err = err[:240] + "…"
        return False, f"API 不可用: {err}"


def readiness_levels(items: list[CheckItem]) -> tuple[bool, bool]:
    """Return (min_render_ok, full_translate_ok)."""
    min_ok = all(i.ok for i in items if i.required_for_render)
    # translate needs min + api + openai pkg
    full_ok = min_ok and all(
        i.ok for i in items if i.required_for_translate or i.key in ("api", "pkg:openai")
    )
    return min_ok, full_ok


def print_readiness_report(items: list[CheckItem] | None = None) -> tuple[bool, bool]:
    """Print checklist + graded readiness. Returns (min_ok, full_ok)."""
    if items is None:
        items = collect_readiness()
    min_ok, full_ok = readiness_levels(items)

    print("\n======== 就绪清单 / Readiness ========")
    for i in items:
        mark = "x" if i.ok else " "
        level = ""
        if i.required_for_render and not i.ok:
            level = " [出片必需]"
        elif i.required_for_translate and not i.ok:
            level = " [翻译需要]"
        print(f"  [{mark}] {i.name}: {i.detail}{level}")
        if not i.ok:
            for cmd in i.fix_cmds[:4]:
                print(f"       {cmd}")
            for u in i.fix_urls[:2]:
                print(f"       文档: {u}")

    print("\n分级:")
    print(
        f"  最小可用（--render-original 原文烧录）: "
        f"{'OK' if min_ok else '未就绪 — 先处理上方「出片必需」'}"
    )
    print(
        f"  完整可用（API 翻译出片）: "
        f"{'OK' if full_ok else '未就绪 — 需出片必需 + .env 翻译配置'}"
    )
    if not min_ok:
        print("\n装好缺失项后请再运行:")
        if not is_console_entry_script(current_cli_script()):
            print("  run.bat doctor")
        print(f"  {current_cli_invocation()} --doctor")
        print(f"  可选自动修复: {current_cli_invocation()} --doctor --offer-fix")
    print("======================================")
    return min_ok, full_ok


def _prompt_yes(msg: str, *, default: bool = False, assume_yes: bool = False) -> bool:
    if assume_yes:
        print(f"{msg} [auto-yes]")
        return True
    try:
        if not sys.stdin or not sys.stdin.isatty():
            print(f"{msg} [非交互: 跳过]")
            return False
    except Exception:
        return False
    suffix = "Y/n" if default else "y/N"
    try:
        raw = input(f"{msg} ({suffix}): ").strip().lower()
    except EOFError:
        return False
    if not raw:
        return default
    return raw in ("y", "yes", "1")


def _run_cmd(cmd: list[str] | str, *, shell: bool = False) -> int:
    print(f"$ {cmd if isinstance(cmd, str) else ' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, shell=shell)
        return int(r.returncode)
    except FileNotFoundError:
        return 127
    except OSError:
        return 1


def try_fix_ffmpeg(*, assume_yes: bool = False) -> bool:
    """P2/P3: try package manager to install ffmpeg. Returns True if ffmpeg becomes available."""
    if safe_which("ffmpeg") and safe_which("ffprobe"):
        return True
    sysname = _system()

    if sysname == "Windows":
        if winget := safe_which("winget"):
            # Outer prompt already confirmed help; default Yes here too.
            if _prompt_yes("使用 winget 安装 FFmpeg？", default=True, assume_yes=assume_yes):
                rc = _run_cmd(
                    [
                        winget,
                        "install",
                        "--id",
                        "Gyan.FFmpeg",
                        "-e",
                        "--accept-package-agreements",
                        "--accept-source-agreements",
                    ]
                )
                if rc == 0:
                    print("  winget 完成。正在刷新本进程 PATH 探测…")
                    _refresh_windows_path_from_machine()
        elif choco := safe_which("choco"):
            if _prompt_yes("使用 choco 安装 FFmpeg？", default=True, assume_yes=assume_yes):
                _run_cmd([choco, "install", "ffmpeg", "-y"])
                _refresh_windows_path_from_machine()
        else:
            print("  未找到 winget/choco。可改用便携包，或手动安装:")
            cmds, urls = _ffmpeg_fix_cmds()
            for c in cmds[:3]:
                print(f"    {c}")
            for u in urls:
                print(f"    {u}")

    elif sysname == "Darwin":
        if brew := safe_which("brew"):
            if _prompt_yes("使用 brew 安装 FFmpeg？", default=True, assume_yes=assume_yes):
                _run_cmd([brew, "install", "ffmpeg"])
        else:
            print("  未找到 brew。请安装 Homebrew 后: brew install ffmpeg")
            print("  https://brew.sh")

    else:
        print("  Linux 需管理员权限，将打印命令（不自动 sudo）:")
        for c in _ffmpeg_fix_cmds()[0]:
            print(f"    {c}")
        if _prompt_yes("是否尝试 sudo apt 安装 ffmpeg + 中文字体？", default=False, assume_yes=assume_yes):
            _run_cmd(
                "sudo apt-get update && sudo apt-get install -y ffmpeg fonts-noto-cjk fonts-wqy-zenhei",
                shell=True,
            )

    prepend_tools_ffmpeg_to_path()
    return bool(safe_which("ffmpeg") and safe_which("ffprobe"))


def _refresh_windows_path_from_machine() -> None:
    """Merge Machine+User PATH into current process (winget often updates registry only)."""
    if os.name != "nt":
        return
    try:
        import winreg  # type: ignore

        parts: list[str] = []
        for root, sub in (
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, "Environment"),
        ):
            try:
                with winreg.OpenKey(root, sub) as key:
                    val, _ = winreg.QueryValueEx(key, "Path")
                    if val:
                        parts.append(str(val))
            except OSError:
                pass
        if parts:
            merged = os.pathsep.join(parts)
            # Keep any PATH entries we already injected (tools/ffmpeg).
            old = os.environ.get("PATH", "")
            os.environ["PATH"] = merged + os.pathsep + old
    except Exception:
        pass


# Gyan essentials build (Windows x64) — used only with explicit user consent (P4).
_GYAN_ESSENTIALS_URL = (
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
)


def _find_ffmpeg_bin(root: Path) -> Path | None:
    candidates = [root, root / "bin"]
    try:
        candidates.extend(child / "bin" for child in root.iterdir() if child.is_dir())
    except OSError:
        return None
    for candidate in candidates:
        if (
            (candidate / "ffmpeg.exe").is_file()
            and (candidate / "ffprobe.exe").is_file()
        ):
            return candidate
    return None


def try_portable_ffmpeg(*, assume_yes: bool = False, root: Path | None = None) -> bool:
    """P4: download portable FFmpeg into tools/ffmpeg (Windows primarily)."""
    if safe_which("ffmpeg") and safe_which("ffprobe"):
        return True
    root = root or _repo_root()
    if prepend_tools_ffmpeg_to_path(root):
        return True

    if _system() != "Windows":
        print("  便携 FFmpeg 自动下载目前主要支持 Windows；其它平台请用包管理器。")
        return False

    if not _prompt_yes(
        f"下载便携 FFmpeg 到 {(root / 'tools' / 'ffmpeg')}？(约数十 MB，需网络)",
        assume_yes=assume_yes,
    ):
        return False

    dest_root = root / "tools" / "ffmpeg"
    dest_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=f".{dest_root.name}.install-", dir=dest_root.parent)
    )
    payload = staging_root / "payload"
    zip_path = staging_root / "ffmpeg-release-essentials.zip"
    ready: Path | None = None
    try:
        print(f"  下载: {_GYAN_ESSENTIALS_URL}")
        print(f"  → {zip_path}")
        request = Request(
            _GYAN_ESSENTIALS_URL,
            headers={"User-Agent": "twitch-chat-cn-overlay"},
        )
        with urlopen(request, timeout=120.0) as response:  # noqa: S310 - fixed vendor URL
            stream_response_to_path(
                response,
                zip_path,
                max_bytes=MAX_PORTABLE_DOWNLOAD_BYTES,
            )
        print("  解压中…")
        with zipfile.ZipFile(zip_path, "r") as archive:
            safe_extract_zip(archive, payload)
        if _find_ffmpeg_bin(payload) is None:
            raise ValueError("archive does not contain sibling ffmpeg.exe and ffprobe.exe")
        zip_path.unlink(missing_ok=True)
        ready = dest_root.parent / f".{dest_root.name}.ready-{uuid.uuid4().hex}"
        payload.rename(ready)
        atomic_replace_directory(ready, dest_root)
        ready = None
    except Exception as exc:
        print(f"  [FAIL] 便携 FFmpeg 下载/解压失败: {exc}")
        print(f"  请手动从 https://www.gyan.dev/ffmpeg/builds/ 下载 essentials 并解压到 {dest_root}")
        return False
    finally:
        for leftover in (ready, staging_root):
            if leftover is None:
                continue
            try:
                _remove_path(leftover)
            except OSError:
                pass

    found = prepend_tools_ffmpeg_to_path(root)
    if found:
        print(f"  [OK] 便携 FFmpeg: {found}")
        return True
    print(f"  [FAIL] 解压后未找到 bin/ffmpeg.exe，请检查 {dest_root} 目录结构")
    return False

def can_prompt_interactive() -> bool:
    """True when we can ask the user (real TTY, not CI/pipe)."""
    if os.environ.get("CI"):
        return False
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        return False


def offer_fixes(*, assume_yes: bool = False) -> None:
    """Interactive fix menu for missing render deps (FFmpeg first)."""
    prepend_tools_ffmpeg_to_path()
    items = collect_readiness()
    need_ff = any(i.key in ("ffmpeg", "ffprobe") and not i.ok for i in items)
    if not need_ff:
        print("  FFmpeg/ffprobe 已可用，无需自动安装。")
        return

    print("\n-- 自动修复 (FFmpeg) --")
    print(f"  可尝试: 包管理器安装，或下载便携包到 {portable_ffmpeg_dir()}")
    if not try_fix_ffmpeg(assume_yes=assume_yes):
        try_portable_ffmpeg(assume_yes=assume_yes)

    # re-check fonts only print
    items2 = collect_readiness()
    font = next((i for i in items2 if i.key == "font"), None)
    if font and not font.ok:
        print("\n  仍缺 CJK 字体（不会自动安装系统字体）:")
        for c in font.fix_cmds:
            print(f"    {c}")


def maybe_prompt_offer_fixes(*, already_offered: bool = False, assume_yes: bool = False) -> bool:
    """If render deps missing, ask whether to help install (default Yes on TTY).

    Returns True if offer_fixes was invoked.
    """
    if already_offered:
        return False
    prepend_tools_ffmpeg_to_path()
    items = collect_readiness()
    min_ok, _ = readiness_levels(items)
    if min_ok:
        return False
    need_ff = any(i.key in ("ffmpeg", "ffprobe") and not i.ok for i in items)
    if not need_ff:
        # Missing font/python/pkgs only — print commands, no auto package install for fonts.
        return False
    if assume_yes:
        offer_fixes(assume_yes=True)
        return True
    if not can_prompt_interactive():
        print("\n  (非交互/CI: 跳过自动安装询问；可手动: --doctor --offer-fix)")
        return False
    # Default Yes: bat/install users expect to be asked and can just press Enter.
    if _prompt_yes("检测到缺少 FFmpeg，是否尝试帮你安装/下载？", default=True):
        offer_fixes(assume_yes=False)
        return True
    print(f"  已跳过。可稍后运行: {current_cli_invocation()} --doctor --offer-fix")
    return False


def offer_td_cli_guide(*, assume_yes: bool = False) -> bool:
    """Optional enhancement: install or explain TwitchDownloaderCLI.

    Prefer auto download of the platform zip into tools/TwitchDownloaderCLI/
    (user consent). Falls back to manual instructions + optional browser open.
    Returns True if CLI is available or user accepted a guide/install step.
    """
    try:
        from twitch_download import (
            find_twitchdownloader_cli,
            td_install_hints,
            try_portable_td_cli,
        )
    except ImportError:
        print("  [WARN] twitch_download 模块不可用")
        return False

    existing = find_twitchdownloader_cli()
    if existing:
        print(f"  [OK] 已检测到 TwitchDownloaderCLI: {existing}")
        return True

    cmds, urls = td_install_hints()
    print("\n-- 可选增强: TwitchDownloaderCLI --")
    print("  用于从 VOD/Clip URL 自动下载视频 + 带嵌入表情的聊天 HTML，")
    print("  免去打开 GUI 再拖文件。出片本身不强制需要。")
    print("  发布页:")
    for u in urls:
        print(f"    {u}")
    dest = _repo_root() / "tools" / "TwitchDownloaderCLI"
    print(f"  安装目录: {dest}")
    print(f"  装好后: {current_cli_invocation()} --download <url>")
    if not is_console_entry_script(current_cli_script()):
        print("         或 run.bat 菜单 → 下载素材并继续")

    if assume_yes:
        ok = try_portable_td_cli(assume_yes=True, root=_repo_root())
        if ok:
            return True
        print("  自动安装失败；请手动下载 zip 到上述目录。")
        for c in cmds:
            print(f"    {c}")
        return False

    if not can_prompt_interactive():
        print("  (非交互: 跳过；可手动: --offer-td-cli 或设 TWITCHDOWNLOADER_CLI)")
        return False

    # Default No: optional enhancement.
    if _prompt_yes(
        "是否自动下载并安装 TwitchDownloaderCLI 到 tools/（约数十 MB，需网络）？",
        default=False,
    ):
        ok = try_portable_td_cli(assume_yes=True, root=_repo_root())
        if ok:
            return True
        print("  自动安装未成功。")
        if _prompt_yes("是否改打开浏览器手动下载？", default=True):
            try:
                dest.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                print(f"  [WARN] 无法创建目录: {e}")
            try:
                import webbrowser

                webbrowser.open(urls[0])
            except Exception:
                pass
            return True
        return False

    if _prompt_yes("是否仅创建目录并打开说明页（不自动下载）？", default=False):
        try:
            dest.mkdir(parents=True, exist_ok=True)
            readme = dest / "README.txt"
            if not readme.is_file():
                readme.write_text(
                    "Place TwitchDownloaderCLI.exe (Windows) or TwitchDownloaderCLI here.\n"
                    f"Or re-run: {current_cli_invocation()} --offer-td-cli\n"
                    "Download: https://github.com/lay295/TwitchDownloader/releases\n",
                    encoding="utf-8",
                )
            print(f"  已创建: {dest}")
        except OSError as e:
            print(f"  [WARN] 无法创建目录: {e}")
        try:
            import webbrowser

            webbrowser.open(urls[0])
        except Exception:
            pass
        return True

    print(f"  已跳过。需要时再运行: {current_cli_invocation()} --offer-td-cli")
    return False


def maybe_prompt_offer_td_cli(*, assume_yes: bool = False) -> bool:
    """At end of install: ask about optional TwitchDownloaderCLI (default No)."""
    try:
        from twitch_download import find_twitchdownloader_cli
    except ImportError:
        return False
    if find_twitchdownloader_cli():
        return False
    if assume_yes:
        # Non-interactive install scripts: do not surprise with large download
        # unless explicitly --offer-td-cli --yes from user.
        return offer_td_cli_guide(assume_yes=True)
    if not can_prompt_interactive():
        return False
    if _prompt_yes(
        "是否安装可选增强 TwitchDownloaderCLI（可自动下载，免 GUI 下 VOD/聊天）？",
        default=False,
    ):
        return offer_td_cli_guide(assume_yes=False)
    return False
