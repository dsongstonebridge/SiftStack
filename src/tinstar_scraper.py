"""TinStar sheriff sale property list scraper (Tulsa County).

TinStar is the platform Tulsa County Sheriff uses to publish scheduled
foreclosure auction properties. Updated every Friday. No login required
for public report viewing.

Reports URL: https://www.tinstar.io/reports
Events URL:  https://www.tinstar.io/events  (bidder registration only)

If TinStar requires a login you will see a warning and an empty result.
Set TINSTAR_EMAIL / TINSTAR_PASSWORD in .env to enable authenticated access.
"""

import logging
import re
from datetime import datetime
from typing import Optional

from playwright.async_api import Page, TimeoutError as PwTimeout, async_playwright

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

TINSTAR_REPORTS_URL = "https://www.tinstar.io/reports"

# Street suffix pattern used to detect addresses in freeform text
_SUFFIX_PAT = (
    r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|"
    r"Boulevard|Blvd|Way|Court|Ct|Place|Pl|Circle|Cir|"
    r"Highway|Hwy|Terrace|Ter|Loop|Trail|Trl|Parkway|Pkwy)\b"
)
_ADDRESS_RE = re.compile(
    r"(\d{1,5}\s+(?:[NSEW]\.?\s+)?(?:[\w'-]+\s+){1,5}" + _SUFFIX_PAT + r"\.?)",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
_CASE_RE = re.compile(r"\bCJ-\d{4}-\d+\b|\b\d{4}-\d+\b")


async def scrape_tinstar(
    county: str = "Tulsa",
    since_date: Optional[str] = None,
    headless: bool = True,
    email: str = "",
    password: str = "",
) -> list[NoticeData]:
    """Fetch Tulsa County sheriff sale listings from TinStar."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()
        page.set_default_timeout(30_000)

        try:
            # Attempt login if credentials are available
            if email and password:
                await _try_login(page, email, password)

            results = await _scrape_reports(page, county, since_date)
        except Exception:
            logger.exception("TinStar scrape error")
            results = []
        finally:
            await browser.close()

    logger.info("TinStar: %d sheriff sale properties found in %s County", len(results), county)
    return results


# ── Login (optional) ──────────────────────────────────────────────────


async def _try_login(page: Page, email: str, password: str) -> bool:
    """Attempt to log in to TinStar if credentials are provided."""
    try:
        await page.goto("https://www.tinstar.io/login")
        await page.wait_for_load_state("domcontentloaded")

        email_input = page.locator("input[type='email'], input[name*='email' i]").first
        pass_input = page.locator("input[type='password']").first

        if await email_input.count() and await pass_input.count():
            await email_input.fill(email)
            await pass_input.fill(password)
            await page.locator("button[type='submit'], input[type='submit']").first.click()
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except PwTimeout:
                await page.wait_for_load_state("domcontentloaded")
            logger.info("TinStar: login attempted")
            return True
    except Exception as e:
        logger.debug("TinStar login attempt failed: %s", e)
    return False


# ── Reports page scraping ─────────────────────────────────────────────


async def _scrape_reports(
    page: Page,
    county: str,
    since_date: Optional[str],
) -> list[NoticeData]:
    await page.goto(TINSTAR_REPORTS_URL)

    # TinStar is a React SPA — wait for JS to render content
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except PwTimeout:
        await page.wait_for_load_state("domcontentloaded")

    # Wait for the main content container to appear
    try:
        await page.wait_for_selector(
            "table, [class*='property'], [class*='report'], [class*='listing'], "
            "[class*='auction'], [class*='case'], main",
            timeout=10_000,
        )
    except PwTimeout:
        pass  # Proceed with what's available

    body_text = await page.inner_text("body")
    body_lower = body_text.lower()

    # ── Detect login wall ──────────────────────────────────────────
    if any(w in body_lower for w in ("sign in", "log in to", "login required", "create account", "register to")):
        logger.warning(
            "TinStar appears to require a login to view property reports.\n"
            "  1. Visit https://www.tinstar.io and create a free account.\n"
            "  2. Add to your .env:  TINSTAR_EMAIL=you@email.com\n"
            "                        TINSTAR_PASSWORD=yourpassword\n"
            "  TinStar will then be scraped automatically on future runs."
        )
        return []

    if len(body_text.strip()) < 100:
        logger.warning(
            "TinStar returned near-empty page — SPA may not have rendered. "
            "Try running with --headless=false to inspect the browser."
        )
        logger.debug("TinStar raw HTML (first 2000):\n%s", (await page.content())[:2000])
        return []

    logger.debug("TinStar page text (first 800):\n%s", body_text[:800])

    # ── Try HTML table first (most structured) ───────────────────
    rows = await page.locator("table tr").all()
    if len(rows) > 1:
        logger.debug("TinStar: parsing HTML table (%d rows)", len(rows))
        return await _parse_table(rows, county, since_date)

    # ── Try card / list item layout ───────────────────────────────
    cards = await page.locator(
        "[class*='card'], [class*='item'], [class*='row'], [class*='property'], [class*='listing']"
    ).all()
    if cards:
        logger.debug("TinStar: parsing %d card elements", len(cards))
        return await _parse_cards(cards, county, since_date)

    # ── Fallback: regex address extraction from raw text ──────────
    logger.warning(
        "TinStar: no recognisable HTML table or card layout found — "
        "falling back to regex address extraction."
    )
    return _extract_addresses_from_text(body_text, county, since_date)


# ── Parsers ───────────────────────────────────────────────────────────


async def _parse_table(
    rows,
    county: str,
    since_date: Optional[str],
) -> list[NoticeData]:
    notices: list[NoticeData] = []
    for row in rows:
        cells = await row.locator("td").all()
        if not cells:
            continue
        texts = [(await c.inner_text()).strip() for c in cells]
        notice = _cells_to_notice(texts, county, since_date)
        if notice:
            notices.append(notice)
    return notices


async def _parse_cards(
    cards,
    county: str,
    since_date: Optional[str],
) -> list[NoticeData]:
    notices: list[NoticeData] = []
    seen: set[str] = set()
    for card in cards:
        text = (await card.inner_text()).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        notice = _cells_to_notice([text], county, since_date)
        if notice:
            notices.append(notice)
    return notices


def _extract_addresses_from_text(
    body_text: str,
    county: str,
    since_date: Optional[str],
) -> list[NoticeData]:
    notices: list[NoticeData] = []
    seen: set[str] = set()
    for m in _ADDRESS_RE.finditer(body_text):
        address = m.group(1).strip()
        if address in seen:
            continue
        seen.add(address)
        # Extract a short context window around the address for metadata
        start = max(0, m.start() - 150)
        end = min(len(body_text), m.end() + 150)
        context = body_text[start:end]
        notice = NoticeData(
            date_added=datetime.now().strftime("%Y-%m-%d"),
            address=address,
            city="Tulsa",
            state="OK",
            notice_type="foreclosure",
            county=county,
            source_url=TINSTAR_REPORTS_URL,
            raw_text=context,
        )
        notices.append(notice)
    if not notices:
        logger.warning(
            "TinStar text-fallback: no addresses found. "
            "The page structure likely requires manual inspection. "
            "Run with -v to see the raw page content."
        )
    else:
        logger.info("TinStar text-fallback: extracted %d addresses", len(notices))
    return notices


_CITY_STATE_RE = re.compile(
    r"^(.+?),\s*([A-Z]{2})\s*(\d{5}(?:-\d{4})?)?",
    re.IGNORECASE,
)


def _cells_to_notice(
    cells: list[str],
    county: str,
    since_date: Optional[str],
) -> Optional[NoticeData]:
    """Build a NoticeData from one table row or card's text cells."""
    joined = " ".join(cells)
    if not joined.strip():
        return None

    address = city = zip_code = auction_date = case_num = defendant = plaintiff = ""

    for raw_cell in cells:
        cell = raw_cell.strip()
        if not cell:
            continue

        # Case number: CJ-YYYY-NNN pattern
        if not case_num:
            m = _CASE_RE.search(cell)
            if m:
                case_num = m.group(0)
                continue

        # Date: M/D/YYYY
        if not auction_date:
            m = _DATE_RE.search(cell)
            if m:
                try:
                    dt = datetime.strptime(m.group(1), "%m/%d/%Y")
                    auction_date = dt.strftime("%Y-%m-%d")
                except ValueError:
                    pass
                continue

        # Address cell — may be multi-line: "123 MAIN ST\nTULSA, OK 74103"
        # or inline: "123 MAIN ST, TULSA, OK 74103"
        if not address:
            lines = [l.strip() for l in re.split(r"[\n\r]+", cell) if l.strip()]
            for i, line in enumerate(lines):
                m = _ADDRESS_RE.search(line)
                if m:
                    address = m.group(1).strip()
                    # Check remainder of same line first (inline city/state/zip)
                    remainder = line[m.end():].strip().lstrip(",").strip()
                    cs_m = _CITY_STATE_RE.match(remainder) if remainder else None
                    # Fall back to next line
                    if not cs_m and i + 1 < len(lines):
                        cs_m = _CITY_STATE_RE.match(lines[i + 1])
                    if cs_m:
                        city = cs_m.group(1).strip().title()
                        zip_code = cs_m.group(3) or ""
                    # Last resort: scan full cell text for any 5-digit zip
                    if not zip_code:
                        zip_m = re.search(r"\b(\d{5})\b", cell)
                        if zip_m:
                            zip_code = zip_m.group(1)
                    break
            if address:
                continue

        # Dollar amounts — skip
        if re.match(r"^\$[\d,]+", cell) or cell.upper() in (
            "RECALLED", "NO SALE NO BID", "NO BID", "POSTPONED"
        ):
            continue

        # Phone numbers — skip
        if re.match(r"^\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}$", cell):
            continue

        # Two-word ALL-CAPS name cells are likely plaintiff/defendant
        # TinStar columns: Plaintiff (2), Defendant (3)
        if not plaintiff and cell.isupper() and len(cell) > 2:
            plaintiff = cell.title()
        elif not defendant and len(cell) > 2:
            defendant = cell.title()

    if not address and not case_num:
        return None

    effective_date = auction_date or datetime.now().strftime("%Y-%m-%d")
    if since_date and effective_date < since_date:
        return None

    # Use defendant (mortgagor = property owner) as owner, plaintiff as note
    owner = defendant or plaintiff
    source_url = f"{TINSTAR_REPORTS_URL}#{case_num}" if case_num else TINSTAR_REPORTS_URL

    return NoticeData(
        date_added=datetime.now().strftime("%Y-%m-%d"),
        auction_date=auction_date,
        address=address,
        city=city or "Tulsa",
        state="OK",
        zip=zip_code,
        owner_name=owner,
        notice_type="foreclosure",
        county=county,
        source_url=source_url,
        raw_text=joined,
    )
