"""Post-enrichment exclusion gate — remove what the user will not buy, BEFORE any trace.

The user's rules (2026-09-23): he does NOT buy a property that is

  1. **listed on the MLS**,
  2. carrying **less than 15% equity**, or
  3. **sold within the last 2 years** (was 3 until 2026-09-25; user widened the net).

Such a record is deleted from the CRM and never skip traced.

PROBATE IS DIFFERENT (user, 2026-10-08)
---------------------------------------
- **MLS is NOT a rule for probate.** A listed probate property stays. Foreclosure
  still excludes MLS-listed records; the user said probate only, "for now".
- **Property type IS a rule for probate**, read off DataSift's own
  `structure_type` after enrichment: condos, mobile/manufactured homes,
  3+ unit buildings and non-residential use are excluded ("keep condo and
  mobile out of probate for now, i may change my mind later"). Single family
  and duplex pass. Foreclosure does not run this check.
- **"Vacant Land" is NOT excluded.** DataSift's data can be stale: on
  2026-10-08 it called Sherman's brand-new house (3012 S 12th St, Broken Arrow)
  "Residential-Vacant Land". The Assessor's improvements check already
  confirmed a structure before creation, so that beats DataSift. The record
  gets a Message Board note instead, so a human can look.
- An unrecognised `structure_type` is logged and NOT excluded (fails open).

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


#: `structure_type` wording that is OUT for probate. Matched as lowercase
#: substrings. Values seen live 2026-10-08: "Single Family Residential",
#: "Duplex (2 units, any combination)", "Mobile/Manufactured Home (regardless
#: of Land ownership)", "Residential-Vacant Land". The others are DataSift's
#: likely wording for the same categories, unconfirmed.
_PROBATE_EXCLUDED_TYPES = (
    ("condo", "condominium"),
    ("mobile", "mobile/manufactured home"),
    ("manufactured", "mobile/manufactured home"),
    ("triplex", "3+ unit building"),
    ("quadruplex", "3+ unit building"),
    ("fourplex", "3+ unit building"),
    ("apartment", "3+ unit building"),
    ("multi-family", "3+ unit building"),
    ("multifamily", "3+ unit building"),
    ("commercial", "commercial"),
    ("industrial", "industrial"),
)
_PROBATE_OK_TYPES = ("single family", "duplex")
_NON_RESIDENTIAL_USE = ("commercial", "industrial", "agricultur", "exempt")


def _probate_type_reasons(prop: dict) -> list[str]:
    st = str(prop.get("structure_type") or "").strip()
    low = st.lower()
    for word, label in _PROBATE_EXCLUDED_TYPES:
        if word in low:
            return [f"{label} (DataSift structure type = {st!r})"]
    try:
        units = int(prop.get("units") or 0)
    except (TypeError, ValueError):
        units = 0
    if units >= 3:
        return [f"{units} units (DataSift structure type = {st!r})"]
    use = str(prop.get("building_use_code") or "").strip().lower()
    if any(w in use for w in _NON_RESIDENTIAL_USE):
        return [f"non-residential use (DataSift building use = "
                f"{prop.get('building_use_code')!r})"]
    if low and "vacant land" not in low and not any(w in low for w in _PROBATE_OK_TYPES):
        logger.info("post-enrich gate: unrecognised structure type %r - not excluding on it", st)
    return []


def vacant_land_flag(prop: dict) -> bool:
    """DataSift calls it vacant land. Not an exclusion (its data can be stale,
    see the module docstring), only a reason to put a note on the board."""
    return "vacant land" in str(prop.get("structure_type") or "").lower()


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


def check_property(prop: dict, *, today: date | None = None,
                   notice_type: str = "foreclosure") -> list[str]:
    """Reasons this CRM property fails the rules; empty means it passes.

    `prop` is a property object as `datasift_api.get_property()` returns it.
    Missing or unparseable data never produces a reason.
    """
    today = today or date.today()
    reasons: list[str] = []
    probate = notice_type == "probate"

    if probate:
        reasons += _probate_type_reasons(prop)

    mls = str(prop.get("mls") or "").strip().lower()
    if probate:
        pass                                # MLS is not a probate rule (2026-10-08)
    elif mls in _ON_MARKET:
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


VACANT_LAND_NOTE = (
    "CHECK PROPERTY TYPE: DataSift calls this {st!r}. The county Assessor "
    "showed a structure on the parcel before this record was created, and "
    "DataSift's data can be out of date (it called a brand-new house vacant "
    "land on 2026-10-08). Kept and traced. Confirm there is a house before "
    "making an offer.")


def _note_vacant_land(prop: dict, street: str, post_board) -> None:
    st = prop.get("structure_type")
    logger.warning("post-enrich gate: %s - DataSift says %r; kept (Assessor showed a "
                   "structure)", street, st)
    owner_uuid = (prop.get("owner") or {}).get("uuid")
    if not (post_board and owner_uuid):
        return
    try:
        post_board(owner_uuid, VACANT_LAND_NOTE.format(st=st))
    except Exception as e:                       # noqa: BLE001 - a note, never a blocker
        logger.warning("post-enrich gate: could not post the vacant-land note for %s: %s",
                       street, e)


def _has_been_worked(prop: dict) -> bool:
    owner = prop.get("owner") or {}
    return bool(owner.get("phones"))


def apply_post_enrich_gate(rows: list[dict], *, find_property, get_property,
                           delete_property, forget_uuids=None,
                           today: date | None = None, notice_type: str = "foreclosure",
                           post_board=None) -> tuple[list[dict], list[dict]]:
    """Split created rows into (kept, excluded), deleting excluded records.

    `rows` are the property-template rows just created (`Property Street`,
    `Property City`). The DataSift calls are passed in so tests never touch
    the network; `forget_uuids(uuids)` drops deleted records from the local
    uuid map.

    Every excluded row is RETURNED, carrying `_gate_reasons`, `_gate_uuid` and
    `_gate_action` ("deleted", "kept in CRM - already has phones", or
    "delete FAILED: ..."), so the caller can report it. None of them is
    handed on to be traced, whatever the action.

    For probate, a KEPT record DataSift calls vacant land gets a Message Board
    note through `post_board(owner_uuid, text)` when that is given.
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

        reasons = check_property(prop, today=today, notice_type=notice_type)
        if not reasons:
            kept.append(r)
            if notice_type == "probate" and vacant_land_flag(prop):
                _note_vacant_land(prop, street, post_board)
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


def describe(notice_type: str = "foreclosure") -> str:
    if notice_type == "probate":
        return (f"Post-enrichment gate (probate): equity at least "
                f"{MIN_EQUITY_PERCENT:.0f}%, not sold within the last {RECENT_SALE_YEARS} "
                f"years, and not a condo, mobile home, 3+ units or non-residential "
                f"(DataSift structure type). MLS-listed is allowed. Missing data never "
                f"excludes.")
    return (f"Post-enrichment gate: not MLS-listed, equity at least "
            f"{MIN_EQUITY_PERCENT:.0f}%, not sold within the last {RECENT_SALE_YEARS} "
            f"years. Missing data never excludes.")
