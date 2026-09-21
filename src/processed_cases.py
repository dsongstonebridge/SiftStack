"""Have we already processed this probate case?

User, 2026-09-21: on a test run two already-created cases (Ross PB-2026-0760,
Johnson PB-2026-0761) were left in the Probates folder on purpose to see whether
they would be caught. They were - but only because Claude remembered them from
earlier sessions. Days will come when the folder is not cleared, so the
pipeline has to catch a repeat by itself.

Two independent nets, both free and read-only:
  1. A LEDGER of every case the pipeline has created, keyed by normalized case
     number ("PB-2026-0761" == "PB-2026-761"). Works before any address is known.
  2. The CRM itself: a record at the row's property address whose Notes carry
     the row's case number. Catches cases created before the ledger existed.

A row is held out of creation if ANY of its cases is already processed. A
merged row (two probates on one house) with only ONE case processed is reported
as PARTIAL rather than silently created - creating it would double-post the
processed case's notes and Message Board.

Nothing here deletes or moves a file; the caller reports and the human decides.

    python src/processed_cases.py check PB-2026-761 PB-2026-778
    python src/processed_cases.py list
    python src/processed_cases.py seed        # the five cases known on 2026-09-21
"""

import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

LEDGER_PATH = Path(__file__).resolve().parent.parent / "output" / ".probate_processed_cases.json"

_CASE_RE = re.compile(r"\bPB\s*-?\s*(\d{4})\s*-?\s*0*(\d+)\b", re.IGNORECASE)


def normalize_case(text: str) -> Optional[str]:
    """First probate case number in `text`, canonical: 'PB-2026-761'."""
    m = _CASE_RE.search(text or "")
    return f"PB-{m.group(1)}-{int(m.group(2))}" if m else None


def extract_cases(text: str) -> list[str]:
    """Every distinct probate case number in `text`, in order. A merged row
    carries several: 'PB-2026-778 (Edwin ...); PB-2026-780 (Marian ...)'."""
    seen: list[str] = []
    for m in _CASE_RE.finditer(text or ""):
        c = f"PB-{m.group(1)}-{int(m.group(2))}"
        if c not in seen:
            seen.append(c)
    return seen


def load_ledger(path: Optional[Path] = None) -> dict:
    path = Path(path or LEDGER_PATH)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as e:
        # A corrupt ledger must be loud: silently treating it as empty would
        # let every repeat through.
        raise RuntimeError(f"processed-cases ledger unreadable at {path}: {e}") from e


def record_case(case: str, *, decedent: str = "", property_address: str = "",
                uuid: str = "", note: str = "", path: Optional[Path] = None) -> None:
    case = normalize_case(case) or ""
    if not case:
        return
    path = Path(path or LEDGER_PATH)
    ledger = load_ledger(path)
    ledger[case] = {
        "decedent": decedent, "property": property_address, "uuid": uuid, "note": note,
        "recorded": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")


def record_row(row: dict, uuid: str = "", path: Optional[Path] = None) -> list[str]:
    """Record every case on a created row. Returns the cases recorded."""
    cases = extract_cases(str(row.get("Case Number") or ""))
    for c in cases:
        record_case(c, decedent=str(row.get("Decedent Name") or ""),
                    property_address=str(row.get("Property Street") or ""),
                    uuid=uuid, path=path)
    return cases


def check_rows(rows: list[dict], *, path: Optional[Path] = None) -> tuple[list[dict], list[dict]]:
    """Split rows into (fresh, already). Every `already` row carries
    `_already_processed`: {"cases": [...], "processed": [...], "partial": bool,
    "where": "ledger"}. Rows are RETURNED, never dropped silently."""
    ledger = load_ledger(path)
    fresh, already = [], []
    for row in rows:
        cases = extract_cases(str(row.get("Case Number") or ""))
        if not cases:
            logger.warning("processed-cases: row %r has no readable case number - "
                           "cannot check it against the ledger",
                           row.get("Decedent Name") or row.get("Property Street") or "?")
            fresh.append(row)
            continue
        done = [c for c in cases if c in ledger]
        if done:
            row["_already_processed"] = {"cases": cases, "processed": done,
                                          "partial": len(done) < len(cases), "where": "ledger"}
            already.append(row)
        else:
            fresh.append(row)
    return fresh, already


def crm_check_rows(rows: list[dict], find_fn: Callable, get_fn: Callable) -> tuple[list[dict], list[dict]]:
    """Second net, run once property addresses are known: a CRM record at the
    row's address whose Notes carry one of the row's case numbers. An address
    that exists WITHOUT the case (say a foreclosure record) is NOT a repeat and
    is only logged. `find_fn(street, city, state)` and `get_fn(uuid)` are
    datasift_api.find_property_by_address / get_property, injected so this is
    testable offline."""
    fresh, already = [], []
    for row in rows:
        street = str(row.get("Property Street") or "").strip()
        cases = extract_cases(str(row.get("Case Number") or ""))
        if not street or not cases:
            fresh.append(row)
            continue
        try:
            found = find_fn(street, str(row.get("Property City") or ""),
                            str(row.get("Property State") or "OK"))
            notes = (get_fn(found["uuid"]) or {}).get("notes") if found else ""
        except Exception as e:                      # noqa: BLE001 - free lookup, never lose the row
            logger.warning("processed-cases: CRM lookup failed for %r: %s", street, e)
            fresh.append(row)
            continue
        done = [c for c in cases if c in extract_cases(notes or "")]
        if found and done:
            row["_already_processed"] = {"cases": cases, "processed": done,
                                          "partial": len(done) < len(cases),
                                          "where": f"CRM record {found['uuid']}"}
            already.append(row)
        else:
            if found:
                logger.info("processed-cases: %r already exists in the CRM but its notes "
                            "do not carry %s - treating as a different lead", street, cases)
            fresh.append(row)
    return fresh, already


def describe(row: dict) -> str:
    a = row.get("_already_processed") or {}
    label = "PARTIALLY processed" if a.get("partial") else "ALREADY PROCESSED"
    return (f"{label}: {', '.join(a.get('processed', []))} "
            f"(row covers {', '.join(a.get('cases', []))}) - found in {a.get('where', '?')}")


#: The cases known when the ledger was introduced (2026-09-21).
SEED = [
    ("PB-2026-760", "Clifton Lee Ross", "4529 E Xyler St N", "366f1349-bdca-4b65-8fc9-0f1474212e2a"),
    ("PB-2026-761", "Tina Fay Johnson", "1916 S 140th East Ave", "9f9c6d08-1995-447a-8fca-dee9ee0bcc61"),
    ("PB-2026-777", "Loretta Jean Sherman", "3012 S 12 ST E", "25036137-b98a-4731-bb61-4f9bd2f6bd83"),
    ("PB-2026-778", "Edwin Russell Cape", "17021 N MEMORIAL DR E", "46ec3e30-6eea-48d7-bd06-61eb88003322"),
    ("PB-2026-780", "Marian Johanna Cape", "17021 N MEMORIAL DR E", "46ec3e30-6eea-48d7-bd06-61eb88003322"),
]


def seed(path: Optional[Path] = None) -> None:
    for case, dec, prop, uuid in SEED:
        record_case(case, decedent=dec, property_address=prop, uuid=uuid,
                    note="seeded 2026-09-21", path=path)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] not in ("check", "list", "seed"):
        print(__doc__)
        sys.exit(1)
    if args[0] == "seed":
        seed()
        print(f"seeded {len(SEED)} case(s) into {LEDGER_PATH}")
    elif args[0] == "list":
        for c, v in sorted(load_ledger().items()):
            print(f"{c}  {v.get('decedent', '')}  |  {v.get('property', '')}  |  {v.get('recorded', '')}")
    else:
        ledger = load_ledger()
        for c in args[1:]:
            n = normalize_case(c)
            print(f"{c} -> {n}: " + (f"ALREADY PROCESSED ({ledger[n].get('decedent')}, {ledger[n].get('property')})"
                                      if n in ledger else "not in the ledger"))
