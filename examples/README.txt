claims.sample.csv — reference format for --claims-file

- pmid column: PubMed pipeline (full five-state verification)
- rows with only arxiv_id (+title): arXiv pipeline (official API check)
- doi column: DOI-splice cross-check against the PubMed-registered DOI
- title: required for verdicts beyond existence-check

Run:  python3 scripts/verify_pmids.py --claims-file examples/claims.sample.csv
