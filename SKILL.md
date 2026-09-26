---
name: pubmed-verifier
author: DoctorQ Lab
license: MIT-0
version: 2.7.0
description: >-
  Batch-verify PMID citations against PubMed and catch the hallucination that
  existence checks miss — a REAL PMID pointing to a DIFFERENT paper (the most
  common AI-fabricated citation). Five-state citation verification (correct /
  mismatch / partial / invalid / unknown), citation-context parsing, dual
  fuzzy matching, Crossref DOI cross-check, retraction detection (RETRACTED
  papers capped at partial), correct-PMID suggestion, SQLite cache, CSV/JSON
  claims, HTML/JSON/text reports. Dual data sources with automatic Europe PMC
  fallback, optional NCBI API key (faster batches), Crossref polite pool,
  Retry-After backoff, UA rotation, host circuit breaker. Network failures are
  honestly reported as unverified, never as "not found". Zero dependencies,
  runs fully local. Triggers — verify PMIDs, check citations, validate
  references, citation audit, reference check, PMID check, audit references,
  batch verify references, AI hallucination detection, verify DOI, DOI check,
  validate citations, PubMed citation verifier.
---

# PubMed Citation Verifier v2.7.0

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
# Scan a project directory for PMIDs (parses citation context automatically)
python3 scripts/verify_pmids.py --source /path/to/project --output report.html

# Verify specific PMIDs
python3 scripts/verify_pmids.py --pmids 31018962,22213727

# Mismatch demo: PMID 34078778 is actually a dental-materials paper, so the
# JIA claims below will NOT match it — expect ⚠️ mismatch verdicts
python3 scripts/verify_pmids.py --claims '[{"pmid":"34078778","title":"JIA pathogenesis","authors":["Zaripova"],"journal":"Pediatr Rheumatol Online J","year":"2021"}]' --output report.html

# Claims from a CSV file + suggest correct PMIDs for mismatches
python3 scripts/verify_pmids.py --claims-file claims.csv --suggest --output report.html

# Crossref DOI cross-verification + audit working-paper + BibTeX + full pipeline
python3 scripts/verify_pmids.py --source /path/to/files --verify-doi --suggest --output report.html --export-audit audit.json --export-bibtex refs.bib

# Institutional niceties (recommended): NCBI API key + contact email
python3 scripts/verify_pmids.py --source . --verify-doi --ncbi-api-key $NCBI_API_KEY --mailto you@lab.org
```

## v2.2.0 — network hardening

| Feature | Flag | Effect |
|---------|------|--------|
| NCBI API key | `--ncbi-api-key` / env `NCBI_API_KEY` | Rate ceiling 3→10 req/s, batch interval 0.4s→0.12s (~3x faster) |
| Europe PMC fallback | `--meta-source auto\|ncbi\|europepmc` | NCBI batch failure automatically retries via Europe PMC (free, no key); per-entry origin in JSON (`meta_source`) |
| Crossref polite pool | `--mailto` / env `PUBMED_VERIFIER_MAILTO` | `?mailto=` on Crossref + tool/email params on NCBI — more generous limits |
| Retry-After backoff | automatic | 429 responses honored (clamped 1–5 s) instead of failing |
| UA rotation | automatic | 403/406 retried with a browser User-Agent |
| Host circuit breaker | automatic | After 2 call-level transport failures a host is skipped with an actionable message; success resets; HTTP errors never trip it |
| Honest unknown | automatic | Network failures report as ❓ unknown + exit code 2, never as "PMID not found", and are never cached |

**Exit codes:** `0` clean · `1` problems found (invalid / mismatch / retracted /
DOI-splice) · `2` could not verify (data sources unreachable) — automation can
tell "all good" from "no answer".

## v2.3.0 — retraction detection

With `--verify-doi`, each cited DOI is also checked against Crossref's
withdrawal records (`updated-by`). A paper Crossref lists as RETRACTED is:

- flagged in JSON (`retracted: true` + `retraction_note`) and in reports,
- **capped at 🔶 partial** even when every metadata field matches — citing a
  retracted paper is never "correct"; the report says *human review required*.

Corrections and other update types do not trigger the cap. Crossref outages
never flag anything (a missing check is not a retraction).

## v2.4.0 — DOI↔PMID cross-check & journal abbreviations

- **DOI splice detection**: add a `doi` field to your claims (JSON or CSV).
  The claimed DOI is compared with the DOI registered for that PMID — a
  mismatch is a splice/fabrication signal (a real DOI attached to the wrong
  paper): flagged in JSON (`doi_splice_suspect`), verdict capped at 🔶
  partial, counted in exit 1. Uses the PubMed record only — no extra API call.
- **Journal abbreviation equivalence**: journal matching now understands
  NLM-style abbreviations in both directions — "N Engl J Med" matches "New
  England Journal of Medicine", "Pediatr Rheumatol" matches "Pediatric
  Rheumatology" (in-order word prefixes, function words skipped). No more
  false "journal differs" for abbreviated citations.

Known limits: highly ambiguous abbreviations can over-match at the
journal-only level ("J Immunol" ~ "Journal of Immunology Research") — the
title remains the decisive field. A DOI-splice flag can also appear on an
otherwise-unverifiable citation (the DOI mismatch is an independent fact).

## v2.5.0 — author-name verification

- **Initials never match**: single-letter tokens ("A.", "L.") on either side
  are excluded from surname matching — an initial is not evidence, and
  substring-matching one produced false author hits.
- **Cross-language honesty**: CJK author names claimed against Latin
  registry records (or the reverse) are skipped, not counted as a mismatch —
  the report marks them `author_check: skipped` and the verdict falls back
  to what was actually comparable (title/journal/year), or to ❓ unknown
  when nothing else is checkable.

## Security & behavior declaration

- Single-run CLI: scan, verify, write the report, exit. No daemons, no
  background jobs, nothing downloaded or installed at runtime (pure standard
  library, zero dependencies).
- Network access is limited to these official academic registries, always
  over HTTPS: `eutils.ncbi.nlm.nih.gov`, `www.ebi.ac.uk` (Europe PMC),
  `api.crossref.org`. No other hosts are contacted; no telemetry, no
  analytics, no data collection — the only outbound payloads are the PMIDs,
  DOIs and titles you asked to verify.
- Your files and reports stay on your machine. Writes are limited to the
  report paths you pass and the SQLite cache under
  `~/.cache/pubmed-verifier/` (`--no-cache` to disable).
- Optional environment variables `NCBI_API_KEY` / `PUBMED_VERIFIER_MAILTO`
  authenticate or attribute your own API requests and are never sent
  anywhere else.
- No OS integration: no subprocesses, no system services, no privilege
  changes, no scheduled tasks.

## v2.6.0 — audit working-paper & report v2

- **`--export-audit audit.json`** — a self-contained JSON working-paper for
  transparent review: tool identity and version, the exact (API-key-redacted)
  invocation, per-citation evidence chains (claimed vs registered fields,
  title match scores from both algorithms, author match with cross-language
  skip records, DOI cross-check, retraction signals) and the
  verdict-ladder trace for every citation. A reviewer can replay the entire
  verification from this file alone.
- **HTML report v2** — verdict filter tabs, severity-sorted rows (retracted
  and DOI-splice first, highlighted), a field-level evidence column
  (title/author/journal/year ✓✗—) and a reproducibility footer (redacted
  command line + version + data sources).
- **Reliability** — negative cache entries now expire after 3 days (a
  legitimately new, ahead-of-print PMID is no longer reported "not found"
  for a month), and the circuit breaker self-heals: after a 30 s cooldown it
  admits one probe call and resets on success.

## v2.7.0 — retraction for every PMID, BibTeX export, readiness verdict

- **Retraction detection, source-independent** — the registry's own
  publication type ("Retracted Publication"; present in both NCBI esummary
  and Europe PMC) now flags retracted papers for EVERY citation: no DOI
  required, no `--verify-doi` required, and the flag survives the cache
  (schema v3). Crossref `updated-by` remains the detail source (the
  retraction-notice DOI) when `--verify-doi` is on. A retraction *notice*
  itself is never flagged.
- **`--export-bibtex refs.bib`** — export the verified bibliography: correct
  entries as `@article`, partial entries commented out with their divergence
  note, mismatched/invalid/unknown/retracted entries excluded and counted.
- **Submission-readiness verdict** — every report now leads with one line:
  `SUBMISSION READY` or `NOT SUBMISSION-READY — <per-problem counts>`.
- Cache schema v3 (adds a `retracted` column, auto-migrated).

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

Context parsing is heuristic — abbreviations like "U.S." can split a title
early. For precise verification, feed structured claims via `--claims-file`.

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

## Related skills (Paper Toolbox family)

- **cn-med-oa** — free Chinese medical literature full-text download & metadata
- **cite-holmes** — deep research with machine-verified citations
- **paper-polisher** — academic polishing, terminology & journal precheck
- **academic-figures** — publication-ready scientific figures in one command
- **doc-holmes** — layout-preserving PDF translation (in testing)

Workflow: cn-med-oa (get papers) → pubmed-verifier / cite-holmes (verify
citations) → paper-polisher (polish) → academic-figures (figures) →
doc-holmes (translate PDFs).

## FAQ & common mistakes

**Large batch (hundreds of PMIDs) is slow — how to speed it up?**
Metadata-only verification queries in batches of 50 with 0.4 s spacing
(0.12 s with `--ncbi-api-key`); cached re-runs are ~5 s. `--verify-doi` adds
one Crossref call *per citation* and `--suggest` adds one search *per
mismatch* — skip them for bulk sweeps, run them on the flagged subset.

**When must I use `--claims-file` instead of scanning?**
Context parsing is heuristic (abbreviations like "U.S." can split a title).
For precise verification — or DOIs in claims (splice detection needs `doi`)
— feed structured JSON/CSV claims.

**Slow or unstable network (China)?**
Standard `HTTPS_PROXY`/`HTTP_PROXY` env vars are honored natively; raise
`--timeout`; `--meta-source europepmc` routes via Europe PMC when NCBI is
unreachable (per-entry `meta_source` shows which was used); cached results
are reused for 30 days.

**❓ unknown vs ❌ invalid?**
`unknown` (exit 2) = "could not verify, sources unreachable" — retry later;
`invalid` (exit 1) = "verified not-found". Network failures are never
reported as not-found and never cached.

**What does RETRACTED mean in a report?**
The registry itself lists the paper's publication type as "Retracted
Publication" (checked for every citation since v2.7.0 — no DOI or flags
needed), and/or Crossref records a retraction. The verdict is capped at
partial and a human review note is attached — citing it would propagate
withdrawn science. A retraction *notice* is never flagged; papers under
*Expression of Concern* (an editorial note, not a retraction) are not
flagged either. Retraction status reflects the registry at cache time — for
a final pre-submission check, run with `--no-cache`.

**Mismatch reported but the title looks similar?**
Check `details` for which field diverged; thresholds are strict on purpose.
Feed the full citation via `--claims-file` for a precise verdict.

## Files

| File | Purpose |
|------|---------|
| `scripts/verify_pmids.py` | Main verifier (v2.7.0, stdlib-only) |
| `references/api_examples.md` | PubMed / Europe PMC / Crossref API notes |
| `tests/` | Offline matrix + real-network acceptance (repo only, not in the package) |

## License

MIT-0 — free to use, modify and redistribute, no attribution required.
