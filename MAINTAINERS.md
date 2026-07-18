# Maintainer notes (handoff)

Short map for the next person. Public product docs: [README.md](README.md) / [README.en.md](README.en.md). Agent-oriented detail may also live in local `CLAUDE.md` (gitignored).

## What this repo is

CLI that burns **TwitchDownloader chat HTML** onto a VOD as a semi-transparent overlay, optional OpenAI-compatible translation.

- **In**: local `video` + TD **HTML** (emotes only if CSS-embedded base64)  
- **Out**: `<video>_chat.mp4`  
- **Optional acquire**: TwitchDownloaderCLI via `--download` / wizard menu (not required for burn)

## Architecture (flat `scripts/`)

```
render_cn_chat.py          # pipeline CLI + doctor + download entry
  twitch_download.py       # optional TD CLI wrapper + multi-segment merge/cut
    twitch_download_transaction.py # serialized process-crash recovery for video/chat pairs
    twitch_download_types.py       # shared download exception contract
    media_health.py        # stream/timeline validation + automatic audio repair
  translate_chat_openai.py # batch translate + .progress.json resume
  job_wizard.py            # run.bat menu
  env_bootstrap.py         # readiness, FFmpeg offer, consented portable TD install
  twitch_chat_burn.py      # parse schedule render compose
    chat_parser.py / chat_window.py / encode_options.py / render_perf.py
    process_util.py / run_meta.py / overlay_config.py
```

Tests: `PYTHONPATH=scripts`, runner `scripts/run_tests.py` (optional `--lint` / `--max`).

## Correctness contracts (do not break)

1. **Export safety** — Non-empty `translation` rows: pipeline **auto-skips** re-export; burn **raises** without `--force-export`.  
2. **Stream timestamps** — Export schema v2: `time_base=stream`, `stream_timestamp`; import prefers stream identity so `--offset` changes do not mass-skip. Legacy video-relative JSON still works.  
3. **Import before window filter** — Export/import indexes are full-chat list positions; never filter first.  
4. **`--fps` ≠ `--output-fps`**.  
5. **Dense seek** — FFmpeg compose must be `-ss N -i video -i overlay` (ss binds to next `-i`).  
6. **Clean live jobs** — `run_meta` pid + freshness; dead/stale `running` is not live; never auto-wipe resume `.progress.json` by default.  
7. **Progress resume** — Require fingerprints; missing fp ⇒ do not trust progress rows.  
8. **Download chat** — Always TD `chatdownload -E` / `.html`; validate embeds when emote tags present.  
9. **Download pair publication** — Single/multi final video + chat publish through `twitch_download_transaction`; the journal/guard guarantee a consistent pair for cooperating process crashes on one local filesystem, not hostile concurrent writers or power loss.
10. **Dual CLI** — Shared layout/encode/fps flags go through `*_FORWARD_SPECS` + `append_*_args` in `render_cn_chat.py` (see `SHARED_FORWARD_FLAGS` / `BURN_ONLY_FLAGS`). Burn-only path flags: export/import/force-export/job-dir/no-job-dir/out-dir. Pipeline forwards `--strict-import` only on import/render burn cmds via `append_strict_import_arg`.

## High-value next work (not blocking)

| Priority | Item |
|----------|------|
| Med | Split god modules only when touching them (`render_overlay` / `compose_video` / pipeline `main`) — extract pure helpers, keep behavior tests green |
| Low | TTY helper dedupe if interactive paths drift |
| Avoid | Full mypy/format gate, GUI, online emote APIs, TD JSON as primary input without a dedicated parser project |

### Recently repaid (do not reintroduce)
- Unified cleaner: `translation_support.clean_translation_text` (burn re-exports as `clean_imported_translation`)
- Job YAML unknown-key WARN (not silent drop)
- Table-driven dual-CLI forward specs + pipeline `--strict-import`

## Verification cheatsheet

```powershell
python scripts\run_tests.py --lint
python scripts\run_tests.py --unit-only
python scripts\run_tests.py --max          # long; needs FFmpeg for smoke/max
# CLI surface (see .claude/skills/verify):
python scripts\twitch_chat_burn.py video.mp4 chat.html --preview-clip 3 --overlay-codec png --offset 0 --out-dir out --no-job-dir
```

Current baseline is **500+ tests**. Treat the latest CI / `pytest` output as release evidence rather than maintaining an exact count here. The `sdist-smoke` CI job must build, unpack, rebuild a wheel, and test the source distribution; scheduled CI runs `--max`.

## Do not commit

`.env`, private VODs/HTML, large outputs, `tools/ffmpeg/`, `tools/TwitchDownloaderCLI/`, `downloads/`, channel glossaries, gitignored `ROADMAP.md` / `HANDOVER.md` unless policy changes.
