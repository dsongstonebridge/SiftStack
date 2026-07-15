"""Tulsa County Assessor property lookup by owner name — 6-tier fallback strategy.

Tier 1: Tulsa County Assessor — full name "LAST FIRST MIDDLE"
Tier 2: Tulsa County Assessor — short name "LAST FIRST" (drop middle)
Tier 3: Tulsa County Assessor — maiden name variant (penultimate surname)
Tier 4: Tulsa County Assessor — last name only (rare/unique names)
Tier 5: Executor family search — search PR/executor's name, filter by decedent surname
Tier 6: People search — CyberBackgroundChecks via Firecrawl (requires FIRECRAWL_API_KEY)

Mirrors the Knox County 3-tier property lookup strategy in property_lookup.py,
extended with additional fallbacks for the Tulsa Assessor's name format.
"""

import asyncio
import logging
import re
from typing import Optional

from playwright.async_api import Page, async_playwright

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

ASSESSOR_SEARCH_URL = "https://assessor.tulsacounty.org/Property/Search?terms={terms}&filterTag=null"

_STREET_SUFFIXES = frozenset({
    "AV", "AVE", "BLVD", "CIR", "CT", "DR", "HWY", "LN", "PKWY",
    "PL", "RD", "ST", "TER", "TR", "TRL", "WAY", "LOOP", "PASS",
    "RUN", "RIDGE", "CV", "XING", "PLACE", "STREET", "AVENUE",
    "ROAD", "DRIVE", "LANE", "COURT", "CIRCLE", "BOULEVARD", "PIKE",
})
_DIRECTIONALS = frozenset({"N", "S", "E", "W", "NE", "NW", "SE", "SW"})
_ZIP_RE = re.compile(r"^(\d{5})\d{0,4}$")

# Oklahoma address in people search results: "1234 S Main St, Tulsa, OK 74105"
_PEOPLE_ADDR_RE = re.compile(
    r"(\d{1,5}\s+[^,\n]{5,50}),\s*([A-Za-z ]+),\s*(OK|Oklahoma)\s*(\d{5})?",
    re.IGNORECASE,
)


# ── Address parsing ───────────────────────────────────────────────────


def _parse_assessor_address(raw: str) -> tuple[str, str, str]:
    """Parse Tulsa Assessor full-address string into (street, city, zip).

    '11405 S GRANITE AV E TULSA 741378110' → ('11405 S Granite Ave E', 'Tulsa', '74137')
    """
    tokens = raw.strip().upper().split()
    if not tokens:
        return raw, "", ""

    zip_code = ""
    if tokens and _ZIP_RE.match(tokens[-1]):
        zip_code = _ZIP_RE.match(tokens[-1]).group(1)
        tokens = tokens[:-1]

    last_suffix_idx = -1
    for i, tok in enumerate(tokens):
        if tok in _STREET_SUFFIXES:
            last_suffix_idx = i

    if last_suffix_idx == -1:
        city = tokens[-1].title() if tokens else ""
        street = " ".join(tokens[:-1]).title() if len(tokens) > 1 else ""
        return street, city, zip_code

    street_end = last_suffix_idx
    if street_end + 1 < len(tokens) and tokens[street_end + 1] in _DIRECTIONALS:
        street_end += 1

    street = " ".join(tokens[: street_end + 1]).title()
    city = " ".join(tokens[street_end + 1 :]).title()
    return street, city, zip_code


# ── Name scoring ──────────────────────────────────────────────────────


def _extract_surname(candidate: str) -> str:
    """Extract the primary/record-holder surname from an Assessor owner_name.

    Records are 'SURNAME, FIRST MIDDLE [AND CO-OWNER...]' or, for trusts
    with no comma, 'SURNAME [FAMILY] TRUST C/O FIRST M & FIRST2 M2 SURNAME
    TTEES' — surname is reliably the first token either way.
    """
    candidate = candidate.strip().upper()
    if not candidate:
        return ""
    if "," in candidate:
        head = candidate.split(",", 1)[0].strip()
        return head.split()[-1] if head else ""
    return candidate.split()[0]


def _fuzzy_first_name_eq(a: str, b: str) -> bool:
    """True if two uppercased first names match exactly or share a 4+ char
    prefix (tolerates minor spelling variants, e.g. Michelle vs Michele)."""
    if not a or not b:
        return False
    if a == b:
        return True
    prefix_len = min(5, len(a), len(b))
    return prefix_len >= 4 and a[:prefix_len] == b[:prefix_len]


def _score_name_match(query: str, candidate: str, dm_first: str = "") -> float:
    """Token-overlap score between query and candidate owner name (0.0-1.0).

    Surname is a hard gate: the query's last name must match the
    candidate's actual record-holder surname (not just appear anywhere
    in the string as a token). Without this, a query like "Nancy Daniel"
    can false-positive match "SIEGEL ... C/O DANIEL E & NANCY J SIEGEL"
    purely because "Daniel" coincidentally appears as someone else's
    first name in an unrelated family's trust.

    dm_first: uppercased first name of a known decision-maker (e.g. a
    surviving spouse), if any — exempts the leading-name penalty below
    when the deed's leading name IS the known DM (trust deeds commonly
    list the surviving spouse first: "BELL, IVEN AND RUTH TRUST").
    """
    q_tokens_list = re.findall(r"\b[A-Za-z]{2,}\b", query.upper())
    if not q_tokens_list:
        return 0.0
    query_surname = q_tokens_list[-1]
    candidate_surname = _extract_surname(candidate)
    if query_surname != candidate_surname:
        return 0.0

    q_tokens = set(q_tokens_list)
    c_tokens = set(re.findall(r"\b[A-Za-z]{2,}\b", candidate.upper()))
    score = len(q_tokens & c_tokens) / len(q_tokens)

    # Penalize when the deed's leading first name doesn't appear in the query.
    # e.g. query="Charles Davis", deed="DAVIS, ANTHONY CHARLES" → "ANTHONY" is
    # not in the query — this is likely a different person who shares the surname
    # and a middle/second name.  Skip short tokens (initials) and suffixes.
    _SUFFIXES = {"JR", "SR", "II", "III", "IV"}
    if "," in candidate.upper():
        after_comma = candidate.upper().split(",", 1)[1].strip()
        first_tokens = re.findall(r"\b[A-Za-z]{2,}\b", after_comma)
        if first_tokens:
            leading = first_tokens[0]
            if (
                len(leading) >= 4
                and leading not in _SUFFIXES
                and leading not in q_tokens
                and not _fuzzy_first_name_eq(leading, dm_first)
            ):
                score *= 0.7

    return score


def _dm_corroborates(candidate: str, dm_name: str) -> Optional[bool]:
    """Check whether a known decision-maker's first name appears anywhere in
    the property record's owner string. Returns None if there's no DM to
    check or no co-owner section to check against (can't confirm OR deny);
    True/False otherwise.

    Searches the FULL owner string, not just the text after "AND"/"&" —
    trust deeds commonly list a surviving spouse as the leading co-trustee
    ("BELL, IVEN AND RUTH TRUST"), so limiting the search to the co-owner
    portion would miss the DM's name entirely.

    Uses a prefix-fuzzy match (4+ shared leading chars) to tolerate minor
    spelling variants between obituary and county records (e.g. Michelle
    vs Michele).
    """
    if not dm_name:
        return None
    candidate_u = candidate.upper()
    # Only meaningful if the record actually lists a co-owner. '&' has no
    # word-boundary transition (flanked by spaces), so check it separately
    # from the \bAND\b word match rather than combining into one \b(...)\b.
    if not (re.search(r"\bAND\b", candidate_u) or "&" in candidate_u):
        return None
    dm_first = dm_name.strip().split()[0].upper() if dm_name.strip() else ""
    if not dm_first or len(dm_first) < 3:
        return None
    candidate_tokens = re.findall(r"\b[A-Za-z]{2,}\b", candidate_u)
    for tok in candidate_tokens:
        if _fuzzy_first_name_eq(dm_first, tok):
            return True
    # No direct match anywhere in the string. If the co-owner portion is a
    # living trust / TTEE entity (not a named person), we can't verify the
    # DM either way — return None to avoid a false SUSPECT.
    # e.g. "CRABB, JAY & JENNIFER TRUST" → co-owner is a trust, not a rival spouse.
    co_match = re.search(r"(?:&|\bAND\b)\s+(.+)$", candidate_u)
    if co_match:
        co_portion = co_match.group(1).strip()
        if re.search(r"\b(?:TRUST|TTEES?|REV|REVOCABLE|LIVING|TR)\b", co_portion):
            return None
    return False


# DM relationships that are heirs but not deed co-owners.
# For these, we prefer sole-owner records (widowed/single) rather than
# records with a co-owner (unrelated spouse of a different John Moss, etc.).
_NON_SPOUSAL = {"child", "son", "daughter", "grandchild", "sibling", "parent"}


def _non_spousal_rejected(match: dict, dm_rel: str) -> bool:
    """Return True when a non-spousal DM match should be dropped.

    For child/grandchild/sibling DMs we only accept:
      - Sole-owner records (_dm_verified is None  → no co-owner on deed)
      - Trust deeds (_dm_verified is None from trust-detection → deceased IS
        named on deed, co-trustee is a legal structure not a rival owner)
      - Records where heir is confirmed on deed   (_dm_verified is True)
    Reject when a regular co-owner exists and doesn't match the child DM
    (_dm_verified is False) — that means a different living person is on the
    deed and this might be the wrong property entirely.
    """
    if dm_rel.lower() not in _NON_SPOUSAL:
        return False
    return match.get("_dm_verified") is False


def _best_match(
    results: list[dict],
    query_name: str,
    min_score: float = 0.4,
    dm_name: str = "",
    dm_relationship: str = "",
) -> Optional[dict]:
    """Return the highest-scoring residential property match.

    Boost logic depends on DM relationship:
    - Spouse: prefer records where DM appears as co-owner (family home)
    - Child/grandchild/sibling: prefer sole-owner records (widowed/single owner)
    - No DM: score-only, flag as AMBIGUOUS when multiple properties tie
    """
    if not results:
        return None
    residential = [r for r in results if "residential" in r.get("acct_type", "").lower()]
    candidates = residential if residential else results
    dm_first = dm_name.strip().split()[0].upper() if dm_name.strip() else ""
    scored = [(r, _score_name_match(query_name, r["owner_name"], dm_first)) for r in candidates]

    if dm_name:
        is_non_spousal = dm_relationship.lower() in _NON_SPOUSAL
        boosted = []
        for r, s in scored:
            corr = _dm_corroborates(r["owner_name"], dm_name)
            if is_non_spousal:
                # Prefer true sole-owner records (no & on deed at all).
                # Trust deeds also return corr=None but still have & on the deed —
                # don't give them the bonus or a lone sole-owner record won't win.
                owner_u = r.get("owner_name", "").upper()
                is_sole_owner = corr is None and not re.search(r"\s+(?:&|AND)\s+", owner_u)
                bonus = 0.05 if is_sole_owner else 0.0
            else:
                # Spouse: prefer records where spouse appears as co-owner
                bonus = 0.05 if corr is True else 0.0
            boosted.append((r, s + bonus))
        scored = boosted

    scored.sort(key=lambda x: x[1], reverse=True)
    if scored and scored[0][1] >= min_score:
        best = scored[0][0]
        top_score = scored[0][1]
        # Count ties within 0.01 tolerance — after boosting, genuine ties
        # mean we still can't disambiguate.
        tied = [r for r, s in scored if abs(s - top_score) < 0.01]
        ambiguous = len(tied) > 1
        if ambiguous:
            best["_ambiguous"] = True
            tied_addrs = ", ".join(r["street"] for r, s in scored if abs(s - top_score) < 0.01)
            logger.warning(
                "  Assessor match AMBIGUOUS: '%s' -> '%s' (score=%.2f) -- "
                "%d properties tied (same owner/DM may hold multiple parcels) — "
                "verify manually which is the actual residence: %s",
                query_name, best["owner_name"], top_score, len(tied), tied_addrs,
            )
        else:
            best["_ambiguous"] = False

        dm_verified = _dm_corroborates(best["owner_name"], dm_name)
        best["_dm_verified"] = dm_verified
        is_non_spousal = dm_relationship.lower() in _NON_SPOUSAL

        if dm_verified is False and not is_non_spousal:
            # Spouse DM not found among co-owners — suspicious
            logger.warning(
                "  Assessor match SUSPECT: '%s' -> '%s' (score=%.2f) -- "
                "known spouse '%s' not found among co-owners [%s]",
                query_name, best["owner_name"], scored[0][1], dm_name, best["street"],
            )
        elif dm_verified is False and is_non_spousal:
            # Child/grandchild not on deed — expected, not suspicious
            logger.debug(
                "  Assessor match: '%s' -> '%s' (score=%.2f) -- "
                "DM '%s' (%s) not on deed as expected [%s]",
                query_name, best["owner_name"], scored[0][1],
                dm_name, dm_relationship, best["street"],
            )
        elif not ambiguous:
            logger.debug(
                "  Assessor match: '%s' -> '%s' (score=%.2f, dm_verified=%s) [%s]",
                query_name, best["owner_name"], scored[0][1], dm_verified, best["street"],
            )
        return best
    return None


# ── Playwright Assessor scraper ───────────────────────────────────────

ASSESSOR_INFO_URL = "https://assessor.tulsacounty.org/Property/Info?accountNo={account_no}"

# Improvements table row format (from page text extraction):
# "YYYY\t(yr_blt)\tResidential\t(sqft) sqft\t(stories)\t(ceiling_ht)\t(baths)\tRoof type"
# Property Type column can be Residential, Condominium, Townhome, etc.
# Tax year is current year, Yr Blt is year property was built.
_IMPROVEMENTS_ROW_RE = re.compile(
    r"\d{4}\s+(\d{4})\s+([A-Za-z][A-Za-z ]{2,24}?)\s+([\d,]+)\s+sqft\s+[\d.]+\s+\d+\s+([\d.]+)",
    re.IGNORECASE,
)


def _apply_property_characteristics(notice: NoticeData, details: dict) -> None:
    """Map Assessor Info-page details onto a notice. Never touches address/owner."""
    if details.get("year_built"):
        notice.year_built = details["year_built"]
    if details.get("sqft"):
        notice.sqft = details["sqft"]
    if details.get("bedrooms"):
        notice.bedrooms = details["bedrooms"]
    if details.get("bathrooms"):
        notice.bathrooms = details["bathrooms"]
    if not notice.property_type and details.get("property_type_assessor"):
        pt = details["property_type_assessor"].lower()
        if "condo" in pt:
            notice.property_type = "Condo"
        elif "townhome" in pt or "townhouse" in pt:
            notice.property_type = "Townhouse"
        elif "residential" in pt:
            notice.property_type = "Single Family"
        else:
            notice.property_type = details["property_type_assessor"].title()


async def _fetch_property_details(page: Page, account_no: str) -> dict:
    """Fetch year built, sqft, baths from Assessor Property/Info page.

    The improvements table on the Info page has columns:
    Tax Year | Yr Blt | Property Type | Livable | Stories | Story Height | Baths | Roof
    Beds are not exposed on this page; omitted rather than guessed.
    """
    if not account_no:
        return {}
    url = ASSESSOR_INFO_URL.format(account_no=account_no.strip())
    try:
        await page.goto(url)
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(1.5)
        text = await page.inner_text("body")
    except Exception as e:
        logger.debug("Info page fetch failed for account %s: %s", account_no, e)
        return {}

    result = {}

    m = _IMPROVEMENTS_ROW_RE.search(text)
    if m:
        result["year_built"] = m.group(1)
        result["property_type_assessor"] = m.group(2).strip()
        result["sqft"] = m.group(3).replace(",", "")
        result["bathrooms"] = m.group(4)

    if result:
        logger.debug("  Info page (%s): %s", account_no, result)
    else:
        logger.debug("  Info page (%s): no property characteristics found", account_no)

    return result


async def _search_assessor(page: Page, name: str, max_pages: int = 5) -> list[dict]:
    """Search Tulsa County Assessor and return list of property dicts.

    Paginates through up to max_pages result pages so records that fall on
    page 2+ are not silently missed.  Pass max_pages=1 for callers that
    intentionally limit result volume (e.g. Tier 4 last-name-only search).
    """
    terms = name.strip().replace(" ", "+")
    if not terms:
        return []
    url = ASSESSOR_SEARCH_URL.format(terms=terms)
    try:
        await page.goto(url)
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(1.5)
    except Exception as e:
        logger.warning("Assessor nav failed for '%s': %s", name, e)
        return []

    body = await page.inner_text("body")
    body_l = body.lower()
    if "no data" in body_l or "no results" in body_l:
        return []
    # Detect rate-limit / CAPTCHA / error pages that would yield an empty table
    if any(k in body_l for k in ("too many requests", "captcha", "access denied", "403 forbidden", "rate limit")):
        logger.warning("  Assessor blocked for '%s': %s", name, body[:120].replace("\n", " "))
        return []

    _ROW_JS = r"""() => {
        const trs = Array.from(document.querySelectorAll("table tr"));
        return trs
            .map(tr => Array.from(tr.querySelectorAll("td")).map(c => c.innerText.trim()))
            .filter(r => r.length >= 6 && r[1] && !/^[A-Za-z\s#]+$/.test(r[1]));
    }"""

    _NEXT_JS = """() => {
        const selectors = [
            '#DataTables_Table_0_next:not(.disabled)',
            '.dataTables_paginate .next:not(.disabled)',
            '.paginate_button.next:not(.disabled)',
            '[aria-label="Next"]:not([disabled])',
            '[aria-label="next page"]:not([disabled])',
            'a[title="Next"]:not(.disabled)',
            'li.next:not(.disabled) a',
            '.pagination .next:not(.disabled) a',
            '.pager .next a',
        ];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el) { el.click(); return 'selector:' + sel; }
        }
        // Broader fallback: any <a>/<button> with "Next" or ">" or "›" text
        const all = [...document.querySelectorAll('a, button')];
        const nx = all.find(el => {
            const t = el.textContent.trim();
            return /^(next|>|›|»)$/i.test(t) &&
                   !el.classList.contains('disabled') &&
                   !el.hasAttribute('disabled') &&
                   el.offsetParent !== null;
        });
        if (nx) { nx.click(); return 'text:' + nx.textContent.trim(); }
        return false;
    }"""

    all_raw = []
    seen_accounts = set()

    for page_num in range(max_pages):
        rows = await page.evaluate(_ROW_JS)
        all_raw.extend(rows)

        if page_num + 1 >= max_pages:
            break

        clicked = await page.evaluate(_NEXT_JS)
        if not clicked:
            logger.debug("  Assessor pagination: no Next button found after page %d", page_num + 1)
            break
        logger.debug("  Assessor pagination: page %d -> %d (clicked %s)", page_num + 1, page_num + 2, clicked)
        await asyncio.sleep(1.5)

    results = []
    for row in all_raw:
        if len(row) < 6:
            continue
        owner_name = row[4]
        full_address = row[5]
        if not full_address or not owner_name:
            continue
        account_no = row[1]
        if account_no in seen_accounts:
            continue  # dedupe across page overlap
        seen_accounts.add(account_no)
        street, city, zip_code = _parse_assessor_address(full_address)
        results.append({
            "account_no": account_no,
            "acct_type":  row[2],
            "owner_name": owner_name,
            "full_address": full_address,
            "street": street,
            "city":   city,
            "zip":    zip_code,
        })
    return results


# ── Name variant helpers ──────────────────────────────────────────────


def _format_for_search(name: str) -> str:
    """Convert 'FIRST LAST' → 'LAST FIRST' for Tulsa Assessor.

    If already 'LAST, FIRST' (with comma), strips the comma.
    """
    name = name.strip().upper()
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        return f"{parts[0]} {parts[1]}".strip()
    name = re.sub(r"\b(?:JR|SR|II|III|IV|MD)\b\.?", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(r"\s+(?:AND|&)\s+.*", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(r"[.,']", "", name).strip()
    tokens = name.split()
    if len(tokens) < 2:
        return name
    return f"{tokens[-1]} {' '.join(tokens[:-1])}"


def _shorten_search(formatted: str) -> Optional[str]:
    """'LAST FIRST MIDDLE' → 'LAST FIRST' (drop middle name/initial)."""
    parts = formatted.split()
    if len(parts) <= 2:
        return None
    return f"{parts[0]} {parts[1]}"


def _maiden_variant(decedent_name: str) -> Optional[str]:
    """For 'FIRST MIDDLE MAIDEN MARRIED' patterns, return 'MAIDEN FIRST'."""
    clean = re.sub(r"\b(?:JR|SR|II|III|IV)\b\.?", "", decedent_name, flags=re.IGNORECASE).strip()
    clean = re.sub(r"[.,'']", "", clean).strip()
    parts = clean.split()
    if len(parts) < 4:
        return None
    return f"{parts[-2]} {parts[0]}".upper()


# ── Tier 6: People search ─────────────────────────────────────────────


def _people_search_address(name: str, city: str = "Tulsa") -> Optional[tuple[str, str, str]]:
    """Tier 6: Look up decedent's last known address via CyberBackgroundChecks + Firecrawl.

    Returns (street, city, zip) or None. Requires FIRECRAWL_API_KEY.
    """
    try:
        from obituary_enricher import _fetch_firecrawl, _search_serper
    except ImportError:
        return None

    parts = name.strip().split()
    if len(parts) < 2:
        return None

    first = parts[0].lower()
    last = parts[-1].lower()
    city_slug = city.lower().replace(" ", "-")

    # Try direct CyberBackgroundChecks URL first (no Serper credit needed)
    direct_url = (
        f"https://www.cyberbackgroundchecks.com/people/{first}-{last}/{city_slug}-ok"
    )
    md = _fetch_firecrawl(direct_url, wait_ms=5000, priority="low")

    # Fallback: use Serper to find the right profile URL
    if not md or "lives at" not in md.lower():
        serper_urls = _search_serper_ok(name, city)
        for url in serper_urls[:2]:
            md = _fetch_firecrawl(url, wait_ms=5000, priority="low")
            if md and "lives at" in md.lower():
                break

    if not md:
        return None

    # Extract "Lives at" address
    for line in md.split("\n"):
        if "lives at" in line.lower():
            continue
        m = _PEOPLE_ADDR_RE.search(line)
        if m:
            street_raw = m.group(1).strip()
            city_found = m.group(2).strip().title()
            zip_found  = m.group(4) or ""
            # Parse street further with our suffix-based parser
            street, _, _ = _parse_assessor_address(street_raw + " " + city_found.upper() + " " + zip_found)
            return street or street_raw, city_found, zip_found

    return None


def _search_serper_ok(name: str, city: str) -> list[str]:
    """Search Google via Serper for Oklahoma people search results."""
    try:
        import requests as _req
        import config as cfg
        if not cfg.SERPER_API_KEY:
            return []
        parts = name.strip().split()
        if len(parts) < 2:
            return []
        first, last = parts[0], parts[-1]
        query = f'"{first} {last}" {city} OK site:cyberbackgroundchecks.com'
        resp = _req.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": cfg.SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": 5},
            timeout=10,
        )
        resp.raise_for_status()
        return [r["link"] for r in resp.json().get("organic", []) if "cyberbackgroundchecks" in r.get("link","")][:3]
    except Exception as e:
        logger.debug("Serper OK search failed for '%s': %s", name, e)
        return []


# ── Tier cascade ──────────────────────────────────────────────────────


async def _search_with_tiers(
    page: Page,
    notice: NoticeData,
) -> Optional[dict]:
    """Run all tiers and return the first successful property match."""
    # Choose the primary search name
    decedent = notice.decedent_name if notice.notice_type == "probate" else ""
    owner = notice.owner_name or ""
    primary = decedent or owner
    if not primary:
        return None

    formatted = _format_for_search(primary)
    logger.debug("  Tiers starting with: '%s'", formatted)
    dm_name = notice.decision_maker_name or ""
    dm_rel  = notice.decision_maker_relationship or ""

    # ── Tier 0: Parcel ID (Acclaim records have account_no from the grid) ─
    if notice.parcel_id and notice.parcel_id.strip():
        raw_parcel = notice.parcel_id.strip()
        # Acclaim parcel column is Sub+Acct concatenated (e.g., "71125830732030")
        # Assessor accepts either the full string or just the 9-digit account portion
        candidates = [raw_parcel]
        if len(raw_parcel) > 9:
            candidates.append(raw_parcel[-9:])
        for search_term in candidates:
            results = await _search_assessor(page, search_term)
            if results:
                r = results[0]
                logger.info("  Tier 0 (parcel_id='%s'): %s, %s %s",
                            search_term, r.get("street"), r.get("city"), r.get("zip"))
                r["_tier"] = 0
                r["_source"] = "parcel_id"
                return r

    # ── Tier 1: Full name ──────────────────────────────────────────────
    results = await _search_assessor(page, formatted)
    match = _best_match(results, primary, dm_name=dm_name, dm_relationship=dm_rel)
    if match:
        if _non_spousal_rejected(match, dm_rel):
            logger.info(
                "  Tier 1: non-spousal DM '%s' (%s) but '%s' has unmatched co-owner"
                " -- can't confirm property, trying next tier",
                dm_name, dm_rel, match["owner_name"],
            )
        else:
            match["_tier"] = 1
            return match

    # ── Tier 2: Short name (drop middle) ──────────────────────────────
    short = _shorten_search(formatted)
    if short:
        logger.debug("  Tier 2: '%s'", short)
        results = await _search_assessor(page, short)
        match = _best_match(results, primary, dm_name=dm_name, dm_relationship=dm_rel)
        if match:
            if _non_spousal_rejected(match, dm_rel):
                logger.debug(
                    "  Tier 2: non-spousal DM, unmatched co-owner on '%s' -- skipping",
                    match["owner_name"],
                )
            else:
                match["_tier"] = 2
                return match

    # ── Tier 3: Maiden name variant ────────────────────────────────────
    maiden = _maiden_variant(primary)
    if maiden:
        logger.debug("  Tier 3 maiden: '%s'", maiden)
        results = await _search_assessor(page, maiden)
        match = _best_match(results, primary, min_score=0.3, dm_name=dm_name, dm_relationship=dm_rel)
        if match:
            if _non_spousal_rejected(match, dm_rel):
                logger.debug(
                    "  Tier 3: non-spousal DM, unmatched co-owner on '%s' -- skipping",
                    match["owner_name"],
                )
            else:
                match["_tier"] = 3
                return match

    # ── Tier 4: Last name only (unique names only) ─────────────────────
    last_only = formatted.split()[0] if formatted else ""
    if len(last_only) > 4:
        logger.debug("  Tier 4 last-name only: '%s'", last_only)
        results = await _search_assessor(page, last_only, max_pages=1)
        if len(results) <= 15:  # Only score when result set is small
            match = _best_match(results, primary, min_score=0.6, dm_name=dm_name, dm_relationship=dm_rel)
            if match:
                if _non_spousal_rejected(match, dm_rel):
                    logger.debug(
                        "  Tier 4: non-spousal DM, unmatched co-owner on '%s' -- skipping",
                        match["owner_name"],
                    )
                else:
                    match["_tier"] = 4
                    return match

    # ── Tier 5: PR/executor family search ─────────────────────────────
    # For probate: search by executor/PR name, filter results where
    # the decedent's last name appears in the owner field (inherited property)
    if notice.notice_type == "probate" and owner and decedent:
        pr_formatted = _format_for_search(owner)
        decedent_last = _format_for_search(decedent).split()[0]
        if pr_formatted and decedent_last and pr_formatted.split()[0] != decedent_last:
            logger.debug("  Tier 5 executor family: '%s' (filter by '%s')", pr_formatted, decedent_last)
            results = await _search_assessor(page, pr_formatted)
            family = [
                r for r in results
                if decedent_last.lower() in r.get("owner_name", "").lower()
            ]
            if family:
                match = _best_match(family, decedent, min_score=0.3, dm_name=dm_name, dm_relationship=dm_rel)
                if match:
                    if _non_spousal_rejected(match, dm_rel):
                        logger.debug(
                            "  Tier 5: non-spousal DM, unmatched co-owner on '%s' -- skipping",
                            match["owner_name"],
                        )
                    else:
                        match["_tier"] = 5
                        return match

    # ── Tier 6: People search via CyberBackgroundChecks + Firecrawl ───
    # Requires FIRECRAWL_API_KEY (falls through gracefully without it).
    # After getting an address, reverse-verify via Assessor to confirm the
    # decedent actually OWNS that property in Tulsa County (not a rental,
    # not an out-of-county address). No Assessor confirmation = no address.
    logger.debug("  Tier 6 people search: '%s'", primary)
    ps_result = _people_search_address(primary, city="Tulsa")
    if ps_result:
        street, city, zip_code = ps_result
        logger.debug("  Tier 6: reverse-verifying '%s' against Assessor...", street)
        rv_results = await _search_assessor(page, street)
        # _extract_surname assumes "LAST, FIRST" or "LAST FIRST" (Assessor format).
        # primary is "FIRST LAST" — use _format_for_search to convert first, then
        # take the leading token which is the surname.
        decedent_surname = _format_for_search(primary).split()[0].upper()
        for r in rv_results:
            owner_surname = _extract_surname(r.get("owner_name", "")).upper()
            if owner_surname and decedent_surname and owner_surname == decedent_surname:
                r_dm_verified = _dm_corroborates(r["owner_name"], dm_name)
                pseudo = {"_dm_verified": r_dm_verified}
                if _non_spousal_rejected(pseudo, dm_rel):
                    logger.debug(
                        "  Tier 6: non-spousal DM '%s' (%s), unmatched co-owner on '%s' -- skipping",
                        dm_name, dm_rel, r["owner_name"],
                    )
                    continue
                logger.debug(
                    "  Tier 6 confirmed: Assessor owner '%s' surname matches '%s' (parcel %s)",
                    r["owner_name"], primary, r.get("account_no", ""),
                )
                return {
                    "street": r.get("street") or street,
                    "city": r.get("city") or city,
                    "zip": r.get("zip") or zip_code,
                    "owner_name": r["owner_name"],
                    "account_no": r.get("account_no", ""),
                    "acct_type": r.get("acct_type", "Residential"),
                    "_tier": 6,
                    "_source": "people_search_verified",
                }
        logger.debug(
            "  Tier 6 REJECTED: people search gave '%s' but Assessor shows no '%s' owner "
            "(likely rental or out-of-county)",
            street, decedent_surname,
        )

    return None


# ── Public entry point ────────────────────────────────────────────────


async def lookup_addresses_tulsa(
    notices: list[NoticeData],
    headless: bool = True,
    delay_sec: float = 0.5,
) -> tuple[int, int]:
    """Back-fill property addresses for Tulsa OSCN records via 6-tier lookup.

    Updates NoticeData objects in-place. Returns (found_count, not_found_count).
    """
    targets = [
        n for n in notices
        if n.county == "Tulsa" and n.state == "OK"
        and (not n.address.strip() or getattr(n, "needs_assessor_lookup", False))
    ]

    if not targets:
        logger.info("Tulsa Assessor: no records need address lookup")
        return 0, 0

    logger.info(
        "Tulsa Assessor: looking up addresses for %d records "
        "(probate=%d, eviction=%d, foreclosure=%d)",
        len(targets),
        sum(1 for n in targets if n.notice_type == "probate"),
        sum(1 for n in targets if n.notice_type == "eviction"),
        sum(1 for n in targets if n.notice_type == "foreclosure"),
    )

    found = not_found = 0
    tier_counts: dict[int, int] = {}
    _cache: dict[str, Optional[dict]] = {}  # name → result (None = not found)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        context.set_default_timeout(20_000)
        page = await context.new_page()

        for i, notice in enumerate(targets, 1):
            # Multi-property Lis Pendens clones (see
            # acclaimed_scraper._extract_all_properties) are never name-
            # searched — ownership of a co-listed property may belong to a
            # different named defendant than the primary owner, so searching
            # by the primary owner's name would just re-find their own
            # property and overwrite this clone's correct address with the
            # wrong one. Checked via the flag rather than "has a parcel_id",
            # so a clone whose parcel extraction happened to fail still gets
            # routed here instead of falling through to the risky name search.
            is_unverified_clone = "co_listed_property_owner_unverified" in (notice.missing_data_flags or "")
            if is_unverified_clone:
                if notice.parcel_id.strip():
                    details = await _fetch_property_details(page, notice.parcel_id.strip())
                    _apply_property_characteristics(notice, details)
                    if details:
                        logger.info(
                            "[%d/%d] Parcel-direct (%s): %s -- got property characteristics",
                            i, len(targets), notice.parcel_id, notice.address,
                        )
                        found += 1
                        continue
                logger.debug(
                    "[%d/%d] Co-listed property clone with no resolvable parcel — "
                    "leaving characteristics blank for manual buy-box check: %s",
                    i, len(targets), notice.address,
                )
                not_found += 1
                continue

            primary = (
                notice.decedent_name if notice.notice_type == "probate" and notice.decedent_name
                else notice.owner_name
            )
            if not primary:
                logger.debug("[%d/%d] No name — skipping", i, len(targets))
                not_found += 1
                continue

            cache_key = primary.strip().upper()
            if cache_key in _cache:
                match = _cache[cache_key]
                logger.debug("[%d/%d] Cache hit for '%s'", i, len(targets), primary)
            else:
                logger.info(
                    "[%d/%d] %s: '%s'",
                    i, len(targets), notice.notice_type,
                    primary,
                )
                match = await _search_with_tiers(page, notice)
                _cache[cache_key] = match

            if match:
                notice.address = match["street"]
                notice.city    = match["city"]
                notice.zip     = match["zip"]
                if match.get("account_no"):
                    notice.parcel_id = match["account_no"]
                notice.tax_owner_name = match.get("owner_name", "")

                # Fetch property characteristics from detail page (year built,
                # sqft, beds, baths) — only when not already populated from
                # a prior source (e.g. Zillow or a previous Assessor run).
                needs_details = not any([notice.year_built, notice.sqft,
                                         notice.bedrooms, notice.bathrooms])
                if needs_details and match.get("account_no"):
                    details = await _fetch_property_details(page, match["account_no"])
                    _apply_property_characteristics(notice, details)
                tier = match.get("_tier", 0)
                tier_counts[tier] = tier_counts.get(tier, 0) + 1
                src = match.get("_source", "assessor")
                dm_verified = match.get("_dm_verified")
                if dm_verified is False:
                    logger.warning(
                        "  SUSPECT Tier %d (%s): %s, %s %s -- property co-owner '%s' "
                        "does not match known DM '%s', verify before mailing",
                        tier, src, notice.address, notice.city, notice.zip,
                        notice.tax_owner_name, notice.decision_maker_name,
                    )
                elif match.get("_ambiguous"):
                    logger.warning(
                        "  AMBIGUOUS Tier %d (%s): %s, %s %s -- multiple tied properties, "
                        "no DM to confirm -- verify before mailing",
                        tier, src, notice.address, notice.city, notice.zip,
                    )
                else:
                    logger.info(
                        "  Tier %d (%s): %s, %s %s",
                        tier, src, notice.address, notice.city, notice.zip,
                    )
                found += 1
            else:
                dm_rel_str = (notice.decision_maker_relationship or "").lower()
                if dm_rel_str in _NON_SPOUSAL:
                    logger.info(
                        "  DROP '%s': non-spousal DM (%s) and all Assessor records have "
                        "unmatched co-owners -- property unconfirmed, record dropped",
                        primary, dm_rel_str,
                    )
                else:
                    logger.debug("  No Assessor match for '%s'", primary)
                not_found += 1

            if i < len(targets) and delay_sec > 0:
                await asyncio.sleep(delay_sec)

        await browser.close()

    if tier_counts:
        breakdown = ", ".join(f"T{t}={c}" for t, c in sorted(tier_counts.items()))
        logger.info(
            "Tulsa Assessor complete: %d found, %d not found  [%s]",
            found, not_found, breakdown,
        )
    else:
        logger.info(
            "Tulsa Assessor complete: %d found, %d not found",
            found, not_found,
        )

    return found, not_found


async def enrich_acclaimed_parcels(
    notices: list[NoticeData],
    headless: bool = True,
    delay_sec: float = 0.5,
) -> tuple[int, int]:
    """Fill missing parcel_ids for Acclaim records via Tulsa County Assessor address search.

    Acclaim records already have addresses from the county recording parcel data,
    but the Kendo grid may not expose the account_no. This searches the Assessor
    by street address to get the account_no and confirm the canonical address.

    Only processes records with an address but empty parcel_id. Returns (enriched, not_found).
    """
    targets = [
        n for n in notices
        if n.county == "Tulsa"
        and n.state == "OK"
        and n.address.strip()
        and not n.parcel_id.strip()
    ]

    if not targets:
        logger.info("Assessor parcel enrich: no Acclaim records need parcel_id lookup")
        return 0, 0

    logger.info("Assessor parcel enrich: looking up parcel_ids for %d Acclaim records", len(targets))
    enriched = not_found = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        context.set_default_timeout(20_000)
        page = await context.new_page()

        for i, notice in enumerate(targets, 1):
            search_term = notice.address.strip()
            logger.info("[%d/%d] Assessor address lookup: '%s'", i, len(targets), search_term)
            results = await _search_assessor(page, search_term)
            if results:
                r = results[0]
                notice.parcel_id = r.get("account_no", "")
                if not notice.tax_owner_name:
                    notice.tax_owner_name = r.get("owner_name", "")
                # Update to Assessor-canonical address format
                if r.get("street"):
                    notice.address = r["street"]
                    notice.city = r.get("city") or notice.city
                    notice.zip = r.get("zip") or notice.zip
                enriched += 1
                logger.info(
                    "  Found: account=%s owner='%s'",
                    notice.parcel_id, notice.tax_owner_name,
                )
            else:
                not_found += 1
                logger.debug("  Not in Assessor (keeping Acclaim address): '%s'", search_term)

            if i < len(targets) and delay_sec > 0:
                await asyncio.sleep(delay_sec)

        await browser.close()

    logger.info(
        "Assessor parcel enrich: %d enriched, %d not found",
        enriched, not_found,
    )
    return enriched, not_found
