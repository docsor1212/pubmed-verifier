# PubMed Citation Verifier 🔬

Batch-verify PMID citations against PubMed API. Built for researchers, medical writers, and evidence-based medicine teams.

## Why?

Academic projects routinely contain hundreds of PMID citations. Manual verification is tedious and error-prone. During our own 225-reference audit, we found 3 invalid PMIDs and 6 cross-domain mismatches — errors that would have undermined the entire project.

## Features

- **Batch verification** — Scan entire project directories, extract all PMIDs, verify against PubMed in one run
- **Mismatch detection** — Five-state verdicts catch a REAL PMID pointing to a DIFFERENT paper (the most common AI hallucination)
- **Metadata validation** — Title, authors, journal and date fuzzy-compared against your claims (DOI cross-checked via Crossref with `--verify-doi`)
- **Dual sources** — NCBI E-utilities primary, Europe PMC automatic fallback when NCBI is unreachable
- **Network hardening** — Optional NCBI API key (3x faster batches), Crossref polite pool, 429 Retry-After backoff, UA rotation, per-host circuit breaker
- **Content matching** — Keyword overlap scoring flags potentially irrelevant citations
- **Three citation types in one audit** — PMIDs, DOIs and arXiv IDs (preprints) verified in a single scan; arXiv IDs checked against the official API (nonexistent = fabrication signal)
- **Retraction detection for every citation** — the registry publication type ("Retracted Publication") flags retracted papers with no DOI or extra flags needed; Crossref `updated-by` adds the retraction-notice DOI
- **Verified-bibliography export** — `--export-bibtex` writes correct entries as BibTeX, comments partial ones, excludes and counts the rest
- **Submission-readiness verdict** — every report leads with `SUBMISSION READY` / `NOT SUBMISSION-READY` and per-problem counts
- **Audit working-paper** — `--export-audit` writes a self-contained JSON trail (tool identity, redacted invocation, evidence chain, verdict trace) a third party can replay
- **Retraction detection for every citation** — the registry publication type ("Retracted Publication") flags retracted papers with no DOI or extra flags needed; Crossref `updated-by` adds the retraction-notice DOI
- **DOI-native verification** — `--dois` verifies DOIs directly (Crossref resolve, 404 = fabrication signal on explicit input); `--source` scans auto-extract DOIs; linked back to PMIDs via Europe PMC
- **Delta audits** — `--diff previous-audit.json` reports newly retracted, degraded, improved, new and dropped citations
- **Exports** — `--export-audit` (self-contained JSON trail), `--export-bibtex` (verified bibliography), `--export-csv` (spreadsheet audit table)
- **Submission-readiness verdict** — every report leads with `SUBMISSION READY` / `NOT SUBMISSION-READY` and per-problem counts
- **Replacement search** — Find correct PMIDs for broken citations via PubMed search
- **Multiple output formats** — HTML report, JSON (with per-entry metadata origin), or terminal summary

## Quick Start

```bash
# Install
openclaw skills install docsor1212/pubmed-verifier

# Verify all PMIDs in a project
python3 scripts/verify_pmids.py --source /path/to/project --output report.html

# Verify specific PMIDs
python3 scripts/verify_pmids.py --pmids 31018962,22213727,999999999

# Institutional mode: API key + polite pool
python3 scripts/verify_pmids.py --source ./papers --verify-doi --ncbi-api-key $NCBI_API_KEY --mailto you@lab.org
```

## Use Cases

| Scenario | Example |
|----------|---------|
| **Systematic review QA** | Verify all 200+ references before submission |
| **Medical website audit** | Check evidence citations across clinical case library |
| **Paper manuscript check** | Validate every PMID in your draft |
| **Teaching material review** | Ensure lecture citations are accurate |
| **Evidence library maintenance** | Periodic batch verification of reference databases |

## Real-World Results

Audited a 35-file pediatric rheumatology evidence library (225 PMID citations):
- **222** citations: valid and content-matched ✅
- **3** citations: invalid PMIDs found and corrected
- **6** cross-domain citations: correctly flagged, reviewed, confirmed appropriate
- Total time: ~5 minutes for full audit

## Technical Details

- **APIs**: PubMed E-utilities (esummary, esearch); Europe PMC fallback; Crossref DOI check
- **Rate limit**: 3 req/s free (0.4s batch delay), 10 req/s with `--ncbi-api-key` (0.12s)
- **Resilience**: 429 Retry-After backoff, 403/406 UA rotation, per-host circuit breaker
- **Batch size**: 50 PMIDs per request
- **File types**: `.html`, `.md`, `.txt`, `.json`, `.htm`
- **PMID patterns**: `PMID: 12345678`, `PubMed: 12345678`, `pubmed.ncbi.nlm.nih.gov/12345678/`
- **Exit codes**: 0 clean / 1 problems found / 2 network-incomplete

## License

MIT-0
