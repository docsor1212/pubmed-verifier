#!/usr/bin/env python3
"""
PMID Citation Verifier -- PubMed E-utilities API v3.1.0
Verifies existence, metadata cross-check, and optional content-matching of PMID citations.
Three citation types in one audit (v3.0.0): PMIDs, DOIs, and arXiv IDs.
arXiv IDs (arXiv:2401.12345 / arxiv.org/abs/...) are verified against the
official arXiv API — a nonexistent ID is a fabrication signal; a resolving
one attaches the registered title/year (verdict stays unknown until claims
are supplied). Timely because arXiv penalizes submissions containing
hallucinated or unverified references.
DOI entries are first-class (v2.9.0): a resolved DOI is linked back to its
PMID via the Europe PMC DOI field query, attaching the full PubMed record
(metadata, retraction pubtype, cache coverage). Linking upgrades the record,
never the verdict — it stays unknown until claimed metadata is supplied.
DOI resolution runs in a thread pool (--workers, default 4).
Retraction detection for EVERY PMID (v2.7.0): the registry's own publication
type ("Retracted Publication", present in both NCBI esummary and Europe PMC)
flags retracted papers with no DOI and no --verify-doi needed -- and the flag
survives the cache (schema v3). Crossref updated-by remains the detail source
(retraction-notice DOI) when --verify-doi is on.
Verified-bibliography export (v2.7.0): --export-bibtex writes correct
entries as @article, partial entries commented out with their divergence
note, everything else excluded and counted.
Submission-readiness summary (v2.7.0): every report leads with one line --
SUBMISSION READY, or NOT READY with per-problem counts.
Retraction detection (v2.3.0): with --verify-doi, papers Crossref lists as
RETRACTED are flagged and their verdict capped at partial (human review required).
DOI↔PMID cross-check (v2.4.0): a claimed DOI that differs from the DOI
registered for the PMID is a splice/fabrication signal (verdict capped at
partial). Journal matching understands NLM-style abbreviations ("N Engl J Med"
~ "New England Journal of Medicine") via in-order word-prefix equivalence.

Five-state verdict:
  correct  -- PMID exists AND matches claimed paper
  mismatch -- PMID exists but points to a DIFFERENT paper (AI hallucination!)
  partial  -- Some metadata matches (e.g. author+journal but title differs)
  invalid  -- PMID not found in PubMed
  unknown  -- Insufficient claimed metadata for cross-check, OR both data
              sources unreachable (a network failure is never reported as
              "PMID not found")

v2.2.0 network hardening (ported from cite-holmes field-proven lessons):
  --ncbi-api-key  NCBI E-utilities API key (env NCBI_API_KEY): rate ceiling
                  3 -> 10 req/s, batch interval 0.4s -> 0.12s (~3x faster)
  --meta-source   auto (default: NCBI with per-batch Europe PMC fallback) /
                  ncbi / europepmc; metadata origin reported per entry
  --mailto        Crossref polite pool (?mailto=) + NCBI tool/email etiquette
  429 Retry-After backoff (clamped 1-5s), 403/406 User-Agent rotation, and a
  per-host circuit breaker (2 call-level transport failures -> skip remaining
  calls with an actionable message; HTTP status errors never trip it).

Usage:
  python3 verify_pmids.py --source <file_or_dir> [--match-keywords] [--output report.html]
  python3 verify_pmids.py --pmids 31018962,22213727
  python3 verify_pmids.py --claims-file claims.json --output report.html

Exit codes: 0 = no problems found; 1 = invalid/mismatch citations found;
            2 = verification incomplete (data sources unreachable).
"""

import argparse
import concurrent.futures
import csv
import io
import xml.etree.ElementTree as ET
import hashlib
import html
import http.client
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
import urllib.parse
from difflib import SequenceMatcher
from pathlib import Path
from collections import defaultdict


# ── Options & HTTP core (v2.2.0 network hardening) ──

_OPTS = {"ncbi_key": "", "mailto": "", "meta_source": "auto", "timeout": 20}
_TOOL_VERSION = "3.1.0"
_UA_TOOL = "pubmed-verifier/3.1 (+citation verifier; stdlib-only)"
_UA_BROWSER = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_CIRCUIT_THRESHOLD = 2  # call-level consecutive transport failures per host
_BREAKER_COOLDOWN_S = 30  # half-open: after this, admit one probe call
_HOST_FAILS = defaultdict(int)
_HOST_OPENED_AT = {}


class CircuitOpenError(RuntimeError):
    """Host circuit breaker is open after repeated transport failures."""


def _host_key(url: str) -> str:
    """'https://eutils.ncbi.nlm.nih.gov' from a full URL (scheme://host granularity)."""
    parts = url.split("/")
    return "/".join(parts[:3]) if len(parts) > 2 else url


def _api_get(url: str, max_retries: int = 3, timeout=None) -> bytes:
    """HTTP GET hardened for the field: UA rotation on 403/406, Retry-After
    compliance on 429, per-host circuit breaker.

    Breaker accounting is per LOGICAL call (one exhausted call = +1), never
    per retry attempt -- per-attempt counting would open the breaker on the
    first slow call. HTTP status errors never count as transport failures.
    """
    timeout = _OPTS["timeout"] if timeout is None else timeout
    hk = _host_key(url)
    if _HOST_FAILS[hk] >= _CIRCUIT_THRESHOLD:
        opened_at = _HOST_OPENED_AT.get(hk)
        if opened_at is None:
            _HOST_OPENED_AT[hk] = time.time()
            raise CircuitOpenError(
                f"{hk} 已连续 {_CIRCUIT_THRESHOLD} 次传输失败并熔断，跳过本批次后续请求。"
                f"建议：确认网络/代理后重试；或 --timeout {_OPTS['timeout'] * 2:.0f} 放宽超时；"
                f"或 --meta-source europepmc 改走 Europe PMC 兜底源。")
        if time.time() - opened_at >= _BREAKER_COOLDOWN_S:
            # half-open: admit one probe call; success resets below
            _HOST_FAILS[hk] = _CIRCUIT_THRESHOLD - 1
            _HOST_OPENED_AT.pop(hk, None)
        else:
            raise CircuitOpenError(
                f"{hk} 熔断中（{_BREAKER_COOLDOWN_S:.0f}s 冷却后自动试探）。"
                f"建议：--meta-source europepmc 改走 Europe PMC 兜底源。")
    last_err = None
    transport = False
    for attempt in range(max_retries):
        ua = _UA_TOOL if attempt == 0 else _UA_BROWSER  # 403/406 → 换浏览器 UA 重试
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            _HOST_FAILS[hk] = 0  # success resets the breaker
            _HOST_OPENED_AT.pop(hk, None)
            return body
        except urllib.error.HTTPError as e:
            last_err, transport = e, False  # HTTP responses never trip the breaker
            if e.code == 429 and attempt < max_retries - 1:
                wait = 2.0
                try:
                    wait = min(5.0, max(1.0, float(e.headers.get("Retry-After", 2.0))))
                except (TypeError, ValueError):
                    pass
                time.sleep(wait)
            elif e.code in (403, 406) and attempt < max_retries - 1:
                time.sleep(0.8)
            elif e.code >= 500 and attempt < max_retries - 1:
                time.sleep(2 ** attempt)      # transient server errors: back off
            elif e.code < 500:
                raise                          # deterministic 4xx: no point retrying
        except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as e:
            last_err, transport = e, True
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    if transport:
        _HOST_FAILS[hk] += 1
    if last_err is None:  # e.g. max_retries=0 — never raise None
        raise RuntimeError(f"no attempts made for {url}")
    raise last_err


def _ncbi_params(extra: dict = None) -> str:
    """E-utilities common params: api_key (when configured) + tool/email etiquette."""
    params = {"tool": "pubmed-verifier"}
    params.update(extra or {})
    if _OPTS["ncbi_key"]:
        params["api_key"] = _OPTS["ncbi_key"]
    if _OPTS["mailto"]:
        params["email"] = _OPTS["mailto"]
    return urllib.parse.urlencode(params)


def _parse_esummary(batch: list, data: dict) -> dict:
    """Map an esummary JSON response onto {pmid: metadata_dict}.

    A 200 response whose body is valid JSON but lacks the `result` map is a
    source anomaly (schema change / silently degraded service), NOT an
    answer: those are marked network_error so they are never classified as
    "PMID not found" and never poison the cache. Individual PMIDs missing
    from a well-formed `result` map ARE an authoritative not-found.
    """
    out = {}
    if not isinstance(data, dict) or "result" not in data:
        for pmid in batch:
            out[pmid] = {"valid": False, "network_error": True,
                         "error": f"esummary 响应异常（缺少 result 结构）：{str(data)[:80]}"}
        return out
    for pmid in batch:
        if pmid in data.get("result", {}):
            article = data["result"][pmid]
            if "error" not in article:
                # Retraction signal from the registry itself (v2.7.0):
                # "Retracted Publication" marks the retracted paper;
                # "Retraction of Publication" (the notice) does NOT flag.
                pubtypes = [" ".join(str(p).split()).lower() for p in article.get("pubtype", []) or []]
                retracted = "retracted publication" in pubtypes
                # Registered DOI: articleids[] is authoritative. elocationid
                # can be a composite string (e.g. eLife "pii: X. doi: Y")
                # that is NOT a bare DOI -- parsing it whole caused false
                # splice flags on perfectly correct DOIs (v2.4.0 review P0,
                # real-data tested on PMID 42770840).
                doi = ""
                for rec in article.get("articleids", []) or []:
                    if isinstance(rec, dict) and rec.get("idtype") == "doi":
                        doi = str(rec.get("value", "") or "")
                        break
                if not doi:
                    m = re.search(r'doi:\s*(\S+)', article.get("elocationid", "") or "")
                    doi = m.group(1) if m else ""
                out[pmid] = {
                    "title": article.get("title", ""),
                    "authors": [a.get("name", "") for a in article.get("authors", [])],
                    "journal": article.get("source", ""),
                    "pubdate": article.get("pubdate", ""),
                    "volume": article.get("volume", ""),
                    "pages": article.get("pages", ""),
                    "doi": doi,
                    "retracted": retracted,
                    "retraction_note": ("PubMed publication type: Retracted Publication"
                                        if retracted else ""),
                    "valid": True,
                    "source": "ncbi",
                }
            else:
                out[pmid] = {"valid": False, "error": article["error"], "source": "ncbi"}
        else:
            out[pmid] = {"valid": False, "error": "PMID not found in API response", "source": "ncbi"}
    return out


def fetch_summaries(pmids: list, batch_size: int = 50) -> dict:
    """Fetch article summaries from PubMed. Primary source: NCBI esummary.

    With --meta-source auto (default), a batch whose NCBI call FAILS
    (transport error, circuit breaker, exhausted retries) is retried against
    Europe PMC before being reported as unreachable. A definite "not found"
    answer from NCBI is authoritative and never re-queried.
    Entries that could not be verified carry network_error=True and are
    classified as unknown -- a network failure is never "invalid".
    """
    if _OPTS["meta_source"] == "europepmc":
        # Explicitly forced: never touch NCBI (deterministic for testing and
        # for users who must avoid NCBI altogether).
        return fetch_summaries_europepmc(pmids, batch_size=min(batch_size, 25),
                                         primary_label="Europe PMC")
    results = {}
    batch_interval = 0.12 if _OPTS["ncbi_key"] else 0.4
    use_fallback = _OPTS["meta_source"] == "auto"
    for i in range(0, len(pmids), batch_size):
        batch = pmids[i:i + batch_size]
        ids_str = ",".join(batch)
        url = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
               + _ncbi_params({"db": "pubmed", "id": ids_str, "retmode": "json"}))
        try:
            data = json.loads(_api_get(url))
            results.update(_parse_esummary(batch, data))
        except Exception as e:
            if use_fallback:
                results.update(fetch_summaries_europepmc(batch))
            else:
                for pmid in batch:
                    results[pmid] = {"valid": False, "error": str(e), "network_error": True}
        time.sleep(batch_interval)
    return results


def fetch_summaries_europepmc(pmids: list, batch_size: int = 25,
                              primary_label: str = "NCBI 与 Europe PMC") -> dict:
    """Europe PMC fallback source (free, no key, mirrors PubMed).

    Returns an entry for every requested PMID: matched records map to the
    same metadata shape (source='europepmc'); records absent from a
    well-formed reply are reported not-found via this source; a failed or
    malformed call marks the whole batch network_error=True (unknown, never
    invalid).
    """
    results = {}
    for i in range(0, len(pmids), batch_size):
        batch = pmids[i:i + batch_size]
        q = " OR ".join(f"EXT_ID:{p}" for p in batch)
        params = urllib.parse.urlencode({
            "query": f"({q}) AND SRC:MED", "format": "json",
            "pageSize": str(batch_size), "resultType": "lite"})
        url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?{params}"
        got = {}
        try:
            data = json.loads(_api_get(url))
            if not isinstance(data, dict) or "resultList" not in data:
                raise ValueError(f"响应缺少 resultList 结构：{str(data)[:80]}")
            for h in data.get("resultList", {}).get("result", []):
                pmid = str(h.get("id", "")).strip()
                if not pmid.isdigit():
                    continue
                epmc_retracted = "retracted publication" in str(h.get("pubType", "") or "").lower()
                got[pmid] = {
                    "title": h.get("title", ""),
                    "authors": [a.strip() for a in (h.get("authorString") or "").split(",") if a.strip()],
                    "journal": h.get("journalTitle", ""),
                    "pubdate": h.get("pubYear", ""),
                    "volume": h.get("journalVolume", ""),
                    "pages": h.get("pageInfo", ""),
                    "doi": h.get("doi", "") or "",
                    "retracted": epmc_retracted,
                    "retraction_note": ("Europe PMC publication type: retracted publication"
                                        if epmc_retracted else ""),
                    "valid": True,
                    "source": "europepmc",
                }
        except Exception as e:
            for pmid in batch:
                results[pmid] = {"valid": False, "network_error": True,
                                 "error": f"{primary_label} 均不可达（最后错误：{e}）。"
                                          f"未判定——网络故障不会判为「PMID不存在」"}
            continue
        for pmid in batch:
            if pmid in got:
                results[pmid] = got[pmid]
            else:
                results[pmid] = {"valid": False, "error": "PMID not found (europepmc)",
                                 "source": "europepmc"}
        time.sleep(0.35)  # be polite to EBI when walking large batches
    return results


def fetch_doi_metadata(doi: str) -> dict:
    """Fetch metadata from Crossref API by DOI. With --mailto set, requests
    join the Crossref polite pool (?mailto=) for more generous rate limits."""
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='')}"
    if _OPTS["mailto"]:
        url += f"?mailto={urllib.parse.quote(_OPTS['mailto'])}"
    try:
        data = json.loads(_api_get(url, timeout=15))
        msg = data.get("message", {})
        authors = []
        for a in msg.get("author", []):
            family = a.get("family", "")
            given = a.get("given", "")
            authors.append(f"{family} {given}".strip())
        year = ""
        pub_date = msg.get("published-print") or msg.get("published-online") or msg.get("created", {})
        parts = pub_date.get("date-parts", [[]])
        if parts and parts[0]:
            year = str(parts[0][0])
        # Retraction detection (v2.3.0, ported from cite-holmes): Crossref
        # lists withdrawal events in updated-by[]; any entry typed
        # "retraction" means the paper has been retracted. Corrections and
        # other update types do NOT count. (In filters the key is hyphenated
        # update-type; response items carry plain "type".)
        retracted, retraction_note = False, ""
        for u in msg.get("updated-by", []) or []:
            if not isinstance(u, dict):
                continue
            utype = str(u.get("type") or u.get("update-type") or "").lower()
            if utype == "retraction":
                retracted = True
                retraction_note = f"retracted by DOI {u.get('DOI', '?')}"
                break
        return {
            "valid": True,
            "title": msg.get("title", [""])[0] if msg.get("title") else "",
            "authors": authors,
            "journal": msg.get("container-title", [""])[0] if msg.get("container-title") else "",
            "pubdate": year,
            "doi": doi,
            "retracted": retracted,
            "retraction_note": retraction_note,
            "source": "crossref",
        }
    except Exception as e:
        return {"valid": False, "error": str(e)}


def search_pubmed(query: str, max_results: int = 5) -> list:
    """Search PubMed and return article summaries."""
    params = _ncbi_params({"db": "pubmed", "term": query, "retmode": "json",
                           "retmax": str(max_results)})
    url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?{params}"
    try:
        data = json.loads(_api_get(url, timeout=15))
        ids = data["esearchresult"]["idlist"]
        if ids:
            return [{"pmid": pid, **fetch_summaries([pid]).get(pid, {})} for pid in ids]
    except Exception:
        pass
    return []


# ── DOI-native verification (v2.8.0) ──

DOI_PATTERNS = [
    # anchored forms (url / doi:) — most reliable; ? cuts tracking params
    # (utm_source etc. on real-world doi.org links — v2.8.0 attacker P1)
    re.compile(r'(?:https?://(?:dx\.)?doi\.org/|doi:\s*)(10\.\d{4,9}/[^\s"\'<>),;\]?&=]+)', re.IGNORECASE),
    # bare 10.x/y — requires the 4-9-digit registrant segment, still noisy-prone:
    # strip trailing punctuation after match
    re.compile(r'\b(10\.\d{4,9}/[^\s"\'<>),;\]?&=]+)', re.IGNORECASE),
]


def extract_dois_from_file(filepath: str) -> list:
    """Extract (doi, context) pairs from a file (v2.8.0). Returns cleaned DOIs."""
    results = []
    try:
        text = Path(filepath).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return results
    seen = set()
    for pattern in DOI_PATTERNS:
        for m in pattern.finditer(text):
            doi = _clean_doi(m.group(1))
            if not doi or doi in seen or "/" not in doi:
                continue
            seen.add(doi)
            start = max(0, m.start() - 200)
            end = min(len(text), m.end() + 40)
            context = text[start:end].replace("\n", " ").strip()
            results.append((doi, context))
    return results


def resolve_doi(doi: str, timeout=None) -> dict:
    """Resolve a DOI against the Crossref works API (v2.8.0).

    Returns {'status': 'resolved'|'not_found'|'error', 'meta': dict|None,
    'error': str}. A 404 is an authoritative not-found (fabrication signal);
    any transport/other failure is an 'error' and never counts against the DOI.
    """
    url = f"https://api.crossref.org/works/{urllib.parse.quote(_clean_doi(doi), safe='')}"
    if _OPTS["mailto"]:
        url += f"?mailto={urllib.parse.quote(_OPTS['mailto'])}"
    try:
        data = json.loads(_api_get(url, timeout=timeout if timeout is not None else _OPTS["timeout"]))
        msg = data.get("message", {})
        year = ""
        pub_date = msg.get("published-print") or msg.get("published-online") or msg.get("created", {})
        parts = pub_date.get("date-parts", [[]])
        if parts and parts[0]:
            year = str(parts[0][0])
        # Retraction signal rides the same response (updated-by) — without it
        # the --dois pipeline would be blind to retractions (v2.8.0 review P1)
        retracted, retraction_note = False, ""
        for u in msg.get("updated-by", []) or []:
            if not isinstance(u, dict):
                continue
            utype = str(u.get("type") or u.get("update-type") or "").lower()
            if utype == "retraction":
                retracted = True
                retraction_note = f"retracted by DOI {u.get('DOI', '?')}"
                break
        meta = {
            "title": msg.get("title", [""])[0] if msg.get("title") else "",
            "journal": msg.get("container-title", [""])[0] if msg.get("container-title") else "",
            "year": year,
            "authors": [f"{a.get('family', '')} {a.get('given', '')}".strip()
                        for a in msg.get("author", [])],
            "type": msg.get("type", ""),
            "retracted": retracted,
            "retraction_note": retraction_note,
        }
        return {"status": "resolved", "meta": meta, "error": ""}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "not_found", "meta": None,
                    "error": "DOI not found in Crossref (404)"}
        return {"status": "error", "meta": None, "error": f"crossref HTTP {e.code}"}
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException,
            json.JSONDecodeError, ValueError, CircuitOpenError) as e:
        return {"status": "error", "meta": None, "error": str(e)}


def find_pmid_by_doi(doi: str) -> str:
    """Europe PMC DOI field query (v2.9.0): link a DOI back to its PMID.
    Returns the PMID string, or "" when unlinked/failed (never a verdict)."""
    q = urllib.parse.quote(f'DOI:"{_clean_doi(doi)}" AND SRC:MED')
    url = (f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
           f"query={q}&format=json&resultType=lite")
    try:
        data = json.loads(_api_get(url, timeout=15))
        for h in data.get("resultList", {}).get("result", []):
            pmid = str(h.get("id", "")).strip()
            if h.get("source") == "MED" and pmid.isdigit():
                return pmid
    except Exception:
        pass
    return ""


def verify_doi_entry(doi: str, src: str, resolution: dict = None,
                     linked_pmid: str = None, linked_info: dict = None) -> tuple:
    """Verify one standalone DOI (v2.8.0). Returns (entry, audit_item).

    Semantics are honest by construction: a resolving DOI proves existence,
    never correctness — without claimed metadata the verdict stays unknown
    with the registered metadata attached for human comparison. A Crossref
    404 on an EXPLICITLY provided DOI counts as invalid (fabrication
    signal); on one auto-extracted from scanned text it stays a suspect
    (unknown) — the user never endorsed that string, and DataCite/repository
    DOIs do not live in Crossref."""
    entry = {"pmid": "", "doi": doi, "source_file": os.path.basename(src),
             "claimed_title": "", "valid": False, "entry_kind": "doi"}
    res = resolution if resolution is not None else resolve_doi(doi)
    if res["status"] == "resolved":
        meta = res["meta"]
        entry["resolved"] = True
        entry["valid"] = True
        entry["title"] = meta["title"]
        entry["journal"] = meta["journal"]
        entry["pubdate"] = meta["year"]
        entry["authors"] = ", ".join(meta["authors"][:3])
        entry["meta_source"] = "crossref"
        entry["verdict"] = "unknown"
        entry["details"] = (f"DOI resolves — registered: {meta['title'][:100]} "
                            f"({meta['journal']}, {meta['year']}). No claimed "
                            f"metadata to cross-verify.")
        if meta.get("retracted"):
            entry["retracted"] = True
            entry["retraction_note"] = meta.get("retraction_note", "")
            entry["retraction_source"] = "crossref updated-by"
        # v2.9.0: link the DOI back to its PMID (Europe PMC DOI query) and
        # attach the full PubMed record. The linked record's registered DOI
        # is cross-checked against the input — an ambiguous link keeps
        # Crossref-only metadata instead of risking wrong metadata.
        if linked_pmid is None:
            pmid = find_pmid_by_doi(doi)
            info = fetch_summaries([pmid], batch_size=1).get(pmid, {}) if pmid else {}
        else:
            pmid, info = linked_pmid, linked_info
        if pmid and info.get("valid"):
            if info.get("doi") and not dois_match(info["doi"], doi):
                entry["link_note"] = ("linked PubMed record registers a different DOI — "
                                      "link ambiguous, keeping Crossref-only metadata")
            else:
                entry["pmid"] = pmid
                entry["title"] = info["title"]
                entry["journal"] = info["journal"]
                entry["pubdate"] = info["pubdate"]
                entry["meta_source"] = "pubmed"
                entry["details"] = (f"DOI resolves and links to PMID {pmid} — full "
                                    f"PubMed record attached. No claimed metadata "
                                    f"to cross-verify.")
                if info.get("retracted"):
                    entry["retracted"] = True
                    if not entry.get("retraction_note"):
                        entry["retraction_note"] = info.get("retraction_note", "")
                    entry["retraction_source"] = entry.get("retraction_source", "") or \
                        "PubMed publication type"
        audit = {"doi": doi, "pmid": entry.get("pmid", ""),
                 "source_file": entry["source_file"],
                 "registered": meta, "resolve": "resolved",
                 "link_note": entry.get("link_note", ""), "error": ""}
    elif res["status"] == "not_found":
        explicit = src == "cli"
        entry["verdict"] = "invalid" if explicit else "unknown"
        if explicit:
            entry["details"] = ("DOI not found in Crossref — fabrication signal "
                                "（DOI 查无——伪造信号；DataCite/仓储 DOI 不经 "
                                "Crossref，处置前请经 doi.org 复核）")
        else:
            entry["details"] = ("DOI auto-extracted from scanned text, not found in "
                                "Crossref（自动抽取的 DOI 在 Crossref 查无——疑似但"
                                "不判伪造：DataCite/仓储 DOI 不经 Crossref，请人工经 "
                                "doi.org 复核）")
        entry["suspect"] = not explicit
        entry["error"] = res["error"]
        audit = {"doi": doi, "pmid": "", "source_file": entry["source_file"],
                 "registered": None, "resolve": "not_found", "error": res["error"]}
    else:
        entry["verdict"] = "unknown"
        entry["network_error"] = True
        entry["error"] = res["error"]
        entry["details"] = "Crossref unreachable — DOI could not be resolved (network), never counted as invalid"
        audit = {"doi": doi, "pmid": "", "source_file": entry["source_file"],
                 "registered": None, "resolve": "error", "error": res["error"]}
    audit["verdict"] = {"final": entry["verdict"], "details": entry["details"]}
    return entry, audit


# ── arXiv ID verification (v3.0.0) ──

ARXIV_NEW_RE = re.compile(r'^(\d{2})(0[1-9]|1[0-2])\.(\d{4,5})(v\d+)?$', re.IGNORECASE)
ARXIV_OLD_RE = re.compile(r'^[a-z-]+(?:\.[A-Z]{2})?/\d{7}(v\d+)?$', re.IGNORECASE)
ARXIV_PATTERNS = [
    re.compile(r'arxiv\.org/(?:abs|pdf|format)/([^\s"\'<>),;\]]+)', re.IGNORECASE),
    re.compile(r'arxiv:\s*([^\s"\'<>),;\]]+)', re.IGNORECASE),
]


def _arxiv_id_valid_shape(arxiv_id: str) -> bool:
    """Shape check first: malformed months (13xx) made the official API hang
    in the past (cite-holmes lesson), and garbage IDs waste calls."""
    s = arxiv_id.strip()
    m = ARXIV_NEW_RE.match(s.upper().replace("ARXIV:", ""))
    if m and 1 <= int(m.group(2)) <= 12:
        return True
    return bool(ARXIV_OLD_RE.match(s))


def extract_arxivs_from_file(filepath: str) -> list:
    """Extract arXiv IDs (with shape validation) from a file. Returns
    (arxiv_id, context) pairs."""
    results = []
    try:
        text = Path(filepath).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return results
    seen = set()
    for pattern in ARXIV_PATTERNS:
        for m in pattern.finditer(text):
            raw = m.group(1).strip()
            if raw.lower().startswith("arxiv:"):
                raw = raw[6:]
            # A sentence-final period is the single most common character
            # after a reference — strip trailing dots but KEEP internal dots
            # (old-style math.GT/0309136). Shape validation happens in
            # verify_arxiv_entry — a malformed ID found in a scanned document
            # is exactly the kind of fabrication/mistake to surface, not skip.
            raw = raw.rstrip(".")
            if raw.lower() in seen:
                continue
            seen.add(raw.lower())
            start = max(0, m.start() - 150)
            end = min(len(text), m.end() + 40)
            results.append((raw, text[start:end].replace("\n", " ").strip()))
    return results


def verify_arxiv_entry(arxiv_id: str, src: str, claimed: dict = None) -> tuple:
    """Verify one arXiv ID against the official API (v3.0.0).

    Returns (entry, audit_item). No entry in the Atom feed = fabrication
    signal (invalid); a resolving ID attaches the registered title/year and
    stays unknown (existence is never dressed up as a match)."""
    arxiv_id = arxiv_id.strip()
    # normalize the arXiv: prefix (CLI paste form) before shape/URL
    if arxiv_id.lower().startswith("arxiv:"):
        arxiv_id = arxiv_id[6:].strip()
    entry = {"pmid": "", "doi": "", "arxiv_id": arxiv_id,
             "source_file": os.path.basename(src),
             "claimed_title": "", "valid": False, "entry_kind": "arxiv"}
    audit = {"arxiv_id": arxiv_id, "pmid": "", "doi": "",
             "source_file": entry["source_file"], "registered": None}
    if not _arxiv_id_valid_shape(arxiv_id):
        entry["verdict"] = "invalid"
        entry["error"] = "not a valid arXiv ID shape (new YYMM.NNNNN or old category/NNNNNNN)"
        entry["details"] = ("Not a valid arXiv ID shape — 疑似笔误，请核对"
                            "（新式 YYMM.NNNNN 或旧式 category/NNNNNNN）；"
                            "未计入伪造信号")
        audit["verdict"] = {"final": entry["verdict"], "details": entry["details"]}
        audit["error"] = entry["error"]
        return entry, audit
    url = (f"https://export.arxiv.org/api/query?id_list="
           f"{urllib.parse.quote(arxiv_id, safe='./')}&max_results=1")
    try:
        body = _api_get(url, timeout=20)
        root = ET.fromstring(body)
        # A 200 response that is not an Atom feed (captive portal, proxy
        # notice page, arXiv maintenance HTML) is NOT an answer — treating
        # it as "no entry" would accuse every real ID of being fabricated
        # (v3.0.0 review P0, deterministic offline repro).
        if root.tag != "{http://www.w3.org/2005/Atom}feed":
            raise ValueError("arXiv API returned non-Atom content")
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entries = root.findall("a:entry", ns)
        if not entries:
            entry["verdict"] = "invalid"
            entry["error"] = "arXiv ID not found in official API (totalResults=0)"
            entry["details"] = ("arXiv ID not found in the official API — fabrication "
                                "signal（arXiv 官方 API 查无——伪造信号；极少数被官方"
                                "移除的论文亦会查无）")
            audit["verdict"] = {"final": entry["verdict"], "details": entry["details"]}
            audit["error"] = entry["error"]
            return entry, audit
        e0 = entries[0]
        title = " ".join((e0.find("a:title", ns).text or "").split()) if e0.find("a:title", ns) is not None and e0.find("a:title", ns).text else ""
        pub_el = e0.find("a:published", ns)
        pub = (pub_el.text or "")[:4] if pub_el is not None and pub_el.text else ""
        authors = [" ".join(((a.find("a:name", ns).text or "") if a.find("a:name", ns) is not None else "").split())
                   for a in e0.findall("a:author", ns)]
        # arXiv documents an "Error entry" reply (200 + <title>Error</title>,
        # no published date) for IDs it refuses to parse — not an answer.
        if not title or title.lower() == "error" or not pub:
            entry["verdict"] = "unknown"
            entry["error"] = f"arXiv API error entry (title={title[:40]!r})"
            entry["details"] = ("arXiv API returned an error entry — verdict stays "
                                "unknown（官方 API 返回错误条目，保持未判定）")
            audit["verdict"] = {"final": entry["verdict"], "details": entry["details"]}
            audit["error"] = entry["error"]
            return entry, audit
        entry["resolved"] = True
        entry["valid"] = True
        entry["title"] = title
        entry["pubdate"] = pub
        entry["authors"] = ", ".join(authors[:3])
        entry["meta_source"] = "arxiv"
        entry["verdict"] = "unknown"
        # v3.1.0: claimed title comparison (claims keyed by arxiv_id) —
        # existence upgrades to a real verdict only when the user supplied
        # what the paper SHOULD be titled.
        claimed_title = str((claimed or {}).get("claimed_title", "") or "").strip()
        if claimed_title:
            claimed_words = _normalize_text(claimed_title)
            reg_words = _normalize_text(title)
            word_ratio = (len(claimed_words & reg_words) / len(claimed_words | reg_words)
                          if claimed_words and reg_words else 0)
            seq_ratio = _sequence_similarity(claimed_title, title)
            title_match = word_ratio >= 0.5 or seq_ratio >= 0.90
            entry["claimed_title"] = claimed_title
            claimed_year = str((claimed or {}).get("claimed_year", "") or "").strip()
            entry["fields"] = {"title": title_match, "author": None, "journal": None,
                               "year": (pub == claimed_year) if claimed_year else None}
            if title_match:
                entry["verdict"] = "correct"
                entry["details"] = (f"arXiv ID resolves and registered title matches the "
                                    f"claim (title similarity {max(word_ratio, seq_ratio):.2f}).")
            else:
                entry["verdict"] = "mismatch"
                entry["details"] = (f"arXiv ID resolves but the registered title differs "
                                    f"from the claim — registered: {title[:80]} "
                                    f"（arXiv ID 存在但登记标题与声称不符——疑似张冠李戴）")
        else:
            entry["details"] = (f"arXiv ID resolves — registered: {title[:100]} ({pub}). "
                                f"No claimed metadata to cross-verify.")
        audit["registered"] = {"title": title, "year": pub, "authors": authors}
        audit["verdict"] = {"final": entry["verdict"], "details": entry["details"]}
        return entry, audit
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException,
            ET.ParseError, ValueError, CircuitOpenError) as e:
        entry["verdict"] = "unknown"
        entry["network_error"] = True   # 未验证——readiness/exit 2 口径依赖此标志
        msg = str(e)
        # HTTPError is a URLError subclass: a 4xx means the source is
        # REACHABLE and refused the ID — honest wording, never "unreachable"
        if "HTTP Error 4" in msg:
            entry["error"] = msg
            entry["details"] = ("arXiv API refused the ID (HTTP 4xx) — verdict stays "
                                "unknown（官方 API 拒绝该 ID，按未判定处理）")
        else:
            entry["error"] = msg
            entry["details"] = "arXiv API unreachable — could not verify (network), never counted as invalid"
        audit["verdict"] = {"final": entry["verdict"], "details": entry["details"]}
        audit["error"] = msg
        return entry, audit


# ── Citation context parsing ──

def parse_citation_context(context: str) -> dict:
    """Parse a citation context string to extract claimed metadata.
    
    Handles common reference formats:
      Author1 A, Author2 B. Title. Journal. Year;Vol(Issue):Pages. PMID: XXXXXXXX
      Author1 A, Author2 B, et al. Title. <i>Journal</i>. Year;Vol:Pages.
      HTML variants with <br>, <sup>, etc.
    
    Returns dict with claimed_title, claimed_authors, claimed_journal, claimed_year.
    """
    claimed = {
        "claimed_title": "",
        "claimed_authors": [],
        "claimed_journal": "",
        "claimed_year": "",
    }
    if not context:
        return claimed

    text = context.strip()
    # Remove HTML tags except <i>...</i> which we need for journal detection
    journal_italic = re.findall(r'<i>([^<]+)</i>', text)
    
    # Extract year (4-digit, 19xx or 20xx)
    year_match = re.search(r'\b((?:19|20)\d{2})\b', text)
    if year_match:
        claimed["claimed_year"] = year_match.group(1)

    # Extract journal from <i>...</i> if present
    if journal_italic:
        claimed["claimed_journal"] = journal_italic[0].strip().rstrip(".")
    
    # Strategy: find PMID position, work backwards
    pmid_match = re.search(r'PMID[:\s]*(\d{4,9})', text, re.IGNORECASE)
    if not pmid_match:
        # Try to parse without PMID anchor
        _parse_freeform_citation(text, claimed)
        return claimed

    # Get text before PMID
    pre_text = text[:pmid_match.start()].strip().rstrip(".")

    # Extract authors (before first period followed by uppercase = title start)
    # Split on ". " — first segment is usually authors, second is title
    segments = re.split(r'\.\s+', pre_text)
    segments = [s.strip() for s in segments if s.strip()]

    if len(segments) >= 2:
        # First segment: authors
        author_str = segments[0]
        claimed["claimed_authors"] = _extract_author_surnames(author_str)
        
        # Second segment: title
        claimed["claimed_title"] = segments[1].strip().rstrip(".")

        # Try to find journal in remaining segments if not already found
        if not claimed["claimed_journal"] and len(segments) >= 3:
            # Journal is typically the segment after title, possibly with year/volume
            for seg in segments[2:]:
                # Skip volume/issue patterns like "2021;17(4):e90285"
                if re.match(r'^\d{4};', seg):
                    continue
                # If it looks like a journal name (words, not just numbers)
                if re.search(r'[a-zA-Z]{3,}', seg) and not re.match(r'^\d', seg):
                    if not claimed["claimed_journal"]:
                        claimed["claimed_journal"] = seg.strip().rstrip(".")
    elif len(segments) == 1:
        # Only one segment — try to split authors from title differently
        _parse_freeform_citation(pre_text, claimed)
    
    return claimed


def _extract_author_surnames(author_str: str) -> list[str]:
    """Extract surname list from author string like 'Ravelli A, Martini A, et al'"""
    surnames = []
    # Split on comma
    parts = author_str.split(",")
    for part in parts:
        part = part.strip()
        if part.lower() in ("et al", "et al.", "et"):
            continue
        # Match "Surname Initials" or just "Surname"
        m = re.match(r'^([A-ZÀ-ÿ][a-zÀ-ÿ]+)', part)
        if m:
            surnames.append(m.group(1))
    return surnames


def _parse_freeform_citation(text: str, claimed: dict) -> None:
    """Fallback parser for less structured citation text."""
    # Try to find year
    if not claimed["claimed_year"]:
        ym = re.search(r'\b((?:19|20)\d{2})\b', text)
        if ym:
            claimed["claimed_year"] = ym.group(1)
    
    # Try to find journal from <i> or known journal patterns
    if not claimed["claimed_journal"]:
        # Common journal abbreviations
        jm = re.search(
            r'(?:in\s+|published\s+in\s+)?'
            r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*'
            r'(?:\s+(?:Med|J|Clin|Pediatr|Rheumatol|Lancet|BMJ|Nature|Science|Blood|Ann|Arch|Int|Immunol|Allergy|Res|Rev|Dis))'
            r'(?:\s+(?:Online\s+J\.?|J\.?|Dis\.?))?)',
            text
        )
        if jm:
            claimed["claimed_journal"] = jm.group(1).strip()

    # Try to extract authors from beginning of text
    if not claimed["claimed_authors"]:
        author_match = re.match(r'^([A-Z][a-z]+(?:\s+[A-Z]\.?,?\s*)+)', text)
        if author_match:
            claimed["claimed_authors"] = _extract_author_surnames(author_match.group(1))


# ── Cross-check claimed vs actual ──

def _normalize_text(s: str) -> set[str]:
    """Normalize text to a set of lowercase words for comparison."""
    s = s.lower()
    # Remove punctuation
    s = re.sub(r'[^\w\s]', ' ', s)
    words = set(s.split())
    # Remove common stop words
    stops = {"a", "an", "the", "of", "in", "on", "for", "and", "to", "with", "by",
             "from", "is", "are", "was", "were", "at", "as", "or", "its", "it",
             "this", "that", "which", "be", "has", "have", "had", "not", "but",
             "also", "into", "than", "through", "during", "between", "their", "our",
             "we", "they", "can", "may", "via", "an", "no", "all", "such"}
    return words - stops


def _clean_for_sequencematch(s: str) -> str:
    """Clean title for SequenceMatcher comparison (char-level fuzzy match)."""
    s = s.lower()
    s = re.sub(r'[^\w\s]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _sequence_similarity(s1: str, s2: str) -> float:
    """Character-level similarity via SequenceMatcher. Handles '2' vs 'Two' etc."""
    return SequenceMatcher(None, _clean_for_sequencematch(s1), _clean_for_sequencematch(s2)).ratio()


def cross_check_citation(claimed: dict, actual: dict) -> dict:
    """Compare claimed citation metadata against PubMed actual metadata.
    
    Args:
        claimed: {"claimed_title": str, "claimed_authors": list, 
                  "claimed_journal": str, "claimed_year": str}
        actual: {"title": str, "authors": list, "journal": str, "pubdate": str}
    
    Returns:
        {
            "verdict": "correct" | "mismatch" | "partial" | "unknown",
            "title_match": bool,
            "author_match": bool, 
            "journal_match": bool,
            "year_match": bool,
            "confidence": float,
            "details": str,
        }
    """
    result = {
        "verdict": "unknown",
        "title_match": False,
        "author_match": False,
        "journal_match": False,
        "year_match": False,
        "confidence": 0.0,
        "details": "",
    }

    checks_run = 0

    # --- Year match ---
    actual_year = ""
    if actual.get("pubdate"):
        ym = re.search(r'((?:19|20)\d{2})', actual["pubdate"])
        if ym:
            actual_year = ym.group(1)
    
    if claimed.get("claimed_year") and actual_year:
        result["year_match"] = claimed["claimed_year"] == actual_year
        checks_run += 1

    # --- Title match (dual strategy: word overlap + SequenceMatcher) ---
    if claimed.get("claimed_title") and actual.get("title"):
        claimed_words = _normalize_text(claimed["claimed_title"])
        actual_words = _normalize_text(actual["title"])
        
        # Strategy 1: Word-level Jaccard overlap (order-independent)
        if claimed_words and actual_words:
            overlap = claimed_words & actual_words
            union = claimed_words | actual_words
            word_ratio = len(overlap) / len(union) if union else 0
        else:
            word_ratio = 0
        
        # Strategy 2: Character-level SequenceMatcher (handles "2" vs "Two", minor typos)
        seq_ratio = _sequence_similarity(claimed["claimed_title"], actual["title"])
        
        # Combined: accept if EITHER strategy passes threshold
        result["title_match"] = word_ratio >= 0.5 or seq_ratio >= 0.90
        result["_title_word_ratio"] = round(word_ratio, 3)
        result["_title_seq_ratio"] = round(seq_ratio, 3)
        checks_run += 1

    # --- Author match ---
    if claimed.get("claimed_authors") and actual.get("authors"):
        def _has_cjk(s: str) -> bool:
            # CJK ideographs + kana + Hangul + ext-A: cross-language tokens
            return any('\u3040' <= ch <= '\u30ff' or '\u3400' <= ch <= '\u4dbf'
                       or '\u4e00' <= ch <= '\u9fff' or '\uac00' <= ch <= '\ud7af'
                       for ch in s)

        def _extract_surname(name: str) -> str:
            parts = name.split()
            if not parts:
                return ""
            if len(parts) == 1:
                surname = parts[0]
            elif len(parts[0]) == 1 or (len(parts[0]) == 2 and parts[0].endswith(".")) \
                    or "." in parts[0]:
                surname = parts[-1]   # initials-first "A Zaripova" / "J.-P. Martin"
            else:
                surname = parts[0]    # esummary format "Zaripova A" → Zaripova
            return surname.lower().rstrip(".,")

        actual_surnames = []
        for a in actual["authors"][:10]:
            surname = _extract_surname(a)
            if len(surname) >= 2:      # single-letter initials never match (CH v1.7)
                actual_surnames.append(surname)

        claimed_all = []
        for s in claimed["claimed_authors"]:
            token = _extract_surname(str(s))
            if len(token) >= 2:        # drop initials ("A." → "a")
                claimed_all.append(token)

        claimed_cjk = any(_has_cjk(s) for s in claimed_all)
        actual_cjk = any(_has_cjk(a) for a in actual_surnames)
        claimed_list = claimed_all
        full_skip = False
        if claimed_all and actual_surnames and claimed_cjk != actual_cjk:
            # Cross-language tokens carry no signal (CH v1.7): drop them on
            # the claimed side; if nothing comparable remains, skip honestly.
            kept = [s for s in claimed_all if _has_cjk(s) == actual_cjk]
            if kept:
                claimed_list = kept
                result["author_check"] = "partial skip: cross-language token(s) ignored"
            else:
                claimed_list = []
                full_skip = True
                result["author_check"] = "skipped (cross-language CJK↔Latin)"

        if full_skip:
            result["author_match"] = False
        elif claimed_list and actual_surnames:
            hits = sum(1 for c in claimed_list if any(c in a for a in actual_surnames))
            if len(claimed_list) >= 2:
                result["author_match"] = hits >= 2
            else:
                result["author_match"] = hits >= 1
            checks_run += 1

    # --- Journal match ---
    if claimed.get("claimed_journal") and actual.get("journal"):
        cj = claimed["claimed_journal"].lower().strip()
        aj = actual["journal"].lower().strip()
        # Direct containment
        result["journal_match"] = cj in aj or aj in cj or cj == aj
        # Also check if significant words overlap
        if not result["journal_match"]:
            cj_words = set(cj.split()) - {"the", "of", "and", "journal", "j"}
            aj_words = set(aj.split()) - {"the", "of", "and", "journal", "j"}
            if cj_words and aj_words:
                overlap = cj_words & aj_words
                result["journal_match"] = len(overlap) / min(len(cj_words), len(aj_words)) >= 0.5
        # NLM abbreviation equivalence (v2.4.0): word-prefixes in order,
        # both directions ("Pediatr Rheumatol" ~ "Pediatric Rheumatology").
        if not result["journal_match"]:
            result["journal_match"] = (
                _journal_abbrev_match(claimed["claimed_journal"], actual["journal"])
                or _journal_abbrev_match(actual["journal"], claimed["claimed_journal"]))
        checks_run += 1

    # --- Compute confidence ---
    if checks_run == 0:
        # Nothing comparable was checked. Honest answer: unknown, never a
        # fabricated mismatch.
        if result.get("author_check"):
            result["details"] = ("author names skipped (cross-language CJK↔Latin); "
                                 "no other claimed metadata to check")
        else:
            result["details"] = "Insufficient claimed metadata for cross-check"
        return result
    match_count = sum([result["title_match"], result["author_match"], 
                       result["journal_match"], result["year_match"]])
    result["confidence"] = match_count / max(checks_run, 1)

    # --- Determine verdict ---
    details_parts = []
    
    if not claimed.get("claimed_title") and not claimed.get("claimed_authors"):
        result["verdict"] = "unknown"
        result["details"] = "Insufficient claimed metadata for cross-check"
        return result

    author_skipped = bool(result.get("author_check"))

    # Cross-language author skip + no claimed title + all comparable fields
    # match: the comparable evidence is unanimous -- correct, not mismatch
    # (v2.5.0 review P1).
    if author_skipped and not claimed.get("claimed_title") \
            and result["journal_match"] and result["year_match"]:
        result["verdict"] = "correct"
        result["details"] = ("author skipped (cross-language); all comparable "
                             "fields match")
        return result

    author_skipped = bool(result.get("author_check"))

    # Title not claimed (v2.8.0 attacker P0): judge ONLY on comparable
    # fields. Unanimous match = correct; any miss = partial. Falling through
    # to the mismatch ladder accused perfectly-cited references of being
    # hallucinations when the user simply omitted the title column.
    if not claimed.get("claimed_title") and checks_run > 0:
        matched_count = sum([result["title_match"], result["author_match"],
                             result["journal_match"], result["year_match"]])
        if matched_count == checks_run and not author_skipped:
            result["verdict"] = "correct"
            result["details"] = "title not claimed; all comparable fields match"
            return result
        if author_skipped and matched_count == checks_run:
            result["verdict"] = "correct"
            result["details"] = ("author skipped (cross-language); all comparable "
                                 "fields match")
            return result
        miss = []
        if claimed.get("claimed_authors") and not author_skipped and not result["author_match"]:
            miss.append("author differs")
        if claimed.get("claimed_journal") and not result["journal_match"]:
            miss.append("journal differs")
        if claimed.get("claimed_year") and not result["year_match"]:
            miss.append("year differs")
        result["verdict"] = "partial"
        result["details"] = ("title not claimed; " +
                             ("; ".join(miss) if miss else "comparable fields partially match"))
        return result

    if result["title_match"] and (result["author_match"] or result["journal_match"]):
        result["verdict"] = "correct"
        if not result["author_match"]:
            details_parts.append("author differs slightly")
        if not result["journal_match"]:
            details_parts.append("journal name variant")
    elif result["author_match"] and result["journal_match"] and not result["title_match"]:
        result["verdict"] = "partial"
        details_parts.append("title differs but author+journal match")
    elif result["title_match"] and not result["author_match"] and not result["journal_match"]:
        result["verdict"] = "partial"
        details_parts.append("title matches but author/journal not claimed or differ")
    else:
        result["verdict"] = "mismatch"
        if claimed.get("claimed_title") and not result["title_match"]:
            details_parts.append("title differs")
        if not author_skipped and not result["author_match"]:
            details_parts.append("author differs")
        if claimed.get("claimed_journal") and not result["journal_match"]:
            details_parts.append("journal differs")
        if claimed.get("claimed_year") and not result["year_match"]:
            details_parts.append("year differs")

    result["details"] = "; ".join(details_parts) if details_parts else "all metadata matches"
    return result


# ── Suggest correct PMID ──

def suggest_correct_pmid(claimed: dict) -> list[dict]:
    """Search PubMed for the correct PMID based on claimed metadata.
    Returns top 3 candidates with pmid, title, authors."""
    parts = []
    
    if claimed.get("claimed_authors"):
        parts.append(f'{claimed["claimed_authors"][0]}[au]')
    
    if claimed.get("claimed_title"):
        # Use first 4 significant words from title
        words = [w for w in claimed["claimed_title"].split() 
                 if len(w) > 3 and w.lower() not in {"the", "and", "for", "with", "from", "that", "this"}]
        title_part = " ".join(words[:4])
        if title_part:
            parts.append(title_part)
    
    if claimed.get("claimed_journal"):
        parts.append(f'{claimed["claimed_journal"]}[jour]')
    
    if claimed.get("claimed_year"):
        parts.append(f'{claimed["claimed_year"]}[dp]')
    
    if not parts:
        return []
    
    query = " AND ".join(parts)
    return search_pubmed(query, max_results=3)


# ── PMID extraction ──

PMID_PATTERNS = [
    re.compile(r'PMID[:\s]*(\d{4,9})', re.IGNORECASE),
    re.compile(r'pubmed[:\s]*(\d{4,9})', re.IGNORECASE),
    re.compile(r'https?://pubmed\.ncbi\.nlm\.nih\.gov/(\d+)/', re.IGNORECASE),
]

def extract_pmids_from_file(filepath: str) -> list[tuple[str, str]]:
    """Extract (pmid, context_line) from a file. Returns list of (pmid, surrounding_text)."""
    results = []
    try:
        text = Path(filepath).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return results
    for pattern in PMID_PATTERNS:
        for m in pattern.finditer(text):
            pmid = m.group(1)
            start = max(0, m.start() - 200)
            end = min(len(text), m.end() + 40)
            context = text[start:end].replace("\n", " ").strip()
            results.append((pmid, context))
    return results


def extract_pmids_from_directory(dirpath: str, extensions: tuple = (".html", ".md", ".txt", ".htm", ".json")) -> dict:
    """Scan directory for files containing PMIDs. Returns {filename: [(pmid, context)]}."""
    results = {}
    for root, dirs, files in os.walk(dirpath):
        for fname in files:
            if fname.lower().endswith(extensions):
                fpath = os.path.join(root, fname)
                found = extract_pmids_from_file(fpath)
                if found:
                    results[fpath] = found
    return results


# ── Keyword matching (auxiliary, not primary verification) ──

_MEDICAL_ABBREVS: dict[str, list[str]] = {
    "sjia": ["systemic juvenile idiopathic arthritis", "systemic jia"],
    "csle": ["childhood systemic lupus", "childhood-onset sle", "childhood lupus", "pediatric sle", "juvenile sle", "lupus nephritis"],
    "sle": ["lupus", "systemic lupus erythematosus"],
    "jia": ["juvenile idiopathic arthritis", "juvenile arthritis"],
    "jdm": ["juvenile dermatomyositis", "dermatomyositis"],
    "kd": ["kawasaki"],
    "mas": ["macrophage activation syndrome"],
    "igav": ["iga vasculitis", "henoch-schönlein", "henoch schonlein", "hsp", "purpura"],
    "aid": ["autoinflammatory", "autoinflammation", "recurrent fever"],
    "fmf": ["familial mediterranean fever"],
    "caps": ["cryopyrin-associated periodic", "cryopyrin associated"],
    "savi": ["sting-associated vasculopathy", "sting associated", "sting vasculopathy", "sting", "vascular and pulmonary"],
    "traps": ["tnf receptor-associated"],
    "nlrc4": ["nlrc4"],
    "ild": ["interstitial lung disease", "lung disease"],
    "pid": ["primary immunodeficiency", "immunodeficiency"],
    "alps": ["autoimmune lymphoproliferative", "alps"],
    "cvid": ["common variable immunodeficiency"],
    "cgd": ["chronic granulomatous disease"],
    "scid": ["severe combined immunodeficiency"],
    "xla": ["x-linked agammaglobulinemia", "bruton"],
    "itp": ["immune thrombocytopenia", "thrombocytopenic"],
    "evans": ["evans syndrome"],
    "uveitis": ["uveitis"],
    "behcet": ["behçet", "behcet"],
    "ln": ["lupus nephritis", "nephritis"],
    "aiha": ["autoimmune hemolytic", "hemolytic anemia"],
    "lahps": ["lupus anticoagulant", "hypoprothrombinemia"],
    "ivig": ["intravenous immunoglobulin", "immunoglobulin", "ivig", "igg"],
    "cal": ["coronary aneurysm", "coronary artery"],
    "gio": ["glucocorticoid-induced osteoporosis", "osteoporosis", "bone loss", "fracture"],
    "ctd": ["connective tissue disease"],
    "ar": ["autoimmune regulator", "aire", "aps-1"],
    "npsle": ["neuropsychiatric lupus", "cns lupus"],
    "apls": ["antiphospholipid", "antiphospholipid syndrome"],
    "refractory": ["refractory", "resistant"],
    "biologic": ["biologic", "biological", "biologics"],
    "failure": ["failure", "refractory", "resistant"],
    "early": ["early", "initial", "onset"],
    "incomplete": ["incomplete", "atypical"],
    "nephritis": ["nephritis", "renal", "glomerul"],
    "pulmonary": ["pulmonary", "lung", "respiratory"],
    "gi": ["gastrointestinal"],
    "double": ["double", "repeat", "second"],
    "neuro": ["neurologic", "neurological", "nervous system"],
    "shock": ["shock", "toxic shock"],
}


def keyword_match(title: str, keywords: list[str]) -> float:
    """Enhanced keyword overlap score with medical abbreviation expansion.
    NOTE: This checks topic relevance only, NOT PMID correctness.
    Returns 0.0-1.0."""
    if not title or not keywords:
        return 1.0
    title_lower = title.lower()
    hits = 0
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in title_lower:
            hits += 1
            continue
        expansions = _MEDICAL_ABBREVS.get(kw_lower, [])
        if any(exp in title_lower for exp in expansions):
            hits += 1
    return hits / len(keywords) if keywords else 1.0


def extract_keywords_from_path(filepath: str) -> list[str]:
    """Extract disease/topic keywords from filename."""
    name = Path(filepath).stem.lower()
    for prefix in ("ev_", "case_", "paper_", "ref_"):
        name = name.replace(prefix, "")
    parts = re.split(r"[_\-]", name)
    return [p for p in parts if len(p) > 2]


# ── Cache (SQLite) ──

def _cache_path() -> Path:
    """Return cache database path."""
    p = Path.home() / ".cache" / "pubmed-verifier"
    p.mkdir(parents=True, exist_ok=True)
    return p / "cache.db"


CACHE_SCHEMA_VERSION = 3  # v2: clears pre-v2.2.0 rows that mislabeled network
                          # failures as valid=0; v3: + retracted column so the
                          # pubtype retraction signal survives caching (v2.7.0)
NEG_CACHE_TTL_DAYS = 3    # negatives expire faster: an ahead-of-print PMID
                          # must not read "not found" for a month (v2.6.0)


def _cache_init(db_path: Path) -> None:
    """Initialize cache database; migrate legacy schemas."""
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS pmid_cache (
                pmid TEXT PRIMARY KEY,
                title TEXT, authors TEXT, journal TEXT, pubdate TEXT,
                doi TEXT, valid INTEGER, error TEXT,
                cached_at REAL,
                source TEXT DEFAULT 'pubmed'
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cached_at ON pmid_cache(cached_at)")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < CACHE_SCHEMA_VERSION:
                if version < 2:
                    # Legacy negatives may be transport failures mislabeled by
                    # older versions -- drop them so they get re-queried honestly.
                    conn.execute("DELETE FROM pmid_cache WHERE valid = 0")
                cols = [r[1] for r in conn.execute("PRAGMA table_info(pmid_cache)").fetchall()]
                if "retracted" not in cols:
                    conn.execute("ALTER TABLE pmid_cache ADD COLUMN retracted INTEGER DEFAULT 0")
                conn.execute(f"PRAGMA user_version = {CACHE_SCHEMA_VERSION}")
    finally:
        conn.close()


def _cache_load(db_path: Path, pmids: list[str], max_age_days: int = 30) -> dict:
    """Load cached results for given PMIDs. Returns {pmid: metadata_dict}."""
    results = {}
    cutoff = time.time() - max_age_days * 86400
    neg_cutoff = time.time() - min(max_age_days, NEG_CACHE_TTL_DAYS) * 86400
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            placeholders = ",".join("?" * len(pmids))
            rows = conn.execute(
                f"SELECT pmid,title,authors,journal,pubdate,doi,valid,error,cached_at,source,retracted "
                f"FROM pmid_cache WHERE pmid IN ({placeholders}) "
                f"AND ((valid = 1 AND cached_at > ?) OR (valid = 0 AND cached_at > ?))",
                pmids + [cutoff, neg_cutoff]
            ).fetchall()
        for row in rows:
            pmid, title, authors_json, journal, pubdate, doi, valid, error, cached_at, source, retracted = row
            if valid:
                results[pmid] = {
                    "title": title or "",
                    "authors": json.loads(authors_json) if authors_json else [],
                    "journal": journal or "",
                    "pubdate": pubdate or "",
                    "doi": doi or "",
                    "retracted": bool(retracted),
                    "retraction_note": ("Europe PMC publication type: retracted publication"
                                        if retracted and source == "europepmc" else
                                        ("PubMed publication type: Retracted Publication"
                                         if retracted else "")),
                    "valid": True,
                    "source": source,
                }
            else:
                results[pmid] = {"valid": False, "error": error or "Unknown", "source": source}
    except Exception:
        pass
    finally:
        conn.close()
    return results


def _cache_save(db_path: Path, pmid: str, info: dict) -> None:
    """Save a single PMID result to cache."""
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            if info.get("valid"):
                conn.execute(
                    "INSERT OR REPLACE INTO pmid_cache (pmid,title,authors,journal,pubdate,doi,valid,error,cached_at,source,retracted) "
                    "VALUES (?,?,?,?,?,?,1,'',?,?,?)",
                    (pmid, info.get("title", ""), json.dumps(info.get("authors", []), ensure_ascii=False),
                     info.get("journal", ""), info.get("pubdate", ""), info.get("doi", ""),
                     time.time(), info.get("source", "pubmed"), int(bool(info.get("retracted"))))
                )
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO pmid_cache (pmid,title,authors,journal,pubdate,doi,valid,error,cached_at,source,retracted) "
                    "VALUES (?,'','','','','',0,?,?,?,0)",
                    (pmid, info.get("error", "Unknown"), time.time(), info.get("source", "pubmed"))
                )
    except Exception:
        pass
    finally:
        conn.close()


def _cache_save_fresh(db_path: Path, fresh: dict) -> None:
    """Persist fresh lookups. Network failures are NOT cached: they are not
    answers, and caching one would poison the PMID as 'invalid' for 30 days."""
    for pmid, info in fresh.items():
        if info.get("network_error"):
            continue
        _cache_save(db_path, pmid, info)


def classify_unverified(info: dict) -> tuple:
    """(verdict, details) for a lookup that returned no article.

    Network failures classify as unknown (unverified) -- honest labeling:
    "both sources unreachable" is a different fact from "PMID not found".
    """
    if info.get("network_error"):
        return "unknown", ("数据源不可达，未能验证（不判为无效 PMID）。建议：检查网络后重试；"
                           "或 --meta-source europepmc；或 --timeout 加大超时。")
    return "invalid", f"PMID not found: {info.get('error', 'Unknown')}"


def apply_retraction_cap(verdict: str, details: str) -> tuple:
    """A retracted paper can never grade above partial (v2.3.0, ported from
    cite-holmes): even a perfect metadata match must go to human review,
    because citing it would propagate withdrawn science. mismatch/unknown
    verdicts stay as they are -- already flagged for other reasons."""
    if verdict in ("correct", "partial"):
        note = "RETRACTED paper — human review required（论文已撤稿，引用前须人工复核）"
        return "partial", (details + "; " if details else "") + note
    return verdict, details


# ── DOI↔PMID cross-check & journal abbreviation equivalence (v2.4.0) ──

def _clean_doi(doi: str) -> str:
    """Normalize a DOI for identity comparison: strip url/doi: prefixes
    (repeatedly -- composite prefixes happen), bare-host forms and trailing
    punctuation (including CJK sentence marks and closing brackets/quotes —
    scanned text routinely ends a DOI with them), lowercase."""
    s = str(doi or "").strip().lower()
    changed = True
    while changed:
        changed = False
        for pref in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/",
                     "http://dx.doi.org/", "dx.doi.org/", "doi.org/", "doi:"):
            if s.startswith(pref):
                s = s[len(pref):].strip()
                changed = True
        if s.endswith((".", ",", ";", ":", "。", "，", "；", "）", "」", "】",
                      ")", "]", "}", "!", "?", "!", "?", '"', "”", "’", "'")):
            s = s[:-1].rstrip()
            changed = True
        if "?" in s:
            s = s.split("?", 1)[0].strip()   # tracking params on doi.org links
            changed = True
    return s


def dois_match(claimed: str, registered: str) -> bool:
    """Exact match after normalization -- DOI identity, never similarity."""
    a, b = _clean_doi(claimed), _clean_doi(registered)
    return bool(a) and bool(b) and a == b


_JOURNAL_STOP = {"of", "and", "the", "in", "on", "for"}


def _journal_words_in_order(words: list, pool: list) -> bool:
    """Greedy in-order match: every word in `words` is a prefix of some later
    word in `pool`."""
    i = 0
    for w in words:
        while i < len(pool) and not pool[i].startswith(w):
            i += 1
        if i >= len(pool):
            return False
        i += 1
    return True


def _journal_abbrev_match(claimed: str, actual: str) -> bool:
    """NLM-style abbreviation equivalence (v2.4.0, ported from cite-holmes),
    both directions:

    - abbrev → full: every significant claimed word is an in-order prefix of
      some actual word ("N Engl J Med" ~ "New England Journal of Medicine");
    - full → abbrev: the claimed words' INITIALS, in order, prefix-match the
      actual words ("New England Journal of Medicine" ~ "N Engl J Med").

    Function words are skipped on both sides; single-word claimed journals
    only use the prefix path (initials of one word would match anything)."""
    cw = [w.rstrip(".").lower() for w in claimed.split()]
    aw = [w.rstrip(".").lower() for w in actual.split()]
    cw = [w for w in cw if w and w not in _JOURNAL_STOP]
    aw = [w for w in aw if w and w not in _JOURNAL_STOP]
    if not cw or not aw or len(cw) > len(aw):
        return False
    if _journal_words_in_order(cw, aw):
        return True
    if len(cw) >= 2:
        return _journal_words_in_order([w[0] for w in cw], aw)
    return False


# ── CSV claims loader ──



def _load_csv_claims(filepath: str) -> dict:
    if not hasattr(_load_csv_claims, "arxiv_rows"):
        _load_csv_claims.arxiv_rows = {}
    """Load claimed metadata from CSV file. Returns {pmid: claimed_dict}.
    
    Expected columns: pmid, title, authors, journal, year
    Authors can be semicolon-separated or pipe-separated.
    """
    claims = {}
    try:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                pmid = str(row.get("pmid", "") or row.get("PMID", "")).strip()
                arxiv_col = str(row.get("arxiv_id", "") or row.get("arxiv", "") or "").strip()
                if not pmid and not arxiv_col:
                    continue
                if pmid and not pmid.isdigit():
                    continue
                if not pmid and arxiv_col:
                    # pure-arXiv row: collected into the arXiv claims map
                    _load_csv_claims.arxiv_rows.setdefault(arxiv_col.lower(), {
                        "claimed_title": row.get("title", "") or row.get("Title", "") or "",
                        "claimed_year": str(row.get("year", "") or row.get("Year", "") or ""),
                    })
                    continue
                authors_raw = row.get("authors", "") or row.get("Authors", "") or ""
                # Support semicolon, pipe, or comma separation (but comma conflicts with CSV)
                if ";" in authors_raw:
                    authors = [a.strip() for a in authors_raw.split(";") if a.strip()]
                elif "|" in authors_raw:
                    authors = [a.strip() for a in authors_raw.split("|") if a.strip()]
                else:
                    authors = [a.strip() for a in authors_raw.split(",") if a.strip()]
                claims[pmid] = {
                    "claimed_title": row.get("title", "") or row.get("Title", "") or "",
                    "claimed_authors": authors,
                    "claimed_journal": row.get("journal", "") or row.get("Journal", "") or "",
                    "claimed_year": str(row.get("year", "") or row.get("Year", "") or ""),
                    "claimed_doi": str(row.get("doi", "") or row.get("DOI", "") or ""),
                    "claimed_arxiv": str(row.get("arxiv_id", "") or row.get("arxiv", "") or "").strip(),
                }
    except Exception as e:
        print(f"Error reading CSV claims file: {e}", file=sys.stderr)
    _load_csv_claims.arxiv_rows = getattr(_load_csv_claims, "arxiv_rows", {})
    return claims


# ── Report generation ──

_VERDICT_LADDER = (
    "correct = title match AND (author or journal match); "
    "partial = title-only match, or author+journal match without title, "
    "or capped from correct by retraction/DOI-splice signals; "
    "mismatch = none of the above; "
    "unknown = insufficient comparable claims or unreachable sources. "
    "Single-letter author initials never match; CJK↔Latin author names are "
    "skipped honestly, never counted as misses."
)


def _redacted_argv() -> list:
    """argv with the NCBI API key redacted (audit/HTML reproducibility).

    argparse accepts prefix abbreviations by default (--nc, --ncbi-api,
    --ncbi-api-key=x all resolve to --ncbi-api-key), so redaction matches
    any "--" token that is a prefix of the flag — over-redaction is free,
    under-redaction leaks."""
    redacted, skip_next = [], False
    for a in sys.argv[1:]:
        if skip_next:
            redacted.append("***")
            skip_next = False
            continue
        prefix = a.split("=", 1)[0]
        if prefix.startswith("--") and "--ncbi-api-key".startswith(prefix):
            redacted.append(prefix + ("=***" if "=" in a else ""))
            skip_next = "=" not in a
            continue
        redacted.append(a)
    return redacted


def _bib_escape(s: str) -> str:
    s = str(s).replace("{", "(").replace("}", ")").strip()
    for ch, rep in (("\\", "\\textbackslash{}"), ("&", "\\&"), ("%", "\\%"),
                    ("#", "\\#"), ("_", "\\_")):
        s = s.replace(ch, rep)
    return s


def _bib_authors(joined: str) -> str:
    """'Smith J, Doe A' -> 'Smith, J and Doe, A' (BibTeX Family, Given)."""
    out = []
    for a in (joined or "").split(","):
        parts = a.strip().rsplit(" ", 1)
        out.append(f"{parts[0]}, {parts[1]}" if len(parts) == 2 else a.strip())
    return " and ".join(o for o in out if o)


def generate_bibtex(results: list) -> str:
    """Verified-bibliography export (v2.7.0): correct entries as @article,
    partial entries commented out with their divergence note, everything else
    excluded and counted -- the audit-to-fix-to-reuse loop for systematic
    review workflows."""
    included, partial, excluded = 0, 0, 0
    lines = [
        f"% Verified bibliography generated by pubmed-verifier v{_TOOL_VERSION}",
        "% correct = included; partial = commented out (verify manually);",
        "% mismatch/invalid/unknown/retracted = excluded.",
        "%",
    ]
    key_counts = {}
    seen_pmids = set()
    for r in results:
        if r.get("pmid"):
            if r["pmid"] in seen_pmids:
                continue          # same paper cited several times: one @article
            seen_pmids.add(r["pmid"])
        if r.get("retracted"):
            excluded += 1
            lines.append(f"% EXCLUDED (RETRACTED): PMID {r['pmid']} — do not cite.")
            continue
        v = r.get("verdict")
        if v not in ("correct", "partial") or not r.get("valid"):
            excluded += 1
            continue
        year_m = re.search(r"((?:19|20)\d{2})", str(r.get("pubdate", "")))
        year = year_m.group(1) if year_m else "n.d."
        first = (r.get("authors", "") or "Anonymous").split(",")[0].split()
        key = f"{first[0] if first else 'anon'}{year}pmid{r['pmid']}".lower()
        key_counts[key] = key_counts.get(key, 0) + 1
        if key_counts[key] > 1:
            key = f"{key}{key_counts[key]}"   # same PMID cited twice: unique key
        author_field = _bib_authors(r.get("authors", ""))
        if r.get("authors_truncated"):
            author_field += " and others"
        entry_lines = [
            f"@article{{{key},",
            f"  pmid = {{{r['pmid']}}},",
            f"  title = {{{_bib_escape(r.get('title', ''))}}},",
            f"  author = {{{author_field}}},",
            f"  journal = {{{_bib_escape(r.get('journal', ''))}}},",
            f"  year = {{{year}}},",
        ]
        if r.get("volume"):
            entry_lines.append(f"  volume = {{{_bib_escape(r['volume'])}}},")
        if r.get("pages"):
            entry_lines.append(f"  pages = {{{_bib_escape(r['pages'])}}},")
        if r.get("doi"):
            entry_lines.append(f"  doi = {{{_bib_escape(r['doi'])}}},")
        entry_lines.append(f"  note = {{verified by pubmed-verifier ({v}; PMID {r['pmid']})}}")
        entry_lines.append("}")
        if v == "partial":
            partial += 1
            lines.append(f"% PARTIAL MATCH — verify manually: {r.get('details', '')}")
            lines.extend("% " + l for l in entry_lines)
        else:
            included += 1
            lines.extend(entry_lines)
        lines.append("")
    lines.append(f"% total: {included} included, {partial} partial (commented), {excluded} excluded")
    return "\n".join(lines) + "\n"


def readiness_summary(stats: dict) -> tuple:
    """(ready, one-line verdict) for the report header (v2.7.0)."""
    problems = (stats.get("invalid", 0) + stats.get("mismatch", 0)
                + stats.get("retracted", 0) + stats.get("doi_splice", 0))
    unverified = stats.get("network_errors", 0)
    unknown_meta = max(0, stats.get("unknown", 0) - unverified)
    if problems == 0 and unverified == 0:
        line = "SUBMISSION READY — no invalid, mismatched, retracted or DOI-spliced citations found"
        # Existence-only checks (--pmids without claims) cannot cross-verify:
        # saying nothing about that would dress existence checks up as a
        # full verification (v2.7.0 medical-review P1).
        if unknown_meta > 0:
            line += (f"; {unknown_meta} citation(s) existence-checked only "
                     f"(no claims to cross-verify — feed --claims-file for full verification)")
        return True, line
    parts = []
    if stats.get("invalid"):
        parts.append(f"{stats['invalid']} invalid")
    if stats.get("mismatch"):
        parts.append(f"{stats['mismatch']} mismatched")
    if stats.get("retracted"):
        parts.append(f"{stats['retracted']} retracted")
    if stats.get("doi_splice"):
        parts.append(f"{stats['doi_splice']} DOI-spliced")
    if unverified:
        parts.append(f"{unverified} unverified (network)")
    if unknown_meta:
        parts.append(f"{unknown_meta} existence-checked only")
    return False, "NOT SUBMISSION-READY — " + ", ".join(parts)


def diff_against_baseline(results: list, baseline_path: str) -> dict:
    """Compare a current run to a previous audit working-paper (v2.8.0):
    newly_retracted (the safety signal), degraded, improved, new, dropped."""
    deltas = {"newly_retracted": [], "degraded": [], "improved": [],
              "new": [], "dropped": []}
    try:
        old = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    except Exception as e:
        deltas["error"] = f"baseline unreadable: {e}"
        return deltas
    old_citations = old.get("citations", old if isinstance(old, list) else [])
    old_by_key = {}
    for c in old_citations:
        if not isinstance(c, dict):
            continue
        key = str(c.get("pmid") or c.get("doi") or "")
        if key:
            old_by_key[key] = c
    new_keys = set()
    bad_now = {"mismatch", "invalid", "unknown"}
    for r in results:
        # DOI-pipeline entries keep their DOI as the stable diff key even
        # after linking (a v2.8.0 baseline keys them by DOI too)
        if r.get("entry_kind") == "doi" or (r.get("doi") and not r.get("pmid")):
            key = str(r.get("doi") or "")
        else:
            key = str(r.get("pmid") or r.get("doi") or "")
        if not key:
            continue
        new_keys.add(key)
        o = old_by_key.get(key)
        old_verdict = ""
        if o:
            v_obj = o.get("verdict")
            old_verdict = v_obj.get("final", "") if isinstance(v_obj, dict) else str(v_obj or "")
        old_retracted = bool((o.get("retraction") or {}).get("flagged")) if o else False
        new_retracted = bool(r.get("retracted"))
        if new_retracted and not old_retracted:
            # a retraction is a safety signal even for newly scanned citations
            deltas["newly_retracted"].append({
                "key": key, "title": str(r.get("title", ""))[:80],
                "note": r.get("retraction_note", "")})
        if o is None:
            deltas["new"].append({"key": key, "verdict": r.get("verdict")})
            continue
        if old_verdict == "correct" and r.get("verdict") in bad_now \
                and not new_retracted and not r.get("network_error"):
            deltas["degraded"].append({"key": key, "was": old_verdict,
                                       "now": r.get("verdict"),
                                       "details": str(r.get("details", ""))[:80]})
        elif old_verdict in bad_now and r.get("verdict") == "correct":
            deltas["improved"].append({"key": key, "was": old_verdict})
    for key in old_by_key:
        if key not in new_keys:
            deltas["dropped"].append({"key": key})
    deltas["counts"] = {k: len(v) for k, v in deltas.items() if isinstance(v, list)}
    return deltas


def _csv_safe(cell) -> str:
    """Neutralize spreadsheet formula injection (= + - @ tab CR prefixes) —
    the tool audits untrusted citations, exported tables get opened in
    Excel/LibreOffice by analysts."""
    s = str(cell if cell is not None else "")
    if s and s[0] in "=+-@\t\r":
        return "'" + s
    return s


def generate_csv_report(results: list) -> str:
    """Spreadsheet-friendly audit table (v2.9.0)."""
    buf = io.StringIO()
    buf.write("\ufeff")   # BOM so Excel renders CJK correctly
    w = csv.writer(buf)
    w.writerow(["key", "kind", "verdict", "valid", "retracted", "doi_splice_suspect",
                "claimed_title", "registered_title", "registered_journal",
                "registered_year", "meta_source", "source_file", "confidence",
                "details"])
    for r in results:
        kind = r.get("entry_kind") or ("doi" if (r.get("doi") and not r.get("pmid")) else "pmid")
        w.writerow([_csv_safe(r.get("pmid") or r.get("doi", "")), kind,
                    _csv_safe(r.get("verdict", "")), r.get("valid", ""),
                    r.get("retracted", ""), _csv_safe(r.get("doi_splice_suspect", "")),
                    _csv_safe(r.get("claimed_title", "")), _csv_safe(r.get("title", "")),
                    _csv_safe(r.get("journal", "")), _csv_safe(r.get("pubdate", "")),
                    _csv_safe(r.get("meta_source", "")), _csv_safe(r.get("source_file", "")),
                    r.get("confidence", ""), _csv_safe(r.get("details", ""))])
    return buf.getvalue()


def generate_audit_report(entries: list, stats: dict, args, deltas: dict = None) -> str:
    """Self-contained audit working-paper (v2.6.0): tool identity, redacted
    invocation, per-citation evidence chain and verdict-ladder trace. A third
    party can replay the whole verification from this file alone."""
    audit = {
        "tool": {
            "name": "pubmed-verifier",
            "version": _TOOL_VERSION,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "argv": _redacted_argv(),
            "options": {
                "meta_source": args.meta_source,
                "verify_doi": args.verify_doi,
                "suggest": args.suggest,
                "timeout_s": args.timeout,
                "cache_days": args.cache_days,
            "cache_used": not args.no_cache,
            "matching": ("title: word-jaccard>=0.5 OR char-similarity>=0.9; "
                         "author: surname substring, initials excluded, "
                         "CJK/Latin cross-language skipped; journal: containment "
                         "or NLM abbreviation; year: exact"),
            "ncbi_api_key_configured": bool(_OPTS["ncbi_key"]),
                "mailto_configured": bool(_OPTS["mailto"]),
            },
            "data_sources": ["eutils.ncbi.nlm.nih.gov", "www.ebi.ac.uk/europepmc",
                             "api.crossref.org"],
            "verdict_ladder": _VERDICT_LADDER,
        },
        "summary": stats,
        "citations": entries,
        "delta_vs_baseline": deltas,
        "honesty_notes": [
            "Network failures are reported as unknown, never as not-found, and are not cached.",
            "A missing retraction record is not evidence of no retraction (source outages skip the check).",
            f"Negative cache entries expire after {NEG_CACHE_TTL_DAYS} days.",
            "Retraction status reflects the registry at cache time; a paper retracted "
            "after caching surfaces when the cache entry expires (positive entries: "
            "--cache-days, default 30) — for a final pre-submission check run with --no-cache.",
            "Papers under Expression of Concern (editorial note, not a retraction) are not flagged.",
        ],
    }
    return json.dumps(audit, ensure_ascii=False, indent=2)


def generate_json_report(results: list[dict], stats: dict) -> str:
    return json.dumps({"stats": stats, "results": results}, ensure_ascii=False, indent=2)


def generate_html_report(results: list[dict], stats: dict, source: str, repro: dict = None,
                         deltas: dict = None) -> str:
    total = stats["total"]
    correct = stats.get("correct", 0)
    mismatch = stats.get("mismatch", 0)
    invalid = stats["invalid"]
    unknown = stats.get("unknown", 0)
    partial = stats.get("partial", 0)
    unmatched = stats.get("unmatched", 0)
    pct = (correct / total * 100) if total else 0

    def _sev(r: dict) -> int:
        if r.get("retracted") or r.get("doi_splice_suspect"):
            return 0
        return {"mismatch": 1, "invalid": 2, "partial": 3, "unknown": 4}.get(r.get("verdict", ""), 5)

    def _mark(v) -> str:
        return "✓" if v else ("✗" if v is False else "—")

    ordered = sorted(results, key=_sev)
    rows = ""
    for r in ordered:
        pmid = r["pmid"]
        verdict = r.get("verdict", "")
        if verdict == "correct":
            status_icon, row_class = "✅", "correct"
        elif verdict == "mismatch":
            status_icon, row_class = "⚠️", "mismatch"
        elif verdict == "partial":
            status_icon, row_class = "🔶", "partial"
        elif verdict == "invalid":
            status_icon, row_class = "❌", "invalid"
        else:
            status_icon, row_class = "❓", "unknown"
        flags = []
        if r.get("retracted"):
            flags.append("RETRACTED")
            row_class += " retracted"
        if r.get("doi_splice_suspect"):
            flags.append("DOI-SPLICE")
            row_class += " splice"
        flag_html = (' <span class="flag">' + " · ".join(flags) + "</span>") if flags else ""

        fld = r.get("fields") or {}
        evidence = " · ".join(f"{k} {_mark(fld.get(k))}" for k in ("title", "author", "journal", "year"))

        claimed_title = html.escape(r.get("claimed_title", "")[:80])
        actual_title = html.escape(str(r.get("title", r.get("error", "?")))[:80])
        journal = html.escape(r.get("journal", ""))
        date = html.escape(r.get("pubdate", ""))
        source_file = html.escape(r.get("source_file", ""))
        details = html.escape(r.get("details", ""))
        key_display = html.escape(r.get("arxiv_id") or r.get("pmid") or r.get("doi", ""))
        suggested = r.get("suggested_pmids", [])
        suggested_str = ""
        if suggested:
            suggested_str = "<br>".join(
                f'<a href="https://pubmed.ncbi.nlm.nih.gov/{s["pmid"]}/" target="_blank">PMID {s["pmid"]}</a>: {html.escape(s.get("title","")[:60])}'
                for s in suggested[:3]
            )
        match_score = r.get("match_score")
        match_cell = f'<td>{match_score:.0%}</td>' if match_score is not None else '<td>—</td>'

        rows += (
            f'<tr class="{row_class}">'
            f'<td>{status_icon}</td>'
            f'<td>{key_display}{flag_html}</td>'
            f'<td>{source_file}</td>'
            f'<td class="claimed">{claimed_title}</td>'
            f'<td>{actual_title}</td>'
            f'<td>{journal}</td>'
            f'<td>{date}</td>'
            f'<td class="evidence">{evidence}</td>'
            f'<td class="details">{details}</td>'
            f'<td>{suggested_str}</td>'
            f'{match_cell}</tr>\n'
        )

    epmc_count = sum(1 for r in results if r.get("meta_source") == "europepmc")
    epmc_note = (f'<div style="color:#b06060;font-size:.8rem;margin:8px 0;">⚠ {epmc_count} citation(s) '
                 f'fetched via Europe PMC fallback (NCBI unreachable) — per-entry origin in JSON output.</div>'
                 if epmc_count else "")
    net_card = (f'<div class="stat"><div class="num" style="color:#b06060">{stats.get("network_errors", 0)}</div>'
                f'<div class="label">Unverified (network)</div></div>' if stats.get("network_errors") else "")
    ret_card = (f'<div class="stat"><div class="num" style="color:#c0392b">{stats.get("retracted", 0)}</div>'
                f'<div class="label">Retracted</div></div>' if stats.get("retracted") else "")
    splice_card = (f'<div class="stat"><div class="num" style="color:#c0392b">{stats.get("doi_splice", 0)}</div>'
                   f'<div class="label">DOI Splice</div></div>' if stats.get("doi_splice") else "")

    repro_html = ""
    if repro:
        repro_html = ('<div class="repro"><b>Reproducibility</b><br>'
                      f'command: <code>{html.escape(repro["command"])}</code><br>'
                      f'version: {html.escape(repro["version"])} · '
                      f'sources: {html.escape(repro["sources"])} · '
                      f'generated: {html.escape(repro["generated_at"])}</div>')

    ready, ready_line = readiness_summary(stats)
    banner = (f'<div class="ready ok">{ready_line}</div>' if ready
              else f'<div class="ready notok">{ready_line}</div>')

    delta_html = ""
    if deltas and "error" not in deltas:
        cnt = deltas["counts"]
        nr_items = "".join(
            f'<div>⚠ <b>{html.escape(str(d["key"]))}</b> — {html.escape(d.get("title", ""))}</div>'
            for d in deltas["newly_retracted"])
        delta_html = (f'<div class="repro"><b>Delta vs baseline:</b> '
                      f'{cnt.get("newly_retracted", 0)} newly retracted · '
                      f'{cnt.get("degraded", 0)} degraded · '
                      f'{cnt.get("improved", 0)} improved · '
                      f'{cnt.get("new", 0)} new · '
                      f'{cnt.get("dropped", 0)} dropped'
                      f'{("<br>" + nr_items) if nr_items else ""}</div>')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PMID Verification Report</title>
<style>
body{{font-family:-apple-system,sans-serif;margin:20px auto;max-width:1400px;padding:0 16px;background:#faf8f5;color:#2d2a26;line-height:1.5;}}
h1{{color:#20a39e;}}
.stats{{display:flex;gap:14px;margin:16px 0;flex-wrap:wrap;}}
.stat{{background:#fff;border-radius:8px;padding:12px 20px;border:1px solid #e5e0d8;text-align:center;min-width:80px;}}
.stat .num{{font-size:1.8rem;font-weight:700;}} .stat .label{{font-size:.75rem;color:#6b6560;}}
.filters{{margin:12px 0;}}
.filters button{{border:1px solid #e5e0d8;background:#fff;border-radius:16px;padding:4px 12px;margin-right:6px;cursor:pointer;font-size:.8rem;}}
.filters button:hover{{background:#20a39e;color:#fff;}}
table{{width:100%;border-collapse:collapse;margin:16px 0;font-size:.78rem;}}
th{{background:#20a39e;color:#fff;padding:8px;text-align:left;position:sticky;top:0;}}
td{{padding:7px 8px;border-bottom:1px solid #e5e0d8;vertical-align:top;}}
tr:nth-child(even){{background:#fafafa;}}
tr.mismatch{{background:#fff8e1;}}
tr.invalid{{background:#ffebee;}}
tr.retracted{{background:#fdecea;box-shadow:inset 3px 0 0 #c0392b;}}
tr.splice{{background:#fdecea;box-shadow:inset 3px 0 0 #c0392b;}}
.claimed{{color:#6b6560;font-style:italic;}}
.evidence{{font-size:.72rem;white-space:nowrap;}}
.flag{{color:#c0392b;font-weight:700;font-size:.7rem;}}
.ready{{border-radius:8px;padding:10px 14px;margin:10px 0;font-weight:600;}}
.ready.ok{{background:#e8f5e9;color:#2e7d32;border:1px solid #a5d6a7;}}
.ready.notok{{background:#fdecea;color:#c0392b;border:1px solid #f5c6cb;}}
.details{{font-size:.72rem;color:#8a8580;}}
.repro{{background:#f4f1ec;border-radius:8px;padding:10px 14px;font-size:.72rem;color:#6b6560;margin-top:16px;word-break:break-all;}}
.footer{{text-align:center;padding:16px;color:#9e9893;font-size:.75rem;border-top:1px solid #e5e0d8;margin-top:20px;}}
.legend{{margin:10px 0;font-size:.82rem;color:#6b6560;}}
</style></head><body>
<h1>📋 PMID Citation Verification Report</h1>
{banner}
<p>Source: <code>{html.escape(source)}</code></p>
{delta_html}
<div class="filters">
<button onclick="flt('all')">All ({total})</button>
<button onclick="flt('retracted')">Retracted</button>
<button onclick="flt('splice')">DOI-Splice</button>
<button onclick="flt('mismatch')">Mismatch ({mismatch})</button>
<button onclick="flt('invalid')">Invalid ({invalid})</button>
<button onclick="flt('partial')">Partial ({partial})</button>
<button onclick="flt('unknown')">Unknown ({unknown})</button>
<button onclick="flt('correct')">Correct ({correct})</button>
</div>
<div class="stats">
<div class="stat"><div class="num">{total}</div><div class="label">Total</div></div>
<div class="stat"><div class="num" style="color:#4a9e3f">{correct}</div><div class="label">Correct</div></div>
<div class="stat"><div class="num" style="color:#e6a817">{mismatch}</div><div class="label">Mismatch</div></div>
<div class="stat"><div class="num" style="color:#d05040">{invalid}</div><div class="label">Invalid</div></div>
<div class="stat"><div class="num" style="color:#888">{unknown}</div><div class="label">Unknown</div></div>
{net_card}
{ret_card}
{splice_card}
<div class="stat"><div class="num" style="color:#e08a30">{partial}</div><div class="label">Partial</div></div>
{f'<div class="stat"><div class="num" style="color:#8a8580">{unmatched}</div><div class="label">Low Match</div></div>' if unmatched else ''}
<div class="stat"><div class="num">{pct:.1f}%</div><div class="label">Correct Rate</div></div>
</div>
<div class="legend">
✅ Correct: PMID exists & matches claimed paper &nbsp;|&nbsp;
⚠️ Mismatch: PMID exists but points to a different paper &nbsp;|&nbsp;
❌ Invalid: PMID not found &nbsp;|&nbsp;
🔶 Partial: Some metadata matches &nbsp;|&nbsp;
❓ Unknown: Insufficient metadata for cross-check, or data sources unreachable (network)
<br>Evidence column: title / author / journal / year — ✓ matched · ✗ differed · — not claimed or not comparable. Rows sorted by severity.
</div>
{epmc_note}
<table><thead><tr>
<th></th><th>PMID</th><th>Source</th><th>Claimed Title</th><th>Actual Title</th>
<th>Journal</th><th>Date</th><th>Evidence</th><th>Details</th><th>Suggested</th>{"<th>Relevance</th>" if unmatched else ""}
</tr></thead><tbody>
{rows}</tbody></table>
<script>
function flt(c) {{
  document.querySelectorAll('tbody tr').forEach(function(tr) {{
    tr.style.display = (c === 'all' || tr.classList.contains(c)) ? '' : 'none';
  }});
}}
</script>
{repro_html}
<div class="footer">Generated by pubmed-verifier skill (v{_TOOL_VERSION}) · {time.strftime("%Y-%m-%d %H:%M")}</div>
</body></html>"""


# ── Main ──

def main():
    parser = argparse.ArgumentParser(
        description=f"PMID Citation Verifier v{_TOOL_VERSION} -- Five-state verification of PMIDs, DOIs and arXiv IDs with retraction detection for every citation, DOI-native verification, delta audits against a baseline, audit/BibTeX/CSV export, and dual-source network hardening")
    parser.add_argument("--source", help="File or directory to scan for PMIDs")
    parser.add_argument("--pmids", help="Comma-separated PMIDs to verify directly")
    parser.add_argument("--claims", help="JSON string with claimed metadata: [{pmid,title,authors,journal,year},...]")
    parser.add_argument("--claims-file", help="JSON or CSV file with claimed metadata")
    parser.add_argument("--verify-doi", action="store_true",
                        help="Also verify DOIs via Crossref; flags RETRACTED papers (verdict capped at partial)")
    parser.add_argument("--match-keywords", action="store_true", 
                        help="Check topic relevance via keyword matching (auxiliary, not PMID correctness)")
    parser.add_argument("--threshold", type=float, default=0.2, help="Keyword match threshold (default: 0.2)")
    parser.add_argument("--suggest", action="store_true", help="Auto-suggest correct PMIDs for mismatches (slower, uses extra API calls)")
    parser.add_argument("--no-cache", action="store_true", help="Disable cache, always query API")
    parser.add_argument("--cache-days", type=int, default=30, help="Cache validity in days (default: 30)")
    parser.add_argument("--output", help="Output file (.json or .html)")
    parser.add_argument("--format", choices=["json", "html", "text"], default="text", help="Output format")
    parser.add_argument("--ncbi-api-key", default="",
                        help="NCBI E-utilities API key (or env NCBI_API_KEY): rate limit 3->10 req/s, batch ~3x faster")
    parser.add_argument("--mailto", default="",
                        help="Contact email (or env PUBMED_VERIFIER_MAILTO): Crossref polite pool + NCBI etiquette")
    parser.add_argument("--meta-source", choices=["auto", "ncbi", "europepmc"], default="auto",
                        help="Metadata source: auto = NCBI with Europe PMC fallback (default)")
    parser.add_argument("--timeout", type=float, default=20, help="Per-request timeout seconds (default: 20)")
    parser.add_argument("--export-audit", metavar="PATH",
                        help="Write a self-contained JSON audit working-paper: tool identity, redacted invocation, per-citation evidence chain and verdict trace")
    parser.add_argument("--export-bibtex", metavar="PATH",
                        help="Export verified references as BibTeX (correct included; partial commented; retracted/excluded counted)")
    parser.add_argument("--export-csv", metavar="PATH",
                        help="Export a spreadsheet-friendly audit table (key, verdict, flags, fields, details)")
    parser.add_argument("--workers", type=int, default=4, metavar="N",
                        help="Parallel Crossref resolutions for --dois batches (default 4, max 8)")
    parser.add_argument("--dois", metavar="DOI_LIST",
                        help="Comma-separated DOIs to verify natively via Crossref (existence + registered metadata; 404 = fabrication signal)")
    parser.add_argument("--arxivs", metavar="ARXIV_LIST",
                        help="Comma-separated arXiv IDs to verify against the official arXiv API (nonexistent = fabrication signal)")
    parser.add_argument("--diff", metavar="BASELINE_JSON",
                        help="Compare this run to a previous audit working-paper: newly retracted, degraded, improved, new, dropped")
    parser.add_argument("--version", action="version", version=f"pubmed-verifier {_TOOL_VERSION}")
    args = parser.parse_args()

    _OPTS["ncbi_key"] = args.ncbi_api_key or os.environ.get("NCBI_API_KEY", "")
    _OPTS["mailto"] = args.mailto or os.environ.get("PUBMED_VERIFIER_MAILTO", "")
    _OPTS["meta_source"] = args.meta_source
    _OPTS["timeout"] = args.timeout

    # Collect PMIDs with optional claimed metadata
    pmid_entries = []  # list of (pmid, source_file, context, claimed_dict)
    explicit_claims = {}
    explicit_arxiv = {}   # claims keyed by arxiv_id (v3.1.0)

    # Load explicit claims from --claims or --claims-file
    if args.claims_file:
        filepath = args.claims_file
        if filepath.lower().endswith(".csv"):
            explicit_claims = _load_csv_claims(filepath)
            explicit_arxiv.update(getattr(_load_csv_claims, "arxiv_rows", {}))
            if not explicit_claims and not explicit_arxiv:
                print(f"No valid PMIDs found in CSV file: {filepath}", file=sys.stderr)
                sys.exit(1)
        else:
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    claims_data = json.load(f)
                if not isinstance(claims_data, list):
                    raise ValueError("claims file must be a JSON array of objects")
                for item in claims_data:
                    if not isinstance(item, dict):
                        continue
                    pmid = str(item.get("pmid", "")).strip()
                    if pmid.isdigit():
                        explicit_claims[pmid] = {
                            "claimed_title": str(item.get("title", "") or ""),
                            "claimed_authors": [str(a) for a in item["authors"]]
                                               if isinstance(item.get("authors"), list) else [],
                            "claimed_journal": str(item.get("journal", "") or ""),
                            "claimed_year": str(item.get("year", "") or ""),
                            "claimed_doi": str(item.get("doi", "") or ""),
                        }
                    elif str(item.get("arxiv_id", "") or "").strip():
                        aid = str(item["arxiv_id"]).strip()
                        if aid.lower().startswith("arxiv:"):
                            aid = aid[6:].strip()
                        explicit_arxiv[aid.lower()] = {
                            "claimed_title": str(item.get("title", "") or ""),
                            "claimed_year": str(item.get("year", "") or ""),
                        }
            except (json.JSONDecodeError, ValueError, OSError) as e:
                print(f"Error reading claims file: {e}", file=sys.stderr)
                sys.exit(1)
    elif args.claims:
        try:
            claims_data = json.loads(args.claims)
            if not isinstance(claims_data, list):
                raise ValueError("--claims must be a JSON array of objects")
            for item in claims_data:
                if not isinstance(item, dict):
                    continue
                pmid = str(item.get("pmid", "")).strip()
                if pmid.isdigit():
                    explicit_claims[pmid] = {
                        "claimed_title": str(item.get("title", "") or ""),
                        "claimed_authors": [str(a) for a in item["authors"]]
                                           if isinstance(item.get("authors"), list) else [],
                        "claimed_journal": str(item.get("journal", "") or ""),
                        "claimed_year": str(item.get("year", "") or ""),
                        "claimed_doi": str(item.get("doi", "") or ""),
                    }
                elif str(item.get("arxiv_id", "") or "").strip():
                    aid = str(item["arxiv_id"]).strip()
                    explicit_arxiv[aid.lower()] = {
                        "claimed_title": str(item.get("title", "") or ""),
                        "claimed_year": str(item.get("year", "") or ""),
                    }
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Error parsing --claims JSON: {e}", file=sys.stderr)
            sys.exit(1)

    # Collect PMIDs from sources
    doi_inputs = []   # (clean_doi, source_label) — v2.8.0
    if args.dois:
        for d in args.dois.split(","):
            cd = _clean_doi(d)
            if cd:
                doi_inputs.append((cd, "cli"))
    if args.pmids and args.source:
        print("WARN: both --pmids and --source given; --source is ignored "
              "entirely (its PMIDs and DOIs are NOT scanned).", file=sys.stderr)
    # Standalone arXiv inputs (v3.0.0) + claims keyed by arxiv_id (v3.1.0)
    arxiv_inputs = []   # (arxiv_id, source_label, claimed_dict|None)
    if args.arxivs:
        for a in args.arxivs.split(","):
            a = a.strip()
            if a:
                arxiv_inputs.append((a, "cli", explicit_arxiv.get(a.lower())))
    if args.pmids:
        for p in args.pmids.split(","):
            p = p.strip()
            if p.isdigit():
                claimed = explicit_claims.get(p, {})
                pmid_entries.append((p, "cli", "", claimed))
    elif args.source:
        source = args.source
        if os.path.isfile(source):
            found = extract_pmids_from_file(source)
            for pmid, ctx in found:
                claimed = explicit_claims.get(pmid, parse_citation_context(ctx))
                pmid_entries.append((pmid, source, ctx, claimed))
            for doi, dctx in extract_dois_from_file(source):
                doi_inputs.append((_clean_doi(doi), source))
            for aid, actx in extract_arxivs_from_file(source):
                arxiv_inputs.append((aid, source, explicit_arxiv.get(aid.lower())))
        elif os.path.isdir(source):
            file_map = extract_pmids_from_directory(source)
            for fpath, items in file_map.items():
                for pmid, ctx in items:
                    claimed = explicit_claims.get(pmid, parse_citation_context(ctx))
                    pmid_entries.append((pmid, fpath, ctx, claimed))
            for root, dirs, files in os.walk(source):
                for fname in files:
                    if fname.lower().endswith((".html", ".md", ".txt", ".htm", ".json")):
                        for doi, dctx in extract_dois_from_file(os.path.join(root, fname)):
                            doi_inputs.append((_clean_doi(doi), os.path.join(root, fname)))
                        for aid, actx in extract_arxivs_from_file(os.path.join(root, fname)):
                            arxiv_inputs.append((aid, os.path.join(root, fname),
                                                 explicit_arxiv.get(aid.lower())))
        else:
            print(f"Error: {source} not found", file=sys.stderr)
            sys.exit(1)
    elif explicit_claims or explicit_arxiv:
        # Only --claims provided, no --source or --pmids
        for pmid, claimed in explicit_claims.items():
            pmid_entries.append((pmid, "claims", "", claimed))
        for aid, claimed in explicit_arxiv.items():
            arxiv_inputs.append((aid, "claims", claimed))
    elif not doi_inputs and not arxiv_inputs:
        parser.print_help()
        sys.exit(1)

    if not pmid_entries and not doi_inputs and not arxiv_inputs:
        print("No PMIDs, DOIs or arXiv IDs found.")
        sys.exit(0)

    # Deduplicate, preserving all source contexts and claims
    seen = {}
    for pmid, src, ctx, claimed in pmid_entries:
        if pmid not in seen:
            seen[pmid] = []
        seen[pmid].append((src, ctx, claimed))

    unique_pmids = list(seen.keys())
    if unique_pmids:
        print(f"Found {len(pmid_entries)} PMID citations ({len(unique_pmids)} unique). Verifying...")
    if doi_inputs:
        print(f"Found {len(doi_inputs)} DOI citations to verify via Crossref...")

    # Initialize cache
    use_cache = not args.no_cache
    db_path = _cache_path()
    if use_cache:
        _cache_init(db_path)
        cached = _cache_load(db_path, unique_pmids, max_age_days=args.cache_days)
        uncached_pmids = [p for p in unique_pmids if p not in cached]
        if cached:
            print(f"Cache: {len(cached)} cached, {len(uncached_pmids)} to query")
    else:
        cached = {}
        uncached_pmids = unique_pmids

    # Query PubMed API for uncached PMIDs
    if uncached_pmids:
        fresh = fetch_summaries(uncached_pmids)
        # Save to cache (network failures excluded -- they are not answers)
        if use_cache:
            _cache_save_fresh(db_path, fresh)
    else:
        fresh = {}

    # Merge cached + fresh
    summaries = {}
    for pmid in unique_pmids:
        summaries[pmid] = cached.get(pmid) or fresh.get(pmid, {"valid": False, "error": "No API response"})

    # Build results with three-state verdict
    results = []
    audit_entries = []
    stats = {"total": len(pmid_entries), "correct": 0, "mismatch": 0, "partial": 0,
             "invalid": 0, "unknown": 0, "unmatched": 0, "retracted": 0, "doi_splice": 0}

    for pmid in unique_pmids:
        info = summaries.get(pmid, {"valid": False, "error": "No API response"})
        for src, ctx, claimed in seen[pmid]:
            entry = {
                "pmid": pmid, 
                "source_file": os.path.basename(src), 
                "context": ctx[:200],
                "claimed_title": claimed.get("claimed_title", ""),
            }

            if info.get("valid"):
                entry["valid"] = True
                entry["title"] = info["title"]
                entry["journal"] = info["journal"]
                entry["pubdate"] = info["pubdate"]
                entry["authors"] = ", ".join(info["authors"][:3])
                entry["authors_truncated"] = len(info.get("authors", [])) > 3
                entry["doi"] = info.get("doi", "")
                entry["volume"] = info.get("volume", "")
                entry["pages"] = info.get("pages", "")
                entry["meta_source"] = info.get("source", "ncbi")

                # DOI cross-verification (optional, via Crossref)
                if args.verify_doi and info.get("doi"):
                    doi_meta = fetch_doi_metadata(info["doi"])
                    if doi_meta.get("valid"):
                        entry["doi_verified"] = True
                        # Crossref prefixes retracted titles ("RETRACTED:",
                        # "RETRACTED ARTICLE:") -- strip before comparing so
                        # a confirmed retraction never also reads as a
                        # title mismatch.
                        cr_title = re.sub(r'^\s*RETRACTED(\s+ARTICLE)?\s*:\s*',
                                          "", doi_meta.get("title", ""), flags=re.IGNORECASE)
                        entry["crossref_title"] = doi_meta.get("title", "")
                        entry["doi_title_match"] = _sequence_similarity(
                            cr_title, info["title"]) >= 0.90
                        if doi_meta.get("retracted"):
                            entry["retracted"] = True
                            entry["retraction_note"] = doi_meta.get("retraction_note", "")
                            entry["retraction_source"] = "crossref updated-by"
                    else:
                        entry["doi_verified"] = False
                        entry["doi_note"] = "crossref unreachable (network) — not a verdict"

                # DOI↔PMID cross-check (v2.4.0): a claimed DOI that differs
                # from the DOI registered for this PMID is a splice/fabrication
                # signal (a real DOI attached to the wrong paper). Uses only
                # the esummary field -- no extra API call, no --verify-doi needed.
                if claimed.get("claimed_doi") and info.get("doi"):
                    entry["claimed_doi"] = claimed["claimed_doi"]
                    entry["doi_cross_match"] = dois_match(claimed["claimed_doi"], info["doi"])
                    if not entry["doi_cross_match"]:
                        entry["doi_splice_suspect"] = True

                # Retraction signal, source-independent (v2.7.0): the
                # registry pubtype flags every PMID with no DOI and no
                # --verify-doi needed (survives the cache); Crossref
                # updated-by (checked above) remains the detail source.
                if info.get("retracted") and not entry.get("retracted"):
                    entry["retracted"] = True
                    entry["retraction_note"] = info.get("retraction_note", "")
                    entry["retraction_source"] = "registry pubtype"

                # Cross-check claimed vs actual
                cross = cross_check_citation(claimed, {
                    "title": info["title"],
                    "authors": info["authors"],
                    "journal": info["journal"],
                    "pubdate": info["pubdate"],
                })
                if entry.get("doi_splice_suspect"):
                    splice_note = ("claimed DOI differs from the DOI registered for this PMID "
                                   "（声称DOI与该PMID登记DOI不符——拼接伪造信号）")
                    cross["details"] = (cross["details"] + "; " if cross["details"] else "") + splice_note
                    stats["doi_splice"] = stats.get("doi_splice", 0) + 1
                    if cross["verdict"] == "correct":
                        cross["verdict"] = "partial"
                if entry.get("retracted"):
                    cross["verdict"], cross["details"] = apply_retraction_cap(
                        cross["verdict"], cross["details"])
                    stats["retracted"] = stats.get("retracted", 0) + 1
                entry["verdict"] = cross["verdict"]
                entry["details"] = cross["details"]
                entry["confidence"] = round(cross["confidence"], 2)
                entry["title_match"] = cross["title_match"]
                entry["author_match"] = cross["author_match"]
                entry["journal_match"] = cross["journal_match"]
                entry["year_match"] = cross["year_match"]
                if cross.get("author_check"):
                    entry["author_check"] = cross["author_check"]
                reg_year = any(ch.isdigit() for ch in str(info.get("pubdate", "")))
                entry["fields"] = {
                    # bool only when the field was actually compared (claimed
                    # AND present in the registry); otherwise None (= "—")
                    "title": cross["title_match"]
                             if (claimed.get("claimed_title") and info.get("title")) else None,
                    "author": (None if str(cross.get("author_check", "")).startswith("skipped")
                               else cross["author_match"])
                              if (claimed.get("claimed_authors") and info.get("authors")) else None,
                    "journal": cross["journal_match"]
                               if (claimed.get("claimed_journal") and info.get("journal")) else None,
                    "year": cross["year_match"]
                            if (claimed.get("claimed_year") and reg_year) else None,
                }

                stats[cross["verdict"]] = stats.get(cross["verdict"], 0) + 1

                # Auto-suggest correct PMID for mismatches
                if cross["verdict"] == "mismatch" and args.suggest:
                    suggested = suggest_correct_pmid(claimed)
                    if suggested:
                        entry["suggested_pmids"] = [
                            {"pmid": s["pmid"], "title": s.get("title", "")[:80]}
                            for s in suggested if s.get("valid")
                        ]

                # Keyword matching (auxiliary)
                if args.match_keywords:
                    keywords = extract_keywords_from_path(src)
                    score = keyword_match(info["title"], keywords)
                    entry["match_score"] = score
                    if score < args.threshold:
                        stats["unmatched"] += 1
            else:
                entry["valid"] = False
                entry["verdict"], entry["details"] = classify_unverified(info)
                entry["error"] = info.get("error", "Unknown")
                entry["meta_source"] = info.get("source", "")
                if info.get("network_error"):
                    entry["network_error"] = True
                    entry["verdict"] = "unknown"
                    stats["unknown"] += 1          # keep verdict stats reconcilable
                    stats["network_errors"] = stats.get("network_errors", 0) + 1
                else:
                    stats["invalid"] += 1

            if entry.get("valid"):
                audit_entries.append({
                    "pmid": pmid,
                    "source_file": entry["source_file"],
                    "claimed": claimed,
                    "registered": {
                        "title": entry.get("title", ""), "journal": entry.get("journal", ""),
                        "pubdate": entry.get("pubdate", ""),
                        "authors": info.get("authors", []),
                        "doi": entry.get("doi", ""), "registry": entry.get("meta_source", ""),
                    },
                    "evidence": cross,
                    "doi_cross": {
                        "claimed_doi": entry.get("claimed_doi", ""),
                        "registered_doi": entry.get("doi", ""),
                        "match": entry.get("doi_cross_match"),
                        "splice_suspect": entry.get("doi_splice_suspect", False)},
                    "retraction": {
                        "flagged": entry.get("retracted", False),
                        "note": entry.get("retraction_note", ""),
                        "source": entry.get("retraction_source",
                                            "crossref updated-by" if entry.get("retracted") else "")},
                    "verdict": {"final": entry["verdict"], "details": entry["details"],
                                "confidence": entry.get("confidence")},
                })
            else:
                audit_entries.append({
                    "pmid": pmid,
                    "source_file": entry["source_file"],
                    "claimed": claimed,
                    "registered": None,
                    "verdict": {"final": entry["verdict"], "details": entry["details"]},
                    "error": entry.get("error", ""),
                    "network_error": entry.get("network_error", False),
                })
            results.append(entry)

    # Standalone DOI verification pipeline (v2.8.0/v2.9.0)
    doi_seen = set(r.get("doi") for r in results if r.get("doi"))
    unique_dois = []
    seen_d = set()
    for d, src in doi_inputs:
        if d in doi_seen or d in seen_d:
            continue
        seen_d.add(d)
        unique_dois.append((d, src))
    doi_processed = len(unique_dois)
    resolutions = {}
    workers = max(1, min(args.workers, 8))
    if unique_dois and workers > 1 and len(unique_dois) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(resolve_doi, d): d for d, _ in unique_dois}
            done = 0
            for fut in concurrent.futures.as_completed(futs):
                d = futs[fut]
                try:
                    resolutions[d] = fut.result()
                except Exception as e:
                    resolutions[d] = {"status": "error", "meta": None, "error": str(e)}
                done += 1
                if done % 25 == 0 or done == len(unique_dois):
                    print(f"  DOI progress: {done}/{len(unique_dois)} resolved...",
                          flush=True)
    elif unique_dois:
        for d, _ in unique_dois:
            resolutions[d] = resolve_doi(d)
    # Phase 2 (v2.9.0): link resolved DOIs back to PMIDs — parallel EPMC queries
    resolved_dois = [d for d, _ in unique_dois
                     if resolutions.get(d, {}).get("status") == "resolved"]
    pmid_map = {}
    if resolved_dois:
        if workers > 1 and len(resolved_dois) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                pmid_map = dict(zip(resolved_dois, ex.map(find_pmid_by_doi, resolved_dois)))
        else:
            for d in resolved_dois:
                pmid_map[d] = find_pmid_by_doi(d)
    # Phase 3: batch-fetch full PubMed records for linked PMIDs (cache-aware,
    # 50/batch) — the linking stage never pays per-PMID serial costs
    linked_pmids = sorted({p for p in pmid_map.values() if p})
    pmid_info_map = fetch_summaries(linked_pmids) if linked_pmids else {}
    # Phase 4: build entries serially (stats stay main-thread)
    for doi, src in unique_dois:
        entry, audit_item = verify_doi_entry(
            doi, src, resolution=resolutions.get(doi),
            linked_pmid=pmid_map.get(doi, ""), linked_info=pmid_info_map.get(pmid_map.get(doi, ""), {}))
        entry, audit_item = verify_doi_entry(
            doi, src, resolution=resolutions.get(doi),
            linked_pmid=pmid_map.get(doi, ""), linked_info=pmid_info_map.get(pmid_map.get(doi, ""), {}))
        if entry["verdict"] == "invalid":
            stats["invalid"] += 1
        elif entry.get("network_error"):
            stats["unknown"] += 1
            stats["network_errors"] = stats.get("network_errors", 0) + 1
        elif entry.get("resolved"):
            stats["unknown"] += 1
            stats["doi_resolved"] = stats.get("doi_resolved", 0) + 1
        else:
            stats["unknown"] += 1   # scanned-404 suspect: existence unknown
        if entry.get("retracted"):
            stats["retracted"] = stats.get("retracted", 0) + 1
        results.append(entry)
        audit_entries.append(audit_item)

    # arXiv verification pipeline (v3.0.0) — serial with 3s politeness
    # (official arXiv etiquette: same client >= 3s between calls)
    arxiv_seen = set(r.get("arxiv_id", "").lower() for r in results if r.get("arxiv_id"))
    arxiv_processed = 0
    last_call = 0.0
    for aid, src, claimed_aid in arxiv_inputs:
        if aid.lower() in arxiv_seen:
            continue
        arxiv_seen.add(aid.lower())
        arxiv_processed += 1
        wait = 3.0 - (time.time() - last_call)
        if wait > 0:
            time.sleep(wait)
        last_call = time.time()
        entry, audit_item = verify_arxiv_entry(aid, src, claimed=claimed_aid)
        if entry["verdict"] == "correct":
            stats["correct"] = stats.get("correct", 0) + 1
        elif entry["verdict"] == "mismatch":
            stats["mismatch"] = stats.get("mismatch", 0) + 1
        if entry["verdict"] == "invalid":
            stats["invalid"] += 1
            stats["arxiv_invalid"] = stats.get("arxiv_invalid", 0) + 1
            stats.setdefault("arxiv_invalid_list", []).append(entry.get("arxiv_id", ""))
        elif entry.get("network_error"):
            stats["unknown"] += 1
            stats["network_errors"] = stats.get("network_errors", 0) + 1
        else:
            stats["unknown"] += 1
        if arxiv_processed % 5 == 0:
            print(f"  arXiv progress: {arxiv_processed} verified...", flush=True)
        if entry["verdict"] == "invalid":
            print(f"  ⚠ INVALID arXiv ID: {entry.get('arxiv_id')} — fabrication signal",
                  flush=True)
        results.append(entry)
        audit_entries.append(audit_item)
    if arxiv_processed:
        stats["total"] = stats.get("total", 0) + arxiv_processed
    if doi_processed:
        stats["total"] = stats.get("total", 0) + doi_processed

    # Delta audit against a previous working-paper (v2.8.0)
    deltas = None
    if args.diff:
        deltas = diff_against_baseline(results, args.diff)
        if deltas.get("error"):
            print(f"Diff error: {deltas['error']}", file=sys.stderr)
            deltas = None
        else:
            stats["deltas"] = deltas["counts"]

    stats["submission_readiness"] = {}
    ready, ready_line = readiness_summary(stats)
    stats["submission_readiness"] = {"ready": ready, "summary": ready_line}
    repro = {
        "command": "verify_pmids.py " + " ".join(_redacted_argv()),
        "version": _TOOL_VERSION,
        "sources": "eutils.ncbi.nlm.nih.gov · www.ebi.ac.uk (Europe PMC) · api.crossref.org — all HTTPS",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    # Output
    if args.output:
        ext = Path(args.output).suffix.lower()
        # Explicit --format wins; otherwise derive from the file extension
        # (.json -> json, .html/.htm -> html). Anything else is refused
        # rather than silently writing HTML into a .txt.
        if args.format == "json" or ext == ".json":
            output_text = generate_json_report(results, stats)
        elif args.format == "html" or ext in (".html", ".htm"):
            output_text = generate_html_report(results, stats, args.source or args.pmids or "claims", repro,
                                               deltas)
        else:
            print("Unsupported --output extension (use .json or .html); "
                  "omit --output for terminal text output.", file=sys.stderr)
            sys.exit(1)
        Path(args.output).write_text(output_text, encoding="utf-8")
        print(f"Report written to {args.output}")

    if args.export_audit:
        Path(args.export_audit).write_text(
            generate_audit_report(audit_entries, stats, args, deltas), encoding="utf-8")
        print(f"Audit trail written to {args.export_audit}")

    if args.export_bibtex:
        Path(args.export_bibtex).write_text(
            generate_bibtex(results), encoding="utf-8")
        print(f"BibTeX written to {args.export_bibtex} "
              f"(correct included; partial commented; others excluded)")

    if args.export_csv:
        Path(args.export_csv).write_text(
            generate_csv_report(results), encoding="utf-8")
        print(f"CSV audit table written to {args.export_csv}")

    if not args.output:
        # Text output
        correct = stats.get("correct", 0)
        mismatch = stats.get("mismatch", 0)
        partial = stats.get("partial", 0)
        unknown = stats.get("unknown", 0)
        print(f"\n{'='*60}")
        print(f"Readiness: {ready_line}")
        if deltas and "error" not in deltas:
            cnt = deltas["counts"]
            print(f"Delta vs baseline: {cnt.get('newly_retracted', 0)} newly retracted, "
                  f"{cnt.get('degraded', 0)} degraded, {cnt.get('improved', 0)} improved, "
                  f"{cnt.get('new', 0)} new, {cnt.get('dropped', 0)} dropped")
            for item in deltas["newly_retracted"]:
                print(f"  ⚠ NEWLY RETRACTED: {item['key']} — {item['title']}")
        print(f"Results: {correct}/{stats['total']} correct, {mismatch} mismatch, "
              f"{stats['invalid']} invalid, {partial} partial, {unknown} unknown")
        if stats.get("unmatched"):
            print(f"Relevance warnings: {stats['unmatched']}")
        if stats.get("network_errors"):
            print(f"⚠ Network: {stats['network_errors']} citation(s) could NOT be verified "
                  f"(data sources unreachable; reported as unknown, NOT invalid)")
        if stats.get("retracted"):
            print(f"⚠ Retracted: {stats['retracted']} citation(s) point to RETRACTED papers "
                  f"(verdict capped at partial — human review required)")
        if stats.get("doi_splice"):
            print(f"⚠ DOI splice: {stats['doi_splice']} citation(s) claim a DOI that differs from "
                  f"the one registered for their PMID (fabrication signal; capped at partial)")
        print(f"{'='*60}\n")

        for r in results:
            verdict = r.get("verdict", "")
            if verdict == "correct":
                icon = "✅"
            elif verdict == "mismatch":
                icon = "⚠️"
            elif verdict == "partial":
                icon = "🔶"
            elif verdict == "invalid":
                icon = "❌"
            else:
                icon = "❓"
            
            if r.get("arxiv_id"):
                key_label = f"arXiv {r['arxiv_id']}"
            elif r.get("doi") and not r.get("pmid"):
                key_label = f"DOI {r['doi']}"
            else:
                key_label = f"PMID {r['pmid']}"
            line = f"{icon} {key_label} ({r['source_file']}) [{verdict}]"
            if r.get("valid"):
                line += f"\n   Actual: {r.get('title', '')[:90]}"
                if r.get("claimed_title"):
                    line += f"\n   Claimed: {r['claimed_title'][:90]}"
                if r.get("details"):
                    line += f"\n   Details: {r['details']}"
                if r.get("suggested_pmids"):
                    for s in r["suggested_pmids"][:2]:
                        line += f"\n   → Suggest: PMID {s['pmid']} - {s['title']}"
                if r.get("match_score") is not None and r["match_score"] < args.threshold:
                    line += f" [ relevance={r['match_score']:.0%} ]"
            else:
                line += f"\n   Error: {r.get('error', '?')}"
            print(line)

    # Exit codes: 2 = verification incomplete (network); 1 = problems found
    # (invalid / mismatch / retracted / DOI-splice); 0 = clean. Network-
    # incomplete outranks problem-found so automation never mistakes
    # "could not verify" for "all verified".
    if stats.get("network_errors"):
        sys.exit(2)
    sys.exit(1 if stats["invalid"] > 0 or stats.get("mismatch", 0) > 0
             or stats.get("retracted", 0) > 0 or stats.get("doi_splice", 0) > 0 else 0)


if __name__ == "__main__":
    main()
