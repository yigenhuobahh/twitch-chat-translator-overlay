# Twitch Chat Translator Overlay

Translate Twitch VOD chat messages into Chinese (or other languages) and burn them onto the video as a semi-transparent overlay.

Release notes: [`CHANGELOG.md`](https://github.com/yigenhuobahh/twitch-chat-translator-overlay/blob/main/CHANGELOG.md)

> **Input**: A video file + TwitchDownloader chat HTML export  
> (Optional) Use this tool’s `--download` / menu “Download media” via TwitchDownloaderCLI  
> **Output**: MP4 with translated chat overlay

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure translation API (copy .env.example to .env) — skip for --render-original

# 3. Guided job + preview (recommended)
python scripts/render_cn_chat.py --init
python scripts/render_cn_chat.py video.mp4 chat.html --mode preview --render-original
python scripts/render_cn_chat.py --job jobs/example_job.yaml

# Or one-shot full render
python scripts/render_cn_chat.py video.mp4 chat.html --output out.mp4
```

Windows: `install.bat` then double-click `run.bat` for the guided Textual UI. Linux/macOS: `bash install.sh` then `bash run.sh`.

### Fastest first run (Windows)

```bat
run.bat          :: guided local video/chat workflow in the TUI
run_cli.bat quick :: scaffold .env and create a reusable job
run_cli.bat demo  :: render a 6-second offline demo; no API key required
```

You can also drag a video file and its Twitch chat HTML onto `run_cli.bat`. It creates a 10-second original-chat preview first, so layout and timing can be checked before configuring translation or rendering a full VOD. Existing `run.bat` commands with arguments are forwarded to this recovery/advanced CLI entry point.

The demo writes its source files and final `demo_overlay.mp4` under `outputs/quick_demo/`. Drag-and-drop previews use the normal pipeline output rules, so the final path is printed when rendering finishes.

### Windows TUI and recovery CLI

The default `run.bat` TUI accepts local video and chat HTML, supports original/translated previews, full translation render, reuse-render, YAML import/export, cancellation, result-folder access, and a redacted diagnostic export. Its Download page uses the installed TwitchDownloaderCLI to fetch public VODs and one-or-more bounded VOD segments, then fills the downloaded video and chat HTML into a new task. For subscription-restricted VODs, its masked OAuth field is used only for the current download; it is excluded from command logs, subprocess output, diagnostics, YAML, manifests, and history. If translation cannot proceed, the TUI reports a manual-translation task instead of claiming that a video was rendered. The TUI defaults to a full-decode gate for every downloaded segment, merge result, and later local source video, so corrupt media is caught before translation starts; this adds one sequential read for long videos. Its History and Artifacts page keeps the most recent 100 local task states and exact artifact paths, preserves a live task when another TUI instance opens, and can reload or rerun their saved local configuration without storing credentials, commands, or translated content. Advanced settings contain presets, encoder options, rules, profiles, and manual-review controls. `run_cli.bat` retains the original menu and drag-and-drop. Raw pipeline options and `video chat.html` invocations with extra options are forwarded directly instead of being rewritten as a drag-and-drop preview.

### TUI recovery

Open **History and Artifacts** and select the task first: its paths come from the result manifest, rather than guessed output names. Export a diagnostic for failed tasks, fix the reported input, FFmpeg, font, or translation configuration, then reload the saved configuration and rerun it. Diagnostics omit commands, environment-variable names, and credentials. A **manual translation required** state means that JSON/XLSX/TSV was created but no video was rendered; edit the review sheet and use reuse-render (or CLI `--review-done`). A failed media gate should be fixed by replacing or re-downloading the named segment; disabling the gate is diagnostic-only. If the TUI does not start, run `run_cli.bat doctor`, then `install.bat`; `run_cli.bat` remains the recovery menu and drag-and-drop entry point. Temporary event files are cleaned after terminal tasks, while local history and failed diagnostics can be cleared from the History page.

> **Default output**: Without `--output`, writes `<video_name>_chat.mp4` next to the source.  
> **Translation JSON**: Defaults to `<video_name>_translation.json`; reuse with `--reuse-translation`.  
> **Re-export safety**: If the JSON already has non-empty `translation` fields, export is **skipped** (pipeline) or **refused** (burn) unless `--force-export`.  
> **`--mode`**: `auto`/`full` full pipeline; `preview` (~10s); `translate` / `render` partial. CLI flags override `jobs/*.yaml`.

## What It Does / Doesn’t

Processes TwitchDownloader chat (text + **embedded** emotes) into an on-video chat box. Emote images stay as images; text is translated.

- **No online emote fetch** — only CSS `content:url(data:image…base64…)` embeds  
- **No ASR/subtitles** — chat only  
- **Terminal UI + CLI** — Textual UI for local workflows; CLI remains for recovery and advanced operations
- **No arbitrary HTML** — TwitchDownloader HTML (and a limited legacy web path)

## Workflow

```
  Acquire assets (either):
    A) TwitchDownloader GUI → video + chat.html
    B) Optional: this tool + TwitchDownloaderCLI
       python scripts/render_cn_chat.py --download <vod-or-clip-url>
       or run.bat / run.sh menu → Download media
        │
        ▼
  render_cn_chat.py
        ├─ parse HTML → messages + emotes
        ├─ export translation JSON (stream timestamps, schema v2)
        ├─ OpenAI-compatible batch translate (or manual tables if API down)
        ├─ optional review XLSX / lint
        ├─ Pillow overlay frames
        └─ FFmpeg compose → MP4
```

### Optional download (no TD GUI)

1. Install [TwitchDownloaderCLI](https://github.com/lay295/TwitchDownloader/releases) (optional):  
   - **Auto**: `python scripts/render_cn_chat.py --offer-td-cli` (downloads the platform zip into the trusted tools directory shown by the command after confirm; `--yes` skips prompts)
   - **Install scripts** may ask at the end (default No)  
   - **Manual**: extract CLI zip to the trusted tools directory shown by the command, or set `TWITCHDOWNLOADER_CLI` / PATH

> Security: source checkouts use the repository tools directory; installed commands use per-user app data. Executables are never loaded from the current media directory.

2. Chat download always uses `--embed-images` (`-E`) so emotes work offline.

```bash
python scripts/render_cn_chat.py --download https://www.twitch.tv/videos/123456789 --download-only
# VOD trim (video + chat same window):
python scripts/render_cn_chat.py --download https://www.twitch.tv/videos/123 --begin 0:01:00 --end 0:05:00
# Multiple windows from one VOD are merged onto one continuous timeline:
python scripts/render_cn_chat.py --download https://www.twitch.tv/videos/123 \
  --segment 0:10:00-0:12:30 --segment 0:40:00-0:43:00 --download-only
# Optional post-merge cut and constant output frame rate (multi-segment only):
python scripts/render_cn_chat.py --download https://www.twitch.tv/videos/123 \
  --segment 1:21:13-1:38:06 --segment 1:42:05-2:17:43 \
  --cut 21:01-22:59 --download-output-fps 60 --download-encoder auto --download-only
```

Interactive runs open a next-step menu (preview / manual table / translate). Use `--download-only` or `--yes` for scripts.

VOD trims use `--download-trim-mode Safe` by default; choose `Exact` only when an exact cut point is more important than avoiding TwitchDownloader timestamp drift. Downloaded segments, merged video, and final chat output pass a media-health gate before publication. `--media-check fast` is the default, `decode` adds a full decode, and `off` is intended only for troubleshooting. For failed download or merge gates, `--media-repair audio` (the default) writes a sibling `*.repaired.mp4`, preserves the original, and continues only after the repaired file passes validation; use `--media-repair off` to disable that repair. A failed final chat-output gate stops publication and preserves the partial file for diagnosis rather than attempting automatic repair.

### Local input-video gate

Before translation or rendering, the pipeline also checks the local input video, so a bad segment does not surface only after API work has completed. CLI defaults to `--source-media-check fast` to preserve existing automation speed; `--source-media-check decode` fully decodes the input before the expensive workflow and is recommended for important renders. The TUI uses `decode` by default. `off` is for diagnosis only.

### Manual translation

```bash
python scripts/render_cn_chat.py video.mp4 chat.html --manual-translation \
  --translation-json translations/my_chat.json --review-xlsx reviews/review.xlsx
# Edit XLSX translation column, then:
python scripts/render_cn_chat.py video.mp4 chat.html --reuse-translation --review-done \
  --translation-json translations/my_chat.json --review-xlsx reviews/review.xlsx --output out.mp4
```

## Installation

- **Python 3.10+**, **FFmpeg/ffprobe** on PATH, **CJK font**
- `pip install -r requirements.txt` or `pip install -e ".[dev]"`
- Console scripts: `twitch-chat-overlay` / `twitch-chat-burn` / `twitch-chat-translate`
- Translation env: `OPENAI_COMPAT_BASE_URL`, `OPENAI_COMPAT_API_KEY`, `OPENAI_COMPAT_MODEL` (or legacy `AGNES_*`)

`python scripts/render_cn_chat.py --doctor` checks tools/fonts/API. Missing FFmpeg may prompt to install on TTY; `--offer-fix` / `--fix-yes` for automation. Optional TD CLI: `--offer-td-cli`.

## Updating and history migration

- Git checkouts can use `update.bat` on Windows or `bash update.sh` on Linux/macOS. Updates are fast-forward only and stop before dependency installation if the pull fails.
- GitHub ZIP and sdist copies have no Git history to pull. Download a fresh archive into a new directory instead.
- Repository history was rewritten in **2026-07**. Clones created before that rewrite cannot fast-forward and require a fresh clone.

For an old clone, back up only `.env`, `jobs/*.yaml`, custom `profiles/*.yaml`, and `configs/launcher.local.yaml`; create a fresh clone in a new directory; then restore those local files. Do not combine the old repository history with the fresh clone. The updater deliberately performs no destructive history repair.

## Key Concepts

### Chat FPS vs Output FPS

- `--fps`: overlay sampling (default 15)  
- `--output-fps`: final MP4 (default: follow source via ffprobe)  
Independent — 60fps VOD can keep 15fps chat.

### Time offset & export identity

HTML timestamps are stream-absolute; VODs may start mid-broadcast. Auto offset is heuristic — confirm with `--preview-frame` / `--preview-clip`, or set `--offset`.

Export JSON schema v2 stores `time_base=stream` + `stream_timestamp` so import identity survives a later different `--offset`. Pipeline export forwards `--offset` for metadata. Legacy JSON without stream fields still imports with video-relative timestamps.

### Layout / render presets

- Layout: `profiles/layout_default|compact|mobile.yaml` (or short names)  
- Encode: `profiles/render_default|fast|hq.yaml`  
CLI wins over YAML. Ratio flags: `--x-ratio` / `--y-ratio` / `--width-ratio` / `--height-ratio` / `--font-size-ratio`.

### Jobs & clean

- Reusable `jobs/*.yaml` usually **do not pin** video/chat (session paths).  
- Parallel burns use isolated `job_*` dirs; avoid `--no-job-dir` for concurrent runs.  
- `--clean`: partials only by default; `--clean-all` removes finished **and stale** tool jobs (skips live PIDs); `--clean-progress` deletes `*.progress.json`.

### After API translate (TTY)

Pause: Enter = continue full render; `P` / `P 30` = short preview; `S` = stop for Excel. Use `--yes` in CI.

## Common parameters (pipeline)

| Flag | Description |
|------|-------------|
| `--render-original` | No LLM; burn original chat |
| `--reuse-translation` | Skip export/API; use existing JSON |
| `--force-export` | Allow wiping non-empty translations on export |
| `--strict-import` | On import/render: hard-fail identity mismatch (forwarded to burn) |
| `--manual-translation` | Export JSON + review tables; stop |
| `--preview-clip` / `--preview-dense` / `--preview-frame` | Short clip / densest window / still |
| `--layout-preset` / `--render-preset` | YAML or short name |
| `--download` / `--download-only` / `--quality` / `--begin` / `--end` | Optional TD CLI acquire and single-window trim |
| `--segment` / `--cut` / `--download-output-fps` / `--download-encoder` | Multi-window merge, post-merge removal, and output encoding |
| `--download-trim-mode` / `--media-check` / `--media-repair` | Trim safety, media validation, and repair policy |
| `--source-media-check` | Local input-video preflight; `decode` fully decodes before expensive work |
| `--doctor` / `--offer-fix` / `--offer-td-cli` | Environment / optional tools |
| `--workdir` / `--output` / `--clean` / `--clean-all` | Paths & cleanup |

### Burn-only extras

`twitch_chat_burn`: `--export-translation`, `--import-translation`, `--force-export`, `--out-dir` / `--job-dir` / `--no-job-dir`.  
`--strict-import` is also on the pipeline CLI and is forwarded on import/render burn commands.

## Testing

```bash
pip install -r requirements-dev.txt
python scripts/run_tests.py              # unit (+ smoke if FFmpeg present)
python scripts/run_tests.py --lint       # Ruff (also in CI; included in --max)
python scripts/run_tests.py --max        # full long-term suite
```

## License

MIT — see [LICENSE](LICENSE)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Prefer correctness fixes and tests; do not commit secrets, private VODs/HTML, or large outputs.
