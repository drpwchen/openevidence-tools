# Security policy

This server drives a logged-in browser session for OpenEvidence, so a bug can expose a session or
account data.

## Reporting a vulnerability

Please report it privately through GitHub: the repository's **Security** tab, then **Report a
vulnerability**. Do not open a public issue for it.

Useful in a report: the version, what you did, and what happened. Never attach cookies, tokens,
or the contents of the dedicated browser profile (`~/.openevidence/browser-profile`).

## What counts

- A way for another site or another local process to read or reuse the OpenEvidence session
  beyond what the documented debugging port (127.0.0.1 only) already allows.
- Any path where credentials, cookies or query contents are written to a log or sent somewhere
  they should not go.

## Supported versions

Only the latest release receives fixes.
