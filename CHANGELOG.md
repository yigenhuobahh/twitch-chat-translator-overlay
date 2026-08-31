# Changelog

Notable changes to this project are documented in this file.

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
