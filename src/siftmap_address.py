"""Reword a property address the way SiftMap writes it, before the record is created.

WHY (2026-10-02): a CRM record only gets its "Open in SiftMap" link, and its
property data (value, year, sqft, MLS, equity), when DataSift can match the
address to a SiftMap parcel. Tulsa's numbered avenues are written several ways
and the two systems disagree: petitions say "10007 South 87th East Avenue",
DataSift stores that as "10007 S 87Th East Ave", and SiftMap has the same house
as "10007 S 87th Ave E". Measured on the live CRM: 22 of 229 records had no
SiftMap link, and SiftMap's own address search found 21 of them as soon as the
street was worded SiftMap's way.

There is no single rewrite rule. SiftMap uses "Ave E" on most numbered avenues
but "East Ave" on others ("13406 S 93rd East Ave", Bixby), so the only reliable
source of the right wording is SiftMap itself.

HOW: SiftMap's address search (free, no auth) is queried with only the parts
every spelling shares: house number, leading direction, the street's number or
name, and the city ("10007 S 87th Tulsa"). A result is accepted only when it
agrees on house number, street core, direction and city or ZIP, and exactly one
distinct street survives (unit numbers must match too). Anything else leaves the
row untouched. This never blocks a record; an unmatched address is created as
before and logged.

Unrecorded silent effect worth knowing: an unlinked record also carries no MLS
or equity data, so the post-enrichment gate (which fails open on missing data)
cannot screen it.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_DIRS = {"n": "N", "s": "S", "e": "E", "w": "W",
         "north": "N", "south": "S", "east": "E", "west": "W"}
_TYPES = {"ave", "av", "avenue", "st", "street", "pl", "place", "ct", "court",
          "dr", "drive", "rd", "road", "ln", "lane", "cir", "circle", "blvd",
          "boulevard", "way", "ter", "terrace", "pkwy", "parkway", "trl", "trail",
          "hwy", "highway", "te"}
# Canonical street type, used only to choose between same-number streets
# ("6310 E 4th St" vs "6310 E 4th Pl" vs "6310 E 4th Te S").
_TYPE_CANON = {"av": "ave", "avenue": "ave", "street": "st", "place": "pl",
               "court": "ct", "drive": "dr", "road": "rd", "lane": "ln",
               "circle": "cir", "boulevard": "blvd", "terrace": "ter", "te": "ter",
               "parkway": "pkwy", "trail": "trl", "highway": "hwy"}
_UNIT_RE = re.compile(r"\s*(?:#|\bunit\b|\bapt\b|\bste\b|\bsuite\b|\blot\b)\s*([a-z0-9-]+)\s*$",
                      re.IGNORECASE)
_ORD_RE = re.compile(r"^(\d+)(st|nd|rd|th)?$", re.IGNORECASE)


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _split_unit(street: str) -> tuple[str, str]:
    m = _UNIT_RE.search(street)
    if not m:
        return street.strip(), ""
    return street[:m.start()].strip(), m.group(1).lower()


def parse_street(street: str) -> dict | None:
    """Pull out house number, leading direction, street core and unit.

    "10007 South 87th East Avenue" -> number 10007, dir S, core 87th
    "334 S Avenue G"               -> number 334,   dir S, core g
    Returns None when there is no house number to anchor on.
    """
    base, unit = _split_unit(str(street or "").replace(".", " ").replace(",", " "))
    toks = base.split()
    if not toks or not toks[0].isdigit():
        return None
    number, rest = toks[0], toks[1:]
    lead = ""
    if len(rest) > 1 and rest[0].lower() in _DIRS:
        lead, rest = _DIRS[rest[0].lower()], rest[1:]
    core, stype = "", ""
    for t in rest:
        if t.lower() in _TYPES:
            stype = _TYPE_CANON.get(t.lower(), t.lower())
    for i, t in enumerate(rest):
        low = t.lower()
        if low in _DIRS:
            continue
        # A street-type word is the core only when nothing follows it
        # ("Avenue G": Avenue is the type, G is the name).
        if low in _TYPES and i + 1 < len(rest):
            continue
        m = _ORD_RE.match(t)
        core = (m.group(1) + _ordinal(int(m.group(1)))) if m else low
        break
    if not core:
        return None
    if stype and core == stype:      # the type word was itself the core ("Avenue")
        stype = ""
    return {"number": number, "dir": lead, "core": core.lower(), "unit": unit,
            "type": stype}


def _zip5(z) -> str:
    return re.sub(r"\D", "", str(z or ""))[:5]


def pick_match(street: str, city: str, zip_code: str, results: list[dict]) -> str | None:
    """Return SiftMap's street line for this property, or None if not certain."""
    want = parse_street(street)
    if not want:
        return None
    city_l = (city or "").strip().lower()
    zip_w = _zip5(zip_code)
    streets: dict[str, str] = {}
    for r in results:
        addr = str(r.get("address") or "")
        parts = [p.strip() for p in addr.split(",")]
        if len(parts) < 3 or (r.get("state") or parts[-1][:2]).upper()[:2] != "OK":
            continue
        got_street, got_city = parts[0], parts[1].lower()
        got_zip = _zip5(parts[-1])
        got = parse_street(got_street)
        if not got or got["number"] != want["number"] or got["core"] != want["core"]:
            continue
        if want["dir"] and got["dir"] and want["dir"] != got["dir"]:
            continue
        if got["unit"] != want["unit"]:
            continue
        if not ((city_l and got_city == city_l) or (zip_w and got_zip == zip_w)):
            continue
        streets[got_street] = got["type"]
    if len(streets) > 1 and want["type"]:
        # Same number and name on several streets: the street type decides.
        streets = {k: v for k, v in streets.items() if v == want["type"]}
    return next(iter(streets)) if len(streets) == 1 else None


def _query(p: dict, city: str) -> str:
    return " ".join(x for x in (p["number"], p["dir"], p["core"], city) if x)


def siftmap_street(street: str, city: str, zip_code: str = "", *, client=None) -> str | None:
    """SiftMap's wording for this address, or None (no match, ambiguous, error)."""
    p = parse_street(street)
    if not p:
        return None
    if client is None:
        from siftmap_standalone import SiftMapClient
        client = SiftMapClient()
    try:
        results = client.autocomplete(_query(p, city), limit=10)
    except Exception as e:                      # noqa: BLE001 - never block a record on this
        logger.warning("SiftMap address search failed for %s: %s", street, e)
        return None
    return pick_match(street, city, zip_code, results)


def align_rows_to_siftmap(rows: list[dict], *, client=None) -> dict:
    """Rewrite each template row's Property Street to SiftMap's wording, in place.

    A template "Mailing Street" that repeated the old property street is
    rewritten with it. Returns {"changed": [...], "unmatched": [...]}.
    """
    changed, unmatched = [], []
    for r in rows:
        old = str(r.get("Property Street") or "").strip()
        if not old:
            continue
        new = siftmap_street(old, str(r.get("Property City") or ""),
                             str(r.get("Property Zip") or ""), client=client)
        if not new:
            unmatched.append(old)
            continue
        if new == old:
            continue
        r["Property Street"] = new
        for k in ("Mailing Street", "Owner Mailing Street"):
            if str(r.get(k) or "").strip().lower() == old.lower():
                r[k] = new
        changed.append((old, new))
    for old, new in changed:
        logger.info("SiftMap wording: %s -> %s", old, new)
    if unmatched:
        logger.warning("No SiftMap match for %d address(es) - created as written, "
                       "will likely have no 'Open in SiftMap' link: %s",
                       len(unmatched), "; ".join(unmatched))
    return {"changed": changed, "unmatched": unmatched}
