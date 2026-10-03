#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Real-network acceptance for pubmed-verifier (repo-level, run where internet works).

Run:  python3 tests/acceptance_realnet.py
Verdict per case: PASS / FAIL / SKIP(environment). Prints a 汇总 line;
exit 0 only when no FAIL. Uses real APIs (NCBI, Europe PMC, Crossref).
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "verify_pmids.py"
GOOD_PMIDS = ["34078778", "31018962", "22213727"]
BOGUS_PMID = "99999999"          # > current PMID space; guaranteed nonexistent
RESULTS = []


def record(cid, ok, note=""):
    RESULTS.append({"case": cid, "ok": ok, "note": note})
    print(("  [PASS] " if ok else "  [FAIL] ") + cid + (f" | {note}" if note else ""))


def run_cli(args):
    """Run the CLI; return (returncode, stdout, json_results_or_None)."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        out_path = tf.name
    try:
        p = subprocess.run([sys.executable, str(SCRIPT), *args, "--output", out_path],
                           capture_output=True, text=True, timeout=240)
        data = json.loads(Path(out_path).read_text(encoding="utf-8")) if Path(out_path).exists() else None
        return p.returncode, p.stdout + p.stderr, data
    finally:
        try:
            Path(out_path).unlink()
        except OSError:
            pass


def fetch_real(pmid):
    """Ground truth straight from NCBI esummary via the script's own function."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import verify_pmids as vp
    return vp.fetch_summaries([pmid], batch_size=1)[pmid]


print("== pubmed-verifier real-network acceptance ==")

# ── T0: ground truth reachable ──
truth = {}
try:
    for pmid in GOOD_PMIDS:
        info = fetch_real(pmid)
        if not info.get("valid"):
            raise RuntimeError(f"esummary invalid for {pmid}: {info}")
        truth[pmid] = info
    record("T0 ground truth (NCBI esummary reachable)", True, f"{len(truth)} PMIDs")
except Exception as e:
    record("T0 ground truth (NCBI esummary reachable)", False, str(e)[:120])
    print("汇总: environment unreachable — remaining cases SKIP")
    sys.exit(3)

# ── T1: correct verdict on real claims ──
claims = [{"pmid": p, "title": truth[p]["title"], "authors": [a.split()[0] for a in truth[p]["authors"][:2]],
           "journal": truth[p]["journal"], "year": truth[p]["pubdate"][:4]} for p in GOOD_PMIDS]
rc, so, data = run_cli(["--claims", json.dumps(claims), "--no-cache"])
v = {r["pmid"]: r["verdict"] for r in (data or {}).get("results", [])}
record("T1 correct verdict (real claims)",
       rc == 0 and all(v.get(p) == "correct" for p in GOOD_PMIDS),
       f"rc={rc} verdicts={v}")

# ── T2: mismatch detection (PMID A claimed as paper B — the hallucination shape) ──
a, b = GOOD_PMIDS[0], GOOD_PMIDS[1]
swapped = [{"pmid": a, "title": truth[b]["title"], "authors": [truth[b]["authors"][0].split()[0]],
            "journal": truth[b]["journal"], "year": truth[b]["pubdate"][:4]}]
rc, so, data = run_cli(["--claims", json.dumps(swapped), "--no-cache"])
v = {r["pmid"]: r["verdict"] for r in (data or {}).get("results", [])}
record("T2 mismatch detection (real PMID, wrong paper)",
       v.get(a) in ("mismatch", "partial"), f"verdict={v.get(a)}")

# ── T3: bogus PMID → invalid + exit 1 ──
rc, so, data = run_cli(["--pmids", BOGUS_PMID, "--no-cache"])
v = [(r["pmid"], r["verdict"]) for r in (data or {}).get("results", [])]
record("T3 invalid detection + exit code", rc == 1 and v and v[0][1] == "invalid",
       f"rc={rc} {v}")

# ── T4: Crossref DOI verification via polite pool (fetch + title compare) ──
rc, so, data = run_cli(["--pmids", GOOD_PMIDS[0], "--verify-doi",
                        "--mailto", "acceptance@example.org", "--no-cache"])
r = ((data or {}).get("results") or [{}])[0]
record("T4 Crossref DOI verify (polite pool)",
       rc in (0, 1) and r.get("doi_verified") is True and "doi_title_match" in r,
       f"doi={r.get('doi','')[:30]} verified={r.get('doi_verified')} match={r.get('doi_title_match')}")

# ── T5: forced Europe PMC path end-to-end ──
rc, so, data = run_cli(["--claims", json.dumps(claims[:1]), "--meta-source", "europepmc", "--no-cache"])
r = ((data or {}).get("results") or [{}])[0]
record("T5 Europe PMC forced path",
       rc in (0, 1) and r.get("meta_source") == "europepmc" and r.get("verdict") in ("correct", "partial", "mismatch"),
       f"meta_source={r.get('meta_source')} verdict={r.get('verdict')}")

# ── T6: cache round-trip (2nd run hits cache) ──
with tempfile.TemporaryDirectory() as td:
    env_cache = "--cache-days"  # default cache dir is shared; use --no-cache=False path
    rc1, so1, _ = run_cli(["--pmids", GOOD_PMIDS[0]])
    rc2, so2, _ = run_cli(["--pmids", GOOD_PMIDS[0]])
    record("T6 cache round-trip", "cached, 0 to query" in so2.replace("'", ""),
           so2.strip().splitlines()[0][:60] if so2.strip() else "no output")

# ── T7: honest unknown when both sources forced unreachable (breaker primed) ──
sys.path.insert(0, str(ROOT / "scripts"))
import verify_pmids as vp
vp._HOST_FAILS[vp._host_key("https://eutils.ncbi.nlm.nih.gov")] = 99
vp._HOST_FAILS[vp._host_key("https://www.ebi.ac.uk")] = 99
out = vp.fetch_summaries([GOOD_PMIDS[0]])
vp._HOST_FAILS.clear()
verdict, details = vp.classify_unverified(out[GOOD_PMIDS[0]])
record("T7 honest unknown (both sources unreachable)", verdict == "unknown", details[:60])

# ── T8: retraction detection (STAP cells, Obokata 2014 Nature, retracted 2014) ──
wake = [{"pmid": "24476887",
         "title": "Stimulus-triggered fate conversion of somatic cells into pluripotency",
         "authors": ["Obokata"], "journal": "Nature", "year": "2014"}]
rc, so, data = run_cli(["--claims", json.dumps(wake), "--verify-doi",
                        "--mailto", "acceptance@example.org", "--no-cache"])
r = ((data or {}).get("results") or [{}])[0]
record("T8 retraction detection (capped at partial)",
       r.get("retracted") is True and r.get("verdict") == "partial",
       f"retracted={r.get('retracted')} verdict={r.get('verdict')} note={str(r.get('retraction_note',''))[:40]}")

# ── T9: DOI splice detection (real paper + real DOI of a DIFFERENT paper) ──
a0 = GOOD_PMIDS[0]
splice_claims = [{"pmid": a0,
                  "title": truth[a0]["title"],
                  "authors": [truth[a0]["authors"][0].split()[0]],
                  "journal": truth[a0]["journal"],
                  "year": truth[a0]["pubdate"][:4],
                  "doi": "10.1038/nature12968"}]   # real, LIVE DOI of a different paper
rc, so, data = run_cli(["--claims", json.dumps(splice_claims), "--no-cache"])
r = ((data or {}).get("results") or [{}])[0]
record("T9 DOI splice detection (capped at partial)",
       r.get("doi_splice_suspect") is True and r.get("verdict") == "partial",
       f"splice={r.get('doi_splice_suspect')} verdict={r.get('verdict')} rc={rc}")

# ── T10: composite elocationid (eLife) must not false-flag splice ──
elife_claims = [{"pmid": "42770840", "doi": "10.7554/eLife.92593"}]
rc, so, data = run_cli(["--claims", json.dumps(elife_claims), "--no-cache"])
r = ((data or {}).get("results") or [{}])[0]
record("T10 composite elocationid (eLife) no false splice",
       r.get("doi_cross_match") is True and "doi_splice_suspect" not in r,
       f"cross={r.get('doi_cross_match')} splice={r.get('doi_splice_suspect')}")

# ── T11: audit working-paper end-to-end ──
import tempfile
with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
    audit_path = tf.name
rc, so, data = run_cli(["--claims", json.dumps(claims[:1]), "--no-cache",
                        "--export-audit", audit_path])
audit = json.loads(Path(audit_path).read_text(encoding="utf-8"))
c0 = audit["citations"][0]
record("T11 audit working-paper e2e",
       rc in (0, 1) and audit["tool"]["version"] == vp._TOOL_VERSION
       and "evidence" in c0 and c0["evidence"].get("title_match") is not None
       and c0["registered"]["title"] == truth[GOOD_PMIDS[0]]["title"][:len(c0["registered"]["title"])],
       f"citations={len(audit['citations'])} evidence_keys={sorted(k for k in c0.get('evidence', {}))[:3]}")
Path(audit_path).unlink(missing_ok=True)

# ── T12: retraction for every PMID (no --verify-doi, no DOI needed) ──
rc, so, data = run_cli(["--claims", json.dumps([
    {"pmid": "24476887", "title": "Stimulus-triggered fate conversion of somatic cells into pluripotency",
     "authors": ["Obokata"], "journal": "Nature", "year": "2014"}]), "--no-cache"])
r = ((data or {}).get("results") or [{}])[0]
record("T12 pubtype retraction (DOI-less path, capped at partial)",
       r.get("retracted") is True and r.get("verdict") == "partial",
       f"retracted={r.get('retracted')} verdict={r.get('verdict')} note={str(r.get('retraction_note',''))[:50]}")

# ── T13: BibTeX export e2e ──
import tempfile
with tempfile.NamedTemporaryFile(suffix=".bib", delete=False) as tf:
    bib_path = tf.name
rc, so, data = run_cli(["--claims", json.dumps(claims[:1]), "--no-cache",
                        "--export-bibtex", bib_path])
bib = Path(bib_path).read_text(encoding="utf-8")
record("T13 BibTeX export e2e",
       rc in (0, 1) and "@article{" in bib and "pmid = {" in bib and "verified by pubmed-verifier" in bib,
       f"entries={bib.count('@article{')}")
Path(bib_path).unlink(missing_ok=True)

# ── T14: DOI-native verification (real DOI resolves, fake DOI = invalid) ──
rc, so, data = run_cli(["--dois", "10.1038/nature12968,10.9999/fake.123456", "--no-cache"])
res = {r.get("doi"): r for r in (data or {}).get("results", [])}
ok1 = res.get("10.1038/nature12968", {}).get("resolved") is True
ok2 = res.get("10.9999/fake.123456", {}).get("verdict") == "invalid"
record("T14 DOI-native (resolve + fabrication signal)", rc == 1 and ok1 and ok2,
       f"resolved={ok1} fake-invalid={ok2} rc={rc}")

# ── T15: delta audit e2e (run, then diff against the exported baseline) ──
import tempfile
with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
    base_path = tf.name
rc1, _, _ = run_cli(["--claims", json.dumps(claims[:1]), "--no-cache",
                     "--export-audit", base_path])
rc2, so2, data2 = run_cli(["--claims", json.dumps(claims[:1]), "--no-cache",
                           "--diff", base_path])
d2 = (data2 or {}).get("stats", {}).get("deltas")
Path(base_path).unlink(missing_ok=True)
record("T15 delta audit e2e",
       rc1 in (0, 1) and rc2 in (0, 1) and d2 is not None and "newly_retracted" in d2,
       f"rc1={rc1} rc2={rc2} deltas={d2}")

# ── T16: arXiv existence two-state (real resolves / malformed = invalid) ──
rc, so, data = run_cli(["--arxivs", "1706.03762,2413.99999", "--no-cache"])
res_a = {r.get("arxiv_id"): r for r in (data or {}).get("results", [])}
ok_real = res_a.get("1706.03762", {}).get("valid") is True
ok_bad = res_a.get("2413.99999", {}).get("verdict") == "invalid"
record("T16 arXiv existence (real resolves, malformed invalid)",
       ok_real and ok_bad,
       f"real-valid={ok_real} badmonth={res_a.get('2413.99999',{}).get('verdict')}")

# ── T17: v3.3.0 preprint ↔ published version-of-record chain ──
# Ground truth probed 10-02: arXiv:2005.13892 registers DOI
# 10.1371/journal.pone.0239699 (PLOS ONE 2020), PMID 32966344 via EPMC.
rc, so, data = run_cli(["--arxivs", "2005.13892", "--no-cache"])
r = ((data or {}).get("results") or [{}])[0]
hint_ok = "Version of record: DOI 10.1371/journal.pone.0239699" in str(r.get("details", ""))
link_ok = r.get("pmid") == "32966344"
record("T17a bare preprint surfaces version of record (+PMID link)",
       r.get("valid") is True and hint_ok and link_ok,
       f"hint={hint_ok} pmid={r.get('pmid')}")

match_claims = [{"arxiv_id": "2005.13892",
                 "title": "City size and the spreading of COVID-19 in Brazil",
                 "doi": "10.1371/journal.pone.0239699"}]
rc, so, data = run_cli(["--claims", json.dumps(match_claims), "--no-cache"])
r = ((data or {}).get("results") or [{}])[0]
st_b = (data or {}).get("stats", {})
record("T17b claimed DOI matches registered → correct + evidence (no unknown inflation)",
       r.get("verdict") == "correct" and (r.get("fields") or {}).get("doi") is True
       and st_b.get("correct") == 1 and st_b.get("unknown", 0) == 0,
       f"verdict={r.get('verdict')} doi_field={(r.get('fields') or {}).get('doi')} "
       f"correct={st_b.get('correct')} unknown={st_b.get('unknown')}")

spliced = [{"arxiv_id": "2005.13892",
            "title": "City size and the spreading of COVID-19 in Brazil",
            "doi": "10.9999/fabricated.pairing"}]
rc, so, data = run_cli(["--claims", json.dumps(spliced), "--no-cache"])
r = ((data or {}).get("results") or [{}])[0]
st = (data or {}).get("stats", {})
record("T17c claimed DOI mismatch caps correct → partial (stats accounting)",
       r.get("verdict") == "partial" and (r.get("fields") or {}).get("doi") is False
       and st.get("partial") == 1 and st.get("unknown") == 0,
       f"verdict={r.get('verdict')} doi_field={(r.get('fields') or {}).get('doi')} "
       f"partial={st.get('partial')} unknown={st.get('unknown')}")

# ── T18: DOI claims first-class (v3.4.0) ──
rc, so, data = run_cli(["--claims", json.dumps([
    {"doi": "10.1371/journal.pone.0239699",
     "title": "City size and the spreading of COVID-19 in Brazil",
     "journal": "PLoS ONE", "year": "2020"}]), "--no-cache"])
r = ((data or {}).get("results") or [{}])[0]
st18 = (data or {}).get("stats", {})
record("T18a DOI claims correct (linked PubMed record, no unknown inflation)",
       r.get("verdict") == "correct" and st18.get("correct") == 1 and st18.get("unknown") == 0,
       f"verdict={r.get('verdict')} correct={st18.get('correct')} unknown={st18.get('unknown')}")

rc, so, data = run_cli(["--claims", json.dumps([
    {"doi": "10.1371/journal.pone.0239699",
     "title": "A completely different paper about widgets"}]), "--no-cache"])
r = ((data or {}).get("results") or [{}])[0]
record("T18b DOI claims wrong title → mismatch",
       rc == 1 and r.get("verdict") == "mismatch",
       f"rc={rc} verdict={r.get('verdict')}")

# ── T18c: claims-sourced DOI 404 = fabrication signal (user-endorsed) ──
rc, so, data = run_cli(["--claims", json.dumps([
    {"doi": "10.9999/fake.does.not.exist", "title": "Anything"}]), "--no-cache"])
r = ((data or {}).get("results") or [{}])[0]
record("T18c claims-sourced DOI 404 → invalid (not suspect)",
       rc == 1 and r.get("verdict") == "invalid",
       f"rc={rc} verdict={r.get('verdict')}")

# ── T19: DOI claims + retraction cap (STAP paper, real retracted DOI) ──
rc, so, data = run_cli(["--claims", json.dumps([
    {"doi": "10.1038/nature12968",
     "title": "Stimulus-triggered fate conversion of somatic cells into pluripotency",
     "journal": "Nature"}]), "--no-cache"])
r = ((data or {}).get("results") or [{}])[0]
record("T19 DOI claims retraction cap (claimed correct → partial)",
       r.get("verdict") == "partial" and r.get("retracted") is True,
       f"verdict={r.get('verdict')} retracted={r.get('retracted')} note={str(r.get('retraction_note',''))[:40]}")

# ── 汇总 ──
passed = sum(1 for r in RESULTS if r["ok"])
failed = sum(1 for r in RESULTS if not r["ok"])
print(f"汇总: PASS {passed}/{len(RESULTS)}, FAIL {failed}")
sys.exit(0 if failed == 0 else 1)
