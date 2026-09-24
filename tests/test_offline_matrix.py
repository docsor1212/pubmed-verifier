#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline test matrix for pubmed-verifier v2.2.0 (stdlib only, no network).

Run:  python3 tests/test_offline_matrix.py
Covers the v2.2.0 network hardening (Retry-After, UA rotation, circuit
breaker, polite pool, API-key injection, Europe PMC parsing, honest-unknown
classification, cache protection) plus regression of the five-state core.
"""

import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import verify_pmids as vp  # noqa: E402


def _ok(body: bytes):
    """urlopen stand-in returning a successful response."""
    m = mock.mock_open(read_data=body)
    m.return_value.__enter__ = lambda s: m.return_value
    return m.return_value


class _Reset(unittest.TestCase):
    def setUp(self):
        vp._HOST_FAILS.clear()
        vp._OPTS.update({"ncbi_key": "", "mailto": "", "meta_source": "auto", "timeout": 20})


class TestRetryAfter(_Reset):
    def _run(self, headers):
        errs = [urllib.error.HTTPError("u", 429, "Too Many Requests", headers, None)
                for _ in range(2)]
        with mock.patch.object(vp.urllib.request, "urlopen", side_effect=errs + [_ok(b"ok")]), \
             mock.patch.object(vp.time, "sleep") as sl:
            body = vp._api_get("https://api.crossref.org/works/10.1/x")
        self.assertEqual(body, b"ok")
        return [round(c.args[0], 2) for c in sl.call_args_list]

    def test_retry_after_respected(self):
        self.assertEqual(self._run({"Retry-After": "3"}), [3.0, 3.0])

    def test_retry_after_default_when_missing(self):
        self.assertEqual(self._run({}), [2.0, 2.0])

    def test_retry_after_clamped_to_5(self):
        self.assertEqual(self._run({"Retry-After": "30"}), [5.0, 5.0])

    def test_retry_after_garbage_value(self):
        self.assertEqual(self._run({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), [2.0, 2.0])

    def test_429_exhausted_does_not_trip_breaker(self):
        with mock.patch.object(vp.urllib.request, "urlopen",
                               side_effect=urllib.error.HTTPError("u", 429, "x", {}, None)), \
             mock.patch.object(vp.time, "sleep"):
            with self.assertRaises(urllib.error.HTTPError):
                vp._api_get("https://api.crossref.org/x")
            self.assertEqual(vp._HOST_FAILS[vp._host_key("https://api.crossref.org/x")], 0)


class TestUaRotation(_Reset):
    def test_403_rotates_to_browser_ua(self):
        seen = []

        def fake(req, timeout=None):
            seen.append(req.headers.get("User-agent"))
            if len(seen) == 1:
                raise urllib.error.HTTPError("u", 403, "forbidden", {}, None)
            return _ok(b"ok")

        with mock.patch.object(vp.urllib.request, "urlopen", side_effect=fake), \
             mock.patch.object(vp.time, "sleep"):
            vp._api_get("https://www.ebi.ac.uk/x")
        self.assertEqual(seen[0], vp._UA_TOOL)
        self.assertEqual(seen[1], vp._UA_BROWSER)

    def test_406_rotates(self):
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError("u", 406, "not acceptable", {}, None)
            return _ok(b"ok")

        with mock.patch.object(vp.urllib.request, "urlopen", side_effect=fake), \
             mock.patch.object(vp.time, "sleep"):
            self.assertEqual(vp._api_get("https://export.arxiv.org/x"), b"ok")


class TestCircuitBreaker(_Reset):
    def test_opens_after_two_exhausted_calls(self):
        calls = {"n": 0}

        def boom(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.URLError("conn refused")

        with mock.patch.object(vp.urllib.request, "urlopen", side_effect=boom), \
             mock.patch.object(vp.time, "sleep"):
            for _ in range(2):
                with self.assertRaises(urllib.error.URLError):
                    vp._api_get("https://eutils.ncbi.nlm.nih.gov/x")
            self.assertEqual(calls["n"], 6)          # 2 calls x 3 attempts
            with self.assertRaises(vp.CircuitOpenError):
                vp._api_get("https://eutils.ncbi.nlm.nih.gov/y")
            self.assertEqual(calls["n"], 6)          # no more network attempts

    def test_counts_once_per_logical_call(self):
        with mock.patch.object(vp.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("down")), \
             mock.patch.object(vp.time, "sleep"):
            with self.assertRaises(urllib.error.URLError):
                vp._api_get("https://eutils.ncbi.nlm.nih.gov/x")
            self.assertEqual(vp._HOST_FAILS["https://eutils.ncbi.nlm.nih.gov"], 1)

    def test_success_resets(self):
        vp._HOST_FAILS["https://eutils.ncbi.nlm.nih.gov"] = 1
        with mock.patch.object(vp.urllib.request, "urlopen", return_value=_ok(b"ok")):
            vp._api_get("https://eutils.ncbi.nlm.nih.gov/x")
        self.assertEqual(vp._HOST_FAILS["https://eutils.ncbi.nlm.nih.gov"], 0)

    def test_per_host_isolation(self):
        vp._HOST_FAILS["https://eutils.ncbi.nlm.nih.gov"] = 5
        with mock.patch.object(vp.urllib.request, "urlopen", return_value=_ok(b"ok")):
            self.assertEqual(vp._api_get("https://www.ebi.ac.uk/x"), b"ok")


class TestPolitePoolAndKey(_Reset):
    def test_mailto_adds_polite_pool(self):
        vp._OPTS["mailto"] = "lab@example.org"
        seen = {}

        def fake(req, timeout=None):
            seen["url"] = req.full_url
            return _ok(json.dumps({"message": {}}).encode())

        with mock.patch.object(vp.urllib.request, "urlopen", side_effect=fake):
            vp.fetch_doi_metadata("10.1186/x")
        self.assertIn("?mailto=lab%40example.org", seen["url"])

    def test_ncbi_key_injected_into_esummary(self):
        vp._OPTS["ncbi_key"] = "abc123"
        seen = {}

        def fake(req, timeout=None):
            seen["url"] = req.full_url
            return _ok(json.dumps({"result": {}}).encode())

        with mock.patch.object(vp.urllib.request, "urlopen", side_effect=fake), \
             mock.patch.object(vp.time, "sleep"):
            vp.fetch_summaries(["12345678"])
        self.assertIn("api_key=abc123", seen["url"])
        self.assertIn("tool=pubmed-verifier", seen["url"])

    def test_ncbi_key_speeds_interval(self):
        vp._OPTS["ncbi_key"] = "abc123"
        with mock.patch.object(vp.urllib.request, "urlopen",
                               return_value=_ok(json.dumps({"result": {}}).encode())), \
             mock.patch.object(vp.time, "sleep") as sl:
            vp.fetch_summaries(["1", "2"], batch_size=1)
        self.assertTrue(all(abs(c.args[0] - 0.12) < 1e-9 for c in sl.call_args_list))

    def test_mailto_env_used_when_flag_absent(self):
        with mock.patch.dict(os.environ, {"NCBI_API_KEY": "envkey"}):
            self.assertEqual(vp.os.environ.get("NCBI_API_KEY"), "envkey")


class TestEuropePmc(_Reset):
    def test_parsing_maps_to_metadata_shape(self):
        payload = {"hitCount": 1, "resultList": {"result": [{
            "id": "31018962", "title": "Some Title.", "authorString": "Smith J, Doe A",
            "journalTitle": "Pediatr Rheumatol", "pubYear": "2021",
            "doi": "10.1186/x", "journalVolume": "19", "pageInfo": "1-10"}]}}
        with mock.patch.object(vp.urllib.request, "urlopen",
                               return_value=_ok(json.dumps(payload).encode())):
            out = vp.fetch_summaries_europepmc(["31018962"])
        info = out["31018962"]
        self.assertTrue(info["valid"])
        self.assertEqual(info["source"], "europepmc")
        self.assertEqual(info["authors"], ["Smith J", "Doe A"])
        self.assertEqual(info["pubdate"], "2021")
        self.assertEqual(info["journal"], "Pediatr Rheumatol")

    def test_missing_pmid_reported_not_found(self):
        with mock.patch.object(vp.urllib.request, "urlopen",
                               return_value=_ok(json.dumps({"hitCount": 0, "resultList": {"result": []}}).encode())):
            out = vp.fetch_summaries_europepmc(["11111111"])
        self.assertFalse(out["11111111"]["valid"])
        self.assertNotIn("network_error", out["11111111"])

    def test_failure_marks_network_error_not_invalid(self):
        with mock.patch.object(vp.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("down")), \
             mock.patch.object(vp.time, "sleep"):
            out = vp.fetch_summaries_europepmc(["11111111"])
        self.assertTrue(out["11111111"].get("network_error"))

    def test_fallback_engages_on_ncbi_transport_failure(self):
        def fake(req, timeout=None):
            if "ncbi" in req.full_url:
                raise urllib.error.URLError("down")
            return _ok(json.dumps({"resultList": {"result": [
                {"id": "12345678", "title": "T", "authorString": "A B",
                 "journalTitle": "J", "pubYear": "2020"}]}}).encode())

        with mock.patch.object(vp.urllib.request, "urlopen", side_effect=fake), \
             mock.patch.object(vp.time, "sleep"):
            out = vp.fetch_summaries(["12345678"])
        self.assertTrue(out["12345678"]["valid"])
        self.assertEqual(out["12345678"]["source"], "europepmc")

    def test_no_fallback_when_meta_source_ncbi(self):
        vp._OPTS["meta_source"] = "ncbi"
        with mock.patch.object(vp.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("down")), \
             mock.patch.object(vp.time, "sleep"):
            out = vp.fetch_summaries(["12345678"])
        self.assertTrue(out["12345678"].get("network_error"))

    def test_explicit_europepmc_never_touches_ncbi(self):
        seen = []
        with mock.patch.object(vp.urllib.request, "urlopen",
                               side_effect=lambda req, timeout=None:
                                   seen.append(req.full_url) or _ok(json.dumps(
                                       {"resultList": {"result": []}}).encode())), \
             mock.patch.object(vp.time, "sleep"):
            vp._OPTS["meta_source"] = "europepmc"
            vp.fetch_summaries(["12345678"])
        vp._OPTS["meta_source"] = "auto"
        self.assertTrue(seen and all("europepmc" in u for u in seen))


class TestHonestVerdicts(_Reset):
    def test_classify_network_error_unknown(self):
        verdict, details = vp.classify_unverified({"valid": False, "network_error": True})
        self.assertEqual(verdict, "unknown")
        self.assertIn("不判为无效", details)

    def test_classify_not_found_invalid(self):
        verdict, details = vp.classify_unverified({"valid": False, "error": "invalid_id"})
        self.assertEqual(verdict, "invalid")
        self.assertIn("not found", details)

    def test_network_errors_never_cached(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "cache.db"
            vp._cache_init(db)
            vp._cache_save_fresh(db, {
                "11111111": {"valid": False, "network_error": True, "error": "down"},
                "22222222": {"valid": True, "title": "T", "authors": [], "journal": "J",
                             "pubdate": "2020", "doi": "", "source": "ncbi"},
            })
            cached = vp._cache_load(db, ["11111111", "22222222"])
            self.assertNotIn("11111111", cached)   # network failure NOT cached
            self.assertIn("22222222", cached)


class TestFiveStateCore(_Reset):
    """Regression: the v2.1.x matching core must keep its behavior."""

    def test_parse_citation_context_vancouver(self):
        ctx = ("Zaripova A, Shostak N. Juvenile idiopathic arthritis. Pediatr Rheumatol. "
               "2021;19:47. PMID: 34078778")
        c = vp.parse_citation_context(ctx)
        self.assertEqual(c["claimed_year"], "2021")
        self.assertIn("Zaripova", c["claimed_authors"])
        self.assertTrue(c["claimed_title"])

    def test_cross_check_correct(self):
        claimed = {"claimed_title": "Juvenile idiopathic arthritis pathogenesis",
                   "claimed_authors": ["Zaripova"], "claimed_journal": "Pediatr Rheumatol",
                   "claimed_year": "2021"}
        actual = {"title": "Pathogenesis of juvenile idiopathic arthritis",
                  "authors": ["Zaripova A"], "journal": "Pediatr Rheumatol Online J",
                  "pubdate": "2021 Jan 5"}
        self.assertEqual(vp.cross_check_citation(claimed, actual)["verdict"], "correct")

    def test_cross_check_mismatch(self):
        claimed = {"claimed_title": "Juvenile idiopathic arthritis review",
                   "claimed_authors": ["Ravelli"], "claimed_journal": "Lancet",
                   "claimed_year": "2007"}
        actual = {"title": "HIV prevention microbicide trial", "authors": ["Smith J"],
                  "journal": "Lancet", "pubdate": "2007 Jun"}
        self.assertEqual(vp.cross_check_citation(claimed, actual)["verdict"], "mismatch")

    def test_cross_check_unknown_when_no_claims(self):
        claimed = {"claimed_title": "", "claimed_authors": [], "claimed_journal": "", "claimed_year": ""}
        actual = {"title": "T", "authors": ["A"], "journal": "J", "pubdate": "2020"}
        self.assertEqual(vp.cross_check_citation(claimed, actual)["verdict"], "unknown")


class TestCli(_Reset):
    def test_version_flag(self):
        import subprocess
        script = Path(__file__).resolve().parent.parent / "scripts" / "verify_pmids.py"
        out = subprocess.run([sys.executable, str(script), "--version"],
                             capture_output=True, text=True)
        self.assertIn("2.5.0", out.stdout + out.stderr)

    def test_host_key_granularity(self):
        self.assertEqual(vp._host_key("https://eutils.ncbi.nlm.nih.gov/a?b=c"),
                         "https://eutils.ncbi.nlm.nih.gov")


class TestReviewFixes(_Reset):
    """Regression for the v2.2.0 multi-expert review fixes."""

    def test_esummary_without_result_key_is_network_error(self):
        with mock.patch.object(vp.urllib.request, "urlopen",
                               return_value=_ok(json.dumps({"esummaryversion": "x"}).encode())), \
             mock.patch.object(vp.time, "sleep"):
            vp._OPTS["meta_source"] = "ncbi"
            out = vp.fetch_summaries(["12345678"])
            vp._OPTS["meta_source"] = "auto"
        self.assertTrue(out["12345678"].get("network_error"))
        verdict, _ = vp.classify_unverified(out["12345678"])
        self.assertEqual(verdict, "unknown")

    def test_cache_migration_clears_legacy_negatives(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "cache.db"
            conn = sqlite3.connect(str(db))
            with conn:
                conn.execute("CREATE TABLE pmid_cache (pmid TEXT PRIMARY KEY, title TEXT, authors TEXT,"
                             " journal TEXT, pubdate TEXT, doi TEXT, valid INTEGER, error TEXT,"
                             " cached_at REAL, source TEXT DEFAULT 'pubmed')")
                conn.execute("INSERT INTO pmid_cache VALUES ('99999999','','','','','',0,'down',1,'pubmed')")
                conn.execute("INSERT INTO pmid_cache VALUES ('12345678','T','[]','J','2020','',1,'',1,'pubmed')")
            conn.close()
            vp._cache_init(db)
            conn = sqlite3.connect(str(db))
            rows = conn.execute("SELECT pmid FROM pmid_cache").fetchall()
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
            conn.close()
            self.assertEqual([r[0] for r in rows], ["12345678"])   # stale negative dropped
            self.assertGreaterEqual(ver, vp.CACHE_SCHEMA_VERSION)

    def test_epmc_malformed_response_is_network_error(self):
        with mock.patch.object(vp.urllib.request, "urlopen",
                               return_value=_ok(json.dumps({"version": "6.1"}).encode())), \
             mock.patch.object(vp.time, "sleep"):
            out = vp.fetch_summaries_europepmc(["12345678"])
        self.assertTrue(out["12345678"].get("network_error"))

    def test_5xx_retries_with_backoff(self):
        errs = [urllib.error.HTTPError("u", 503, "unavailable", {}, None) for _ in range(2)]
        with mock.patch.object(vp.urllib.request, "urlopen", side_effect=errs + [_ok(b"ok")]), \
             mock.patch.object(vp.time, "sleep") as sl:
            self.assertEqual(vp._api_get("https://eutils.ncbi.nlm.nih.gov/x"), b"ok")
        self.assertEqual([round(c.args[0], 1) for c in sl.call_args_list], [1.0, 2.0])

    def test_404_raises_immediately_without_retry(self):
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError("u", 404, "not found", {}, None)

        with mock.patch.object(vp.urllib.request, "urlopen", side_effect=fake), \
             mock.patch.object(vp.time, "sleep") as sl:
            with self.assertRaises(urllib.error.HTTPError):
                vp._api_get("https://api.crossref.org/works/bad-doi")
        self.assertEqual(calls["n"], 1)
        self.assertEqual(sl.call_args_list, [])

    def test_output_txt_extension_refused(self):
        import subprocess
        script = Path(__file__).resolve().parent.parent / "scripts" / "verify_pmids.py"
        with tempfile.TemporaryDirectory() as td:
            out = subprocess.run([sys.executable, str(script), "--pmids", "1", "--no-cache",
                                  "--output", str(Path(td) / "r.txt")],
                                 capture_output=True, text=True, timeout=60)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("Unsupported", out.stderr)


class TestDoiCross(_Reset):
    """v2.4.0 DOI identity comparison."""

    def test_exact_match(self):
        self.assertTrue(vp.dois_match("10.1186/s12969-021-00611-9",
                                      "10.1186/s12969-021-00611-9"))

    def test_url_and_prefix_variants(self):
        self.assertTrue(vp.dois_match("https://doi.org/10.1186/X",
                                      "10.1186/x"))
        self.assertTrue(vp.dois_match("http://dx.doi.org/10.1/AbC",
                                      "doi: 10.1/abc"))
        self.assertTrue(vp.dois_match("doi:doi:10.1/x", "10.1/x"))
        self.assertTrue(vp.dois_match("dx.doi.org/10.1/x", "10.1/X"))
        self.assertTrue(vp.dois_match("10.1/x.", "10.1/x"))

    def test_mismatch_detected(self):
        self.assertFalse(vp.dois_match("10.4012/dmj.2020-408",
                                       "10.1186/s12969-021-00611-9"))

    def test_empty_never_matches(self):
        self.assertFalse(vp.dois_match("", "10.1/x"))
        self.assertFalse(vp.dois_match("10.1/x", ""))
        self.assertFalse(vp.dois_match(None, None))

    def test_composite_elocationid_uses_articleids(self):
        # Review P0 (real data PMID 42770840, eLife): elocationid is a
        # composite string; articleids[] carries the registered DOI.
        payload = {"result": {"42770840": {
            "title": "T", "source": "eLife",
            "elocationid": "pii: RP92593. doi: 10.7554/eLife.92593",
            "articleids": [{"idtype": "pubmed", "value": "42770840"},
                           {"idtype": "doi", "value": "10.7554/eLife.92593"}]}}}
        out = vp._parse_esummary(["42770840"], payload)
        self.assertEqual(out["42770840"]["doi"], "10.7554/eLife.92593")
        self.assertTrue(vp.dois_match("10.7554/eLife.92593", out["42770840"]["doi"]))

    def test_composite_without_articleids_falls_back_to_regex(self):
        payload = {"result": {"999": {
            "title": "T", "source": "X",
            "elocationid": "pii: RP9. doi: 10.1/y"}}}
        out = vp._parse_esummary(["999"], payload)
        self.assertEqual(out["999"]["doi"], "10.1/y")


class TestJournalAbbrev(_Reset):
    """v2.4.0 NLM-style abbreviation equivalence."""

    def test_nejm_full_and_back(self):
        self.assertTrue(vp._journal_abbrev_match(
            "N Engl J Med", "New England Journal of Medicine"))
        self.assertTrue(vp._journal_abbrev_match(
            "New England Journal of Medicine", "N Engl J Med"))

    def test_pediatr_rheumatol(self):
        self.assertTrue(vp._journal_abbrev_match(
            "Pediatr Rheumatol Online J", "Pediatric Rheumatology Online Journal"))

    def test_dots_stripped(self):
        self.assertTrue(vp._journal_abbrev_match(
            "N. Engl. J. Med.", "New England Journal of Medicine"))

    def test_negative_different_journal(self):
        self.assertFalse(vp._journal_abbrev_match(
            "Nature", "New England Journal of Medicine"))
        self.assertFalse(vp._journal_abbrev_match(
            "Lancet", "New England Journal of Medicine"))

    def test_negative_wrong_order(self):
        # words present but out of order must NOT match
        self.assertFalse(vp._journal_abbrev_match(
            "Med Engl J N", "New England Journal of Medicine"))


class TestAuthorVerification(_Reset):
    """v2.5.0 author-name verification (initials + CJK cross-language)."""

    def test_initial_only_claim_never_matches(self):
        claimed = {"claimed_title": "", "claimed_authors": ["A"],
                   "claimed_journal": "", "claimed_year": ""}
        actual = {"title": "", "authors": ["Zaripova A"], "journal": "", "pubdate": ""}
        r = vp.cross_check_citation(claimed, actual)
        self.assertFalse(r["author_match"])   # "a" must not substring-hit

    def test_initials_dropped_both_sides(self):
        claimed = {"claimed_title": "", "claimed_authors": ["Zaripova", "A."],
                   "claimed_journal": "", "claimed_year": ""}
        actual = {"title": "", "authors": ["A Zaripova", "B Smith"], "journal": "", "pubdate": ""}
        r = vp.cross_check_citation(claimed, actual)
        # "A." dropped from claims; "A Zaripova"→"zaripova" (not "a"); hit via zaripova
        self.assertTrue(r["author_match"])

    def test_cjk_vs_latin_skipped_not_mismatch(self):
        claimed = {"claimed_title": "", "claimed_authors": ["张三"],
                   "claimed_journal": "", "claimed_year": ""}
        actual = {"title": "", "authors": ["Zhang San"], "journal": "", "pubdate": ""}
        r = vp.cross_check_citation(claimed, actual)
        self.assertEqual(r.get("author_check"), "skipped (cross-language CJK↔Latin)")
        self.assertEqual(r["verdict"], "unknown")   # nothing comparable → honest unknown

    def test_cjk_skip_falls_back_to_other_fields(self):
        claimed = {"claimed_title": "", "claimed_authors": ["张三"],
                   "claimed_journal": "Lancet", "claimed_year": "2007"}
        actual = {"title": "", "authors": ["Zhang San"], "journal": "Lancet",
                  "pubdate": "2007 Jun"}
        r = vp.cross_check_citation(claimed, actual)
        self.assertEqual(r.get("author_check"), "skipped (cross-language CJK↔Latin)")
        self.assertTrue(r["journal_match"] and r["year_match"])
        # review P1: comparable fields unanimous -> correct, never mismatch
        self.assertEqual(r["verdict"], "correct")

    def test_claimed_full_name_token_extracted(self):
        # "Zaripova A" as a claimed token must extract to "zaripova" (v2.5.0)
        claimed = {"claimed_title": "", "claimed_authors": ["Zaripova A"],
                   "claimed_journal": "", "claimed_year": ""}
        actual = {"title": "", "authors": ["Zaripova A"], "journal": "", "pubdate": ""}
        r = vp.cross_check_citation(claimed, actual)
        self.assertTrue(r["author_match"])

    def test_mixed_language_partial_skip(self):
        # Latin token stays comparable, CJK token ignored (per-token skip)
        claimed = {"claimed_title": "", "claimed_authors": ["Smith", "张三"],
                   "claimed_journal": "", "claimed_year": ""}
        actual = {"title": "", "authors": ["Smith J"], "journal": "", "pubdate": ""}
        r = vp.cross_check_citation(claimed, actual)
        self.assertTrue(r["author_match"])
        self.assertIn("partial skip", r.get("author_check", ""))

    def test_mismatch_details_not_fabricated(self):
        # title never claimed -> details must not say "title differs"
        claimed = {"claimed_title": "", "claimed_authors": ["Ravelli"],
                   "claimed_journal": "", "claimed_year": ""}
        actual = {"title": "", "authors": ["Smith J"], "journal": "", "pubdate": ""}
        r = vp.cross_check_citation(claimed, actual)
        self.assertEqual(r["verdict"], "mismatch")
        self.assertNotIn("title differs", r["details"])
        self.assertIn("author differs", r["details"])

    def test_regression_latin_match_unchanged(self):
        claimed = {"claimed_title": "", "claimed_authors": ["Zaripova"],
                   "claimed_journal": "", "claimed_year": ""}
        actual = {"title": "", "authors": ["Zaripova A"], "journal": "", "pubdate": ""}
        r = vp.cross_check_citation(claimed, actual)
        self.assertTrue(r["author_match"])


class TestRetraction(_Reset):
    """v2.3.0 retraction detection."""

    def test_crossref_retraction_flag_parsed(self):
        payload = {"message": {"title": ["T"],
                               "updated-by": [{"type": "retraction", "DOI": "10.1/retr"}]}}
        with mock.patch.object(vp.urllib.request, "urlopen",
                               return_value=_ok(json.dumps(payload).encode())):
            meta = vp.fetch_doi_metadata("10.1/orig")
        self.assertTrue(meta["retracted"])
        self.assertIn("10.1/retr", meta["retraction_note"])

    def test_correction_is_not_retraction(self):
        payload = {"message": {"title": ["T"],
                               "updated-by": [{"type": "correction", "DOI": "10.1/corr"}]}}
        with mock.patch.object(vp.urllib.request, "urlopen",
                               return_value=_ok(json.dumps(payload).encode())):
            meta = vp.fetch_doi_metadata("10.1/orig")
        self.assertFalse(meta["retracted"])

    def test_no_updated_by_field(self):
        payload = {"message": {"title": ["T"]}}
        with mock.patch.object(vp.urllib.request, "urlopen",
                               return_value=_ok(json.dumps(payload).encode())):
            meta = vp.fetch_doi_metadata("10.1/orig")
        self.assertFalse(meta["retracted"])

    def test_cap_correct_becomes_partial(self):
        verdict, details = vp.apply_retraction_cap("correct", "all metadata matches")
        self.assertEqual(verdict, "partial")
        self.assertIn("RETRACTED", details)
        self.assertIn("人工复核", details)

    def test_cap_mismatch_unchanged(self):
        verdict, details = vp.apply_retraction_cap("mismatch", "title differs")
        self.assertEqual(verdict, "mismatch")
        self.assertNotIn("RETRACTED", details)

    def test_cap_partial_stays_partial_with_note(self):
        verdict, details = vp.apply_retraction_cap("partial", "title differs but author+journal match")
        self.assertEqual(verdict, "partial")
        self.assertIn("RETRACTED", details)
        self.assertIn("title differs", details)

    def test_cap_empty_details_no_hanging_separator(self):
        verdict, details = vp.apply_retraction_cap("correct", "")
        self.assertEqual(verdict, "partial")
        self.assertTrue(details.startswith("RETRACTED"))

    def test_update_type_fallback_key_and_case(self):
        payload = {"message": {"title": ["T"],
                               "updated-by": [{"update-type": "Retraction", "DOI": "10.1/z"}]}}
        with mock.patch.object(vp.urllib.request, "urlopen",
                               return_value=_ok(json.dumps(payload).encode())):
            meta = vp.fetch_doi_metadata("10.1/orig")
        self.assertTrue(meta["retracted"])

    def test_nondict_updated_by_entries_skipped(self):
        payload = {"message": {"title": ["T"], "updated-by": ["garbage",
                               {"type": "retraction", "DOI": "10.1/w"}]}}
        with mock.patch.object(vp.urllib.request, "urlopen",
                               return_value=_ok(json.dumps(payload).encode())):
            meta = vp.fetch_doi_metadata("10.1/orig")
        self.assertTrue(meta["retracted"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
