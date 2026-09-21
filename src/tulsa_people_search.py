"""Tulsa probate - Tier 3 people-search fallback (TruePeopleSearch).

MANUAL, DELIBERATE USE ONLY - not part of the automatic per-row probate
chain (main._create_records_for_batch() / main._treasurer_true_negative_
check()). Run this by hand for a specific stuck case: the Assessor
(tulsa_assessor.py) and Treasurer (tulsa_treasurer.py) both came up empty,
and there's a real reason to believe the decedent left Tulsa-area real
property that just hasn't been found yet (the filing states real property,
or a heir/PR mentions a house). User, 2026-09-16: "It's definitely not
something that we run on every single record, because most of the time it
should be unnecessary."

Live-verified 2026-09-16, real requests, one real 2Captcha solve:
- A cold, single Playwright request clears TruePeopleSearch's Cloudflare
  edge Managed Challenge with NO captcha solving at all - most one-off
  lookups should cost nothing.
- Under repeated requests (unavoidable once you open several candidates'
  detail pages back to back), TruePeopleSearch falls back to its OWN
  app-level rate limiter: an HTTP 200 page (title "Captcha", NOT Cloudflare's
  raw edge 403) embedding a genuine Turnstile widget - sitekey visible in
  the page, posts to /internalcaptcha/captchasubmit with a
  cf-turnstile-response field. Same pattern already proven for OSCN
  (oscn_scraper.py / CLAUDE.md's "OSCN detail-page Turnstile" section):
  2Captcha solves it over the widget's sitekey (~$0.0015/solve), no browser
  fingerprint games needed for the SOLVE itself (Playwright still drives the
  page around it). The page submits via its own JS (fetch(), not a plain
  form POST) - _clear_internal_captcha() calls that same JS function
  (submitFormCaptcha()) directly with the solved token, rather than trying
  to fake a form submission.
- FastPeopleSearch, in the one sample taken the same day, fell back straight
  to Cloudflare's own raw edge "Just a moment..." interstitial instead - no
  visible solvable widget there. NOT supported by this module; out of scope
  unless someone proves a working path for it later.
- The results LISTING gives only city-level history + a TRUNCATED "Related
  to" list (real page: "...Plant C...", "...Paula Koo..." - cut off, not
  reliable for matching). The DETAIL page (/find/person/{id}) gives full
  current + previous STREET addresses with date ranges, phone numbers, and
  an UNTRUNCATED "Possible Relatives" list - all free, no extra challenge on
  the click-through from an already-cleared results page.

Different site AND different capability from tulsa_assessor.py's older
"Tier 6" people search (CyberBackgroundChecks via Firecrawl) - that one is
embedded in the old async Playwright obituary-pipeline cascade
(lookup_addresses_tulsa()) and historically went 0/10 in the one recorded
test batch (see feedback_tulsa_assessor.md memory - its own reverse-Assessor
verification correctly rejected every hit, it just never had a real one to
accept). This module is independent, targets a different site, and does not
touch that path. User's explicit call, 2026-09-16: build this fresh rather
than try to fix/reuse the old one.

NEVER TRUST A NAME-ONLY HIT. Same collision risk already confirmed live for
"Tina Johnson" (Assessor, 26-way tie) and "Elizabeth Coleman" (Treasurer) -
a single TruePeopleSearch query for "William Fulton" in Tulsa, OK returned
SIX different candidates. Every hit must be corroborated against an address
already known from the filing before being treated as a match -
address_corroborates() is reused directly from tulsa_treasurer.py (same
house-number + street-token rule, not a reimplementation) rather than
inventing a second copy of that logic. A "Possible Relatives" overlap with a
named PR/heir is useful supporting context for the human reviewing this -
it is never, by itself, treated as confirmation.
"""

import logging
import os
import re
import sys
import time
from typing import Optional

from bs4 import BeautifulSoup

from tulsa_treasurer import address_corroborates

logger = logging.getLogger(__name__)

BASE_URL = "https://www.truepeoplesearch.com"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: Between requests to the same site in one session - TruePeopleSearch's own
#: app-level rate limiter escalates on request velocity, confirmed live
#: 2026-09-16 (a fresh context + 20s delay still eventually hit the internal
#: captcha on a third request). This does not eliminate the captcha, it just
#: keeps the frequency down.
_REQUEST_DELAY_SEC = 5


def _search_url(first: str, last: str, city: str, state: str) -> str:
    name = f"{first} {last}".strip().replace(" ", "%20")
    citystatezip = f"{city}%2C%20{state}".strip()
    return f"{BASE_URL}/results?name={name}&citystatezip={citystatezip}"


class PeopleSearchError(RuntimeError):
    """The site could not be read (captcha not cleared, unrecognized page).
    Raised instead of returning [] so a FAILURE can never pass for a real
    "no such person" - on 2026-09-21 a mid-redirect page was parsed as zero
    results and Dallas Copley of Sapulpa, who exists, was reported missing."""


def _classify_search_page(body_text: str, n_cards: int) -> str:
    """'results' (cards present), 'zero' (page explicitly says none), or
    'unknown' (anything else - never treated as a confirmed zero)."""
    if n_cards > 0:
        return "results"
    b = (body_text or "").lower()
    if "no records found" in b or "0 records found" in b or "no results found" in b:
        return "zero"
    return "unknown"


def _clear_internal_captcha(page, api_key: str) -> bool:
    """If the page is TruePeopleSearch's own rate-limit captcha (title
    "Captcha", HTTP 200 - NOT Cloudflare's raw edge 403), solve its Turnstile
    widget via 2Captcha and submit through the page's own JS. Returns True if
    the page is clear (either it never was captchaed, or the solve worked).
    """
    if page.title() != "Captcha":
        return True

    from twocaptcha import TwoCaptcha

    if not api_key:
        logger.error("people-search: hit the internal captcha but no "
                      "CAPTCHA_API_KEY is set - cannot solve")
        return False

    sitekey_el = page.query_selector("#h-captcha")
    sitekey = sitekey_el.get_attribute("data-sitekey") if sitekey_el else None
    if not sitekey:
        logger.error("people-search: captcha page has no sitekey - "
                      "TruePeopleSearch's challenge markup may have changed")
        return False

    for attempt in (1, 2):
        logger.info("people-search: solving TruePeopleSearch internal Turnstile "
                    "(~$0.0015, attempt %d)...", attempt)
        solver = TwoCaptcha(api_key)
        result = solver.turnstile(sitekey=sitekey, url=page.url)
        token = result.get("code") if isinstance(result, dict) else str(result)
        if not token:
            logger.error("people-search: 2Captcha returned no token")
            return False
        if _submit_captcha_token(page, token):
            return True
        logger.warning("people-search: still on the captcha page after solving")
    return False


def _submit_captcha_token(page, token: str) -> bool:

    # The page submits via its own JS (fetch(), not a plain form POST) - call
    # that same function directly with the solved token, same idea as
    # captcha_solver.py invoking the grecaptcha client callback for
    # tnpublicnotice.com rather than faking a form submit.
    page.evaluate(
        """(token) => {
            captchaToken = token;
            document.querySelectorAll('input[name="cf-turnstile-response"]')
                .forEach(el => { el.value = token; });
            submitFormCaptcha();
        }""",
        token,
    )
    page.wait_for_timeout(6000)
    return page.title() != "Captcha"


def _parse_search_results(html: str) -> list[dict]:
    """Parse TruePeopleSearch's results-listing cards.

    used_to_live_in / related_to are TRUNCATED on this page ("...Plant C...")
    - advisory only, never used for matching. Full data lives on the detail
    page (get_person_detail()).
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[dict] = []
    for card in soup.select("div.card-summary[data-detail-link]"):
        classes = card.get("class") or []
        if "d-none" in classes:
            continue  # hidden duplicate card - seen in the real page
        href = card.get("data-detail-link")
        name_el = card.select_one(".content-header")
        name = name_el.get_text(strip=True) if name_el else ""
        if not href or not name:
            continue

        values = [v.get_text(strip=True) for v in card.select(".content-value")]
        age = values[0] if values else ""
        current_city = values[1] if len(values) > 1 else ""

        used_to_live_in: list[str] = []
        related_to: list[str] = []
        for label_el in card.select(".content-label"):
            label = label_el.get_text(strip=True)
            value_el = label_el.find_next("span", class_="content-value")
            value = value_el.get_text(strip=True) if value_el else ""
            parts = [p.strip() for p in value.split(",") if p.strip()]
            if "Used to live" in label:
                used_to_live_in = parts
            elif "Related to" in label:
                related_to = parts

        results.append({
            "name": name,
            "age": age,
            "current_city": current_city,
            "used_to_live_in": used_to_live_in,
            "related_to": related_to,
            "detail_href": href,
        })
    return results


_CITY_STATE_ZIP_RE = re.compile(r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})")


def _parse_address_entries(soup: BeautifulSoup) -> list[dict]:
    """Every address on the page - the single Current Address (if present)
    always comes first in document order, the rest are Previous Addresses.
    Both sections share the exact same markup (verified live 2026-09-16,
    tps_detail.html):

        <a data-link-to-more="address">STREET<br>CITY, ST ZIP</a>
        <div class="mt-1 dt-ln">
            <span class="dt-sb">COUNTY</span><br>
            <span class="dt-sb">(date range)</span>
        </div>

    <a> and the county/date <div> are siblings under a shared parent, not
    nested - the div is found via find_next_sibling(), not descended into.
    """
    entries = []
    for a in soup.select('a[data-link-to-more="address"]'):
        strings = list(a.stripped_strings)
        if len(strings) < 2:
            continue
        street, city_state_zip = strings[0], strings[1]
        m = _CITY_STATE_ZIP_RE.match(city_state_zip)
        if not m:
            continue

        county, date_range = "", ""
        sib = a.find_next_sibling("div", class_="mt-1")
        if sib:
            for span_text in (s.get_text(strip=True) for s in sib.select("span.dt-sb")):
                if span_text.startswith("(") and span_text.endswith(")"):
                    date_range = span_text.strip("()")
                elif span_text:
                    county = span_text

        entries.append({
            "street": street,
            "city": m.group("city"),
            "state": m.group("state"),
            "zip": m.group("zip"),
            "county": county,
            "date_range": date_range,
        })
    return entries


def _parse_aliases(soup: BeautifulSoup) -> list[str]:
    """'Also Seen As' names - a comma-joined <span> list in the data row
    right after the section's description text."""
    marker = soup.find(string=re.compile(r"Includes all names used in any public records"))
    if not marker:
        return []
    header_row = marker.find_parent("div", class_="row")
    if not header_row:
        return []
    data_row = header_row.find_next_sibling("div", class_="row")
    if not data_row:
        return []
    return [s.get_text(strip=True) for s in data_row.select("span") if s.get_text(strip=True)]


def _parse_phones(soup: BeautifulSoup) -> list[dict]:
    """The detail page's Phone Numbers section (verified live 2026-09-16/21):

        <a data-link-to-more="phone" href="/find/phone/NNNNNNNNNN"><span>(806) 392-1417</span></a>
        - <span class="smaller">Wireless</span>
        <div class="mt-1 dt-ln">
            <span class="dt-sb"><b>Possible Primary Phone</b></span><br>
            <span class="dt-sb">Last reported Jul 2026</span><br>
            <span class="dt-sb">T-Mobile</span>
        </div>

    Returns [{"number": "8063921417", "line_type": "Wireless", "is_primary":
    bool, "last_reported": "Jul 2026", "carrier": "T-Mobile"}]. Only anchors
    that link to /find/phone/ count, so relatives' numbers never leak in.
    """
    out, seen = [], set()
    for a in soup.select('a[data-link-to-more="phone"]'):
        href = a.get("href") or ""
        digits = re.sub(r"\D", "", a.get_text(strip=True))
        if not href.startswith("/find/phone/") or len(digits) != 10 or digits in seen:
            continue
        seen.add(digits)
        line_type = ""
        nxt = a.find_next_sibling("span", class_="smaller")
        if nxt:
            line_type = nxt.get_text(strip=True)
        info = a.find_next_sibling("div", class_="mt-1")
        texts = [t.get_text(strip=True) for t in info.select("span.dt-sb")] if info else []
        out.append({
            "number": digits,
            "line_type": line_type,
            "is_primary": any("primary" in t.lower() for t in texts),
            "last_reported": next((t.replace("Last reported", "").strip()
                                   for t in texts if t.startswith("Last reported")), ""),
            "carrier": next((t for t in texts if t and "primary" not in t.lower()
                             and not t.startswith("Last reported")), ""),
        })
    return out


def _parse_person_detail(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    addresses = _parse_address_entries(soup)
    current = addresses[0] if addresses else None
    previous = addresses[1:]

    relatives = [
        a.get_text(strip=True) for a in soup.select('a[data-link-to-more="relative"]')
        if a.get_text(strip=True)
    ]

    return {
        "current_address": current,
        "previous_addresses": previous,
        "relatives": relatives,
        "also_seen_as": _parse_aliases(soup)[:5],
        "phones": _parse_phones(soup),
    }


def search_person(
    first: str, last: str, city: str = "Tulsa", state: str = "OK",
    *, api_key: Optional[str] = None, headless: bool = True,
) -> list[dict]:
    """Search TruePeopleSearch by name + city/state. Returns candidate
    summary cards (see _parse_search_results). Does NOT open detail pages -
    call get_person_detail() per candidate you want to check."""
    import config
    from playwright.sync_api import sync_playwright

    api_key = api_key or config.CAPTCHA_API_KEY
    url = _search_url(first, last, city, state)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=_UA, viewport={"width": 1280, "height": 800})
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            if not _clear_internal_captcha(page, api_key):
                raise PeopleSearchError(
                    f"could not clear the captcha searching {first} {last}")
            state_seen, results = "unknown", []
            for _ in range(8):                 # up to ~16s for a slow/redirecting page
                results = _parse_search_results(page.content())
                state_seen = _classify_search_page(page.inner_text("body"), len(results))
                if state_seen != "unknown":
                    break
                page.wait_for_timeout(2000)
                if page.title() == "Captcha":
                    if not _clear_internal_captcha(page, api_key):
                        raise PeopleSearchError("captcha reappeared and could not be cleared")
            if state_seen == "unknown":
                raise PeopleSearchError(
                    f"unrecognized page for {first} {last} (title {page.title()!r}) - "
                    "NOT a confirmed zero")
            logger.info("people-search: %d candidate(s) for '%s %s' in %s, %s",
                        len(results), first, last, city, state)
            return results
        finally:
            browser.close()


def get_person_detail(
    detail_href: str, *, api_key: Optional[str] = None, headless: bool = True,
) -> Optional[dict]:
    """Open one candidate's detail page. Returns current_address,
    previous_addresses (each with street/city/state/zip/county/date_range),
    relatives (untruncated), also_seen_as. None on failure."""
    import config
    from playwright.sync_api import sync_playwright

    api_key = api_key or config.CAPTCHA_API_KEY
    url = detail_href if detail_href.startswith("http") else BASE_URL + detail_href

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=_UA, viewport={"width": 1280, "height": 800})
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            if not _clear_internal_captcha(page, api_key):
                logger.error("people-search: could not clear captcha for %s", url)
                return None
            return _parse_person_detail(page.content())
        finally:
            browser.close()


_NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V", "ESQ"}

#: The Assessor rate-limits after ~15-20 hits in a row.
_ASSESSOR_DELAY_SEC = 2
_MAX_ASSESSOR_ADDRESSES = 6


def _name_tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^A-Za-z]+", (s or "").upper())
            if len(t) > 1 and t not in _NAME_SUFFIXES}


def _surname(decedent_name: str) -> str:
    """'LAST, FIRST' -> LAST; 'First Middle Last Jr' -> LAST."""
    name = (decedent_name or "").strip()
    if "," in name:
        return re.sub(r"[^A-Za-z]", "", name.split(",", 1)[0]).upper()
    parts = [p.strip(".,") for p in name.split() if p.strip(".,")]
    while len(parts) > 1 and parts[-1].upper() in _NAME_SUFFIXES:
        parts.pop()
    return re.sub(r"[^A-Za-z]", "", parts[-1]).upper() if parts else ""


def owner_matches_estate(owner: str, decedent_name: str,
                         related_names: Optional[list[str]] = None) -> bool:
    """User's rule, 2026-09-21: a person, LLC or trust on title counts as the
    decedent's when its name carries the decedent's SURNAME (or full name).
    Anything else - a stranger, or an unrelated entity - is a miss.

    A named PR/heir's full name (first AND last) also counts, consistent with
    batch_review's "clean title holder" rule. Surname-only matching is the
    user's call and is loose on a common surname - the caller reports the
    owner string verbatim so a human can see what matched.
    """
    owner_tokens = _name_tokens(owner)
    if not owner_tokens:
        return False
    surname = _surname(decedent_name)
    if surname and surname in owner_tokens:
        return True
    for rel in related_names or []:
        toks = _name_tokens(rel)
        if len(toks) >= 2 and toks <= owner_tokens:
            return True
    return False


def verify_addresses_with_assessor(
    candidate_detail: dict, decedent_name: str,
    related_names: Optional[list[str]] = None, *, assessor_search=None,
) -> list[dict]:
    """Free Assessor cross-check of a candidate's Tulsa County addresses.

    People search only says where someone LIVED. The Assessor says who holds
    title there - which separates "owns it" from the rental / out-of-county
    pattern that took the old CyberBackgroundChecks tier to 0 for 10.

    One result per address: {"street", "city", "verdict", "records"} where
    verdict is "match" (owner carries the decedent's surname / a named
    PR-heir), "miss" (a parcel exists but title is held by a stranger or an
    unrelated entity), or "no_parcel" (nothing at that address). Only Tulsa
    County addresses are checked (the Assessor covers nothing else), current
    address first, capped to protect the Assessor's rate limit.
    """
    if assessor_search is None:
        from tulsa_assessor import search_assessor as assessor_search

    addrs = [candidate_detail["current_address"]] if candidate_detail.get("current_address") else []
    addrs += candidate_detail.get("previous_addresses") or []
    addrs = [a for a in addrs if "TULSA" in (a.get("county") or "").upper()]

    results = []
    for addr in addrs[:_MAX_ASSESSOR_ADDRESSES]:
        street = addr.get("street", "")
        try:
            recs = assessor_search(street)
        except Exception as e:                    # noqa: BLE001 - free lookup; report, never crash
            logger.warning("people-search: assessor lookup failed for %r: %s", street, e)
            recs = []
        time.sleep(_ASSESSOR_DELAY_SEC)

        at_address = [
            r for r in recs
            if address_corroborates({"owner_street": r.get("FullPropertyStreet", "")}, street)
        ]
        records = [{
            "account_no": r.get("AccountNo", ""),
            "situs": r.get("FullPropertyStreet", ""),
            "owner": (r.get("FullPrimaryOwnerName") or "").strip(),
            "matches_estate": owner_matches_estate(
                r.get("FullPrimaryOwnerName") or "", decedent_name, related_names),
        } for r in at_address]

        if not records:
            verdict = "no_parcel"
        elif any(r["matches_estate"] for r in records):
            verdict = "match"
        else:
            verdict = "miss"
        results.append({"street": street, "city": addr.get("city", ""),
                        "verdict": verdict, "records": records})
    return results


def _candidate_age(cand: dict) -> Optional[int]:
    m = re.search(r"\d+", str(cand.get("age") or ""))
    return int(m.group()) if m else None


def find_property_via_people_search(
    first: str, last: str, known_addresses: list[str],
    *, city: str = "Tulsa", state: str = "OK", max_candidates: int = 5,
    headless: bool = True, decedent_name: str = "",
    related_names: Optional[list[str]] = None, assessor_search=None,
    decedent_age: Optional[int] = None, age_tolerance: int = 5,
) -> dict:
    """The Tier 3 orchestration: search, open up to `max_candidates` detail
    pages, and report which (if any) has an address that corroborates one of
    `known_addresses` (PR/heir mailing address from the filing, or a property
    address already known some other way).

    For every CORROBORATED candidate it then runs the free Assessor check
    (verify_addresses_with_assessor) on that person's Tulsa County addresses.
    `decedent_name` defaults to "first last"; pass it explicitly when the
    person searched is a PR/heir rather than the decedent, since the
    surname rule is about the decedent.

    Returns {"candidates": [...], "corroborated_hit": dict or None,
    "verified_property": dict or None}. "verified_property" is the first
    corroborated candidate with an Assessor "match" address - a straight hit
    needing no extra stop-and-ask per the user's rule. ALWAYS returns every
    candidate checked - this is a manual tool for a human to review, and a
    hit is still never written to the CRM directly from here.

    `decedent_age` (from the filing) is a COST SAVER only: a candidate whose
    listed age is more than `age_tolerance` years off is skipped before its
    detail page is opened, saving requests and captcha solves. It never
    confirms anyone - only the known-address match does. A candidate with an
    unreadable age is kept, not skipped. Skipped candidates are returned
    under "skipped_by_age" so nothing disappears silently.
    """
    import config
    api_key = config.CAPTCHA_API_KEY
    known_addresses = [a for a in (known_addresses or []) if str(a or "").strip()]
    decedent_name = decedent_name or f"{first} {last}".strip()

    candidates = search_person(first, last, city, state, api_key=api_key, headless=headless)
    if not candidates:
        return {"candidates": [], "corroborated_hit": None, "verified_property": None,
                "skipped_by_age": []}

    skipped_by_age: list[dict] = []
    if decedent_age is not None:
        kept = []
        for cand in candidates:
            age = _candidate_age(cand)
            if age is not None and abs(age - decedent_age) > age_tolerance:
                skipped_by_age.append(cand)
            else:
                kept.append(cand)
        candidates = kept

    checked: list[dict] = []
    corroborated_hit = None
    verified_property = None
    for cand in candidates[:max_candidates]:
        time.sleep(_REQUEST_DELAY_SEC)
        detail = get_person_detail(cand["detail_href"], api_key=api_key, headless=headless)
        if detail is None:
            cand["detail"] = None
            cand["corroborated"] = False
            checked.append(cand)
            continue

        all_addrs = [detail["current_address"]] if detail["current_address"] else []
        all_addrs += detail["previous_addresses"]
        matched_against = None
        for addr in all_addrs:
            street = addr.get("street", "")
            for known in known_addresses:
                if address_corroborates({"owner_street": street}, known):
                    matched_against = (street, known)
                    break
            if matched_against:
                break

        cand["detail"] = detail
        cand["corroborated"] = bool(matched_against)
        cand["matched_against"] = matched_against
        if matched_against:
            cand["assessor_check"] = verify_addresses_with_assessor(
                detail, decedent_name, related_names, assessor_search=assessor_search)
            if verified_property is None:
                good = next((r for r in cand["assessor_check"] if r["verdict"] == "match"), None)
                if good:
                    verified_property = {"candidate": cand, "address": good}
        checked.append(cand)
        if matched_against and corroborated_hit is None:
            corroborated_hit = cand

    return {"candidates": checked, "corroborated_hit": corroborated_hit,
            "verified_property": verified_property, "skipped_by_age": skipped_by_age}


if __name__ == "__main__":
    import argparse
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    ap = argparse.ArgumentParser(
        description="Tulsa probate Tier 3 fallback: search TruePeopleSearch for a "
                    "decedent/heir/PR and check for a corroborated Tulsa-area address. "
                    "Manual use only - see the module docstring.")
    ap.add_argument("--first", required=True)
    ap.add_argument("--last", required=True)
    ap.add_argument("--city", default="Tulsa")
    ap.add_argument("--state", default="OK")
    ap.add_argument("--known-address", action="append", default=[],
                    help="A known address to corroborate against (repeatable). "
                        "E.g. the PR's mailing address from the filing.")
    ap.add_argument("--max-candidates", type=int, default=5)
    ap.add_argument("--decedent-name", default="",
                    help="Full decedent name for the Assessor surname rule. "
                        "Defaults to --first --last; set it when searching a PR/heir.")
    ap.add_argument("--age", type=int, default=None,
                    help="Decedent's age from the filing. Skips candidates clearly off by "
                        "age BEFORE opening their pages (saves captcha solves). "
                        "A cost saver only - never confirms anyone.")
    ap.add_argument("--age-tolerance", type=int, default=5)
    ap.add_argument("--related-name", action="append", default=[],
                    help="A named PR/heir (repeatable) - a full-name match on title also counts.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(message)s")

    report = find_property_via_people_search(
        args.first, args.last, args.known_address,
        city=args.city, state=args.state, max_candidates=args.max_candidates,
        decedent_name=args.decedent_name, related_names=args.related_name,
        decedent_age=args.age, age_tolerance=args.age_tolerance,
    )
    if report["skipped_by_age"]:
        print(f"Skipped {len(report['skipped_by_age'])} candidate(s) by age "
              f"(no pages opened): " + ", ".join(
                  f"{c['name']} ({c['age']})" for c in report["skipped_by_age"]))

    for c in report["candidates"]:
        print(f"\n{'='*60}\n{c['name']} (age {c['age']}, currently {c['current_city']})")
        print(f"  used to live in (truncated, advisory only): {c['used_to_live_in']}")
        d = c.get("detail")
        if not d:
            print("  -- could not open detail page --")
            continue
        if d["current_address"]:
            ca = d["current_address"]
            print(f"  CURRENT: {ca['street']}, {ca['city']} {ca['state']} {ca['zip']} "
                  f"({ca['county']}) {ca['date_range']}")
        for pa in d["previous_addresses"]:
            print(f"  previous: {pa['street']}, {pa['city']} {pa['state']} {pa['zip']} "
                  f"({pa['county']}) {pa['date_range']}")
        print(f"  possible relatives: {', '.join(d['relatives']) or '(none listed)'}")
        if c["corroborated"]:
            print(f"  *** CORROBORATED against known address: {c['matched_against'][1]!r} "
                  f"(matched {c['matched_against'][0]!r}) ***")
            for chk in c.get("assessor_check", []):
                print(f"  ASSESSOR {chk['verdict'].upper()}: {chk['street']}, {chk['city']}")
                for r in chk["records"]:
                    print(f"      {r['account_no']}  owner of record: {r['owner']!r}"
                          f"{'  <- carries the estate surname/name' if r['matches_estate'] else ''}")

    hit = report["corroborated_hit"]
    vp = report["verified_property"]
    print(f"\n{'='*60}")
    if vp:
        a = vp["address"]
        owners = "; ".join(r["owner"] for r in a["records"] if r["matches_estate"])
        print(f"VERIFIED PROPERTY: {a['street']}, {a['city']} - Assessor owner of record "
              f"{owners!r} carries the decedent's name. Straight hit, no extra stop needed; "
              f"still not written to the CRM from here.")
    elif hit:
        print("Corroborated candidate found, but NO Tulsa County address checks out with the "
              "Assessor (stranger/unrelated owner, or no parcel) - treat as a miss.")
    else:
        print("No corroborated hit among the candidates checked. "
              "Not proof of a true negative on its own - review the candidates above.")
