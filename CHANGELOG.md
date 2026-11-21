# Changelog

All notable changes to **claudeshift-cli** will be documented in this file.

## [0.1.0] - 2026-11-21
- **Atomic Profile Vault:** Crash-safe staging & rollback profile switching with SHA-256 integrity verification, auto-sync of active credentials before switching, symlink-escape guards, and Windows long-path (`\\?\`) support.
- **Multi-Provider Support:** Profile management for Claude Code CLI, Claude Desktop App (including Windows Store paths), OpenAI Codex CLI, Gemini CLI, and Cursor.
- **Offline Token & Quota Telemetry:** Incremental JSONL parser with message/heartbeat deduplication, 5-hour rolling session gauge, burn-rate (`tokens/min` & `$/hr`) and ETA calculation, automatic subscription tier detection, and live pricing catalog sync (`claudeshift pricing sync`).
- **Gumroad Pro Licensing:** Online Gumroad License API verification with HMAC-SHA256 machine-bound caching and a 14-day offline grace window.