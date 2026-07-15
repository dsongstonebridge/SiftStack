"""Legacy.com obituary scraper — Tulsa World syndicated obituaries.

Legacy.com hosts Tulsa World's paid newspaper obituaries. Much richer
content than Echovita (full "survived by" sections with complete names),
but lower volume (~3/day vs Echovita's ~17-24/day) since these are paid
placements, not free funeral-home submissions.

Protected by Cloudflare Bot Management — requires a real headless browser
(Playwright) to pass the JS challenge. Plain HTTP requests get a 403
"Just a moment..." challenge page.

No date-range search exists on Legacy.com's Tulsa World pages — the
listing only shows "today's" current obituaries with no history. This
means it must run DAILY (not weekly) to avoid missing any, unlike the
Echovita scraper's wide 7-21 day searchable window.

Pipeline: scrape listing -> dedup against seen file -> fetch each detail
page -> parse survived-by section -> same NoticeData-compatible dict
format as tulsa_obituary_scraper.py.
"""

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

LEGACY_LISTING_URL = "https://www.legacy.com/us/obituaries/tulsaworld/today"
_SEEN_FILE = Path(__file__).resolve().parent.parent / "output" / "legacy_seen_urls.json"
_SEEN_PRUNE_DAYS = 30

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_AGE_RANGE_RE = re.compile(r"\b(\d{4})\s*-\s*(\d{4})\b")
_DATE_RE = re.compile(
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December"
    r"|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{1,2},?\s+\d{4}",
    re.IGNORECASE,
)

_SURVIVED_MARKERS = [
    "is survived by", "she is survived", "he is survived",
    "survived by", "survivors include", "leaves behind",
]
_END_MARKERS = [
    "graveside services", "funeral service", "memorial service",
    "visitation", "in lieu of", "to plant trees", "published by",
    "published in",
]


def _load_seen() -> dict[str, str]:
    if _SEEN_FILE.exists():
        try:
            return json.loads(_SEEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_seen(seen: dict[str, str]) -> None:
    cutoff = (datetime.now() - timedelta(days=_SEEN_PRUNE_DAYS)).strftime("%Y-%m-%d")
    pruned = {url: dt for url, dt in seen.items() if dt >= cutoff}
    _SEEN_FILE.write_text(json.dumps(pruned, indent=2), encoding="utf-8")


async def scrape_legacy_obituaries(headless: bool = True) -> list[dict]:
    """Scrape today's Tulsa World obituaries from Legacy.com.

    Returns list of dicts with keys: name, date_of_death, age, detail_url,
    obituary_text, funeral_home, survived_by_raw. Same shape as
    tulsa_obituary_scraper.scrape_obituaries() output for pipeline compat.
    """
    from playwright.async_api import async_playwright

    seen = _load_seen()
    results: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            page = await browser.new_page(user_agent=_USER_AGENT)
            logger.info("Legacy.com: fetching listing page")
            await page.goto(LEGACY_LISTING_URL, timeout=30000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)

            content = await page.content()
            if "Just a moment" in content:
                logger.error("Legacy.com: blocked by Cloudflare challenge")
                return []

            links = await page.eval_on_selector_all(
                "a",
                "els => els.map(e => ({href: e.href, text: e.innerText.trim()}))"
                ".filter(l => l.href.includes('/name/') && l.href.includes('obituary'))",
            )
            unique_links = {}
            for link in links:
                href = link["href"].split("#")[0]
                if href not in unique_links:
                    unique_links[href] = link["text"]

            logger.info("Legacy.com: %d obituary links on listing page", len(unique_links))

            new_links = {url: text for url, text in unique_links.items() if url not in seen}
            skipped = len(unique_links) - len(new_links)
            if skipped:
                logger.info("Legacy.com: skipped %d already-processed obituaries", skipped)

            today = datetime.now().strftime("%Y-%m-%d")
            for url, link_text in new_links.items():
                logger.debug("Legacy.com: fetching detail %s", url)
                detail_page = await browser.new_page(user_agent=_USER_AGENT)
                try:
                    await detail_page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    await detail_page.wait_for_timeout(4000)
                    detail_content = await detail_page.content()
                    if "Just a moment" in detail_content:
                        logger.warning("Legacy.com: blocked on detail page %s", url)
                        await detail_page.close()
                        continue

                    body_text = await detail_page.inner_text("body")
                    obit = _parse_legacy_page(body_text, url)
                    if obit:
                        results.append(obit)
                        seen[url] = today
                except Exception as e:
                    logger.warning("Legacy.com: detail fetch error for %s: %s", url, e)
                finally:
                    await detail_page.close()
                await page.wait_for_timeout(3000)

        finally:
            await browser.close()

    _save_seen(seen)
    logger.info("Legacy.com: %d new obituaries scraped", len(results))
    return results


_OBIT_HEADING_RE = re.compile(r"^(.+?)\s+Obituary$")


def _parse_legacy_page(body_text: str, url: str) -> Optional[dict]:
    """Parse a Legacy.com obituary detail page's visible text.

    Page structure (consistent across samples):
      ...nav...
      {Name}
      {birth}-{death range}
      ...
      FUNERAL HOME          <- only present if funeral home listed
      {Funeral Home Name}
      ...
      {Name} Obituary       <- heading marks start of narrative
      {narrative paragraph(s)}
      Published in {paper} on {date}.
    """
    lines = [l.strip() for l in body_text.split("\n") if l.strip()]
    if not lines:
        return None

    # Name + narrative start: find "{Name} Obituary" heading line
    name = ""
    heading_idx = -1
    for i, line in enumerate(lines):
        m = _OBIT_HEADING_RE.match(line)
        if m:
            name = m.group(1).strip()
            heading_idx = i
            break
    if not name:
        # Fallback: name is usually the line right before the birth-year-range line
        for i, line in enumerate(lines):
            if _AGE_RANGE_RE.search(line) and i > 0:
                name = lines[i - 1]
                break
    if not name:
        logger.debug("Legacy.com: could not extract name from %s", url)
        return None

    age_match = _AGE_RANGE_RE.search(body_text[:300])
    birth_year = age_match.group(1) if age_match else ""

    # Funeral home — line after "FUNERAL HOME" marker
    funeral_home = ""
    for i, line in enumerate(lines):
        if line.strip().upper() == "FUNERAL HOME" and i + 1 < len(lines):
            funeral_home = lines[i + 1]
            break

    # Obituary narrative — lines after the heading, until "Published in/by"
    obit_text = ""
    if heading_idx >= 0:
        narrative_lines = []
        for line in lines[heading_idx + 1:]:
            if line.lower().startswith(("published in", "published by")):
                break
            narrative_lines.append(line)
        obit_text = "\n".join(narrative_lines)

    # Extract date of death from narrative (e.g. "passed away ... June 21, 2026")
    dod = ""
    passed_idx = obit_text.lower().find("passed away")
    if passed_idx >= 0:
        date_m = _DATE_RE.search(obit_text[passed_idx:passed_idx + 100])
        if date_m:
            dod = _parse_date_str(date_m.group(0))

    # Extract survived-by section
    survived_by = ""
    lower_text = obit_text.lower()
    for marker in _SURVIVED_MARKERS:
        idx = lower_text.find(marker)
        if idx >= 0:
            raw = obit_text[idx:]
            end_idx = len(raw)
            for em in _END_MARKERS:
                ei = raw.lower().find(em)
                if 0 < ei < end_idx:
                    end_idx = ei
            survived_by = raw[:end_idx].strip()
            break

    return {
        "name": name,
        "date_of_death": dod,
        "birth_year": birth_year,
        "detail_url": url,
        "obituary_text": obit_text,
        "funeral_home": funeral_home,
        "survived_by_raw": survived_by,
        "source": "legacy.com",
    }


def _parse_date_str(date_str: str) -> str:
    date_str = date_str.replace(".", "").strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    obits = asyncio.run(scrape_legacy_obituaries())
    print("\n%d new obituaries found:" % len(obits))
    for o in obits:
        print("  %s (DOD: %s)" % (o["name"], o.get("date_of_death", "?")))
        if o.get("survived_by_raw"):
            print("    Survived by: %s" % o["survived_by_raw"][:200])
        if o.get("funeral_home"):
            print("    Funeral home: %s" % o["funeral_home"])
