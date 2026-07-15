"""Tulsa County tax-delinquent property scraper.

Source: oktaxrolls.com/searchTaxRoll/Tulsa (Tulsa County Treasurer public portal)
No login or subscription required.

Strategy (monthly-sustainable):
  1. Query the amount-sorted list API for the TWO most recent completed
     tax years in one combined scan (e.g. 2024 + 2025 in mid-2026).
     The API returns year-2 records first, then year-1 records.
  2. Collect all Real Estate tax_ids for each year into two sets.
  3. The INTERSECTION of those two sets = properties delinquent in BOTH
     years = confirmed 2+ year delinquents.  No per-record parcel lookup
     needed — the intersection IS the proof of multi-year delinquency.
  4. Fetch detail pages only for intersection records to get the full
     property address and owner mailing address.
  5. Return NoticeData with notice_type="tax_delinquent".

Why this is monthly-sustainable:
  - Re-running next month produces the SAME 2+ year delinquents PLUS any
    new records where 2025 delinquency solidified (second-half April deadline).
  - The pipeline's existing address-dedup removes already-imported records
    so DataSift only receives net-new leads.
  - Year logic advances automatically: in 2027 this pulls 2025+2026, etc.

Scan depth:
  Combined query sort order: all year-2 (SA→Business→RE descending by $),
  then all year-1 (SA→Business→RE).  In Tulsa County:
    - year-2 RE records start at ~offset 10,000
    - year-1 RE records start at ~offset 20,700
  MAX_SCAN_PAGES is set high enough to capture both RE blocks.

Output columns populated:
  address / city / state / zip        — property location
  owner_name                          — owner of record
  Owner Street/City/State/ZIP         — owner mailing address (may differ)
  tax_delinquent_amount               — total amount due (latest year)
  tax_delinquent_years                — comma-separated delinquent tax years
  parcel_id                           — county property/parcel ID
  source_url                          — detail page URL
"""

import csv
import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

BASE_URL   = "https://oktaxrolls.com"
LIST_URL   = f"{BASE_URL}/searchResult/Tulsa/amount"
DETAIL_URL = f"{BASE_URL}/owner_details/Tulsa"

# Pages needed to reach year-1 RE records in combined 2-year query (~25 000 rows)
MAX_SCAN_PAGES = 280

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://oktaxrolls.com/searchTaxRoll/Tulsa",
    "X-Requested-With": "XMLHttpRequest",
}

_TAG_RE      = re.compile(r"<[^>]+>")
_TAX_DATA_RE = re.compile(r"taxDataId=(\d+)")
_ZIP_RE      = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_SUFFIX_RE   = re.compile(
    r"(.*?\b(?:ST|AVE?|DR|RD|LN|BL?VD?|PL|CT|CIR?|WAY|PKWY|HWY|TRL|LOOP|PASS|XING)"
    r"(?:\s+[NSEW])?\b)(.*)",
    re.IGNORECASE,
)

_CACHE_FILE     = Path(__file__).parent.parent / "output" / "tulsa_tax_cache.json"
_CACHE_MAX_DAYS = 7


def _load_cache(older_year: int, newer_year: int) -> tuple[dict, dict] | None:
    """Return (older_map, newer_map) from disk if cache is fresh, else None."""
    if not _CACHE_FILE.exists():
        return None
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        if data.get("older_year") != older_year or data.get("newer_year") != newer_year:
            logger.info("Tax cache is for different years — ignoring")
            return None
        cached_at = datetime.fromisoformat(data["cached_at"])
        age_days = (datetime.now() - cached_at).days
        if age_days > _CACHE_MAX_DAYS:
            logger.info("Tax cache is %d days old (max %d) — refreshing", age_days, _CACHE_MAX_DAYS)
            return None
        logger.info(
            "Tax cache hit: %d older + %d newer RE records (cached %s, %d day(s) ago)",
            len(data["older_map"]), len(data["newer_map"]),
            data["cached_at"][:10], age_days,
        )
        return data["older_map"], data["newer_map"]
    except Exception as e:
        logger.warning("Tax cache unreadable (%s) — doing full scan", e)
        return None


def _save_cache(older_map: dict, newer_map: dict, older_year: int, newer_year: int) -> None:
    """Persist scan results to disk for reuse on subsequent runs."""
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cached_at":  datetime.now().isoformat(timespec="seconds"),
            "older_year": older_year,
            "newer_year": newer_year,
            "older_map":  older_map,
            "newer_map":  newer_map,
        }
        _CACHE_FILE.write_text(json.dumps(payload), encoding="utf-8")
        logger.info("Tax cache saved: %s", _CACHE_FILE)
    except Exception as e:
        logger.warning("Could not save tax cache: %s", e)


def _load_seen_parcel_ids() -> set[str]:
    """Return parcel IDs already written to any ok_notices_*.csv in output/."""
    seen: set[str] = set()
    output_dir = _CACHE_FILE.parent
    if not output_dir.exists():
        return seen
    for csv_path in output_dir.glob("ok_notices_*.csv"):
        try:
            with csv_path.open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    pid = row.get("parcel_id", "").strip()
                    if pid:
                        seen.add(pid)
        except Exception as e:
            logger.warning("Could not read %s for seen-parcel check: %s", csv_path.name, e)
    if seen:
        logger.info("Skipping %d parcel IDs already in output CSVs", len(seen))
    return seen


# ── Combined two-year scan ─────────────────────────────────────────────


def _collect_two_years(
    session: requests.Session,
    older_year: int,
    newer_year: int,
    page_size: int = 100,
    delay: float = 0.25,
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Scan the combined amount-sorted list for two consecutive tax years.

    Returns (older_map, newer_map) — each keyed by tax_id, values contain
    {tax_id, tax_data_id, owner_name, total_due}.  Stops once both year
    blocks have been fully consumed (API returns < page_size rows).
    """
    older_map: dict[str, dict] = {}
    newer_map: dict[str, dict] = {}
    seen_newer = False

    params_base = {
        "from_years": str(older_year),
        "to_year":    str(newer_year),
        "amount": "", "total_due_amount": "",
        "show_unpaid_only": "1",
        "show_records": str(page_size),
        "total_record": "999999",
    }

    for page_num in range(MAX_SCAN_PAGES):
        start = page_num * page_size
        data = {
            "draw":   str(page_num + 1),
            "start":  str(start),
            "length": str(page_size),
            "search[value]": "", "search[regex]": "false",
        }
        try:
            resp = session.post(LIST_URL, params=params_base, data=data, timeout=30)
            resp.raise_for_status()
            raw_rows = resp.json().get("data", [])
        except Exception as e:
            logger.warning("List API error page %d: %s", page_num, e)
            break

        if not raw_rows:
            break

        for row in raw_rows:
            if len(row) < 6 or row[3] != "Real Estate":
                continue
            try:
                row_year = int(row[0])
            except (ValueError, TypeError):
                continue
            if row_year not in (older_year, newer_year):
                continue

            tax_id = str(row[1]).strip()
            m = _TAX_DATA_RE.search(row[2])
            if not m:
                continue
            rec = {
                "tax_id":      tax_id,
                "tax_data_id": m.group(1),
                "owner_name":  _TAG_RE.sub("", row[2]).strip(),
                "total_due":   _safe_float(row[5]),
                "tax_year":    row_year,
            }
            if row_year == older_year:
                older_map.setdefault(tax_id, rec)
            else:
                newer_map.setdefault(tax_id, rec)
                seen_newer = True

        # Once we've seen newer-year records AND the page is short, we're done
        if len(raw_rows) < page_size and seen_newer:
            break

        # Progress logging every 50 pages
        if (page_num + 1) % 50 == 0:
            logger.info(
                "  Scan page %d: older=%d, newer=%d RE records so far",
                page_num + 1, len(older_map), len(newer_map),
            )

        time.sleep(delay)

    logger.info(
        "Scan complete: %d year-%d records, %d year-%d records",
        len(older_map), older_year, len(newer_map), newer_year,
    )
    return older_map, newer_map


def _safe_float(val: str) -> float:
    try:
        return float(str(val).replace(",", "").replace("$", ""))
    except ValueError:
        return 0.0


# ── Detail-page parser ─────────────────────────────────────────────────


def _fetch_detail(
    session: requests.Session,
    tax_data_id: str,
    tax_year: int,
) -> Optional[dict]:
    """Fetch owner detail page; return address dict or None."""
    params = {
        "fromTaxYear": str(tax_year), "toTaxYear": str(tax_year),
        "info": "amount", "show_records": "100", "taxDataId": tax_data_id,
    }
    try:
        resp = session.get(DETAIL_URL, params=params, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        logger.debug("Detail fetch error (id=%s): %s", tax_data_id, e)
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    text = soup.get_text(" ", strip=True)

    result: dict = {
        "source_url":      resp.url,
        "owner_name":      "",
        "owner_street":    "",
        "owner_city":      "",
        "owner_state":     "OK",
        "owner_zip":       "",
        "property_street": "",
        "property_city":   "",
        "property_zip":    "",
        "parcel_id":       "",
        "total_due":       0.0,
    }

    # Owner name + mailing address block
    owner_m = re.search(
        r"Owner Name and Address\s+(.+?)\s+(?:Property ID|Taxroll)",
        text, re.IGNORECASE | re.DOTALL,
    )
    if owner_m:
        block = owner_m.group(1).strip()
        parts = [p.strip() for p in re.split(r"\s{2,}|\n", block) if p.strip()]
        addr_part, name_parts = "", []
        for p in parts:
            if re.search(r"\bOK\b", p, re.IGNORECASE) and _ZIP_RE.search(p):
                addr_part = p
            else:
                name_parts.append(p)
        result["owner_name"] = " ".join(name_parts).strip()
        if addr_part:
            zip_m = _ZIP_RE.search(addr_part)
            result["owner_zip"] = zip_m.group(1) if zip_m else ""
            clean = _ZIP_RE.sub("", addr_part).strip().rstrip("-").strip()
            ok_m = re.search(r"(.+?)\s+OK\b", clean, re.IGNORECASE)
            if ok_m:
                sc = ok_m.group(1).strip()
                comma = sc.rfind(",")
                if comma > 0:
                    result["owner_street"] = sc[:comma].strip().title()
                    result["owner_city"]   = sc[comma + 1:].strip().title()
                else:
                    result["owner_street"] = sc.title()
            result["owner_state"] = "OK"

    if not result["owner_name"]:
        link = soup.find("a", href=re.compile(r"owner_details"))
        if link:
            result["owner_name"] = link.get_text(strip=True)

    # Property location
    loc_m = re.search(r"Location\s*:\s*(.+?)(?:School|Tax Year|$)", text, re.IGNORECASE)
    if loc_m:
        # Normalize non-breaking spaces and strip "CITY OF" prefix
        loc = loc_m.group(1).replace("\xa0", " ")
        loc = re.sub(r"\bCITY OF\b", "", loc, flags=re.IGNORECASE)
        loc = re.sub(r"\s+", " ", loc).strip()
        suf_m = _SUFFIX_RE.match(loc)
        if suf_m:
            result["property_street"] = suf_m.group(1).strip().title()
            remainder = suf_m.group(2).strip()
            # Remainder may be "Tulsa 74136" or "Tulsa, OK 74136" — split out zip
            zip_m = _ZIP_RE.search(remainder)
            if zip_m:
                result["property_zip"] = zip_m.group(1)
                remainder = _ZIP_RE.sub("", remainder).strip().rstrip(",").strip()
            result["property_city"] = re.sub(r"\s+OK\b.*", "", remainder, flags=re.IGNORECASE).strip().title()
        else:
            result["property_street"] = loc.title()

    # Parcel ID
    pid_m = re.search(r"Property ID\s*:\s*([\w\-]+)", text, re.IGNORECASE)
    if pid_m:
        result["parcel_id"] = pid_m.group(1).strip()

    # Total due
    due_m = re.search(r"Total Due\s+([\d,]+\.\d{2})", text)
    if due_m:
        result["total_due"] = _safe_float(due_m.group(1))

    return result


# ── Public entry point ─────────────────────────────────────────────────


def scrape_tulsa_tax_delinquent(
    min_years_delinquent: int = 2,
    min_amount: float = 500.0,
    max_records: int = 500,
    delay: float = 0.35,
    newer_year: Optional[int] = None,
    force_refresh: bool = False,
) -> list[NoticeData]:
    """Scrape Tulsa County tax-delinquent residential properties.

    Finds properties delinquent in the two most recent consecutive tax
    years (confirmed 2+ year delinquents), sorted by amount owed descending.

    Args:
        min_years_delinquent: Must be 2 (multi-year intersection approach).
        min_amount:           Skip records with total_due below this ($500).
        max_records:          Max NoticeData records to return after filters.
        delay:                Polite delay between HTTP requests in seconds.
        newer_year:           Override the most recent tax year to check.
                              Defaults to current calendar year minus 1.
        force_refresh:        Ignore disk cache and do a full re-scan.
    """
    now = datetime.now()
    if newer_year is None:
        newer_year = now.year - 1   # most recent fully-due tax year
    older_year = newer_year - 1

    logger.info(
        "Tulsa tax delinquent: scanning years %d+%d intersection "
        "(min_amount=$%.0f, max=%d)",
        older_year, newer_year, min_amount, max_records,
    )

    session = requests.Session()
    session.headers.update(_HEADERS)

    # ── Step 1: Scan combined query, build two-year maps ─────────────
    cached = None if force_refresh else _load_cache(older_year, newer_year)
    if cached:
        older_map, newer_map = cached
    else:
        older_map, newer_map = _collect_two_years(
            session, older_year, newer_year, delay=delay
        )
        _save_cache(older_map, newer_map, older_year, newer_year)

    if not older_map or not newer_map:
        logger.warning("No RE records found for one or both years — aborting")
        return []

    # ── Step 2: Intersection = confirmed 2+ year delinquents ─────────
    common_ids = set(older_map) & set(newer_map)
    logger.info(
        "Intersection (%d & %d): %d properties delinquent in both years",
        older_year, newer_year, len(common_ids),
    )

    if not common_ids:
        return []

    # Sort by newer-year amount desc (highest debt = most motivated seller)
    sorted_ids = sorted(
        common_ids,
        key=lambda tid: newer_map[tid]["total_due"],
        reverse=True,
    )

    # Apply min_amount filter using newer-year amount
    sorted_ids = [
        tid for tid in sorted_ids
        if newer_map[tid]["total_due"] >= min_amount
    ]
    logger.info(
        "After min_amount filter ($%.0f): %d records",
        min_amount, len(sorted_ids),
    )

    # ── Step 3: Fetch detail pages for top N ─────────────────────────
    seen_parcel_ids = _load_seen_parcel_ids()

    logger.info(
        "Fetching detail pages for top %d records...",
        min(len(sorted_ids), max_records),
    )

    notices: list[NoticeData] = []
    date_added = now.strftime("%Y-%m-%d")

    for i, tax_id in enumerate(sorted_ids, 1):
        if len(notices) >= max_records:
            break

        rec = newer_map[tax_id]   # use newer year's record for detail page
        logger.info("[%d] %s — due=$%.0f", i, rec["owner_name"], rec["total_due"])

        detail = _fetch_detail(session, rec["tax_data_id"], newer_year)
        if not detail:
            logger.debug("  Detail fetch failed — skip")
            time.sleep(delay)
            continue

        # Skip parcels already in the output CSV (avoid re-importing known leads)
        parcel_id = detail.get("parcel_id", "").strip()
        if parcel_id and parcel_id in seen_parcel_ids:
            logger.debug("  Parcel %s already in output — skip", parcel_id)
            continue

        prop_street = detail["property_street"] or detail["owner_street"]
        prop_city   = detail["property_city"]   or detail["owner_city"]
        if not prop_street:
            logger.debug("  No property address — skip")
            time.sleep(delay)
            continue

        delinquent_years = sorted({older_year, newer_year})
        total_due = detail["total_due"] or rec["total_due"]
        owner = detail["owner_name"] or rec["owner_name"]

        logger.info(
            "  [OK] %s -- %s, %s | years=%s | due=$%.0f",
            owner, prop_street, prop_city, delinquent_years, total_due,
        )

        notices.append(NoticeData(
            date_added           = date_added,
            address              = prop_street,
            city                 = prop_city,
            state                = "OK",
            zip                  = detail["property_zip"] or detail["owner_zip"],
            owner_name           = owner,
            owner_street         = detail["owner_street"],
            owner_city           = detail["owner_city"],
            owner_state          = detail["owner_state"] or "OK",
            owner_zip            = detail["owner_zip"],
            notice_type          = "tax_delinquent",
            county               = "Tulsa",
            parcel_id            = detail["parcel_id"],
            tax_delinquent_amount= str(total_due),
            tax_delinquent_years = ",".join(str(y) for y in delinquent_years),
            source_url           = detail["source_url"],
        ))
        time.sleep(delay)

    logger.info(
        "Tulsa tax delinquent complete: %d records (%d intersection, %d checked)",
        len(notices), len(common_ids), i if sorted_ids else 0,
    )
    return notices
