"""Quick obituary check for one person: free, read-only, built for deep prospecting.

Answers the questions that decide who goes in the phone list:
  - Is there an obituary for THIS owner (name + place agree)?
  - When did they die?
  - Who survives them (spouse, children), and who died before them?
  - THE SPOUSE TRAP: is this actually someone else's obituary that lists the
    owner as a SURVIVOR? Then the owner is alive and it is not an heir case.

It finds and reads candidate pages and flags what it sees; a human or Claude
still reads the excerpts and decides. Search is DuckDuckGo/Google/Brave through
the existing obituary_enricher helpers (no API key, no cost).

    python src/obituary_check.py --name "Johnnie Fulton" --city Tulsa
    python src/obituary_check.py --name "Mary Smith" --city Tulsa --spouse "John Smith" \
        --street "123 Main St" --out output/deep_prospect/obits/mary_smith.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

import obituary_enricher as oe
from funeral_home_scraper import _extract_survived_by

logger = logging.getLogger(__name__)

_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")}
_SUFFIX = {"JR", "SR", "II", "III", "IV", "V"}
_MONTHS = ("January|February|March|April|May|June|July|August|September|October|"
           "November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec")
_DATE = r"(?:" + _MONTHS + r")\.? \d{1,2},? (?:19|20)\d\d"
_DIED = re.compile(r"(?:passed away|passed on|died|went home to be with|entered (?:into )?"
                   r"(?:eternal )?rest|went to be with)[^.]{0,80}?(" + _DATE + ")", re.I)
_RANGE = re.compile(r"((?:19|20)\d\d)\s*[-–—~]\s*((?:19|20)\d\d)")
_DATE_RANGE = re.compile("(" + _DATE + r")\s*[-–—~]\s*(" + _DATE + ")", re.I)
_PRECEDED = re.compile(r"(?:preceded in death by|predeceased by|was preceded by)(.{0,400}?)"
                       r"(?:\.\s|$)", re.I | re.S)


def tokens(name: str) -> list[str]:
    """Upper-case name words, suffixes and initials-with-dots kept as letters."""
    return [t for t in re.findall(r"[A-Za-z']+", (name or "").upper())
            if t not in _SUFFIX]


def first_last(name: str) -> tuple[str, str]:
    t = tokens(name)
    return (t[0], t[-1]) if len(t) >= 2 else ("", "")


def name_in(name: str, text: str) -> bool:
    """First name (or a nickname of it) and last name both appear, close together."""
    first, last = first_last(name)
    if not first:
        return False
    firsts = {first} | {v.upper() for v in oe._get_name_variants(first)}
    up = (text or "").upper()
    for f in firsts:
        if re.search(r"(?<![A-Z])" + re.escape(f) + r"(?![A-Z])(?:[^.]{0,30}?)(?<![A-Z])"
                     + re.escape(last) + r"(?![A-Z])", up):
            return True
    return False


def first_name_in(name: str, text: str) -> bool:
    first, _ = first_last(name)
    if not first:
        return False
    firsts = {first} | {v.upper() for v in oe._get_name_variants(first)}
    up = (text or "").upper()
    return any(re.search(r"(?<![A-Z])" + re.escape(f) + r"(?![A-Z])", up) for f in firsts)


_STREET_WORDS = {"ST", "STREET", "AVE", "AVENUE", "RD", "ROAD", "DR", "DRIVE", "LN", "LANE",
                 "PL", "PLACE", "CT", "COURT", "BLVD", "WAY", "CIR", "N", "S", "E", "W"}


def street_in(street: str, text: str) -> bool:
    """House number and the street's name word both appear."""
    parts = (street or "").split()
    words = [t for t in tokens(street) if t not in _STREET_WORDS]
    return bool(parts and words) and parts[0] in (text or "") and words[0] in (text or "").upper()


def find_dod(text: str) -> str:
    m = _DIED.search(text or "")
    if m:
        return m.group(1)
    m = _DATE_RANGE.search(text or "")
    if m:
        return m.group(2)
    m = _RANGE.search(text or "")
    return m.group(2) if m else ""


def preceded_by(text: str) -> str:
    m = _PRECEDED.search(text or "")
    return m.group(1).strip() if m else ""


def middle_conflict(owner: str, head: str) -> str:
    """The page names FIRST <other middle> LAST where the owner's middle name is
    known and different: 'Russell Hand Thomas' is not 'Russell Wayne Thomas'
    (2026-10-20, a wrong LIKELY on the first live run). Returns the page's
    middle name, or ""."""
    t = tokens(owner)
    if len(t) < 3:
        return ""
    first, middle, last = t[0], t[1], t[-1]
    firsts = {first} | {v.upper() for v in oe._get_name_variants(first)}
    up = (head or "").upper()
    for f in firsts:
        for m in re.finditer(r"(?<![A-Z])" + re.escape(f) + r"\s+([A-Z]+)\.?\s+(?:[A-Z]\.?\s+)?"
                             + re.escape(last) + r"(?![A-Z])", up):
            mid = m.group(1)
            if mid in _SUFFIX or mid == middle or mid == middle[0]:
                return ""
            return mid.title()
    return ""


def family_hits(known: list[str], text: str) -> list[str]:
    """Known relatives (from the probate filing / record) whose FIRST name shows
    up in the survivors text. First name only: married daughters change surname."""
    return [k for k in known if k and first_name_in(k, text)]


def analyze(page: dict, owner: str, *, city: str = "", street: str = "",
            spouse: str = "", known: list[str] | None = None) -> dict:
    """Flag one candidate page. `page` = {url, title, text}.

    `known`: family already known from the probate filing or the record (spouse,
    heirs, PR). An obituary whose survivors name none of them is NOT this
    owner's, however well the name and city match (Jeff, 2026-10-20)."""
    text, title = page.get("text") or "", page.get("title") or ""
    # Title + FIRST SENTENCE only: that is where the person who died is named.
    # A wider window reaches "survived by his wife, Margaret" and reads the
    # survivor as the decedent, which is the spouse trap itself.
    first_sentence = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0][:300]
    head = title + "\n" + first_sentence
    survived = _extract_survived_by(text)[:800]
    preceded = preceded_by(text)
    out = {
        "url": page.get("url"), "title": title[:160],
        "name_in_title_or_opening": name_in(owner, head),
        "name_anywhere": name_in(owner, text),
        "city_mentioned": bool(city) and city.lower() in (title + " " + text).lower(),
        "street_mentioned": street_in(street, text),
        "dod": find_dod(text),
        "survived_by": survived,
        "preceded_by": preceded[:400],
        "spouse_named_in_opening": bool(spouse) and name_in(spouse, head),
    }
    owner_survives = bool(survived) and (name_in(owner, survived) or
                                         (first_name_in(owner, survived)
                                          and re.search(r"(?i)\b(wife|husband|spouse)\b", survived)
                                          and not out["name_in_title_or_opening"]))
    owner_preceded = bool(preceded) and name_in(owner, preceded)
    known = [k for k in (known or []) if k and not name_in(k, owner)]
    family_text = survived + " " + preceded
    hits = family_hits(known, family_text)
    other_middle = middle_conflict(owner, head)
    out.update(known_family_named=hits, other_middle_name=other_middle)
    named = out["name_in_title_or_opening"]
    if owner_survives and not named:
        verdict = "OWNER IS A SURVIVOR (someone else's obituary; owner likely ALIVE)"
    elif owner_preceded:
        verdict = "OWNER DIED EARLIER (listed as 'preceded in death' in a relative's obituary)"
    elif named and other_middle:
        verdict = f"DIFFERENT PERSON (middle name {other_middle}, not the owner's)"
    elif named and hits:
        verdict = "CONFIRMED (name matches and survivors include known family: " + ", ".join(hits) + ")"
    elif named and known and survived.strip():
        verdict = "DIFFERENT PERSON (none of the known family is among the survivors)"
    elif named and (out["city_mentioned"] or out["street_mentioned"]):
        verdict = ("LIKELY THE OWNER'S OBITUARY (name + place match; survivors not checked "
                   "against known family)")
    elif named:
        verdict = "POSSIBLE (name matches, place not confirmed - check age/relatives)"
    elif out["spouse_named_in_opening"]:
        verdict = "SPOUSE'S OBITUARY (check whether the owner is named as a survivor)"
    else:
        verdict = "UNRELATED / WEAK"
    out["verdict"] = verdict
    return out


def fetch_text(url: str) -> str:
    """Obituary body text, or "" (blocked, empty, or a listing page)."""
    try:
        r = requests.get(url, headers=_UA, timeout=20)
    except requests.RequestException as e:
        logger.debug("fetch failed %s: %s", url, e)
        return ""
    if r.status_code == 403:
        return oe._fetch_cached_text(url)
    if r.status_code != 200:
        return ""
    html = r.text[:400000]
    structured = oe._extract_structured_text(html, url)
    if structured and len(structured) >= 100:
        return structured[:20000]
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))[:20000]


#: "auto" FIRST. Measured 2026-10-16: "google,duckduckgo,brave" (the backend list
#: obituary_enricher uses) and each of those alone raised "No results found" on a
#: query "auto" answered with 8 hits, including the right Legacy and FindAGrave
#: pages. The others are still tried in turn in case auto comes back empty.
BACKENDS = ["auto", "google", "duckduckgo", "brave"]
_OBIT_WORDS = ("obituar", "passed away", "death notice", "funeral", "memorial", "find a grave")


def web_search(query: str, max_results: int = 8) -> list[dict]:
    from ddgs import DDGS
    for backend in BACKENDS:
        try:
            hits = DDGS().text(query, max_results=max_results, backend=backend)
        except Exception as e:                 # noqa: BLE001 - "No results" is an exception here
            logger.debug("search %s via %s: %s", query, backend, e)
            continue
        if hits:
            return hits
    logger.warning("obituary search: every backend returned nothing for %r", query)
    return []


#: People-search aggregators are never fetched (Jeff, 2026-10-19: stay away from
#: people search). Their pages mention "died" often enough to pass the
#: obituary-word filter below.
PEOPLE_SEARCH_DOMAINS = ("truepeoplesearch", "fastpeoplesearch", "whitepages", "spokeo",
                         "beenverified", "radaris", "peoplefinders", "thatsthem", "intelius",
                         "familytreenow", "cyberbackgroundchecks", "searchpeoplefree",
                         "peoplesearchnow", "clustrmaps", "instantcheckmate", "truthfinder",
                         "mylife", "usphonebook", "nuwber", "fastbackgroundcheck", "peoplelooker",
                         "addresses.com", "411.com", "advancedbackgroundchecks", "publicrecordsnow")


def _people_search(url: str) -> bool:
    u = url.lower()
    return any(d in u for d in PEOPLE_SEARCH_DOMAINS)


def _search_page(url: str) -> bool:
    """Legacy's name-search pages (/obituaries/name/<last>/<first>) list many people."""
    u = url.lower()
    return "legacy.com" in u and "/obituaries/name/" in u and "-obituary" not in u


def search(owner: str, city: str, state: str) -> list[dict]:
    """Candidate pages from a few free searches, deduped, listings dropped."""
    first, last = first_last(owner)
    state_name = oe._STATE_NAMES.get(state, state)
    short = f"{first.title()} {last.title()}"
    queries = [f"{owner} obituary {city} {state_name}", f"{short} obituary {city} {state_name}",
               f'"{short}" obituary {state_name}']
    seen, out = set(), []
    for q in queries:
        for h in web_search(re.sub(r"\s+", " ", q).strip()):
            url = (h.get("href") or "").split("#")[0]
            blob = (h.get("title", "") + " " + h.get("body", "")).lower()
            if (not url or url in seen or oe._is_listing_url(url) or _search_page(url)
                    or _people_search(url)
                    or not (oe._is_obituary_url(url) or any(w in blob for w in _OBIT_WORDS))):
                continue
            seen.add(url)
            out.append({"url": url, "title": h.get("title", ""), "snippet": h.get("body", "")})
        time.sleep(1.0)
    return out[:10]


_RANK = ["OWNER IS A SURVIVOR", "OWNER DIED EARLIER", "CONFIRMED", "LIKELY THE OWNER", "POSSIBLE",
         "SPOUSE'S OBITUARY", "DIFFERENT PERSON", "UNRELATED"]
#: Verdicts that mean "not this owner": hidden from the report and the PDF.
RULED_OUT = ("DIFFERENT PERSON", "UNRELATED")


def check(owner: str, *, city: str = "", state: str = "OK", street: str = "",
          spouse: str = "", known: list[str] | None = None,
          pages: list[dict] | None = None) -> dict:
    """Search, read and flag. Pass `pages` ([{url, title, text}]) to skip the web.
    `known`: family already known (filing heirs, PR, spouse); see analyze()."""
    known = list(dict.fromkeys([k for k in ([spouse] + list(known or [])) if k]))
    if pages is None:
        pages = []
        for hit in search(owner, city, state):
            text = fetch_text(hit["url"]) or hit.get("snippet", "")
            pages.append({"url": hit["url"], "title": hit.get("title", ""), "text": text})
    results = [analyze(p, owner, city=city, street=street, spouse=spouse, known=known)
               for p in pages]
    results.sort(key=lambda r: next(i for i, k in enumerate(_RANK) if r["verdict"].startswith(k)))
    top = results[0]["verdict"] if results else "NO OBITUARY FOUND"
    return {"owner": owner, "city": city, "state": state, "spouse": spouse, "known": known,
            "summary": top, "candidates": results}


def report(res: dict) -> str:
    lines = [f"OBITUARY CHECK: {res['owner']} ({res['city'] or '?'}, {res['state']})",
             "Known family checked against survivors: " + (", ".join(res.get("known") or []) or "NONE GIVEN"),
             f"Top finding: {res['summary']}", ""]
    for i, c in enumerate(res["candidates"], 1):
        if c["verdict"].startswith(RULED_OUT):
            continue
        lines += [f"{i}. {c['verdict']}", f"   {c['title']}", f"   {c['url']}",
                  f"   died: {c['dod'] or 'not found'}"]
        if c["survived_by"]:
            lines.append("   survived by: " + re.sub(r"\s+", " ", c["survived_by"])[:400])
        if c["preceded_by"]:
            lines.append("   preceded by: " + re.sub(r"\s+", " ", c["preceded_by"])[:300])
        lines.append("")
    diff = [c for c in res["candidates"] if c["verdict"].startswith("DIFFERENT PERSON")]
    for c in diff:
        lines.append(f"RULED OUT: {c['title']} - {c['verdict']}")
    weak = sum(c["verdict"].startswith("UNRELATED") for c in res["candidates"])
    if weak:
        lines.append(f"({weak} unrelated result(s) hidden; see the JSON)")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--name", required=True)
    ap.add_argument("--city", default="")
    ap.add_argument("--state", default="OK")
    ap.add_argument("--street", default="")
    ap.add_argument("--spouse", default="")
    ap.add_argument("--known", action="append", default=[],
                    help="a relative already known (heir, PR, child); repeat")
    ap.add_argument("--out", help="write the full JSON here")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("ddgs").setLevel(logging.ERROR)
    res = check(a.name, city=a.city, state=a.state, street=a.street, spouse=a.spouse,
                known=a.known)
    print(report(res).encode("ascii", "replace").decode("ascii"))
    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        json.dump(res, open(a.out, "w", encoding="utf-8"), indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
