---
name: oe-verify
version: "1.1.0"
description: >
  Verify OpenEvidence clinical responses by cross-checking each citation against
  primary sources and detecting evidence omissions. Two-layer verification:
  Layer 1 (Haiku agents) checks per-citation accuracy; Layer 2 (Sonnet agent)
  checks evidence completeness. Use when: user says "/oe-verify", "verify OE",
  or auto-triggered after ask_openevidence in a note-writing workflow.
triggers:
  - "oe-verify"
  - "verify OE"
  - "check OE citations"
---

# /oe-verify — OpenEvidence Citation Verification

## Why This Skill Exists

OpenEvidence uses RAG (vector search + LLM synthesis) to answer clinical questions.
Its structural weaknesses cannot be fixed by improving the prompt:
1. **Transitive citation** — OE retrieves a chunk of a review's full text in which the review
   quotes *another* trial's number, then credits that number to the review. The claim is real;
   the attribution is wrong. The `origin` corpus field (Step 0) tells us exactly when to suspect
   this, which is why provenance drives the whole Layer 1 flow.
2. **Secondary source confusion** — embedding search treats review-article claims the same as primary findings
3. **Impact-score ranking bias** — older high-IF positive results rank above newer negative ones
4. **Selective presentation** — synthesis picks supporting evidence, not a balanced view.
   Layer 2 (external anchor: what did OE *omit*) and Layer 3 (do OE's own citations contradict
   each other?) exist for this.

This skill independently verifies OE output before you absorb it.

## Transport

OE calls run through the `openevidence` MCP server (see the server README in this repo),
which executes them as page-context `fetch()` inside your logged-in openevidence.com tab
via a local browser bridge. If OE tools fail: check the bridge daemon is up (default
`http://127.0.0.1:10086`) and openevidence.com is logged in in your normal browser.

## Input Modes

| Mode | Trigger | Behavior |
|------|---------|----------|
| Question | `/oe-verify {question}` | Call `ask_openevidence(question, preserve_citations=True)`, then verify |
| In-context | `/oe-verify` (no args) | Verify the most recent OE response in this conversation |
| Auto-trigger | From a note-writing workflow | Receives OE response + question, runs verification silently |

## Pipeline

### Step 0: Obtain & Parse OE Response

**If calling OE directly**: use `ask_openevidence(question, preserve_citations=True)`.

**Parse the response**:
1. If `## Citation Data` JSON block exists → `json.loads()` to get citation array
2. Each citation object has: `index`, `title`, `doi`, `pmid`, `date`, `journal`, `authors`, `publication_types`, `impact_score`, `recency_days`, `is_guideline`, `origin`, `origin_risk`, `url`
3. The body text uses `[N]` inline markers matching `citation.index`

**`origin` / `origin_risk` — which corpus the claim was retrieved from**:

OE is a RAG system, and `origin` names the corpus the cited chunk came from. The MCP
server decodes it (OE ships it ROT-1 obfuscated) and tiers it:

| `origin_risk` | Corpora | What it means for verification |
|---|---|---|
| `HIGH` | `*_fulltext_scraped_*` (lancet, nejm, aan, …) | Text scraped from a **review's full text**. The sentence OE quotes is frequently the review *citing someone else's trial* — so OE attributes another study's number to this paper. **This is the main OE failure mode.** |
| `MEDIUM` | `wiley_cdsr_fulltext` (Cochrane), `wiley_research_fulltext`, `guidelines_fulltext_*`, `media_annotated_gemini` | Publisher full text or AI-annotated figures. Real full-text chunks, but still not necessarily the paper's own finding. |
| `LOW` | `pubmed_abstracts_*` | Abstract-level. Little room for mis-attribution. |
| `UNKNOWN` | (no span metadata) | Provenance unavailable — treat as MEDIUM, never as LOW. |

⚠️ **One paper can hold several indices.** OE assigns an index per retrieved *chunk*, so
the same PMID legitimately appears under multiple `[N]` (observed: 10 indices over 7
papers). Two consequences:
- **Dedupe by PMID/DOI before spawning agents** — verify each *paper* once, then apply its
  verdict to every index it owns. Spawning one agent per index burns tokens re-verifying
  the same paper.
- Never assume `[N]` ordering equals the bibliography order.

**Build claim-span map**:
1. Split body text into sentences (split on `. ` but preserve `e.g.`, `vs.`, `et al.`, numbered decimals)
2. For each sentence containing `[N]`, record: `{index: N, claim: "sentence text", context: "preceding sentence for context"}`
3. Group by citation index, then merge groups that share a paper → each merged group is one Haiku agent's input
4. If a sentence has multiple `[N][M]`, it maps to both citations
5. Mark a claim `quantitative: true` if it contains a number that carries an evidential load —
   effect size, HR/OR/RR, CI, %, p-value, n. `HIGH`/`MEDIUM` origin + quantitative claim is
   the transitive-citation danger zone (Layer 1 Step 5 below).

### Step 0.5: Deterministic Existence Gate (main agent, BEFORE spawning Haiku)

A citation-shaped object is not a source — OE returns well-formatted metadata whose DOI can
be fabricated or resolve to a different paper. Existence checking is mechanical; do it
deterministically and cheaply BEFORE spending any agent tokens on semantic comparison:

1. For each selected citation with a DOI: WebFetch `https://api.crossref.org/works/{doi}`
   (no key needed; batch all fetches in parallel).
   - 404 / no `message` → verdict **FABRICATED** immediately. Do NOT spawn a Haiku agent
     for it — the semantic layer is meaningless for a nonexistent source.
   - Resolves → compare returned `title` vs OE's `citation.title` (token-overlap, not
     exact-match). Low overlap → keep the citation in the Haiku batch but prepend to that
     agent's prompt: "⚠️ DOI resolves to a different title than OE reports — verify
     identity via PMID/title search before trusting any content."
2. No DOI but has PMID → skip the gate here; the Haiku agent's PubMed lookup doubles as
   the existence check.
3. Neither DOI nor PMID → note "existence unverified" and let the Haiku agent attempt
   title search; failure there → UNVERIFIABLE as usual.

Failure-mode rule: if CrossRef itself is unreachable, do not treat the gate as passed —
proceed to Layer 1 but mark those rows "existence gate skipped (CrossRef down)" in the
report. A verification-tool outage must degrade to explicit uncertainty, never to a
silent pass.

### Step 1: Layer 1 — Citation Verification (Parallel Haiku Agents)

**Citation cap**: Max 10 **papers** (after PMID/DOI dedupe). If more exist, prioritize by:
1. `origin_risk` HIGH first — that is where mis-attribution actually lives
2. Citations backing key therapeutic claims (not background/epidemiology)
3. Most recent citations (lower `recency_days`)
4. Citations with DOI or PMID (verifiable)

**For each paper**, spawn `Agent(model: "haiku")` with this prompt template:

```
You are verifying a citation from OpenEvidence. Your job: check whether the
cited paper actually supports what OE claims.

## Citation
- Title: {title}
- DOI: {doi}
- PMID: {pmid}
- Journal: {journal}
- Date: {date}
- Publication types: {publication_types}
- OE retrieved this from corpus: {origin} (transitive-risk tier: {origin_risk})

## OE Claims Referencing This Citation
{numbered list of claim sentences; mark each [quantitative] where applicable}

## Instructions
1. Look up this paper:
   - First try: `mcp__semantic-scholar__get_paper_details` with DOI or title
   - Fallback: `mcp__claude_ai_PubMed__get_article_metadata` with PMID or title
   - If both fail: mark as UNVERIFIABLE
2. Read the abstract (and `tldr` if available from Semantic Scholar)
3. For each OE claim, compare against what the source actually says
4. Determine study type: is this a PRIMARY study (RCT, cohort, case-control,
   case series) or SECONDARY source (review, meta-analysis, guideline, scoping review)?
5. **Attribution check — run this whenever the corpus tier is HIGH or MEDIUM and the
   claim is quantitative.** The tier means OE pulled this text from the paper's full
   text, and reviews spend most of their full text restating OTHER studies' results.
   So ask specifically: is the number OE quotes **this paper's own finding**, or is
   this paper merely reporting someone else's trial?
   - If the abstract shows the paper generated this data → is_primary_source: true
   - If the claim names or implies another trial (e.g. "the LEADER trial showed…"),
     or the paper is a review/guideline restating pooled results → is_primary_source: false.
     Then TRACE IT: find the original study (Semantic Scholar `get_paper_references`,
     or PubMed search on the trial name / the exact effect size) and check whether the
     number matches the original. Report what the ORIGINAL said.
   - Verdict TRANSITIVE = the number is real but belongs to a different paper than the
     one OE cited for it.

## Output Format (use EXACTLY this structure)
CITATION_INDEX: {index — list all indices this paper holds}
VERDICT: [one of: ACCURATE, OVERSTATED, MISREPRESENTED, TRANSITIVE, SECONDARY_UNTRACED, UNVERIFIABLE]
STUDY_TYPE: {RCT|Systematic Review|Meta-analysis|Guideline|Scoping Review|Cohort|Case series|Other}
IS_SECONDARY: {true|false}
IS_PRIMARY_SOURCE: {true|false — did THIS paper generate the data OE attributes to it?}
OE_CLAIMS:
- Claim 1: {what OE said}
  SOURCE_SAYS: {what the paper actually says}
  MATCH: {true|false|partial}
- Claim 2: ...
PRIMARY_TRACE: {if not primary source — the original paper's title/DOI, what IT actually
  found, and whether OE's number matches it. "N/A" if this paper is the primary source}
KEY_FINDINGS_FROM_SOURCE: {what this paper actually concluded — 1-2 sentences. Layer 3
  uses this to cross-check citations against each other, so state findings, not verdicts}
EXPLANATION: {1-3 sentences summarizing the verification result}
```

**Spawn all agents in a single message** (parallel execution).

### Step 2: Layer 2 — Evidence Completeness Check (Sonnet Agent)

Spawn 1x `Agent(model: "sonnet")` **in the same message** as Layer 1 agents (runs in parallel).

Prompt template:

```
You are checking whether OpenEvidence gave a BALANCED picture of the evidence
for a clinical question. Your job is neutral — find what OE missed in BOTH
directions (positive AND negative), anchored on the highest-level evidence
synthesis available.

## Clinical Question
{original question}

## Citations OE Used
{numbered list: index, title, DOI, year, study type}

## Instructions
1. **First priority: find the best evidence synthesis.**
   Search for the most recent systematic review or meta-analysis on this exact
   clinical question. This is your anchor for judging OE's balance.
   - `mcp__semantic-scholar__search_papers`: query="{topic} systematic review
     meta-analysis", year="2020-2026", limit=15
   - `mcp__claude_ai_PubMed__search_articles`: query="{topic} AND
     (systematic review[pt] OR meta-analysis[pt])", date_from="2020",
     max_results=15
2. **Then broaden**: search for RCTs, guidelines, and safety studies not in OE.
   - `mcp__semantic-scholar__search_papers`: query="{clinical question}",
     year="2021-2026", limit=20
3. **Compare OE's narrative against the anchor MA/SR:**
   - Does OE's conclusion align with the pooled effect from the MA?
   - Does OE present the effect size / confidence level accurately?
   - Does OE acknowledge limitations the MA highlights?
   - Are there important nuances (subgroups, safety signals, effect size
     comparisons to other populations) that OE omits?
4. **Identify missing papers** — but classify them fairly:
   - Papers that CHANGE the conclusion (high importance)
   - Papers that ADD nuance without changing direction (medium)
   - Papers that are supplementary/confirmatory (low — don't inflate the
     "missing" list with these)
5. **Applicability / context check**: correct evidence can still be the wrong answer for
   your setting. Compare the evidence base's populations and care settings against your
   own practice context (region, specialty, and health-system funding model): pediatric
   vs adult, inpatient vs outpatient, resource assumptions (devices/drugs not available
   or not reimbursed in your setting), ethnicity-dependent dosing or epidemiology. Flag
   mismatches — do not judge efficacy here, only transferability.
6. Compute citation age distribution from OE's citations.

IMPORTANT: Do not assume OE is wrong. Your job is to assess balance, not to
build a case against OE. If OE's conclusion aligns with the best available
MA/SR, say so — then note what nuances are missing.

## Output Format
ANCHOR_SYNTHESIS:
- {Author Year} "{Title}" (DOI) — {key finding: pooled effect size, confidence}
- OE alignment: {agrees/partially agrees/disagrees with anchor}

MISSING_PAPERS:
- {Author Year} "{Title}" (DOI) — {importance: HIGH/MEDIUM/LOW} — {what it adds}
- ...
(Write "None of high importance" if OE's coverage is adequate)

AGE_DISTRIBUTION:
- OE citations: median year={Y}, range={min}-{max}
- {N} of {total} citations are >5 years old

HETEROGENEITY: {conflicting findings, or "No significant heterogeneity"}

APPLICABILITY: {population/setting mismatches vs your practice context, e.g. "all RCTs
in Western outpatient cohorts, device not reimbursed locally" — or "No major mismatch"}

NUANCES_MISSING: {specific qualifications OE should have included, e.g.
"effect size smaller than school-age population" or "moderate vs high
confidence" — things that don't change the conclusion but matter clinically}

OVERALL_ASSESSMENT: {1-3 sentences on balance. State whether OE's conclusion
is supported, partially supported, or unsupported by current best evidence.}
```

### Step 2.5: Layer 3 — Cross-Citation Contradiction Scan

**Run only if** Layer 1 returned ≥2 papers with quantitative claims on the *same outcome*.
Otherwise skip and write "N/A — no overlapping quantitative claims" in the report.

Layers 1 and 2 both compare OE against *outside* sources. This layer looks *inside*: do the
papers OE itself cited actually agree with each other? OE synthesizes across retrieved chunks
without reconciling them, so it can cite a 2019 review reporting a 13% benefit and a 2025
meta-analysis reporting none — in the same paragraph — and simply present the friendlier number.

Spawn 1× `Agent(model: "sonnet")` with all Layer 1 `KEY_FINDINGS_FROM_SOURCE` + `PRIMARY_TRACE` blocks:

```
Below are verified findings from each paper OpenEvidence cited, plus what OE claimed.
Find places where the CITED SOURCES DISAGREE WITH EACH OTHER on the same outcome.

{for each paper: index/indices, title, year, study type, origin_risk,
 KEY_FINDINGS_FROM_SOURCE, the OE claims it backs}

## OE's claims
{the quantitative claims from OE's answer, with their [N] markers}

## Instructions
For each outcome that ≥2 papers address, compare their findings. Classify:
- CRITICAL: one source supports the effect, another explicitly finds none (or the opposite direction)
- WARNING: same direction, but magnitudes differ by >20% relative
- NOTE: findings differ but populations/timeframes/comparators plausibly explain it

Then judge OE: given the disagreement, did OE present a balanced picture, or did it
report one side as settled? Prefer the higher-quality / more recent / larger evidence
when saying which side is better supported — and say WHY, not just which.

Do not manufacture contradictions. If the cited sources are consistent, say so plainly.

## Output Format
CONTRADICTIONS_FOUND: {N}
- OUTCOME: {e.g. "stroke reduction"}
  OE_CLAIMED: {what OE said, with [N]}
  SOURCE_A: {index, finding, study type}
  SOURCE_B: {index, finding, study type}
  SEVERITY: {CRITICAL|WARNING|NOTE}
  BETTER_SUPPORTED: {which side, and why}
OE_BALANCE: {1-2 sentences — did OE acknowledge the disagreement or paper over it?}
```

### Step 3: Compile Verification Report

After all agents return, compile into a Markdown callout block:

**Verdict mapping**:
| Agent verdict | Symbol | Note |
|---------------|--------|------|
| ACCURATE | ✅ | |
| OVERSTATED | ⚠️ | Minor discrepancy |
| MISREPRESENTED | ❌ | Source doesn't support claim |
| FABRICATED | 🚫 | DOI does not resolve (Step 0.5 gate) — citation-shaped object, no source |
| TRANSITIVE | ↗️ | Number is real but belongs to a DIFFERENT paper — OE credited the review that quoted it |
| SECONDARY_UNTRACED | 🔄 | Review cited, couldn't trace primary |
| UNVERIFIABLE | ❓ | Paper not found in databases |

**Report format**:

```markdown
> [!warning] OE Verification Report
> Query: {question}
> Date: {YYYY-MM-DD} | Papers checked: {N} | Pass: {count ✅+⚠️}/{N} | Coverage: {score}%
> Provenance: {n} HIGH-risk / {n} MEDIUM / {n} LOW | Transitive: {n} | Contradictions: {n} ({c} critical)

### Citation Verification

| # | Citation | Year | Type | Corpus (risk) | OE Claim | Source Says | Verdict |
|---|----------|------|------|---------------|----------|-------------|---------|
| {index(es)} | {Author} | {year} | {study_type} | {origin} ({origin_risk}) | {claim summary} | {source summary} | {symbol} |

{For any ↗️ TRANSITIVE or 🔄 secondary sources with primary traces, add a sub-row:}
> ↳ Actually from: {Author Year} — {what the ORIGINAL found; does OE's number match?}

### Cross-Citation Contradictions

{Only if Layer 3 ran. Otherwise: "N/A — no overlapping quantitative claims".}

| Outcome | OE Claimed | Source A | Source B | Severity | Better supported |
|---------|-----------|----------|----------|----------|------------------|

> **OE balance**: {did OE acknowledge the disagreement, or present one side as settled?}

### Completeness Check

> [!info] Evidence Landscape
> - **Missing studies**: {list or "None identified"}
> - **Newest negative**: {paper or "None found"}
> - **Citation age**: median {year}, range {min}–{max} ({N}/{total} >5yr old)
> - **Heterogeneity**: {description}
> - **Applicability**: {population/setting fit vs your practice context, or "no major mismatch"}
> - **Coverage**: {X}/{Y} key papers ({%})
> - **Overall**: {assessment}
```

### Step 4: Disposition

**Standalone mode** (`/oe-verify`): Present report to user.

**Auto-trigger mode** (from a note-writing workflow):
- All ✅ or ⚠️ → proceed with normal review gating
- Any ❌ or 🚫 → escalate ALL OE-sourced content for manual review, show verification table
  (🚫 additionally: drop that citation and every claim solely backed by it)
- Coverage <50% → warn user: "OE missed significant evidence — consider manual literature search"
- Append a collapsed verification report below OE-sourced content in the note:

```markdown
> [!tip]- OE Verification ({date}) — {X}/{N} pass
> {full report table}
```

## Self-Check

Before presenting the report:
- [ ] Every checked citation has a verdict (no silent skips)
- [ ] Every DOI-bearing citation passed (or explicitly failed/skipped) the Step 0.5
      existence gate BEFORE its semantic verdict — no ✅/⚠️ without it
- [ ] Layer 2 includes an APPLICABILITY line (context fit, not just evidence balance)
- [ ] Unverifiable citations are clearly marked, not passed
- [ ] Layer 2 search covered last 5 years
- [ ] Report uses valid Markdown callout syntax
- [ ] Every HIGH/MEDIUM-origin citation with a quantitative claim got an attribution
      check — an ✅ on a scraped-review chunk without one is exactly the miss this
      skill exists to catch
- [ ] Secondary sources (🔄) and ↗️ TRANSITIVE include the primary trace
- [ ] `origin_risk: UNKNOWN` was treated as MEDIUM, never silently as LOW
- [ ] Papers holding multiple indices were verified ONCE and their verdict applied to
      every index they own (no duplicate agents, no index left without a verdict)
- [ ] Citation indices in report match the [N] markers in OE text

## Rate Limiting & Call History

OpenEvidence may rate-limit an account that fires too many queries too fast. The MCP
server enforces a built-in courtesy limiter (sliding window + parallel cap); treat its
defaults as the primary guard and stay well under OE's ~100 questions/hour account quota.

Optionally, keep an empirical call log so you converge on safe thresholds instead of
guessing them each session. A simple JSONL logger works well:

```bash
# After every OE batch (success or fail):
python scripts/oe-log.py log <skill> <qcount> <parallel> <duration_s> <outcome> [topic]
#   outcome ∈ {ok, partial, timeout, error, banned}
#   parallel ∈ {true, false}

# Before issuing a new batch, check current empirical thresholds:
python scripts/oe-log.py stats        # default last 30 days
python scripts/oe-log.py recent 10    # last 10 entries
```

(`oe-log.py` is an optional helper you provide; it is not shipped in this repo.)
A `stats` command can recommend `timeout_per_query` (= max(p95 × 1.5, 60s), capped 180s)
and `max_parallel` based on the largest parallelism that ever succeeded vs. the smallest
that failed.

### Suggested behavior

1. **Before every OE batch**: check current empirical thresholds. Use the recommended
   `timeout_per_query` × `qcount` as the wall-clock budget; use `max_parallel` as the cap.
2. **Issue the batch** — single message with all `ask_openevidence` calls when parallel;
   record `t0 = time()`.
3. **After all responses arrive** (or budget elapses): compute `duration_s = time() - t0`,
   classify outcome:
   - All queries returned non-empty → `ok`
   - Some returned empty / error → `partial`
   - Wall-clock > budget but eventually returned → `timeout` (suspected throttle)
   - Tool error / no result → `error`
   - Generic refusal / "rate limited" message → `banned`
4. **Log it** if you keep a call log.

### Heuristics for recovery

- `timeout` or `banned` once → halve parallelism on next batch, wait 5 min before retry
- `banned` twice in same hour → stop OE for the rest of the session, fall back to PubMed/Semantic Scholar
- ≥60 queries in last hour → pause the batch
- **No prior data → permissive defaults** (start wide, narrow only on observed failure):
  - `timeout_per_query = 180s`
  - `max_parallel = 15`
  - `hourly_cap = 60`
- After a batch with no failures, push the recommended `max_parallel` up to probe the real
  limit. Only narrow when you actually see a `timeout` / `banned`.

### Why this matters

Empirical thresholds beat guessing. Without logging, every session re-discovers the limits
the hard way; with logging, you converge on safe values within a few sessions and detect when
behavior shifts (provider changes rate limits, account flagged, etc.).

## Token Budget

- Layer 1: ~10 Haiku agents x ~5K tokens = ~50K Haiku tokens (dedupe by paper keeps this down)
- Layer 2: 1 Sonnet agent x ~20K tokens
- Layer 3: 1 Sonnet agent x ~15K tokens (skipped when no overlapping quantitative claims)
- Compilation: ~5K tokens
- Total: ~75-90K tokens per verification (dominated by cheap Haiku)

## Limitations

- Haiku can detect factual mismatches but may miss subtle framing bias
- Layer 2 completeness check depends on PubMed/S2 search quality
- Transitive tracing capped at 1 level (won't trace primary-of-primary)
- `origin_risk` is corpus-level, not claim-level: a LOW-risk abstract chunk can still be
  misquoted, and a HIGH-risk review chunk is often perfectly attributed. It says where to
  look hardest — it is not itself a verdict
- Papers without DOI or PMID may be unverifiable
- Citations lacking span metadata get `origin: ""` / `origin_risk: UNKNOWN` — provenance
  unknown, not provenance-safe
