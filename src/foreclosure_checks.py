"""Foreclosure checks that run BEFORE a record is created (user, 2026-10-09).

Built for the pre-July Tulsa backfill. Both are free and read-only, and both
run before `upload_to_datasift()`, because `--create` writes to the CRM whether
or not `--commit` is passed.

1. ALREADY IN THE CRM -> SKIPPED ENTIRELY
   "if its already in there I'm out". A row whose property address already
   resolves to a CRM record is never created, never gets notes, a Message Board
   post or a tag, and is never traced. Re-tracing an existing record re-bills
   it and stamps `Pre-existing` on its numbers, and phone tags cannot be
   removed. A FAILED lookup is not a miss: that row is held, not created.
   Matched on address alone (no city), because the petition's city and the
   CRM's can differ (Glenpool vs Tulsa) and a missed duplicate is the costly
   mistake here.

   Runs on EVERY foreclosure batch.

2. ASSESSOR OWNERSHIP CHECK -> BACKFILL ONLY (`--backfill`)
   User, 2026-10-09: "if I ever make it clear that I'm doing backfill
   foreclosures again, I need the assessor check to become a step". OSCN
   dockets on old cases often do not say whether the house went to sheriff
   sale or changed hands, so for a backfill every row's parcel is read off the
   Tulsa County Assessor (plain HTTP, free) and the row is HELD, not created,
   when:
     - the owner of record shares no surname with the petition's parties, or
     - the parcel's Sales/Documents table shows a deed recorded ON OR AFTER
       the foreclosure filing date (sheriff's deed, warranty deed, quitclaim,
       anything: a transfer since filing is what the docket would not show), or
     - the parcel cannot be pinned down (no match, or 2+ parcels at the
       address). In backfill mode that is a hold, not a pass: the whole point
       is that every row gets checked.
   Held rows go to `output/foreclosure_owner_review_<ts>.csv`. Setting the
   row's `Owner Confirmed` column to `Yes` releases it on the next run.
   Daily (recent) foreclosures do NOT run this check.
"""

from __future__ import annotations

import csv
import logging
import re
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]+")

#: Words in a defendant list or owner string that are never a family name.
_NOT_A_NAME = frozenset("""
    the and of a an et al aka fka nka dba c o co jr sr ii iii iv
    unknown occupants occupant spouse heirs heir estate devisees executors
    administrators trustees successors assigns creditors deceased decedent
    personal representative representatives mr mrs ms
    bank national association mortgage federal home loan loans financial credit
    union savings services servicing company corp corporation inc llc lp ltd
    trust trustee revocable living family rev dtd dated
    county city state oklahoma tulsa united states america department housing
    urban development secretary internal revenue service treasurer board
    commissioners commission authority finance capital funding lending holdings
    properties property investments group partners homes realty
""".split())

_DIRS = {"N", "S", "E", "W"}

#: Assessor street-type abbreviations -> siftmap_address.parse_street's canon.
#: Used only to break a tie between same-number streets ("3431 S 116 PL E" vs
#: "3431 S 116 AV E", seen live 2026-10-09).
_ASSESSOR_TYPES = {"AV": "ave", "AVE": "ave", "ST": "st", "PL": "pl", "CT": "ct",
                   "DR": "dr", "RD": "rd", "LN": "ln", "CR": "cir", "CIR": "cir",
                   "BV": "blvd", "BLVD": "blvd", "WY": "way", "WAY": "way",
                   "TR": "ter", "TER": "ter", "TE": "ter", "PK": "pkwy",
                   "PKWY": "pkwy", "TRL": "trl", "HY": "hwy", "HWY": "hwy"}


def _name_words(text: str) -> set[str]:
    return {w.upper() for w in _WORD.findall(str(text or ""))
            if len(w) >= 3 and w.lower() not in _NOT_A_NAME}


def _yes(v) -> bool:
    return str(v or "").strip().lower() in {"yes", "y", "true", "1"}


# ── 1. Already in the CRM ────────────────────────────────────────────────

def crm_duplicate_check(rows: list[dict], *, find_property) -> tuple[list[dict], list[dict], list[dict]]:
    """Split rows into (new, already_in_crm, lookup_failed).

    `find_property(street)` returns the CRM record or None and RAISES on a
    failed search (datasift_api.find_property_by_address with strict=True).
    Rows in a batch that repeat an earlier row's address are also dropped
    (reported as already in the CRM, since the first copy will be).
    """
    new, dupes, failed = [], [], []
    seen: dict[str, str] = {}
    for r in rows:
        street = str(r.get("Property Street") or "").strip()
        key = " ".join(street.upper().split())
        if key and key in seen:
            dupes.append({**r, "_dup_reason": f"repeats {seen[key]} in this batch"})
            continue
        try:
            hit = find_property(street) if street else None
        except Exception as e:                       # noqa: BLE001 - fail CLOSED
            failed.append({**r, "_dup_reason": f"CRM lookup failed: {e}"})
            continue
        if hit:
            stored = (hit.get("address") or {}).get("street") or street
            dupes.append({**r, "_dup_reason": f"already in the CRM as {stored!r}",
                          "_dup_uuid": hit.get("uuid")})
            continue
        if key:
            seen[key] = r.get("Case Number") or street
        new.append(r)
    return new, dupes, failed


# ── 2. Owner of record vs the defendant ──────────────────────────────────

def _parcel_street_parts(full: str) -> tuple[str, str, set[str]]:
    toks = str(full or "").upper().split()
    if not toks:
        return "", "", set()
    lead = toks[1] if len(toks) > 1 and toks[1] in _DIRS else ""
    return toks[0], lead, set(toks[1:])


def _core_token(core: str) -> str:
    m = re.match(r"^(\d+)(?:st|nd|rd|th)?$", core, re.IGNORECASE)
    return m.group(1) if m else core.upper()


def assessor_owner(street: str, *, search, parse_street) -> tuple[str | None, str, str]:
    """(owner_of_record, note, account_no) for one property street, or
    (None, why_not, "").

    `search(terms)` is tulsa_assessor.search_assessor; `parse_street` is
    siftmap_address.parse_street. The Assessor search is a loose word match
    ("9921 E 114" returns 14 parcels across the county), so a parcel counts
    only when its house number, street name/number and leading direction all
    agree, and exactly one account survives.
    """
    p = parse_street(street)
    if not p:
        return None, "no house number to search on", ""
    core = _core_token(p["core"])
    terms = " ".join(x for x in (p["number"], p["dir"], core) if x)
    recs = search(terms)
    if not recs:
        time.sleep(10)                              # the Assessor 503s under load
        recs = search(terms)
    hits = {}
    for rec in recs:
        num, lead, words = _parcel_street_parts(rec.get("FullPropertyStreet"))
        if num != p["number"] or core not in words:
            continue
        if p["dir"] and lead and lead != p["dir"]:
            continue
        hits[rec.get("AccountNo")] = rec
    if not hits:
        return None, f"no Assessor parcel matched {terms!r}", ""
    if len(hits) > 1 and p.get("type"):
        typed = {a: rec for a, rec in hits.items()
                 if p["type"] in {_ASSESSOR_TYPES.get(w) for w in
                                  str(rec.get("FullPropertyStreet") or "").upper().split()}}
        if len(typed) == 1:
            hits = typed
    if len(hits) > 1:
        return None, f"{len(hits)} Assessor parcels match {terms!r}", ""
    rec = next(iter(hits.values()))
    account = str(rec.get("AccountNo") or "")
    owner = (rec.get("FullPrimaryOwnerName") or rec.get("OwnerName1") or "").strip()
    if not owner:
        return None, "Assessor parcel has no owner name", account
    return owner, f"Assessor {account} {rec.get('FullPropertyStreet')}", account


def _as_date(v):
    """A filing or sale date as a date: datetime/date, "M/D/YYYY" or
    "YYYY-MM-DD" (the sheet carries real Excel dates). None if unreadable."""
    from datetime import date, datetime
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%y"):
        try:
            return datetime.strptime(s[:19] if "-" in s else s, fmt).date()
        except ValueError:
            continue
    return None


def deeds_since_filing(history: list[dict], filed) -> list[dict]:
    """Every Sales/Documents row recorded on or after the filing date."""
    day = _as_date(filed)
    if not day:
        return []
    return [h for h in history if (_as_date(h.get("sale_date")) or day.replace(year=1)) >= day]


def _describe_deed(h: dict) -> str:
    price = h.get("sale_price")
    return (f"{h.get('sale_date')} {h.get('deed_type') or 'deed'}: "
            f"{h.get('grantor') or '?'} -> {h.get('grantee') or '?'}"
            + (f" (${price:,})" if price else ""))


def petition_names(row: dict) -> set[str]:
    """Family names the petition ties to this property: the defendant, the
    co-borrower, every other defendant, and anyone named in Owner Status (a
    deceased owner whose heir is the contact)."""
    names = _name_words(row.get("Last Name"))
    names |= _name_words(row.get("Co-Borrower Last Name"))
    for k in ("Co-Defendants", "Owner Status", "Decedent Name"):
        names |= _name_words(row.get(k))
    return names


def owner_surnames(owner: str) -> set[str]:
    """Family names in an Assessor owner string. The county writes
    "LAST, FIRST M & FIRST2" and "LAST FIRST REV TRUST", with extra parties
    after "C/O" or "&": the surname is before the comma, else the first word."""
    out: set[str] = set()
    for seg in re.split(r"&|\bC/O\b|\bAND\b", str(owner or "").upper()):
        seg = seg.strip()
        if not seg:
            continue
        head = seg.split(",", 1)[0] if "," in seg else (seg.split() or [""])[0]
        out |= _name_words(head)
    return out


def owner_matches(owner: str, row: dict) -> bool:
    return bool(owner_surnames(owner) & petition_names(row))


def owner_of_record_check(rows: list[dict], *, search, parse_street, sales_history=None,
                          hold_unchecked: bool = False,
                          pause: float = 2.0) -> tuple[list[dict], list[dict], list[dict]]:
    """Split rows into (go_on, held, unchecked).

    `sales_history(account_no)` (tulsa_assessor.get_parcel_sales_history), when
    given, also holds any row whose parcel shows a deed on or after its
    `Date Foreclosure Filed`. `hold_unchecked=True` (backfill) holds a row
    whose parcel cannot be pinned down; otherwise it goes on. Unchecked rows
    are reported in `unchecked` either way, and each held row carries
    `_hold_reason`."""
    go_on, held, unchecked = [], [], []
    for i, r in enumerate(rows):
        if _yes(r.get("Owner Confirmed")):
            go_on.append(r)
            continue
        if i:
            time.sleep(pause)
        street = str(r.get("Property Street") or "").strip()
        try:
            owner, note, account = assessor_owner(street, search=search,
                                                  parse_street=parse_street)
        except Exception as e:                       # noqa: BLE001 - reported, never silent
            owner, note, account = None, f"Assessor lookup failed: {e}", ""
        if owner is None:
            unchecked.append({**r, "_owner_note": note})
            if hold_unchecked:
                held.append({**r, "_owner_of_record": "", "_owner_note": note,
                             "_hold_reason": f"could not check the Assessor: {note}"})
            else:
                go_on.append(r)
            continue
        if not owner_matches(owner, r):
            held.append({**r, "_owner_of_record": owner, "_owner_note": note,
                         "_hold_reason": "owner of record is not on the petition"})
            continue
        if sales_history and account:
            try:
                later = deeds_since_filing(sales_history(account),
                                           r.get("Date Foreclosure Filed"))
            except Exception as e:                   # noqa: BLE001
                later, note = [], f"{note}; sales history failed: {e}"
            if later:
                held.append({**r, "_owner_of_record": owner, "_owner_note": note,
                             "_hold_reason": "deed recorded since the filing: "
                             + "; ".join(_describe_deed(h) for h in later)})
                continue
        go_on.append(r)
    return go_on, held, unchecked


def write_owner_review(held: list[dict], out_dir: Path) -> Path | None:
    """One CSV row per held record: what the petition says, what the county
    says. Mark `Owner Confirmed` = Yes in the batch sheet to release a row."""
    if not held:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"foreclosure_owner_review_{datetime.now():%Y%m%d_%H%M%S}.csv"
    cols = ["Case Number", "Date Foreclosure Filed", "Property Street", "Property City",
            "First Name", "Last Name", "Co-Borrower First Name", "Co-Borrower Last Name",
            "Owner Status", "Owner of Record (Assessor)", "Assessor Parcel", "Why Held"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in held:
            w.writerow({**{c: r.get(c, "") for c in cols[:-3]},
                        "Owner of Record (Assessor)": r.get("_owner_of_record", ""),
                        "Assessor Parcel": r.get("_owner_note", ""),
                        "Why Held": r.get("_hold_reason", "")})
    return path
