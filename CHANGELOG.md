# Changelog

Notable changes to this project are documented in this file.

## [Unreleased]

### Changed

- Started the next development cycle as `0.2.6.dev0` after the v0.2.5 release.
- Added automatic GitHub issue creation when the scheduled max-suite or security-audit workflows fail, so nightly regressions are noticed instead of silently staying red.
- Pinned all GitHub Actions to exact commit SHAs (with version comments) so a compromised action repository cannot move a tag under the release workflow's `contents: write` permission.
- Split the review-table export/import pipeline and translation lint into `scripts/review_tables.py`, the `--download` flow into `scripts/download_flow.py`, and the `--doctor` environment check into `scripts/doctor_check.py`, shrinking the pipeline orchestrator from ~2900 to ~2100 lines; all moved code is verbatim and public signatures are unchanged.

### Fixed

- Fixed translation failing to start on glossary-heavy profiles: contexts longer than 8000 characters now travel to the translator subprocess via `--context-file` instead of the Windows 32,767-character command-line limit, and the per-batch character cap rises with the context so the translator's own guard accepts it.
- Unified YAML loading errors onto `PipelineError` (which subclasses `SystemExit`, preserving exit codes) so callers can catch missing files, missing PyYAML, and invalid syntax uniformly.
- Fixed catastrophic quadratic backtracking in the chat HTML emote-CSS regex: parsing now locates `content:url(...)` anchors linearly and only looks back a bounded window, so truncated HTML or brace-free multi-hundred-KB files parse in milliseconds instead of hanging for hours; covered by dedicated ReDoS regression tests.
- Fixed audio lagging video (`audio_start > video_start`) producing A/V desync on the default AAC path: compose now re-inserts the source skew via `adelay` after `asetpts`, symmetric to the existing video lead-in freeze.
- Fixed the chat timeline tail being silently cut during lead-in freeze: the compose duration cap now allows `duration + lead_in` so late messages can appear, and the contrary comment was corrected.
- Fixed mid-video `--preview-frame` extraction using output seek (full decode up to the seek point, hitting the 120s timeout): `-ss` now precedes `-i` like the compose path.
- Fixed `--clean-all` being able to delete an active job whose `run_meta.json` had not been written yet: `make_job_dir` now seeds a live `running` meta immediately, and `write_run_meta` uses a unique temp file + atomic replace.
- Fixed explicit hardware encoder selection (`nvenc`/`qsv`/`amf`) failing its trial encode silently and only blowing up hours later at compose: it now fails fast with a suggested fallback, escape hatch via `TWITCH_OVERLAY_ALLOW_ENCODER_RISK=1`.
- Fixed the TUI default offset `0.0` silently disabling auto alignment: the field now starts empty and zero is treated as unset, matching the "leave empty or 0 for auto" hint.
- Fixed TUI API-probe worker and history-lock reads: probe feedback uses `post_message` (no more unbounded `call_from_thread` blocking on shutdown), the poll timer is stopped on unmount, and history read paths degrade gracefully when another instance holds the lock.
- Fixed job YAML syntax errors raising raw tracebacks (`yaml.YAMLError` is now wrapped as a friendly `ValueError`), `_yaml_quote` producing unparseable YAML for newline/`- `-prefixed values, and `write_job_file` now writing atomically.
- Fixed TSV review-table export failing when the parent directory does not exist (now created, matching the XLSX path), and `quick_demo`/`run_tests` now see the portable `tools/ffmpeg` install.
- Fixed manual `--context-file` runs rejecting their own context when `--max-batch-chars` is left default (cap now auto-raises with context length, same formula as the pipeline), stopped persisting glossary plaintext in `.progress.json` (fingerprint instead), warn on corrupt progress files, and clean up per-run `translation_context_*.txt` handoff files.
- Fixed `--doctor` entering the job media prompts when combined with `--job`, doctor treating a missing `openai` package as a hard failure (now WARN, plus an `openpyxl` WARN check), and `_save_api_config` wiping a stored API key when the field is left empty; added a `crf 0..51` bound to TUI validation.
- Fixed packaging metadata drift: removed the deprecated `fix_merge` shim (superseded by `--segment`/`--cut`, and importing it exits by design), added the 11 missing modules to isort `known-first-party`, and pinned `textual>=8.2,<9` in the dev extra to match the pinned dev environment.

### Changed

- Restructured the three oversized modules into facade + owner-module layers, moving code verbatim with module-attribute calls so existing monkeypatch targets keep working: `render_cn_chat.py` (~2170 → 1525 lines; `cli_spec.py` for argparse with a single-source defaults dict that a new parser-vs-dict sync test guards, `build_burn_command` deduplicating three command assemblies, and the YAML/profile/output cluster folded into `review_tables.py`), `twitch_chat_burn.py` (3804 → 1510 lines; `chat_text_layout.py`, `chat_schedule.py`, `translation_io.py`, `media_probe.py` with memoized probes, and `overlay_render.py`/`overlay_compose.py` where render/compose now return `RenderResult`/`ComposeResult` instead of writing through `OverlayConfig`), and `twitch_download.py` (1674 → 1019 lines; `vod_merge.py` and `td_cli_install.py`).
- Batch-era test files renamed to module-based names via `git mv` (e.g. `test_p2_fixes.py` → `test_burn_compose_and_encode.py`, `test_audit_cli_clean.py` → `test_cli_clean_and_contracts.py`); collection count unchanged.
- CI: top-level least-privilege `contents: read`, a PR-path bandit gate, and `pip-audit` now also auditing dev dependencies; `run_tests --coverage` enforces a per-module 60% floor for scripts with ≥500 statements.
- In-process test coverage for the job_wizard interactive menu (75 cases, 27% → 63% module coverage) and the doctor() diagnosis body (56% → 97%).

## [0.2.5] - 2026-08-31

### Added

- Added an in-TUI API configuration editor and asynchronous non-blocking connectivity testing with live health/latency feedback.
- Added core time offset (`--offset`) support directly in the TUI with bidirectional YAML persistence and range validation.
- Added new public presets: `layout_right` (right-side UI avoidance), `layout_transparent` (subtle game overlay), `layout_sidebar` (full-height chat column), `layout_top_right`, `render_audio_copy` (lossless audio stream copy), and translation profiles `gaming_slang` and `acg_anime`.
- Added a tested Windows direct-dependency constraint set that `install.bat` uses automatically while keeping cross-platform package requirements compatible.
- Added a shared cut timeline so multi-segment video trimming and chat timestamp remapping use the same normalized ranges.
- Added execution-scoped pipeline results and an immutable overlay-scene plan for safer task lifecycle handling and render planning.
- Added overlay content-level smoke tests that assert rendered frames actually contain chat pixels, plus end-to-end translation pipeline tests covering concurrent workers with partial failures and a stub HTTP API subprocess.

### Changed

- Started the next development cycle as `0.2.5.dev0` after the v0.2.4 release.
- Overhauled all TUI interface text across all 6 tabs for significantly improved readability while preserving 100% of technical boundary explanations and safety warnings.
- Reorganized task modes into three clear paths: Quick Preview, Full Production, and Step-by-Step Manual Review, with seamless resumption guidance and full backward compatibility.
- Optimized auto hardware encoder detection priority to discrete GPUs first (`NVENC -> AMF -> QSV -> x264 fallback`).
- Upgraded layout presets, render presets, and video encoders in TUI from free-text inputs to structured `Select` dropdowns with built-in options.
- Made VOD crop segments optional in the download tab, allowing full VOD downloads without mandatory cropping.
- Centralized pipeline command projection and shared burn-flag forwarding so TUI and CLI adapters use one canonical parameter contract.
- Consolidated the duplicate pytest configuration into `pyproject.toml` as the single source of truth and enabled `--strict-markers` so undeclared test markers fail collection.
- Documented in the README (Chinese and English) and in the TUI download tab that the TwitchDownloaderCLI only accepts OAuth as a command-line argument, so the token is briefly visible in the local process list during downloads.

### Fixed

- Prevented local history clearing while queued or running tasks still exist, including tasks owned by another TUI process.
- Fixed `scripts/run_tests.py --help` crashing when formatting the coverage percentage.
- Kept the scheduled CI lint gate green under ruff 0.16 by removing newly flagged unused imports, fixing import order, renaming ambiguous loop variables, and pinning ruff to `0.16.5` in dev requirements.
- Prevented the TUI poll timer from crashing the app with a widget lookup error when a task finishes during shutdown or before a lazily mounted tab is ready; affected refreshes now skip the tick instead of raising.
- Hardened timing-sensitive TUI tests against loaded CI runners by polling lazily mounted widgets and async form validation instead of using fixed pauses, and pinned textual in dev requirements.
- Fixed catastrophic regex backtracking in the emote-only message detectors that could hang translation and lint for hours on a single emote-spam line; the patterns now disambiguate whitespace so matching stays linear.
- Fixed `--mode render` accepting `--review`/`--lint-translation` in a way that silently ran full LLM translation despite the mode's no-API promise; `--mode render --lint-translation <file>` is now an explicit lint-only path that actually checks the given file.
- Made translation JSON rewrites (`normalize_translation`, XLSX/TSV review import) atomic so an interrupted process can no longer corrupt the only resume source; a manual pause now also records a `manual_required` end state instead of a false success.
- Fixed preview windows combining `--preview-frame` with `--preview-clip` (or out-of-range frames) rendering an empty or time-mismatched overlay; the chat filter window now uses the same clamped preview time as the renderer and warns on adjustment.
- Throttled translation progress persistence (fingerprints computed once, time/batch-based saving) so very large VODs no longer spend quadratic CPU and disk rewriting unchanged state every batch.
- Hardened TUI task workers and history locking against shutdown races, blocked indefinite POSIX file-lock waits, drained superseded sessions cleanly, capped the in-memory log buffer, redacted additional URL credential and authorization header shapes, and stopped saving userinfo credentials from pasted URLs into history snapshots.
- Preserved pre-video-start chat messages under large `--offset` values instead of clamping them to the first frame, counted silently dropped messages under `--min-visible-seconds`, and marked `run_meta` as failed when the renderer crashes outside its stage guards.
- Hardened the pipeline against `--dry-run` side effects (real downloads, review exports, lint reports, last-job writes are now suppressed), bare media paths dropped onto job YAML, and duplicate lint passes when exporting both review table formats.
- Extended CJK detection to kana, hangul, and Extension A ideographs so echo prefixes strip correctly for Japanese/Korean targets, and fixed parser edge cases for `background-color` attributes and HTML comments containing fake messages.

## [0.2.4] - 2026-07-24

### Changed

- Added an opt-in TUI Issue summary that runs the existing environment check and writes a reviewable local report with credentials and common absolute paths removed.
- Added a direct Bug report button next to the TUI Issue summary and percentage progress events for long full-decode media checks.

### Fixed

- Prevented the TUI and pipeline from replacing the source video when the output path points to the same file.
- Preserved imported YAML advanced fields and CLI-only modes in durable TUI history snapshots, so history reruns use the recorded configuration.
- Required OAuth-protected download reruns to request a fresh credential instead of starting anonymously; the credential remains absent from history and diagnostics.
- Extended diagnostic redaction to client secrets, Basic authorization values, legacy translation base URLs, and URL user information.
- Kept the TUI full-decode media check as the default when imported YAML omits that setting.
- Kept translate-only completion messages tied to the translation JSON even when reuse settings are present, instead of claiming that a video was rendered.

## [0.2.3] - 2026-07-19

### Added

- Added GitHub issue forms and a Windows batch-launcher smoke check so support reports and release-entry regressions are easier to catch.

### Fixed

- Removed OAuth query parameters and fragments from locally stored TUI download history, including safely rewriting compatible older history records.
- Serialized concurrent TUI history updates so separately launched windows cannot silently discard one another's completed tasks.
- Preserved incomplete trailing task-event records until their JSONL line is complete, preventing progress events from being lost during polling.
- Prevented the TUI from reporting a task as successful when its expected result manifest or downloaded video/chat artifacts are missing.
- Made Twitch HLS crop-boundary expansion visible in the TUI, so a short requested VOD window that downloads longer cannot silently consume extra translation time.
- Ignored local package-build and release-verification directories so generated artifacts do not accidentally enter commits.

## [0.2.2] - 2026-07-18

### Changed

- Reorganized the double-click launcher into a beginner-focused main menu, a continue-work path, and an explained tools menu while retaining the legacy full menu for advanced users.
- Made the Textual task UI the default double-click entry. Existing command arguments, drag-and-drop behavior, and the recovery menu are preserved through `run_cli.bat`.
- Explicit pipeline flags and video/chat invocations with extra options now bypass the drag-and-drop preview route, preserving their requested CLI behavior.

### Added

- Added a form-based TUI for local preview, translated preview, full render, reuse-render, YAML import/export, advanced settings, diagnostics, cancellation, and result-folder access.
- Added bounded task-output capture and versioned JSONL task events for responsive progress reporting and safe diagnostic export.
- Added opt-in atomic pipeline result manifests and a bounded local TUI history with lifecycle recovery, exact artifact paths, rerun support, and diagnostic references.
- Added a TUI download page backed by the existing TwitchDownloaderCLI flow, with bounded VOD segments, artifact manifests, automatic new-task fill, and rerunnable local download history.
- Added an ephemeral masked OAuth field for subscription-restricted TUI downloads; command logs, diagnostics, manifests, YAML, and history redact or omit it.
- Added a `manual_required` task outcome so translation fallback is shown as pending human work instead of a successful render; failed-task diagnostics now persist without retaining transient event files.

## [0.2.1] - 2026-07-18

### Added

- Added `run.bat quick` to scaffold first-run files and continue into the guided job wizard.
- Added `run.bat demo`, an offline six-second overlay demo that verifies FFmpeg, fonts, and rendering without a translation API.
- Added drag-and-drop routing for a local video plus Twitch chat HTML; it creates a safe ten-second original-chat preview before a full translation render.

## [0.2.0] - 2026-07-17

### Added

- Added stricter YAML, numeric, media, and empty-chat validation with actionable errors.
- Added bounded resource handling for embedded emotes, downloaded archives, release metadata, and short media probes.
- Added long-term regression suites for configuration, runtime recovery, download security, packaging, and translation state.
- Added fault-injection coverage for serialized, process-crash recovery of paired video/chat publication.
- Added source-distribution rebuild checks and scheduled full-suite CI coverage.

### Changed

- Translation progress and cache entries now include the complete translation context and use atomic, collision-resistant writes.
- Human-reviewed translations take precedence over compatible saved progress; incompatible progress is safely rebuilt.
- Long chats use more efficient scheduling, visibility tracking, and lazy message-image caching.
- Portable tool installation now stages and validates downloads before atomically replacing an existing installation.
- Single- and multi-segment downloads now publish video and chat as one recoverable pair across cooperating processes.
- Download transaction state is isolated in a dedicated runtime module with a narrow integration surface.
- Wheels now carry the public configuration assets and complete example job; source distributions additionally include launchers and the test contract needed for isolated validation.

### Fixed

- Prevented stale or unrelated download files from being mistaken for newly acquired media.
- Explicit TwitchDownloaderCLI installation now reports success only when the executable is actually available.
- Fixed work-directory translation exports, floating-point preset handling, render statistics, and several media publication failure paths.
- Improved retry classification, translation response validation, process cleanup, interrupted-run recovery, and output rollback behavior.
- Preserved valid Unicode when normalizing damaged chat input, including emoji and supplementary CJK characters.

### Security

- Prevented partial environment overrides from combining trusted process configuration with untrusted local values.
- Restricted executable discovery and release assets to trusted locations and expected sources.
- Rejected unsafe, oversized, encrypted, duplicate, linked, or traversal archive members before extraction.
- Avoided following symlinks and Windows reparse points during cleanup.

[0.2.0]: https://github.com/yigenhuobahh/twitch-chat-translator-overlay/releases/tag/v0.2.0
[0.2.1]: https://github.com/yigenhuobahh/twitch-chat-translator-overlay/releases/tag/v0.2.1
[0.2.2]: https://github.com/yigenhuobahh/twitch-chat-translator-overlay/releases/tag/v0.2.2
[0.2.3]: https://github.com/yigenhuobahh/twitch-chat-translator-overlay/releases/tag/v0.2.3
[0.2.4]: https://github.com/yigenhuobahh/twitch-chat-translator-overlay/releases/tag/v0.2.4
