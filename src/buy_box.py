"""Buy-box gate — reject records BEFORE the CRM and before any spend.

The user's standing requirement: a record outside the buy box must **never hit
the CRM at all**, and must never be skip traced. In their words, the thing to
prevent is "spending money to be able to reach out to a record that I don't
even want to purchase the house from". So this runs on the extracted sheet,
ahead of `skip-trace --create` — not as a tag applied afterwards.

Criteria, as of 2026-09-04
--------------------------
**Single-family homes, at minimum.** More parameters to come (equity
percentage and ZIP exclusions were named as likely additions), so everything
here is written to be extended without restructuring.

What is actually enforced today, and what is NOT
------------------------------------------------
Enforced, from free county data available before creation:

1. **There is a structure on it.** `tulsa_assessor.get_parcel_improvements()`
   reads the assessor's verbatim "This property has no improvements". Verified
   8/8 on a real Tulsa batch. Bare land is out.
2. **It is residential.** The assessor's `AcctType` separates Residential from
   Commercial / Agricultural / Industrial.
3. **It is not a condominium** (added 2026-09-04, after the user deleted a
   condo that reached the CRM). Neither criterion above catches one: a condo
   has a structure, and `AcctType` reads "Residential" for it exactly as for a
   house. Caught instead from the petition itself — a unit-ownership legal
   description, or a trailing unit designator in the street address — so it
   fires even earlier than the assessor lookup. See `_CONDO_LEGAL_RE` for why
   the bare word "condominium" is NOT one of the signals.

NOT enforced — be honest about this rather than implying otherwise:

- **Single-family vs duplex/triplex is not distinguishable from the free data.**
  `AcctType` reads "Residential" for a duplex exactly as for a house, and the
  assessor's structure detail is rendered client-side from an endpoint that is
  not exposed (checked 2026-09-04). A duplex will pass this gate.
  DataSift's own `Structure Type` field arrives only AFTER enrichment, which
  happens post-creation — too late for a gate whose whole purpose is to keep
  the record out of the CRM. If catching duplexes matters, it needs either a
  paid data source or a second pass that deletes after enrichment, and the
  latter contradicts the "never hit the CRM" requirement.

`fail_open` is deliberate: a MISSING signal never rejects a record. A gate that
silently drops leads on absent data is worse than one that lets a few through,
because the loss is invisible. Same reasoning as `filter_buy_box()` in
`enrichment_pipeline.py`.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: Assessor account types that can hold a single-family home.
_RESIDENTIAL_TYPES = frozenset({"residential"})

#: Account types that are never a house.
_REJECT_TYPES = frozenset({"commercial", "industrial", "agricultural", "exempt"})

#: Condo markers in a LEGAL DESCRIPTION. These describe a unit-ownership
#: estate, which a single-family lot never does.
#:
#: Deliberately NOT the bare word "condominium". Every Fannie/Freddie uniform
#: mortgage carries a rider checkbox list reading "Condominium Rider / Planned
#: Unit Development Rider / 1-4 Family Rider", present whether or not it is
#: ticked. That phrase appeared in 14 of 22 petition PDFs across the 2026-08-31
#: and 2026-09-04 batches while exactly ONE property was actually a condo — a
#: naive grep would have rejected most of the batch.
_CONDO_LEGAL_RE = re.compile(
    r"UNDIVIDED\s+[.\d]+\s*%?\s*INTEREST\s+IN\s+COMMON"
    r"|INTEREST\s+IN\s+COMMON\s+ELEMENTS"
    r"|DECLARATION\s+OF\s+UNIT\s+OWNERSHIP"
    r"|HORIZONTAL\s+PROPERTY\s+REGIME"
    r"|\bUNIT\s+[\w-]+,\s*BUILDING\b"
    r"|,\s*A\s+CONDOMINIUM\b",
    re.IGNORECASE,
)

#: Condo/multi-unit markers in a STREET ADDRESS. A unit designator means the
#: parcel is not a whole house. Anchored so "Unity Dr" or "Apthorp St" cannot
#: trip it.
_UNIT_ADDRESS_RE = re.compile(
    r"(?:^|[\s,])(?:UNIT|APT|APARTMENT|STE|SUITE|BLDG|BUILDING)[\s.#]*[\w-]+\s*$"
    r"|(?:^|[\s,])#\s*[\w-]+\s*$",
    re.IGNORECASE,
)


def check_buy_box(row: dict) -> tuple[bool, list[str]]:
    """Does this row belong in the CRM?

    Returns (passes, reasons). `reasons` explains a rejection, and is empty on
    a pass. Missing data never rejects — see the module docstring.

    Expects the columns the probate/foreclosure extraction sheets carry:
    `Vacant Lot` (Yes/No), `AcctType`, `Land Value`, plus whatever future
    parameters get added below.
    """
    reasons: list[str] = []

    # 1. Bare land — the one signal that is both free and reliable.
    vacant = str(row.get("Vacant Lot") or row.get("is_vacant_lot") or "").strip().lower()
    if vacant in ("yes", "true", "1"):
        reasons.append("empty lot - no structure on the parcel")

    # 2. Non-residential account type.
    acct = str(row.get("AcctType") or row.get("Acct Type") or "").strip().lower()
    if acct and acct in _REJECT_TYPES:
        reasons.append(f"account type is {acct}, not residential")
    elif acct and acct not in _RESIDENTIAL_TYPES:
        # Unknown value: report it, do not reject on it.
        logger.info("buy box: unrecognised AcctType %r - not rejecting on it", acct)

    # 3. Condominium — user, 2026-09-04: "i want single family homes".
    #    Both signals come from the petition itself, so this fires even
    #    earlier than the assessor lookup above, and it catches what AcctType
    #    cannot: a condo is "Residential" to the assessor exactly like a house.
    legal = str(row.get("Legal Description") or row.get("legal_description") or "")
    if legal and _CONDO_LEGAL_RE.search(legal):  # noqa: E501 - see _CONDO_LEGAL_RE on why not a bare keyword
        reasons.append("condominium - legal description is a unit-ownership "
                       "estate, not a single-family lot")

    street = str(row.get("Property Street") or row.get("street") or "").strip()
    if street and _UNIT_ADDRESS_RE.search(street):
        reasons.append(f"address carries a unit designator ({street!r}) - "
                       "not a whole single-family house")

    # --- future parameters go here -----------------------------------
    # Equity percentage and ZIP exclusions were named as likely additions.
    # Both must fail open on missing data, and both must run HERE, before
    # creation — not as a post-upload tag.

    return (not reasons), reasons


def apply_buy_box(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split rows into (kept, rejected). Every rejection carries `_buy_box_reasons`.

    Rejected rows are RETURNED, not discarded — the caller is expected to report
    them. A lead that silently vanishes is indistinguishable from one that was
    never found.
    """
    kept, rejected = [], []
    for r in rows:
        ok, reasons = check_buy_box(r)
        if ok:
            kept.append(r)
        else:
            rejected.append({**r, "_buy_box_reasons": reasons})
    if rejected:
        logger.warning("buy box: %d of %d row(s) rejected before the CRM",
                       len(rejected), len(rows))
    return kept, rejected


def describe() -> str:
    """One-line summary of the active criteria, for run logs and reports."""
    return ("Buy box: single-family homes at minimum - must have a structure "
            "(no bare land), a residential account type, and must not be a "
            "condominium (unit-ownership legal description, or a unit "
            "designator in the street address). NOTE: duplex/triplex cannot be "
            "distinguished from free county data and will pass.")
