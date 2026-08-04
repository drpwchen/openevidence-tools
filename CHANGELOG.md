# Changelog

All notable changes to this project are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/); versions follow SemVer.

## [0.2.0] — 2026-08-04

### Added
- **Browser auto-launch**: when the bridge daemon is up but its browser extension is not
  connected (i.e. the browser is closed), the server now launches the browser itself and
  waits for the extension handshake before proceeding. Detection uses the daemon's
  `/status` endpoint (`extension_connected`), the authoritative signal.
- `OE_BROWSER_EXE` environment variable to point at the browser carrying the bridge
  extension; defaults to auto-detecting common Edge/Chrome install paths.
- This `CHANGELOG.md` (versions before it are reconstructed from git history).

### Changed
- README (en + zh-TW): documented the self-healing startup chain
  (daemon auto-start → browser auto-launch → OE tab open/reuse).

## [0.1.1] — 2026-08-02

### Added
- Gitleaks secret-scan CI workflow (`cb455e5`).
- Beginner-series learning path links in README (`ee74605`).
- Proper attribution to htlin222: `NOTICE` with full MIT text, rewritten
  acknowledgment section (`28ab48f`).

## [0.1.0] — 2026-08-02

Initial public release: OpenEvidence MCP server (4 tools: ask / list_recent /
fetch_result / ask_followup) over a browser-bridge transport, plus the `/oe-verify`
skill that cross-checks every citation before you trust the answer (`229eba9`).
