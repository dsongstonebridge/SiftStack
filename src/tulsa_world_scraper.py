"""Tulsa World legal notice scraper (Column.us / enotice-production API).

Source: tulsaworld.column.us
API:    POST https://us-central1-enotice-production.cloudfunctions.net/api/search/public-notices

No login or subscription required — public legal notices.

Notice types pulled: "Notice of Sale", "Foreclosure Sale"
Post-filtering: keeps real-estate notices only (court foreclosures, power of
sale, probate property sales). Discards storage/equipment/vehicle lien sales.

Address patterns parsed (in priority order):
  1. "commonly known as [ADDRESS]"        -- Power of Sale notices
  2. "real property located at [ADDRESS]" -- Probate sale notices
  3. "located at [ADDRESS]"               -- Generic
  4. "property address: [ADDRESS]"        -- Label format
  5. "situated at [ADDRESS]"              -- Older court format

Output columns populated:
  date_added    -- notice publication date (YYYY-MM-DD)
  auction_date  -- scheduled sale/auction date if parseable
  address / city / state / zip -- property location
  owner_name    -- defendant (court) or grantor (power of sale)
  decedent_name -- populated for probate real-estate sales
  notice_type   -- "foreclosure" or "probate"
  county        -- "Tulsa"
  state         -- "OK"
  source_url    -- direct PDF URL (or notice page URL)
  raw_text      -- full notice text
"""

import logging
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import requests

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

_API_URL = (
    "https://us-central1-enotice-production.cloudfunctions.net"
    "/api/search/public-notices"
)
_NOTICE_BASE_URL = "https://tulsaworld.column.us/notice"

_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://tulsaworld.column.us",
    "Referer": "https://tulsaworld.column.us/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

# Real-estate keyword filter — notices lacking any of these are discarded
_RE_KW_RE = re.compile(
    r"DISTRICT COURT|MORTGAGE|POWER OF SALE|COMMONLY KNOWN AS|"
    r"REAL PROPERTY|SHERIFF SALE|FORECLOSURE|CJ-\d{4}-\d+|"
    r"NOTICE OF SALE OF REAL|ESTATE OF .{3,60} DECEASED",
    re.IGNORECASE,
)

# Address anchor phrases — tried in order
_ADDR_ANCHORS = [
    "commonly known as",
    "real property located at",
    "located at",
    "property address:",
    "property address is",
    "situated at",
    "described as follows:",
]

# City/state/zip tail patterns
_ZIP_RE = re.compile(r",?\s*(?:Tulsa|Broken Arrow|Owasso|Sand Springs|Jenks|Bixby|Sapulpa|Glenpool)?"
                     r",?\s*(?:OK|Oklahoma)\s*,?\s*(\d{5}(?:-\d{4})?)", re.IGNORECASE)
_CITY_RE = re.compile(
    r",\s*(Tulsa|Broken Arrow|Owasso|Sand Springs|Jenks|Bixby|Sapulpa|Glenpool"
    r"|Collinsville|Skiatook|Sperry|Catoosa|Claremore|Pryor)\s*,?\s*(?:OK|Oklahoma)",
    re.IGNORECASE,
)

# Case numbers
_CASE_RE = re.compile(r"\b(CJ|PB|CV|SC)-\d{4}-\d+\b", re.IGNORECASE)

# Month pattern used in date parsing
_MONTH_FULL = (
    r"(?:January|February|March|April|May|June|July|"
    r"August|September|October|November|December)"
)
_MONTH_ABBR = (
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?"
)
_MONTH_PAT = f"(?:{_MONTH_FULL}|{_MONTH_ABBR})"

# Sale date: "sale ... Month D, YYYY" near sale language (50-char window avoids matching
# mortgage origination dates that appear in "Power Of Sale ... dated Month D, YYYY" phrases)
_SALE_DATE_NAMED = re.compile(
    rf"(?:sale|auction|bid|sold)[^\n;.{{}}]{{0,50}}?"
    rf"({_MONTH_PAT}\s+\d{{1,2}},?\s+\d{{4}})",
    re.IGNORECASE,
)
_SALE_DATE_MDY = re.compile(
    r"(?:sale|auction|bid|sold)[^\n;.{}]{0,60}?(\d{1,2}/\d{1,2}/\d{4})",
    re.IGNORECASE,
)

# "ESTATE OF [NAME], Deceased" for probate classification
_ESTATE_RE = re.compile(r"ESTATE OF\s+([A-Z][A-Z .,'-]{2,60}?)\s*,?\s*DECEASED", re.IGNORECASE)

# "vs\n[NAME]" for defendant in court foreclosures
_DEFENDANT_RE = re.compile(
    r"\bvs?\.?\s*\n\s*([A-Z][A-Z ,.'&-]{4,80?}?)(?:\n|,\s+(?:his|her|their|et al|an?\s+))",
    re.IGNORECASE,
)

# "GIVEN TO:\n[NAME]" for power-of-sale grantors
_GIVEN_TO_RE = re.compile(r"GIVEN TO:?\s*\n\s*([^\n]{5,80})", re.IGNORECASE)


def scrape_tulsa_world(
    since_date: Optional[str] = None,
    days_back: int = 30,
    page_size: int = 100,
) -> list[NoticeData]:
    """Fetch Tulsa World legal notices and return real-estate-related NoticeData records.

    Args:
        since_date: ISO date string (YYYY-MM-DD). Overrides days_back.
        days_back:  How many days back to search (default 30).
        page_size:  Results per API page (max 100).
    """
    if since_date:
        try:
            from_dt = datetime.strptime(since_date, "%Y-%m-%d")
        except ValueError:
            logger.warning("Invalid since_date '%s' — defaulting to %d days back", since_date, days_back)
            from_dt = datetime.utcnow() - timedelta(days=days_back)
    else:
        from_dt = datetime.utcnow() - timedelta(days=days_back)

    to_dt = datetime.utcnow()

    from_ms = int(from_dt.timestamp() * 1000)
    to_ms   = int(to_dt.timestamp() * 1000)

    logger.info(
        "Tulsa World: searching notices from %s to %s",
        from_dt.strftime("%Y-%m-%d"),
        to_dt.strftime("%Y-%m-%d"),
    )

    all_results: list[dict] = []
    current_page = 1
    total_pages  = 1

    session = requests.Session()
    session.headers.update(_HEADERS)

    while current_page <= total_pages:
        payload = {
            "search": "",
            "allFilters": [
                {"publishedtimestamp": {"from": from_ms, "to": to_ms}},
                {"newspapername": ["Tulsa World"]},
                {"noticetype": ["Foreclosure Sale", "Notice of Sale"]},
            ],
            "noneFilters": [],
            "sort": [{"publishedtimestamp": "desc"}],
            "pageSize": page_size,
            "page": current_page,
            "isDemo": False,
        }

        try:
            resp = session.post(_API_URL, json=payload, timeout=30)
            resp.raise_for_status()
        except requests.exceptions.RequestException as exc:
            logger.error("Tulsa World API request failed (page %d): %s", current_page, exc)
            break

        data = resp.json()
        if not data.get("success"):
            logger.error("Tulsa World API returned success=false: %s", data)
            break

        page_info   = data.get("page", {})
        total_pages = page_info.get("total_pages", 1)
        results     = data.get("results", [])

        logger.info(
            "Tulsa World: page %d/%d - %d notices",
            current_page, total_pages, len(results),
        )
        all_results.extend(results)
        current_page += 1

        if current_page <= total_pages:
            time.sleep(0.5)

    logger.info("Tulsa World: %d total notices fetched; filtering for real estate...", len(all_results))

    notices: list[NoticeData] = []
    for result in all_results:
        notice = _parse_result(result)
        if notice is not None:
            notices.append(notice)

    logger.info(
        "Tulsa World: %d real-estate notices extracted from %d total",
        len(notices), len(all_results),
    )
    return notices


def _parse_result(result: dict) -> Optional[NoticeData]:
    """Parse a single API result dict into NoticeData, or return None if not real estate."""
    text = result.get("text") or result.get("highlighted_text") or ""
    if not text:
        return None

    if not _RE_KW_RE.search(text):
        return None

    notice_id   = result.get("id", "")
    pdf_url     = result.get("pdfurl") or ""
    source_url  = pdf_url if pdf_url else (f"{_NOTICE_BASE_URL}/{notice_id}" if notice_id else "")

    # Published date
    ts_ms = result.get("publishedtimestamp", 0)
    if ts_ms:
        date_added = datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")
    else:
        date_added = datetime.utcnow().strftime("%Y-%m-%d")

    # Classify notice type
    estate_m = _ESTATE_RE.search(text)
    if estate_m:
        notice_type   = "probate"
        decedent_name = _clean_name(estate_m.group(1))
    else:
        notice_type   = "foreclosure"
        decedent_name = ""

    # Extract property address
    street, city, state, zip_code = _extract_address(text)

    # Extract owner/defendant name
    owner_name = _extract_owner(text, notice_type, decedent_name)

    # Extract case number
    case_m = _CASE_RE.search(text)
    case_number = case_m.group(0).upper() if case_m else ""

    # Extract sale/auction date
    auction_date = _extract_sale_date(text)

    return NoticeData(
        date_added    = date_added,
        auction_date  = auction_date,
        address       = street,
        city          = city,
        state         = state or "OK",
        zip           = zip_code,
        owner_name    = owner_name,
        decedent_name = decedent_name,
        notice_type   = notice_type,
        county        = "Tulsa",
        source_url    = source_url,
        raw_text      = text[:2000],
        parcel_id     = case_number,
    )


_AUCTION_LOC_RE = re.compile(
    r"will\s+(?:offer|sell|be\s+sold|conduct|be\s+held)",
    re.IGNORECASE,
)


def _extract_address(text: str) -> tuple[str, str, str, str]:
    """Return (street, city, state, zip) from notice text."""
    for anchor in _ADDR_ANCHORS:
        for m in re.finditer(re.escape(anchor), text, re.IGNORECASE):
            start = m.end()
            chunk = text[start : start + 300].strip().lstrip(",: ")
            if not chunk or len(chunk) < 5:
                continue

            # Skip "located at" when it describes the auction venue, not the property
            # (e.g. "sale will be held at the offices of ... located at [venue]")
            if anchor.lower() == "located at" and _AUCTION_LOC_RE.search(chunk[:200]):
                continue

            # Best case: a state-anchored ZIP code appears within the chunk.
            # Require "OK" or "Oklahoma" before the ZIP to avoid matching a
            # 5-digit street number that starts the address (e.g. "56680 East...").
            zip_m = re.search(
                r"(?:OK|Oklahoma)\s+(\d{5}(?:-\d{4})?)\b", chunk, re.IGNORECASE
            )
            if zip_m:
                addr_raw = chunk[: zip_m.end()].strip()
                return _split_address_components(addr_raw)

            # Fallback: stop at newline or semicolon only (never cut on "." abbreviations)
            stop_m = re.search(r"[;\n]", chunk)
            if stop_m:
                addr_raw = chunk[: stop_m.start()].strip().rstrip(".,;")
            else:
                addr_raw = chunk[:100].strip().rstrip(".,;")

            if len(addr_raw) >= 5:
                return _split_address_components(addr_raw)

    return ("", "Tulsa", "OK", "")


def _split_address_components(raw: str) -> tuple[str, str, str, str]:
    """Split a raw address string into (street, city, state, zip)."""
    raw = raw.strip().rstrip(".,;")

    # Extract ZIP
    zip_m = re.search(r"\b(\d{5}(?:-\d{4})?)\s*$", raw)
    zip_code = zip_m.group(1) if zip_m else ""
    if zip_code:
        raw = raw[: zip_m.start()].strip().rstrip(",")

    # Remove trailing state
    state_m = re.search(r",?\s*\b(OK|Oklahoma)\s*$", raw, re.IGNORECASE)
    if state_m:
        raw = raw[: state_m.start()].strip().rstrip(",")

    # Extract city from last comma-segment
    city_m = _CITY_RE.search(raw + ", OK")
    if city_m:
        city  = city_m.group(1).title()
        raw   = raw[: raw.lower().rfind(city.lower())].strip().rstrip(",")
    else:
        parts = [p.strip() for p in raw.rsplit(",", 1)]
        if len(parts) == 2 and 2 < len(parts[1]) < 30:
            raw, city = parts
        else:
            city = "Tulsa"

    return (raw.strip(), city.strip().title(), "OK", zip_code)


def _extract_owner(text: str, notice_type: str, decedent_name: str) -> str:
    """Best-effort owner/defendant name extraction."""
    if notice_type == "probate" and decedent_name:
        return decedent_name

    # Power of Sale: "GIVEN TO:\n[Name]"
    given_m = _GIVEN_TO_RE.search(text)
    if given_m:
        return _clean_name(given_m.group(1))

    # Court foreclosure: "vs\n[Defendant]"
    def_m = _DEFENDANT_RE.search(text)
    if def_m:
        return _clean_name(def_m.group(1))

    return ""


_SALE_DATE_ORDINAL = re.compile(
    rf"(?:sale|auction|bid|sold|on)[^\n;.{{}}]{{0,60}}?"
    rf"(?:the\s+)?(\d{{1,2}})(?:st|nd|rd|th)\s+day\s+of\s+({_MONTH_PAT}),?\s*(\d{{4}})",
    re.IGNORECASE,
)


def _extract_sale_date(text: str) -> str:
    """Return YYYY-MM-DD sale date, or '' if not found."""
    # Named month near sale language (e.g. "sale ... June 15, 2026")
    m = _SALE_DATE_NAMED.search(text)
    if m:
        raw = m.group(1).replace(",", "").strip()
        for fmt in ("%B %d %Y", "%b %d %Y", "%b. %d %Y"):
            try:
                return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue

    # Ordinal date near sale language (e.g. "on the 2nd day of July, 2026")
    m_ord = _SALE_DATE_ORDINAL.search(text)
    if m_ord:
        day, month_str, year = m_ord.group(1), m_ord.group(2), m_ord.group(3)
        raw = f"{month_str.rstrip('.')} {day} {year}"
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue

    # MM/DD/YYYY near sale language
    m2 = _SALE_DATE_MDY.search(text)
    if m2:
        try:
            return datetime.strptime(m2.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    return ""


def _clean_name(raw: str) -> str:
    """Normalize a name string: strip excess whitespace, trailing punctuation."""
    cleaned = re.sub(r"\s+", " ", raw).strip().strip(".,;:")
    # Drop if clearly not a name (all numbers, very short, etc.)
    if len(cleaned) < 3 or re.fullmatch(r"[\d\s]+", cleaned):
        return ""
    return cleaned
