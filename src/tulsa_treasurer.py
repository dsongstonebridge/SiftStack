"""Tulsa County Treasurer client — owner-name search, parcel detail, and tax-payer history.

Source: oktaxrolls.com (Tulsa County Treasurer public portal). Plain HTTP, no
login, no Playwright — same class as tulsa_assessor.py and tulsa_loccat.py.

This is a SEPARATE flow from tulsa_tax_delinquent.py, which drives the same
site's "View By Amount" search to find delinquent properties. This module
drives "View By Owner Name" (found by reading the site's own custom_data_table.js
rather than guessing — the DataTables global `search[value]` parameter on
these endpoints is silently ignored, same trap as DataSift's `search=` param
elsewhere in this codebase; verified with a gibberish-vs-real-name control).
The real owner-name filter lives in dedicated `first_name`/`last_name`/
`business_owner_name` query params on a dedicated route:
`POST /searchResult/Tulsa/owner_name`.

Role in the probate pipeline (calibrated against a real video walkthrough,
2026-09-15, "Using Tulsa County Treasurer for Probate Properties" — Mantle and
Coleman cases): this is a CORROBORATION and TRUE-NEGATIVE-CONFIRMATION source,
not a strict first-tier replacement for the Assessor.

  - On a case the Assessor already resolved (Mantle), the Treasurer's
    owner-name hit matched the probate's own stated mailing address for the
    surviving spouse — useful confirmation, not required once the Assessor
    has already verified.
  - On a true-negative case (Coleman), searching BOTH the decedent's name and
    the named heir's name matters: the heir (Lewis Coleman) had a hit, but it
    was HIS OWN pre-existing property, unrelated to the estate. The `History`
    button — `get_owner_history()` below — is what proves that: it lists who
    paid the tax, per year, for that parcel's lineage. If the decedent's (or
    another estate-connected) name never appears in that history, a hit under
    an heir's name is NOT evidence of a family transfer.

Verified live against the Fulton reference case already documented in
CLAUDE.md (Johnnie Fulton Sr., PB-2026-587/588/589): searching "FULTON,
JOHNNIE" returns FIVE real-estate parcels, not the two the original
Assessor-only investigation found — both known ones (`40800-02-13-05520`,
the reference property at 4503 N Iroquois Ave, and the unplatted
`90328-03-28-15610`, the Assessor's `R...` parcel IDs with dashes and no `R`
prefix) PLUS three real parcels (`02575-02-24-00480`, `06100-02-26-00100`,
`11225-02-24-03090`) that Assessor-only search never surfaced. A gibberish
name (`ZZZQQQNOTAREAL`) returns 0 rows.

Detail-page and history-page links come back from the site carrying extra
`lastName`/`firstName` query tokens that look encrypted/session-bound. They
are NOT required — a bare `taxDataId` resolves the same detail/history pages
(verified live) — so this module builds URLs from `tax_data_id` alone rather
than carrying those opaque tokens around.

COMMON-NAME COLLISION RISK (user-flagged 2026-09-15, confirmed live): a
name-only hit here is NOT proof of identity. Testing the Coleman true-negative
case from memory (no case file in hand, just a recalled name) turned up a
real, direct hit under "COLEMAN, ELIZABETH" on an actual parcel — with
nothing to say whether it's the same Elizabeth Coleman from any given probate
or a different person sharing a common name. Same trap already documented in
CLAUDE.md for the Assessor ("Tina Johnson" — 26-way tie at 0.67). Use
`address_corroborates()` to require a hit's address to match something
already known (the PR's/heir's own stated mailing address, or a property
address found some other way) before treating it as confirmed — a name match
by itself is never sufficient.
"""

import logging
import re
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://oktaxrolls.com"
SEARCH_URL = f"{BASE_URL}/searchResult/Tulsa/owner_name"
DETAIL_URL = f"{BASE_URL}/owner_details/Tulsa"
HISTORY_URL = f"{BASE_URL}/owner_history/Tulsa"

# Earliest year selectable in the site's own "All Years" dropdown option.
# The History table has returned data a year earlier than this in practice
# (it is not bounded by the from/to year params at all) — this is only the
# search-endpoint floor, not a hard limit on what History can show.
EARLIEST_YEAR = 2019

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://oktaxrolls.com/searchTaxRoll/Tulsa",
    "X-Requested-With": "XMLHttpRequest",
}

_TAG_RE = re.compile(r"<[^>]+>")
_TAXDATAID_RE = re.compile(r"taxDataId=(\d+)")
_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

# Incorporated cities within Tulsa County — the site's address blocks have no
# comma between street and city (e.g. "1807 N MAIN TULSA OK 74106"), so a
# trailing-city match is the only reliable way to split them. Unlisted here
# means left unsplit rather than guessed wrong.
_TULSA_COUNTY_CITIES = (
    "BROKEN ARROW", "SAND SPRINGS", "GLENPOOL", "BIXBY", "JENKS", "OWASSO",
    "COLLINSVILLE", "SKIATOOK", "BERRYHILL", "SPERRY", "LIBERTY", "TULSA",
)
_CITY_SUFFIX_RE = re.compile(
    r"^(.*?)\s+(" + "|".join(_TULSA_COUNTY_CITIES) + r")$", re.IGNORECASE
)


_CITY_ONLY_RE = re.compile(
    r"^(" + "|".join(_TULSA_COUNTY_CITIES) + r")$", re.IGNORECASE
)


def _split_trailing_city(s: str) -> tuple[str, str]:
    """Split 'STREET ... CITY' with no delimiter into (street, city.title()).

    An unplatted/metes-and-bounds parcel's Location can be JUST the city
    (no street at all) — that must come back as ("", city), not as a bogus
    "street" equal to the city name.
    """
    stripped = s.strip()
    city_only_m = _CITY_ONLY_RE.match(stripped)
    if city_only_m:
        return "", city_only_m.group(1).strip().title()
    m = _CITY_SUFFIX_RE.match(stripped)
    if m:
        return m.group(1).strip(), m.group(2).strip().title()
    return stripped, ""


def _clean(s: str) -> str:
    return _TAG_RE.sub("", s or "").strip()


def _safe_float(val) -> float:
    try:
        return float(str(val).replace(",", "").replace("$", ""))
    except (ValueError, TypeError):
        return 0.0


def _current_year() -> int:
    return datetime.now().year


# ── Tier: owner-name search ─────────────────────────────────────────────


def search_owner_name(
    last_name: str = "",
    first_name: str = "",
    business_name: str = "",
    *,
    from_year: int = EARLIEST_YEAR,
    to_year: Optional[int] = None,
    real_estate_only: bool = True,
    show_records: int = 50,
    timeout: int = 60,
) -> list[dict]:
    """Search the Treasurer's tax roll by owner name, across all years by default.

    Pass either (last_name[, first_name]) for an individual, or business_name
    for an entity/trust — mirrors the site's own "Search Business or Owner
    name" vs "first/last name" radio choice. At least one of last_name or
    business_name is required by the site; an empty last_name with only a
    business_name is fine.

    Returns one dict per tax-roll row: {tax_year, tax_id, tax_data_id,
    owner_name, parcel_id, tax_type, base_tax, detail_url}. Filtered to
    tax_type == "Real Estate" by default (real_estate_only) — the video's own
    guidance: "we don't care what other taxes they're paying."
    """
    if not last_name and not business_name:
        raise ValueError("search_owner_name requires last_name or business_name")

    if to_year is None:
        to_year = _current_year()

    session = requests.Session()
    session.headers.update(_HEADERS)

    params = {
        "from_years": str(from_year),
        "to_year": str(to_year),
        "first_name": first_name,
        "last_name": last_name,
        "business_owner_name": business_name,
        "show_unpaid_only": "0",
        "total_record": "999999",
        "show_records": str(show_records),
    }
    data = {
        "draw": "1",
        "start": "0",
        "length": str(show_records),
        "search[value]": "",
        "search[regex]": "false",
    }

    try:
        resp = session.post(SEARCH_URL, params=params, data=data, timeout=timeout)
        resp.raise_for_status()
        raw_rows = resp.json().get("data", [])
    except Exception as e:
        logger.warning("Treasurer owner-name search error (%s %s): %s", last_name, business_name, e)
        return []

    results: list[dict] = []
    for row in raw_rows:
        if len(row) < 6:
            continue
        try:
            tax_year = int(row[0])
        except (ValueError, TypeError):
            continue
        tax_id = str(row[1]).strip()
        owner_link_html = row[2]
        parcel_id = str(row[3]).strip()
        tax_type = str(row[4]).strip()
        base_tax = _safe_float(row[5])

        if real_estate_only and tax_type != "Real Estate":
            continue

        m = _TAXDATAID_RE.search(owner_link_html)
        tax_data_id = m.group(1) if m else ""
        owner_name = _clean(owner_link_html)

        results.append({
            "tax_year": tax_year,
            "tax_id": tax_id,
            "tax_data_id": tax_data_id,
            "owner_name": owner_name,
            "parcel_id": parcel_id,
            "tax_type": tax_type,
            "base_tax": base_tax,
            "detail_url": detail_url_for(tax_data_id, from_year=from_year, to_year=to_year) if tax_data_id else "",
        })

    logger.info(
        "Treasurer owner-name search '%s %s%s': %d real-estate row(s)",
        first_name, last_name, f" / {business_name}" if business_name else "",
        len(results),
    )
    return results


def detail_url_for(tax_data_id: str, *, from_year: int = EARLIEST_YEAR, to_year: Optional[int] = None) -> str:
    if to_year is None:
        to_year = _current_year()
    return (
        f"{DETAIL_URL}?fromTaxYear={from_year}&toTaxYear={to_year}"
        f"&info=owner_name&taxDataId={tax_data_id}"
    )


def history_url_for(tax_data_id: str, *, from_year: int = EARLIEST_YEAR, to_year: Optional[int] = None) -> str:
    if to_year is None:
        to_year = _current_year()
    return (
        f"{HISTORY_URL}?fromTaxYear={from_year}&toTaxYear={to_year}"
        f"&info=owner_name&taxDataId={tax_data_id}"
    )


# ── Parcel detail ──────────────────────────────────────────────────────


def get_parcel_detail(tax_data_id: str, *, from_year: int = EARLIEST_YEAR, to_year: Optional[int] = None, timeout: int = 60) -> dict:
    """Fetch the owner/parcel detail page for one tax_data_id.

    Returns {owner_name, owner_street, owner_city, owner_state, owner_zip,
    property_street, property_city, parcel_id, legal_description, total_due,
    improvements_value, source_url}. Any field that can't be found stays
    "" / 0.0 — never guessed. `improvements_value` is the exception: it
    stays None (not 0.0) when the page's "Improvements" figure can't be
    found, because 0.0 is itself a real, meaningful signal (a confirmed-empty
    lot) that must never be confused with "we don't know."
    """
    url = detail_url_for(tax_data_id, from_year=from_year, to_year=to_year)
    result = {
        "owner_name": "", "owner_street": "", "owner_city": "",
        "owner_state": "OK", "owner_zip": "",
        "property_street": "", "property_city": "",
        "parcel_id": "", "legal_description": "", "total_due": 0.0,
        "improvements_value": None,
        "source_url": url,
    }

    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:
        logger.debug("Treasurer detail fetch error (taxDataId=%s): %s", tax_data_id, e)
        return result

    soup = BeautifulSoup(resp.text, "lxml")
    text = soup.get_text(" ", strip=True)

    owner_m = re.search(
        r"Owner Name and Address\s+(.+?)\s+(?:Taxroll Information|Property ID)",
        text, re.IGNORECASE | re.DOTALL,
    )
    if owner_m:
        block = owner_m.group(1).strip()
        zip_m = _ZIP_RE.search(block)
        if zip_m:
            result["owner_zip"] = zip_m.group(1)
            addr_start = block.rfind(zip_m.group(0))
            # Walk back from the zip to find where the street/city portion starts:
            # split on the state token "OK" that precedes the zip.
            pre_zip = block[:addr_start]
            ok_m = re.search(r"(.+?)\s+OK\b\s*$", pre_zip.strip(), re.IGNORECASE)
            if ok_m:
                owner_and_addr = ok_m.group(1).strip()
                # Owner name is everything before the street number begins.
                sm = re.search(r"(\d.*)$", owner_and_addr)
                if sm:
                    addr_part = sm.group(1).strip()
                    result["owner_name"] = owner_and_addr[: sm.start()].strip()
                    street, city = _split_trailing_city(addr_part)
                    result["owner_street"] = street.title()
                    result["owner_city"] = city
                else:
                    result["owner_name"] = owner_and_addr
        if not result["owner_name"]:
            result["owner_name"] = block

    loc_m = re.search(r"Location\s*:\s*(.+?)(?:School District|Tax Year|$)", text, re.IGNORECASE)
    if loc_m:
        loc = loc_m.group(1).replace("\xa0", " ")
        loc = re.sub(r"\bCITY OF\b", "", loc, flags=re.IGNORECASE)
        loc = re.sub(r"\s+", " ", loc).strip()
        zip_m = _ZIP_RE.search(loc)
        if zip_m:
            loc = _ZIP_RE.sub("", loc).strip().rstrip(",").strip()
        street, city = _split_trailing_city(loc)
        result["property_street"] = street.title()
        result["property_city"] = city

    pid_m = re.search(r"Property ID\s*:\s*([\w\-]+)", text, re.IGNORECASE)
    if pid_m:
        result["parcel_id"] = pid_m.group(1).strip()

    legal_m = re.search(
        r"Legal Description and Other Information:\s*(.+?)\s+History\b",
        text, re.IGNORECASE,
    )
    if legal_m:
        result["legal_description"] = re.sub(r"\s+", " ", legal_m.group(1)).strip()

    due_m = re.search(r"Total Due\s+([\d,]+\.\d{2})", text)
    if due_m:
        result["total_due"] = _safe_float(due_m.group(1))

    # "Assessed Valuations ... Land 2497 Improvements 0 Net Assessed 2497".
    # NOT a reliable vacant-lot signal by itself — live-tested 2026-09-15 on
    # the KNOWN, real, structure-confirmed Fulton house at 4503 N Iroquois
    # Ave: this figure reads $0 there too. Oklahoma ad valorem assessed
    # improvement value is not the same concept as "a structure exists" (age,
    # condition, and exemptions can drive it near zero on a real house) —
    # unlike the Assessor's own get_parcel_improvements(), which states
    # vacancy outright ("This property has no improvements", verified 8/8
    # elsewhere in this codebase). Never auto-conclude vacant from this
    # number alone; surface it for a human to check (Zillow/Maps) instead.
    impr_m = re.search(r"\bImprovements\s+([\d,]+(?:\.\d+)?)", text, re.IGNORECASE)
    if impr_m:
        result["improvements_value"] = _safe_float(impr_m.group(1))

    return result


# ── Owner (tax-payer) history — the family-transfer disambiguator ───────


def get_owner_history(tax_data_id: str, *, from_year: int = EARLIEST_YEAR, to_year: Optional[int] = None, timeout: int = 60) -> list[dict]:
    """Fetch the 'History' table for one parcel: who paid tax, per year.

    Returns rows oldest-year-last-or-first as the site renders them (newest
    first, in practice): {tax_year, tax_id, tax_type, owner_name, base_tax,
    fees, penalty, total_paid, total_due}. This is a real server-rendered
    <table>, not a JS grid — parsed directly, no DataTables involved.

    This is the tool that answers "is a hit under the heir's name actually
    the estate property, or just something they already owned?" — check
    whether the decedent's (or another estate-connected) name ever appears
    as owner_name across the years returned here for that parcel lineage.
    """
    url = history_url_for(tax_data_id, from_year=from_year, to_year=to_year)
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:
        logger.debug("Treasurer history fetch error (taxDataId=%s): %s", tax_data_id, e)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    table = soup.find("table", class_="table-tax-data")
    if not table:
        return []
    body = table.find("tbody")
    if not body:
        return []

    rows: list[dict] = []
    for tr in body.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 9:
            continue
        try:
            tax_year = int(cells[0].get_text(strip=True))
        except ValueError:
            continue
        rows.append({
            "tax_year": tax_year,
            "tax_id": cells[1].get_text(strip=True),
            "tax_type": cells[2].get_text(strip=True),
            "owner_name": cells[3].get_text(strip=True),
            "base_tax": _safe_float(cells[4].get_text(strip=True)),
            "fees": _safe_float(cells[5].get_text(strip=True)),
            "penalty": _safe_float(cells[6].get_text(strip=True)),
            "total_paid": _safe_float(cells[7].get_text(strip=True)),
            "total_due": _safe_float(cells[8].get_text(strip=True)),
        })
    return rows


def history_contains_name(history_rows: list[dict], name: str) -> bool:
    """True if any owner_name in the history overlaps significantly with `name`.

    Loose token-overlap on real person/estate names (last + first name both
    present), same style as main._names_overlap() for insider-transfer
    matching — NOT the entity-name substring match in tulsa_loccat.py, which
    solves a different false-positive problem (filler words in business
    names). Names here are people, so token overlap is the right tool.
    """
    target_tokens = {t for t in re.split(r"[^A-Za-z]+", name.upper()) if len(t) > 1}
    if not target_tokens:
        return False
    for row in history_rows:
        row_tokens = {t for t in re.split(r"[^A-Za-z]+", row.get("owner_name", "").upper()) if len(t) > 1}
        if len(target_tokens & row_tokens) >= 2:
            return True
    return False


# ── Common-name collision guard ──────────────────────────────────────────
#
# A name-only hit on this site is NOT proof of identity. Live-tested
# 2026-09-15: searching "COLEMAN, ELIZABETH" (a name recalled from an earlier
# true-negative case) returned a real, direct hit under that exact name on a
# real parcel — with no way to tell from the name alone whether it is the
# same Elizabeth Coleman as any given probate case, or a different person who
# happens to share a common name. This is the same trap already documented in
# CLAUDE.md for the Assessor ("Tina Johnson" returned a 26-way tie at 0.67).
# A hit must be corroborated against an address already known from the
# probate filing (the PR/heir's own stated mailing address, or a property
# address already found some other way) before it is treated as confirming
# ownership — never accept a name match alone.

_STREET_TYPE_WORDS = {
    "ST", "AVE", "AV", "DR", "RD", "LN", "BLVD", "PL", "CT", "CIR", "WAY",
    "PKWY", "TER", "TRL", "LOOP", "PLACE", "STREET", "AVENUE", "DRIVE",
    "ROAD", "LANE", "BOULEVARD", "COURT", "CIRCLE",
}
_DIRECTIONAL_WORDS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}
_HOUSE_NUM_RE = re.compile(r"^\s*(\d+)")


def _address_tokens(addr: str) -> set[str]:
    """Street-NAME words only — no house number (checked separately, exactly,
    by address_corroborates), no directional, no street-type suffix. Without
    excluding the house number here, two addresses that merely share a house
    number (e.g. "1807 N Main" vs "1807 S Elm Ave" — pure coincidence) would
    "overlap" on the digits alone and wrongly count as corroborating."""
    toks = re.split(r"[^A-Za-z0-9]+", (addr or "").upper())
    return {
        t for t in toks
        if t and not t.isdigit() and t not in _DIRECTIONAL_WORDS and t not in _STREET_TYPE_WORDS
    }


def address_corroborates(detail: dict, known_address: str) -> bool:
    """True only if a hit's owner OR property street matches a known address:
    same house number, AND at least one shared street-name word. This is the
    common-name collision guard — pass the PR's/heir's own mailing address
    from the probate filing (or a property address already established some
    other way) as `known_address`. Returns False (never a guess) if either
    address is missing a leading house number.
    """
    known_m = _HOUSE_NUM_RE.match(known_address or "")
    if not known_m:
        return False
    known_house = known_m.group(1)
    known_tokens = _address_tokens(known_address)

    for candidate in (detail.get("owner_street", ""), detail.get("property_street", "")):
        if not candidate:
            continue
        cand_m = _HOUSE_NUM_RE.match(candidate)
        if not cand_m or cand_m.group(1) != known_house:
            continue
        if known_tokens & _address_tokens(candidate):
            return True
    return False
