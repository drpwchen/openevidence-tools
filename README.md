# openevidence-tools

*[繁體中文版 → README.zh-TW.md](README.zh-TW.md)*

Two pieces that work together to use [OpenEvidence](https://www.openevidence.com) as an
evidence source you can actually trust:

1. **An MCP server** that exposes OpenEvidence to any MCP client (Claude Code, etc.) as four
   tools — `ask_openevidence`, `list_recent_oe_queries`, `fetch_oe_result`, `ask_oe_followup`.
2. **The `/oe-verify` skill**, which cross-checks *every* citation in an OE answer against
   primary sources and flags what OE left out.

**Why both?** OpenEvidence answers clinical questions fast, but it is a RAG pipeline (vector
search + LLM synthesis), and its citations can be wrong even when the underlying claim is real.
The classic failure is **transitive citation**: OE retrieves a chunk of a *review's* full text
in which the review quotes *another* trial's number, then credits that number to the review
itself. The claim is true; the attribution is false. **Never absorb an OE answer unverified** —
that is the whole reason `/oe-verify` exists.

---

## What the MCP server exposes

| Tool | Purpose |
|---|---|
| `ask_openevidence(question, preserve_citations=False)` | Submit a new clinical question; returns evidence-based answer + citations. Auto-logs the article_id for recovery. |
| `list_recent_oe_queries(limit=20)` | List recently-submitted queries (ts, article_id, question) — recover an article_id after an interrupted run. |
| `fetch_oe_result(article_id, preserve_citations=False)` | Re-fetch a previously-submitted answer by ID. This is a GET and costs **no** question quota. |
| `ask_oe_followup(parent_article_id, followup_question, preserve_citations=False)` | Drill down in the same conversation thread; OE expands the prior Q&A as context. |

### `preserve_citations=True` — the provenance channel `/oe-verify` consumes

With `preserve_citations=True`, the answer keeps its inline `[N]` markers and appends a
`## Citation Data` JSON block. Each entry carries the metadata the verifier needs — `title`,
`doi`, `pmid`, `journal`, `authors`, `publication_types`, `impact_score`, `recency_days`,
`is_guideline`, `url`, plus two provenance fields:

- **`origin`** — the RAG corpus the cited chunk was retrieved from. OE ships this ROT-1
  obfuscated (every byte shifted +1, so `_` arrives as a backtick); the server decodes it, e.g.
  `` mbodfu`gvmmufyu`tdsbqfe`w3 `` → `lancet_fulltext_scraped_v2`.
- **`origin_risk`** — a transitive-citation risk tier derived from `origin`:

  | Tier | Corpora | Meaning |
  |---|---|---|
  | **HIGH** | `*_fulltext_scraped_*` (lancet, nejm, aan, …) | Scraped **review full text** — the quoted sentence is often the review citing *someone else's* trial. This is OE's main failure mode. |
  | **MEDIUM** | `wiley_cdsr_fulltext` (Cochrane), `wiley_research_fulltext`, `guidelines_fulltext_*`, `media_annotated_gemini` | Publisher full text / AI-annotated figures — real chunks, still not necessarily the paper's own finding. |
  | **LOW** | `pubmed_abstracts_*` | Abstract-level; little room to mis-attribute. |
  | **UNKNOWN** | no span metadata | Provenance unavailable — treated as MEDIUM, never as LOW. |

`/oe-verify` uses `origin_risk` to aim its attribution check: a HIGH/MEDIUM chunk backing a
**quantitative** claim is the danger zone, and each such citation gets traced back to whether
the number is really *that paper's own* finding.

> One paper can legitimately hold several `[N]` indices — OE indexes per retrieved *chunk*, not
> per paper (observed: 10 indices over 7 papers). The server maps markers to citation objects
> positionally (the i-th `[N]` occurrence == the i-th citation object in document order) and, on
> any count mismatch, degrades explicitly rather than guessing.

---

## The `/oe-verify` skill

A three-layer verification pass over one OE answer (`skills/oe-verify/SKILL.md`):

- **Step 0.5 — existence gate**: every DOI is checked against CrossRef *before* any semantic
  work. A DOI that doesn't resolve is `FABRICATED`; no agent tokens are wasted on it.
- **Layer 1 — per-citation accuracy** (parallel cheap agents): look each paper up in
  Semantic Scholar / PubMed, compare what OE claimed against what the source says, and — for
  HIGH/MEDIUM-provenance quantitative claims — run the attribution check that catches transitive
  citation.
- **Layer 2 — completeness** (one stronger agent): anchor on the best recent systematic
  review / meta-analysis and ask what OE *omitted*, in both directions, plus an applicability
  check against your own practice context.
- **Layer 3 — internal contradiction scan**: do the papers OE itself cited actually agree with
  each other? Only runs when ≥2 papers make quantitative claims on the same outcome.

The output is a compact Markdown report with a per-citation verdict table (✅ accurate, ⚠️
overstated, ❌ misrepresented, 🚫 fabricated, ↗️ transitive, 🔄 secondary-untraced, ❓
unverifiable) and an evidence-landscape summary.

---

## Requirements

- **An OpenEvidence account** (OpenEvidence is intended for clinicians). You log in yourself,
  once, in the browser window the server opens for you.
- **Edge, Chrome or Chromium** installed — on **Windows, macOS or Linux**. The server does not
  scrape or spoof anything: it runs each OpenEvidence API call as a page-context `fetch()`
  **inside a logged-in openevidence.com tab**, so every request uses *your own account and
  session* — exactly what would happen if you clicked in the tab yourself. OpenEvidence has no
  public API and its session cookie is httpOnly, which is why an in-tab fetch (rather than a plain
  HTTP client) is used at all.

  By default (transport `cdp`) the server starts **a dedicated copy of your browser on its own
  profile** (`~/.openevidence/browser-profile`) and drives it over the Chrome DevTools Protocol
  on `127.0.0.1:9222`, through Playwright. It is launched on demand and left running; nothing is
  downloaded, and your everyday browser profile is never touched. No extension, no third-party
  daemon.
- **Python 3.10+** and the deps in `requirements.txt` (`mcp`, pinned `<2` — see below — and
  `playwright`). Only the Python package is needed: **skip `playwright install`**, it attaches to
  the browser you already have.

> **`mcp` must stay below 2.0.** mcp 2.0 removed `mcp.server.fastmcp`, so an unpinned install
> resolves to 2.x and `server.py` dies at import with `ModuleNotFoundError` — which an MCP client
> only ever surfaces as `Failed to connect — Connection closed`, sending you hunting in the wrong
> place. `requirements.txt` pins it; if you install by hand, pin it too.

> **🔒 Log in to OpenEvidence only, in the dedicated browser.** While it runs, any program on
> your own machine can drive that browser through its debugging port (the port listens on
> `127.0.0.1` only, never on the network). That is exactly why it gets a separate profile: keep
> it for OpenEvidence, and do not sign in to your bank, mail or anything else there.

<details>
<summary>Legacy transport: Kimi WebBridge (<code>OE_TRANSPORT=http</code>)</summary>

Versions up to 0.3.x drove your *everyday* browser through
**[Kimi WebBridge](https://www.kimi.com/features/webbridge)** (a browser extension plus a local
daemon on `http://127.0.0.1:10086/command`). It still works: set `OE_TRANSPORT=http`, or set
`OE_BRIDGE_URL` to any daemon that speaks the same `{action, args, session}` JSON contract
(setting `OE_BRIDGE_URL` selects this transport by itself, so existing configurations keep
working). Note there are unrelated third-party projects using the name `kimi-webbridge`, some of
which expose an *unauthenticated* port 10086; this means the official one. The daemon auto-starts
only on Windows (or wherever `OE_BRIDGE_BIN` points at a binary that accepts `start`); on
macOS/Linux start the bridge yourself. A debugger-based bridge holds your logged-in identity on
every site at once — if you use one, consider
**[kimi-webbridge-lockdown](https://github.com/drpwchen/kimi-webbridge-lockdown)** to fence your
bank, brokerage and mailbox out of its reach.

</details>

### Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `OE_TRANSPORT` | `cdp` (or `http` when `OE_BRIDGE_URL` is set) | `cdp` = dedicated browser over DevTools; `http` = legacy Kimi WebBridge daemon. |
| `OE_BROWSER_EXE` | auto-detected per platform (Edge/Chrome/Chromium) | Browser to launch when none is running. |
| `OE_CDP_PORT` | `9222` | DevTools port of the dedicated browser. Change it if something else already uses 9222. If a browser already answers on this port, the server uses it instead of launching one. |
| `OE_CDP_PROFILE` | `<OE_DATA_DIR>/browser-profile` | Profile directory of the dedicated browser — your OpenEvidence login lives here. |
| `OE_BROWSER_ARGS` | *(empty)* | Extra flags for launching the dedicated browser, e.g. `--no-sandbox` inside a container. If the browser fails to start, its own output is in `<OE_DATA_DIR>/browser-launch.log` and quoted in the error. |
| `OE_BRIDGE_URL` | `http://127.0.0.1:10086/command` | `http` transport only: browser-bridge daemon endpoint. |
| `OE_BRIDGE_BIN` | `~/.kimi-webbridge/bin/kimi-webbridge.exe` on Windows; **unset elsewhere** | `http` transport only: daemon binary to auto-start. Unset ⇒ no auto-start. |
| `OE_DATA_DIR` | `~/.openevidence` | Where the query log, rate log and submission-spacing state are written. |
| `OE_SUBMIT_SPACING_SEC` | `700` (≈11.7 min) | Minimum wall-clock gap between two question submissions. Lower it only against measurements of your own account — see below. |

### Install

```bash
pip install -r requirements.txt

# Log in to OpenEvidence once, in the dedicated browser this opens:
python mcp/server.py --login

# Register with an MCP client, e.g. Claude Code:
claude mcp add openevidence -- python /path/to/openevidence-tools/mcp/server.py

# Put skills/oe-verify/ where your client discovers skills.
```

The login is kept in the dedicated profile; repeat `--login` only if OpenEvidence logs you out.
From then on the server is self-healing: it launches the browser if it is closed, reconnects if
it was restarted, and opens/reuses an OpenEvidence tab as needed. A `Not authenticated` error
means the session expired — run `--login` again.

Check the transport on your machine without touching OpenEvidence (it starts a throwaway browser
profile against a local stand-in server, then closes it):

```bash
python tests/smoke_cdp.py              # Linux without a display: xvfb-run -a python tests/smoke_cdp.py
python -m unittest discover -s tests   # unit tests, no browser needed
```

Both run in CI on Windows, macOS and Linux.

---

## Responsible use

- **Your own account, your own session.** This tool automates *your* logged-in browser tab. Use
  it only with an account you are entitled to use, and follow OpenEvidence's terms of service.
- **Respect the quota — it is a gap, not a bucket.** Since the 2026-09 site revamp OpenEvidence
  no longer throttles by "N per hour" but by a long **minimum gap between submissions**, and a
  throttled request appears to *re-arm* that gap rather than being free. Measured on one account
  2026-09-02: submissions ≥11 min apart all succeeded (4/4 at 11, 13, 14 and 16 min); a 6-min gap
  failed, as did every burst fired within a couple of minutes. So the server serialises
  submissions and paces them at `OE_SUBMIT_SPACING_SEC` (default 700s), persisting the last
  attempt to `OE_DATA_DIR/submit_state.json` so that **restarting the server does not reset the
  clock**. Only new questions and follow-ups are paced; re-fetching a prior answer is free.
- **Never retry straight after a 429.** OE's own `Expected available in 0 seconds` is always 0 and
  cannot be used. Retrying on it does not recover — it extends the lockout. The server therefore
  reports the time to come back instead of retrying, and you should do the same by hand.
- **Verify before clinical use.** Run `/oe-verify` (or otherwise check the primary sources) before
  you rely on an OE answer. That is the entire point of this repo.
- **Not medical advice.** These tools help a clinician evaluate evidence; they do not replace
  clinical judgment, and their output is not medical advice.

---

## When OpenEvidence changes its site

There is no public API here — the server talks to the same private endpoints the website
talks to, so a site revamp can break it. The **2026-08-26 revamp** (new sidebar, Discover,
Tools, voice mode) broke every part of the path at once. All five faults are fixed in 0.3.0,
and they are worth writing down because the same shapes will recur:

| Symptom | Actual cause | Fix |
|---|---|---|
| Every submission returns `429 throttled` | The throttle window grew to roughly 10 minutes, **and a failed request extends it**. The old code retried 2s after a 429 (OE's own `Expected available in 0 seconds` is always 0 and cannot be used), which kept the account locked out indefinitely | Cross-process minimum spacing between submissions; a 429 reports when to come back instead of retrying |
| Payload rejected | `variant_configuration_file` moved `minimal` → `prod`; `component_config_version` was removed | Payload realigned with what the site now sends |
| A blob of JSON at the top of the answer | The generation-step component was renamed `Thinking` → `InlineGenerationStep`, and only the old name was being skipped | Skip both |
| The citation list vanished entirely | `structured_article.citations` was removed | Derive the list from the per-span citation objects; keep the old field as the path for pre-revamp articles |
| Answers containing a table or figure lost every PMID and corpus origin | Table/figure components expand into `<mdtable>` / `<visual>` regions in `raw_text`, whose `[N]` markers do not match the component span's citation objects one-for-one (7 markers against 3 objects in a live article). That broke the 1:1 assumption behind citation matching and degraded the whole answer | Match prose against prose — exclude component regions from both sides — and mark an index that appears *only* inside a component as metadata-unavailable rather than guessing |

`<follow-up-question>` / `<follow-up-topic>` / `<visual>` are new wrapper tags too; the old
"Would you like to explore" sentence match no longer fired, so that trailing chrome was
leaking into the answer body. Both forms are now stripped.

If something breaks again, the fastest route is the one used here: watch what the *website
itself* sends from your own browser's network panel, and diff it against what this server
sends.

---

## Credit where it's due — htlin222

This project started independently: I wanted OpenEvidence answers to flow into my note-writing
workflow, and the first version drove the site with my own browser automation. That version died
to bot detection, and it was while looking for a way forward that I found
**[Hsieh-Ting Lin (htlin222)](https://github.com/htlin222)**'s work. Much of what this repo does
well now comes from his more recent updates, and it is worth being specific about what came from
where.

From **[audit-oe-skill](https://github.com/htlin222/audit-oe-skill)** (MIT) came the core insight
this whole repo is built around: that OpenEvidence's `origin` field is ROT-1 obfuscated, that it
names the RAG corpus a chunk was retrieved from, and that the corpus therefore *predicts* how
likely the citation is to be mis-attributed. He also named the failure mode — **transitive
citation**, where a review quotes another trial's number and OE credits the review — and built
the verification architecture we adopted: one cheap agent per citation in parallel, a trace-back
stage for quantitative claims in review full text, and a single stronger agent scanning the cited
papers for contradictions with each other, graded CRITICAL / WARNING / NOTE. Our `origin_risk`
tiers are his risk table; our Layer 1 and Layer 3 are his Phase 3 and Phase 4. When you read our
verification report, most of the questions it asks are his questions.

From **[openevidence-mcp](https://github.com/htlin222/openevidence-mcp)** (Apache-2.0) came the
transport idea that got us unstuck: OpenEvidence has no public API and an httpOnly session cookie,
so don't scrape it — run the call inside the clinician's own already-logged-in browser tab. Our
implementation is our own (and now drives a dedicated browser over the DevTools Protocol
rather than an extension), but the direction was his.

We wrote our own tool layer rather than forking, for a practical reason: our verifier needs
`publication_types`, `impact_score`, `recency_score`, `is_guideline` and per-chunk `origin`
returned through the MCP contract itself, and we drive the browser through a general-purpose
channel rather than a dedicated extension. We also added a completeness layer his skill doesn't have (what did
OE *omit*?), a CrossRef existence gate, and a marker↔citation index mapping our architecture
needs and his does not. Those are additions to his foundation, not a replacement for it.

`skills/oe-verify/` is derived from `audit-oe-skill`, MIT © 2026 Hsieh-Ting Lin; see
[NOTICE](NOTICE). Thank you, Hsieh-Ting.

## Works with textbook-to-note

`/oe-verify` is designed to plug into the
[textbook-to-note](https://github.com/drpwchen/textbook-to-note) note-writing workflow as its
optional **evidence-enrichment stage**: when a note's management/treatment section needs current
guidance, `ask_openevidence` fetches it and `/oe-verify` gates it before it enters the note.
Standalone use — just asking OE questions and verifying the answers — is equally fine.

---

## 🌱 Start here if you're new to AI agents ／ AI agent 新手起點

This tool is one piece of my personal AI workflow. If you want to learn how to use AI agents like Claude Code from zero (no programming background needed), I wrote a beginner series (in Traditional Chinese):

這個工具是我個人 AI 工作流的一部分。想從零開始學怎麼用 Claude Code 這類 AI agent（不需要程式背景），可以從我的入門系列開始：

1. [從零開始：安裝、看懂 GitHub、跑起你的第一個工具](https://drpwchen.com/posts/getting-started/)
2. [怎麼跟 AI agent 講話：心法、元技能與規則檔](https://drpwchen.com/posts/talking-to-agents/)
3. [自動化流程不是設計出來的，是長出來的](https://drpwchen.com/posts/growing-your-workflow/)

Full map of my tools and posts ／ 所有工具與文章的全貌 → [drpwchen.com/map](https://drpwchen.com/map/)
