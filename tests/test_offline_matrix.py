#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline test matrix for pubmed-verifier (stdlib only, no network).

Run:  python3 tests/test_offline_matrix.py
Covers the v2.2.0 network hardening (Retry-After, UA rotation, circuit
breaker, polite pool, API-key injection, Europe PMC parsing, honest-unknown
classification, cache protection) plus regression of the five-state core.
"""

import csv
import json
import os
import re
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
        self.assertIn(vp._TOOL_VERSION, out.stdout + out.stderr)

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
        # title never claimed -> details must not say "title differs"; and
        # with only the author field comparable (and wrong), the honest
        # verdict is partial, not mismatch (v2.8.0 title-not-claimed ladder)
        claimed = {"claimed_title": "", "claimed_authors": ["Ravelli"],
                   "claimed_journal": "", "claimed_year": ""}
        actual = {"title": "", "authors": ["Smith J"], "journal": "", "pubdate": ""}
        r = vp.cross_check_citation(claimed, actual)
        self.assertEqual(r["verdict"], "partial")
        self.assertNotIn("title differs", r["details"])
        self.assertIn("author differs", r["details"])

    def test_regression_latin_match_unchanged(self):
        claimed = {"claimed_title": "", "claimed_authors": ["Zaripova"],
                   "claimed_journal": "", "claimed_year": ""}
        actual = {"title": "", "authors": ["Zaripova A"], "journal": "", "pubdate": ""}
        r = vp.cross_check_citation(claimed, actual)
        self.assertTrue(r["author_match"])


class TestReliability(_Reset):
    """v2.6.0 negative-cache TTL + circuit-breaker half-open."""

    def test_negative_cache_short_ttl(self):
        import sqlite3
        now = __import__("time").time()
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "cache.db"
            conn = sqlite3.connect(str(db))
            with conn:
                conn.execute("CREATE TABLE pmid_cache (pmid TEXT PRIMARY KEY, title TEXT, authors TEXT,"
                             " journal TEXT, pubdate TEXT, doi TEXT, valid INTEGER, error TEXT,"
                             " cached_at REAL, source TEXT DEFAULT 'pubmed')")
                conn.execute("INSERT INTO pmid_cache VALUES ('11111111','','','','','',0,'nf',?, 'pubmed')",
                             (now - 4 * 86400,))          # 4 days old -> expired
                conn.execute("INSERT INTO pmid_cache VALUES ('22222222','','','','','',0,'nf',?, 'pubmed')",
                             (now - 2 * 86400,))          # 2 days old -> still cached
                conn.execute("INSERT INTO pmid_cache VALUES ('33333333','T','[]','J','2020','',1,'',?, 'pubmed')",
                             (now - 10 * 86400,))         # positive 10 days -> cached (30d)
                conn.execute("PRAGMA user_version = 2")   # simulate already-migrated DB
            conn.close()
            vp._cache_init(db)   # migration is a no-op at version 2
            cached = vp._cache_load(db, ["11111111", "22222222", "33333333"])
            self.assertNotIn("11111111", cached)   # negative expired at 3 days
            self.assertIn("22222222", cached)
            self.assertIn("33333333", cached)

    def test_breaker_half_open_after_cooldown(self):
        calls = {"n": 0}

        def flaky(req, timeout=None):
            calls["n"] += 1
            if calls["n"] <= 6:                      # 2 exhausted calls
                raise urllib.error.URLError("down")
            return _ok(b"recovered")

        with mock.patch.object(vp.urllib.request, "urlopen", side_effect=flaky), \
             mock.patch.object(vp.time, "sleep"):
            for _ in range(2):
                with self.assertRaises(urllib.error.URLError):
                    vp._api_get("https://eutils.ncbi.nlm.nih.gov/x")
            with self.assertRaises(vp.CircuitOpenError):
                vp._api_get("https://eutils.ncbi.nlm.nih.gov/x")
            # simulate cooldown elapsed, then the probe succeeds and resets
            vp._HOST_OPENED_AT["https://eutils.ncbi.nlm.nih.gov"] = \
                __import__("time").time() - vp._BREAKER_COOLDOWN_S - 1
            self.assertEqual(vp._api_get("https://eutils.ncbi.nlm.nih.gov/x"), b"recovered")
            self.assertEqual(vp._HOST_FAILS["https://eutils.ncbi.nlm.nih.gov"], 0)

    def test_audit_export_redacts_key(self):
        # sealed (no network): main() runs against a mocked esummary reply
        with tempfile.TemporaryDirectory() as td:
            audit_path = Path(td) / "a.json"
            fake = _ok(json.dumps({"result": {}}).encode())
            with mock.patch.object(sys, "argv",
                                   ["verify_pmids.py", "--pmids", "1", "--no-cache",
                                    "--ncbi-api-key", "SECRETVAL",
                                    "--nc", "ALSOSECRET",
                                    "--export-audit", str(audit_path)]), \
                 mock.patch.object(vp.urllib.request, "urlopen", side_effect=lambda req, timeout=None: fake), \
                 mock.patch.object(vp.time, "sleep"):
                try:
                    vp.main()
                except SystemExit:
                    pass
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
        blob = json.dumps(audit)
        self.assertNotIn("SECRETVAL", blob)
        self.assertNotIn("ALSOSECRET", blob)   # argparse prefix abbreviation
        self.assertTrue(audit["tool"]["options"]["ncbi_api_key_configured"])
        self.assertIn("verdict_ladder", audit["tool"])


class TestRetractionCoverage(_Reset):
    """v2.7.0 retraction for every PMID (pubtype signal, cache-confirmed)."""

    def test_ncbi_pubtype_flags_retracted(self):
        payload = {"result": {"24476887": {
            "title": "STAP", "source": "Nature",
            "pubtype": ["Journal Article", "Retracted Publication"],
            "articleids": [{"idtype": "doi", "value": "10.1038/nature12968"}]}}}
        out = vp._parse_esummary(["24476887"], payload)
        self.assertTrue(out["24476887"]["retracted"])

    def test_expression_of_concern_not_flagged(self):
        # EoC is an editorial note, NOT a retraction (medical-review P2)
        payload = {"result": {"555": {
            "title": "Concerned paper", "source": "X",
            "pubtype": ["Journal Article", "Expression of Concern"], "articleids": []}}}
        out = vp._parse_esummary(["555"], payload)
        self.assertFalse(out["555"]["retracted"])

    def test_retraction_notice_itself_not_flagged(self):
        payload = {"result": {"999": {
            "title": "Retraction of: X", "source": "Nature",
            "pubtype": ["Retraction of Publication"], "articleids": []}}}
        out = vp._parse_esummary(["999"], payload)
        self.assertFalse(out["999"]["retracted"])

    def test_epmc_pubtype_flags_retracted(self):
        payload = {"resultList": {"result": [{
            "id": "24476887", "title": "STAP", "authorString": "Obokata K",
            "journalTitle": "Nature", "pubYear": "2014",
            "pubType": "retracted publication; journal article"}]}}
        with mock.patch.object(vp.urllib.request, "urlopen",
                               return_value=_ok(json.dumps(payload).encode())):
            out = vp.fetch_summaries_europepmc(["24476887"])
        self.assertTrue(out["24476887"]["retracted"])

    def test_pubtype_signal_survives_cache(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "cache.db"
            vp._cache_init(db)
            vp._cache_save(db, "24476887", {
                "valid": True, "title": "STAP", "authors": [], "journal": "Nature",
                "pubdate": "2014", "doi": "10.1038/nature12968",
                "retracted": True, "source": "ncbi"})
            cached = vp._cache_load(db, ["24476887"])
        self.assertTrue(cached["24476887"]["retracted"])

    def test_schema_v3_adds_retracted_column(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "cache.db"
            conn = sqlite3.connect(str(db))
            with conn:
                conn.execute("CREATE TABLE pmid_cache (pmid TEXT PRIMARY KEY, title TEXT, authors TEXT,"
                             " journal TEXT, pubdate TEXT, doi TEXT, valid INTEGER, error TEXT,"
                             " cached_at REAL, source TEXT DEFAULT 'pubmed')")
                conn.execute("PRAGMA user_version = 2")   # v2-era DB, no retracted column
            conn.close()
            vp._cache_init(db)
            conn = sqlite3.connect(str(db))
            cols = [r[1] for r in conn.execute("PRAGMA table_info(pmid_cache)").fetchall()]
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
            conn.close()
            self.assertIn("retracted", cols)
            self.assertEqual(ver, vp.CACHE_SCHEMA_VERSION)

    def test_schema_v3_reinit_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "cache.db"
            vp._cache_init(db)
            vp._cache_init(db)   # second run: no-op, must not raise
            import sqlite3
            conn = sqlite3.connect(str(db))
            cols = [r[1] for r in conn.execute("PRAGMA table_info(pmid_cache)").fetchall()]
            conn.close()
        self.assertIn("retracted", cols)


class TestExportsAndReadiness(_Reset):
    """v2.7.0 BibTeX export + submission-readiness summary."""

    def _results(self):
        return [
            {"pmid": "123", "valid": True, "verdict": "correct", "title": "A study",
             "authors": "Smith J, Doe A", "journal": "J Test", "pubdate": "2020 Jan",
             "retracted": False},
            {"pmid": "456", "valid": True, "verdict": "partial", "title": "B study",
             "authors": "Lee M", "journal": "J T2", "pubdate": "2021",
             "retracted": False, "details": "title differs"},
            {"pmid": "789", "valid": True, "verdict": "partial", "title": "C",
             "authors": "X Y", "journal": "J", "pubdate": "2019", "retracted": True},
            {"pmid": "000", "valid": False, "verdict": "invalid", "retracted": False},
        ]

    def test_bibtex_includes_correct_only_uncommented(self):
        bib = vp.generate_bibtex(self._results())
        uncommented = [l for l in bib.splitlines() if l.startswith("@article{")]
        self.assertEqual(len(uncommented), 1)
        self.assertIn("author = {Smith, J and Doe, A}", bib)
        self.assertIn("% PARTIAL MATCH", bib)
        self.assertIn("% EXCLUDED (RETRACTED): PMID 789", bib)
        self.assertIn("1 included, 1 partial (commented), 2 excluded", bib)

    def test_readiness_ready_and_not(self):
        ready, line = vp.readiness_summary({"invalid": 0, "mismatch": 0, "retracted": 0,
                                            "doi_splice": 0, "network_errors": 0})
        self.assertTrue(ready and line.startswith("SUBMISSION READY"))
        ready, line = vp.readiness_summary({"invalid": 1, "mismatch": 0, "retracted": 1,
                                            "doi_splice": 2, "network_errors": 3})
        self.assertFalse(ready)
        for token in ("1 invalid", "1 retracted", "2 DOI-spliced", "3 unverified"):
            self.assertIn(token, line)

    def test_scanned_404_is_suspect_not_invalid(self):
        # compliance P1: auto-extracted DOIs are not user-endorsed — a
        # Crossref 404 stays a suspect (unknown), never "fabrication"
        with mock.patch.object(vp, "resolve_doi",
                               return_value={"status": "not_found", "meta": None,
                                             "error": "404"}):
            entry, _ = vp.verify_doi_entry("10.5281/zenodo.12146", "scanned.html")
        self.assertEqual(entry["verdict"], "unknown")
        self.assertTrue(entry.get("suspect"))

    def test_resolved_retracted_propagates(self):
        with mock.patch.object(vp, "resolve_doi",
                               return_value={"status": "resolved",
                                             "meta": {"title": "T", "journal": "J",
                                                      "year": "2020", "authors": [],
                                                      "retracted": True,
                                                      "retraction_note": "r by 10.1/x"},
                                             "error": ""}):
            entry, _ = vp.verify_doi_entry("10.1/retr", "cli")
        self.assertTrue(entry["retracted"])
        self.assertEqual(entry.get("retraction_source"), "crossref updated-by")


class TestHtmlRender(_Reset):
    """P0 escape-root-cause guard: HTML must render for every shape."""

    def _render(self, results, deltas=None):
        stats = {"total": len(results), "correct": 0, "mismatch": 0, "partial": 0,
                 "invalid": 0, "unknown": 0, "unmatched": 0, "retracted": 0,
                 "doi_splice": 0}
        return vp.generate_html_report(results, stats, "test",
                                       {"command": "x", "version": vp._TOOL_VERSION,
                                        "sources": "s", "generated_at": "t"},
                                       deltas)

    def test_html_renders_with_and_without_deltas(self):
        results = [{"pmid": "123", "verdict": "correct", "title": "T", "retracted": False,
                    "fields": {"title": True, "author": None, "journal": None, "year": None}}]
        for deltas in (None, {"counts": {"newly_retracted": 1}, "newly_retracted": [
                {"key": "1", "title": "T"}]}):
            html = self._render(results, deltas)
            self.assertIn("flt(", html)
            self.assertIn("Delta vs baseline" if deltas else "PMID", html)

    def test_html_doi_entries_render(self):
        results = [{"pmid": "", "doi": "10.1/x", "verdict": "unknown", "resolved": True,
                    "title": "Registered T", "retracted": False,
                    "fields": None}]
        html = self._render(results, None)
        self.assertIn("10.1/x", html)

    def test_readiness_unknown_only_discloses_existence_check(self):
        # medical-review P1: --pmids without claims -> unknown (existence-only);
        # READY must disclose that, never dress it up as full verification
        ready, line = vp.readiness_summary({"invalid": 0, "mismatch": 0, "retracted": 0,
                                            "doi_splice": 0, "network_errors": 0, "unknown": 4})
        self.assertTrue(ready)
        self.assertIn("4 citation(s) existence-checked only", line)
        self.assertIn("--claims-file", line)


class TestDoiNative(_Reset):
    """v2.8.0 DOI-native verification."""

    def test_extract_patterns(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.html"
            p.write_text('<a href="https://doi.org/10.1186/s12969-021-00611-9">x</a> '
                         'doi:10.4012/dmj.2020-408 and bare 10.1038/nature12968. end',
                         encoding="utf-8")
            dois = [d for d, _ in vp.extract_dois_from_file(str(p))]
        self.assertEqual(dois, ["10.1186/s12969-021-00611-9",
                                "10.4012/dmj.2020-408", "10.1038/nature12968"])

    def test_resolve_resolved(self):
        payload = {"message": {"title": ["Registered Title"], "type": "journal-article",
                               "container-title": ["J"], "author": [{"family": "S", "given": "G"}],
                               "issued": {"date-parts": [[2020]]}}}
        with mock.patch.object(vp.urllib.request, "urlopen",
                               return_value=_ok(json.dumps(payload).encode())):
            res = vp.resolve_doi("10.1/x")
        self.assertEqual(res["status"], "resolved")
        self.assertEqual(res["meta"]["title"], "Registered Title")

    def test_resolve_404_is_not_found(self):
        with mock.patch.object(vp.urllib.request, "urlopen",
                               side_effect=urllib.error.HTTPError("u", 404, "nf", {}, None)):
            res = vp.resolve_doi("10.1/fake")
        self.assertEqual(res["status"], "not_found")

    def test_resolve_network_error_never_invalid(self):
        with mock.patch.object(vp.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("down")), \
             mock.patch.object(vp.time, "sleep"):
            res = vp.resolve_doi("10.1/x")
        self.assertEqual(res["status"], "error")

    def test_verify_entry_semantics(self):
        with mock.patch.object(vp, "resolve_doi",
                               return_value={"status": "not_found", "meta": None,
                                             "error": "404"}):
            entry, _ = vp.verify_doi_entry("10.1/fake", "cli")
        self.assertEqual(entry["verdict"], "invalid")
        with mock.patch.object(vp, "resolve_doi",
                               return_value={"status": "resolved",
                                             "meta": {"title": "T", "journal": "J",
                                                      "year": "2020", "authors": []},
                                             "error": ""}):
            entry, _ = vp.verify_doi_entry("10.1/ok", "cli")
        # existence without claims is unknown, never dressed as correct
        self.assertEqual(entry["verdict"], "unknown")
        self.assertTrue(entry["resolved"])
        self.assertIn("No claimed metadata", entry["details"])


class TestDeltaAudit(_Reset):
    """v2.8.0 delta audit against a baseline working-paper."""

    def _baseline(self, path):
        path.write_text(json.dumps({"citations": [
            {"pmid": "123", "verdict": {"final": "correct"}, "retraction": {"flagged": False}},
            {"pmid": "999", "verdict": {"final": "invalid"}, "retraction": {"flagged": False}},
        ]}), encoding="utf-8")

    def test_newly_retracted_including_new_entries(self):
        with tempfile.TemporaryDirectory() as td:
            bp = Path(td) / "b.json"
            self._baseline(bp)
            results = [{"pmid": "123", "verdict": "correct", "retracted": False},
                       {"pmid": "24476887", "verdict": "partial", "retracted": True,
                        "title": "STAP", "retraction_note": "n"}]
            d = vp.diff_against_baseline(results, str(bp))
        self.assertEqual(d["counts"]["newly_retracted"], 1)
        self.assertEqual(d["newly_retracted"][0]["key"], "24476887")

    def test_dropped_and_counts(self):
        with tempfile.TemporaryDirectory() as td:
            bp = Path(td) / "b.json"
            self._baseline(bp)
            results = [{"pmid": "123", "verdict": "correct", "retracted": False}]
            d = vp.diff_against_baseline(results, str(bp))
        self.assertEqual(d["dropped"], [{"key": "999"}])
        self.assertEqual(d["counts"]["dropped"], 1)

    def test_degraded_and_improved(self):
        with tempfile.TemporaryDirectory() as td:
            bp = Path(td) / "b.json"
            bp.write_text(json.dumps({"citations": [
                {"pmid": "1", "verdict": {"final": "correct"}, "retraction": {"flagged": False}},
                {"pmid": "2", "verdict": {"final": "invalid"}, "retraction": {"flagged": False}},
            ]}), encoding="utf-8")
            results = [{"pmid": "1", "verdict": "mismatch", "retracted": False},
                       {"pmid": "2", "verdict": "correct", "retracted": False}]
            d = vp.diff_against_baseline(results, str(bp))
        self.assertEqual(d["counts"]["degraded"], 1)
        self.assertEqual(d["counts"]["improved"], 1)


class TestAttackRegressions(_Reset):
    """v2.8.0 strict-round attacker findings — locked as regressions."""

    def test_authors_only_correct_claim_not_mismatch(self):
        # attacker P0: title omitted, authors correct -> was accused "mismatch"
        claimed = {"claimed_title": "", "claimed_authors": ["Gattorno", "Hofer"],
                   "claimed_journal": "", "claimed_year": ""}
        actual = {"title": "", "authors": ["Gattorno M", "Hofer M"],
                  "journal": "", "pubdate": ""}
        r = vp.cross_check_citation(claimed, actual)
        self.assertEqual(r["verdict"], "correct")
        self.assertIn("all comparable fields match", r["details"])

    def test_authors_only_wrong_journal_partial_not_mismatch(self):
        claimed = {"claimed_title": "", "claimed_authors": ["Gattorno"],
                   "claimed_journal": "Wrong Journal", "claimed_year": ""}
        actual = {"title": "", "authors": ["Gattorno M"], "journal": "Right J",
                  "pubdate": ""}
        r = vp.cross_check_citation(claimed, actual)
        self.assertEqual(r["verdict"], "partial")
        self.assertIn("journal differs", r["details"])

    def test_family_given_comma_authors_match(self):
        # attacker P2: "Gattorno, Marco" claims never matched
        claimed = {"claimed_title": "", "claimed_authors": ["Gattorno, Marco"],
                   "claimed_journal": "", "claimed_year": ""}
        actual = {"title": "", "authors": ["Gattorno M"], "journal": "", "pubdate": ""}
        r = vp.cross_check_citation(claimed, actual)
        self.assertTrue(r["author_match"])

    def test_doi_query_string_stripped(self):
        # attacker P1: utm params on doi.org links framed real DOIs as fake
        self.assertEqual(vp._clean_doi("10.1038/nature12968?utm_source=twitter"),
                         "10.1038/nature12968")

    def test_cjk_trailing_punctuation_stripped(self):
        self.assertEqual(vp._clean_doi("10.1038/abc。"), "10.1038/abc")
        self.assertEqual(vp._clean_doi("10.1038/abc]"), "10.1038/abc")

    def test_malformed_claims_shapes(self):
        # attacker P2: dict/int top-level and items must not traceback
        import subprocess
        script = Path(__file__).resolve().parent.parent / "scripts" / "verify_pmids.py"
        for bad in ('{"pmid":"1","title":"x"}', "[1,2,3]",
                    '[{"pmid":"31018962","title":123}]'):
            out = subprocess.run([sys.executable, str(script), "--claims", bad,
                                  "--no-cache"], capture_output=True, text=True,
                                 timeout=60)
            self.assertNotIn("Traceback", out.stderr, bad)


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


class TestV290FirstClassDoi(_Reset):
    """v2.9.0 DOI→PMID linking, parallel workers, CSV export."""

    def test_resolved_doi_links_to_pmid(self):
        with mock.patch.object(vp, "resolve_doi",
                               return_value={"status": "resolved",
                                             "meta": {"title": "CR T", "journal": "CR J",
                                                      "year": "2020", "authors": [],
                                                      "retracted": False,
                                                      "retraction_note": ""},
                                             "error": ""}), \
             mock.patch.object(vp, "find_pmid_by_doi", return_value="24476887"), \
             mock.patch.object(vp, "fetch_summaries",
                               return_value={"24476887": {
                                   "valid": True, "title": "PubMed T", "authors": [],
                                   "journal": "Nature", "pubdate": "2014",
                                   "doi": "10.1038/nature12968", "retracted": True,
                                   "retraction_note": "pubtype", "source": "ncbi"}}):
            entry, _ = vp.verify_doi_entry("10.1038/nature12968", "cli")
        self.assertEqual(entry["pmid"], "24476887")
        self.assertEqual(entry["title"], "PubMed T")
        self.assertEqual(entry["meta_source"], "pubmed")
        self.assertTrue(entry["retracted"])

    def test_unlinked_doi_stays_existence_only(self):
        with mock.patch.object(vp, "resolve_doi",
                               return_value={"status": "resolved",
                                             "meta": {"title": "T", "journal": "J",
                                                      "year": "2020", "authors": [],
                                                      "retracted": False,
                                                      "retraction_note": ""},
                                             "error": ""}), \
             mock.patch.object(vp, "find_pmid_by_doi", return_value=""):
            entry, _ = vp.verify_doi_entry("10.1/unlinked", "cli")
        self.assertEqual(entry["pmid"], "")
        self.assertEqual(entry["verdict"], "unknown")

    def test_find_pmid_by_doi_failure_returns_empty(self):
        with mock.patch.object(vp.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("down")), \
             mock.patch.object(vp.time, "sleep"):
            self.assertEqual(vp.find_pmid_by_doi("10.1/x"), "")

    def test_csv_export_content(self):
        results = [
            {"pmid": "123", "verdict": "correct", "valid": True, "retracted": False,
             "doi_splice_suspect": False, "claimed_title": "c", "title": "T",
             "journal": "J", "pubdate": "2020", "meta_source": "ncbi",
             "source_file": "f", "confidence": 1.0, "details": "ok"},
            {"pmid": "", "doi": "10.1/x", "verdict": "unknown", "valid": True,
             "retracted": False, "doi_splice_suspect": False, "claimed_title": "",
             "title": "R", "journal": "", "pubdate": "2021", "meta_source": "crossref",
             "source_file": "cli", "details": "resolves"},
        ]
        csv_text = vp.generate_csv_report(results)
        rows = list(csv.reader(csv_text.lstrip("\ufeff").splitlines()))
        self.assertEqual(rows[0][:3], ["key", "kind", "verdict"])
        self.assertEqual(rows[1][0], "123")
        self.assertEqual(rows[2][0], "10.1/x")
        self.assertEqual(rows[2][1], "doi")

    def test_workers_doi_pipeline_smoke(self):
        import subprocess
        script = Path(__file__).resolve().parent.parent / "scripts" / "verify_pmids.py"
        with tempfile.TemporaryDirectory() as td:
            out = subprocess.run(
                [sys.executable, str(script), "--dois",
                 "10.9999/fake.1,10.9999/fake.2", "--workers", "2", "--no-cache",
                 "--export-csv", str(Path(td) / "t.csv")],
                capture_output=True, text=True, timeout=180)
            csv_text = (Path(td) / "t.csv").read_text(encoding="utf-8")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(csv_text.count("10.9999/fake"), 2)


class TestArxiv(_Reset):
    """v3.0.0 arXiv ID verification."""

    def test_extract_patterns_and_bad_month_kept(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.md"
            p.write_text("arXiv:2401.12345 and https://arxiv.org/abs/cs/0211004 "
                         "and arXiv:2413.99999 (bad month kept for flagging)",
                         encoding="utf-8")
            ids = [a for a, _ in vp.extract_arxivs_from_file(str(p))]
        # order follows pattern groups (anchored url/prefix first, bare second)
        self.assertEqual(sorted(ids), ["2401.12345", "2413.99999", "cs/0211004"])

    def test_shape_rejects_bad_month(self):
        e, _ = vp.verify_arxiv_entry("2413.99999", "cli")
        self.assertEqual(e["verdict"], "invalid")
        self.assertIn("shape", e["details"])

    def test_not_found_is_invalid(self):
        import io
        empty_feed = b"""<?xml version='1.0'?><feed xmlns="http://www.w3.org/2005/Atom"><title>arXiv Query</title><opensearch:totalResults xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">0</opensearch:totalResults></feed>"""
        r = mock.mock_open(read_data=empty_feed).return_value
        with mock.patch.object(vp.urllib.request, "urlopen", return_value=r), \
             mock.patch.object(vp.time, "sleep"):
            e, _ = vp.verify_arxiv_entry("2401.99999", "cli")
        self.assertEqual(e["verdict"], "invalid")
        self.assertIn("fabrication", e["details"])

    def test_resolved_attaches_registered_metadata(self):
        atom = b"""<?xml version='1.0'?><feed xmlns="http://www.w3.org/2005/Atom"><title>q</title><entry><title>Distributionally Robust Combining</title><published>2024-01-05T00:00:00Z</published><author><name>A Author</name></author></entry></feed>"""
        with mock.patch.object(vp.urllib.request, "urlopen", return_value=_ok(atom)):
            e, a = vp.verify_arxiv_entry("2401.12345", "cli")
        self.assertEqual(e["verdict"], "unknown")   # existence, never a match
        self.assertTrue(e["resolved"])
        self.assertEqual(e["title"], "Distributionally Robust Combining")
        self.assertEqual(e["pubdate"], "2024")

    def test_non_atom_200_is_unknown_never_fabricated(self):
        # review P0: captive-portal/proxy HTML 200 would accuse every real ID
        html = b"<html><body>login</body></html>"
        r = mock.mock_open(read_data=html).return_value
        with mock.patch.object(vp.urllib.request, "urlopen", return_value=r), \
             mock.patch.object(vp.time, "sleep"):
            e, _ = vp.verify_arxiv_entry("2402.00002", "cli")
        self.assertEqual(e["verdict"], "unknown")
        self.assertNotIn("fabrication", e["details"])

    def test_error_entry_is_unknown_never_crash(self):
        # review P1: real API returns 200 + <title>Error</title> (no published)
        atom = b"""<?xml version='1.0'?><feed xmlns="http://www.w3.org/2005/Atom"><title>q</title><entry><title>Error</title><summary>bad id</summary><author><name>api</name></author></entry></feed>"""
        r = mock.mock_open(read_data=atom).return_value
        with mock.patch.object(vp.urllib.request, "urlopen", return_value=r), \
             mock.patch.object(vp.time, "sleep"):
            e, _ = vp.verify_arxiv_entry("a/0309136", "cli")
        self.assertEqual(e["verdict"], "unknown")

    def test_trailing_dot_extraction_stripped(self):
        # review P1: sentence-final period framed real papers as fabricated
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.md"
            p.write_text("see https://arxiv.org/abs/1706.03762. and arXiv:1706.03762v2.",
                         encoding="utf-8")
            ids = [a for a, _ in vp.extract_arxivs_from_file(str(p))]
        self.assertIn("1706.03762", ids)
        self.assertIn("1706.03762v2", ids)
        self.assertFalse(any(i.endswith(".") for i in ids))

    def test_arxiv_prefix_normalized(self):
        # review P1: CLI paste form arXiv:... must reach the API without prefix
        e, _ = vp.verify_arxiv_entry("arXiv:1706.03762", "cli")
        self.assertEqual(e["arxiv_id"], "1706.03762")

    def test_network_error_never_invalid(self):
        with mock.patch.object(vp.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("down")), \
             mock.patch.object(vp.time, "sleep"):
            e, _ = vp.verify_arxiv_entry("2401.12345", "cli")
        self.assertEqual(e["verdict"], "unknown")
        self.assertTrue(e.get("network_error"))


class TestArxivClaims(_Reset):
    """v3.1.0 arXiv claims verification e2e (unit-level)."""

    def _atom(self):
        return mock.mock_open(read_data=b"""<?xml version='1.0'?><feed xmlns="http://www.w3.org/2005/Atom"><title>q</title><entry><title>Attention Is All You Need</title><published>2017-06-12T00:00:00Z</published></entry></feed>""").return_value

    def test_correct_match(self):
        with mock.patch.object(vp.urllib.request, "urlopen", return_value=self._atom()), \
             mock.patch.object(vp.time, "sleep"):
            e, _ = vp.verify_arxiv_entry("1706.03762", "cli",
                claimed={"claimed_title": "Attention Is All You Need", "claimed_year": "2017"})
        self.assertEqual(e["verdict"], "correct")
        self.assertEqual(e.get("claimed_title"), "Attention Is All You Need")
        self.assertEqual(e["fields"]["year"], True)

    def test_mismatch_detected(self):
        with mock.patch.object(vp.urllib.request, "urlopen", return_value=self._atom()), \
             mock.patch.object(vp.time, "sleep"):
            e, _ = vp.verify_arxiv_entry("1706.03762", "cli",
                claimed={"claimed_title": "Completely Different Paper About CNN", "claimed_year": "2017"})
        self.assertEqual(e["verdict"], "mismatch")

    def test_year_mismatch_shown_not_verdict_breaking(self):
        with mock.patch.object(vp.urllib.request, "urlopen", return_value=self._atom()), \
             mock.patch.object(vp.time, "sleep"):
            e, _ = vp.verify_arxiv_entry("1706.03762", "cli",
                claimed={"claimed_title": "Attention Is All You Need", "claimed_year": "1999"})
        self.assertEqual(e["verdict"], "correct")
        self.assertEqual(e["fields"]["year"], False)

    def test_csv_arxiv_only_row_collected(self):
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "c.csv"
            fp.write_text("arxiv_id,title,year\n1706.03762,Attention Is All You Need,2017\n",
                          encoding="utf-8")
            claims = vp._load_csv_claims(str(fp))
            rows = getattr(vp._load_csv_claims, "arxiv_rows", {})
        self.assertEqual(rows.get("1706.03762", {}).get("claimed_title"),
                         "Attention Is All You Need")


class TestV330(_Reset):
    """v3.3.0: arXiv preprint↔published cross-check, abbreviation-safe
    context parsing, progress wiring, readiness extension — plus a
    test-hygiene lock (no duplicate class definitions, the v3.2.0 lesson)."""

    def _atom_doi(self, with_doi=True):
        doi = ("<arxiv:doi>10.1371/journal.pone.0239699</arxiv:doi>"
               if with_doi else "")
        return mock.mock_open(read_data=b"""<?xml version='1.0'?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"><title>q</title><entry><title>City size and the spreading of COVID-19 in Brazil</title><published>2020-05-28T00:00:00Z</published><author><name>A Author</name></author>""" + doi.encode() + b"</entry></feed>").return_value

    def _verify(self, atom, arxiv_id="2005.13892", claimed=None, link=None):
        with mock.patch.object(vp.urllib.request, "urlopen", return_value=atom), \
             mock.patch.object(vp.time, "sleep"):
            return vp.verify_arxiv_entry(arxiv_id, "cli", claimed=claimed,
                                         link_pmid=link)

    def test_doi_match_reported_as_evidence(self):
        e, _ = self._verify(self._atom_doi(), claimed={
            "claimed_title": "City size and the spreading of COVID-19 in Brazil",
            "claimed_doi": "10.1371/journal.pone.0239699"})
        self.assertEqual(e["verdict"], "correct")
        self.assertIs(e["fields"]["doi"], True)
        self.assertIn("matches the arXiv-registered", e["details"])

    def test_doi_mismatch_caps_correct_to_partial(self):
        e, _ = self._verify(self._atom_doi(), claimed={
            "claimed_title": "City size and the spreading of COVID-19 in Brazil",
            "claimed_doi": "10.1234/fabricated.doi"})
        self.assertEqual(e["verdict"], "partial")
        self.assertIs(e["fields"]["doi"], False)
        self.assertIn("pairing mismatch", e["details"])

    def test_doi_mismatch_without_title_stays_unknown(self):
        # the ladder stays honest: a DOI mismatch alone never manufactures
        # a partial — only caps an existing correct
        e, _ = self._verify(self._atom_doi(), claimed={
            "claimed_doi": "10.1234/fabricated.doi"})
        self.assertEqual(e["verdict"], "unknown")
        self.assertIs(e["fields"]["doi"], False)

    def test_bare_preprint_surfaces_version_of_record(self):
        seen = []
        e, _ = self._verify(self._atom_doi(),
                            link=lambda d: (seen.append(d), "32966344")[1])
        self.assertEqual(seen, ["10.1371/journal.pone.0239699"])
        self.assertEqual(e["pmid"], "32966344")
        self.assertIn("Version of record: DOI 10.1371/journal.pone.0239699", e["details"])
        self.assertIn("PMID 32966344", e["details"])

    def test_linker_failure_never_breaks_verdict(self):
        # the DOI hint survives (it comes from the arXiv record itself);
        # only the PMID enrichment is dropped, and the verdict never moves
        def boom(d):
            raise OSError("epmc down")
        e, _ = self._verify(self._atom_doi(), link=boom)
        self.assertEqual(e["verdict"], "unknown")
        self.assertEqual(e.get("pmid", ""), "")
        self.assertIn("Version of record: DOI 10.1371/journal.pone.0239699", e["details"])
        self.assertNotIn("PMID", e["details"])

    def test_no_registered_doi_no_link_no_hint(self):
        called = []
        e, _ = self._verify(self._atom_doi(with_doi=False), arxiv_id="1706.03762",
                            link=lambda d: called.append(d))
        self.assertEqual(called, [])
        self.assertNotIn("Version of record", e["details"])

    def test_readiness_counts_arxiv_doi_mismatch(self):
        ready, line = vp.readiness_summary(
            {"total": 1, "correct": 0, "mismatch": 0, "partial": 0,
             "invalid": 0, "unknown": 0, "arxiv_doi_mismatch": 1})
        self.assertFalse(ready)
        self.assertIn("arXiv-DOI pairing mismatch", line)

    def test_context_us_title_not_truncated(self):
        c = vp.parse_citation_context(
            "Smith J, Jones B. Blood pressure in the U.S. population: NHANES "
            "2015. Hypertension. 2015;66(4):123-9. PMID: 26133316")
        self.assertEqual(c["claimed_title"],
                         "Blood pressure in the U.S. population: NHANES 2015")
        self.assertEqual(c["claimed_journal"], "Hypertension")

    def test_context_eg_vs_st_not_truncated(self):
        c = vp.parse_citation_context(
            "Doe J. Outcomes vs. expectations in St. John's wort trials, e.g. "
            "dosage effects. J Altern Med. 2019. PMID: 31000001")
        self.assertEqual(c["claimed_title"],
                         "Outcomes vs. expectations in St. John's wort trials, e.g. dosage effects")

    def test_context_et_al_still_splits_authors(self):
        # deliberate non-guard: "et al." terminates the author segment
        c = vp.parse_citation_context(
            "Ravelli A, Martini A, et al. Felty syndrome. Ann Rheum Dis. "
            "2003;62(6):571. PMID: 12730673")
        self.assertEqual(c["claimed_title"], "Felty syndrome")
        self.assertIn("Ravelli", c["claimed_authors"])

    def test_csv_arxiv_row_carries_doi(self):
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "c.csv"
            fp.write_text("arxiv_id,title,doi\n"
                          "2005.13892,City size,10.1371/journal.pone.0239699\n",
                          encoding="utf-8")
            vp._load_csv_claims(str(fp))
            rows = getattr(vp._load_csv_claims, "arxiv_rows", {})
        self.assertEqual(rows["2005.13892"]["claimed_doi"],
                         "10.1371/journal.pone.0239699")

    def test_json_arxiv_claim_helper_carries_doi(self):
        h = vp._arxiv_claim_from_item(
            {"arxiv_id": "2005.13892", "title": "T", "year": "2020",
             "doi": "10.1371/journal.pone.0239699"})
        self.assertEqual(h["claimed_doi"], "10.1371/journal.pone.0239699")
        p = vp._pmid_claim_from_item({"pmid": "123", "title": "T"})
        self.assertEqual(p["claimed_doi"], "")

    def test_verify_doi_entry_called_once_per_doi(self):
        # v3.3.0 P2 fix: the DOI loop used to call verify_doi_entry twice
        # per entry (first result silently discarded — latent hazard)
        src = Path(__file__).with_name("..") .joinpath("scripts", "verify_pmids.py").resolve().read_text(encoding="utf-8")
        self.assertEqual(src.count("entry, audit_item = verify_doi_entry("), 1)

    def test_markdown_deltas_no_nameerror(self):
        # strict-round P0 lock: the v3.2.0 markdown deltas block called an
        # undefined esc_md — crashed (NameError) on --diff + markdown when
        # newly_retracted entries existed (deterministic offline repro)
        deltas = {"counts": {"newly_retracted": 1, "degraded": 0, "improved": 0,
                             "new": 0, "dropped": 0},
                  "newly_retracted": [{"key": "123|456", "title": "Retracted | paper"}],
                  "degraded": [], "improved": [], "new": [], "dropped": []}
        md = vp.generate_markdown_report([], {"total": 0}, "test", deltas=deltas)
        self.assertIn("Delta vs baseline", md)
        self.assertIn("123\\|456", md)   # pipe escaped — table-safe

    def test_delta_arxiv_unknown_twin_not_degraded(self):
        # an enriched bare-preprint twin (arXiv entry linked to a PMID that
        # the baseline knows as a correct PMID citation) is enrichment, not
        # a degradation — must not emit a false "degraded" delta
        baseline = {"citations": [{"pmid": "32966344", "verdict": "correct"}]}
        import json as _json, tempfile as _tf, os as _os
        with _tf.TemporaryDirectory() as td:
            bp = _os.path.join(td, "base.json")
            open(bp, "w", encoding="utf-8").write(_json.dumps(baseline))
            current = [{"pmid": "32966344", "entry_kind": "arxiv",
                        "verdict": "unknown", "network_error": False,
                        "arxiv_id": "2005.13892"}]
            d = vp.diff_against_baseline(current, bp)
        self.assertEqual(d.get("degraded", []), [])

    def test_no_duplicate_test_class_definitions(self):
        # v3.2.0 lesson: a shadowed class silently skips its tests
        classes = re.findall(r"^class (\w+)\(", 
                             Path(__file__).read_text(encoding="utf-8"), re.M)
        self.assertEqual(len(classes), len(set(classes)),
                         f"duplicate test classes: {classes}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
