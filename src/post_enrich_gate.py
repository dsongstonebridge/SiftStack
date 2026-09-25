"""Post-enrichment exclusion gate — remove what the user will not buy, BEFORE any trace.

The user's rules (2026-09-23): he does NOT buy a property that is

  1. **listed on the MLS**,
  2. carrying **less than 15% equity**, or
  3. **sold within the last 2 years** (was 3 until 2026-09-25; user widened the net).

Such a record is deleted from the CRM and never skip traced.

Why this is a SECOND gate, separate from buy_box.py
---------------------------------------------------
`buy_box.py` runs on the extracted sheet BEFORE creation, where none of these
facts exist yet: they come from DataSift itself. So this gate runs after
`upload_to_datasift()` (create + API enrich) and before `run_pipeline()` — the
last point where excluding a record still saves the trace spend. The record
has already reached the CRM by then, so exclusion means DELETING it. On
2026-09-22, six of 28 records (~21%) would have failed these rules and were
traced anyway, because the gate did not exist.

Data, all native property fields, verified live
-----------------------------------------------
- `equity_percent` (string, e.g. "6.52") and `last_sold` ("YYYY-MM-DD") —
  populated by DataSift AT CREATE TIME, not only by enrich. Blank on roughly
  5 of 28 on a real batch.
- `mls` — reads "Off Market" or "Listed". **Validated 2026-09-25 with a
  positive control**: a throwaway at 2441 S Norwood Ave, Tulsa (listed that
  day, per the user) read "Listed" straight off bulk-create, while an
  off-market record read "Off Market". Before that the field looked useless,
  because the only known listed property (Reyes) had been deleted before the
  check ran - a negative over a sample with no positives in it.
  Only "Listed" has been observed as a positive. Other on-market wordings
  below are a reasonable guess and exclude too; any value we do not recognise
  is logged and NOT excluded.
- Equity is a snapshot: Dana Miller read 14.81% on 2026-09-23 and 36.35% two
  days later after a re-enrich. The gate judges the value as of this run.

FAILS OPEN, like buy_box.py: a missing or unparseable value never excludes
(user: "if its true for most of the properties but not all of them, it doesnt
mean that we cant write that into the code for the ones that it would
benefit"). A record that cannot be looked up at all is kept, and said so.

Never deletes a record that has been worked
-------------------------------------------
Bulk-create leaves an existing address alone, so a row in a new batch can
resolve to an OLD record - possibly one already traced and paid for, or with
call history. Deletion is irreversible, so the gate deletes only a record
whose owner carries no phone numbers (a freshly created record never does -
phones arrive later, via run_pipeline). A failing record that already has
phones is still dropped from the trace, but left in the CRM and reported
for the user to decide.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

logger = logging.getLogger(__name__)

MIN_EQUITY_PERCENT = 15.0
RECENT_SALE_YEARS = 2

#: `mls` values that mean on the market. Only "listed" has been SEEN live
#: (2026-09-25); the rest are the standard MLS statuses, excluded on the same
#: reasoning. An unrecognised value is logged, never excluded.
_ON_MARKET = frozenset({"listed", "active", "pending", "under contract",
                        "contingent", "coming soon", "for sale"})
_OFF_MARKET = frozenset({"off market", "off-market", "not listed", "sold",
                         "expired", "withdrawn", "cancelled", "canceled"})


def _years_before(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:                      # Feb 29 -> Feb 28
        return d.replace(year=d.year - years, day=28)


def _parse_date(v) -> date | None:
    if not v:
        return None
    if isinstance(v, date):
        return v
    s = str(v).strip()[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def check_property(prop: dict, *, today: date | None = None) -> list[str]:
    """Reasons this CRM property fails the rules; empty means it passes.

    `prop` is a property object as `datasift_api.get_property()` returns it.
    Missing or unparseable data never produces a reason.
    """
    today = today or date.today()
    reasons: list[str] = []

    mls = str(prop.get("mls") or "").strip().lower()
    if mls in _ON_MARKET:
        reasons.append(f"MLS-listed (mls = {prop.get('mls')!r})")
    elif mls and mls not in _OFF_MARKET:
        logger.info("post-enrich gate: unrecognised mls value %r - not excluding on it",
                    prop.get("mls"))

    equity = prop.get("equity_percent")
    try:
        eq = float(equity) if equity not in (None, "") else None
    except (TypeError, ValueError):
        eq = None
    if eq is not None and eq < MIN_EQUITY_PERCENT:
        reasons.append(f"equity {eq:.2f}% is under {MIN_EQUITY_PERCENT:.0f}%")

    sold = _parse_date(prop.get("last_sold"))
    if sold and sold > _years_before(today, RECENT_SALE_YEARS):
        reasons.append(f"sold {sold.isoformat()}, within the last {RECENT_SALE_YEARS} years")

    return reasons


def _has_been_worked(prop: dict) -> bool:
    owner = prop.get("owner") or {}
    return bool(owner.get("phones"))


def apply_post_enrich_gate(rows: list[dict], *, find_property, get_property,
                           delete_property, forget_uuids=None,
                           today: date | None = None) -> tuple[list[dict], list[dict]]:
    """Split created rows into (kept, excluded), deleting excluded records.

    `rows` are the property-template rows just created (`Property Street`,
    `Property City`). The DataSift calls are passed in so tests never touch
    the network; `forget_uuids(uuids)` drops deleted records from the local
    uuid map.

    Every excluded row is RETURNED, carrying `_gate_reasons`, `_gate_uuid` and
    `_gate_action` ("deleted", "kept in CRM - already has phones", or
    "delete FAILED: ..."), so the caller can report it. None of them is
    handed on to be traced, whatever the action.
    """
    kept, excluded, deleted = [], [], []
    for r in rows:
        street = str(r.get("Property Street") or r.get("Property Street Address") or "").strip()
        city = str(r.get("Property City") or "").strip()
        try:
            hit = find_property(street, city, "")
            prop = get_property(hit["uuid"]) if hit else None
        except Exception as e:                   # noqa: BLE001 - fail open, loudly
            logger.warning("post-enrich gate: could not read %s (%s) - kept", street, e)
            kept.append(r)
            continue
        if not prop:
            logger.warning("post-enrich gate: no CRM record found for %s, %s - kept, "
                           "unchecked", street, city)
            kept.append(r)
            continue

        reasons = check_property(prop, today=today)
        if not reasons:
            kept.append(r)
            continue

        uuid = prop.get("uuid") or hit.get("uuid")
        if _has_been_worked(prop):
            action = "kept in CRM - already has phones, delete by hand if wanted"
        else:
            try:
                delete_property(uuid)
                deleted.append(uuid)
                action = "deleted"
            except Exception as e:               # noqa: BLE001 - still never traced
                action = f"delete FAILED: {e}"
        excluded.append({**r, "_gate_reasons": reasons, "_gate_uuid": uuid,
                         "_gate_action": action})

    if deleted and forget_uuids:
        forget_uuids(deleted)
    if excluded:
        logger.warning("post-enrich gate: %d of %d record(s) excluded before any trace",
                       len(excluded), len(rows))
    return kept, excluded


def describe() -> str:
    return (f"Post-enrichment gate: not MLS-listed, equity at least "
            f"{MIN_EQUITY_PERCENT:.0f}%, not sold within the last {RECENT_SALE_YEARS} "
            f"years. Missing data never excludes.")
