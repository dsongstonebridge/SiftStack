"""Tulsa County Acclaim Web document recording scraper.

Acclaim Web (Harris Recording Solutions / Tyler Technologies) is the Tulsa
County Clerk's document recording system. Every recorded instrument is tied
to a parcel record (address, parcel ID, legal description), eliminating the
~47% address-failure rate seen with OSCN name-only searches.

URLs:
  Base:   https://acclaim.tulsacounty.org/AcclaimWeb
  Login:  https://acclaim.tulsacounty.org/AcclaimWeb/Account/Login
  Search: https://acclaim.tulsacounty.org/AcclaimWeb/Search/SearchTypeDocType

Document types targeted:
  LIS PENDENS       -- foreclosure suit filed; borrower still in possession
  SHERIFF DEED      -- property transferred post-auction to winning bidder
  NOTICE SHERIFF SALE -- notice of upcoming sheriff auction date

Credentials are read from .env:
  acclaim_EMAIL    -- login email
  acclaim_PASSWORD -- login password
"""

import dataclasses
import io
import logging
import re
import sys
import tempfile
from datetime import datetime
from typing import Optional

from playwright.async_api import BrowserContext, Page, TimeoutError as PwTimeout, async_playwright

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

_BASE_URL = "https://acclaim.tulsacounty.org/AcclaimWeb"
_LOGIN_URL = f"{_BASE_URL}/Account/Login"
_DOCTYPE_SEARCH_URL = f"{_BASE_URL}/Search/SearchTypeDocType"

# Document type labels as they appear in Tulsa County's Acclaim dropdown.
# Tulsa County does not have a distinct "LIS PENDENS" type — foreclosure
# filings land under NOTICE (NOT) with the lender as grantor, and foreclosure
# decrees land under DECREE (DEC).
_DEFAULT_DOC_TYPES = [
    "NOTICE",
    "DECREE",
]

# Tuple maps → notice_type for each doc type label (case-insensitive prefix match)
_DOC_TYPE_TO_NOTICE_TYPE = [
    ("notice",               "foreclosure"),
    ("decree",               "foreclosure"),
    ("lis pendens",          "foreclosure"),
    ("sheriff deed",         "foreclosure"),
    ("notice sheriff sale",  "foreclosure"),
    ("sheriff sale",         "foreclosure"),
    ("deed in lieu",         "foreclosure"),
    ("trustee deed",         "foreclosure"),
    ("warranty deed",        "foreclosure"),
]

# Regex to identify mortgage servicers and lenders in the Acclaim grantor field.
# For NOTICE records, grantor = the party who filed the notice (lender for Lis Pendens).
# Records where grantor does NOT match are non-foreclosure notices and are dropped.
# Matches any business/legal entity that could be a foreclosure lender.
# Intentionally broad: private lenders (LLC, Corp, Capital, Investment) are
# just as valid as traditional banks for Lis Pendens NOTICE filings.
# Pure individual-name grantors (no corporate indicator) are the only exclusion.
_LENDER_RE = re.compile(
    r"\b(?:"
    # Corporate entity suffixes / forms
    r"LLC|L\.L\.C|INC(?:ORPORATED)?|CORP(?:ORATION)?|LTD|LP|L\.P|"
    r"PLC|NA|N\.A\.|FSB|SSB|"
    # Financial / lending keywords
    r"BANK|BANCORP|BANKERS?|BANKING|MORTGAGE|MORTGAGEE|FINANCIAL|FINANCE|"
    r"LENDING|LENDER|LOANS?|CREDIT|CREDIT UNION|SAVINGS|TRUST|"
    # Investment / holding company indicators
    r"CAPITAL|INVESTMENT|INVESTMENTS|HOLDINGS|VENTURES|PARTNERS|"
    r"PROPERTIES|REALTY|MANAGEMENT|GROUP|ASSOCIATES?|ENTERPRISES?|SERVICES?|"
    # Federal agencies / GSEs
    r"FANNIE MAE|FREDDIE MAC|GINNIE MAE|FHA|HUD|VA LOAN|VETERANS AFFAIRS|"
    r"FEDERAL HOME|FEDERAL SAVINGS|FEDERAL RESERVE|"
    # Named servicers kept for clarity
    r"PENNYMAC|NATIONSTAR|LOANCARE|FREEDOM|MR\.?\s*COOPER|QUICKEN|ROCKET|"
    r"CALIBER|CARRINGTON|NEWREZ|SHELLPOINT|RUSHMORE|ROUNDPOINT|"
    r"SELECT PORTFOLIO|OCWEN|WELLS FARGO|BANK OF AMERICA|US BANK|"
    r"REGIONS|ARVEST|BANCFIRST|CENTRAL BANK|BOKF|BOK FINANCIAL"
    r")\b",
    re.IGNORECASE,
)

_DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
# Tulsa County parcel format: XX-XX-XX-XXXXX  or  XXXXXXXXXX
_PARCEL_RE = re.compile(
    r"\b(\d{2}-\d{2}-\d{2}-\d{3,5}|[0-9]{8,15})\b"
)
_CITY_STATE_ZIP_RE = re.compile(
    r",?\s*(Tulsa|Broken Arrow|Owasso|Sand Springs|Jenks|Bixby|Sapulpa|Glenpool"
    r"|Collinsville|Skiatook|Sperry|Catoosa|Claremore|Pryor"
    r"|Oklahoma City|Muskogee)\s*,?\s*(?:OK|Oklahoma)\s*,?\s*(\d{5}(?:-\d{4})?)?",
    re.IGNORECASE,
)
_STREET_ADDR_RE = re.compile(
    r"(\d{1,5}\s+(?:[NSEW]\.?\s+)?[\w'-]+(?:\s+[\w'-]+){0,4}"
    r"\s+(?:ST|AVE|BLVD|DR|RD|LN|CT|PL|CIR|HWY|PKWY|WAY|TER|LOOP|TRL|CV|PASS|XING)\b\.?)",
    re.IGNORECASE,
)


# ── Public entry point ─────────────────────────────────────────────────

async def scrape_acclaimed(
    since_date: str,
    email: str,
    password: str,
    county: str = "Tulsa",
    headless: bool = True,
    doc_types: Optional[list[str]] = None,
    max_records: int = 500,
    verify_pdf: bool = True,
    seen_ids: Optional[dict] = None,
    until_date: Optional[str] = None,
) -> list[NoticeData]:
    """Search Tulsa County Acclaim Web for foreclosure recordings.

    Args:
        since_date:  ISO date (YYYY-MM-DD); only records on/after this date.
        email:       acclaim_EMAIL from .env
        password:    acclaim_PASSWORD from .env
        county:      County name for NoticeData (default "Tulsa")
        headless:    Run Chromium headless (default True)
        doc_types:   Acclaim document-type labels to search (default: lis pendens + sheriff deed)
        max_records: Hard cap on total records returned
        until_date:  ISO date (YYYY-MM-DD); only records on/before this date.
            Defaults to today when omitted. Useful for scoping a targeted
            rescrape to a narrow historical window instead of since_date..today.
    """
    if doc_types is None:
        doc_types = list(_DEFAULT_DOC_TYPES)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()
        page.set_default_timeout(30_000)

        all_notices: list[NoticeData] = []
        try:
            logged_in = await _login(page, email, password)
            if not logged_in:
                logger.error(
                    "Acclaim: login failed -- check acclaim_EMAIL / acclaim_PASSWORD in .env"
                )
                return []

            for doc_type in doc_types:
                if len(all_notices) >= max_records:
                    break
                try:
                    notices = await _search_doc_type(
                        page, doc_type, since_date, county,
                        email=email, password=password,
                        verify_pdf=verify_pdf,
                        seen_ids=seen_ids,
                        until_date=until_date,
                    )
                    # Retry once if grid returned 0 — Kendo AJAX can misfire
                    if not notices:
                        logger.info("Acclaim %s: 0 records on first attempt -- retrying", doc_type)
                        await page.wait_for_timeout(1_000)
                        notices = await _search_doc_type(
                            page, doc_type, since_date, county,
                            email=email, password=password,
                            verify_pdf=verify_pdf,
                            seen_ids=seen_ids,
                            until_date=until_date,
                        )
                    all_notices.extend(notices)
                    logger.info("Acclaim %s: %d records", doc_type, len(notices))
                except Exception:
                    logger.exception("Acclaim search failed for doc_type=%s", doc_type)

        except Exception:
            logger.exception("Acclaim scrape error")
        finally:
            await browser.close()

    result = all_notices[:max_records]
    logger.info(
        "Acclaim: %d total records across %d doc type(s) since %s",
        len(result), len(doc_types), since_date,
    )
    return result


# ── Login ──────────────────────────────────────────────────────────────

async def _login(page: Page, email: str, password: str) -> bool:
    """Navigate to Acclaim login and authenticate. Returns True on success."""
    try:
        await page.goto(_LOGIN_URL)
        await page.wait_for_load_state("domcontentloaded")

        # Acclaim uses "UserName" (email address) + "Password" in its MVC form.
        # Selectors cover both exact names and common variants across counties.
        user_sel = (
            "input[name='UserName'], input[name='Username'], "
            "input[name='Email'], input[type='email'], "
            "input[id*='user' i], input[id*='email' i]"
        )
        pass_sel = "input[type='password'], input[name='Password'], input[id*='pass' i]"

        try:
            await page.wait_for_selector(user_sel, timeout=10_000)
        except PwTimeout:
            logger.error("Acclaim: login form not found at %s", _LOGIN_URL)
            return False

        await page.fill(user_sel, email)
        await page.fill(pass_sel, password)

        submit_sel = (
            "input[type='submit'], button[type='submit'], "
            "button:has-text('Login'), button:has-text('Log In'), "
            "button:has-text('Sign In')"
        )
        await page.click(submit_sel)

        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except PwTimeout:
            await page.wait_for_load_state("domcontentloaded")

        current_url = page.url.lower()
        if "login" in current_url or "account/login" in current_url:
            # May have failed -- check for error text
            body = await page.inner_text("body")
            body_lower = body.lower()
            if any(w in body_lower for w in ("invalid", "incorrect", "failed", "wrong", "error")):
                logger.error(
                    "Acclaim: login rejected -- bad credentials. "
                    "Check acclaim_EMAIL / acclaim_PASSWORD in .env"
                )
            else:
                logger.warning(
                    "Acclaim: still on login page after submit (may be redirect loop). "
                    "Proceeding anyway."
                )
            return False

        logger.info("Acclaim: logged in as %s", email)
        return True

    except Exception as e:
        logger.error("Acclaim login error: %s", e)
        return False


# ── Document-type search ───────────────────────────────────────────────

async def _search_doc_type(
    page: Page,
    doc_type: str,
    since_date: str,
    county: str,
    email: Optional[str] = None,
    password: Optional[str] = None,
    verify_pdf: bool = False,
    seen_ids: Optional[dict] = None,
    until_date: Optional[str] = None,
) -> list[NoticeData]:
    """Run a single document-type search and return all paginated results.

    When verify_pdf=True (NOTICE type only): clicks each result row to capture
    the Acclaim itemId, then visits DocDetails to OCR the scanned document,
    confirming foreclosure keywords and extracting 'commonly known as' address.
    """
    await page.goto(_DOCTYPE_SEARCH_URL)
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except PwTimeout:
        await page.wait_for_load_state("domcontentloaded")

    # Session recovery: if navigating to the search page redirected to login,
    # re-authenticate and retry once.
    if "login" in page.url.lower() or "account" in page.url.lower():
        logger.warning("Acclaim: session expired before %s search -- re-logging in", doc_type)
        if email and password:
            if not await _login(page, email, password):
                logger.error("Acclaim: re-login failed -- skipping %s", doc_type)
                return []
            await page.goto(_DOCTYPE_SEARCH_URL)
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except PwTimeout:
                await page.wait_for_load_state("domcontentloaded")

            # Verify the re-login actually stuck before falling through to the
            # Kendo-widget wait below. Previously this wasn't re-checked, so a
            # session that expired again immediately after re-login (observed
            # 2026-07-10 and 2026-07-11 -- login succeeds, bounces back to
            # login within ~2s) silently fell through into the Kendo wait on
            # what was actually still the login page, timing out after 10s
            # with the misleading "Kendo widgets not ready" / "no doc type
            # input found" messages instead of a clear session-churn signal.
            if "login" in page.url.lower() or "account" in page.url.lower():
                logger.error(
                    "Acclaim: session expired again immediately after re-login -- "
                    "skipping %s (likely site-side session churn, not a Kendo/page "
                    "load issue -- see feedback_acclaim_repeated_login_failures)",
                    doc_type,
                )
                return []
        else:
            logger.error("Acclaim: session expired and no credentials to re-login -- skipping %s", doc_type)
            return []

    # Wait for Kendo widgets to initialize before interacting
    try:
        await page.wait_for_function(
            """() => {
                var el = document.getElementById('DocTypesList')
                       || document.querySelector('[data-role="multiselect"]');
                if (!el) return false;
                if (typeof $ === 'undefined') return false;
                var w = $(el).data('kendoMultiSelect') || $(el).data('kendoDropDownList');
                return !!w;
            }""",
            timeout=10_000,
        )
    except PwTimeout:
        logger.warning("Acclaim: Kendo widgets not ready after 10s for %s", doc_type)

    # ── Fill doc type field ───────────────────────────────────────────
    # AcclaimWeb shows either a typeahead text input or a <select> dropdown.
    doc_type_sel = (
        "input[name*='doctype' i], input[id*='doctype' i], "
        "input[name*='document' i], input[id*='document' i], "
        "input[placeholder*='type' i], input[placeholder*='document' i]"
    )
    doc_select_sel = (
        "select[name*='doctype' i], select[id*='doctype' i], "
        "select[name*='document' i], select[id*='document' i]"
    )

    filled = False
    # Try typeahead input first
    try:
        input_el = page.locator(doc_type_sel).first
        if await input_el.count():
            await input_el.fill(doc_type)
            # Wait for autocomplete dropdown to appear and pick first match
            try:
                await page.wait_for_selector(
                    "ul[role='listbox'] li, .ui-autocomplete li, .autocomplete-suggestion, "
                    "[class*='dropdown'] li",
                    timeout=3_000,
                )
                await page.locator(
                    "ul[role='listbox'] li:first-child, "
                    ".ui-autocomplete li:first-child, "
                    ".autocomplete-suggestion:first-child"
                ).first.click()
            except PwTimeout:
                pass  # No autocomplete; keep typed value
            filled = True
    except Exception:
        pass

    # Try <select> / Kendo MultiSelect if no text input worked
    if not filled:
        try:
            sel_el = page.locator(doc_select_sel).first
            if await sel_el.count():
                # Read all options (underlying <select> is always populated even when hidden)
                options = await sel_el.evaluate(
                    "el => Array.from(el.options).map(o => ({v: o.value, t: o.text.trim()}))"
                )
                match_val = None
                doc_lower = doc_type.lower()
                doc_words = set(doc_lower.split())

                for opt in options:
                    t = opt["t"].lower()
                    v = opt["v"].lower()
                    if doc_lower in t or doc_lower in v:
                        match_val = opt["v"]
                        break

                if not match_val:
                    for opt in options:
                        t = opt["t"].lower()
                        if all(w in t for w in doc_words):
                            match_val = opt["v"]
                            logger.info(
                                "Acclaim: matched '%s' via word-set to '%s'",
                                doc_type, opt["t"],
                            )
                            break

                if not match_val:
                    logger.warning(
                        "Acclaim: doc type '%s' not found in dropdown (%d options). "
                        "All available: %s",
                        doc_type, len(options), [o["t"] for o in options],
                    )
                    return []

                # The select is hidden behind a Kendo MultiSelect widget.
                # Set the value via the Kendo JS API (bypasses visibility requirement).
                kendo_ok = await page.evaluate(
                    """([selId, val]) => {
                        var el = document.getElementById(selId);
                        if (!el) return false;
                        // Kendo MultiSelect
                        if (typeof $ !== 'undefined' && $(el).data('kendoMultiSelect')) {
                            var w = $(el).data('kendoMultiSelect');
                            w.value([val]);
                            w.trigger('change');
                            return true;
                        }
                        // Kendo DropDownList
                        if (typeof $ !== 'undefined' && $(el).data('kendoDropDownList')) {
                            var w = $(el).data('kendoDropDownList');
                            w.value(val);
                            w.trigger('change');
                            return true;
                        }
                        // Plain hidden select fallback
                        for (var o of el.options) {
                            if (o.value === val) { o.selected = true; }
                        }
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }""",
                    [await sel_el.evaluate("el => el.id"), match_val],
                )
                if kendo_ok:
                    filled = True
                    # Wait for Kendo UI to update after programmatic value set
                    await page.wait_for_timeout(500)
                    # Verify the widget value actually stuck
                    verify_val = await page.evaluate(
                        """([selId]) => {
                            var el = document.getElementById(selId);
                            if (!el) return null;
                            var w = $(el).data('kendoMultiSelect');
                            if (w) return w.value();
                            w = $(el).data('kendoDropDownList');
                            if (w) return [w.value()];
                            return null;
                        }""",
                        [await sel_el.evaluate("el => el.id")],
                    )
                    logger.info(
                        "Acclaim: set doc type '%s' (value=%s, verified=%s)",
                        doc_type, match_val, verify_val,
                    )
                    if not verify_val or str(match_val) not in [str(v) for v in (verify_val or [])]:
                        logger.warning(
                            "Acclaim: doc type value did not stick (expected %s, got %s) -- retrying",
                            match_val, verify_val,
                        )
                        # Retry: clear and re-set
                        await page.evaluate(
                            """([selId, val]) => {
                                var el = document.getElementById(selId);
                                var w = $(el).data('kendoMultiSelect');
                                if (w) { w.value([]); w.value([val]); w.trigger('change'); }
                            }""",
                            [await sel_el.evaluate("el => el.id"), match_val],
                        )
                        await page.wait_for_timeout(500)
                else:
                    logger.warning("Acclaim: Kendo set failed for doc type '%s'", doc_type)
                    return []
        except Exception as e:
            logger.warning("Acclaim: could not set doc type '%s': %s", doc_type, e)
            return []

    if not filled:
        logger.warning("Acclaim: no doc type input found on search page -- skipping %s", doc_type)
        return []

    # ── Fill date range via Kendo DatePicker JS API ───────────────────
    # "HistoryObject" is a hidden JSON state field whose keys reveal the
    # real Kendo DatePicker element IDs: FromDatePicker / ToDatePicker.
    # Plain Playwright fill/locator would accidentally match HistoryObject
    # (it contains the substring "to"), so we set dates through JavaScript.
    since_fmt = _to_mdy(since_date)
    if until_date:
        to_fmt = _to_mdy(until_date)
    else:
        _n = datetime.now()
        to_fmt = f"{_n.month}/{_n.day}/{_n.year}"

    date_result = await page.evaluate(
        """([fromDate, toDate]) => {
            var results = {from: false, to: false};

            function setDP(id, dateStr, role) {
                var el = document.getElementById(id);
                if (!el) return false;
                if (typeof $ !== 'undefined' && $(el).data('kendoDatePicker')) {
                    var w = $(el).data('kendoDatePicker');
                    w.value(new Date(dateStr));
                    w.trigger('change');
                }
                // Belt-and-suspenders: also update HistoryObject JSON directly
                // (AcclaimWeb may read form state from this field, not the widget)
                var hist = document.getElementById('HistoryObject');
                if (hist) {
                    try {
                        var state = JSON.parse(hist.value || '{}');
                        var key = (role === 'from') ? 'FromDatePicker' : 'ToDatePicker';
                        state[key] = {BE: dateStr, UI: dateStr};
                        hist.value = JSON.stringify(state);
                    } catch(e) {}
                }
                return true;
            }

            // Primary: known Acclaim DatePicker IDs (from HistoryObject JSON keys)
            var fromIds = ['FromDatePicker', 'BeginDate', 'StartDate', 'FromDate'];
            var toIds   = ['ToDatePicker',   'EndDate',   'StopDate',  'ToDate'];
            for (var id of fromIds) { if (setDP(id, fromDate, 'from')) { results.from = true; break; } }
            for (var id of toIds)   { if (setDP(id, toDate,   'to'))   { results.to   = true; break; } }

            // Fallback: scan all Kendo datepicker widgets by ID keyword
            if (!results.from || !results.to) {
                var dps = document.querySelectorAll('[data-role="datepicker"]');
                dps.forEach(function(el) {
                    var lid = el.id.toLowerCase();
                    if (!results.from && (lid.includes('from') || lid.includes('begin') || lid.includes('start'))) {
                        setDP(el.id, fromDate, 'from');
                        results.from = true;
                    } else if (!results.to && lid.includes('to') && !lid.includes('history')) {
                        setDP(el.id, toDate, 'to');
                        results.to = true;
                    }
                });
            }

            // Read back HistoryObject to confirm state
            var hist = document.getElementById('HistoryObject');
            results.historyObject = hist ? hist.value : null;
            return results;
        }""",
        [since_fmt, to_fmt],
    )
    logger.debug("Acclaim: date picker set result: %s", date_result)
    if not date_result.get("from"):
        logger.warning("Acclaim: could not set from-date via Kendo API")
    if not date_result.get("to"):
        logger.warning("Acclaim: could not set to-date via Kendo API (may use page default)")
    if date_result.get("historyObject"):
        logger.debug("Acclaim: HistoryObject after date set: %s", date_result["historyObject"])

    # ── Submit search via JS (avoids navbar Search toggle) ───────────
    # page.click("button:has-text('Search')") resolves to #layoutSearchMenu
    # (the responsive navbar toggle) before the real form submit button.
    # JavaScript can scope the click to the actual search form.
    submit_result = await page.evaluate(
        """() => {
            // Scope to the form that contains the doc-type select
            var sel = document.getElementById('DocTypesList') ||
                      document.querySelector('[data-role="multiselect"]');
            var form = sel ? sel.closest('form') : null;
            if (form) {
                var btn = form.querySelector(
                    'input[type="submit"], button[type="submit"]:not(.navbar-toggle):not([data-toggle])'
                );
                if (btn) { btn.click(); return 'clicked:' + (btn.id || btn.value || btn.textContent.trim()); }
                form.submit();
                return 'form.submit()';
            }
            // Fallback: common Acclaim search button IDs
            var ids = ['btnSearch', 'SearchSubmit', 'btnSearchDocType', 'SearchButton', 'searchSubmit'];
            for (var id of ids) {
                var el = document.getElementById(id);
                if (el) { el.click(); return 'clicked:' + id; }
            }
            // Last resort: any submit button that is not the navbar toggle
            var btns = document.querySelectorAll('button[type="submit"]:not(.navbar-toggle):not([data-toggle])');
            if (btns.length > 0) { btns[0].click(); return 'clicked fallback:' + btns[0].id; }
            return false;
        }"""
    )
    logger.debug("Acclaim: search submit result: %s", submit_result)
    if not submit_result:
        logger.warning("Acclaim: could not locate search submit button -- skipping %s", doc_type)
        return []

    # Wait for the Kendo Grid to finish its AJAX data fetch after Search is
    # clicked.  The grid fires its request asynchronously — networkidle can
    # resolve before the request even starts, so we wait for a response that
    # looks like a grid data payload (JSON array or the HTML results page).
    try:
        async with page.expect_response(
            lambda r: "Search" in r.url or "DocType" in r.url or r.url.endswith("/Search/SearchTypeDoctype"),
            timeout=15_000,
        ):
            pass
    except (PwTimeout, Exception):
        # Fallback: give the AJAX request time to start, then wait for idle
        await page.wait_for_timeout(1_000)

    try:
        await page.wait_for_load_state("networkidle", timeout=25_000)
    except PwTimeout:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except PwTimeout:
            pass

    # Wait for the Kendo Grid to render data rows or its empty sentinel.
    # The grid fires a secondary AJAX request for row data AFTER networkidle
    # resolves — the structural tbody/td elements exist immediately but rows
    # are only populated once the data response arrives (~1-2s later).
    # We poll for actual data rows only (NOT the norecords sentinel) so we
    # don't exit early while AJAX is still loading — the sentinel appears
    # transiently before data arrives and caused false 0-record reads.
    try:
        await page.wait_for_function(
            """() => {
                const rows = document.querySelectorAll("table tbody tr, .k-grid tbody tr");
                for (const r of rows) {
                    const cells = r.querySelectorAll("td");
                    if (cells.length > 1 && cells[1].innerText.trim()) return true;
                }
                return false;
            }""",
            timeout=15_000,
        )
    except PwTimeout:
        # Grid truly empty or AJAX failed — add extra wait then accept whatever's there
        logger.debug("Acclaim: grid did not populate within 15s for %s — waiting 3s more", doc_type)
        await page.wait_for_timeout(3_000)

    logger.debug("Acclaim: post-search URL: %s", page.url)
    # Save screenshot only when debug logging is active (e.g., -v flag)
    if logger.isEnabledFor(logging.DEBUG):
        try:
            import tempfile, os as _os
            _ss_path = _os.path.join(tempfile.gettempdir(), f"acclaim_{doc_type.lower()}_search.png")
            await page.screenshot(path=_ss_path, full_page=True)
            logger.debug("Acclaim: screenshot saved to %s", _ss_path)
        except Exception as _e:
            logger.debug("Acclaim: screenshot failed: %s", _e)

    # ── Parse paginated results ───────────────────────────────────────
    notices: list[NoticeData] = []
    page_num = 1

    while True:
        page_notices = await _parse_results_page(
            page, county, since_date, doc_type,
            capture_item_ids=verify_pdf,
            seen_ids=seen_ids,
            until_date=until_date,
        )
        notices.extend(page_notices)
        logger.debug(
            "Acclaim %s page %d: %d records (running total %d)",
            doc_type, page_num, len(page_notices), len(notices),
        )

        # Advance to next page
        next_btn = page.locator(
            "a:has-text('Next'), a[rel='next'], "
            "input[title*='Next' i], button:has-text('Next'), "
            "a[class*='next' i], li.next a"
        ).first
        if not await next_btn.count():
            break
        if not await next_btn.is_enabled():
            break

        try:
            await next_btn.click()
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except PwTimeout:
                await page.wait_for_load_state("domcontentloaded")
            page_num += 1
        except Exception as e:
            logger.warning("Acclaim: next-page navigation failed on page %d: %s", page_num, e)
            break

        if page_num > 50:  # Safety cap: 50 pages max
            logger.warning("Acclaim: hit 50-page safety cap for doc_type=%s", doc_type)
            break

    # PDF verification: visit DocDetails for each NOTICE, OCR scanned pages,
    # confirm foreclosure keywords and extract 'commonly known as' address.
    # Also runs for DECREE to recover parcel IDs from PDF for records missing them.
    doc_type_lower = doc_type.lower()
    if verify_pdf and notices and ("notice" in doc_type_lower or "decree" in doc_type_lower):
        notices = await _enrich_notices_with_pdf(
            page, page.context, notices, doc_type, seen_ids=seen_ids
        )

    return notices


# ── DocDetails PDF verification ─────────────────────────────────────────

# Patterns that confirm a NOTICE is a mortgage foreclosure when found in OCR text.
_FORECLOSURE_OCR_KEYWORDS = frozenset({
    "lis pendens",
    "foreclosure",
    "deed of trust",
    "mortgage",
    "promissory note",
    "notice of default",
    "in default",
})

# Confirms a CJ DECREE is a mortgage/real-property foreclosure (judgment language
# differs from Lis Pendens — no "lis pendens" but "sheriff" and "foreclose" appear).
_DECREE_FORECLOSURE_KEYWORDS = frozenset({
    "foreclosure",
    "foreclose",
    "deed of trust",
    "mortgage",
    "sheriff",
    "promissory note",
})

# Oklahoma probate decree heir extraction — "[NAME], residing at [STREET], [CITY], OK [ZIP]"
_HEIR_RE = re.compile(
    r"([A-Z][A-Z\s\.,'-]{4,50}?),?\s+"
    r"(?:residing|resides|who\s+resides|whose\s+address\s+is)\s+(?:at\s+)?"
    r"(\d{1,5}\s+[^\n,]{5,60}),\s*"
    r"([A-Za-z][A-Za-z\s]{2,30}),?\s*"
    r"(?:OK|Oklahoma)[,\s]*"
    r"(\d{5}(?:-\d{4})?)",
    re.IGNORECASE,
)

# Heir table format — "NAME RELATIONSHIP [AGE]\nSTREET\n\nCITY, OK ZIP"
# Used when decree lists heirs in a columnar table without "residing at"
# NOTE: name char class uses space (not \s) to prevent spanning newlines into header rows
_HEIR_TABLE_RE = re.compile(
    r"([A-Za-z][A-Za-z ,\.,'-]{4,50}?)\s+"
    r"(?:Husband|Wife|Spouse|Son|Daughter|Child(?:ren)?|Brother|Sister|"
    r"Father|Mother|Parent|Nephew|Niece|Uncle|Aunt|Grand\w+|Step\w+|Heir)\b"
    r"[^\n]*\n+"
    r"(\d{1,5}\s+[^\n,]{5,60})\s*\n+"
    r"([A-Za-z][A-Za-z\s]{2,30}),?\s*(?:OK|Oklahoma)[,\s]*(\d{5}(?:-\d{4})?)",
    re.IGNORECASE,
)

# Heir inline format — name + relationship + street ADDRESS all on ONE line
# Used when heir is out-of-state (city/zip on next page or cut off at page boundary)
# Groups: (name, street)
# Address allows commas (e.g. "2720 N, Hearthside St" — OCR renders "N." as "N,")
# Trailing OCR noise (": | ;") is absorbed by the end anchor
_HEIR_INLINE_RE = re.compile(
    r"^([A-Za-z][A-Za-z ,\.,'-]{2,40}?)\s+"
    r"(?:Husband|Wife|Spouse|Son|Daughter|Child(?:ren)?|"
    r"Brother|Sister|Father|Mother|Parent|Nephew|Niece|Uncle|Aunt|Grand\w+|Step\w+|Heir|Adult)\b"
    r"\s+(\d{1,5}\s+[^\n]{4,60}?)\s*[:\|\.;]*\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Named Personal Representative sentence: "Personal Representative, NAME, appears/has/was..."
# Used as a fallback when the heir table can't be parsed (garbled OCR line-wrap) but the
# decree still names the PR in plain prose elsewhere in the document. No address is given
# by this pattern — downstream DM address lookup (Assessor/People Search/Tracerfy) fills it in.
# Name may wrap across a line break (real \n in OCR text); collapsed to a single space below.
_PR_NAMED_RE = re.compile(
    r"Personal\s+Repres[ce]ntative,\s+"
    r"([A-Z][A-Za-z.\s]{3,45}?)"
    r",\s*(?:has|appears|was|is)\b",
    re.IGNORECASE,
)

# Heir pipe-delimited devisee format: "NAME | STREET [Adult] REL | CITY, Oklahoma ZIP"
# Used when OCR renders a PDF table with | column separators.
# Some rows use / instead of | between name and street; some have no separator at all.
# NOTE: the real OCR `text` uses newlines between table rows, not literal "|" — the
# pipe only appears in the cosmetic debug-log rendering (text.replace("\n", " | ")
# above). The separators below must therefore also match "\n", or this pass never
# fires against real data (confirmed 2026-07-09: 0 matches on real text, 3 on the
# log-formatted string that was used to "verify" the fix).
# Groups: (name, street, relationship, city, zip)
_HEIR_DEVISEE_RE = re.compile(
    r"([A-Za-z][A-Za-z .,'-]{3,45}?)"           # Group 1: Name (First Last natural order)
    r"\s*(?:[|/\n]\s*)?"                          # Optional column separator (| or / or newline)
    r"(\d{1,5}[^|\n]{3,55}?)"                    # Group 2: Street (lazy — stops at | or newline)
    r"\s+(?:Adult\s+)?"                          # Optional "Adult" modifier before relationship
    r"(Son|Daughter|Husband|Wife|Spouse|Brother|Sister|Father|Mother|"  # Group 3: Relationship
    r"Child(?:ren)?|Grandchild|Stepchild|Heir)\b"
    r"[^|\n]*"                                    # Rest of relationship column text
    r"\s*[|\n]+\s*"                               # Separator before city column (pipe or newline)
    r"([A-Za-z][A-Za-z\s]{2,30}?)"              # Group 4: City
    r",?\s*(?:OK|Oklahoma)[,\s]*"                # State
    r"(\d{5}(?:-\d{4})?)",                      # Group 5: ZIP
    re.IGNORECASE,
)

_PROBATE_RELATIONSHIPS = (
    "son", "daughter", "wife", "husband", "spouse", "mother", "father",
    "brother", "sister", "grandson", "granddaughter", "nephew", "niece",
    "grandchild", "child", "stepson", "stepdaughter",
    "executor", "executrix", "administrator", "administratrix",
    "personal representative",
)

# "Commonly known as" address pattern in Lis Pendens text
_COMMONLY_KNOWN_RE = re.compile(
    r"commonly\s+known\s+as\s*[:\-]?\s*"
    r"(\d{1,5}\s+[A-Z0-9 \.\-]+(?:ST|AVE|BLVD|DR|CT|WAY|PL|RD|LN|CIR|PKWY|HWY|LOOP|TRAIL|TRL|RUN)[A-Z \.]*)"
    r"(?:[,\s]+([A-Za-z\s]+))?"
    r"(?:[,\s]+(OK|Oklahoma))?"
    r"(?:[,\s]+(\d{5}(?:-\d{4})?))?",
    re.IGNORECASE,
)

# Tulsa County's actual phrasing is usually "a/k/a [address]" in the Legal
# Description section, not "commonly known as" — confirmed 2026-07-10 on the
# Keeton Lis Pendens (Sharp Mortgage Company v. Keeton, CJ-2026-02827).
_AKA_ADDRESS_RE = re.compile(
    r"a/?k/?a\s*"
    r"(\d{1,5}\s+[A-Z0-9 \.\-]+(?:ST|AVE|BLVD|DR|CT|WAY|PL|RD|LN|CIR|PKWY|HWY|LOOP|TRAIL|TRL|RUN)[A-Z \.]*)"
    r"(?:[,\s]+([A-Za-z\s]+))?"
    r"(?:[,\s]+(OK|Oklahoma))?"
    r"(?:[,\s]+(\d{5}(?:-\d{4})?))?",
    re.IGNORECASE,
)


def _extract_all_properties(ocr_text: str) -> list[dict]:
    """Find every distinct property named in a Lis Pendens/NOTICE filing.

    A single foreclosure suit can name multiple properties (e.g. a lender
    foreclosing on two parcels securing the same loan, or several defendants'
    separate properties in one consolidated action) — confirmed 2026-07-10 on
    a Keeton Lis Pendens naming both "1834 W 64th St" and "4103 W 61st St".
    Both `_COMMONLY_KNOWN_RE` and `_AKA_ADDRESS_RE` previously only matched
    the first occurrence via `.search()`; this uses `finditer()` on both and
    pairs results with parcel numbers found in document order (best-effort —
    OCR line-break noise means this pairing isn't guaranteed, but property
    address and parcel number consistently appear in the same Legal
    Description block in practice).

    Returns a list of dicts: [{"street", "city", "zip", "parcel_id"}, ...],
    deduplicated by normalized street, in document order.
    """
    properties: list[dict] = []
    seen_streets: set[str] = set()

    for pattern in (_COMMONLY_KNOWN_RE, _AKA_ADDRESS_RE):
        for m in pattern.finditer(ocr_text):
            street = (m.group(1) or "").strip().title()
            if not street:
                continue
            norm = re.sub(r"\s+", " ", street.upper())
            if norm in seen_streets:
                continue
            seen_streets.add(norm)
            properties.append({
                "street": street,
                "city": (m.group(2) or "").strip().title() or "Tulsa",
                "zip": (m.group(4) or "").strip(),
                "parcel_id": "",
            })

    parcels = [pm.group(1) for pm in _PARCEL_RE.finditer(ocr_text)]
    for i, prop in enumerate(properties):
        if i < len(parcels):
            prop["parcel_id"] = parcels[i]

    return properties


async def _get_item_id_for_row(page: Page, row_locator) -> Optional[str]:
    """Click a grid row and intercept GetToken to extract the itemId.

    Returns the numeric itemId string, or None if not captured.
    """
    captured: list[str] = []

    async def _on_req(req):
        m = re.search(r"GetToken\?itemId=(\d+)", req.url)
        if m:
            captured.append(m.group(1))

    page.on("request", _on_req)
    try:
        await row_locator.click(force=True)
        await page.wait_for_timeout(2000)
    except Exception as e:
        logger.debug("Acclaim: row click failed: %s", e)
    finally:
        page.remove_listener("request", _on_req)

    return captured[0] if captured else None


async def _get_doc_image_urls(
    page: Page,
    ctx: BrowserContext,
    item_id: str,
    row_idx: int,
    num_pages: int = 2,
) -> list[str]:
    """Open DocDetails for an item and capture all page image request URLs.

    Flow:
      1. Call GetToken API (authenticated fetch from main page) → JWT token
      2. Navigate to DocDetails in a new tab using that token
      3. For each page button (1, 2…): click it, intercept the image request
      4. Return captured image URLs for OCR

    Avoids page.evaluate() on doc_page — DocDetails has a CSP that blocks eval.
    Uses Playwright locators and request interception instead.
    """
    import urllib.parse

    # ── Step 1: get token via Playwright APIRequestContext ────────────────
    # ctx.request shares the browser's authenticated session (cookies, etc.)
    # No page.evaluate() needed — avoids CSP restrictions entirely.
    try:
        token_url = f"{_BASE_URL}/Document/GetToken?itemId={item_id}"
        api_resp = await ctx.request.get(
            token_url,
            headers={"Referer": f"{_BASE_URL}/Search/SearchTypeDoctype"},
        )
        if not api_resp.ok:
            logger.debug(
                "Acclaim: GetToken returned %d for itemId=%s", api_resp.status, item_id
            )
            return []
        token_data = await api_resp.json()
        token = token_data.get("token") or ""
    except Exception as e:
        logger.debug("Acclaim: GetToken request failed for itemId=%s: %s", item_id, e)
        return []

    if not token:
        logger.debug("Acclaim: no token returned for itemId=%s", item_id)
        return []

    details_url = (
        f"{_BASE_URL}/Document/DocDetails"
        f"?incomingTransactionItemId={urllib.parse.quote(token)}&rowId={row_idx}"
    )

    doc_page = await ctx.new_page()
    image_urls: list[str] = []

    try:
        # Intercept ALL requests on doc_page to find image loads
        all_image_reqs: list[str] = []

        async def _on_doc_req(req):
            url_lower = req.url.lower()
            if "tulsacounty" in url_lower or "acclaim" in url_lower:
                if any(x in url_lower for x in ["getimage", "image", ".jpg", ".png", ".tif", "rendition"]):
                    all_image_reqs.append(req.url)

        doc_page.on("request", _on_doc_req)

        # Wait for the Atala WebDocViewer to fire its first render request.
        # expect_request must wrap the navigation so the listener is active before
        # the request fires (wait_for_request is not available in this Playwright build).
        try:
            async with doc_page.expect_request(
                lambda r: "webdocviewerhandler" in r.url.lower(),
                timeout=45000,  # up to 45s for slow server renders
            ):
                await doc_page.goto(details_url, wait_until="domcontentloaded")
        except PwTimeout:
            logger.debug(
                "Acclaim: WebDocViewer didn't fire within 45s for itemId=%s", item_id
            )
            # Fail-open: return empty so _enrich_notices_with_pdf keeps the record
            doc_page.remove_listener("request", _on_doc_req)
            return []

        # Page 2+ is often where the actual Lis Pendens/foreclosure language lives
        # (page 1 is frequently just the case caption). A blind fixed sleep here
        # previously missed page 2 whenever the viewer rendered it slowly, silently
        # truncating OCR text to page 1 only and causing real foreclosures to be
        # dropped as "NOT foreclosure (no keywords)". Actively wait for a second
        # distinct page-image request instead of guessing a fixed delay; fail open
        # (proceed with page 1 only) if the document genuinely has just one page.
        try:
            async with doc_page.expect_request(
                lambda r: "webdocviewerhandler" in r.url.lower(),
                timeout=10000,
            ):
                pass
        except PwTimeout:
            logger.debug(
                "Acclaim: no page-2 image request within 10s for itemId=%s "
                "(likely single-page document)", item_id,
            )

        # Final settle wait for any trailing page 3/4 requests
        await doc_page.wait_for_timeout(2000)

        # Verify page loaded
        body_text = await doc_page.inner_text("body")
        logger.debug("Acclaim: DocDetails loaded, body length=%d", len(body_text))

        _ORIGIN = "https://acclaim.tulsacounty.org"

        def _absolutify(url: str) -> str:
            if url.startswith("http"):
                return url
            if url.startswith("/"):
                return _ORIGIN + url
            return url

        # Collect WebDocViewerHandler URLs from request interception (most reliable)
        doc_page_urls = [
            _absolutify(u) for u in all_image_reqs
            if "webdocviewerhandler" in u.lower() or "ataladocpage" in u.lower()
        ]

        # Also check img tags as fallback (in case interception missed any)
        if not doc_page_urls:
            imgs = await doc_page.locator("img").all()
            for img in imgs:
                src = await img.get_attribute("src") or ""
                if src and ("webdocviewerhandler" in src.lower() or "ataladocpage" in src.lower()):
                    doc_page_urls.append(_absolutify(src))

        doc_page.remove_listener("request", _on_doc_req)

        # Deduplicate while preserving order
        seen: set[str] = set()
        image_urls = []
        for u in doc_page_urls:
            if u not in seen:
                seen.add(u)
                image_urls.append(u)

        logger.debug(
            "Acclaim: DocDetails itemId=%s → %d doc-page URLs: %s",
            item_id, len(image_urls), image_urls[:3],
        )

    except Exception as e:
        logger.debug("Acclaim: DocDetails failed for itemId=%s: %s", item_id, e)
    finally:
        try:
            await doc_page.close()
        except Exception:
            pass

    return image_urls


async def _ocr_acclaim_image(page: Page, image_url: str) -> str:
    """Download an Acclaim document image and OCR it.

    Uses Playwright APIRequestContext (shares session cookies, no eval).
    Returns empty string on failure.
    """
    try:
        import pytesseract
        from PIL import Image

        # Set explicit path on Windows (installer doesn't add to PATH)
        if sys.platform == "win32":
            pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

        # Use APIRequestContext — shares browser cookies, no page.evaluate() needed
        api_resp = await page.context.request.get(
            image_url,
            headers={"Referer": f"{_BASE_URL}/Document/DocDetails"},
        )
        if not api_resp.ok:
            logger.debug(
                "Acclaim: image fetch HTTP %d for %s", api_resp.status, image_url[:80]
            )
            return ""

        img_data = await api_resp.body()
        if not img_data:
            return ""

        img = Image.open(io.BytesIO(img_data))

        # Preprocess for better Tesseract accuracy on low-DPI court document scans.
        # Scanned Acclaim docs are often 150-200 DPI; Tesseract works best at 300 DPI.
        try:
            import cv2
            import numpy as np
            gray = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2GRAY)
            # 2x upscale brings ~150 DPI scans to ~300 DPI effective resolution
            gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            # Otsu binarization: auto-determines optimal threshold for black/white text
            _, gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            img = Image.fromarray(gray)
        except Exception:
            pass  # fall back to raw image if cv2 unavailable

        text = pytesseract.image_to_string(img, config="--psm 6 --oem 1")
        return text.strip()

    except Exception as e:
        logger.debug("Acclaim: OCR failed for %s: %s", image_url[:80], e)
        return ""


def _parse_ocr_for_address_and_keywords(
    ocr_text: str,
) -> tuple[bool, str, str, str]:
    """Parse OCR'd Lis Pendens text.

    Returns (is_foreclosure, street, city, zip_code).
    """
    lower = ocr_text.lower()
    is_foreclosure = any(kw in lower for kw in _FORECLOSURE_OCR_KEYWORDS)

    street = city = zip_code = ""
    m = _COMMONLY_KNOWN_RE.search(ocr_text)
    if m:
        street   = m.group(1).strip().title() if m.group(1) else ""
        city     = (m.group(2) or "").strip().title() or "Tulsa"
        zip_code = (m.group(4) or "").strip()

    return is_foreclosure, street, city, zip_code


def _normalize_ocr_street(s: str) -> str:
    """Fix common OCR corruptions in extracted street address strings."""
    s = re.sub(r'(\d+)[%$#@]', r'\1th', s)                      # "115%" / "115$" → "115th"
    s = re.sub(r'(\d+)"(?=\s|[A-Za-z])', r'\1th', s)  # 115" -> 115th (OCR artifact)
    s = re.sub(r'(?<![A-Za-z])F\.(?![A-Za-z])', 'E.', s)        # "F." directional → "E."
    return s


def _parse_probate_decree(text: str) -> dict:
    """Extract property address and heir data from OCR'd Oklahoma probate decree.

    Returns {"address": str, "city": str, "zip": str, "heirs": list[dict]}
    where each heir dict has keys: name, street, city, zip, relationship.
    """
    result: dict = {"address": "", "city": "", "zip": "", "heirs": []}

    # Log full OCR text in 1000-char chunks so we can diagnose missing patterns
    clean = text.replace("\n", " | ")
    for _ci in range(0, min(len(clean), 6000), 1000):
        logger.debug("DECREE probate OCR [%d-%d]: %s", _ci, _ci + 1000, clean[_ci:_ci + 1000])

    # Normalize common OCR artifacts that break address regexes:
    #   "1710S." → "1710 S."  (digit immediately followed by directional letter+dot)
    #   "1710$." → "1710 S."  (OCR renders S as $)
    norm = re.sub(r'(\d)([\$])\.', r'\1 S.', text)        # 1710$. → 1710 S.
    norm = re.sub(r'(\d)([NSEW])\.', r'\1 \2.', norm)     # 1710S. → 1710 S.
    norm = re.sub(r'\$\.', 'S.', norm)                     # standalone $. → S.

    # Property address: "commonly known as" pattern
    m = _COMMONLY_KNOWN_RE.search(norm)
    if m:
        result["address"] = m.group(1).strip().title() if m.group(1) else ""
        result["city"]    = (m.group(2) or "").strip().title() or "Tulsa"
        result["zip"]     = (m.group(4) or "").strip()

    # Fallback 1: "located at" / "situate[d] at"
    if not result["address"]:
        m2 = re.search(
            r"(?:located|situate[d]?)\s+at\s+"
            r"(\d{1,5}\s+[^\n,]{5,60}),\s*"
            r"([A-Za-z][A-Za-z\s]{2,30}),?\s*"
            r"(?:OK|Oklahoma)[,\s]*(\d{5})",
            norm, re.IGNORECASE,
        )
        if m2:
            result["address"] = m2.group(1).strip().title()
            result["city"]    = m2.group(2).strip().title()
            result["zip"]     = m2.group(3).strip()

    # Fallback 2: ancillary probate lettered list "a. 1710 S. Cheyenne Ave W., Tulsa, OK 74119"
    if not result["address"]:
        m3 = re.search(
            r"\b[a-z]\.\s*(\d{1,5}\s+[A-Za-z0-9][^\n,]{4,60}?)\s*,\s*"
            r"([A-Za-z][A-Za-z\s]{2,25})\s*,?\s*"
            r"(?:OK|Oklahoma)[,\s]*(\d{5}(?:-\d{4})?)",
            norm, re.IGNORECASE,
        )
        if m3:
            result["address"] = m3.group(1).strip().title()
            result["city"]    = m3.group(2).strip().title()
            result["zip"]     = m3.group(3).strip()

    # Fallback 3: bare street address followed by OK zip ("12345 E Main St, Tulsa, OK 74133")
    if not result["address"]:
        m4 = re.search(
            r"(\d{1,5}\s+[A-Za-z0-9 \.\-]{5,50}?)\s*,\s*"
            r"([A-Za-z][A-Za-z\s]{2,25})\s*,?\s*"
            r"(?:OK|Oklahoma)\s*"
            r"(\d{5}(?:-\d{4})?)",
            norm, re.IGNORECASE,
        )
        if m4:
            result["address"] = m4.group(1).strip().title()
            result["city"]    = m4.group(2).strip().title()
            result["zip"]     = m4.group(3).strip()

    # Normalize OCR corruptions in extracted property address (e.g. "115%" → "115th", "F." → "E.")
    if result["address"]:
        result["address"] = _normalize_ocr_street(result["address"])

    # Extract petitioner's full name — used to resolve single-name heir entries in ancillary probates
    # Pattern: "Petition of Lynda Austin, Petitioner" → "Lynda Austin"
    petitioner_name = ""
    _pm = re.search(
        r"Petition\s+of\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s*[,.]?\s*Petitioner",
        norm, re.IGNORECASE,
    )
    if _pm:
        petitioner_name = _pm.group(1).strip()
        logger.debug("DECREE probate: petitioner = %s", petitioner_name)

    # Heir extraction pass 1: "[NAME], residing at [STREET], [CITY], OK [ZIP]"
    _skip = {"court", "state", "county", "oklahoma", "district", "decedent", "estate"}
    seen: set = set()

    def _add_heir(raw_name: str, street: str, city: str, zip_: str, rel: str = "") -> None:
        raw = raw_name.strip().rstrip(",. ")
        if len(raw.split()) < 2:
            return
        if any(s in raw.lower() for s in _skip):
            return
        key = raw.upper()
        if key in seen:
            return
        seen.add(key)
        # Normalize OCR corruptions in street, then title-case with ordinal fix
        raw_street = _normalize_ocr_street(street.strip()).title()
        raw_street = re.sub(
            r'(\d)(St|Nd|Rd|Th)\b',
            lambda m: m.group(1) + m.group(2).lower(),
            raw_street,
        )
        result["heirs"].append({
            "name":         _pdf_name_to_court_format(key),
            "street":       raw_street,
            "city":         city.strip().title(),
            "zip":          zip_.strip(),
            "relationship": rel,
        })

    for hm in _HEIR_RE.finditer(norm):
        ctx_text = norm[hm.end(): hm.end() + 250]
        rel = ""
        for r in _PROBATE_RELATIONSHIPS:
            if re.search(r"\b" + re.escape(r) + r"\b", ctx_text, re.IGNORECASE):
                rel = r
                break
        _add_heir(hm.group(1), hm.group(2), hm.group(3), hm.group(4), rel)

    # Heir extraction pass 2: table format "NAME RELATIONSHIP\nSTREET\n\nCITY, OK ZIP"
    for hm in _HEIR_TABLE_RE.finditer(text):
        # Relationship keyword is in the match itself — extract it
        rel_m = re.search(
            r"\b(Husband|Wife|Spouse|Son|Daughter|Child(?:ren)?|Brother|Sister|"
            r"Father|Mother|Parent|Nephew|Niece|Uncle|Aunt|Grand\w+|Step\w+|Heir)\b",
            hm.group(0), re.IGNORECASE,
        )
        rel = rel_m.group(1).lower() if rel_m else ""
        _add_heir(hm.group(1), hm.group(2), hm.group(3), hm.group(4), rel)

    # Heir extraction pass 3: inline format "NAME RELATIONSHIP STREET" all on one line
    # Handles out-of-state heirs where city/zip are on next page or cut off at page boundary
    # Single-name heirs (e.g. "Lynda") are resolved via petitioner cross-reference
    for hm in _HEIR_INLINE_RE.finditer(text):
        raw_name = hm.group(1).strip()
        raw_street = hm.group(2).strip()
        rel_m = re.search(
            r"\b(Husband|Wife|Spouse|Son|Daughter|Child(?:ren)?|Brother|Sister|"
            r"Father|Mother|Parent|Nephew|Niece|Uncle|Aunt|Grand\w+|Step\w+|Heir|Adult)\b",
            hm.group(0), re.IGNORECASE,
        )
        rel = rel_m.group(1).lower() if rel_m else ""

        # Resolve single-name heir via petitioner cross-reference (ancillary probate pattern)
        name_parts = raw_name.split()
        if len(name_parts) == 1 and petitioner_name:
            petitioner_first = petitioner_name.split()[0]
            if raw_name.upper() == petitioner_first.upper():
                logger.debug(
                    "DECREE probate: resolved single-name heir '%s' → '%s' via petitioner",
                    raw_name, petitioner_name,
                )
                raw_name = petitioner_name

        _add_heir(raw_name, raw_street, "", "", rel)

    # Heir extraction pass 4: pipe-delimited devisee table format
    # "NAME | STREET ADULT SON | CITY, Oklahoma ZIP Devisee and/or Legatee"
    # Must run on text (not norm): norm strips all | characters, which the regex requires.
    for hm in _HEIR_DEVISEE_RE.finditer(text):
        _add_heir(
            hm.group(1),
            hm.group(2).strip(),
            hm.group(4).strip(),
            hm.group(5).strip(),
            hm.group(3).lower(),
        )

    # Heir extraction pass 5: named Personal Representative fallback (no address)
    # Only fires for names not already captured with a real address by passes 1-4.
    if not result["heirs"]:
        m_pr = _PR_NAMED_RE.search(text)
        if m_pr:
            pr_name = re.sub(r"\s+", " ", m_pr.group(1)).strip()
            _add_heir(pr_name, "", "", "", "personal representative")

    return result


async def _enrich_notices_with_pdf(
    page: Page,
    ctx: BrowserContext,
    notices: list[NoticeData],
    doc_type: str,
    seen_ids: Optional[dict] = None,
) -> list[NoticeData]:
    """Download+OCR DocDetails page images for NOTICE and DECREE records.

    itemIds must already be stored in notice.raw_text as '|acclaim_item_id:{id}'.
    Items without an itemId are kept as-is (fail-open).

    NOTICE path: confirm foreclosure keywords, extract 'commonly known as' address,
    drop non-foreclosures.

    DECREE — probate path (PB/EP case prefix): extract property address + heir names/
    addresses from court-filed probate decree. Populates DM fields directly — no
    obituary enricher or skip trace needed for these records.

    DECREE — CJ path (foreclosure judgment): verify foreclosure keywords, extract
    parcel number or 'commonly known as' address, drop non-foreclosure civil judgments.
    """
    is_decree = "decree" in doc_type.lower()
    _dt_tag = "DECREE" if is_decree else "NOTICE"
    notices_to_process = notices

    enriched: list[NoticeData] = []
    confirmed = dropped = recovered = 0

    def _cache_dropped(n: NoticeData) -> None:
        """Add a dropped record to seen_ids so it is skipped on future runs."""
        if seen_ids is None:
            return
        m_i = re.search(r'instrumentNumber=(\d+)', n.source_url or '')
        if m_i:
            seen_ids[m_i.group(1)] = f"{datetime.now().strftime('%Y-%m-%d')}|{_dt_tag}|DROPPED"

    for notice in notices_to_process:
        # Extract itemId embedded in raw_text by _parse_results_page
        m = re.search(r"\|acclaim_item_id:(\d+)", notice.raw_text or "")
        item_id = m.group(1) if m else ""

        if not item_id:
            logger.debug("Acclaim: no itemId stored — keeping %s", notice.owner_name)
            notice.needs_assessor_lookup = True
            enriched.append(notice)
            confirmed += 1
            continue

        # Get document page image URLs from DocDetails
        image_urls = await _get_doc_image_urls(page, ctx, item_id, row_idx=1)
        if not image_urls:
            logger.debug("Acclaim: no image URLs for itemId=%s — keeping", item_id)
            notice.needs_assessor_lookup = True
            enriched.append(notice)
            confirmed += 1
            continue

        # OCR all pages and combine text (up to 4 pages — foreclosure language often on p2+)
        full_text = ""
        for _pi, url in enumerate(image_urls[:4]):
            page_text = await _ocr_acclaim_image(page, url)
            if logger.isEnabledFor(logging.DEBUG) and notice.notice_type == "probate" and is_decree:
                logger.debug("DECREE probate page %d OCR snippet: %s", _pi, page_text[:300].replace("\n", " | "))
            full_text += "\n" + page_text

        if not full_text.strip():
            logger.debug("Acclaim: OCR returned empty for itemId=%s — keeping", item_id)
            notice.needs_assessor_lookup = True
            enriched.append(notice)
            confirmed += 1
            continue

        # ── DECREE path ──────────────────────────────────────────────────────
        if is_decree:
            if notice.notice_type == "probate":
                # Probate DECREE: extract property address + heirs from court filing
                probate = _parse_probate_decree(full_text)

                if probate["address"] and not notice.address:
                    notice.address = probate["address"]
                    notice.city    = probate["city"] or notice.city
                    notice.zip     = probate["zip"] or notice.zip
                    notice.needs_assessor_lookup = True  # get sqft for buy-box filter
                    recovered += 1
                    logger.info(
                        "  DECREE probate address: '%s' -> %s, %s",
                        notice.owner_name, notice.address, notice.city,
                    )

                heirs = probate["heirs"]
                if heirs:
                    dm = heirs[0]
                    notice.decision_maker_name         = dm["name"]
                    notice.decision_maker_relationship = dm["relationship"]
                    notice.decision_maker_street       = dm["street"]
                    notice.decision_maker_city         = dm["city"]
                    notice.decision_maker_state        = "OK"
                    notice.decision_maker_zip          = dm["zip"]
                    notice.decision_maker_status       = "verified_living"
                    notice.decision_maker_source       = "probate_decree_pdf"
                    notice.dm_confidence               = "high"
                    notice.dm_confidence_reason        = "Court-filed probate decree"
                    notice.owner_deceased              = "yes"
                    if len(heirs) > 1:
                        notice.decision_maker_2_name         = heirs[1]["name"]
                        notice.decision_maker_2_relationship = heirs[1]["relationship"]
                        notice.decision_maker_2_status       = "verified_living"
                    if len(heirs) > 2:
                        notice.decision_maker_3_name         = heirs[2]["name"]
                        notice.decision_maker_3_relationship = heirs[2]["relationship"]
                        notice.decision_maker_3_status       = "verified_living"
                    logger.info(
                        "  DECREE probate heirs: '%s' -> %d heir(s) (DM: %s @ %s)",
                        notice.owner_name, len(heirs), dm["name"], dm["street"],
                    )
                    # Surviving spouse address as property address proxy — when the decree
                    # only has a legal description (no street address), the marital home
                    # is almost always the inherited property. Use the spouse's address
                    # as the property address so Smarty/buy-box can evaluate it.
                    if not notice.address and dm["street"] and dm["zip"]:
                        spouse_rel = dm.get("relationship", "").lower()
                        if spouse_rel in ("husband", "wife", "spouse"):
                            notice.address = dm["street"]
                            notice.city    = dm["city"] or notice.city
                            notice.zip     = dm["zip"]
                            notice.needs_assessor_lookup = True  # verify parcel via assessor
                            recovered += 1
                            logger.info(
                                "  DECREE probate spouse-proxy address: '%s' -> %s, %s",
                                notice.owner_name, notice.address, notice.city,
                            )
                else:
                    notice.owner_deceased = "yes"  # probate estate = deceased, even without heirs
                    notice.needs_assessor_lookup = True
                    logger.debug(
                        "Acclaim DECREE probate: no heirs found in PDF for '%s'",
                        notice.owner_name,
                    )

                enriched.append(notice)
                confirmed += 1
                continue

            # CJ DECREE (foreclosure judgment): verify keywords, extract parcel
            is_foreclosure_decree = any(
                kw in full_text.lower() for kw in _DECREE_FORECLOSURE_KEYWORDS
            )
            if not is_foreclosure_decree:
                if len(full_text.strip()) < 300:
                    logger.debug(
                        "Acclaim: DECREE OCR sparse (%d chars) — keeping '%s'",
                        len(full_text.strip()), notice.owner_name,
                    )
                    notice.needs_assessor_lookup = True
                    enriched.append(notice)
                    confirmed += 1
                    continue
                logger.info(
                    "Acclaim PDF: NOT foreclosure decree (no keywords, %d chars) — dropped '%s'",
                    len(full_text.strip()), notice.owner_name,
                )
                _cache_dropped(notice)
                dropped += 1
                continue

            # Confirmed CJ foreclosure — extract parcel or address if missing
            if not notice.parcel_id:
                pm = _PARCEL_RE.search(full_text)
                if pm:
                    notice.parcel_id = pm.group(1)
                    recovered += 1
                    logger.info(
                        "  DECREE PDF parcel: '%s' -> %s",
                        notice.owner_name, notice.parcel_id,
                    )
                else:
                    _, street, city, zip_code = _parse_ocr_for_address_and_keywords(full_text)
                    if street:
                        notice.address = street
                        notice.city    = city or notice.city
                        notice.zip     = zip_code or notice.zip
                        notice.needs_assessor_lookup = True  # get sqft/year/baths for buy-box filter
                        recovered += 1
                        logger.info(
                            "  DECREE PDF address: '%s' -> %s, %s",
                            notice.owner_name, street, city,
                        )
                    else:
                        notice.needs_assessor_lookup = True

            enriched.append(notice)
            confirmed += 1
            continue

        is_foreclosure, street, city, zip_code = _parse_ocr_for_address_and_keywords(full_text)

        if not is_foreclosure:
            # NOTICE records only reach this point after already passing the
            # structured, non-OCR grantor-is-a-lender check (_LENDER_RE, applied
            # in _parse_results_page against Acclaim's own Grantor field) — that
            # check is the reliable foreclosure confirmation. This OCR keyword
            # check was meant as secondary confirmation, but Tulsa County's actual
            # document title is "NOTICE OF PENDENCY OF ACTION AFFECTING REAL
            # ESTATE" (12 O.S. Sec. 2004.2), not literally "Lis Pendens" — combined
            # with imperfect OCR, this was dropping real, already-lender-confirmed
            # foreclosures (confirmed 2026-07-10 via manual review of Agha,
            # Oconnell, Dargatz, Kinkead, Huerta — all had a legitimate lender
            # grantor but no matching keyword in the OCR text). Fail-open here too:
            # keep the record and flag for assessor follow-up rather than drop.
            logger.debug(
                "Acclaim: no foreclosure keywords in OCR (%d chars) but grantor "
                "already confirmed lender — keeping '%s'",
                len(full_text.strip()), notice.owner_name,
            )
            notice.needs_assessor_lookup = True
            enriched.append(notice)
            confirmed += 1
            continue

        confirmed += 1

        # Update address from "commonly known as" if found
        if street:
            if not notice.address:
                recovered += 1
                logger.info(
                    "  PDF situs address: '%s' -> %s, %s %s",
                    notice.owner_name, street, city, zip_code,
                )
            notice.address  = street
            notice.city     = city or notice.city
            notice.zip      = zip_code or notice.zip
        # Always flag for Assessor — get sqft/year/baths for buy-box filter regardless of OCR address
        notice.needs_assessor_lookup = True

        enriched.append(notice)

        # Multi-property filings: a single Lis Pendens can name more than one
        # property (confirmed 2026-07-10 on Keeton — one suit named both
        # "1834 W 64th St" and "4103 W 61st St"). Clone a separate record for
        # each additional property found. needs_assessor_lookup=True is safe
        # here specifically because each clone already carries its OWN parcel
        # ID extracted from the document's Legal Description section —
        # tulsa_assessor.lookup_addresses_tulsa() checks for
        # (parcel_id AND address) already set and, when found, fetches
        # property characteristics directly by parcel number instead of
        # searching by owner name. That sidesteps the real risk: searching by
        # the PRIMARY owner's name would just re-find their own property and
        # overwrite this clone's correct address with the wrong one, since
        # ownership of a co-listed property may belong to a different named
        # defendant. Ownership itself is still unverified, so that's flagged
        # via missing_data_flags rather than assumed.
        all_props = _extract_all_properties(full_text)
        primary_norm = re.sub(r"\s+", " ", (notice.address or "").upper())
        extra_props = [
            p for p in all_props
            if re.sub(r"\s+", " ", p["street"].upper()) != primary_norm
        ]
        for i, prop in enumerate(extra_props, 2):
            clone = dataclasses.replace(
                notice,
                address=prop["street"],
                city=prop["city"] or notice.city,
                zip=prop["zip"] or "",
                parcel_id=prop["parcel_id"],
                source_url=f"{notice.source_url}#property{i}",
                needs_assessor_lookup=True,
                missing_data_flags=(
                    (notice.missing_data_flags + "|" if notice.missing_data_flags else "")
                    + "co_listed_property_owner_unverified"
                ),
            )
            enriched.append(clone)
            recovered += 1
            logger.info(
                "  Multi-property Lis Pendens: '%s' also names %s, %s "
                "(owner unverified — same suit as %s)",
                notice.owner_name, prop["street"], prop["city"], notice.owner_name,
            )

        await page.wait_for_timeout(300)

    logger.info(
        "Acclaim %s PDF verify: %d confirmed, %d dropped, %d recovered (parcel/address/heirs)",
        doc_type, confirmed, dropped, recovered,
    )
    return enriched


def _to_court_format(name: str) -> str:
    """Convert Acclaim 'LAST FIRST MIDDLE' to 'LAST, FIRST MIDDLE' court format.

    Acclaim stores names without commas. OSCN and the rest of the pipeline
    expect 'LAST, FIRST' format for correct first/last splitting.
    Skips names that already have commas or are single-word.
    """
    name = name.strip()
    if not name or "," in name:
        return name
    parts = name.split(None, 1)
    if len(parts) < 2:
        return name
    return f"{parts[0]}, {parts[1]}"


def _pdf_name_to_court_format(name: str) -> str:
    """Convert PDF heir name 'FIRST [MIDDLE] LAST' to 'LAST, FIRST [MIDDLE]' court format.

    PDF OCR names appear in natural reading order (First Last), unlike Acclaim header
    names which are already in LAST FIRST order. Also removes OCR comma artifacts
    such as 'L,' where a period was misread as a comma (e.g., 'ROBYN L, PIERCE').
    """
    name = name.strip()
    if not name:
        return name
    # Strip OCR comma artifacts — PDF heir names never have commas in natural order
    name = name.replace(",", " ")
    name = " ".join(name.split())  # normalize extra spaces
    parts = name.split()
    if len(parts) < 2:
        return name
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


# ── Results page parser ────────────────────────────────────────────────

async def _parse_results_page(
    page: Page,
    county: str,
    since_date: str,
    doc_type: str,
    capture_item_ids: bool = False,
    seen_ids: Optional[dict] = None,
    until_date: Optional[str] = None,
) -> list[NoticeData]:
    """Extract NoticeData rows from the current Acclaim results page.

    When capture_item_ids=True (PDF verification mode), clicks each row to
    capture its Acclaim internal itemId from the GetToken request, storing it
    as '|acclaim_item_id:{id}' in notice.raw_text.
    """
    # Check for no results
    body = await page.inner_text("body")
    body_lower = body.lower()
    if any(p in body_lower for p in (
        "no records found", "no results", "0 records", "no documents",
        "no records to display",  # Kendo Grid default empty message
    )):
        logger.info("Acclaim %s: no results on this page", doc_type)
        return []

    rows = await page.locator("table tbody tr, table tr").all()
    if len(rows) <= 1:
        # Log a snippet of body text to diagnose blank/loading pages
        snippet = body[:300].replace("\n", " ").strip()
        logger.debug("Acclaim %s: no table rows found (body snippet: %s)", doc_type, snippet)
        return []

    # Detect header row to map column positions
    header_row = rows[0]
    header_cells = await header_row.locator("th, td").all()
    headers = [
        (await c.inner_text()).strip().lower()
        for c in header_cells
    ]

    # Map known column names → index
    col_instrument = _find_col(headers, ("instrument", "instr", "number", "doc #", "rec #"))
    col_date       = _find_col(headers, ("rec date", "record date", "date recorded", "recording date", "date"))
    col_doctype    = _find_col(headers, ("doc type", "document type", "type"))
    col_grantor    = _find_col(headers, ("grantor", "grantors", "seller", "borrower", "debtor"))
    col_grantee    = _find_col(headers, ("grantee", "grantees", "buyer", "lender", "bank"))
    col_legal      = _find_col(headers, ("legal", "description", "legal desc", "legal description"))
    col_parcel     = _find_col(headers, ("parcel", "parcel #", "parcel id", "account", "acct"))
    col_case       = _find_col(headers, ("case #", "case number", "case no", "caseno"))

    logger.debug(
        "Acclaim %s: headers=%s  col_map={instr:%s date:%s type:%s grantor:%s grantee:%s legal:%s parcel:%s}  rows=%d",
        doc_type, headers,
        col_instrument, col_date, col_doctype, col_grantor, col_grantee, col_legal, col_parcel,
        len(rows) - 1,
    )
    # Log the first data row for inspection
    if len(rows) > 1:
        first_cells = [(await c.inner_text()).strip() for c in await rows[1].locator("td").all()]
        logger.debug("Acclaim %s: first data row cells=%s", doc_type, first_cells)

    notice_type = _doc_type_to_notice_type(doc_type)
    notices: list[NoticeData] = []

    for row in rows[1:]:
        cells = await row.locator("td").all()
        if not cells:
            continue
        cell_texts = [(await c.inner_text()).strip() for c in cells]
        if not any(cell_texts):
            continue

        # Extract instrument number + link for source_url
        instrument = _get_cell(cell_texts, col_instrument, "")
        source_url = ""
        if col_instrument is not None and col_instrument < len(cells):
            link = cells[col_instrument].locator("a").first
            if await link.count():
                href = await link.get_attribute("href") or ""
                if href:
                    source_url = href if href.startswith("http") else f"{_BASE_URL}{href}"

        # Skip already-processed instruments BEFORE any OCR
        if instrument and seen_ids and instrument in seen_ids:
            logger.debug("Acclaim: skip cached instrument %s", instrument)
            continue

        # Recording date
        date_raw = _get_cell(cell_texts, col_date, "")
        date_added = _parse_date(date_raw)
        if date_added and date_added < since_date:
            continue  # Older than our window
        if date_added and until_date and date_added > until_date:
            continue  # Newer than our window (safety net; the date picker
            # should already exclude these, but the site doesn't always honor it)

        grantor_raw = _get_cell(cell_texts, col_grantor, "")
        if "\n" in grantor_raw:
            grantor_raw = grantor_raw.split("\n")[0].strip()
        grantee_raw = _get_cell(cell_texts, col_grantee, "")
        if "\n" in grantee_raw:
            grantee_raw = grantee_raw.split("\n")[0].strip()

        case_num = _get_cell(cell_texts, col_case, "")
        case_upper = case_num.upper()

        # For NOTICE records: grantor = lender who filed the notice,
        # grantee = property owner (homeowner). Drop non-foreclosure notices
        # (utilities, easements, etc.) by requiring grantor to look like a lender.
        # For DECREE records: use case number prefix to determine notice_type.
        doc_lower = doc_type.lower()
        if "notice" in doc_lower:
            if not _LENDER_RE.search(grantor_raw):
                logger.debug(
                    "Acclaim NOTICE skipped (grantor not a lender): %s", grantor_raw
                )
                continue
            owner_name = _to_court_format(grantee_raw)
            row_notice_type = notice_type  # "foreclosure"
        else:
            owner_name = _to_court_format(grantor_raw)
            # Reclassify DECREE by case number prefix:
            # CJ = Civil Judgment (foreclosure), PB/EP = Probate/Estate, DM/FD/DN = Domestic
            if case_upper.startswith(("PB", "EP")):
                row_notice_type = "probate"
            elif case_upper.startswith(("DM", "FD", "DN")):
                logger.debug("Acclaim DECREE skipped (domestic/divorce case): %s", case_num)
                continue
            elif case_upper.startswith("CJ") or not case_num:
                row_notice_type = "foreclosure"
            else:
                row_notice_type = notice_type

        # Parcel ID
        parcel_id = _get_cell(cell_texts, col_parcel, "")
        if not parcel_id:
            # Try regex on all cells
            for ct in cell_texts:
                m = _PARCEL_RE.search(ct)
                if m:
                    parcel_id = m.group(1)
                    break

        # Legal description / address
        legal = _get_cell(cell_texts, col_legal, "")
        address, city, zip_code = _extract_address_from_legal(legal)

        # If still no source URL, build from instrument number
        if not source_url and instrument:
            source_url = f"{_BASE_URL}/Document/DocumentDetail?instrumentNumber={instrument}"

        raw_text = " | ".join(cell_texts)
        _dt_tag = "DECREE" if "decree" in doc_type.lower() else "NOTICE" if "notice" in doc_type.lower() else doc_type.replace(" ", "_").upper()
        raw_text += f"|acclaim_doc_type:{_dt_tag}"

        # Capture Acclaim internal itemId by clicking this row (PDF verify mode).
        # Clicking fires GetToken?itemId=XXXX which we intercept to get the numeric ID.
        need_item_id = capture_item_ids and (
            "notice" in doc_lower or "decree" in doc_lower
        )
        if need_item_id:
            item_id_val = await _get_item_id_for_row(page, row)
            if item_id_val:
                raw_text += f"|acclaim_item_id:{item_id_val}"
                logger.debug("Acclaim: captured itemId=%s for instrument %s", item_id_val, instrument)

        notices.append(NoticeData(
            date_added  = date_added or datetime.now().strftime("%Y-%m-%d"),
            address     = address,
            city        = city,
            state       = "OK",
            zip         = zip_code,
            owner_name  = owner_name,
            notice_type = row_notice_type,
            county      = county,
            source_url  = source_url,
            parcel_id   = parcel_id,
            raw_text    = raw_text,
        ))

    return notices


# ── Detail page fallback ───────────────────────────────────────────────

async def _fetch_detail(page: Page, url: str) -> dict:
    """Navigate to an instrument detail page and extract parcel + address.

    Returns dict with keys: parcel_id, address, city, zip, owner_name.
    Used as a fallback when the results table doesn't include enough data.
    """
    result: dict = {}
    try:
        await page.goto(url)
        await page.wait_for_load_state("domcontentloaded")
        body = await page.inner_text("body")

        # Parcel ID
        m = _PARCEL_RE.search(body)
        if m:
            result["parcel_id"] = m.group(1)

        # Address from "Property Address:" or "Situs:" label
        for label in ("property address", "situs address", "situs:", "address:"):
            idx = body.lower().find(label)
            if idx != -1:
                chunk = body[idx + len(label):idx + len(label) + 200].strip().lstrip(": ")
                addr, city, zip_code = _extract_address_from_legal(chunk)
                if addr:
                    result["address"] = addr
                    result["city"]    = city
                    result["zip"]     = zip_code
                    break
    except Exception as e:
        logger.debug("Acclaim detail fetch failed for %s: %s", url, e)

    return result


# ── Helpers ────────────────────────────────────────────────────────────

def _find_col(headers: list[str], keywords: tuple[str, ...]) -> Optional[int]:
    """Return the index of the first header that contains any keyword."""
    for kw in keywords:
        for i, h in enumerate(headers):
            if kw in h:
                return i
    return None


def _get_cell(cells: list[str], idx: Optional[int], default: str = "") -> str:
    if idx is None or idx >= len(cells):
        return default
    return cells[idx].strip()


def _doc_type_to_notice_type(doc_type: str) -> str:
    doc_lower = doc_type.lower()
    for prefix, nt in _DOC_TYPE_TO_NOTICE_TYPE:
        if doc_lower.startswith(prefix) or prefix in doc_lower:
            return nt
    return "foreclosure"


def _parse_date(raw: str) -> str:
    """Parse M/D/YYYY or YYYY-MM-DD into YYYY-MM-DD. Returns '' on failure."""
    raw = raw.strip()
    if not raw:
        return ""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Try regex extraction
    m = _DATE_RE.search(raw)
    if m:
        try:
            return datetime.strptime(m.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def _to_mdy(date_str: str) -> str:
    """YYYY-MM-DD → M/D/YYYY without leading zeros."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{dt.month}/{dt.day}/{dt.year}"
    except ValueError:
        return date_str


def _extract_address_from_legal(text: str) -> tuple[str, str, str]:
    """Extract (street, city, zip) from a legal description or address string."""
    if not text:
        return ("", "Tulsa", "")

    # Try street address regex first
    m = _STREET_ADDR_RE.search(text)
    if m:
        street = m.group(1).strip().rstrip(",;.")
        city = "Tulsa"
        zip_code = ""
        # Look for city + zip after the street match
        remainder = text[m.end():m.end() + 100]
        cm = _CITY_STATE_ZIP_RE.search(remainder)
        if cm:
            city     = cm.group(1).title()
            zip_code = cm.group(2) or ""
        else:
            # Look for 5-digit zip in text
            zm = re.search(r"\b(7[34]\d{3})\b", text)
            if zm:
                zip_code = zm.group(1)
        return (street.title(), city, zip_code)

    return ("", "Tulsa", "")
