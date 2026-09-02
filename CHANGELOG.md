# Changelog

All notable changes to this project are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/); versions follow SemVer.

## [0.3.0] — 2026-09-02

**Repairs the 2026-08-26 OpenEvidence site revamp, which broke every part of the path.**
If you are on 0.2.x nothing works: submissions are throttled into a permanent lockout, and
any answer that does get through arrives with a JSON blob on top and no citation list.
Upgrade.

Five separate faults, found by watching what the *website itself* sends and diffing it
against what this server sends.

### Fixed
- **Submissions locked out permanently (`429 throttled`).** Two changes on OE's side
  compounded: the throttle became a long **minimum gap between submissions** (≈10 min, not a
  per-hour bucket), and a *failed* request appears to re-arm that gap rather than being free.
  The old limiter was a 5-per-60s sliding window with 3-way concurrency, and on a 429 it slept
  for the number OE puts in `Expected available in N seconds` — which is **always 0** — and
  retried. So one parallel batch turned into a tight retry loop that kept the account locked
  for days. Now: submissions are serialised (`RL_MAX_PARALLEL = 1`), paced at
  `OE_SUBMIT_SPACING_SEC` (default 700s), and a 429 returns immediately with the time the next
  attempt is allowed instead of retrying. Every *attempt* is recorded, not just the successful
  ones, since a throttled POST extends the gap too.
- **Pacing survives a server restart.** The gap is enforced across processes via
  `OE_DATA_DIR/submit_state.json`, so a fresh MCP server cannot burst straight through the
  limit the previous one was respecting.
- **Payload drift.** `variant_configuration_file` moved `minimal` → `prod`, and
  `component_config_version` was removed. Both realigned, for new questions and follow-ups.
- **A raw `{"steps": [...]}` JSON blob at the top of every answer.** OE renamed the
  generation-step component `Thinking` → `InlineGenerationStep`; only the old name was being
  skipped. Both are now.
- **The citation list vanished entirely.** `structured_article.citations` was removed. The
  bibliography is now derived from the per-span citation objects; the old field is kept as the
  path for articles created before the revamp.
- **Answers containing a table or figure lost every PMID and corpus `origin`.** The revamp
  renders table/figure components into `raw_text` as `<mdtable>` / `<visual>` regions whose
  `[N]` markers do not correspond one-for-one to the component span's citation objects — a
  live 2026-09-02 article had 7 table markers against 3 objects. That broke the 1:1 assumption
  behind marker→citation matching and pushed the *whole* answer into the degraded branch,
  costing `/oe-verify` exactly the provenance it runs on. Prose is now matched against prose
  (component regions excluded from the marker count, component spans from the objects), and an
  index appearing *only* inside a component is labelled metadata-unavailable rather than
  guessed at.
- **Follow-up chrome leaking into the answer body.** `<follow-up-question>` /
  `<follow-up-topic>` are new wrapper tags, so the old "Would you like to explore" sentence
  match no longer fired. Both forms are stripped; `<visual>` and `<mdtable>` wrappers are
  removed while their content is kept.
- **Citation numbering.** Rendered lists now print each citation's real `index` rather than
  re-counting from 1, so the `[N]` markers in the body point at the right entries.

### Added
- `OE_SUBMIT_SPACING_SEC` environment variable (default `700`) to tune the submission gap
  against measurements of your own account.

### Changed
- README (both languages): rewrote the quota guidance for the gap-based throttle, documented
  the new environment variable, and added a **"When OpenEvidence changes its site"** section
  tabulating all five faults — the same shapes will recur on the next revamp.
- README (both languages): added a security note pointing to
  [kimi-webbridge-lockdown](https://github.com/drpwchen/kimi-webbridge-lockdown) — since
  WebBridge drives your real logged-in browser, readers who also run an AI agent on it can fence
  their bank / brokerage / mailbox out of the bridge's reach.

### Verification
Six consecutive queries (midfoot fracture topic) all succeeded first try, with citations
carrying PMID / DOI / corpus provenance.

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
