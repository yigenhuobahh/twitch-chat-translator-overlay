#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Interactive job.yaml wizard: guided create + Chinese run menu."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

from common_utils import (
    current_cli_invocation,
    discover_presets,
    format_preset_menu_lines,
    pick_preset_from_menu,
    safe_which,
)
from job_config import (
    default_jobs_dir,
    last_job_path,
    list_job_files,
    load_job_file,
    resolve_job_arg,
    save_last_job,
    summarize_job,
    write_job_file,
)

_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}


def run_drag_drop(arguments: list[str]) -> int:
    """Route dropped media to a short API-free preview or an existing job."""
    paths = [Path(_normalize_user_path(value)).expanduser() for value in arguments]
    existing = [path for path in paths if path.is_file()]
    jobs = [path for path in existing if path.suffix.lower() in {".yaml", ".yml"}]
    if jobs:
        return _confirm_and_run_job(jobs[0], extra_cli=[str(value) for value in arguments[1:]])

    video = next((path for path in existing if path.suffix.lower() in _VIDEO_EXTENSIONS), None)
    chat = next((path for path in existing if path.suffix.lower() in {".html", ".htm"}), None)
    if video is not None and chat is None:
        guessed = _guess_chat_html(video)
        if guessed:
            chat = Path(guessed)
            print(f"[drag] detected chat HTML: {chat}")
        elif _stdin_is_interactive():
            try:
                chat = Path(_prompt_path("  Chat HTML", must_exist=True))
            except (EOFError, FileNotFoundError) as exc:
                print(f"[FAIL] {exc}")
                return 1
    if video is not None and chat is not None:
        print("[drag] creating a 10-second original-chat preview (no translation API needed).")
        return _run_pipeline(
            str(video), str(chat), "--mode", "preview", "--render-original", "--preview-clip", "10", "--yes"
        )

    if arguments:
        try:
            path = resolve_job_arg(arguments[0])
        except ValueError:
            print("[FAIL] Drop a video + chat HTML pair, or a job YAML file.")
            return 1
        return _confirm_and_run_job(path, extra_cli=arguments[1:])
    return run_menu()


def run_quick_start() -> int:
    """Scaffold first-run files, then use the existing job wizard."""
    from ux_setup import run_init

    if run_init(create_job=True) != 0:
        return 1
    print("\nNext: choose a purpose and layout. You can drag media onto run.bat later.")
    created = run_job_wizard()
    return 0 if created is None else _confirm_and_run_job(created)


def _stdin_is_interactive() -> bool:
    """True only when we can reasonably prompt the user (real TTY, not piped/devnull)."""
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return False
    except Exception:
        return False
    # Windows / CI sometimes still reports isatty on non-usable streams.
    try:
        name = getattr(sys.stdin, "name", "") or ""
        if name in ("nul", "NUL", "/dev/null"):
            return False
    except Exception:
        pass
    return True


def _prompt(msg: str, default: str | None = None) -> str:
    if default is not None and default != "":
        suffix = f" [{default}]"
    else:
        suffix = ""
    try:
        raw = input(f"{msg}{suffix}: ").strip()
    except EOFError as e:
        # Non-interactive / closed stdin: do not loop forever.
        if default is not None:
            return default
        raise EOFError("标准输入已关闭，无法交互询问") from e
    if not raw and default is not None:
        return default
    return raw


def _normalize_user_path(value: str) -> str:
    """Clean pasted Windows paths (quotes, file://, accidental 'E:\\video:\\...')."""
    s = (value or "").strip().strip('"').strip("'").strip()
    # file:///C:/... or file://localhost/C:/...
    if s.lower().startswith("file:"):
        s = re.sub(r"^file:(//|\\\\)+", "", s, flags=re.I)
        s = s.lstrip("/")
        # file:///E:/foo -> E:/foo
        if re.match(r"^[A-Za-z]:", s) is None and re.match(r"^[A-Za-z]%3A", s, re.I):
            s = s.replace("%3A", ":", 1)
    # PowerShell / drag-drop sometimes yields E:\video:\foo when cwd is E:\video
    # Fix drive letter + extra colon:  X:\something:\rest  or  X:\:\rest
    m = re.match(r"^([A-Za-z]):[\\/]([^:\\/]+):[\\/](.*)$", s)
    if m:
        drive, first, rest = m.group(1), m.group(2), m.group(3)
        # If first segment looks like a folder name that was cwd, drop the extra ":"
        s = f"{drive}:\\{first}\\{rest}"
    else:
        m2 = re.match(r"^([A-Za-z]):[\\/]:[\\/]?(.*)$", s)
        if m2:
            s = f"{m2.group(1)}:\\{m2.group(2)}"
    # Collapse accidental double separators (keep \\ for UNC)
    if not s.startswith("\\\\"):
        s = re.sub(r"[\\/]{2,}", lambda m: "\\" if "\\" in m.group(0) else "/", s)
    return s


def _path_not_found_hints(p: Path) -> None:
    """Print short recovery hints when a path is missing."""
    s = str(p)
    print(f"  文件不存在: {s}")
    if re.search(r"^[A-Za-z]:[\\/][^:\\/]+:[\\/]", s):
        print("  提示: 路径里多了一个冒号（例如 E:\\video:\\文件.mp4）。")
        print("        请改成 E:\\download\\文件.mp4 这种「盘符:\\目录\\文件」形式。")
    if "[" in s or "]" in s:
        print("  提示: 文件名含 [ ] 时请整段用英文引号包起来，或直接拖文件到窗口。")
    # Suggest likely sibling if user used wrong folder but right filename
    name = p.name
    if name:
        for folder in (Path("E:/download"), Path("E:/video"), Path.cwd()):
            try:
                cand = folder / name
                if cand.is_file():
                    print(f"  是否本意: {cand}")
                    break
            except OSError:
                pass


def _prompt_path(msg: str, default: str | None = None, *, must_exist: bool = False) -> str:
    empty_tries = 0
    while True:
        try:
            value = _prompt(msg, default)
        except EOFError:
            raise
        value = _normalize_user_path(value)
        if not value:
            empty_tries += 1
            print("  路径不能为空")
            if empty_tries >= 3:
                raise EOFError("多次未输入路径，已中止（非交互或输入为空）")
            # If no default and stdin not interactive, fail fast
            if default is None and not _stdin_is_interactive():
                raise EOFError("非交互终端无法询问路径")
            continue
        p = Path(value).expanduser()
        if must_exist and not p.is_file():
            _path_not_found_hints(p)
            if not _stdin_is_interactive():
                # Soft cancel for piped/menu scripts — callers map this to "已取消".
                raise FileNotFoundError(f"文件不存在: {p}")
            try:
                cont = _prompt("  重新输入路径？(Y=重试 / n=取消)", "y").lower()
            except EOFError as e:
                raise FileNotFoundError(f"文件不存在: {p}") from e
            if cont in ("n", "no", "c", "cancel", "0"):
                raise FileNotFoundError(f"文件不存在: {p}")
            continue
        return str(p)


def _safe_stem(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[^\w\-.一-鿿]+", "_", name, flags=re.UNICODE)
    name = name.strip("._") or "job"
    return name[:80]


def _guess_chat_html(video: Path) -> str | None:
    parent = video.parent if video.parent.is_dir() else Path.cwd()
    stem = video.stem
    candidates = [
        parent / f"{stem}.html",
        parent / f"{stem}_chat.html",
        parent / f"{stem}.chat.html",
        parent / "chat.html",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    try:
        for c in sorted(parent.glob("*chat*.html")):
            if c.is_file():
                return str(c)
    except OSError:
        pass
    return None


def _guess_translation_json(video: Path) -> list[Path]:
    """Likely translation JSON locations next to the video / common folders."""
    parent = video.parent if video.parent.is_dir() else Path.cwd()
    stem = video.stem
    cands = [
        parent / f"{stem}_translation.json",
        parent / f"{stem}.translation.json",
        parent / "translations" / f"{stem}_translation.json",
        parent / "translations" / f"{stem}.json",
        Path.cwd() / "translations" / f"{stem}_translation.json",
        Path.cwd() / f"{stem}_translation.json",
    ]
    return [p for p in cands if p.is_file()]


def _pipeline_cmd(*args: str) -> list[str]:
    """Prefer sibling render_cn_chat.py under scripts/."""
    script = Path(__file__).resolve().parent / "render_cn_chat.py"
    return [sys.executable, str(script), *args]


def _run_pipeline(*args: str) -> int:
    cmd = _pipeline_cmd(*args)
    print("\n$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    try:
        p = subprocess.run(cmd)
        return int(p.returncode)
    except OSError as e:
        print(f"[FAIL] 无法启动: {e}")
        return 1


def _external_command(value: str) -> list[str] | None:
    """Resolve an editor/helper command without Windows cwd executable search."""
    try:
        parts = shlex.split(str(value), posix=os.name != "nt")
    except ValueError:
        return None
    if not parts:
        return None
    executable = parts[0].strip('"')
    explicit = Path(executable).expanduser()
    if explicit.is_absolute() and explicit.is_file():
        resolved = str(explicit.resolve())
    else:
        resolved = safe_which(executable)
    return [resolved, *parts[1:]] if resolved else None

def _open_editor(path: Path) -> None:
    """Open job YAML in a simple editor without flooding the console.

    On Windows, avoid os.startfile(.yaml): file association may launch an IDE /
    store app that dumps logs into this console or steals focus chaotically.
    Prefer notepad (silent, no stdout), then EDITOR, then startfile as last resort.
    """
    path = Path(path)
    if not path.is_file():
        print(f"文件不存在，无法编辑: {path}")
        return
    path = path.resolve()
    print(f"  正在打开编辑器: {path}")
    print("  改完后请保存并关闭编辑器，再回到本窗口按提示继续。")
    if os.name == "nt":
        # 1) Notepad: predictable, no console spam
        notepad = safe_which("notepad.exe")
        if notepad:
            try:
                subprocess.Popen(
                    [notepad, str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
                return
            except OSError:
                pass
        # 2) User EDITOR if set (still swallow output)
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
        editor_cmd = _external_command(editor) if editor else None
        if editor_cmd:
            try:
                subprocess.Popen(
                    [*editor_cmd, str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    shell=False,
                )
                return
            except OSError:
                pass
        # 3) Last resort: system association (may be noisy — warn)
        try:
            print("  [提示] 将用系统默认程序打开 YAML；若控制台刷屏，请改用记事本关联 .yaml")
            os.startfile(str(path))  # type: ignore[attr-defined]
            return
        except OSError as e:
            print(f"  无法打开编辑器: {e}")
            print(f"  请手动编辑: {path}")
            return
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
    editor_cmd = _external_command(editor)
    try:
        if not editor_cmd:
            raise FileNotFoundError(f"找不到编辑器: {editor}")
        subprocess.run([*editor_cmd, str(path)], check=False)
    except OSError as e:
        print(f"  无法打开编辑器 {editor}: {e}")
        print(f"  请手动编辑: {path}")


def _open_folder(path: Path) -> None:
    """Open containing folder in the OS file manager (best-effort)."""
    path = Path(path)
    folder = path if path.is_dir() else path.parent
    if not folder.is_dir():
        print(f"目录不存在: {folder}")
        return
    try:
        if os.name == "nt":
            # Prefer select the file when possible
            explorer = safe_which("explorer.exe")
            if path.is_file() and explorer:
                subprocess.run([explorer, "/select,", str(path.resolve())], check=False)
            else:
                os.startfile(str(folder))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            opener = safe_which("open")
            if opener:
                subprocess.run([opener, str(folder)], check=False)
        else:
            opener = safe_which("xdg-open")
            if opener:
                subprocess.run([opener, str(folder)], check=False)
    except OSError as e:
        print(f"无法打开文件夹: {e}")


def _infer_output_from_job(job: dict, job_path: Path) -> Path | None:
    """Best-effort final output path from job fields."""
    out = job.get("output")
    if out:
        p = Path(str(out))
        if p.is_file():
            return p
    video = job.get("video")
    if video:
        vp = Path(str(video))
        candidate = vp.with_name(vp.stem + "_chat.mp4")
        if candidate.is_file():
            return candidate
        workdir = job.get("workdir")
        if workdir:
            wd = Path(str(workdir))
            for pat in (f"{vp.stem}_chat.mp4", "*_chat.mp4"):
                hits = list(wd.glob(pat)) if wd.is_dir() else []
                if hits:
                    return max(hits, key=lambda x: x.stat().st_mtime)
    # last resort: recent mp4 next to job
    parent = job_path.parent
    hits = sorted(parent.glob("*_chat.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return hits[0] if hits else None


def _apply_extra_cli_path_overrides(
    session: dict | None,
    extra_cli: list[str] | None,
) -> dict:
    """Merge path-related flags from extra_cli into session (CLI wins).

    Simple scan: ``--flag value`` or ``--flag=value``. Used so post-run
    report/open-folder/clean use the same paths the pipeline received.
    """
    out = dict(session or {})
    if not extra_cli:
        return out
    flags = {
        "--output": "output",
        "--workdir": "workdir",
        "--translation-json": "translation_json",
    }
    args = [str(a) for a in extra_cli]
    i = 0
    while i < len(args):
        a = args[i]
        key = flags.get(a)
        if key is not None:
            if i + 1 < len(args) and not str(args[i + 1]).startswith("-"):
                out[key] = args[i + 1]
                i += 2
                continue
            i += 1
            continue
        for flag, skey in flags.items():
            prefix = flag + "="
            if a.startswith(prefix) and len(a) > len(prefix):
                out[skey] = a[len(prefix) :]
                break
        i += 1
    return out


def _resolve_clean_root(session: dict | None, job: dict | None) -> Path | None:
    """Prefer workdir/temp, then workdir, then video parent."""
    session = dict(session or {})
    job = dict(job or {})
    workdir = session.get("workdir") or job.get("workdir")
    if workdir:
        root = Path(str(workdir)).expanduser()
        temp = root / "temp"
        return temp if temp.is_dir() else root
    video = session.get("video") or job.get("video")
    if video:
        vp = Path(str(video)).expanduser()
        if vp.is_file():
            return vp.parent
        if vp.parent.is_dir():
            return vp.parent
    return None


def _maybe_clean_temp_after_run(session: dict | None, job: dict | None) -> None:
    """Ask whether to clean temp artifacts under this run's workdir/temp only.

    Scope is intentionally narrow (aligns with safer CLI default):
    - Prefer workdir/temp; if only video parent is known, refuse bulk clean
      (would risk wiping sibling job_* next to the VOD).
    - clean_all=False: only *.partial* under that root (not every finished job).
    """
    from process_util import clean_temp_artifacts, is_dangerous_publish_path

    session = dict(session or {})
    job = dict(job or {})
    workdir = session.get("workdir") or job.get("workdir")
    if not workdir:
        # No isolated workdir: do not offer wiping the video folder.
        return
    root = Path(str(workdir)).expanduser()
    temp = root / "temp"
    clean_root = temp if temp.is_dir() else root
    if not clean_root.is_dir():
        return
    # Default N so batch scripts / Enter-through users keep resume artifacts.
    ans = _prompt(
        f"是否清理临时文件（仅 {clean_root} 下 *.partial*）？(y/N)",
        "n",
    ).lower()
    if ans not in ("y", "yes", "1"):
        return
    if is_dangerous_publish_path(clean_root):
        print(f"  跳过清理（系统目录）: {clean_root}")
        return
    try:
        count, freed = clean_temp_artifacts(
            clean_root,
            clean_all=False,
            clean_progress=False,
        )
    except OSError as e:
        print(f"  清理失败: {e}")
        return
    print(f"  清理完成: {count} 项, 释放 {freed / (1024 * 1024):.1f} MB")


def _report_run_success(job_path: Path, session: dict | None = None) -> None:
    """Print output path/size, offer to open folder, then optional temp clean."""
    try:
        job = load_job_file(job_path)
    except Exception:
        job = {}
    # Session overrides for this-run output/video/workdir (not always written back).
    if session:
        job = dict(job)
        for key in ("output", "video", "workdir", "chat_html"):
            if session.get(key):
                job[key] = session[key]
    out = _infer_output_from_job(job, job_path)
    print("\n[OK] 任务结束。")
    if out and out.is_file():
        size_mb = out.stat().st_size / (1024 * 1024)
        print(f"  输出文件: {out}")
        print(f"  大小: {size_mb:.2f} MB")
        open_it = _prompt("是否打开所在文件夹？(y/N)", "n").lower()
        if open_it in ("y", "yes", "1"):
            _open_folder(out)
    else:
        # Still show configured path if any
        cfg_out = job.get("output") if isinstance(job, dict) else None
        if cfg_out:
            print(f"  配置中的输出路径: {cfg_out}")
            print("  （未在磁盘找到文件；若是仅导出/预览图，请查看配置旁或 workdir）")
        else:
            print("  （未解析到输出文件；预览图/导出 JSON 请查看配置中的路径）")
    _maybe_clean_temp_after_run(session, job)


def _prompt_translation_json(video_p: Path) -> str | None:
    """Ask for translation JSON with existence check and recovery options.

    Returns path string, or None if user aborts / switches purpose.
    Special return via raising SystemExit is avoided — caller handles codes:
      - path str
      - empty string means switch to purpose 2 (translate first)
      - None means cancel
    """
    found = _guess_translation_json(video_p)
    default_tj = str(video_p.with_name(video_p.stem + "_translation.json"))
    if found:
        print("  发现可能的翻译文件:")
        for i, p in enumerate(found[:5], 1):
            print(f"    [{i}] {p}")
        print(f"    或手动输入路径（默认: {default_tj}）")
        choice = _prompt("  选择编号或路径", "1" if found else default_tj)
        if choice.isdigit() and 1 <= int(choice) <= len(found):
            return str(found[int(choice) - 1])
        candidate = choice.strip().strip('"').strip("'") or default_tj
    else:
        print("  未在视频旁找到现成的翻译 JSON。")
        print(f"  默认会找: {default_tj}")
        candidate = _prompt("  translation_json 路径（回车用默认）", default_tj)

    p = Path(candidate).expanduser()
    if p.is_file():
        return str(p.resolve())

    print(f"\n  [!] 翻译文件不存在: {p}")
    print("  [1] 重新输入路径")
    print("  [2] 改为「翻译出片」（先跑 API 翻译）")
    print("  [3] 改为「预览原文」（不翻译）")
    print("  [0] 取消")
    act = _prompt("  请选择", "2")
    if act == "1":
        return _prompt_translation_json(video_p)
    if act == "2":
        return ""  # signal: switch to full translate
    if act == "3":
        return "__preview__"
    return None


def _session_paths_from_job(job: dict) -> dict[str, str]:
    """Return only non-empty path fields already pinned in the job YAML."""
    out: dict[str, str] = {}
    for key in ("video", "chat_html", "output", "translation_json", "workdir"):
        val = job.get(key)
        if val is None or str(val).strip() == "":
            continue
        out[key] = str(val).strip()
    return out


def _prompt_session_media(
    job: dict,
    *,
    prior: dict[str, str] | None = None,
    force_ask: bool = False,
) -> dict[str, str] | None:
    """Ask for video/HTML (and maybe translation) when not pinned in YAML.

    Returns session-only paths (never written back to the job file), or None if cancelled.

    prior: paths already collected this run (e.g. before opening the editor).
    If prior has a valid video/HTML and the job still does not pin those fields,
    reuse prior instead of asking again.
    force_ask: always prompt even when prior is valid.
    """
    session: dict[str, str] = {}
    pinned = _session_paths_from_job(job)
    prior = dict(prior or {})

    try:
        # --- video ---
        if "video" in pinned and Path(pinned["video"]).is_file():
            session["video"] = pinned["video"]
            print(f"  使用配置中的视频: {session['video']}")
        elif (
            not force_ask
            and prior.get("video")
            and Path(prior["video"]).is_file()
            and "video" not in pinned
        ):
            session["video"] = prior["video"]
            print(f"  继续使用本次视频: {session['video']}")
        else:
            if "video" in pinned:
                print(f"  配置中的 video 无效或不存在: {pinned['video']}")
            elif not prior.get("video"):
                print("  配置未固定视频路径（推荐：每次询问，便于复用同一套布局/编码）。")
            video = _prompt_path("  本次源视频路径", prior.get("video"), must_exist=True)
            session["video"] = video

        video_p = Path(session["video"])

        # --- chat html ---
        if "chat_html" in pinned and Path(pinned["chat_html"]).is_file():
            session["chat_html"] = pinned["chat_html"]
            print(f"  使用配置中的 HTML: {session['chat_html']}")
        elif (
            not force_ask
            and prior.get("chat_html")
            and Path(prior["chat_html"]).is_file()
            and "chat_html" not in pinned
        ):
            session["chat_html"] = prior["chat_html"]
            print(f"  继续使用本次 HTML: {session['chat_html']}")
        else:
            if "chat_html" in pinned:
                print(f"  配置中的 chat_html 无效或不存在: {pinned['chat_html']}")
            guess = prior.get("chat_html") or _guess_chat_html(video_p)
            chat = _prompt_path("  本次聊天 HTML 路径", guess, must_exist=True)
            session["chat_html"] = chat
    except (EOFError, FileNotFoundError) as e:
        print(f"  已中止输入: {e}")
        return None

    if "output" in pinned:
        session["output"] = pinned["output"]
    elif prior.get("output"):
        session["output"] = prior["output"]
    # else: pipeline default <video>_chat.mp4

    if "workdir" in pinned:
        session["workdir"] = pinned["workdir"]
    elif prior.get("workdir"):
        session["workdir"] = prior["workdir"]

    # translation only when reuse mode
    if job.get("reuse_translation") and not job.get("render_original"):
        if "translation_json" in pinned and Path(pinned["translation_json"]).is_file():
            session["translation_json"] = pinned["translation_json"]
            print(f"  使用配置中的翻译 JSON: {session['translation_json']}")
        elif (
            not force_ask
            and prior.get("translation_json")
            and Path(prior["translation_json"]).is_file()
            and "translation_json" not in pinned
        ):
            session["translation_json"] = prior["translation_json"]
            print(f"  继续使用本次翻译 JSON: {session['translation_json']}")
        else:
            tj = _prompt_translation_json(video_p)
            if tj is None:
                return None
            if tj == "":
                print("  [提示] 配置为 reuse，但未找到翻译文件；请改用途或先翻译。")
                return None
            if tj == "__preview__":
                print("  [提示] 预览模式请改配置 render_original: true，或新建预览配置。")
                return None
            session["translation_json"] = tj

    return session


def _confirm_and_run_job(path: Path, extra_cli: list[str] | None = None) -> int:
    path = Path(path)
    print(f"\n将使用配置: {path.name}")
    print(f"  {summarize_job(path)}")
    try:
        job = load_job_file(path)
    except (OSError, ValueError) as e:
        print(f"[FAIL] 无法读取配置: {e}")
        return 1

    # Paths: pinned in YAML → use file; missing/commented → ask for this run only (not written back).
    try:
        session = _prompt_session_media(job)
    except (EOFError, FileNotFoundError) as e:
        print(f"[FAIL] {e}")
        print("  已取消本次运行（配置文件未改动）。")
        # Non-interactive / bad path: fail so automation does not treat cancel as success.
        return 1 if (not _stdin_is_interactive() or isinstance(e, FileNotFoundError)) else 0
    if session is None:
        # Soft cancel from missing media: fail in pipes/CI, 0 when user cancelled interactively.
        if not _stdin_is_interactive():
            print("已取消（非交互：按失败退出）")
            return 1
        print("已取消")
        return 0

    # Apply CLI path overrides before the confirm screen so the plan matches the pipeline.
    if extra_cli:
        session = _apply_extra_cli_path_overrides(session, extra_cli)

    # Show a short plan then run (no second "are you sure?" unless user wants to edit).
    print("\n本次将使用:")
    print(f"  视频: {session.get('video')}")
    print(f"  HTML: {session.get('chat_html')}")
    if session.get("output"):
        print(f"  输出: {session['output']}")
    else:
        try:
            vp = Path(session["video"])
            print(f"  输出: {vp.with_name(vp.stem + '_chat.mp4')} （默认）")
        except Exception:
            pass
    if session.get("workdir"):
        print(f"  工作目录: {session['workdir']}")
    print("  （路径只用于本次，不会写回配置）")
    try:
        go = _prompt("回车开始渲染，E=编辑配置，C=取消", "")
    except EOFError:
        print("已取消（无更多输入）")
        return 1 if not _stdin_is_interactive() else 0
    if go.lower() in ("c", "cancel", "n", "no"):
        print("已取消")
        return 0
    if go.lower() in ("e", "edit"):
        _open_editor(path)
        try:
            go2 = _prompt("编辑完成并保存后，回车开始运行，C=取消", "")
        except EOFError:
            print("已取消（无更多输入）")
            return 1 if not _stdin_is_interactive() else 0
        if go2.lower() in ("c", "cancel", "n", "no"):
            print("已取消")
            return 0
        try:
            job = load_job_file(path)
        except (OSError, ValueError) as e:
            print(f"[FAIL] {e}")
            return 1
        # Keep this-run video/HTML; only re-ask if edit pinned new paths or prior invalid.
        try:
            session2 = _prompt_session_media(job, prior=session, force_ask=False)
        except (EOFError, FileNotFoundError) as e:
            print(f"[FAIL] {e}")
            return 1
        if session2 is None:
            print("已取消")
            return 1 if not _stdin_is_interactive() else 0
        session = session2
        if extra_cli:
            session = _apply_extra_cli_path_overrides(session, extra_cli)

    save_last_job(path)
    # Build CLI: job style + this-run paths (never auto-written into YAML).
    # Path flags already merged from extra_cli into session — emit once (avoid double --output).
    extra: list[str] = ["--job", str(path)]
    extra.extend([str(session["video"]), str(session["chat_html"])])
    if session.get("output"):
        extra.extend(["--output", session["output"]])
    if session.get("workdir"):
        extra.extend(["--workdir", session["workdir"]])
    if session.get("translation_json"):
        extra.extend(["--translation-json", session["translation_json"]])
        if job.get("reuse_translation"):
            extra.append("--reuse-translation")
    if extra_cli:
        # Forward non-path extras only (paths already applied above).
        path_flags = {"--output", "--workdir", "--translation-json"}
        skip_next = False
        for i, a in enumerate(extra_cli):
            if skip_next:
                skip_next = False
                continue
            s = str(a)
            if s in path_flags:
                # skip flag and its value if present
                if i + 1 < len(extra_cli) and not str(extra_cli[i + 1]).startswith("-"):
                    skip_next = True
                continue
            if any(s.startswith(f + "=") for f in path_flags):
                continue
            extra.append(s)
    rc = _run_pipeline(*extra)
    if rc != 0:
        print(f"\n[FAIL] 退出码 {rc}。可先做环境检查，或核对本次输入的视频/HTML 路径。")
        print("  提示: 菜单 [5] 环境检查  ·  确认 mp4/html 路径与磁盘文件一致")
    else:
        _report_run_success(path, session=session)
    return rc


def print_preset_catalog() -> None:
    """Show layout/render presets discovered under profiles/ (not jobs/)."""
    print("# 可用布局/编码预设（profiles/，在「新建配置」第 5/6 步选择）")
    layouts = discover_presets("layout")
    renders = discover_presets("render")
    if layouts:
        print("  布局 layout_preset:")
        for e in layouts:
            print(f"    - {e.get('menu_text') or e.get('short')}")
    else:
        print("  布局: （未找到 profiles/layout_*.yaml）")
    if renders:
        print("  编码 render_preset:")
        for e in renders:
            print(f"    - {e.get('menu_text') or e.get('short')}")
    else:
        print("  编码: （未找到 profiles/render_*.yaml）")
    print("  提示: 预设不是任务配置；任务配置在 jobs/，用菜单 [1] 新建或 [2] 运行。")


def print_job_list(jobs_dir: Path | None = None, *, show_presets: bool = True) -> list[Path]:
    root = jobs_dir or default_jobs_dir()
    files = list_job_files(root)
    print("# 可用任务配置 jobs/ （可复用的模式/布局/编码）")
    print(f"# 目录: {root}")
    if not files:
        print("  （无任务配置）请先选菜单 [1] 新建配置")
    else:
        last = last_job_path(root)
        for i, p in enumerate(files, 1):
            mark = "  ← 上次" if last and p.resolve() == last.resolve() else ""
            print(f"  [{i}] {summarize_job(p)}{mark}")
        if last and not any(p.resolve() == last.resolve() for p in files):
            print(f"  上次(不在列表中): {last}")
    if show_presets:
        print()
        print_preset_catalog()
    return files


def run_list_jobs() -> int:
    print_job_list(show_presets=True)
    return 0


def _list_index_for(path: Path, jobs_dir: Path) -> int | None:
    files = list_job_files(jobs_dir)
    try:
        rp = path.resolve()
        for i, p in enumerate(files, 1):
            if p.resolve() == rp:
                return i
    except OSError:
        pass
    return None


def run_job_wizard(
    *,
    name: str | None = None,
    jobs_dir: Path | None = None,
    non_interactive_fields: dict | None = None,
) -> Path | None:
    """Guided create of an annotated job.yaml. Returns path or None if cancelled."""
    root = jobs_dir or default_jobs_dir()
    root.mkdir(parents=True, exist_ok=True)

    print("# 新建任务配置（可复用样式）")
    print(f"保存位置: {root}")
    print("直接回车 = 默认；Ctrl+C 取消")
    print("说明: 只保存用途/布局/编码；视频与 HTML 默认每次运行再问。\n")

    if non_interactive_fields:
        fields = dict(non_interactive_fields)
        job_name = _safe_stem(name or Path(str(fields.get("video", "job"))).stem)
        path = root / f"{job_name}.yaml"
        # Default workdir for non-interactive too when missing
        if "workdir" not in fields:
            fields["workdir"] = str((root / job_name).resolve())
        pin = bool(fields.pop("_pin_paths", False))
        write_job_file(path, fields, title=job_name, overwrite=path.exists(), pin_paths=pin)
        save_last_job(path, root)
        print(f"[OK] 已写入: {path}")
        return path

    try:
        default_name = _safe_stem(name or "my_style")
        job_name = _safe_stem(
            _prompt("1) 配置名称（英文/数字更好，如 mobile_preview）", default_name)
        )
        if job_name in {"3", "1", "2", "0"} or (job_name.isdigit() and len(job_name) <= 2):
            print("  提示: 纯数字名称容易和菜单编号搞混，建议改成有含义的名字。")
            alt = _prompt("  改用名称（回车保持原样）", job_name)
            job_name = _safe_stem(alt or job_name)

        print("2) 用途:")
        print("   [1] 预览原文（推荐首次，不调用 API）")
        print("   [2] 翻译出片（API 可用则自动译；不通时可选手翻表）")
        print("   [3] 复用已有翻译再渲染（运行时再选翻译 JSON）")
        purpose = _prompt("   选择", "1")

        # Dynamic menus from profiles/ (layout_*.yaml / render_*.yaml)
        layout_entries = discover_presets("layout")
        render_entries = discover_presets("render")

        print("3) 布局预设（弹幕位置/样式）:")
        if layout_entries:
            for line in format_preset_menu_lines(layout_entries, none_option=True):
                print(line)
            # Prefer "default" as default selection when present
            layout_default_idx = 1
            for i, e in enumerate(layout_entries, 1):
                if e.get("short") == "default":
                    layout_default_idx = i
                    break
            layout_choice = _prompt("   选择编号或短名", str(layout_default_idx))
            layout = pick_preset_from_menu(
                layout_entries, layout_choice, default_index=layout_default_idx
            )
        else:
            print("   （未找到 profiles/layout_*.yaml，跳过）")
            layout = None

        print("4) 编码预设（速度/画质；预览建议 fast）:")
        if render_entries:
            for line in format_preset_menu_lines(render_entries, none_option=True):
                print(line)
            # Preview purpose → prefer fast; else default
            render_default_idx = 1
            prefer = "fast" if purpose == "1" else "default"
            for i, e in enumerate(render_entries, 1):
                if e.get("short") == prefer:
                    render_default_idx = i
                    break
            render_choice = _prompt("   选择编号或短名", str(render_default_idx))
            render_preset = pick_preset_from_menu(
                render_entries, render_choice, default_index=render_default_idx
            )
        else:
            print("   （未找到 profiles/render_*.yaml，跳过）")
            render_preset = None

        # workdir: auto under jobs/<name>/, silent default write for resume/isolation
        auto_workdir = str((root / job_name).resolve())
        workdir = auto_workdir
        print(f"5) 工作目录（自动）: {auto_workdir}")

        # offset / pin paths: advanced only, off by default
        offset_s = ""
        pin = False
        video = chat = output = tj_pinned = None
        adv = _prompt("6) 高级选项（固定路径/手动 offset）？(y/N)", "n").lower()
        if adv in ("y", "yes", "1"):
            adv_off = _prompt("   写死 offset 到配置？(y/N)", "n").lower()
            if adv_off in ("y", "yes", "1"):
                offset_s = _prompt("   offset 秒数", "")
            pin = _prompt("   把视频/HTML 路径写死进配置？(y/N)", "n").lower() in (
                "y",
                "yes",
                "1",
            )
            if pin:
                video = _prompt_path("   源视频路径", must_exist=True)
                video_p = Path(video)
                chat = _prompt_path("   聊天 HTML", _guess_chat_html(video_p), must_exist=True)
                default_out = str(video_p.with_name(video_p.stem + "_chat.mp4"))
                output = _prompt("   输出路径", default_out)
        else:
            print("   （已跳过：路径每次运行询问，offset 自动检测）")

        fields: dict = {}
        if workdir:
            fields["workdir"] = workdir
        if offset_s.strip():
            try:
                fields["offset"] = float(offset_s.strip())
            except ValueError:
                print(f"  忽略非法 offset: {offset_s}")

        if purpose == "2":
            fields["mode"] = "full"
            fields["render_original"] = False
        elif purpose == "3":
            fields["mode"] = "render"
            fields["render_original"] = False
            fields["reuse_translation"] = True
            if pin and video:
                tj = _prompt_translation_json(Path(video))
                if tj is None:
                    print("已取消")
                    return None
                if tj == "":
                    fields["mode"] = "full"
                    fields["render_original"] = False
                    fields.pop("reuse_translation", None)
                elif tj == "__preview__":
                    fields["mode"] = "preview"
                    fields["render_original"] = True
                    fields["preview_clip"] = 10
                    fields.pop("reuse_translation", None)
                else:
                    tj_pinned = tj
                    fields["translation_json"] = tj
        else:
            fields["mode"] = "preview"
            fields["render_original"] = True
            fields["preview_clip"] = 10

        if layout:
            fields["layout_preset"] = layout
        if render_preset:
            fields["render_preset"] = render_preset
        if pin:
            if video:
                fields["video"] = video
            if chat:
                fields["chat_html"] = chat
            if output:
                fields["output"] = output
            if tj_pinned:
                fields["translation_json"] = tj_pinned

        path = root / f"{job_name}.yaml"
        if path.exists():
            print(f"\n文件已存在: {path}")
            act = _prompt("  [o]覆盖  [r]改名加后缀  [c]取消", "r").lower()
            if act in ("c", "cancel", "n"):
                print("已取消")
                return None
            if act in ("r", "rename", ""):
                n = 2
                while True:
                    alt = root / f"{job_name}_{n}.yaml"
                    if not alt.exists():
                        path = alt
                        job_name = alt.stem
                        if workdir and fields.get("workdir") == auto_workdir:
                            fields["workdir"] = str((root / job_name).resolve())
                        break
                    n += 1

        print("\n将写入（路径类默认仅注释，除非你选择了写死）:")
        for k, v in fields.items():
            print(f"  {k}: {v}")
        print(f"  pin_paths={pin}")
        print(f"  -> {path}")
        ok = _prompt("确认保存？(Y/n)", "y").lower()
        if ok in ("n", "no"):
            print("已取消")
            return None

        write_job_file(
            path,
            fields,
            title=job_name,
            overwrite=path.exists(),
            pin_paths=pin,
        )
        save_last_job(path, root)
        idx = _list_index_for(path, root)
        print(f"\n[OK] 已创建可复用配置: {path}")
        if not pin:
            print("     video/chat_html 未写死：每次运行会询问本次文件（不写回配置）。")
        if idx is not None:
            print(f"     已加入列表第 [{idx}] 项（{current_cli_invocation()} --list-jobs 可见）")
        print(f'     一键复用: {current_cli_invocation()} --job "{path}"')

        print("\n保存后:")
        print("  [1] 立刻运行（将询问本次视频/HTML，除非已写死）")
        print("  [2] 只保存，稍后用上方命令运行")
        # Piped/non-TTY: default to save-only so "run.bat new" does not EOF-crash into run.
        after_default = "1" if _stdin_is_interactive() else "2"
        after = _prompt("选择", after_default)
        if after == "1":
            try:
                _confirm_and_run_job(path)
            except (EOFError, FileNotFoundError, KeyboardInterrupt) as e:
                print(f"\n[FAIL] 无法立刻运行: {e}")
                print(f"  配置已保存: {path}")
                print(f'  稍后: {current_cli_invocation()} --job "{path}"')
        else:
            if idx is not None:
                print(f'\n已保存。任务编号 [{idx}]；稍后运行: {current_cli_invocation()} --job "{path}"')
            else:
                print(f'\n已保存。稍后运行: {current_cli_invocation()} --job "{path}"')
        return path
    except KeyboardInterrupt:
        print("\n已取消")
        return None


def _prompt_multi_segments() -> list[tuple[str, str]]:
    """Interactive loop: collect begin/end pairs until empty line."""
    from twitch_download import TwitchDownloadError, parse_segment_line

    print("将按顺序下载并拼接多段（同一 VOD）。Clip 不支持多段。")
    print("每行输入: 起点 终点（空格或逗号分隔）")
    print("支持格式: 0:01:40 0:05:00   或   100s 300s   或   1m40s 5m0s")
    print("输入空行结束。")
    pairs: list[tuple[str, str]] = []
    while True:
        try:
            line = _prompt(f"第 {len(pairs) + 1} 段 begin end", "")
        except EOFError:
            break
        if not (line or "").strip():
            break
        try:
            seg = parse_segment_line(line)
        except TwitchDownloadError as e:
            print(f"  [FAIL] {e}")
            continue
        if seg is None:
            break
        pairs.append((seg.begin, seg.end))
        approx = max(0.0, seg.end_s - seg.begin_s)
        print(f"  + 已加: {seg.begin} → {seg.end}  (约 {approx:.0f}s，以实际下载为准)")
    if not pairs:
        raise TwitchDownloadError("未输入任何裁切段")
    print(f"\n将下载并拼接 {len(pairs)} 段:")
    for i, (b, e) in enumerate(pairs, start=1):
        print(f"  [{i}] {b} → {e}")
    confirm = _prompt("确认开始下载？(Y/n)", "Y").strip().lower()
    if confirm in ("n", "no", "q"):
        raise TwitchDownloadError("已取消多段下载")
    return pairs


def _menu_download_and_continue() -> int:
    """一级入口: TwitchDownloaderCLI 下载后进入下一步（路径预填）。"""
    try:
        from twitch_download import (
            TwitchDownloadError,
            download_assets,
            download_assets_multi,
            find_twitchdownloader_cli,
            tools_td_bin_dirs,
        )
    except ImportError as e:
        print(f"[FAIL] 无法加载下载模块: {e}")
        return 1

    if find_twitchdownloader_cli() is None:
        print("[FAIL] 未找到 TwitchDownloaderCLI（可选增强）。")
        print(f"  安装引导: {current_cli_invocation()} --offer-td-cli")
        print(f"  或放到可信工具目录: {tools_td_bin_dirs()[0]}")
        return 1

    print("\n======== 下载素材并继续 ========")
    print("  使用 TwitchDownloaderCLI 下载 VOD/Clip + 带嵌入表情的聊天 HTML")
    try:
        url = _prompt("VOD/Clip URL 或 ID", None)
        kind = _prompt("类型 auto/vod/clip", "auto").strip().lower() or "auto"
        quality = _prompt("画质（空=默认 1080p60）", "1080p60").strip() or "1080p60"
        print("裁切模式:")
        print("  [1] 单段（与以前相同，可选 begin/end）")
        print("  [2] 多段裁切拼接（同一 VOD 多段下载后自动拼接视频并合并聊天）")
        crop_mode = _prompt("请选择", "1").strip() or "1"
        begin = end = None
        multi_segments: list[tuple[str, str]] | None = None
        cut_ranges: list[tuple[float, float]] | None = None
        output_fps: float | None = None
        if crop_mode in ("2", "multi", "m"):
            multi_segments = _prompt_multi_segments()
            # Optional: cut ranges from merged video
            cut_input = _prompt("切除合并后某时间段？（格式 21:01-22:59，可逗号分隔多段，空=跳过）", "").strip() or None
            if cut_input:
                from twitch_download import parse_segment_line
                cut_ranges = []
                for part in re.split(r'[,;，；]', cut_input):
                    part = part.strip()
                    if not part:
                        continue
                    seg = parse_segment_line(part.replace('-', ' ', 1)) if '-' in part else None
                    if seg is None:
                        print(f"  警告: 无法解析切段 '{part}'，已跳过")
                    else:
                        cut_ranges.append((seg.begin_s, seg.end_s))
                if not cut_ranges:
                    cut_ranges = None
            fps_input = _prompt("合并视频帧率（空=保持源帧率，60=CFR 60fps）", "").strip() or None
            if fps_input:
                try:
                    output_fps = float(fps_input)
                except ValueError:
                    print(f"  警告: 无法解析帧率 '{fps_input}'，已跳过")
        else:
            begin = _prompt("裁切起点 begin（仅 VOD，可空）", "").strip() or None
            end = _prompt("裁切终点 end（仅 VOD，可空）", "").strip() or None
        trim_mode = _prompt("VOD 裁切模式 Safe/Exact（默认 Safe；推荐）", "Safe").strip().capitalize() or "Safe"
        if trim_mode not in ("Safe", "Exact"):
            print("  警告: 未识别，使用 Safe")
            trim_mode = "Safe"
        media_check = _prompt("媒体健康检查 off/fast/decode（默认 fast）", "fast").strip().lower() or "fast"
        if media_check not in ("off", "fast", "decode"):
            print("  警告: 未识别，使用 fast")
            media_check = "fast"
        media_repair = _prompt("健康失败时自动修复音频时间轴？audio/off（默认 audio）", "audio").strip().lower() or "audio"
        if media_repair not in ("off", "audio"):
            print("  警告: 未识别，使用 audio")
            media_repair = "audio"
        ddir = _prompt("下载目录（空=默认 downloads/…）", "").strip() or None
        # Optional: sub-only VODs. Empty = skip (do not log token).
        print("  OAuth：仅订阅限定 VOD 需要；公开内容直接回车跳过。")
        oauth = _prompt("Twitch OAuth（可空=跳过）", "").strip() or None
    except TwitchDownloadError as e:
        print(f"[FAIL] {e}")
        return 2
    except EOFError:
        print("已取消")
        return 0

    try:
        out = Path(ddir).expanduser() if ddir else None
        if multi_segments is not None:
            result = download_assets_multi(
                url,
                multi_segments,
                out_dir=out,
                kind=kind,
                quality=quality,
                oauth=oauth,
                remove_ranges=cut_ranges,
                output_fps=output_fps,
                trim_mode=trim_mode,
                media_check=media_check,
                media_repair=media_repair,
            )
        else:
            result = download_assets(
                url,
                out_dir=out,
                kind=kind,
                quality=quality,
                begin=begin,
                end=end,
                oauth=oauth,
                trim_mode=trim_mode,
                media_check=media_check,
                media_repair=media_repair,
            )
    except TwitchDownloadError as e:
        print(f"[FAIL] {e}")
        return 2
    except Exception as e:
        print(f"[FAIL] 下载异常: {e}")
        return 1

    video = str(result.video_path)
    chat = str(result.chat_html_path)
    print("\n请选择下一步（路径已预填）:")
    print("  [1] 预览短片（原文 10s）")
    print("  [2] 导出人工翻译表")
    print("  [3] 翻译出片")
    print("  [4] 选用已有 job 样式再跑")
    print("  [0] 结束")
    try:
        choice = _prompt("请选择", "1")
    except EOFError:
        return 0
    if choice in ("0", "q"):
        return 0
    if choice == "2":
        return _run_pipeline(video, chat, "--manual-translation", "--yes")
    if choice == "3":
        return _run_pipeline(video, chat, "--mode", "full", "--yes")
    if choice == "4":
        files = print_job_list(root=default_jobs_dir())
        if not files:
            return 0
        sel = _prompt("输入编号或配置名", "1")
        path: Path | None = None
        try:
            idx = int(sel)
            if 1 <= idx <= len(files):
                path = files[idx - 1]
        except ValueError:
            try:
                path = resolve_job_arg(sel, default_jobs_dir())
            except ValueError as e:
                print(e)
                return 1
        if path is None:
            print("无效选择")
            return 1
        # Inject media as CLI overrides so session uses downloaded paths
        return _confirm_and_run_job(path, extra_cli=[video, chat])
    return _run_pipeline(
        video,
        chat,
        "--mode",
        "preview",
        "--render-original",
        "--preview-clip",
        "10",
        "--yes",
    )


def run_menu() -> int:
    """Full Chinese interactive launcher for run.bat / run.sh."""
    root = default_jobs_dir()
    while True:
        print()
        print("======== 弹幕压制 / 一键运行 ========")
        print(f"配置目录: {root}")
        print("新手: 先 [1] 建样式 → 再 [2] 选样式并填本次视频/HTML")
        print("      或 [3] 从 Twitch 链接下载素材再继续")
        print()
        print("  [1] 新建配置（用途 + 布局/编码预设）")
        print("  [2] 使用已有任务配置（会询问本次视频/HTML）")
        print("  [3] 下载素材并继续（Twitch VOD/Clip，可选）")
        print("  [4] 列出任务配置 + 可用预设")
        print("  [5] 复用上次任务配置")
        print("  [6] 环境检查")
        print("  [0] 退出")
        print()
        # Default to last job if any, else new config
        default_choice = "1"
        if last_job_path(root) is not None:
            default_choice = "5"
        choice = _prompt("请选择", default_choice)

        if choice == "0":
            print("再见。")
            return 0

        if choice == "1":
            path = run_job_wizard(jobs_dir=root)
            if path is not None:
                idx = _list_index_for(path, root)
                if idx is not None:
                    print(f"\n提示: 配置在列表第 [{idx}] 项，名称「{path.stem}」。")
            continue

        if choice == "3":
            try:
                rc = _menu_download_and_continue()
            except (EOFError, KeyboardInterrupt):
                print("\n已取消")
                rc = 0
            if rc != 0:
                print(f"(下载/后续步骤退出码 {rc})")
            _prompt("回车返回菜单", "")
            continue

        if choice == "4":
            # jobs/ 任务 + profiles/ 预设，避免用户以为「没有预设」
            print_job_list(root, show_presets=True)
            _prompt("回车返回菜单", "")
            continue

        if choice == "6":
            rc = _run_pipeline("--doctor")
            _prompt("回车返回菜单", "")
            if rc != 0:
                print(f"(doctor 退出码 {rc})")
            continue

        if choice == "5":
            last = last_job_path(root)
            if not last:
                print("没有上次配置。请先选 [1] 新建，或 [2] 运行一次。")
                _prompt("回车返回菜单", "")
                continue
            try:
                _confirm_and_run_job(last)
            except (EOFError, FileNotFoundError, KeyboardInterrupt) as e:
                print(f"[FAIL] {e}")
            _prompt("回车返回菜单", "")
            continue

        if choice == "2":
            files = print_job_list(root)
            if not files:
                _prompt("回车返回菜单", "")
                continue
            sel = _prompt("输入编号或配置名", "1")
            path: Path | None = None
            try:
                idx = int(sel)
                if 1 <= idx <= len(files):
                    path = files[idx - 1]
                else:
                    print("无效编号")
            except ValueError:
                try:
                    path = resolve_job_arg(sel, root)
                except ValueError as e:
                    print(e)
            if path is None:
                _prompt("回车返回菜单", "")
                continue
            try:
                _confirm_and_run_job(path)
            except (EOFError, FileNotFoundError, KeyboardInterrupt) as e:
                print(f"[FAIL] {e}")
            _prompt("回车返回菜单", "")
            continue

        print("无效选择，请输入 0–6。")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("用法: job_wizard.py [menu|new|list|run <名称>|resolve <名称>]")
        print("  menu          中文交互菜单（run.bat 默认入口）")
        print("  new           引导新建配置")
        print("  list          列出 jobs/ + 预设")
        print("  run <名称>    运行任务配置（会询问本次视频/HTML）")
        print("  run <名称> <额外参数>  运行并转发额外 CLI 参数给 pipeline")
        print("  resolve <名称> 只解析路径")
        return 0
    cmd = argv[0]
    if cmd in ("new", "init", "init-job"):
        name = argv[1] if len(argv) > 1 else None
        path = run_job_wizard(name=name)
        return 0 if path else 1
    if cmd == "quick":
        return run_quick_start()
    if cmd == "drop":
        return run_drag_drop(argv[1:])
    if cmd == "list":
        return run_list_jobs()
    if cmd == "menu":
        return run_menu()
    if cmd == "run" and len(argv) > 1:
        try:
            path = resolve_job_arg(argv[1])
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        extra_cli = argv[2:] if len(argv) > 2 else None
        return int(_confirm_and_run_job(path, extra_cli=extra_cli))
    if cmd == "resolve" and len(argv) > 1:
        try:
            print(resolve_job_arg(argv[1]))
            return 0
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
    print(f"未知命令: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        from common_utils import ensure_utf8_stdio

        ensure_utf8_stdio()
    except Exception:
        try:
            sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)  # type: ignore[attr-defined]
            sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)  # type: ignore[attr-defined]
        except Exception:
            pass
    # Windows console: best-effort UTF-8 so Chinese menu renders
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    raise SystemExit(main())
