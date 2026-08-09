# Changelog

All notable changes to this project are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/); versions follow SemVer.

## Unreleased

### Docs
- README (both languages): added a security note pointing to
  [kimi-webbridge-lockdown](https://github.com/drpwchen/kimi-webbridge-lockdown) — since
  WebBridge drives your real logged-in browser, readers who also run an AI agent on it can fence
  their bank / brokerage / mailbox out of the bridge's reach. No code change; no tag bump.

## [0.2.1] — 2026-08-08

Cross-platform fixes. Thanks to [@drivysu](https://github.com/drivysu) (PR #1) for
reporting and fixing the first two — they were found while setting the server up on macOS.

### Fixed
- **`mcp` pinned to `<2` — this was breaking every fresh install, on all platforms.**
  mcp 2.0 (released 2026-07-28) removed `mcp.server.fastmcp`, so an unpinned
  `pip install -r requirements.txt` resolved to 2.x and `server.py` died at import with
  `ModuleNotFoundError`. MCP clients never show that traceback; they report only
  `Failed to connect — Connection closed`, which reads like a bridge or path problem.
- **Browser auto-detection off Windows.** `_BROWSER_CANDIDATES` held Windows paths only, so
  `BROWSER_EXE` was always `None` elsewhere and auto-launch could never fire. Candidates are
  now per-platform: macOS `.app` bundles (system and `~/Applications`), and on Linux a
  `PATH` lookup (`microsoft-edge` / `google-chrome` / `chromium` / `chromium-browser`) plus
  `/snap/bin/chromium`, rather than hardcoded `/usr/bin` paths that apt, snap and flatpak
  each place differently.
- **`AttributeError` when launching anything off Windows.** Both `subprocess.Popen` calls
  passed `creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`; `subprocess` imports
  those from `_winapi` and they simply do not exist on POSIX, so naming them raised
  `AttributeError: module 'subprocess' has no attribute 'DETACHED_PROCESS'`. Detach kwargs
  are now platform-selected (`start_new_session=True` on POSIX). Without this, fixing browser
  detection alone would have *replaced* a clear "open the browser or set `OE_BROWSER_EXE`"
  error with an opaque crash.
- **Misleading daemon error off Windows.** `OE_BRIDGE_BIN` defaulted to a `.exe` path on every
  platform, so macOS/Linux users hit `browser-bridge not installed at ~/.kimi-webbridge/bin/
  kimi-webbridge.exe`. There is no documented standalone daemon binary there (the Kimi desktop
  app *is* the bridge), so no path is guessed: the default is now unset off Windows and the
  error says to start the bridge yourself or point `OE_BRIDGE_BIN` at a binary taking `start`.

### Changed
- README (en + zh-TW): link the official
  [Kimi WebBridge](https://www.kimi.com/features/webbridge) and warn that same-named
  third-party projects exist, some exposing an unauthenticated port 10086; document the
  `mcp<2` pin and the platform support matrix.

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
