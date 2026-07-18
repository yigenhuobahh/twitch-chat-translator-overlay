# Changelog

Notable changes to this project are documented in this file.

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
