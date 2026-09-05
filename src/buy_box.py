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

Deliberately NOT rejected:

- **A duplex passes, on purpose.** User, 2026-09-04: *"I dont want condos but i
  guess id be ok with a duplex but mostly want sfr."* So single-family is the
  preference, a duplex is acceptable, and only condos are actually out. This
  used to be documented here as a limitation ("cannot be distinguished from the
  free data, a duplex will pass") — it is now the intended behaviour. **Do not
  "fix" it by adding duplex rejection.**

  The underlying detection limit is still real and still worth knowing:
  `AcctType` reads "Residential" for a duplex exactly as for a house, and the
  assessor's structure detail is rendered client-side from an endpoint that is
  not exposed (checked 2026-09-04). So the gate could not reject duplexes even
  if it wanted to.

  If SFR-vs-duplex is ever wanted as a *ranking* signal rather than a gate,
  DataSift's `Structure Type` is the place to get it, and it is available
  earlier than this file previously implied: enrichment is unmetered and runs
  during `--create`, i.e. BEFORE any skip-trace spend. Too late to keep a
  record out of the CRM, but in good time to deprioritise it or skip tracing
  it. That would be a preference sort, not a rejection.

`fail_open` is deliberate: a missing signal never rejects a record. A gate that
silently drops leads on absent data is worse than one that lets a few through,
because the loss is invisible. Same reasoning as `filter_buy_box()` in
`enrichment_pipeline.py`.

**One deliberate exception to fail-open: a missing PROPERTY ADDRESS (criterion
5).** That is not a buy-box judgement about a property — it is the row being
unusable, since a record cannot be created without one. It is still *reported*
rather than discarded, so the loss is not invisible, which is what the
principle above actually protects. Note that `_read_property_template()`
already drops rows lacking a street, so on the foreclosure path this criterion
is redundant; it earns its place on probate, where an address only appears
after the assessor step and its absence means that step found nothing.

Every other criterion fails open, and any new one must. If you are adding a
check and find yourself rejecting on absent data, that is the moment to
re-read this paragraph.
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

    # 4. The filing itself says there is no real property.
    #    Probate petitions state this explicitly and the wording is the
    #    discriminator: "an interest in real property" / "real and personal
    #    property" versus "leaving personal property". On a real batch the
    #    Hokanson estate used the last phrasing and an independent assessor
    #    search returned zero parcels — two free sources agreeing there is no
    #    house to buy. Policy exclusion, not a data defect, so it belongs here
    #    rather than blocking the whole batch in review.
    rp = str(row.get("Real Property Stated") or "").strip().lower()
    if rp.startswith("no") or ("personal property" in rp and "real" not in rp):
        reasons.append("the filing states NO real property - no house to buy")

    # 5. No property address could be resolved.
    #    After the assessor step this means nothing was found, so the row
    #    cannot become a CRM record. Report it; do not stop the batch.
    if not str(row.get("Property Street") or row.get("Property Street Address") or "").strip():
        reasons.append("no property address could be resolved")

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
    return ("Buy box: single-family preferred - must have a structure (no bare "
            "land), a residential account type, and must NOT be a condominium "
            "(unit-ownership legal description, or a unit designator in the "
            "street address). A duplex is acceptable and passes on purpose; "
            "only condos are rejected on property type.")
