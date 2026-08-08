"""OpenEvidence MCP Server — query OpenEvidence for evidence-based clinical answers.

Exposes four MCP tools (ask / list_recent / fetch_result / ask_followup) that run
against OpenEvidence through your own logged-in browser session (see Transport
below). Pair it with the /oe-verify skill in this repo — OpenEvidence answers fast,
but a RAG pipeline can mis-attribute citations, so never absorb an answer unverified.
"""

import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

BASE_URL = "https://www.openevidence.com"

# ------- Transport: browser bridge -------
# OpenEvidence has no public API, and its session cookie is httpOnly (it cannot be
# exported to a plain HTTP client). So this server runs each OE API call as a
# page-context fetch() INSIDE your own logged-in openevidence.com browser tab,
# driven through a local browser-bridge daemon (default: kimi-webbridge, listening
# on 127.0.0.1). Every request therefore goes through YOUR OWN account and session —
# exactly what would happen if you clicked in the tab yourself. Set OE_BRIDGE_URL to
# point at a different daemon endpoint.
#
# Two hard constraints learned by measurement, both encoded below:
#   1. NEVER await a slow promise inside evaluate — the daemon serializes
#      commands, so an in-flight `await fetch(...)` blocks EVERY other bridge
#      consumer until it settles. Hence fire-and-forget: evaluate returns
#      immediately, the fetch lands in a window-scoped job slot, and we poll for it.
#   2. Responses are big (a 29-citation article is ~80 KB) and evaluate returns
#      them as a JSON string, so the body is pulled back in CHUNK-sized slices.
KIMI_URL = os.environ.get("OE_BRIDGE_URL", "http://127.0.0.1:10086/command")
KIMI_SESSION = "openevidence-mcp"
KIMI_BIN = Path(
    os.environ.get(
        "OE_BRIDGE_BIN",
        str(Path.home() / ".kimi-webbridge" / "bin" / "kimi-webbridge.exe"),
    )
)
CHUNK = 40_000          # verified intact end-to-end; keeps each evaluate response small
POLL_INTERVAL_SEC = 2.0

# Browser auto-launch: the bridge daemon is only a relay — the extension living
# inside the browser does the actual work, so a closed browser means every
# command fails even though the daemon answers. Set OE_BROWSER_EXE to the browser
# that has the bridge extension installed; otherwise common paths are tried.
if sys.platform == "darwin":
    _BROWSER_CANDIDATES = [
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
elif sys.platform == "win32":
    _BROWSER_CANDIDATES = [
        "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    ]
else:
    _BROWSER_CANDIDATES = [
        "/usr/bin/microsoft-edge",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    ]
BROWSER_EXE = os.environ.get("OE_BROWSER_EXE") or next(
    (p for p in _BROWSER_CANDIDATES if Path(p).exists()), None
)


def _kimi_cmd(action: str, args: dict, timeout: int = 30) -> dict:
    """One browser-bridge command. Returns the `data` payload."""
    req = urllib.request.Request(
        KIMI_URL,
        data=json.dumps({"action": action, "args": args, "session": KIMI_SESSION}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(f"browser-bridge: {data.get('error', 'unknown error')}")
    return data.get("data", {})


def _kimi_daemon_up() -> bool:
    """Is the browser-bridge daemon listening?

    The distinction that bit us: `HTTPError` is a SUBCLASS of `URLError`, so a
    daemon that answers with *any* HTTP status (even 4xx/5xx) still raises
    `URLError`. Catching `URLError` alone therefore misreads a live-but-erroring
    daemon as "not started" and spawns a redundant one. Only a genuine connection
    failure (URLError that is NOT an HTTPError) means it's actually down.
    """
    try:
        _kimi_cmd("list_tabs", {}, timeout=5)
        return True
    except urllib.error.HTTPError:
        return True   # answered with a status code — it's listening
    except urllib.error.URLError:
        return False  # genuinely unreachable
    except Exception:
        return True   # answered something non-fatal (bad JSON / ok:false) — it's up


def _ensure_kimi_daemon() -> None:
    """Start the browser-bridge daemon if it isn't listening (no-ops if it is)."""
    if _kimi_daemon_up():
        return
    if not KIMI_BIN.exists():
        raise RuntimeError(f"browser-bridge not installed at {KIMI_BIN}")
    subprocess.Popen(
        [str(KIMI_BIN), "start"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    for _ in range(30):  # cold start can take >10s
        time.sleep(1.0)
        if _kimi_daemon_up():
            return
    raise RuntimeError(
        "browser-bridge daemon did not come up within 30s — check it is installed "
        "and not crashing (start it manually per the daemon's own instructions)"
    )


def _bridge_status() -> dict:
    """GET the daemon's /status JSON; {} when unreachable/unparseable."""
    status_url = KIMI_URL.rsplit("/", 1)[0] + "/status"
    try:
        with urllib.request.urlopen(status_url, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}


def _ensure_browser() -> None:
    """Launch the browser if the bridge extension isn't connected.

    /status.extension_connected is the authoritative signal for "a browser with
    the bridge extension is up". No-ops when already connected.
    """
    if _bridge_status().get("extension_connected"):
        return
    if not BROWSER_EXE or not Path(BROWSER_EXE).exists():
        raise RuntimeError(
            "bridge extension not connected and no browser found to launch — "
            "open the browser with the bridge extension manually, or set OE_BROWSER_EXE"
        )
    subprocess.Popen(
        [str(BROWSER_EXE)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    for _ in range(30):  # browser cold start + extension websocket handshake
        time.sleep(2.0)
        if _bridge_status().get("extension_connected"):
            return
    raise RuntimeError(
        "browser was started but the bridge extension did not connect within 60s — "
        "check the extension is installed/enabled in this browser profile"
    )


def _ensure_oe_tab() -> None:
    """Make an openevidence.com tab the session's current tab, opening one if needed.

    The tab must be on the OE origin: page-context fetch() is what supplies the
    logged-in session, so a wrong-origin tab silently yields an anonymous request.
    """
    _ensure_kimi_daemon()
    _ensure_browser()
    try:
        r = _kimi_cmd("find_tab", {"url": BASE_URL}, timeout=15)
        if r.get("success"):
            return
    except Exception:
        pass  # not open in this session yet
    _kimi_cmd(
        "navigate",
        {"url": f"{BASE_URL}/", "newTab": True, "group_title": "OpenEvidence MCP"},
        timeout=45,
    )


def _relay_request(method: str, path: str, body: dict | None = None, timeout_ms: int = 180000) -> tuple[int, str]:
    """Run one OpenEvidence API request inside your logged-in OE tab.

    Returns (http_status, raw_body_text) exactly as the OE API responded, so all
    downstream parsing (_extract_text, _build_citation_data, ...) is unchanged.
    """
    _ensure_oe_tab()
    job = f"__oe_{secrets.token_hex(4)}"
    body_js = json.dumps(json.dumps(body)) if body is not None else "null"

    # Fire-and-forget: returns instantly so the shared daemon is never blocked.
    fire = f"""(() => {{
      window.{job} = {{done: false}};
      const init = {{method: {json.dumps(method)}, credentials: 'include'}};
      const b = {body_js};
      if (b !== null) {{
        init.body = b;
        init.headers = {{'content-type': 'application/json'}};
      }}
      fetch({json.dumps(path)}, init)
        .then(r => r.text().then(t => {{ window.{job} = {{done: true, status: r.status, body: t}}; }}))
        .catch(e => {{ window.{job} = {{done: true, status: 0, body: '', error: String(e)}}; }});
      return 'started';
    }})()"""
    _kimi_cmd("evaluate", {"code": fire}, timeout=30)

    poll = (
        f"(() => {{ const j = window.{job}; "
        f"if (!j) return JSON.stringify({{gone: true}}); "
        f"return JSON.stringify({{done: !!j.done, status: j.status ?? null, "
        f"len: (j.body || '').length, error: j.error ?? null}}); }})()"
    )
    deadline = time.monotonic() + timeout_ms / 1000
    state: dict = {}
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SEC)
        state = json.loads(_kimi_cmd("evaluate", {"code": poll}, timeout=30)["value"])
        if state.get("gone"):
            # window slot vanished → the OE tab navigated/reloaded mid-flight.
            raise RuntimeError("OE tab reloaded while the request was in flight — retry")
        if state.get("done"):
            break
    else:
        raise RuntimeError(f"OE request timed out after {timeout_ms / 1000:.0f}s ({method} {path})")

    if state.get("error"):
        raise RuntimeError(f"in-tab fetch failed: {state['error']}")

    # Pull the body back in slices — one evaluate response per chunk.
    parts: list[str] = []
    for off in range(0, state["len"], CHUNK):
        code = f"window.{job}.body.slice({off}, {off + CHUNK})"
        parts.append(_kimi_cmd("evaluate", {"code": code}, timeout=30)["value"])
    _kimi_cmd("evaluate", {"code": f"delete window.{job}; 'ok'"}, timeout=15)

    return state["status"], "".join(parts)


# Local data dir (query log + rate log). Override with OE_DATA_DIR.
DATA_DIR = Path(os.environ.get("OE_DATA_DIR", str(Path.home() / ".openevidence")))
QUERIES_LOG = DATA_DIR / "queries.jsonl"
RATE_LOG = DATA_DIR / "rate_log.jsonl"

# Rate-limit settings — a courtesy throttle so this tool respects OpenEvidence's
# own account-level limits (OE returns a "throttled" JSON response, handled below).
# OE's account quota is roughly 100 questions/hour, and only new questions /
# follow-ups spend it (fetch is free). These defaults stay conservative on purpose:
# you are querying with your own account, so leave headroom.
RL_WINDOW_SEC = 60          # sliding window length
RL_MAX_IN_WINDOW = 5        # max queries inside the window
RL_MAX_PARALLEL = 3         # hard concurrent cap
RL_MAX_WAIT_SEC = 120       # max time to wait at window edge; beyond → fail fast

mcp = FastMCP("openevidence")

# ------- Rate limiter (module-level, async-safe) -------
import asyncio
from collections import deque

_rl_recent: deque = deque()   # monotonic timestamps of accepted starts
_rl_lock = asyncio.Lock()
_rl_sem = asyncio.Semaphore(RL_MAX_PARALLEL)


def _rate_log(event: str, **kwargs) -> None:
    try:
        RATE_LOG.parent.mkdir(exist_ok=True)
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **kwargs}
        with RATE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def _rate_acquire() -> str | None:
    """Acquire rate-limit + concurrency slot.

    Returns:
        None on success (caller must finally _rate_release()),
        Error string if window-wait exceeds RL_MAX_WAIT_SEC.
    """
    # Sliding window
    async with _rl_lock:
        now = time.monotonic()
        while _rl_recent and now - _rl_recent[0] > RL_WINDOW_SEC:
            _rl_recent.popleft()
        if len(_rl_recent) >= RL_MAX_IN_WINDOW:
            wait_until = _rl_recent[0] + RL_WINDOW_SEC
            wait = wait_until - now + 0.5
            if wait > RL_MAX_WAIT_SEC:
                _rate_log("blocked_window", in_window=len(_rl_recent), wait_needed=round(wait, 1))
                return (
                    f"Rate-limit: {len(_rl_recent)} queries already in last {RL_WINDOW_SEC}s "
                    f"(max {RL_MAX_IN_WINDOW}). Would need to wait {wait:.0f}s, exceeds RL_MAX_WAIT_SEC={RL_MAX_WAIT_SEC}s. "
                    f"Spread queries out or wait."
                )
            _rate_log("queued", in_window=len(_rl_recent), wait_sec=round(wait, 1))
            await asyncio.sleep(wait)
            # re-purge
            now = time.monotonic()
            while _rl_recent and now - _rl_recent[0] > RL_WINDOW_SEC:
                _rl_recent.popleft()
        _rl_recent.append(time.monotonic())

    # 3. Parallel cap
    await _rl_sem.acquire()
    _rate_log("acquired", in_window=len(_rl_recent), inflight=RL_MAX_PARALLEL - _rl_sem._value)
    return None


def _rate_release() -> None:
    try:
        _rl_sem.release()
    except ValueError:
        pass


def _parse_submit_response(status: int, body: str) -> dict:
    """Parse the /api/article POST response.

    The request runs as a page-context fetch inside your own logged-in tab, so a
    normal call returns JSON. But if the session has expired / you are not logged
    in, OE serves a login HTML page instead, so we still guard the .loads() and
    surface a clear message either way.
    """
    try:
        return json.loads(body)
    except Exception:
        low = body.lower()
        if "log in" in low or "sign in" in low or status in (401, 403):
            return {
                "__autherr__": (
                    f"Not authenticated (HTTP {status}). Log into openevidence.com in your "
                    "normal browser — requests run inside that tab's session via the browser bridge."
                )
            }
        return {"__autherr__": f"Unexpected non-JSON submit response (status {status}): {body[:200]}"}


def _log_query(question: str, article_id: str, parent_id: str | None = None) -> None:
    """Append submitted query to local log for later recovery.

    parent_id: if this is a follow-up, the article_id of the immediately
    preceding article in the conversation thread.
    """
    try:
        QUERIES_LOG.parent.mkdir(exist_ok=True)
        entry: dict = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "article_id": article_id,
            "question": question,
        }
        if parent_id:
            entry["parent_id"] = parent_id
        with QUERIES_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _submit_article(payload: dict) -> dict:
    """POST /api/article via the bridge, retrying once on OE's own throttle."""
    status, body = _relay_request("POST", "/api/article", payload)
    resp_body = _parse_submit_response(status, body)
    if "throttled" in json.dumps(resp_body):
        detail = resp_body.get("errors", [{}])[0].get("detail", "")
        wait_match = re.search(r'(\d+)\s*second', detail)
        wait_secs = int(wait_match.group(1)) + 2 if wait_match else 10
        time.sleep(wait_secs)
        status, body = _relay_request("POST", "/api/article", payload)
        resp_body = _parse_submit_response(status, body)
    return resp_body


def _ask_oe_followup(parent_article_id: str, followup_question: str) -> dict:
    """Submit a follow-up question in the same conversation as a prior article."""
    # OE auto-expands {articleId} into full history.
    payload = {
        "article_type": "Ask OpenEvidence Light with citations",
        "inputs": {
            "history": [{"articleId": parent_article_id}],
            "question": followup_question,
            "attachments": [],
            "use_gatekeeper": True,
            "component_config_version": "stable",
            "variant_configuration_file": "minimal",
        },
        "original_article": parent_article_id,
        "personalization_enabled": False,
    }
    try:
        resp_body = _submit_article(payload)
    except RuntimeError as e:
        return {"error": f"Relay request failed: {e}"}
    if resp_body.get("__autherr__"):
        return {"error": resp_body["__autherr__"]}

    article_id = resp_body.get("id")
    if not article_id:
        return {"error": f"Followup submit failed: {json.dumps(resp_body)[:200]}"}

    _log_query(followup_question, article_id, parent_id=parent_article_id)
    return _poll_article(article_id)


def _poll_article(article_id: str, max_polls: int = 140) -> dict:
    """Poll /api/article/{id}/output until status=success or timeout (~280s)."""
    for _ in range(max_polls):
        time.sleep(2)
        try:
            status, body = _relay_request("GET", f"/api/article/{article_id}/output", timeout_ms=20000)
            data = json.loads(body)
            if data.get("status") == "success":
                return data
            elif data.get("status") == "error":
                return {"error": data.get("user_error_msg", "unknown error")}
        except Exception:
            pass
    return {"error": "Timeout (3 min)"}


def _extract_text(data: dict) -> str:
    raw = data.get("output", {}).get("text", "")
    lines = []
    for line in raw.split("\n"):
        if line.startswith("REACTCOMPONENT!:!Thinking"):
            continue
        if line.startswith("REACTCOMPONENT!:!"):
            parts = line.split("!:!", 2)
            if len(parts) >= 3:
                lines.append(parts[2])
            continue
        lines.append(line)
    text = "\n".join(lines).strip()
    text = re.sub(r'<strong>(.*?)</strong>', r'**\1**', text)
    text = re.sub(r'<a[^>]*>(.*?)</a>', r'\1', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\[\[\[\$\$\$.*?\$\$\$\](?:!!!\[.*?\])?\]\]', '', text)
    text = text.replace('--Practice Guideline--', '## Practice Guideline')
    text = re.sub(r'^----$', '---', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _extract_citations(data: dict) -> list[dict]:
    sa = data.get("output", {}).get("structured_article", {})
    citations = sa.get("citations", [])
    result = []
    for c in citations:
        entry = {"title": c.get("title", "")}
        if c.get("url"):
            entry["url"] = c["url"]
        if c.get("doi"):
            entry["doi"] = c["doi"]
        if c.get("published_date"):
            entry["date"] = c["published_date"]
        result.append(entry)
    return result


def _extract_raw_text(data: dict) -> str:
    """Extract raw_text which already has [N] inline citation markers."""
    sa = data.get("output", {}).get("structured_article", {})
    raw = sa.get("raw_text", "")
    if not raw:
        return _extract_text(data)
    # Strip trailing follow-up question
    raw = re.sub(r'\n\nWould you like to explore.*$', '', raw, flags=re.DOTALL)
    return raw.strip()


def _parse_marker_citations(text: str) -> list[dict]:
    """Parse citation metadata from [[[$$$...$$$]]] markers in output.text.

    Each marker block contains 1+ citations separated by !!!.
    Returns a flat list of unique citations in appearance order with
    whatever metadata can be extracted from the marker text.
    """
    seen: dict[str, int] = {}
    result: list[dict] = []

    # Find all marker blocks: [[[ ... ]]]
    for block_match in re.finditer(r'\[\[\[(.+?)\]\]\]', text):
        block = block_match.group(1)
        # Split by !!! to get individual citation strings
        parts = block.split("!!!")
        for part in parts:
            # Extract text between $$$ delimiters
            segments = part.split("$$$")
            # segments: ['', 'citation text', ']', ...] or ['[', 'citation text', '']
            cit_text = ""
            for seg in segments:
                seg = seg.strip().strip("[]")
                if seg and len(seg) > 10:
                    cit_text = seg
                    break
            if not cit_text:
                continue

            # Extract DOI from href or text
            doi_match = re.search(r'doi[:\s]+([^\s<"]+)', cit_text, re.IGNORECASE)
            doi = doi_match.group(1).rstrip(".") if doi_match else ""

            # Extract title from <a> tag or from text
            title_match = re.search(r'<a[^>]*>([^<]+)</a>', cit_text)
            title = title_match.group(1) if title_match else ""

            # Clean: strip all HTML tags to get plain citation string
            plain = re.sub(r'<[^>]+>', '', cit_text).strip()

            # Extract authors (text before the title/first sentence)
            authors = ""
            if title and title in plain:
                idx = plain.index(title)
                authors = plain[:idx].strip().rstrip(".")
            elif ". " in plain:
                authors = plain.split(". ")[0]

            # Extract date from "Published YYYY-MM-DD" pattern
            date_match = re.search(r'Published\s+(\d{4}-\d{2}-\d{2})', plain)
            date = date_match.group(1) if date_match else ""

            # Dedup by DOI or title
            key = doi or title or plain[:60]
            if key in seen:
                continue
            seen[key] = len(result)

            entry = {"title": title or plain[:120], "doi": doi, "authors": authors}
            if date:
                entry["date"] = date
            result.append(entry)

    return result


def _decode_origin(raw: str) -> str:
    """Decode OE's obfuscated RAG-corpus name (ROT-1: every byte shifted +1).

    `mbodfu`gvmmufyu`tdsbqfe`w3` → `lancet_fulltext_scraped_v2` (the backtick is
    an underscore shifted by one). The corpus a chunk was retrieved from is the
    best available predictor of TRANSITIVE-CITATION risk: text scraped from a
    review's full text is frequently the review *quoting someone else's trial*,
    which OE then attributes to the review itself. Technique credit:
    htlin222/audit-oe-skill.
    """
    if not raw:
        return ""
    try:
        return "".join(chr(ord(c) - 1) for c in raw)
    except ValueError:
        return ""


def _origin_risk(origin: str) -> str:
    """Corpus-level transitive-citation risk. Combine with publication_types
    downstream: scraped-review-fulltext + a quantitative claim is the danger zone.
    """
    if not origin:
        return "UNKNOWN"
    if "fulltext_scraped" in origin:
        return "HIGH"      # scraped review/journal full text — may be quoting others
    if "fulltext" in origin or "media_annotated" in origin:
        return "MEDIUM"    # publisher full text (incl. Cochrane CDSR) / AI-annotated figures
    if "abstracts" in origin:
        return "LOW"       # abstract-level claims, nothing to mis-attribute
    return "UNKNOWN"


def _citation_entry(cit: dict, index: int) -> dict:
    """Flatten one span-level citation object into our citation record."""
    meta = cit.get("metadata", {})
    cd = meta.get("citation_detail", {})
    why = meta.get("why_cited", {})
    cm = meta.get("content_metadata", {})
    origin = _decode_origin(meta.get("origin", ""))
    return {
        "index": index,
        "title": cd.get("title", ""),
        "doi": cd.get("doi", ""),
        "pmid": cd.get("pmid", ""),
        "date": cd.get("dt_published", ""),
        "journal": cd.get("journal_name", ""),
        "authors": cd.get("authors_string", ""),
        "publication_types": cd.get("publication_types", []),
        "impact_score": why.get("impact_score", ""),
        "recency_days": why.get("recency_score", ""),
        "is_guideline": cm.get("is_guideline", False),
        "origin": origin,
        "origin_risk": _origin_risk(origin),
        "url": cd.get("href", ""),
    }


def _build_citation_data(data: dict) -> list[dict]:
    """Map every [N] marker in raw_text to the paper it actually cites.

    OE emits one citation object per marker OCCURRENCE, in document order, so the
    i-th `[N]` in raw_text is the i-th citation object walked span-by-span. We rely
    on exactly that and nothing else.

    The subtlety that makes any other approach wrong: OE assigns a *separate index
    to each retrieved chunk*, so one paper legitimately appears under several
    numbers when it was retrieved from several corpora (in a live 2026-07-14
    article: 10 indices over 7 distinct papers). The previous implementation
    de-duplicated citations by PMID first and then matched them to indices
    positionally — that silently shifted every index past the first duplicate onto
    the WRONG paper and dropped the tail entirely (indices 4-7 mis-attributed, 8-10
    lost). Downstream that is worse than useless: /oe-verify would check a claim
    against a paper OE never cited for it and report a confident verdict.

    If the 1:1 correspondence doesn't hold (marker count != citation-object count),
    we do NOT guess: fall back to the [[[$$$...$$$]]] bibliography and mark those
    entries as having no known provenance.
    """
    sa = data.get("output", {}).get("structured_article", {})
    raw_text = sa.get("raw_text", "")
    output_text = data.get("output", {}).get("text", "")

    # Citation objects in document order — one per marker occurrence.
    flat: list[dict] = [
        cit
        for section in sa.get("articlesection_set", [])
        for para in section.get("articleparagraph_set", [])
        for span in para.get("articlespan_set", [])
        for cit in span.get("citations", [])
    ]
    occurrences = [int(m) for m in re.findall(r"\[(\d+)\]", raw_text)]

    result: list[dict] = []
    if flat and len(flat) == len(occurrences):
        by_index: dict[int, dict] = {}
        for n, cit in zip(occurrences, flat):
            if n not in by_index:            # same index recurs; first occurrence wins
                by_index[n] = _citation_entry(cit, n)
        result = [by_index[n] for n in sorted(by_index)]
    else:
        # Length mismatch → the positional guarantee is void. Degrade explicitly.
        marker_cits = _parse_marker_citations(output_text)
        for i, idx in enumerate(sorted(set(occurrences))):
            if i < len(marker_cits):
                entry = dict(marker_cits[i])
                entry["index"] = idx
            else:
                entry = {"index": idx, "title": f"(citation {idx} — metadata unavailable)"}
            result.append(entry)

    # Fallback/placeholder entries have no corpus provenance. Normalise rather than
    # omit: /oe-verify keys on these fields, and "we don't know the corpus" must read
    # as UNKNOWN, never as a missing key that degrades to a low-risk default.
    for entry in result:
        entry.setdefault("origin", "")
        entry.setdefault("origin_risk", "UNKNOWN")

    return result


def _ask_openevidence(question: str) -> dict:
    """Core function: submit question to OpenEvidence, return parsed result."""
    payload = {
        "article_type": "Ask OpenEvidence Light with citations",
        "inputs": {
            "variant_configuration_file": "minimal",
            "component_config_version": "stable",
            "attachments": [],
            "question": question,
            "use_gatekeeper": True,
        },
        "personalization_enabled": False,
    }
    try:
        resp_body = _submit_article(payload)
    except RuntimeError as e:
        return {"error": f"OE request failed: {e}. Check the browser bridge is running and openevidence.com is logged in."}
    if resp_body.get("__autherr__"):
        return {"error": resp_body["__autherr__"]}

    article_id = resp_body.get("id")
    if not article_id:
        return {"error": f"Submit failed: {json.dumps(resp_body)[:200]}"}

    _log_query(question, article_id)
    return _poll_article(article_id)


def _fetch_existing_article(article_id: str) -> dict:
    """Fetch a previously-submitted article by ID (no new submission)."""
    return _poll_article(article_id, max_polls=10)


@mcp.tool()
async def ask_openevidence(question: str, preserve_citations: bool = False) -> str:
    """Ask OpenEvidence a clinical question. Returns evidence-based answer with citations.

    Best for: treatment guidelines, management recommendations, clinical decision-making.
    Response includes structured text with citations from NEJM, JAMA, Cochrane, NCCN, etc.

    Args:
        question: A clinical medical question in English (e.g. "What is the first-line treatment for patellofemoral pain syndrome?")
        preserve_citations: If True, keep inline [N] citation markers in text and append
            structured Citation Data (JSON) with full metadata (PMID, DOI, impact score,
            publication types). Used by /oe-verify for citation verification.
    """
    # Rate-limit guard (sliding window + parallel cap)
    block = await _rate_acquire()
    if block is not None:
        return f"Error: {block}"

    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(_ask_openevidence, question),
            timeout=300,
        )
    except asyncio.TimeoutError:
        _rate_release()
        return "Error: ask_openevidence wall-clock timeout (300s). Check the browser bridge is up."
    _rate_release()

    if "error" in data:
        return f"Error: {data['error']}"

    if preserve_citations:
        text = _extract_raw_text(data)
        cit_data = _build_citation_data(data)

        result = text
        if cit_data:
            result += "\n\n## Citations\n"
            for c in cit_data:
                authors = c.get("authors", "")
                line = f"{c['index']}. {authors} {c.get('title', '')}."
                if c.get("journal"):
                    line += f" {c['journal']}."
                if c.get("doi"):
                    line += f" DOI: {c['doi']}"
                if c.get("date"):
                    line += f" [{c['date'][:10]}]"
                result += line + "\n"

            result += "\n## Citation Data\n"
            result += json.dumps(cit_data, ensure_ascii=False, indent=2)

        return result

    # Default mode: clean text without inline markers
    text = _extract_text(data)
    citations = _extract_citations(data)

    result = text
    if citations:
        result += "\n\n## Citations\n"
        for i, c in enumerate(citations, 1):
            line = f"{i}. {c['title']}"
            if c.get("doi"):
                line += f" (DOI: {c['doi']})"
            if c.get("date"):
                line += f" [{c['date']}]"
            result += line + "\n"

    return result


def _format_result(data: dict, preserve_citations: bool = False) -> str:
    """Format raw OE response dict into markdown."""
    if "error" in data:
        return f"Error: {data['error']}"

    if preserve_citations:
        text = _extract_raw_text(data)
        cit_data = _build_citation_data(data)
        result = text
        if cit_data:
            result += "\n\n## Citations\n"
            for c in cit_data:
                authors = c.get("authors", "")
                line = f"{c['index']}. {authors} {c.get('title', '')}."
                if c.get("journal"):
                    line += f" {c['journal']}."
                if c.get("doi"):
                    line += f" DOI: {c['doi']}"
                if c.get("date"):
                    line += f" [{c['date'][:10]}]"
                result += line + "\n"
            result += "\n## Citation Data\n"
            result += json.dumps(cit_data, ensure_ascii=False, indent=2)
        return result

    text = _extract_text(data)
    citations = _extract_citations(data)
    result = text
    if citations:
        result += "\n\n## Citations\n"
        for i, c in enumerate(citations, 1):
            line = f"{i}. {c['title']}"
            if c.get("doi"):
                line += f" (DOI: {c['doi']})"
            if c.get("date"):
                line += f" [{c['date']}]"
            result += line + "\n"
    return result


@mcp.tool()
async def list_recent_oe_queries(limit: int = 20) -> str:
    """List recently-submitted OpenEvidence queries from local log.

    Returns JSON list of {ts, article_id, question}. Use this to recover
    article_ids after an agent was killed or session lost — pass article_id
    to `fetch_oe_result` to retrieve the answer without re-submitting.

    Args:
        limit: Max number of recent entries to return (default 20).
    """
    if not QUERIES_LOG.exists():
        return "No queries logged yet."
    lines = QUERIES_LOG.read_text(encoding="utf-8").strip().split("\n")
    entries = []
    for line in lines[-limit:]:
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    return json.dumps(entries, ensure_ascii=False, indent=2)


@mcp.tool()
async def ask_oe_followup(parent_article_id: str, followup_question: str, preserve_citations: bool = False) -> str:
    """Ask a follow-up question in the same OpenEvidence conversation thread.

    The follow-up sees the prior Q&A as context, so you can drill down without
    re-stating background. Use `list_recent_oe_queries` to find the parent
    article_id.

    Args:
        parent_article_id: article_id of the previous OE article to follow up on.
        followup_question: The follow-up question (English, clinical).
        preserve_citations: Same as ask_openevidence.
    """
    block = await _rate_acquire()
    if block is not None:
        return f"Error: {block}"
    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(_ask_oe_followup, parent_article_id, followup_question),
            timeout=300,
        )
    except asyncio.TimeoutError:
        _rate_release()
        return "Error: ask_oe_followup wall-clock timeout (300s)."
    _rate_release()
    return _format_result(data, preserve_citations=preserve_citations)


@mcp.tool()
async def fetch_oe_result(article_id: str, preserve_citations: bool = False) -> str:
    """Fetch a previously-submitted OpenEvidence article by its ID.

    Use this to recover answers when the original `ask_openevidence` call
    was interrupted (e.g. agent killed) but the question was already submitted.
    Get article IDs via `list_recent_oe_queries`.

    Args:
        article_id: The OpenEvidence article ID (UUID).
        preserve_citations: Same as ask_openevidence — keep inline [N] markers.
    """
    import asyncio
    data = await asyncio.to_thread(_fetch_existing_article, article_id)
    return _format_result(data, preserve_citations=preserve_citations)


if __name__ == "__main__":
    mcp.run(transport="stdio")
