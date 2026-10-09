"""Foreclosure checks that run BEFORE a record is created (user, 2026-10-09).

Built for the pre-July Tulsa backpull, and run on every foreclosure batch from
then on. Both are free and read-only. Both run before `upload_to_datasift()`,
because `--create` writes to the CRM whether or not `--commit` is passed.

1. ALREADY IN THE CRM -> SKIPPED ENTIRELY
   "if its already in there I'm out". A row whose property address already
   resolves to a CRM record is never created, never gets notes, a Message Board
   post or a tag, and is never traced. Re-tracing an existing record re-bills
   it and stamps `Pre-existing` on its numbers, and phone tags cannot be
   removed. A FAILED lookup is not a miss: that row is held, not created.
   Matched on address alone (no city), because the petition's city and the
   CRM's can differ (Glenpool vs Tulsa) and a missed duplicate is the costly
   mistake here.

2. OWNER OF RECORD IS NOT THE DEFENDANT -> HELD FOR REVIEW
   On an old filing, a different owner on title usually means the house
   already sold, often by sheriff deed to the lender, before DataSift shows a
   sale date. Read off the Tulsa County Assessor (plain HTTP, free). The row is
   held, not created, and written to a review sheet. Setting the row's
   `Owner Confirmed` column to `Yes` releases it on the next run.
   Fails OPEN: no Assessor match, or more than one parcel at the address,
   means the row is not checked and goes on (and is listed as unchecked).
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


def assessor_owner(street: str, *, search, parse_street) -> tuple[str | None, str]:
    """(owner_of_record, note) for one property street, or (None, why_not).

    `search(terms)` is tulsa_assessor.search_assessor; `parse_street` is
    siftmap_address.parse_street. The Assessor search is a loose word match
    ("9921 E 114" returns 14 parcels across the county), so a parcel counts
    only when its house number, street name/number and leading direction all
    agree, and exactly one account survives.
    """
    p = parse_street(street)
    if not p:
        return None, "no house number to search on"
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
        return None, f"no Assessor parcel matched {terms!r}"
    if len(hits) > 1:
        return None, f"{len(hits)} Assessor parcels match {terms!r}"
    rec = next(iter(hits.values()))
    owner = (rec.get("FullPrimaryOwnerName") or rec.get("OwnerName1") or "").strip()
    if not owner:
        return None, "Assessor parcel has no owner name"
    return owner, f"Assessor {rec.get('AccountNo')} {rec.get('FullPropertyStreet')}"


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


def owner_of_record_check(rows: list[dict], *, search, parse_street,
                          pause: float = 2.0) -> tuple[list[dict], list[dict], list[dict]]:
    """Split rows into (go_on, held, unchecked). `unchecked` rows are ALSO in
    `go_on`: the check fails open."""
    go_on, held, unchecked = [], [], []
    for i, r in enumerate(rows):
        if _yes(r.get("Owner Confirmed")):
            go_on.append(r)
            continue
        if i:
            time.sleep(pause)
        street = str(r.get("Property Street") or "").strip()
        try:
            owner, note = assessor_owner(street, search=search, parse_street=parse_street)
        except Exception as e:                       # noqa: BLE001 - fail open, loudly
            owner, note = None, f"Assessor lookup failed: {e}"
        if owner is None:
            unchecked.append({**r, "_owner_note": note})
            go_on.append(r)
            continue
        if owner_matches(owner, r):
            go_on.append(r)
            continue
        held.append({**r, "_owner_of_record": owner, "_owner_note": note})
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
            "Owner Status", "Owner of Record (Assessor)", "Assessor Parcel"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in held:
            w.writerow({**{c: r.get(c, "") for c in cols[:-2]},
                        "Owner of Record (Assessor)": r.get("_owner_of_record", ""),
                        "Assessor Parcel": r.get("_owner_note", "")})
    return path
