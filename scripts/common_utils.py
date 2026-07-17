#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small shared helpers used by CLI scripts."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import re
import shlex
import site
import sys
import sysconfig

_DISTRIBUTION_SHARE = Path("share") / "twitch-chat-translator-overlay"
_CONSOLE_ENTRY_NAMES = {
    "twitch-chat-overlay",
    "twitch-chat-overlay.exe",
    "twitch-chat-burn",
    "twitch-chat-burn.exe",
    "twitch-chat-translate",
    "twitch-chat-translate.exe",
}
_SOURCE_SCRIPT_NAMES = {
    "render_cn_chat.py",
    "translate_chat_openai.py",
    "twitch_chat_burn.py",
}
_TRUSTED_EXECUTABLE_DIRS: set[Path] = set()
_DOTENV_LOADED_KEYS: set[str] = set()
_DOTENV_ALLOWED_KEYS = {
    "OPENAI_COMPAT_BASE_URL",
    "OPENAI_COMPAT_MODEL",
    "OPENAI_COMPAT_API_KEY",
    "AGNES_BASE_URL",
    "AGNES_MODEL",
    "AGNES_API_KEY",
}

# Windows otherwise searches the current directory before PATH for bare
# executable names. This protects legacy subprocess calls; security-sensitive
# lookup below also resolves an explicit absolute path.
if os.name == "nt":
    os.environ.setdefault("NoDefaultCurrentDirectoryInExePath", "1")


def _script_name(script: str) -> str:
    cleaned = str(script or "").strip().strip("'").strip('"')
    return cleaned.replace("\\", "/").rsplit("/", 1)[-1].lower()


def is_console_entry_script(script: str) -> bool:
    return _script_name(script) in _CONSOLE_ENTRY_NAMES


def quote_cli_arg(value: str | Path) -> str:
    """Quote one argument for the current platform's interactive shell."""
    text = str(value)
    if os.name == "nt":
        if '"' in text:
            raise ValueError("Windows command arguments cannot contain a double quote")
        return f'"{text}"'
    return shlex.quote(text)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def register_trusted_executable_dir(path: str | Path) -> Path:
    """Allow one explicit executable directory, including trusted repo tools."""
    resolved = Path(path).expanduser().resolve()
    _TRUSTED_EXECUTABLE_DIRS.add(resolved)
    return resolved


def safe_which(command: str) -> str | None:
    """Resolve an executable from absolute PATH entries without trusting cwd."""
    name = str(command or "").strip().strip('"')
    if not name or "/" in name or chr(92) in name:
        return None

    try:
        cwd = Path.cwd().resolve()
    except (OSError, RuntimeError):
        cwd = None

    trusted_dirs = set(_TRUSTED_EXECUTABLE_DIRS)
    try:
        trusted_dirs.add(Path(sys.executable).resolve().parent)
    except (OSError, RuntimeError, TypeError, ValueError):
        pass

    path_value = os.environ.get("PATH") or ""
    if os.name == "nt":
        raw_exts = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
        extensions = [ext.lower() for ext in raw_exts.split(os.pathsep) if ext]
        suffix = Path(name).suffix.lower()
        names = [name] if suffix in extensions else [name, *(name + ext for ext in extensions)]
    else:
        names = [name]

    for raw_dir in path_value.split(os.pathsep):
        raw_dir = raw_dir.strip().strip('"')
        if not raw_dir:
            continue
        expanded = os.path.expandvars(raw_dir)
        directory = Path(expanded).expanduser()
        if not directory.is_absolute():
            continue
        try:
            directory = directory.resolve()
        except (OSError, RuntimeError):
            continue
        trusted = any(directory == item or _is_within(directory, item) for item in trusted_dirs)
        inside_cwd = cwd is not None and directory == cwd
        if inside_cwd and not trusted:
            continue
        for candidate_name in names:
            candidate = directory / candidate_name
            if not candidate.is_file():
                continue
            if os.name != "nt" and not os.access(candidate, os.X_OK):
                continue
            try:
                return str(candidate.resolve())
            except (OSError, RuntimeError):
                return str(candidate)
    return None


def require_executable(command: str) -> str:
    """Return a trusted absolute executable path or raise FileNotFoundError."""
    found = safe_which(command)
    if found is None:
        raise FileNotFoundError(f"{command} was not found in trusted PATH directories")
    return found


def env_loaded_from_dotenv(key: str) -> bool:
    """Whether this process variable was populated by load_dotenv_if_present."""
    return str(key) in _DOTENV_LOADED_KEYS

def format_cli_invocation(script: str) -> str:
    """Return a copy-pasteable source-script or installed-entry command."""
    cleaned = str(script or "scripts/render_cn_chat.py").strip().strip("'").strip('"')
    if is_console_entry_script(cleaned):
        return quote_cli_arg(cleaned)
    python = sys.executable or "python"
    return f"{quote_cli_arg(python)} {quote_cli_arg(cleaned)}"


def current_cli_script() -> str:
    """Return the active source script or installed console entry path."""
    try:
        raw = str(sys.argv[0])
        name = _script_name(raw)
        if name != "-c" and (name in _SOURCE_SCRIPT_NAMES or name in _CONSOLE_ENTRY_NAMES):
            return raw
    except Exception:
        pass
    return "scripts/render_cn_chat.py"


def current_cli_invocation() -> str:
    """Return the active CLI as a copy-pasteable command prefix."""
    return format_cli_invocation(current_cli_script())


def source_checkout_root(module_file: str | Path) -> Path | None:
    """Return the repository root when *module_file* lives in a source checkout."""
    try:
        candidate = Path(module_file).resolve().parent.parent
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if (candidate / "pyproject.toml").is_file() and (candidate / "scripts").is_dir():
        return candidate
    return None


def runtime_app_root(module_file: str | Path) -> Path:
    """Use the source root in a checkout, otherwise the user's current directory."""
    source_root = source_checkout_root(module_file)
    if source_root is not None:
        return source_root
    try:
        return Path.cwd().resolve()
    except (OSError, RuntimeError):
        return trusted_tools_root(module_file)


def trusted_tools_root(module_file: str | Path) -> Path:
    """Return a trusted writable app root for portable executables.

    A source checkout keeps repository-local tools. Installed commands use
    per-user application data, never the arbitrary media cwd.
    """
    source_root = source_checkout_root(module_file)
    if source_root is not None:
        return source_root

    if os.name == "nt":
        base = None if "LOCALAPPDATA" in _DOTENV_LOADED_KEYS else os.environ.get("LOCALAPPDATA")
        if not base and "APPDATA" not in _DOTENV_LOADED_KEYS:
            base = os.environ.get("APPDATA")
        root = Path(base).expanduser() if base else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        base = None if "XDG_DATA_HOME" in _DOTENV_LOADED_KEYS else os.environ.get("XDG_DATA_HOME")
        root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return (root / "twitch-chat-translator-overlay").resolve()


def _active_console_install_root() -> Path | None:
    """Infer the active wheel prefix from its console entry, outside a checkout."""
    if source_checkout_root(__file__) is not None:
        return None
    try:
        raw = str(sys.argv[0])
        if not is_console_entry_script(raw):
            return None
        script_path = Path(raw).expanduser()
        if not script_path.is_absolute():
            located = safe_which(raw)
            if not located:
                return None
            script_path = Path(located)
        script_path = script_path.resolve()
        if script_path.parent.name.lower() not in {"bin", "scripts"}:
            return None
        return script_path.parent.parent
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _module_uses_user_site() -> bool:
    """Whether this module is imported from the active per-user site-packages."""
    try:
        module_path = Path(__file__).resolve()
        user_sites = site.getusersitepackages()
        if isinstance(user_sites, str):
            user_sites = [user_sites]
        return any(
            module_path.is_relative_to(Path(item).expanduser().resolve())
            for item in user_sites
            if item
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return False


def distribution_share_dirs() -> list[Path]:
    """Candidate wheel data roots, preferring the active install over stale ones."""
    data_roots: list[Path] = []

    def add_root(value) -> None:
        if not value:
            return
        try:
            root = Path(value).expanduser()
        except (TypeError, ValueError):
            return
        if root not in data_roots:
            data_roots.append(root)

    add_root(_active_console_install_root())

    user_base = None
    try:
        user_base = site.getuserbase()
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    if _module_uses_user_site():
        add_root(user_base)

    try:
        add_root(sysconfig.get_path("data"))
    except (KeyError, TypeError, ValueError):
        pass
    try:
        add_root(sys.prefix)
    except (TypeError, ValueError):
        pass
    add_root(user_base)

    dirs = [root / _DISTRIBUTION_SHARE for root in data_roots]

    # Retain compatibility with older/local installs that placed data below a
    # sys.path entry instead of the interpreter's data prefix.
    for entry in sys.path:
        if not entry:
            continue
        try:
            share = Path(entry) / _DISTRIBUTION_SHARE
        except (TypeError, ValueError):
            continue
        if share not in dirs:
            dirs.append(share)
    return dirs

def resolve_public_resource(path: str | Path, *, subdir: str) -> Path:
    """Resolve a public data file from cwd/source first, then installed share."""
    candidate = Path(path).expanduser()
    if candidate.is_file() or candidate.is_absolute():
        return candidate.resolve()

    parts = candidate.parts
    if parts and parts[0].lower() == subdir.lower():
        relative = Path(*parts[1:])
    else:
        relative = candidate

    source_root = source_checkout_root(__file__)
    search: list[Path] = []
    if source_root is not None:
        search.append(source_root / subdir / relative)
    search.extend(root / subdir / relative for root in distribution_share_dirs())
    for item in search:
        if item.is_file():
            return item.resolve()
    return candidate.resolve()


def profile_name_candidates(name: str, *, prefix: str) -> list[str]:
    """Expand short preset names: compact → layout_compact.yaml; fast → render_fast.yaml.

    Prefixed filenames are tried *before* bare names so short name ``default``
    resolves to layout_default.yaml / render_default.yaml, not the translation
    profile profiles/default.yaml.
    """
    base = str(name).strip()
    if not base:
        return []
    stem = Path(base).name
    bare = stem
    for suf in (".yaml", ".yml"):
        if bare.endswith(suf):
            bare = bare[: -len(suf)]
            break
    pref = f"{prefix}_" if prefix and not prefix.endswith("_") else prefix
    if pref and bare.startswith(pref):
        core = bare[len(pref) :]
    else:
        core = bare

    names: list[str] = []
    # 1) Prefixed forms first (layout_default.yaml, render_fast.yaml, …)
    for n in (
        f"{pref}{core}.yaml",
        f"{pref}{core}.yml",
        f"{pref}{core}",
        f"{pref}{bare}.yaml",
        f"{pref}{bare}.yml",
    ):
        if n and n not in names:
            names.append(n)
    # 2) Exact path-like stem as given
    if stem and stem not in names:
        names.append(stem)
    # 3) Bare short names last (avoid default.yaml shadowing layout_default)
    for n in (f"{core}.yaml", f"{core}.yml", core, bare, f"{bare}.yaml", f"{bare}.yml"):
        if n and n not in names:
            names.append(n)
    return names


def profiles_search_dirs() -> list[Path]:
    """Directories that may contain public layout_/render_ YAML presets."""
    scripts_dir = Path(__file__).resolve().parent
    dirs: list[Path] = [scripts_dir.parent / "profiles"]
    try:
        cwd_profiles = Path.cwd() / "profiles"
        if cwd_profiles not in dirs:
            dirs.append(cwd_profiles)
    except Exception:
        pass
    for share_root in distribution_share_dirs():
        profiles = share_root / "profiles"
        if profiles not in dirs:
            dirs.append(profiles)
    return [d for d in dirs if d.is_dir()]


def resolve_profiles_preset(path: str | Path, *, prefix: str) -> Path:
    """Resolve a layout/render preset path or short name under profiles/ / wheel share/.

    Search order:
    1. As-is (cwd-relative or absolute file)
    2. Repo profiles/ next to scripts/
    3. Installed ``<sysconfig data>/share/.../profiles/`` resources
    """
    p = Path(path)
    if p.is_file():
        return p
    existing_dirs = profiles_search_dirs()
    for name in profile_name_candidates(p.name if p.name else str(path), prefix=prefix):
        for d in existing_dirs:
            candidate = d / name
            if candidate.is_file():
                return candidate
    return p


def _first_yaml_comment_blurb(text: str, *, max_len: int = 48) -> str:
    """First useful '# ...' comment line (prefer Chinese / non-usage prose)."""
    for raw in text.splitlines():
        s = raw.strip()
        if not s.startswith("#"):
            if s and not s.startswith("---"):
                break
            continue
        body = s.lstrip("#").strip()
        if not body:
            continue
        low = body.lower()
        if low.startswith("用法") or low.startswith("usage") or "layout-preset" in low or "render-preset" in low:
            continue
        # Strip trailing "— detail" keep left title when long
        if "—" in body:
            body = body.split("—", 1)[0].strip()
        elif " - " in body and len(body) > 40:
            body = body.split(" - ", 1)[0].strip()
        if len(body) > max_len:
            body = body[: max_len - 1] + "…"
        return body
    return ""


def discover_presets(kind: str) -> list[dict]:
    """Scan profiles/ for layout_* or render_* YAML and return menu entries.

    Each entry: {
      short, name, label, description, path, menu_text
    }
    ``kind`` is ``layout`` or ``render``.
    Prefer repo profiles over wheel share; de-dupe by short name.
    """
    kind = str(kind or "").strip().lower()
    if kind not in ("layout", "render"):
        raise ValueError("kind must be layout or render")
    prefix = f"{kind}_"
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None

    found: dict[str, dict] = {}
    for d in profiles_search_dirs():
        try:
            files = sorted(d.glob(f"{prefix}*.yaml")) + sorted(d.glob(f"{prefix}*.yml"))
        except OSError:
            continue
        for p in files:
            if not p.is_file():
                continue
            stem = p.stem  # layout_compact
            if not stem.startswith(prefix):
                continue
            short = stem[len(prefix) :] or stem
            if short in found:
                continue  # first search dir wins (repo before share)
            label = ""
            description = ""
            name = stem
            blurb = ""
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                text = ""
            blurb = _first_yaml_comment_blurb(text)
            if yaml is not None and text:
                try:
                    data = yaml.safe_load(text) or {}
                    if isinstance(data, dict):
                        name = str(data.get("name") or stem)
                        label = str(data.get("label") or "").strip()
                        description = str(data.get("description") or "").strip()
                except Exception:
                    pass
            # Menu text: prefer Chinese comment blurb, then description, then label
            detail = blurb or description or label or short
            # Shorten English-only descriptions for console
            if detail and not re.search(r"[一-鿿]", detail) and len(detail) > 42:
                detail = detail[:41] + "…"
            menu_text = f"{short}  - {detail}" if detail and detail != short else short
            found[short] = {
                "short": short,
                "name": name,
                "label": label,
                "description": description,
                "path": str(p.resolve()),
                "menu_text": menu_text,
            }

    # Stable order: default first, then known commons, then alpha
    preferred = ["default", "compact", "mobile", "fast", "hq"]
    keys = list(found.keys())

    def sort_key(k: str) -> tuple:
        try:
            return (0, preferred.index(k), k)
        except ValueError:
            return (1, 0, k)

    keys.sort(key=sort_key)
    return [found[k] for k in keys]


def format_preset_menu_lines(entries: list[dict], *, none_option: bool = True) -> list[str]:
    """Return printable menu lines like '   [1] compact  - ...' plus optional [0]."""
    lines: list[str] = []
    for i, e in enumerate(entries, 1):
        lines.append(f"   [{i}] {e.get('menu_text') or e.get('short')}")
    if none_option:
        lines.append("   [0] 不写（用程序默认）")
    return lines


def pick_preset_from_menu(
    entries: list[dict],
    choice: str,
    *,
    default_index: int = 1,
) -> str | None:
    """Map user choice to short name. Empty choice -> default_index. '0' -> None."""
    choice = (choice or "").strip()
    if choice in ("0", "none", "n"):
        return None
    if not choice:
        choice = str(default_index)
    if choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(entries):
            return str(entries[idx - 1]["short"])
        return None
    # allow typing short name directly
    low = choice.lower()
    for e in entries:
        if str(e.get("short", "")).lower() == low or str(e.get("name", "")).lower() == low:
            return str(e["short"])
    return choice  # pass through for resolve later


def detect_cjk_font() -> tuple[str | None, str | None]:
    """Auto-detect a CJK-capable font. Returns (regular, bold) or (None, None).

    Never invents a platform-foreign fallback path. Callers must handle None.
    """
    system = platform.system()
    candidates: list[tuple[str, str]] = []
    if system == "Windows":
        candidates = [
            (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc"),
            (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyhbd.ttc"),
            (r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simhei.ttf"),
            (r"C:\Windows\Fonts\simsun.ttc", r"C:\Windows\Fonts\simsun.ttc"),
            (r"C:\Windows\Fonts\msjh.ttc", r"C:\Windows\Fonts\msjhbd.ttc"),
        ]
    elif system == "Darwin":
        candidates = [
            ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/PingFang.ttc"),
            ("/System/Library/Fonts/Hiragino Sans GB.ttc", "/System/Library/Fonts/Hiragino Sans GB.ttc"),
            ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
        ]
    else:
        # Linux / CI: prefer Noto CJK, then WenQuanYi, then any discovered CJK file.
        candidates = [
            (
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            ),
            (
                "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
                "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
            ),
            (
                "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
            ),
            (
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
            ),
            (
                "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Regular.otf",
                "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Bold.otf",
            ),
            ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
            ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
            ("/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc", "/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc"),
            ("/usr/share/fonts/wqy-microhei/wqy-microhei.ttc", "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc"),
        ]

    for reg, bold in candidates:
        if os.path.isfile(reg):
            return reg, (bold if bold and os.path.isfile(bold) else reg)

    # Last-resort scan on Linux/macOS font roots for any CJK-ish family.
    search_roots: list[str] = []
    if system == "Windows":
        search_roots = [r"C:\Windows\Fonts"]
    elif system == "Darwin":
        search_roots = ["/System/Library/Fonts", "/Library/Fonts"]
    else:
        search_roots = ["/usr/share/fonts", "/usr/local/share/fonts"]

    keywords = ("noto", "cjk", "wqy", "sourcehan", "droid", "pingfang", "msyh", "simhei", "simsun")
    found: list[str] = []
    for root in search_roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                lower = name.lower()
                if not lower.endswith((".ttf", ".ttc", ".otf")):
                    continue
                if any(k in lower for k in keywords):
                    found.append(os.path.join(dirpath, name))
            if len(found) >= 20:
                break
        if len(found) >= 20:
            break
    if found:
        # Prefer Regular over Bold when both exist.
        found.sort(key=lambda p: (("regular" not in p.lower()), ("bold" in p.lower()), p.lower()))
        reg = found[0]
        bold = next((p for p in found if "bold" in p.lower()), reg)
        return reg, bold
    return None, None


def resolve_font_paths(
    font_path: str | None = "auto",
    font_bold_path: str | None = "auto",
) -> tuple[str, str]:
    """Resolve auto/empty font paths. Raises FileNotFoundError if unavailable."""
    reg_in = (font_path or "auto").strip() or "auto"
    bold_in = (font_bold_path or "auto").strip() or "auto"

    reg: str | None
    bold: str | None
    if reg_in == "auto" or bold_in == "auto":
        auto_reg, auto_bold = detect_cjk_font()
    else:
        auto_reg, auto_bold = None, None

    if reg_in == "auto":
        reg = auto_reg
    else:
        reg = reg_in
    if bold_in == "auto":
        bold = auto_bold or reg
    else:
        bold = bold_in

    if not reg or not os.path.isfile(reg):
        raise FileNotFoundError(
            "No usable CJK font found. Install a CJK font (e.g. fonts-noto-cjk / ????) "
            "or pass --font-path / --font-bold-path."
        )
    if not bold or not os.path.isfile(bold):
        bold = reg
    return reg, bold


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Parse #RGB / #RRGGBB into an RGB tuple. Raises ValueError on bad input."""
    h = (hex_color or "").strip().lstrip("#")
    if len(h) == 3 and all(c in "0123456789abcdefABCDEF" for c in h):
        h = "".join(ch * 2 for ch in h)
    if len(h) != 6 or any(c not in "0123456789abcdefABCDEF" for c in h):
        raise ValueError(f"invalid hex color: {hex_color!r}")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def hex_to_rgb_soft(hex_color: str, default: tuple[int, int, int] = (255, 255, 255)) -> tuple[int, int, int]:
    """Like hex_to_rgb, but invalid colors fall back to default (author colors must not crash render)."""
    try:
        return hex_to_rgb(hex_color)
    except ValueError:
        return default


def normalize_text(text: str) -> str:
    """Light whitespace normalize for chat fragments."""
    text = (text or "").replace("\r", " ").replace("\n", " ")
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def validate_positive_int(name: str, value: int, minimum: int = 1, maximum: int | None = None) -> int:
    if not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def validate_non_negative_float(name: str, value: float, maximum: float | None = None) -> float:
    value = float(value)
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def validate_positive_float(
    name: str,
    value: float,
    minimum: float = 1e-9,
    maximum: float | None = None,
) -> float:
    """Require a strictly positive float (rejects 0 and negatives)."""
    value = float(value)
    if value < minimum or value <= 0:
        raise ValueError(f"{name} must be > 0")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def positive_float_arg(value: str) -> float:
    """argparse type: float that must be > 0 (rejects 0)."""
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"invalid float value: {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be > 0 (0 is not allowed)")
    return parsed


def ensure_utf8_stdio() -> None:
    """Best-effort UTF-8 for stdout/stderr (Windows CI often defaults to cp1252).

    Chinese log lines must not crash the process with UnicodeEncodeError.
    Safe to call multiple times; ignores streams that cannot be reconfigured.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def load_dotenv_if_present() -> None:
    """Load translation API keys from the first .env without overriding process vars.

    Search order (wheel-friendly):
    1. current working directory
    2. repository root when running from a source checkout (`scripts/` parent)
    3. parent of this module file (legacy editable/source layout)
    """
    # Tests may deliberately clear API env vars; do not rehydrate them.
    if os.environ.get("_TWITCH_TRANSPARENT_TEST_MODE") == "1":
        return

    candidates: list[Path] = []
    try:
        candidates.append(Path.cwd() / ".env")
    except Exception:
        pass
    try:
        module_path = Path(__file__).resolve()
        # Source layout: <repo>/scripts/common_utils.py -> <repo>/.env
        candidates.append(module_path.parents[1] / ".env")
        # Wheel/editable flat module layout fallback: next to installed module
        candidates.append(module_path.parent / ".env")
    except Exception:
        pass

    seen: set[Path] = set()
    for env_path in candidates:
        try:
            resolved = env_path.resolve()
        except Exception:
            resolved = env_path
        if resolved in seen:
            continue
        seen.add(resolved)
        if not env_path.is_file():
            continue
        try:
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key in _DOTENV_ALLOWED_KEYS and key not in os.environ:
                    os.environ[key] = val
                    _DOTENV_LOADED_KEYS.add(key)
        except OSError:
            return
        # Only load the first existing .env.
        return
