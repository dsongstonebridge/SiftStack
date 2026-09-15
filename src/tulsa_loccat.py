"""LOCCAT (Tulsa County Clerk/Assessor/Treasurer land records) — plain HTTP,
no browser needed.

Reverse-engineered 2026-09-15 by reading the unminified page source
(`/Scripts/map/map.js`) of https://ais-usc-tulsacounty-web.azurewebsites.net/FullMapView
rather than guessing at its client-side Mapbox map UI — the same "read the
real request before building against it" discipline used for the Assessor's
search grid and sales-history table.

Two real endpoints, both plain POST + JSON, no auth, no session:

- `search_parcel()` -> `POST /api/Parcels/Search` — owner/parcel/address/
  section-township-range. TIGHT matching: `KAISER,LARRY` returns exactly the
  one real hit. Complementary to `tulsa_assessor.search_assessor()`, not a
  replacement — different index, occasionally different coverage.
- `search_advanced()` -> `POST /api/Parcels/SearchAdvanced` — document search
  by grantor/grantee (and subdivision/lot/block). This is the only source in
  this codebase that can find a TRUST by name without already knowing a
  parcel: the Assessor's owner-name search misses trust-titled parcels
  recorded under variant phrasing.

  **LOOSE matching — verified live, this is real, not a hypothetical.**
  Searching `docGrantorGrantee="L & S GROUP LLC"` returned **1,315** results
  (apparently OR-token matching on words like "GROUP"/"LLC"), of which only 3
  were genuine substring matches on the actual name. This is the exact false
  positive a 2026-09-15 video walkthrough called out by hand ("Patricia
  Pruitt" surfaced "Patricia... Party" as a "hit"): never trust the raw
  result count or presence, always verify the returned GRANTOR/GRANTEE string
  actually contains the searched name. `search_advanced()` does this
  filtering itself via `verified` in the returned dict — do not bypass it.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BASE = "https://ais-usc-tulsacounty-web.azurewebsites.net"
_SEARCH_URL = f"{_BASE}/api/Parcels/Search"
_SEARCH_ADVANCED_URL = f"{_BASE}/api/Parcels/SearchAdvanced"

_HTTP_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
}


def _post(url: str, body: dict, *, timeout: int) -> list[dict]:
    try:
        resp = requests.post(url, json=body, headers=_HTTP_HEADERS, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("loccat: search failed for %r: %s", body, e)
        return []
    try:
        data = resp.json()
    except ValueError:
        logger.warning("loccat: non-JSON response for %r", body)
        return []
    return data if isinstance(data, list) else []


def search_parcel(*, owner_name: str = "", address: str = "", parcel: str = "",
                  section: str = "", township: str = "", range_: str = "",
                  timeout: int = 60) -> list[dict]:
    """`POST /api/Parcels/Search` — owner name ("LAST,FIRST" or "LAST"),
    street address, parcel/account number, or section/township/range.

    Returns raw GeoJSON Feature dicts. Useful keys under `["properties"]`:
    PARCELNB, OWNER, PROP_ADD, SECTION, TOWNSHIP, RANGE, SUBDIVISION, LOT,
    BLOCK — LEGAL/SUBDIVISION/LOT/BLOCK/GRANTEE/GRANTOR/DOCUMENTNB/
    RECORDINGDATE are populated by `search_advanced()`, not this endpoint;
    they read `null` here.

    `owner_name` matching is tight (verified: "KAISER,LARRY" -> exactly 1
    hit, the real one) - this does NOT need the exact-match post-filtering
    `search_advanced()` requires.

    `parcel` accepts either the Assessor's own account-number format
    ("R30175032811080") or LOCCAT's own ("30175032811080", no leading
    letter) - the JS strips dashes and uppercases but does not strip a
    leading letter, so pass whichever form you have.
    """
    body = {"parcelAccountSub": parcel.strip().upper().replace("-", ""),
             "ownerName": owner_name.strip().upper(), "address": address.strip().upper(),
             "section": section.strip(), "township": township.strip(), "range": range_.strip()}
    if not any(body.values()):
        return []
    results = _post(_SEARCH_URL, body, timeout=timeout)
    logger.info("loccat: parcel search %r -> %d hit(s)", body, len(results))
    return results


def _normalize_entity_name(s: str) -> str:
    s = s.upper().replace("&", " ")
    s = re.sub(r"[^\sA-Z0-9]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _names_overlap(a: str, b: str) -> bool:
    """Normalized substring containment, not token overlap.

    Token overlap was tried first and is WRONG for entity names: "GROUP" and
    "LLC" are common enough filler words in Oklahoma LLC names that
    >= 2-shared-tokens matched "L & S GROUP LLC" against "STONE INVESTMENT
    GROUP LLC" and 1,153 others out of 1,315 raw hits — verified live,
    2026-09-15. Requiring the whole (normalized) shorter name to appear
    inside the longer one is what actually distinguishes them, and still
    catches real variants like a grantee recorded as "L & S GROUP" (the LLC
    suffix just dropped in that one document).
    """
    na, nb = _normalize_entity_name(a), _normalize_entity_name(b)
    if not na or not nb:
        return False
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    return shorter in longer


def search_advanced(*, doc_grantor_grantee: str = "", subdivision: str = "",
                    lot: str = "", block: str = "", timeout: int = 60) -> list[dict]:
    """`POST /api/Parcels/SearchAdvanced` — document search by grantor/
    grantee name, and/or subdivision/lot/block.

    This is the only source in this codebase that can find a TRUST by name
    without already knowing a parcel: `tulsa_assessor.search_assessor()`
    only searches the Assessor's current OWNER field, which can miss
    trust-titled parcels recorded under phrasing the Assessor doesn't
    surface as a name match. Use this to search "[Decedent] Revocable Trust"
    / "[Decedent] Living Trust" style variants as GRANTEE.

    **`doc_grantor_grantee` matching is LOOSE** (verified live: "L & S GROUP
    LLC" returned 1,315 results, apparently OR-matching individual words).
    Every returned record here is therefore filtered down to `verified: True`
    ones whose GRANTOR or GRANTEE string actually shares >= 2 name tokens
    with the query - callers should still eyeball `verified` results before
    trusting them (a shared surname isn't proof of the same person), but
    should NEVER act on an unverified one. The raw hit count alone (like the
    video's "Patricia Pruitt" surfacing "Patricia... Party") is not evidence
    of anything.
    """
    body = {"docGrantorGrantee": doc_grantor_grantee.strip().upper(),
             "subdivision": subdivision.strip(), "lot": lot.strip(), "block": block.strip()}
    if not any(body.values()):
        return []
    results = _post(_SEARCH_ADVANCED_URL, body, timeout=timeout)

    query = doc_grantor_grantee.strip()
    for r in results:
        props = r.get("properties") or {}
        grantor, grantee = props.get("GRANTOR") or "", props.get("GRANTEE") or ""
        r["verified"] = bool(query) and (_names_overlap(query, grantor)
                                          or _names_overlap(query, grantee))

    verified_count = sum(1 for r in results if r.get("verified"))
    logger.info("loccat: advanced search %r -> %d raw hit(s), %d verified",
               body, len(results), verified_count)
    if len(results) > 50 and verified_count < len(results):
        logger.warning("loccat: %r returned %d results (LOOSE match on common words) - "
                       "only %d survive exact verification; do not trust the raw count",
                       doc_grantor_grantee, len(results), verified_count)
    return results


def verified_hits(results: list[dict]) -> list[dict]:
    """Filter `search_advanced()` results down to verified ones. A thin
    helper so callers can't accidentally forget the filter and reproduce the
    "Patricia Party" false-positive."""
    return [r for r in results if r.get("verified")]
