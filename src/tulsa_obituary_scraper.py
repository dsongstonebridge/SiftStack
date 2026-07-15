"""Tulsa County obituary scraper — Echovita.com.

Scrapes recent Tulsa-area obituaries for pre-probate lead generation.
Obituaries publish 2-5 days after death; probate filings take 2-8 weeks.
This gives a first-to-market window of 2-6 weeks on every probate lead.

Pipeline: scrape obits -> Assessor lookup (owns property?) -> LLM parse heirs
         -> skip trace decision maker -> DataSift upload as "Pre-Probate"
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

ECHOVITA_BASE = "https://www.echovita.com"
ECHOVITA_TULSA_URL = "https://www.echovita.com/us/obituaries/ok/tulsa"
_SEEN_FILE = Path(__file__).resolve().parent.parent / "output" / "obit_seen_urls.json"
_SEEN_PRUNE_DAYS = 30


def _load_seen() -> dict[str, str]:
    """Load seen obituary URLs with their processing date."""
    if _SEEN_FILE.exists():
        try:
            return json.loads(_SEEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_seen(seen: dict[str, str]) -> None:
    """Save seen URLs, pruning entries older than _SEEN_PRUNE_DAYS."""
    cutoff = (datetime.now() - timedelta(days=_SEEN_PRUNE_DAYS)).strftime("%Y-%m-%d")
    pruned = {url: dt for url, dt in seen.items() if dt >= cutoff}
    _SEEN_FILE.write_text(json.dumps(pruned, indent=2), encoding="utf-8")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

_DATE_FORMATS = [
    "%B %d, %Y",      # June 23, 2026
    "%b %d, %Y",      # Jun 23, 2026
    "%m/%d/%Y",        # 06/23/2026
]

_AGE_RE = re.compile(r"\((\d+)\s+years?\s+old\)", re.IGNORECASE)
_DATE_RE = re.compile(
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December"
    r"|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"\s+\d{1,2},\s+\d{4}",
    re.IGNORECASE,
)
_DOD_RANGE_RE = re.compile(
    r"(\w+ \d{1,2}, \d{4})\s*[-–—]\s*(\w+ \d{1,2}, \d{4})",
)


def _parse_date(date_str: str) -> Optional[str]:
    """Parse a date string into YYYY-MM-DD format."""
    date_str = date_str.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def scrape_obituaries(
    max_pages: int = 3,
    max_age_days: int = 7,
    min_age_days: int = 0,
    delay: float = 3.0,
) -> list[dict]:
    """Scrape recent Tulsa obituaries from Echovita.

    Args:
        max_pages: Maximum listing pages to fetch (20 obits per page).
        max_age_days: Only return obituaries with DOD within this many days.
        min_age_days: Skip obituaries newer than this many days (0 = no minimum).
        delay: Seconds between HTTP requests.

    Returns:
        List of dicts with keys: name, date_of_death, age, detail_url,
        obituary_text, funeral_home, survived_by_raw.
    """
    cutoff = datetime.now() - timedelta(days=max_age_days)
    fresh_cutoff = datetime.now() - timedelta(days=min_age_days) if min_age_days else None
    all_obits: list[dict] = []
    seen_urls: set[str] = set()

    for page_num in range(1, max_pages + 1):
        url = ECHOVITA_TULSA_URL
        if page_num > 1:
            url += "?page=%d" % page_num

        logger.info("Echovita: fetching page %d", page_num)
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=15)
            if resp.status_code != 200:
                logger.warning("Echovita: HTTP %d on page %d", resp.status_code, page_num)
                break
        except Exception as e:
            logger.warning("Echovita: fetch error on page %d: %s", page_num, e)
            break

        soup = BeautifulSoup(resp.text, "lxml")

        # Find obituary name links (class="text-name-obit-in-list")
        name_links = soup.select("a.text-name-obit-in-list")

        if not name_links:
            logger.info("Echovita: no obituary links on page %d, stopping", page_num)
            break

        page_count = 0
        for link in name_links:
            href = link.get("href", "")
            if not href:
                continue

            full_url = href if href.startswith("http") else ECHOVITA_BASE + href
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            name = link.get_text(strip=True)
            if not name or len(name) < 3:
                continue

            # Date and age are in the parent div alongside the name link
            parent = link.parent
            while parent and parent.name != "div":
                parent = parent.parent
            parent_text = parent.get_text(" ", strip=True) if parent else ""

            date_match = _DATE_RE.search(parent_text)
            age_match = _AGE_RE.search(parent_text)

            dod_str = date_match.group(0) if date_match else ""
            age = int(age_match.group(1)) if age_match else None

            page_count += 1
            all_obits.append({
                "name": name,
                "date_of_death_raw": dod_str,
                "age": age,
                "detail_url": full_url,
            })

        logger.info("Echovita: page %d - %d obituaries found", page_num, page_count)

        if page_num < max_pages:
            time.sleep(delay)

    logger.info("Echovita: %d total obituaries from %d pages", len(all_obits), min(max_pages, page_num))

    # Filter by date
    filtered = []
    skipped_fresh = 0
    for obit in all_obits:
        dod = _parse_date(obit["date_of_death_raw"])
        if dod:
            obit["date_of_death"] = dod
            dod_dt = datetime.strptime(dod, "%Y-%m-%d")
            if dod_dt < cutoff:
                logger.debug("Echovita: skipping %s (DOD %s older than %d days)",
                             obit["name"], dod, max_age_days)
                continue
            if fresh_cutoff and dod_dt > fresh_cutoff:
                logger.debug("Echovita: skipping %s (DOD %s too fresh, < %d days)",
                             obit["name"], dod, min_age_days)
                skipped_fresh += 1
                continue
            filtered.append(obit)
        else:
            obit["date_of_death"] = ""
            filtered.append(obit)

    window = "%d-%d day" % (min_age_days, max_age_days) if min_age_days else "%d-day" % max_age_days
    logger.info("Echovita: %d obituaries in %s window (skipped %d too fresh)",
                len(filtered), window, skipped_fresh)

    # Skip obituaries already processed in a prior run
    seen = _load_seen()
    before_seen = len(filtered)
    filtered = [o for o in filtered if o["detail_url"] not in seen]
    skipped_seen = before_seen - len(filtered)
    if skipped_seen:
        logger.info("Echovita: skipped %d already-processed obituaries", skipped_seen)

    # Fetch detail pages for full obituary text
    for i, obit in enumerate(filtered):
        detail_url = obit["detail_url"]
        logger.debug("Echovita: fetching detail %d/%d: %s", i + 1, len(filtered), obit["name"])

        try:
            resp = requests.get(detail_url, headers=_HEADERS, timeout=15)
            if resp.status_code == 200:
                _parse_detail_page(resp.text, obit)
            else:
                logger.debug("Echovita: detail HTTP %d for %s", resp.status_code, obit["name"])
                obit["obituary_text"] = ""
                obit["funeral_home"] = ""
                obit["survived_by_raw"] = ""
        except Exception as e:
            logger.debug("Echovita: detail fetch error for %s: %s", obit["name"], e)
            obit["obituary_text"] = ""
            obit["funeral_home"] = ""
            obit["survived_by_raw"] = ""

        if i + 1 < len(filtered):
            time.sleep(delay)

    # Filter out template obituaries (no real family data)
    real_obits = [o for o in filtered if not o.get("is_template")]
    template_count = len(filtered) - len(real_obits)
    if template_count:
        logger.info("Echovita: removed %d template obituaries (%d real remain)",
                     template_count, len(real_obits))

    # Mark as seen:
    #   - Real obits WITH survived-by data (done, don't re-fetch)
    #   - Empty templates (Echovita placeholders, will never get real content)
    # Re-check next run:
    #   - Real obits WITHOUT survived-by data (funeral home may update)
    today = datetime.now().strftime("%Y-%m-%d")
    seen_count = 0
    recheck_count = 0
    for o in filtered:
        if o.get("is_template"):
            seen[o["detail_url"]] = today
            seen_count += 1
        elif o.get("survived_by_raw", "").strip():
            seen[o["detail_url"]] = today
            seen_count += 1
        else:
            recheck_count += 1
    _save_seen(seen)
    logger.info("Echovita: marked %d URLs as seen (%d real obits without heirs will be re-checked)",
                seen_count, recheck_count)

    return real_obits


def _parse_detail_page(html: str, obit: dict) -> None:
    """Extract obituary text, funeral home, and survived-by info from detail page."""
    soup = BeautifulSoup(html, "lxml")

    # Full obituary text from paragraphs
    paragraphs = soup.find_all("p")
    text_parts = []
    for p in paragraphs:
        text = p.get_text(" ", strip=True)
        if len(text) > 30:
            text_parts.append(text)
    obit["obituary_text"] = "\n".join(text_parts)

    # Funeral home
    fh_link = soup.select_one('a[href*="/funeral-homes/"]')
    obit["funeral_home"] = fh_link.get_text(strip=True) if fh_link else ""

    # Birth-death date range from detail page (more reliable)
    for text_block in text_parts:
        range_match = _DOD_RANGE_RE.search(text_block)
        if range_match:
            dod = _parse_date(range_match.group(2))
            if dod:
                obit["date_of_death"] = dod
                dob = _parse_date(range_match.group(1))
                if dob:
                    obit["date_of_birth"] = dob
            break

    # Detect auto-generated template obituaries (no real family data).
    # Echovita page boilerplate ("prepare a personalized obituary...", "echovita offers...")
    # appears on ALL pages — only check content-specific template phrases.
    _TEMPLATE_PHRASES = [
        "remembered by those who knew",
        "remembered by family members, friends, and others whose lives were connected",
        "remembered by family, friends, and others who shared time and connection",
        "will be remembered for the time spent with loved ones",
        "will be remembered for the impact she had on the lives",
        "will be remembered for the impact he had on the lives",
        "bonds he created and the experiences shared will endure",
        "bonds she created and the experiences shared will endure",
        "friends and family will honor",
        "friends, family, and colleagues will cherish their memories",
        "you can send your sympathy in the guestbook provided",
        "leaves behind cherished memories for friends, family",
        "life was marked by connections and exper",
    ]
    # Real obituary indicators — actual named family members
    _REAL_INDICATORS = [
        "survived by", "is survived", "survivors include",
        "leaves behind", "he leaves", "she leaves",
        "his wife", "her husband", "his children", "her children",
        "his son", "her son", "his daughter", "her daughter",
        "his mother", "her mother", "his father", "her father",
        "his parents", "her parents", "his brother", "her brother",
        "his sister", "her sister",
    ]
    full_text_lower = obit["obituary_text"].lower()
    template_hits = sum(1 for phrase in _TEMPLATE_PHRASES if phrase in full_text_lower)
    real_hits = sum(1 for phrase in _REAL_INDICATORS if phrase in full_text_lower)
    obit["is_template"] = template_hits >= 2 and real_hits == 0

    # Extract "survived by" section
    survived_by = ""
    full_text = full_text_lower
    for marker in ["survived by", "is survived", "survivors include", "leaves behind",
                    "he leaves", "she leaves", "loved and cherished by",
                    "cherished by", "mourned by", "remembered by",
                    "people including:"]:
        idx = full_text.find(marker)
        if idx >= 0:
            # Grab text from marker to next period-ending sentence or "preceded"
            raw = obit["obituary_text"][idx:]
            end_markers = ["preceded in death", "preceded by", "predeceased",
                           "funeral service", "memorial service", "visitation",
                           "in lieu of", "contributions may",
                           # Echovita UI boilerplate marking end of obituary text /
                           # start of user-submitted sympathy comments. Without
                           # these, generic templates with no real end marker
                           # bleed into the comments section and pick up
                           # unrelated commenters' family mentions.
                           "would you like to offer", "wrote a sympathy message",
                           "make sure relatives of", "leave a sympathy message",
                           "share a memory", "authorize the original obituary",
                           "a unique and lasting tribute", "subscribe to receive"]
            end_idx = len(raw)
            for em in end_markers:
                ei = raw.lower().find(em)
                if 0 < ei < end_idx:
                    end_idx = ei
            survived_by = raw[:end_idx].strip()
            break

    obit["survived_by_raw"] = survived_by


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    obits = scrape_obituaries(max_pages=1, max_age_days=3)
    print("\n%d obituaries found:" % len(obits))
    for o in obits[:5]:
        print("  %s (DOD: %s, age: %s)" % (o["name"], o.get("date_of_death", "?"), o.get("age", "?")))
        if o.get("survived_by_raw"):
            print("    Survived by: %s" % o["survived_by_raw"][:150])
        if o.get("funeral_home"):
            print("    Funeral home: %s" % o["funeral_home"])
