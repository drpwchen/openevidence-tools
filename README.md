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
  in your normal browser.
- **A local browser-bridge daemon.** The server does not scrape or spoof anything: it runs each
  OpenEvidence API call as a page-context `fetch()` **inside your own already-logged-in
  openevidence.com browser tab**, driven through a small local daemon that listens on
  `127.0.0.1`. Every request therefore uses *your own account and session* — exactly what would
  happen if you clicked in the tab yourself. The reference implementation targets a daemon
  called `kimi-webbridge` (default endpoint `http://127.0.0.1:10086/command`);
  point `OE_BRIDGE_URL` at any daemon that speaks the same simple `{action, args, session}`
  JSON contract. OpenEvidence has no public API and its session cookie is httpOnly, which is why
  an in-tab fetch (rather than a plain HTTP client) is used at all.
- **Python 3.10+** and the deps in `requirements.txt` (`mcp`).

### Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `OE_BRIDGE_URL` | `http://127.0.0.1:10086/command` | Browser-bridge daemon endpoint. |
| `OE_BRIDGE_BIN` | `~/.kimi-webbridge/bin/kimi-webbridge.exe` | Daemon binary to auto-start if it isn't running. |
| `OE_DATA_DIR` | `~/.openevidence` | Where the query log and rate log are written. |

### Install

```bash
pip install -r requirements.txt

# Register with an MCP client, e.g. Claude Code:
claude mcp add openevidence -- python /path/to/openevidence-tools/mcp/server.py

# Put skills/oe-verify/ where your client discovers skills.
```

Log into openevidence.com in your normal browser and make sure your browser-bridge daemon is
running; the server will open/reuse an OpenEvidence tab as needed.

---

## Responsible use

- **Your own account, your own session.** This tool automates *your* logged-in browser tab. Use
  it only with an account you are entitled to use, and follow OpenEvidence's terms of service.
- **Respect the quota.** OpenEvidence's account quota is roughly **100 questions/hour**, and only
  new questions and follow-ups spend it (re-fetching a prior answer is free). The server enforces
  a built-in courtesy rate limiter (sliding window + concurrency cap) that stays conservative on
  purpose — don't raise it to hammer the service.
- **Verify before clinical use.** Run `/oe-verify` (or otherwise check the primary sources) before
  you rely on an OE answer. That is the entire point of this repo.
- **Not medical advice.** These tools help a clinician evaluate evidence; they do not replace
  clinical judgment, and their output is not medical advice.

---

## Relationship to upstream

This is **not a fork** of [htlin222/openevidence-mcp](https://github.com/htlin222/openevidence-mcp).
It has its own tool layer, because the verifier depends on fields upstream doesn't expose —
`publication_types`, `impact_score`, `recency_score`, `is_guideline`, and the per-chunk `origin`
provenance.

With thanks to **[htlin222](https://github.com/htlin222)** for two ideas this project builds on:
the browser-relay transport concept, and the `origin` ROT-1 decode technique
(from `audit-oe-skill`), which is what makes the provenance-tiered verification possible.

## Works with textbook-to-note

`/oe-verify` is designed to plug into the
[textbook-to-note](https://github.com/drpwchen/textbook-to-note) note-writing workflow as its
optional **evidence-enrichment stage**: when a note's management/treatment section needs current
guidance, `ask_openevidence` fetches it and `/oe-verify` gates it before it enters the note.
Standalone use — just asking OE questions and verifying the answers — is equally fine.
