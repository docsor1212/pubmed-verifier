claims.sample.csv — reference format for --claims-file

- pmid column: PubMed pipeline (full five-state verification)
- rows with only arxiv_id (+title): arXiv pipeline (official API check)
- doi column: DOI-splice cross-check against the PubMed-registered DOI
  (PMID rows) / version-of-record pairing check (arXiv rows, v3.3.0)
- title: required for verdicts beyond existence-check
- note: the sample row for PMID 24476887 (STAP cells) is RETRACTED on
  purpose — expect a RETRACTED flag and a partial cap for it; it
  demonstrates retraction detection, not a data error

Run:  python3 scripts/verify_pmids.py --claims-file examples/claims.sample.csv
