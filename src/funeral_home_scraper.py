"""Direct funeral home website obituary scraper.

Scrapes Tulsa-area funeral home websites directly instead of relying on
aggregators (Echovita) or the gated newspaper syndicate (Legacy.com/Tulsa
World). Funeral homes are the ORIGINAL source both of those pull from —
content here is consistently richer (full survived-by names) and volume
per home is higher than Tulsa World's combined daily total, with zero
bot protection (no Cloudflare, no TollBit).

Two platforms cover the majority of Tulsa County funeral homes:
  - FuneralOne: listing renders obituary cards directly; detail page
    needs the "OBITUARY" tab clicked (defaults to Tribute Wall/comments).
  - Tukios: listing + detail page render obituary text directly, no
    tab interaction needed.

Both platforms are JS-rendered (React/SPA) — requires Playwright with a
~6-8s wait for content to hydrate. Plain HTTP requests return an empty
shell.

Pipeline: scrape each funeral home's listing -> dedup against seen file
-> fetch each new detail page -> extract survived-by section -> same
dict shape as tulsa_obituary_scraper.py / legacy_obituary_scraper.py for
pipeline compatibility.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SEEN_FILE = Path(__file__).resolve().parent.parent / "output" / "funeral_home_seen_urls.json"
_SEEN_PRUNE_DAYS = 30

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Known Tulsa-area funeral homes by platform. Add more here as identified —
# see feedback-funeral-home-platforms memory for the full county list and
# which ones remain unclassified (Ninde, Stanley's, Dyer were blocked/custom
# during initial reconnaissance).
FUNERAL_HOMES = [
    {"name": "Floral Haven Funeral Home", "platform": "funeralone",
     "base_url": "https://www.floralhaven.com", "listing_path": "/obituaries/"},
    {"name": "Schaudt Funeral Service", "platform": "funeralone",
     "base_url": "https://www.schaudtfuneralservice.com", "listing_path": "/obituaries"},
    {"name": "Hayhurst Funeral Home", "platform": "funeralone",
     "base_url": "https://www.hayhurstfuneralhome.com", "listing_path": "/obituaries/"},
    {"name": "Bixby Funeral Service", "platform": "funeralone",
     "base_url": "https://www.bixbyfuneralservice.com", "listing_path": "/obituaries/"},
    {"name": "Moore Funeral Home", "platform": "tukios",
     "base_url": "https://www.moorefuneral.com", "listing_path": "/obituaries"},
    {"name": "Fitzgerald Funeral Service", "platform": "tukios",
     "base_url": "https://www.fitzgeraldfuneralservice.com", "listing_path": "/obituaries"},
]

_DATE_RE = re.compile(
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December"
    r"|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}",
    re.IGNORECASE,
)

_SURVIVED_MARKERS = [
    "is survived by", "she is survived", "he is survived",
    "survived by", "survivors include", "leaves behind",
    "lovingly remembered by", "is lovingly remembered",
]
_END_MARKERS = [
    "preceded in death", "preceded by", "predeceased",
    "graveside services", "funeral service", "memorial service",
    "visitation will be", "services will be", "in lieu of",
    "what's your fondest memory", "share a memory", "guestbook",
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


def _extract_survived_by(text: str) -> str:
    """Pull the survived-by section out of full obituary text."""
    lower_text = text.lower()
    for marker in _SURVIVED_MARKERS:
        idx = lower_text.find(marker)
        if idx >= 0:
            raw = text[idx:]
            end_idx = len(raw)
            for em in _END_MARKERS:
                ei = raw.lower().find(em)
                if 0 < ei < end_idx:
                    end_idx = ei
            return raw[:end_idx].strip()
    return ""


def _clean_text(text: str) -> str:
    """Strip non-ASCII artifacts (smart quotes etc.) that crash Windows cp1252 logging."""
    return text.encode("ascii", "ignore").decode("ascii")


def _extract_obit_links(hrefs: list[str], base_url: str) -> set[str]:
    """Filter raw href list to individual obituary detail page URLs."""
    obit_links = set()
    for href in hrefs:
        if not href.startswith(base_url):
            continue
        if "/obituaries/" not in href:
            continue
        if any(x in href for x in (
            "obituary-notification", "obituary-listing", "privacy",
            "obituaries/#", "obituaries/?",
        )):
            continue
        suffix = href.split("/obituaries/", 1)[1].strip("/")
        if suffix and "/" not in suffix:
            obit_links.add(href)
    return obit_links


async def scrape_funeral_home(browser, home: dict, seen: dict[str, str]) -> list[dict]:
    """Scrape one funeral home's obituary listing + new detail pages."""
    results: list[dict] = []
    listing_url = home["base_url"] + home["listing_path"]
    platform = home["platform"]

    page = await browser.new_page(user_agent=_USER_AGENT)
    try:
        await page.goto(listing_url, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(8000)

        content = await page.content()
        if "Just a moment" in content or "403 Forbidden" in content:
            logger.warning("%s: blocked on listing page", home["name"])
            return []

        links = await page.eval_on_selector_all("a", "els => els.map(e => e.href)")
        obit_links = _extract_obit_links(links, home["base_url"])

        # If listing returned nothing, retry up to 2 times with 30s cooldown.
        # Hayhurst (and possibly others) use soft bot-detection that returns an
        # empty listing page after rapid successive visits — a wait+reload usually
        # clears it without needing a fresh context.
        for attempt in range(2):
            if obit_links:
                break
            logger.warning(
                "%s: no obituary links on listing (attempt %d/2), waiting 30s then retrying...",
                home["name"], attempt + 1,
            )
            await page.wait_for_timeout(30000)
            await page.reload(timeout=30000, wait_until="domcontentloaded")
            await page.wait_for_timeout(10000)
            links = await page.eval_on_selector_all("a", "els => els.map(e => e.href)")
            obit_links = _extract_obit_links(links, home["base_url"])

        logger.info("%s: %d obituary links on listing page", home["name"], len(obit_links))

        new_links = [u for u in obit_links if u not in seen]
        skipped = len(obit_links) - len(new_links)
        if skipped:
            logger.info("%s: skipped %d already-processed", home["name"], skipped)

        today = datetime.now().strftime("%Y-%m-%d")
        for url in new_links:
            detail_page = await browser.new_page(user_agent=_USER_AGENT)
            try:
                await detail_page.goto(url, timeout=30000, wait_until="domcontentloaded")
                await detail_page.wait_for_timeout(6000)

                if platform == "funeralone":
                    # Different FuneralOne template variants use different tab
                    # labels to reveal full obituary text (default view is
                    # often "Tribute Wall" / truncated preview). Try known
                    # variants in order; harmless if a given site has none.
                    for tab_text in ("text=OBITUARY", "text=Obituary & Service"):
                        try:
                            await detail_page.click(tab_text, timeout=4000)
                            await detail_page.wait_for_timeout(2500)
                            break
                        except Exception:
                            continue

                body_text = await detail_page.inner_text("body")
                obit = _parse_funeral_home_page(body_text, url, home["name"])
                if obit:
                    results.append(obit)
                    seen[url] = today
            except Exception as e:
                logger.warning("%s: detail fetch error for %s: %s", home["name"], url, e)
            finally:
                await detail_page.close()
            await page.wait_for_timeout(2500)

    finally:
        await page.close()

    return results


def _name_from_slug(url: str) -> str:
    """Derive a display name from the obituary URL slug.

    e.g. /obituaries/darla-trout -> "Darla Trout"
         /obituaries/sylvia-davis-21 -> "Sylvia Davis"  (trailing id stripped)
    """
    slug = url.rstrip("/").rsplit("/obituaries/", 1)[-1]
    slug = re.sub(r"-\d+$", "", slug)  # strip trailing disambiguation number
    parts = [p.capitalize() for p in slug.split("-") if p]
    return " ".join(parts)


def _parse_funeral_home_page(body_text: str, url: str, funeral_home: str) -> Optional[dict]:
    text = _clean_text(body_text)

    name = _name_from_slug(url)
    if not name:
        return None

    # Narrative: find the first occurrence of the name as connected prose
    # (the obituary heading "{Name}'s Obituary" or "{Name}, age..." etc),
    # search using just the first name + last name as loose anchors since
    # exact casing/punctuation varies by site.
    name_parts = name.split()
    anchor = name_parts[0] if name_parts else name
    start_idx = text.find(anchor)
    narrative = text[start_idx:start_idx + 4000] if start_idx >= 0 else text

    dod = ""
    # Structured "DEATH DATE:" field (FuneralOne) is more reliable than prose
    death_field_m = re.search(r"DEATH DATE:\s*\n?\s*([A-Za-z]+\.?\s+\d{1,2},?\s+\d{4})", text)
    if death_field_m:
        dod = _parse_date_str(death_field_m.group(1))
    if not dod:
        death_markers = ["passed away", "graduated to heaven", "entered into eternal rest",
                          "passed from this life", "died"]
        for marker in death_markers:
            idx = narrative.lower().find(marker)
            if idx >= 0:
                date_m = _DATE_RE.search(narrative[idx:idx + 100])
                if date_m:
                    dod = _parse_date_str(date_m.group(0))
                    break

    survived_by = _extract_survived_by(narrative)

    return {
        "name": name,
        "date_of_death": dod,
        "detail_url": url,
        "obituary_text": narrative,
        "funeral_home": funeral_home,
        "survived_by_raw": survived_by,
        "source": "funeral_home_direct",
    }


def _parse_date_str(date_str: str) -> str:
    date_str = re.sub(r"(st|nd|rd|th)\b", "", date_str).replace(".", "").strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


async def scrape_all_funeral_homes(headless: bool = True) -> list[dict]:
    """Scrape all configured funeral homes for new obituaries."""
    from playwright.async_api import async_playwright

    seen = _load_seen()
    all_results: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            for i, home in enumerate(FUNERAL_HOMES):
                if i > 0:
                    # Brief inter-home pause to avoid looking like a rapid crawler.
                    # Keeps us below the soft-block threshold that hit Hayhurst.
                    await asyncio.sleep(8)
                try:
                    results = await scrape_funeral_home(browser, home, seen)
                    all_results.extend(results)
                    logger.info("%s: %d new obituaries", home["name"], len(results))
                except Exception as e:
                    logger.error("%s: scrape failed: %s", home["name"], e)
        finally:
            await browser.close()

    _save_seen(seen)
    logger.info("Funeral homes total: %d new obituaries across %d homes",
                len(all_results), len(FUNERAL_HOMES))
    return all_results


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    obits = asyncio.run(scrape_all_funeral_homes())
    print("\n%d new obituaries found:" % len(obits))
    for o in obits:
        print("  [%s] %s (DOD: %s)" % (o["funeral_home"], o["name"], o.get("date_of_death", "?")))
        if o.get("survived_by_raw"):
            print("    Survived by: %s" % _clean_text(o["survived_by_raw"][:200]))
