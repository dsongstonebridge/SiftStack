"""Title check before a SmartSkip order (Jeff, 2026-11-25).

Ty's Level 2 (ownership verification) has to be done before Level 3 (SmartSkip
relatives). The owner profile already pulls the county title holder and the
deed history for free; this module decides, per record, what that means for
the order:

    clean   the search name is the person on title, at this parcel. Ordered.
            When the county carries a middle name/initial the search lacks,
            it is added so SmartSkip searches the fuller name.
    review  close or questionable: held OUT of the order and listed for Jeff
            with a one-line reason. Jeff clears it with title_ok.
    skip    clear miss: not ordered, listed in the run summary only. Nothing
            for Jeff to review.

Jeff: "I dont need to review all failures... just close and/or questionable ones."

age_check() runs AFTER the order (SmartSkip is the only source of the age): a
searched person too young to have bought the house is the wrong person (521 N
Tacoma, 2026-11-16: "John Smith" matched the owner's 36-year-old grandson).
"""
from __future__ import annotations

import re
from datetime import date, datetime

SUFFIXES = {"JR", "SR", "II", "III", "IV"}
_TRUST_WORDS = {"REV", "REVOCABLE", "LIVING", "TRUST", "TRUSTEE", "TRUSTEES", "TR", "TRS",
                "TTEE", "TTEES", "DTD", "DATED", "ETAL", "ET", "AL", "ETUX", "UX", "LIFE",
                "ESTATE", "EST", "IRREVOCABLE", "AGREEMENT", "UTA", "UTD", "OF", "THE"}
_FAMILY = "FAMILY"
_ENTITY = re.compile(r"\b(LLC|L L C|INC|CORP|CORPORATION|COMPANY|CO|LP|LTD|LLP|PROPERTIES|"
                     r"PROPERTY|HOLDINGS|INVESTMENTS?|BANK|CHURCH|CITY|COUNTY|STATE|HOUSING|"
                     r"AUTHORITY|PARTNERS|PARTNERSHIP|GROUP|REALTY|HOMES|ASSOCIATION|ASSN|"
                     r"MINISTRIES|FOUNDATION|VENTURES|CAPITAL|SERVICES|ENTERPRISES|RENTALS|"
                     r"FUND|SECRETARY|HUD|FEDERAL|MORTGAGE)\b")
RECENT_DEED_DAYS = 730          # Jeff's "sold within 2 years" line


def _toks(s: str) -> list[str]:
    return re.sub(r"[^A-Z ]", " ", (s or "").upper().replace(".", "")).split()


def parse_search(first: str, last: str) -> dict:
    """The CRM / named search person. `first` may carry a middle ("John E")."""
    ft, lt = _toks(first), _toks(last)
    suffix = ""
    while lt and lt[-1] in SUFFIXES:
        suffix = lt.pop()
    while len(ft) > 1 and ft[-1] in SUFFIXES:
        suffix = suffix or ft.pop()
    return {"first": ft[0] if ft else "", "middle": ft[1:], "last": lt, "suffix": suffix}


def _person(tokens: list[str], last: list[str], alt_last: str = "") -> dict | None:
    suffix = ""
    t = [x for x in tokens]
    while t and t[-1] in SUFFIXES:
        suffix = t.pop()
    t = [x for x in t if x not in SUFFIXES]
    if not t and not last:
        return None
    return {"first": t[0] if t else "", "middle": t[1:], "last": last, "alt_last": alt_last,
            "suffix": suffix}


def parse_owners(owner: str) -> tuple[list[dict], list[str]]:
    """County owner string -> (people, entity names).

    'SMITH, JOHN E'                      -> John E Smith
    'FENWICK, PAUL & LINDA ORMSBY'       -> Paul Fenwick, Linda Ormsby (or Linda Ormsby Fenwick)
    'SMITH JOHN E REV TRUST'             -> John E Smith (trust words dropped)
    'SMITH FAMILY TRUST'                 -> last name Smith only, no first
    'ABC PROPERTIES LLC'                 -> entity, no person
    'LAST, FIRST C/O LAST2, FIRST2 ...'  -> both people
    """
    people, entities = [], []
    for seg in re.split(r"\bC/O\b|\bC O\b|;", (owner or "").upper()):
        seg = seg.strip(" ,")
        if not seg:
            continue
        if _ENTITY.search(seg):
            entities.append(seg)
            continue
        family = _FAMILY in _toks(seg)
        if "," in seg:
            last_part, rest = seg.split(",", 1)
            last = [x for x in _toks(last_part) if x not in _TRUST_WORDS and x != _FAMILY]
            pieces = re.split(r"&|\bAND\b", rest)
        else:
            toks = [x for x in _toks(seg) if x not in _TRUST_WORDS and x != _FAMILY]
            if not toks:
                continue
            last, rest_toks = toks[:1], toks[1:]
            pieces = re.split(r"&|\bAND\b", " ".join(rest_toks))
        for i, piece in enumerate(pieces):
            pt = [x for x in _toks(piece) if x not in _TRUST_WORDS and x != _FAMILY]
            if i == 0:
                p = _person(pt, last)
            else:
                # 'LINDA ORMSBY' may be first + own last name, or a two-word first name.
                alt = pt[-1] if len(pt) >= 2 and len(pt[-1]) > 1 and pt[-1] not in SUFFIXES else ""
                p = _person(pt[:-1] if alt else pt, last, alt)
            if p:
                people.append(p)
        if family and not any(p["first"] for p in people if p["last"] == last):
            people.append({"first": "", "middle": [], "last": last, "alt_last": "", "suffix": ""})
    return people, entities


def _last_match(s: dict, p: dict) -> bool:
    want = set(s["last"])
    if not want:
        return False
    return want <= set(p["last"]) or (p.get("alt_last") and want == {p["alt_last"]}) \
        or (set(p["last"]) and set(p["last"]) <= want)


def _full(p: dict, s: dict) -> str:
    last = " ".join(x.title() for x in (s["last"] or p["last"]))
    mid = " ".join(x.title() for x in p["middle"])
    return " ".join(x for x in (p["first"].title(), mid, last, p["suffix"].title()) if x)


def compare(s: dict, p: dict) -> tuple[str, str]:
    """One county person vs the search: (level, reason). level is
    'clean' | 'close' | 'none'."""
    last_ok = _last_match(s, p)
    first_ok = bool(p["first"]) and p["first"] == s["first"]
    if last_ok and first_ok:
        if s["middle"] and p["middle"] and s["middle"][0][0] != p["middle"][0][0]:
            return "close", (f"middle name differs: searched {' '.join(s['middle'])}, title has "
                             f"{' '.join(p['middle'])}")
        if s["suffix"] and p["suffix"] and s["suffix"] != p["suffix"]:
            return "close", f"suffix differs: searched {s['suffix']}, title has {p['suffix']}"
        return "clean", ""
    if last_ok and p["first"] and s["first"] and (len(p["first"]) == 1 or len(s["first"]) == 1) \
            and p["first"][0] == s["first"][0]:
        return "close", "title gives only an initial for the first name"
    if last_ok and not p["first"]:
        return "close", "title is a family trust with no first name, so it can't show who"
    if last_ok:
        return "close", (f"same last name, different first name on title "
                         f"({p['first'].title()} vs {s['first'].title()})")
    if first_ok and s["last"]:
        return "close", "first name matches but the last name differs (maiden or married name?)"
    return "none", ""


def _deed_date(s: str):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime((s or "").strip(), fmt).date()
        except ValueError:
            pass
    return None


def _named_in(s: dict, text: str) -> bool:
    t = set(_toks(text))
    return bool(s["first"]) and s["first"] in t and set(s["last"]) <= t


def verdict(first: str, last: str, assessor: dict, *, named: bool = False,
            today: date | None = None) -> dict:
    """assessor = the owner profile's 'Tulsa County Assessor' section.
    named = Claude/Jeff named the search person (probate decedent, a root owner):
    a title mismatch is then a question, never a silent skip."""
    today = today or date.today()
    s = parse_search(first, last)
    who = " ".join(x for x in (first, last) if x).strip()
    data = (assessor or {}).get("data") or {}
    status = (assessor or {}).get("status", "")
    findings = " ".join((assessor or {}).get("findings") or [])
    out = {"verdict": "", "reason": "", "middle": "", "title": ""}

    if not data.get("account"):
        near = data.get("near_situs") or []
        if near:
            n = near[0]
            out.update(verdict="review", reason=(
                f"address close but not exact: record says the house is on a different street type "
                f"than the county parcel {n.get('street')} (owner {n.get('owner') or '?'})"))
        elif "assessor error" in findings.lower() or not assessor:
            out.update(verdict="review", reason=f"the Assessor could not be read, so title is unchecked ({findings})")
        else:
            out.update(verdict="skip", reason="no county parcel matches this address")
        return out

    owner = (data.get("situs") or {}).get("owner") or ""
    out["title"] = owner
    people, entities = parse_owners(owner)
    best, reasons = None, []
    for p in people:
        lvl, why = compare(s, p)
        if lvl == "clean":
            best = p
            break
        if lvl == "close":
            reasons.append(why)

    sales = data.get("sales") or []
    last_deed = sales[0] if sales else {}
    d = _deed_date(last_deed.get("sale_date", ""))
    recent = bool(d and (today - d).days <= RECENT_DEED_DAYS)
    deed_txt = (f"{last_deed.get('sale_date')}: {last_deed.get('grantor') or '?'} -> "
                f"{last_deed.get('grantee') or '?'}") if last_deed else ""

    if best:
        # Only a recent deed that moved the house OUT of their name is a question;
        # a recent purchase BY them leaves them the owner (2026-11-25).
        if recent and not _named_in(s, last_deed.get("grantee") or ""):
            out.update(verdict="review", reason=f"the house changed hands recently ({deed_txt})")
            return out
        if best["middle"] and not s["middle"]:
            out["middle"] = " ".join(x.title() for x in best["middle"])
        out.update(verdict="clean", reason=f"{who} is on title ({owner})")
        return out

    # Not on title. A deed FROM the searched person is an estate-connected transfer.
    if last_deed and _named_in(s, last_deed.get("grantor") or ""):
        to_family = bool(set(s["last"]) & set(_toks(last_deed.get("grantee") or "")))
        if recent or to_family:
            out.update(verdict="review", reason=(
                f"{who} deeded the house away ({deed_txt}); title is now {owner}"))
        else:
            out.update(verdict="skip", reason=f"{who} sold the house ({deed_txt}); title is now {owner}")
        return out
    if reasons:
        out.update(verdict="review", reason=f"{reasons[0]} (title: {owner})")
        return out
    if named:
        out.update(verdict="review", reason=(
            f"{who} (named for the search) is not the name on title ({owner}). "
            "Fine for a root owner or decedent; confirm before ordering."))
        return out
    if entities and not people:
        out.update(verdict="skip", reason=f"title is a company with no person ({owner})")
        return out
    out.update(verdict="skip", reason=f"title holder is a different person or family ({owner})")
    return out


def acquired(first: str, last: str, sales: list[dict]) -> date | None:
    """When the searched person took title: the OLDEST deed granting it to them."""
    s = parse_search(first, last)
    hits = [_deed_date(x.get("sale_date", "")) for x in sales or []
            if _named_in(s, x.get("grantee") or "")]
    hits = [h for h in hits if h]
    return min(hits) if hits else None


def age_check(age, acquired_on: date | None, *, today: date | None = None) -> str | None:
    """A plain-English reason the SmartSkip subject cannot be the owner, or None."""
    try:
        age = int(str(age).strip())
    except (TypeError, ValueError):
        return None
    if not acquired_on:
        return None
    today = today or date.today()
    at_purchase = age - (today.year - acquired_on.year)
    if at_purchase < 18:
        then = "not yet born" if at_purchase < 1 else f"about {at_purchase}"
        return (f"SmartSkip's person is {age} now, so {then} when the owner took title "
                f"in {acquired_on.year}. Too young to be the owner: likely a same-name relative "
                "(the 521 N Tacoma grandson).")
    return None
