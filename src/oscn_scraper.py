"""Oklahoma State Courts Network (OSCN) scraper.

Fetches case filings from Tulsa County District Court for:
  - Probate (PB / dcct=7)  — petitioner = PR/executor; decedent from case title
  - Small Claims / FED (SC / dcct=26) — plaintiff = landlord (our contact)
  - Civil relief (CJ / dcct=2) — defendant = mortgagor (foreclosure)

OSCN is public. The search form at Search.aspx POSTs to Results.aspx.
Plain HTTP requests work fine — only headless Playwright is blocked by
OSCN's Cloudflare Turnstile. No login, no CAPTCHA needed.

Case-type → dcct value mapping (from OSCN dropdown):
  7  = Probate
  26 = Small Claims  (includes Forcible Entry and Detainer / FED evictions)
  2  = Civil relief more than $10,000  (includes mortgage foreclosures)
  1  = Civil relief less than $10,000

Results URL: https://www.oscn.net/dockets/Results.aspx
Case detail: https://www.oscn.net/dockets/GetCaseInformation.aspx?db=tulsa&number=PB-2025-1
"""

import logging
import re
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

OSCN_RESULTS_URL = "https://www.oscn.net/dockets/Results.aspx"
OSCN_BASE = "https://www.oscn.net"

OSCN_DCCT: dict[str, str] = {
    "probate":     "7",   # Probate
    "eviction":    "26",  # Small Claims (includes FED)
    "foreclosure": "2",   # Civil relief > $10,000  (mortgage foreclosures)
}

OSCN_COUNTY_DB: dict[str, str] = {
    "Tulsa": "tulsa",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.oscn.net/dockets/Search.aspx",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://www.oscn.net",
}

_CASE_NUM_RE = re.compile(r"\b([A-Z]{1,3}-\d{4}-\d+)\b")
_DATE_RE = re.compile(r"^(\d{1,2}/\d{1,2}/\d{4})$")
_DECEDENT_RE = re.compile(
    r"(?:Estate\s+of|In\s+Re[:,]?\s+(?:the\s+Estate\s+of\s+)?)\s*([A-Z][A-Za-z ,'.'-]+)",
    re.IGNORECASE,
)
_STATUS_WORDS = frozenset(("open", "closed", "active", "inactive", "disposed", "dismissed"))

# ── Mortgage lender identification ───────────────────────────────────
# Keywords that identify mortgage lenders/servicers as plaintiffs.
# Matched against the plaintiff name on the results page to pre-filter
# CJ cases before fetching detail pages.
_LENDER_KEYWORDS = (
    "MORTGAGE", "BANK", "CREDIT UNION", "FEDERAL NATIONAL",
    "WELLS FARGO", "MIDFIRST", "NATIONSTAR", "MR. COOPER",
    "LOAN SERVI", "SAVINGS FUND", "SAVINGS BANK",
    "FEDERAL HOME", "PENNYMAC", "FREEDOM MORT", "NEWREZ",
    "SHELLPOINT", "CALIBER", "CARRINGTON", "LAKEVIEW",
    "SPECIALIZED LOAN", "ARVEST", "GATEWAY MORT",
    "MELLON", "WILMINGTON", "DEUTSCHE", "US BANK",
    "TRUST COMPANY", "TRUIST", "CHASE", "CITIBANK", "CITIMORTGAGE",
    "ROCKET", "QUICKEN", "GUILD MORT", "FLAGSTAR", "CROSSCOUNTRY",
    "OCWEN", "PHH", "BAYVIEW", "DITECH", "HOMEPOINT",
    "BOKF", "ONITY", "PLANET HOME", "HOME FEDERAL",
    "TINKER FEDERAL", "FIRST TECHNOLOGY",
)
# Plaintiffs that match _LENDER_KEYWORDS but file non-mortgage cases
_LENDER_EXCLUDE = (
    "AMERICAN EXPRESS", "CAPITAL ONE", "DISCOVER", "SYNCHRONY",
    "PORTFOLIO RECOVERY", "MIDLAND CREDIT", "LVNV FUNDING",
    "CACH LLC", "CAVALRY SPV", "CAVALRY PORTFOLIO",
    "BARCLAYS", "ONEMAIN", "CONSUMER PORTFOLIO",
    "WESTLAKE", "EXETER", "WORLD ACCEPTANCE",
)


def _is_mortgage_lender(plaintiff_name: str) -> bool:
    """Check if a plaintiff name matches a known mortgage lender."""
    pu = plaintiff_name.upper()
    if any(ex in pu for ex in _LENDER_EXCLUDE):
        return False
    return any(kw in pu for kw in _LENDER_KEYWORDS)


# Defendants that are NOT the property owner — deprioritize these so the
# actual homeowner gets selected as the record's owner_name.
_NON_PERSON_DEFENDANTS = re.compile(
    r"DEPARTMENT OF|OKLAHOMA TAX|TAX COMMISSION|COUNTY COMMISSION|"
    r"TREASURER OF|BOARD OF|UNITED STATES|STATE OF OKLAHOMA|"
    r"HOMEOWNERS ASSOC|HOA\b|ASSOCIATION INC|"
    r"\bLLC\b|\bINC\b|\bCORP\b|\bLTD\b|\bLP\b|"
    r"\bBANK\b|CREDIT UNION|FINANCIAL|LENDING|SERVICING|"
    r"TOWNHOME|CONDOMINIUM|CONDO\b|VILLAGE\b.*(?:HOME|TOWN)|"
    r"UNKNOWN OCCUPANT|JOHN DOE|JANE DOE|SPOUSE",
    re.IGNORECASE,
)


def _is_person_defendant(name: str) -> bool:
    """Return True if the defendant name looks like a real person, not an entity."""
    return not bool(_NON_PERSON_DEFENDANTS.search(name))


def _extract_lender_cases(html: str) -> set[str]:
    """Extract case numbers where the plaintiff is a known mortgage lender.

    Parses the results page HTML once, checking plaintiff names against
    lender keywords. Returns the set of case numbers to keep.
    """
    soup = BeautifulSoup(html, "lxml")
    lender_cases: set[str] = set()
    for row in soup.select("table tr"):
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cells) < 4:
            continue
        case_num_m = _CASE_NUM_RE.search(cells[0])
        if not case_num_m:
            continue
        party_info = cells[3] if len(cells) > 3 else ""
        if "(Plaintiff)" in party_info:
            plaintiff = party_info.replace("(Plaintiff)", "").strip()
            if _is_mortgage_lender(plaintiff):
                lender_cases.add(case_num_m.group(1))
                logger.debug("  Lender match: %s in %s", plaintiff, case_num_m.group(1))
    return lender_cases


def scrape_oscn(
    county: str,
    notice_type: str,
    since_date: str,
    until_date: Optional[str] = None,
    max_cases: int = 500,
    **_kwargs,  # absorb headless/captcha_api_key kwargs from old callers
) -> list[NoticeData]:
    """Search OSCN via HTTP POST and return NoticeData for recently filed cases."""
    db = OSCN_COUNTY_DB.get(county, county.lower())
    dcct = OSCN_DCCT.get(notice_type)
    if not dcct:
        logger.error("Unsupported OSCN notice_type: %s (valid: %s)", notice_type, list(OSCN_DCCT))
        return []

    since_fmt = _to_mdy(since_date)
    until_fmt = _to_mdy(until_date) if until_date else _to_mdy(datetime.now().strftime("%Y-%m-%d"))

    logger.info(
        "OSCN: %s/%s  %s -> %s  (dcct=%s db=%s)",
        county, notice_type, since_fmt, until_fmt, dcct, db,
    )

    payload = {
        "db":       db,
        "dcct":     dcct,
        "FiledDateL": since_fmt,
        "FiledDateH": until_fmt,
        "lname": "", "fname": "", "mname": "",
        "number": "", "partytype": "", "DoBMin": "", "DoBMax": "",
    }

    try:
        resp = requests.post(
            OSCN_RESULTS_URL,
            data=payload,
            headers=_HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.error("OSCN HTTP error: %s", e)
        return []

    if "OSCN Turnstile" in resp.text:
        logger.error(
            "OSCN: Turnstile challenge on Results.aspx — unexpected. "
            "Try again; if it persists the IP may be rate-limited."
        )
        return []

    # For foreclosure (CJ) cases, pre-filter by plaintiff name on the results
    # page BEFORE parsing defendants. This avoids fetching detail pages for
    # debt collection, auto negligence, replevin, etc.
    lender_cases: set[str] | None = None
    if notice_type == "foreclosure":
        lender_cases = _extract_lender_cases(resp.text)
        logger.info(
            "OSCN: %d cases have mortgage lender plaintiffs (pre-filter)",
            len(lender_cases),
        )

    notices = _parse_html(resp.text, db, county, notice_type, max_cases,
                          allowed_cases=lender_cases)
    logger.info("OSCN %s/%s: %d cases after lender filter", county, notice_type, len(notices))

    # For the remaining lender-filtered cases, verify issue type on detail page.
    # Credit unions and some banks file non-mortgage cases (replevin, etc.),
    # so this catches false positives from the lender keyword match.
    if notice_type == "foreclosure" and notices:
        notices = _filter_real_foreclosures(notices)

    return notices


# ── HTML parsing ──────────────────────────────────────────────────────


def _parse_html(
    html: str,
    db: str,
    county: str,
    notice_type: str,
    max_cases: int,
    allowed_cases: set[str] | None = None,
) -> list[NoticeData]:
    soup = BeautifulSoup(html, "lxml")
    body_text = soup.get_text(" ", strip=True).lower()

    if any(p in body_text for p in ("no cases found", "0 cases", "no results found")):
        logger.info("OSCN: no cases in response")
        return []

    rows = soup.select("table tr")
    if not rows:
        logger.warning("OSCN: no table rows in response. Snippet: %s", body_text[:500])
        return []

    logger.debug("OSCN: %d table rows to parse", len(rows))

    case_best: dict[str, tuple[int, NoticeData]] = {}
    # Track the current case number across multi-row OSCN results.
    # OSCN shows one row per party — the case number only appears in the
    # first row; subsequent party rows have an empty first cell.  Without
    # tracking, the lender pre-filter only blocks the first row and lets
    # every following defendant row slip through unchecked.
    _current_case_num: str | None = None

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        cell_texts = [td.get_text(strip=True) for td in cells]
        cell_hrefs = []
        for td in cells:
            a = td.find("a")
            cell_hrefs.append(a["href"] if a and a.get("href") else "")

        joined_lower = " ".join(cell_texts).lower()
        if any(h in joined_lower for h in ("case number", "party name", "filed date", "case type")):
            continue

        # Update current case tracker whenever the first cell has a case number
        case_m_first = _CASE_NUM_RE.search(cell_texts[0]) if cell_texts else None
        if case_m_first:
            _current_case_num = case_m_first.group(1)

        # Skip cases not in the allowed set (lender pre-filter for foreclosure).
        # Use _current_case_num so ALL rows for a rejected case are filtered,
        # not just the first row (which is the only one with a case number in cell[0]).
        if allowed_cases is not None and _current_case_num is not None:
            if _current_case_num not in allowed_cases:
                continue

        result = _row_to_notice(cell_texts, cell_hrefs, db, county, notice_type)
        if result is None:
            continue

        priority, notice = result
        ck_m = re.search(r"[A-Z]{1,3}-\d{4}-\d+", notice.source_url or "")
        ck = ck_m.group(0) if ck_m else notice.source_url

        if ck not in case_best:
            case_best[ck] = (priority, notice)
        elif priority < case_best[ck][0]:
            # Upgrading to a preferred party row — carry over any decedent info already found
            old_notice = case_best[ck][1]
            if notice_type == "probate" and not notice.decedent_name and old_notice.decedent_name:
                notice.decedent_name = old_notice.decedent_name
            case_best[ck] = (priority, notice)
        else:
            # Lower-or-equal priority row — don't replace, but merge decedent for probate
            if notice_type == "probate" and notice.decedent_name:
                existing_n = case_best[ck][1]
                if not existing_n.decedent_name:
                    existing_n.decedent_name = notice.decedent_name

    notices = [n for _, n in list(case_best.values())[:max_cases]]

    # Drop foreclosure cases where the best defendant is a non-person entity
    # (commercial property — no residential homeowner to contact)
    if notice_type == "foreclosure" and allowed_cases is not None:
        before = len(notices)
        notices = [n for n in notices if _is_person_defendant(n.owner_name)]
        dropped = before - len(notices)
        if dropped:
            logger.info("OSCN: dropped %d commercial/entity-only foreclosure cases", dropped)

    return notices


_ROLE_RE = re.compile(r"\(([^)]+)\)\s*$")

# priority: 1 = ideal role, 2 = estate/case-name row, 99 = skip
def _row_to_notice(
    cells: list[str],
    hrefs: list[str],
    db: str,
    county: str,
    notice_type: str,
) -> Optional[tuple[int, NoticeData]]:
    """Parse one OSCN result row. Returns (priority, NoticeData) or None to skip."""
    case_num = filed_date = source_url = ""
    raw_name = ""

    for i, cell in enumerate(cells):
        m = _CASE_NUM_RE.search(cell)
        if m:
            case_num = m.group(1)
            if hrefs[i]:
                href = hrefs[i]
                if href.startswith("http"):
                    source_url = href
                elif href.startswith("/"):
                    source_url = OSCN_BASE + href
                else:
                    source_url = f"{OSCN_BASE}/dockets/{href}"
        elif _DATE_RE.match(cell):
            if not filed_date:
                filed_date = cell
        elif cell and len(cell) > 2 and cell.lower() not in _STATUS_WORDS:
            if not re.match(r"^\d+$", cell):
                raw_name = cell

    if not case_num:
        return None

    if not source_url:
        source_url = f"{OSCN_BASE}/dockets/GetCaseInformation.aspx?db={db}&number={case_num}"

    date_added = ""
    if filed_date:
        try:
            date_added = datetime.strptime(filed_date, "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            date_added = filed_date

    owner_name = decedent_name = ""
    role_m = _ROLE_RE.search(raw_name)
    role = role_m.group(1).lower() if role_m else ""
    clean_name = _ROLE_RE.sub("", raw_name).strip()  # name without "(Role)" suffix

    # ── Probate ──────────────────────────────────────────────────────
    if notice_type == "probate":
        if role in ("attorney", "respondent"):
            return None
        estate_m = _DECEDENT_RE.search(raw_name)
        if estate_m:
            # Row is "ESTATE OF JOHN DOE" — gives decedent info only, priority 2
            decedent_name = estate_m.group(1).strip().rstrip(",.")
            owner_name = decedent_name  # placeholder — real owner comes from Petitioner row
            priority = 2
        elif role in ("deceased", "decedent", "testator"):
            # Explicit deceased party row — also gives decedent name
            decedent_name = clean_name
            owner_name = ""
            priority = 2
        elif role in ("petitioner", "petitioner/appellant", "personal representative"):
            # Best row: the executor/PR
            owner_name = clean_name
            priority = 1
        else:
            owner_name = clean_name
            priority = 3

    # ── Eviction (Small Claims / FED) ────────────────────────────────
    elif notice_type == "eviction":
        if role in ("attorney",):
            return None
        if role in ("plaintiff", "appellant"):
            # Landlord = our contact
            owner_name = clean_name
            priority = 1
        elif role in ("defendant", "appellee"):
            # Tenant — we skip unless no plaintiff row exists
            owner_name = clean_name
            priority = 3
        else:
            owner_name = clean_name
            priority = 3

    # ── Foreclosure (Civil) ───────────────────────────────────────────
    elif notice_type == "foreclosure":
        if role in ("plaintiff", "attorney"):
            return None  # bank/lender — not the property owner
        if role in ("defendant", "appellee"):
            owner_name = clean_name
            priority = 1 if _is_person_defendant(clean_name) else 5
        else:
            owner_name = clean_name
            priority = 3

    else:
        owner_name = clean_name
        priority = 1

    return priority, NoticeData(
        date_added=date_added,
        owner_name=owner_name,
        decedent_name=decedent_name,
        notice_type=notice_type,
        county=county,
        state="OK",
        source_url=source_url,
        raw_text=" | ".join(cells),
    )


def _to_mdy(date_str: str) -> str:
    """YYYY-MM-DD -> M/D/YYYY without leading zeros (OSCN form format)."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{dt.month}/{dt.day}/{dt.year}"
    except ValueError:
        return date_str


# ── Foreclosure case-type verification ───────────────────────────────

# Issue type keywords that indicate a real mortgage foreclosure
_FORECLOSURE_ISSUES = re.compile(
    r"FORECLOS|MORTGAGE|DEED\s+OF\s+TRUST|NOTE\s+AND\s+MORTGAGE|"
    r"FORFEITURE|QUIET\s+TITLE|LAND\s+SALE|TAX\s+LIEN\s+FORECL|"
    r"MORTGAGE\s+FORECL",
    re.IGNORECASE,
)

# Docket codes to skip when looking for the issue type entry
_DOCKET_SKIP_CODES = frozenset({
    "TEXT", "DMFE", "PFE1", "PFE2", "PFE7", "PFFP",
    "OCISR", "OCJC", "ASGR", "ASGN", "ALIAS", "AFF",
    "SMNS", "CERT", "RTRN", "MISC", "MOTN", "ORDR",
})

_DELAY_BETWEEN_DETAIL = 12  # seconds between detail page fetches (OSCN Turnstiles after ~5 rapid GETs)


def _fetch_case_issue_type(source_url: str) -> Optional[str]:
    """Fetch an OSCN case detail page and extract the issue type.

    Returns the issue description string (e.g. "FORECLOSURE OF MORTGAGE"),
    or None if the page can't be fetched. Retries once after 15s on Turnstile.
    """
    import time as _time

    for attempt in range(2):
        try:
            resp = requests.get(source_url, headers=_HEADERS, timeout=15)
            if resp.status_code != 200:
                return None
            if "Turnstile" in resp.text:
                if attempt == 0:
                    logger.debug("OSCN detail: Turnstile on %s -- retrying in 30s", source_url)
                    _time.sleep(30)
                    continue
                logger.debug("OSCN detail: Turnstile persists on %s", source_url)
                return None

            soup = BeautifulSoup(resp.text, "lxml")
            for row in soup.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
                if len(cells) < 3:
                    continue
                code = cells[1].strip().upper()
                desc = cells[2].strip()
                if not code or not desc or len(desc) < 3 or len(desc) > 100:
                    continue
                if code in _DOCKET_SKIP_CODES:
                    continue
                if desc.upper().startswith("CIVIL RELIEF") or desc.startswith("$"):
                    continue
                return desc
        except Exception as e:
            logger.debug("OSCN detail fetch failed for %s: %s", source_url, e)
    return None


def _filter_real_foreclosures(notices: list[NoticeData]) -> list[NoticeData]:
    """Filter CJ cases to only keep real foreclosures by checking case detail pages.

    Fetches each case's OSCN detail page, extracts the issue type, and keeps
    only cases where the issue type matches foreclosure-related keywords.
    """
    import time as _time

    if not notices:
        return notices

    logger.info("OSCN: verifying %d CJ cases are real foreclosures...", len(notices))

    kept = []
    removed_types: list[str] = []
    turnstile_count = 0

    for notice in notices:
        if not notice.source_url or "oscn.net" not in notice.source_url:
            kept.append(notice)
            continue

        issue = _fetch_case_issue_type(notice.source_url)
        _time.sleep(_DELAY_BETWEEN_DETAIL)

        if issue is None:
            # Turnstile or fetch failure -- drop unverified records
            turnstile_count += 1
            removed_types.append("(Turnstile/fetch failure)")
            continue

        if _FORECLOSURE_ISSUES.search(issue):
            logger.debug(
                "  OSCN keep: %s -- issue=%s", notice.owner_name, issue,
            )
            kept.append(notice)
        else:
            removed_types.append(issue)
            logger.debug(
                "  OSCN drop: %s -- issue=%s (not foreclosure)",
                notice.owner_name, issue,
            )

    removed = len(notices) - len(kept)
    if removed:
        logger.info(
            "OSCN: removed %d non-foreclosure CJ cases (%d kept, %d unverifiable)",
            removed, len(kept), turnstile_count,
        )
        type_counts: dict[str, int] = {}
        for t in removed_types:
            type_counts[t] = type_counts.get(t, 0) + 1
        for t, c in sorted(type_counts.items(), key=lambda x: -x[1])[:5]:
            logger.info("    %d x %s", c, t)
    else:
        logger.info("OSCN: all %d CJ cases verified as real foreclosures", len(kept))

    return kept
