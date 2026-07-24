# Changelog

Notable changes to this project are documented in this file.

## [Unreleased]

### Changed

- Started the next development cycle as `0.2.5.dev0` after the v0.2.4 release.

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
