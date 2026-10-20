---
name: pubmed-verifier
author: DoctorQ Lab
license: MIT-0
version: 5.3.0
description: >-
  Reference checker for AI-fabricated citations: batch-verify PMIDs against
  PubMed and catch the hallucination existence checks miss — a REAL PMID
  pointing to a DIFFERENT paper. Five-state citation verification (correct /
  mismatch / partial / invalid / unknown), citation-context parsing, Crossref
  DOI cross-check, retraction detection (capped at partial), correct-PMID
  suggestion, arXiv ID verification, plain-text reference list parsing,
  formatted reference lists (GB/T 7714 / Vancouver / APA / AMA), OpenAlex DOI
  fallback for non-Crossref registries, Chinese-reference title search,
  CSV/JSON claims, HTML/JSON/text reports. Four data sources: NCBI, Europe PMC
  fallback, Crossref, OpenAlex. Network failures are honestly reported as
  unverified, never as "not found". Zero dependencies, runs fully local.
  Triggers: verify PMIDs, check citations, citation audit, reference check,
  citation check, batch verify references, AI hallucination detection, verify DOI,
  DOI check, citation formatting, reference formatter, GB/T 7714.
---

# PubMed Citation Verifier v5.3.0

Batch verification of PMID citations via the PubMed E-utilities API. Not just
"does this PMID exist" — **does this PMID point to the paper you claim?**
Zero dependencies, pure standard library, fully local.

## When to use this skill

Invoke it whenever citation truth matters:

- "Verify / check these PMIDs / this reference list" (before submission or release)
- Auditing citations in AI-generated text (fabricated or mismatched references)
- Spot-checking a systematic review's bibliography
- "Does PMID 12345678 really say X?" — point-of-doubt verification
- Batch QA of a knowledge base's PMID/DOI citations
- Automatic invocation: trigger this skill whenever the user asks to
  "verify/check/audit" citations, PMIDs, DOIs, arXiv preprints or
  references — no flags needed for a first pass; add `--claims-file`
  when they supply the expected titles.

**Trigger priority & tool choice** — explicit "verify / check / audit
citations, references, PMIDs, DOIs" requests invoke this skill first.
cite-holmes is for deep research with machine-verified citations; when a
request mixes research and verification, run the research first, then
this tool for the final reference audit.

## Trigger words

Use this skill when the user's request matches any of: **verify PMIDs**,
**check citations**, **validate references**, **citation audit**,
**reference check**, **batch verify references**, **AI hallucination
detection**, **verify DOI**, **DOI check**, **citation formatting**,
**reference formatter**, **GB/T 7714**, 中文文献验证 / 参考文献核查 /
文献排版. A pasted reference list, a `.bib`/`.ris` file, a Word `.docx`
document, a PowerPoint `.pptx` deck, an exported `.html` page, or a
claims CSV are all direct inputs.

Three quick mappings: "帮我验证这几个 PMID 31018962、22213727" →
`--pmids`; "检查我论文参考文献" + a pasted list → `--parse-text` (or pipe
it to `--stdin` — **a pasted list IS the input, no explicit "verify"
word needed**); "这篇撤稿了该引什么" → `--suggest` on a retracted entry.

**When to invoke — by pattern, not just by keyword:**
- MUST invoke: any request containing citation identifiers (PMID/DOI/
  arXiv ID) together with words like check/verify/audit/validate — even
  without the word "verify" ("are these PMIDs real?", "is this DOI
  right?");
- MUST invoke: a reference list is pasted or attached, whatever the
  framing ("look at my references", "any problems here?") — the list IS
  the input (`--parse-text`/`--stdin`);
- MUST NOT invoke alone: "write me a paper/lit review" (research first —
  cite-holmes; run this skill on the DRAFT's reference list at the end);
- MUST NOT invoke for: plain formatting of UNVERIFIED citations without
  verification intent — formatting stands on verified records
  (`--format-references` verifies first by design).

**Combination runs** (flags stack; each does one job):

| Scenario | Command shape |
|---|---|
| Pre-submission final pass | `--source DRAFT --quick --output report.html` |
| Paste-and-verify from clipboard | `pbpaste \| ... --stdin --suggest` |
| Knowledge-base audit + evidence pack | `--source KB --report-bundle DIR` |
| Bibliography round-trip | `--bibliography refs.bib --export-bibtex out.bib` |
| Delta audit vs last month | `--source KB --diff old-audit.json` |
| CJK-only reference list | `--parse-text refs-cn.txt` (OpenAlex CJK leg) |

**When `--claims-file` is required:** a plain scan verifies existence plus
whatever metadata the citation context carries — if a verdict will be
acted on (mismatch/partial before submission), feed the expected
title+authors via `--claims-file` for the decisive five-state verdict.
**Tool boundary:** cite-holmes covers deep research with machine-verified
citations; this skill is the verification/formatting pass — when a request
mixes both, research first, then audit the final reference list here.

## Use from Python

The whole tool is one stdlib module — import it without installing
anything (full reference: `references/python_api.md`):

```python
import sys
sys.path.insert(0, "/path/to/pubmed-verifier/scripts")
import verify_pmids as vp

info = vp.fetch_summaries(["31018962"], batch_size=1)["31018962"]
cross = vp.cross_check_citation(
    {"claimed_title": "Classification criteria for autoinflammatory recurrent fevers",
     "claimed_year": "2019"},
    {"title": info["title"], "authors": info["authors"],
     "journal": info["journal"], "pubdate": info["pubdate"]})
print(cross["verdict"], round(cross["confidence"], 2))
```

## Know before you run

Four limits cover 95% of surprises — each has a one-flag workaround:

| Limit | Workaround |
|---|---|
| Citation-context parsing is heuristic (exotic formatting can mis-split) | `--claims-file` for exact metadata |
| Chinese references without a PMID/DOI marker parse weakly | `--parse-text` (OpenAlex CJK leg) or supply an ID |
| Network failures stay `unknown` — never silently "not found" | `--check-net` first, retry when reachable |
| Retraction/DOI status is as of cache time (30 days) | `--no-cache` for the final pre-submission pass |

Everything else below expands on these.

**Documentation map** — what lives where, so nothing needs hunting:

| If you want... | Go to |
|---|---|
| The full version history | `references/CHANGELOG.md` (main doc keeps the recent highlights) |
| Every FAQ, scenario walkthrough and tuning knob | `references/usage_guide.md` |
| The Python API reference / runnable examples | `references/python_api.md` / `references/api_examples.md` |
| Which file to open for which task | `references/INDEX.md` |

## The five-state verdict

| Verdict | Meaning |
|---------|---------|
| ✅ Correct | PMID exists AND matches the claimed paper |
| ⚠️ Mismatch | PMID exists but points to a **different** paper (the most common AI hallucination!) |
| 🔶 Partial | Some metadata matches (e.g. author+journal but title differs) |
| ❌ Invalid | PMID does not exist in PubMed |
| ❓ Unknown | Not enough claimed metadata to cross-check — or both data sources unreachable (never misreported as invalid) |

**Why existence checks are not enough:** a large share of fabricated
citations use REAL PMIDs that point to a different paper from the same
year/journal/field — in one of our own audits, 4 of 5 "valid" PMIDs were
wrong this way. A binary exists/not-exists check misses them all.

## Quick start

```bash
# Pre-submission final pass in ONE command (v5.0.0): fresh registries
# (--no-cache) + Crossref DOI cross-check (--verify-doi) + correct-PMID
# suggestions (--suggest), no flags to remember
python3 scripts/verify_pmids.py --source /path/to/draft --quick --output report.html

# Environment self-check (v5.3.0): versions, cache state, key/proxy
# config — add --check-net to include the five-registry probe
python3 scripts/verify_pmids.py --doctor

# Paste-and-verify (v5.0.0): a copied reference list straight from the
# clipboard or another command — no temp file needed
pbpaste | python3 scripts/verify_pmids.py --stdin
git show HEAD:refs.txt | python3 scripts/verify_pmids.py --stdin

# Scan a project directory for PMIDs (parses citation context automatically)
python3 scripts/verify_pmids.py --source /path/to/project --output report.html

# Word documents and exported HTML pages are read directly (v4.8.0):
# .docx paragraphs/tables and .html text are extracted in-memory —
# script/style content is ignored, link targets (href) are kept
python3 scripts/verify_pmids.py --source guideline_review.docx --output report.html

# PowerPoint decks too (v4.9.0): every slide in order plus the speaker
# notes (.pptx, a:t text) — patient-education courseware audits directly
python3 scripts/verify_pmids.py --source lecture_deck.pptx --output report.html

# Verify specific PMIDs
python3 scripts/verify_pmids.py --pmids 31018962,22213727

# Mismatch demo: PMID 34078778 is actually a dental-materials paper, so the
# JIA claims below will NOT match it — expect ⚠️ mismatch verdicts
python3 scripts/verify_pmids.py --claims '[{"pmid":"34078778","title":"JIA pathogenesis","authors":["Zaripova"],"journal":"Pediatr Rheumatol Online J","year":"2021"}]' --output report.html

# Claims from a CSV file + suggest correct PMIDs for mismatches
python3 scripts/verify_pmids.py --claims-file claims.csv --suggest --output report.html

# Crossref DOI cross-verification + audit working-paper + BibTeX + full pipeline
python3 scripts/verify_pmids.py --source /path/to/files --verify-doi --suggest --output report.html --export-audit audit.json --export-bibtex refs.bib

# Verify DOIs directly (no PMIDs) + delta audit vs a previous run
python3 scripts/verify_pmids.py --dois "10.1038/nature12968,10.4012/dmj.2020-408" --workers 4 --export-audit audit.json --export-csv table.csv
python3 scripts/verify_pmids.py --source /path/to/project --diff audit.json --output report.html

# Verify arXiv IDs (preprints) — mixed audits supported
python3 scripts/verify_pmids.py --arxivs "2401.12345,cs/0211004" --no-cache

# Pre-flight: are the five data sources reachable right now?
python3 scripts/verify_pmids.py --check-net

# Audit a BibTeX or RIS bibliography file directly (PMID > DOI > arXiv routing)
python3 scripts/verify_pmids.py --bibliography refs.bib --no-cache
python3 scripts/verify_pmids.py --bibliography refs.ris --no-cache --export-ris verified.ris

# Verify a reference list copied from a paper draft (inline IDs route
# exactly; title-only entries resolve via PubMed title search)
python3 scripts/verify_pmids.py --parse-text references.txt --no-cache

# Format the verified entries as a ready-to-paste reference list
# (--citation-style: gbt | vancouver | apa | ama; default gbt)
python3 scripts/verify_pmids.py --parse-text references.txt --format-references refs_gbt.txt --citation-style gbt

# Stream results for agent pipelines (v5.3.0): one citation per line
python3 scripts/verify_pmids.py --parse-text refs.txt --jsonl results.jsonl

# Submission evidence pack: one command, every artifact (audit + HTML +
# BibTeX/RIS + CSV + GB/T list + manifest)
python3 scripts/verify_pmids.py --parse-text references.txt --report-bundle evidence_pack

# Turn a copied reference list into a claims CSV skeleton (fill the gaps,
# then run --claims-file for exact five-state verification)
python3 scripts/verify_pmids.py --claims-template references.txt,claims_skeleton.csv

# Institutional niceties (recommended): NCBI API key + contact email
python3 scripts/verify_pmids.py --source . --verify-doi --ncbi-api-key $NCBI_API_KEY --mailto you@lab.org
```

## What the output looks like

Terminal text report (verdict line, then per-citation evidence):

```
Readiness: NOT SUBMISSION-READY — 1 invalid, 1 mismatched
Results: 1/3 correct, 1 mismatch, 1 invalid, 0 partial, 0 unknown
============================================================

✅ PMID 31018962 (claims) [correct]
   Actual: Classification criteria for autoinflammatory recurrent fevers
   Claimed: Classification criteria for autoinflammatory recurrent fevers
   Evidence: title ✓ · author ✓ · journal ✓ · year ✓

⚠️ PMID 34078778 (claims) [mismatch]
   Actual: Effect of CAD/CAM materials on the marginal fit of crowds
   Claimed: JIA pathogenesis and treatment
   → Suggest: PMID 34425842 - Juvenile idiopathic arthritis: from aetio...
   Evidence: title ✗ · author ✗ · journal ✗ · year —

❌ PMID 99999999 (cli) [invalid]
   Error: PMID not found in API response
```

`--output report.html` renders a self-contained dashboard (inline CSS,
no external assets): readiness banner, a filterable verdict table
(✅/⚠️/🔶/❌/❓ tabs with per-field ✓✗— evidence columns, retraction and
DOI-splice flags highlighted on top), a delta panel when `--diff` is
given, the finding ledger, and a reproducibility footer (redacted
command + version + data sources). Open it in any browser — it is also
what `--report-bundle` packs for reviewers.

`--output report.json` carries the same verdicts plus the full evidence
chain per citation (`evidence.title_match / author_match / journal_match /
year_match`, `fields` tri-state, `meta_source` naming which registry
answered, `resolved_by` when the title-search/OpenAlex legs matched) and a
`stats` block with the submission-readiness verdict. `--format-references`
writes the ready-to-paste citation list (see the v4.0.0 entry in references/CHANGELOG.md).

## FAQ & common mistakes

**Top 10 things NOT to do** (each is detailed below or in Anti-patterns):

| # | Don't | Do instead |
|---|-------|------------|
| 1 | Treat `--pmids` existence output as "verified" | Feed `--claims-file` with titles for real verification |
| 2 | Submit claims without `title` | Always include titles — the verdict caps at partial without one |
| 3 | Trust cached verdicts on publication day | Final check with `--no-cache` |
| 4 | Read "not found" as "fabricated" for auto-extracted DOIs | Check doi.org / arxiv.org by hand first |
| 5 | Treat the leading `'` in CSV cells as corruption | It is the formula-injection guard — strip after import |
| 6 | Read the READY line as a quality score | It means "no problems among the checks that ran" |
| 7 | Pass `--source` together with `--pmids` | `--source` is ignored entirely when `--pmids` is given |
| 8 | Deep-verify (`--verify-doi` / `--suggest`) a thousand-entry sweep | Sweep first, deep-verify the flagged subset |
| 9 | Expect author matching across CJK↔Latin names | They are skipped honestly (`author_check: skipped`) |
| 10 | Ship a reference list without the audit trail | `--export-audit` writes a replayable working paper |

**Large batch (hundreds of PMIDs) is slow — how to speed it up?**
Metadata-only verification queries in batches of 50 with 0.4 s spacing
(0.12 s with `--ncbi-api-key`); cached re-runs are ~5 s. `--verify-doi` adds
one Crossref call *per citation* and `--suggest` adds one search *per
mismatch* — skip them for bulk sweeps, run them on the flagged subset.

**When must I use `--claims-file` instead of scanning?**
Context parsing is heuristic (v3.3.0 guards common abbreviations, exotic formatting can still mis-split).
For precise verification — or DOIs in claims (splice detection needs `doi`)
— feed structured JSON/CSV claims.

**My citation text is in Chinese — the scan parses little?**
GB/T 7714-style references (……标题[J]. 刊名, 年… PMID: xxx) parse since
v3.9.0 — title/journal comparisons against Latin registries are skipped
cross-language, so verdicts rest on year/DOI evidence. Free Chinese prose
without a PMID marker still yields little — feed `--claims-file` for full
verdicts regardless of language.

**Slow or unstable network (China)?**
Standard `HTTPS_PROXY`/`HTTP_PROXY` env vars are honored natively; raise
`--timeout`; `--meta-source europepmc` routes via Europe PMC when NCBI is
unreachable (per-entry `meta_source` shows which was used); cached results
are reused for 30 days.

More FAQs, the three worked scenarios and the complete tuning playbook live in [references/usage_guide.md](references/usage_guide.md).

## Anti-patterns — things done WRONG

Each entry: the mistake → why it fails → the right way.

1. **Treating `--pmids` output as "fully verified"** — existence-only.
   → Wrong: "all 5 PMIDs exist, so the citations are correct."
   → Right: existence-checked only; feed `--claims-file` with titles for
   real verification (the READY line says so explicitly).
2. **Claims without `title`** — author/journal/year alone can never reach
   `correct`; the report caps at `partial`. → Always include titles.
3. **Trusting a cached verdict right after publication day** — a brand-new
   PMID may have been cached as not-found by an earlier run, and retraction
   status is as of cache time. → Final pre-submission check: `--no-cache`.
4. **Assuming "not found" always means fabricated** — a Crossref 404 is
   re-checked at OpenAlex (DataCite/Zenodo/Chinese-registry DOIs live
   there), so "unknown in both registries" is now the bar; auto-extracted
   DOIs that 404 in both stay *suspects*. arXiv IDs removed by moderators
   also return empty. → Check doi.org / arxiv.org by hand before accusing.
5. **Copying the leading `'` from CSV cells** — that apostrophe is the
   formula-injection guard, not data corruption. → Strip it after import.
6. **Reading the READY line as a quality score** — it only means "no
   problems found among the checks that ran", not "this paper is good".

## Boundaries — declared limits

The capability×limit pairs — what each promise covers, where it stops, and
the one-flag workaround:

| Capability | What it does | Where it stops | Workaround |
|---|---|---|---|
| Context parsing | Splits reference text into claimed metadata; common abbreviations (U.S., e.g., vs.) protected | Exotic formatting can still mis-split; CJK without a PMID/DOI marker parses weakly | `--claims-file` for exact metadata; `--parse-text` adds the OpenAlex CJK leg |
| Five-state verdicts | correct / mismatch / partial / invalid / unknown with per-field evidence | Heuristic — a verdict is evidence-weighted, never absolute proof | Audit trail (`--export-audit`) keeps the chain replayable |
| Retraction detection | Registry pubtype + Crossref `updated-by`, capped at partial | Status is as of cache time (30 days) | `--no-cache` for the final pass |
| DOI verification | Crossref first, OpenAlex fallback for non-Crossref registries | DOIs absent from BOTH registries stay suspect/invalid by the 404 rule | Cross-check at doi.org before accusing |
| Title-search legs (Latin + CJK) | Markerless entries resolved by PubMed/OpenAlex title search, honestly labeled | Never a mismatch source; no hit = unknown | Supply a PMID/DOI for a decisive verdict |
| Second-opinion leg (v4.8.0) | A gated title-only claim is corroborated via PubMed title search (correct, labeled) or flagged as a wrong-PMID signal | Confirms or reports only — never manufactures a mismatch; CJK titles skip (OpenAlex CJK leg owns those) | `--claims-file` for the decisive verdict |
| Word/HTML/PowerPoint input (v4.8.0/v4.9.0) | `.docx` paragraphs/tables, `.pptx` slides+speaker notes, and `.html` text are extracted in-memory; `href` link targets kept, script/style dropped | A damaged office file falls back to a raw read (scans bytes, finds little); scanned PDFs/images are NOT text | Export to text/HTML first for PDFs |
| Verdict-ladder monotonicity (v4.9.0) | A scanned citation whose ONLY gap is the missing author claim (title matched, journal/year never claimed) grades correct with `route: existence_title_match` — stronger evidence than a bare existence check can never grade lower | Any claimed-but-differing author/journal/year keeps partial; explicit `--claims-file`/CLI inputs always get the full verdict | `--claims-file` with authors for the complete five-state pass |
| Retraction reasons (v5.3.0) | A retracted citation carries `retraction_reason` — the notice abstract when the registry has one, else the notice title — plus `retraction_reason_doi` for the source statement | Many notices are just "Retraction Note: <original title>"; the original title alone NEVER masquerades as the reason — the notice is cited instead | Read `--export-audit` for the full chain |
| Input scale & concurrency (v5.1.0/v5.2.0) | Any input size; the `--max-entries` guard (default 2000) counts the de-duplicated workload and warns with a split recommendation; DOI verification runs 4 workers; PMID batches go 50/call (0.4 s spacing, 0.12 s with an API key) | arXiv is throttled to 1 call/3 s BY THE API (1,000 arXiv IDs ≈ 50+ minutes no matter what); citation counts (`cited_by_count`) only exist on the Europe PMC/OpenAlex paths — the NCBI path reports none | `--max-entries N` raises the threshold, `0` silences it; split very large scans by section |
| Explicit precedence (v5.1.0) | `--pmids` outranks `--source` when both are given | The ignored input is announced on stderr, never silently dropped | Give only the input you mean |
| Formatted reference lists | GB/T (numeric + author-date), Vancouver, APA, AMA — verified entries only | Registry-truncated author lists close with et-al, not invented names | Verify first, then format (`--format-references`) |
| Network honesty | Failures are unknown + retry hints, never "not found"; nothing cached on failure | Cannot verify anything while registries are unreachable | `--check-net` first |

## Claims reference format

`--claims-file` accepts JSON (an array of objects) or CSV. Recognized
columns: `pmid`, `title`, `authors` (semicolon/pipe-separated),
`journal`, `year`, `doi`, `arxiv_id`. A row needs one of `pmid`,
`arxiv_id` or `doi`; a missing `title` caps the verdict at partial.

```csv
pmid,title,authors,journal,year,doi,arxiv_id
31018962,Candidate criteria for diagnosis of familial...,Gattorno,Ann Rheum Dis,2019,10.1136/annrheumdis-2019-215048,
,Attention Is All You Need,Vaswani,NeurIPS,2017,,1706.03762
,City size and the spreading of COVID-19 in Brazil,Silva Junior;Other,PLOS ONE,2020,10.1371/journal.pone.0239699,
```

Validate any file offline first: `--lint-claims file.csv` reports
unusable rows, ID shape errors, unknown columns, duplicates and missing
titles (no network). Lint wins when combined with verification flags —
only the lint runs.


## Using it from mainland China — the one page

Everything network-related in one place (each item also lives where it is used):

- **Pre-flight**: `--check-net` probes all four registries + arXiv (5 s each) and prints per-host reachability with next steps.
- **Registry choice**: default `--meta-source auto` already retries a failed NCBI batch against Europe PMC (no key, generally reachable); `--meta-source europepmc` forces it.
- **Proxy**: standard `HTTPS_PROXY`/`HTTP_PROXY` env vars are honored on every request; direct-mode automation should `unset` them (a dead proxy env otherwise masks as unreachable registries).
- **Package mirror**: ModelScope carries the skill package (`modelscope.cn/skills/Docsor/pubmed-verifier`) — but verification itself still calls the academic registries at runtime; the mirror only removes the install friction.
- **Honest failure**: whatever the network does, failures grade `unknown` (exit 2), never "not found", and are never cached.

## Best practices & tuning

Full playbook: [references/usage_guide.md](references/usage_guide.md) (caching, batching, proxies, CI usage).

## Recent releases (full history in references/CHANGELOG.md)

- **v5.3.0** — retraction reasons distilled from the notice record
  (abstract > semantic title; the original title never masquerades as a
  reason); `--doctor` environment self-check; `--jsonl` stream output;
  HTML rows expand the full evidence trail inline.
- **v5.2.0** — `--max-entries` quantified input guard (the de-duplicated
  workload counted against a threshold, with a split recommendation);
  `cited_by_count` rides Europe PMC/OpenAlex responses (zero extra
  requests); trigger section described by pattern with a
  combination-run table.
- **v5.1.0** — structured retraction detail (`retraction_type` across
  all three registries; removal newly detected and capped); explicit
  `--pmids`/`--source` precedence warning; mainland-China usage guide
  one-pager; boundary table gains scale/concurrency rows.
- **v5.0.0** — paste-and-verify (`--stdin` reads a copied reference list
  straight from the pipe) and `--quick`, the pre-submission final pass in
  one flag (--no-cache + --verify-doi + --suggest); FAQ depth (a
  format-specific "when parsing fails" table); main doc slimmed to ~450
  lines.
- **v4.9.0** — PowerPoint `.pptx` decks are read directly (slides in
  order + speaker notes); verdict-ladder monotonicity: a scanned
  citation whose only gap is the missing author claim (title matched,
  journal/year never claimed) grades correct with the evidence route
  labeled — stronger evidence can no longer grade below a bare
  existence check.
- **v4.8.0** — Word `.docx` and `.html` inputs are read directly by
  `--source`/`--parse-text` (in-memory extraction; link targets kept,
  script/style dropped); title-only citations get a second opinion via
  PubMed title search — same-PMID corroboration upgrades to correct
  (honestly labeled), a different-record hit surfaces a wrong-PMID
  signal, and the leg never manufactures a mismatch.
- Versions **v4.7.0 and older** — full entries in [references/CHANGELOG.md](references/CHANGELOG.md).

## Safety & behavior statement

- Single-run CLI: scan → verify → write report → exit. No daemon, no
  background task, zero runtime downloads/installs (pure standard
  library, zero dependencies).
- Network access is limited to these official academic registries, all
  HTTPS: `eutils.ncbi.nlm.nih.gov`, `www.ebi.ac.uk` (Europe PMC),
  `api.crossref.org`, `api.openalex.org`, `export.arxiv.org` — no other
  hosts; no telemetry, no stats, no data collection — outbound payloads
  are only the PMID/DOI/titles you asked to verify.
- Your files and reports stay local. Writes go only to the report paths
  you specify and the SQLite cache under `~/.cache/pubmed-verifier/`
  (disable with `--no-cache`).
- The optional `NCBI_API_KEY` / `PUBMED_VERIFIER_MAILTO` env vars
  authenticate/sign YOUR API requests only — they go nowhere else.
- No OS integration: no subprocesses, no services, no permission
  changes, no scheduled tasks.

## How it works

1. **Extract + parse context** — finds `PMID: 12345678` / PubMed URLs in
   `.html .md .txt .htm .json`, and parses the surrounding reference into
   claimed authors / title / journal / year.
2. **Fetch metadata (cached)** — PubMed esummary in batches of 50, SQLite
   cache (30 days, `--cache-days`), 3 retries with backoff.
   Europe PMC steps in per failed batch when NCBI is unreachable.
3. **Cross-check claimed vs actual** — dual fuzzy matching: word-level
   Jaccard overlap ≥ 50% OR SequenceMatcher ≥ 90% on titles; author surname
   hits; journal containment or NLM abbreviation equivalence; exact year.
4. **DOI↔PMID cross-check** (automatic when claims include `doi`) — a claimed
   DOI differing from the PMID's registered DOI is a splice/fabrication
   signal (capped at partial).
5. **Crossref DOI verification** (optional `--verify-doi`) — resolves each
   cited DOI via Crossref, compares the registered title with the PubMed
   record (`doi_title_match` in JSON), and detects RETRACTED papers (verdict
   capped at partial). A `doi_verified: false` with note
   "crossref unreachable" is a network fact, not a verdict.
6. **Suggest the right PMID** (optional `--suggest`) — for mismatches,
   searches PubMed with the claimed metadata and proposes top-3 candidates.
   (Suggestion search always uses NCBI, even with `--meta-source europepmc`.)

Context parsing is heuristic — since v3.3.0, common abbreviations
("U.S.", "e.g.", "vs.") no longer split a title, but exotic formatting
still can. For precise verification, feed structured claims via
`--claims-file`.

| Report | Flag | Use |
|--------|------|-----|
| HTML | `--output report.html` | Human review: claimed vs actual side by side |
| JSON | `--output report.json` | Programmatic processing (includes `meta_source` per entry) |
| Text | default | Quick terminal look |

## Performance

Measured on a 225-PMID audit (5 esummary batches): metadata-only verification
runs in seconds; cached re-runs take ~5 s. Each batch waits 0.4 s between
calls (0.12 s with `--ncbi-api-key`). Optional extras are per-citation:
`--verify-doi` adds one Crossref call (~0.5–1 s) per cited DOI, and
`--suggest` adds one PubMed search per mismatch.

## When to use which tool

- **pubmed-verifier (this skill)** — fast, batch, targeted: I have a list of
  PMIDs/DOIs and need to know if they are real and correctly cited.
- **cite-holmes** — deep research: interrogate every citation of a whole
  document across multiple databases, with graded confidence reports.

They share the same five-state philosophy and are safe to use together.

## Use cases

- Systematic review / meta-analysis reference audits
- Verifying citations in AI-generated content
- Pre-submission self-check of a manuscript's reference list
- Medical knowledge base / teaching material QA
- Pharmacovigilance literature verification

## Related tools

Each tool solves one step of reference work; use whichever fits the task.

- **cn-med-oa** — free Chinese medical literature full-text download & metadata
- **cite-holmes** — deep research with machine-verified citations
- **paper-polisher-pro** — academic polishing, terminology & journal precheck
- **academic-figures** — publication-ready scientific figures in one command
- **doc-holmes** — layout-preserving PDF translation (in testing)

Typical order: get papers (cn-med-oa), verify citations (this tool or
cite-holmes), polish (paper-polisher-pro), make figures (academic-figures),
translate PDFs (doc-holmes) — pick whichever step you need.

## Files

| File | Purpose |
|------|---------|
| `scripts/verify_pmids.py` | Main verifier (v5.3.0, stdlib-only) |
| `references/api_examples.md` | PubMed / Europe PMC / Crossref / arXiv API notes |
| `references/usage_guide.md` | full FAQ, worked scenarios, tuning playbook |
| `references/INDEX.md` | 任务→文档 30 秒路由表 |
| `references/python_api.md` | Calling the verifier from Python (stable surfaces + examples) |
| `examples/claims.sample.csv` | Reference format for `--claims-file` (incl. a DOI-only row) |
| `examples/refs.sample.bib` | Sample bibliography for `--bibliography` (DOI/PMID/arXiv routing) |
| `examples/refs.sample.ris` | Sample RIS bibliography (Zotero/EndNote/Mendeley) |
| `tests/` | Offline matrix + real-network acceptance (repo only, not in the package) |

## License

MIT-0 — free to use, modify and redistribute, no attribution required.
