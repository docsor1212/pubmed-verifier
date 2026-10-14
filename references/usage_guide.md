> **何时读我**：遇到 FAQ 之外的报错、网络/代理问题、批量提速与 CI 集成、或想深调缓存与阈值时。
> **最短路径**：先看 SKILL.md 的「Know before you run」四条限制与 Quick start——90% 的问题在那里已有单参数解法。
> **回 INDEX**：[references/INDEX.md](INDEX.md)

# Usage guide — FAQ（完整版）· 场景演练 · 最佳实践

## FAQ — full list

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

**Can I audit my .bib or .ris file directly?**
Yes — `--bibliography refs.bib` (BibTeX) and `--bibliography refs.ris`
(RIS/Zotero/EndNote/Mendeley) route each entry by PMID > DOI > arXiv
and cross-check the claimed metadata. `--lint-claims` validates either
format offline first. The round trip works both ways: `--export-bibtex`
and `--export-ris` output can be fed back after edits.

**My claims file seems to lose rows / verdicts look weaker than expected?**
Lint it offline first: `python3 scripts/verify_pmids.py --lint-claims
claims.csv` reports unusable rows, ID shape errors, unknown columns
(typo'd headers like "titel"), missing titles and DOI prefix problems —
no network, exit 1 on errors.

**Can I verify a DOI with claimed metadata (full verdict)?**
Yes — since v3.4.0 a claims row keyed by `doi` (with `title`, optionally
`authors`/`journal`/`year`) gets the same cross-check as PMID claims:
correct / mismatch / partial against the registered metadata. A DOI row
without claims stays `unknown` (existence only).

**Why did my arXiv citation drop from correct to partial?**
Your claims row paired an `arxiv_id` with a `doi`, and the DOI does not
match the version-of-record DOI registered on that arXiv entry — a typo,
or a DOI from a different paper. The registered DOI is in `details`;
fix the claim or drop the `doi` cell.

### Worked scenarios

- **"The reviewer asked how I checked my references."** — run the audit
  with `--export-audit audit.json --verify-doi`: the JSON working-paper
  contains tool identity, version, the exact (API-key-redacted) invocation,
  and the per-citation evidence chain a reviewer or editor can replay.
  Submit it alongside the manuscript.
- **"Checking the reference list of my thesis."** — copy the list into a
  plain-text file (one reference per line, or keep the `[1]` numbering) and
  run `--parse-text refs.txt`: inline PMIDs/DOIs route exactly; English
  entries resolve via PubMed title search; Chinese entries resolve via the
  OpenAlex CJK leg. Finish with
  `--format-references refs_clean.txt --citation-style gbt` for a corrected,
  uniformly formatted list.
- **"Screening a systematic review's bibliography (PRISMA)."** — verify the
  full list once, export the audit as the baseline, then re-run
  `--diff audit.json` before each resubmission: newly retracted, degraded
  and new citations are reported as deltas, so you only re-read what
  changed.

## Worked scenarios — reviewer evidence / thesis check / PRISMA


- **"The reviewer asked how I checked my references."** — run the audit
  with `--export-audit audit.json --verify-doi`: the JSON working-paper
  contains tool identity, version, the exact (API-key-redacted) invocation,
  and the per-citation evidence chain a reviewer or editor can replay.
  Submit it alongside the manuscript.
- **"Checking the reference list of my thesis."** — copy the list into a
  plain-text file (one reference per line, or keep the `[1]` numbering) and
  run `--parse-text refs.txt`: inline PMIDs/DOIs route exactly; English
  entries resolve via PubMed title search; Chinese entries resolve via the
  OpenAlex CJK leg. Finish with
  `--format-references refs_clean.txt --citation-style gbt` for a corrected,
  uniformly formatted list.
- **"Screening a systematic review's bibliography (PRISMA)."** — verify the
  full list once, export the audit as the baseline, then re-run
  `--diff audit.json` before each resubmission: newly retracted, degraded
  and new citations are reported as deltas, so you only re-read what
  changed.


## Best practices & tuning — full playbook

- **Speed up large batches** — request an NCBI API key (see
  https://ncbiinsights.ncbi.nlm.nih.gov/api-keys/): batches of 50 IDs run
  at 0.12 s spacing instead of 0.4 s; cached re-runs take seconds.
- **Parallel DOI verification** — `--workers` (default 4, cap 8) applies to
  Crossref resolution and Europe PMC linking; arXiv stays serial by
  official etiquette (≥3 s between calls).
- **Two-phase workflow** — sweep with metadata-only verification first
  (no `--verify-doi`, no `--suggest`), then deep-verify only the flagged
  subset; each deep flag adds one API call per citation.
- **Round-trip bibliographies** — `--export-bibtex` / `--export-ris`
  write verified entries; after edits, `--bibliography refs.bib` /
  `refs.ris` re-audits the file (the PMID re-links the full record).
- **Claims over context parsing** — whenever you know the expected titles,
  feed `--claims-file`: it enables the full verdict ladder and the DOI /
  arXiv pairing checks. Validate the file offline first:
  `python3 scripts/verify_pmids.py --lint-claims claims.csv` reports ID
  shape errors, missing titles, unknown columns and duplicates without any
  network access.
- **Flaky networks** — raise `--timeout`; HTTPS_PROXY/HTTP_PROXY are
  honored natively; unreachable NCBI falls back to Europe PMC
  automatically (`meta_source` shows which answered).
- **Cache policy** — results cache 30 days, negative entries 3 days;
  `--cache-days` to tune; `--no-cache` for the final pre-submission pass.
- **Scale expectations** — metadata-only throughput is API-bound
  (~1–2 min per 1000 PMIDs with an API key); DOI resolution adds one
  Crossref call per DOI. One deliberate trade-off: the verifier is a
  single stdlib-only file — copy `scripts/verify_pmids.py` anywhere with
  Python 3.9+ and it runs, no pip, no venv (that portability is why the
  code is not split into modules).
