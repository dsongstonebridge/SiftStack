"""Human check step: review an extracted batch BEFORE anything reaches the CRM.

Why this exists
---------------
`skip-trace --create` writes to the CRM whether or not `--commit` is passed —
the dry-run gate protects spend, not the CRM. So the last place a person can
catch a bad row is here, on the extracted sheet, before creation.

It matters more for probate than foreclosure because the source documents are
scanned court filings and OCR damages exactly the fields that feed skip trace:
on the 2026-09-02 Tulsa batch it rendered `Faulk` as `Haulk` and
`7635 E. 49th St.` as `7635 E. 49" St.`. Phone tags are append-only, so a
trace against a mangled name cannot be cleanly undone.

Every check here is free and local — no API calls, no spend.

Usage
-----
    from batch_review import review_batch, print_review
    findings = review_batch(rows, notice_type="probate")
    print_review(findings)          # -> True if it is safe to proceed
"""

from __future__ import annotations

import csv
import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

#: Severity levels. BLOCK means do not create the record; WARN means a human
#: should look but the row is structurally usable.
BLOCK = "BLOCK"
WARN = "WARN"
#: Not a defect — a deliberate policy exclusion that must still be surfaced.
#: Bare land never reaches the CRM (the user buys houses), but every excluded
#: lot is a real estate asset and gets reported EVERY TIME, never dropped
#: quietly. Standing instruction, 2026-09-04.
EXCLUDE = "EXCLUDE"

#: Characters that should never appear inside a person's name. A double quote
#: is the classic Tesseract rendering of a superscript ordinal ("49th" ->
#: '49"'), and digits in a surname almost always mean the OCR merged a line.
_BAD_NAME_CHARS = re.compile(r'["\'`~^*|\\<>{}\[\]_]|\d')

#: "49" St", "3'" Ave", "12'" day" — an ordinal whose suffix OCR destroyed.
_BROKEN_ORDINAL = re.compile(r"\b\d+\s*['\"`]+\s*(?:St|Ave|Av|Street|Avenue|Pl|Ct|Dr|Rd|day)\b",
                             re.IGNORECASE)

#: A street that never got a number, e.g. a legal description leaking through.
_NO_HOUSE_NUMBER = re.compile(r"^\s*(?!\d)")

_SUSPICIOUS_OCR_WORDS = (
    "Haulk",       # observed: Faulk -> Haulk
    "l1", "0f", "rn ",
)


def _flag(findings: list[dict], row_no: int, sev: str, field: str,
          message: str, value: str = "") -> None:
    findings.append({"row": row_no, "severity": sev, "field": field,
                     "message": message, "value": str(value)[:80]})


def review_batch(rows: list[dict], *, notice_type: str = "probate") -> list[dict]:
    """Check an extracted batch and return findings, most severe first.

    `rows` are raw property-template rows (the same shape
    `_read_property_template()` produces), NOT DataSift CSV rows — the point is
    to catch problems before formatting, while the source columns are intact.
    """
    findings: list[dict] = []
    is_probate = (notice_type or "").strip().lower() == "probate"

    seen_addresses: dict[str, int] = {}
    seen_people: dict[str, list[int]] = {}

    for i, r in enumerate(rows, start=1):
        street = str(r.get("Property Street") or "").strip()
        city = str(r.get("Property City") or "").strip()
        first = str(r.get("First Name") or "").strip()
        last = str(r.get("Last Name") or "").strip()
        mail = str(r.get("Mailing Street") or r.get("Owner Mailing Street") or "").strip()
        decedent = str(r.get("Decedent Name") or "").strip()
        pr = str(r.get("Personal Representative") or "").strip()

        # ── bare land: excluded by policy, reported every time ───────
        # A parcel can have a perfectly good street address and still be an
        # empty lot, so this is checked on its own and never inferred from
        # whether an address exists. `tulsa_assessor.get_parcel_improvements()`
        # supplies the flag.
        vacant = str(r.get("Vacant Lot") or r.get("is_vacant_lot") or "").strip().lower()
        if vacant in ("yes", "true", "1"):
            _flag(findings, i, EXCLUDE, "Vacant Lot",
                  "Empty lot, no structure - NOT uploaded to the CRM, but it IS "
                  "an estate asset worth knowing about",
                  " ".join(x for x in (str(r.get("Parcel ID") or ""),
                                       street or "(no street address)",
                                       f"land ${r.get('Land Value')}" if r.get("Land Value") else "")
                           if x))
            continue   # nothing else about this row matters; it is not becoming a record

        # ── structural: can this become a record at all ──────────────
        if not street:
            _flag(findings, i, BLOCK, "Property Street",
                  "No property address - row will be DROPPED, asset lost unless "
                  "attached to another record as an additional parcel",
                  r.get("Parcel ID") or r.get("Case Number") or "")
        if not last:
            _flag(findings, i, BLOCK, "Last Name", "No owner surname - cannot create or trace")

        # ── the recurring probate bug ────────────────────────────────
        # Compare NAME TOKENS, not substrings. A plain `in` test misses
        # "Karen Castellanos" inside "Karen D. Castellanos" because of the
        # middle initial — which is the single most common way this bug
        # actually appears. See the dm-contact-pattern memory: putting the
        # deceased in the Owner field is the longest-running defect in this
        # project, so this check has to be hard to fool.
        if is_probate and decedent and first and last:
            def _tokens(s: str) -> set[str]:
                return {t for t in re.findall(r"[a-z]+", s.lower())
                        if len(t) > 1 and t not in {"jr", "sr", "ii", "iii", "iv"}}
            owner_tok = _tokens(f"{first} {last}")
            dec_tok = _tokens(decedent)
            if owner_tok and owner_tok <= dec_tok:
                _flag(findings, i, BLOCK, "Owner",
                      "OWNER IS THE DECEDENT. The contact must be the PR or an "
                      "heir - never the deceased owner of record",
                      f"{first} {last} / {decedent}")
            # Deliberately NOT flagged: an owner who merely shares the
            # decedent's surname. A surviving spouse or child with the same
            # last name is the ordinary probate case (Julienne Lovelace is the
            # correct PR for Robert Clarence Lovelace), so warning on it would
            # fire on most rows and teach the reviewer to skim past warnings.
            # Only full-name containment is a real defect.

        # ── OCR damage in fields that feed skip trace ────────────────
        for label, val in (("First Name", first), ("Last Name", last)):
            if val and _BAD_NAME_CHARS.search(val):
                _flag(findings, i, BLOCK, label,
                      "Name contains characters OCR usually invents - verify "
                      "against the source PDF before tracing (phone tags are "
                      "append-only)", val)
        for label, val in (("Property Street", street), ("Mailing Street", mail)):
            if val and _BROKEN_ORDINAL.search(val):
                _flag(findings, i, BLOCK, label,
                      "Broken ordinal suffix (OCR ate the 'th'/'rd') - the "
                      "address will not match", val)
        blob = " ".join(str(v) for v in r.values())
        for w in _SUSPICIOUS_OCR_WORDS:
            if w in blob:
                _flag(findings, i, WARN, "OCR",
                      f"Contains {w!r}, a known OCR misread - check the source", w)
                break

        # ── mailing address: probate must not inherit the property ───
        if is_probate:
            if not mail:
                _flag(findings, i, WARN, "Mailing Street",
                      "No mailing address for the PR/heir. The trace will fall "
                      "back to the PROPERTY address, which they usually do not "
                      "live at - expect a miss you still pay for")
            elif street and mail.lower() == street.lower():
                _flag(findings, i, WARN, "Mailing Street",
                      "Mailing address equals the property address. Correct only "
                      "if the PR/heir actually lives in the decedent's house",
                      mail)
            if mail.upper().replace(".", "").startswith(("PO BOX", "P O BOX")):
                _flag(findings, i, WARN, "Mailing Street",
                      "PO Box only - usable for mail, weak for skip trace "
                      "matching; a physical address would trace better", mail)

        # ── probate completeness ─────────────────────────────────────
        if is_probate:
            if not decedent:
                _flag(findings, i, WARN, "Decedent Name",
                      "No decedent recorded - the CRM record loses its probate context")
            if not pr:
                _flag(findings, i, WARN, "Personal Representative",
                      "No PR recorded - cannot show a signing chain to the caller")
            rp = str(r.get("Real Property Stated") or "").strip().lower()
            if rp.startswith("no") or "personal property" in rp:
                _flag(findings, i, BLOCK, "Real Property Stated",
                      "Filing states NO real property - this estate has no house "
                      "to buy and should not reach the CRM", rp)
            unknown = str(r.get("heirs_address_unknown") or "").strip()
            if unknown:
                _flag(findings, i, WARN, "Heirs",
                      "An heir has no address on file - must be located before "
                      "closing", unknown)

        # ── duplicates within the batch ──────────────────────────────
        if street:
            key = f"{street.lower()}|{city.lower()}"
            if key in seen_addresses:
                _flag(findings, i, BLOCK, "Property Street",
                      f"Duplicate of row {seen_addresses[key]} - would create two "
                      f"CRM records for one property", street)
            else:
                seen_addresses[key] = i
        if first and last:
            seen_people.setdefault(f"{first.lower()}|{last.lower()}", []).append(i)

    for person, rws in seen_people.items():
        if len(rws) > 1:
            nm = person.replace("|", " ").title()
            _flag(findings, rws[0], WARN, "Owner",
                  f"{nm} is the contact on rows {rws} - traced ONCE and applied to "
                  f"all (billing is deduped); confirm they really are the right "
                  f"contact for each property")

    order = {BLOCK: 0, EXCLUDE: 1, WARN: 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["row"]))
    return findings


def write_review_sheet(rows: list[dict], findings: list[dict],
                       output_path: str | Path | None = None) -> Path:
    """Write the batch plus its findings to a CSV a human can read and edit."""
    if output_path is None:
        output_path = (Path("output") /
                       f"REVIEW_batch_{datetime.now().strftime('%Y-%m-%d_%H%M')}.csv")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    by_row: dict[int, list[str]] = {}
    for f in findings:
        by_row.setdefault(f["row"], []).append(
            f"[{f['severity']}] {f['field']}: {f['message']}")

    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)

    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Row", "Verdict", "Findings"] + cols)
        for i, r in enumerate(rows, start=1):
            notes = by_row.get(i, [])
            verdict = ("BLOCK" if any(n.startswith("[BLOCK]") for n in notes)
                       else "EXCLUDED-VACANT" if any(n.startswith("[EXCLUDE]") for n in notes)
                       else "REVIEW" if notes else "OK")
            w.writerow([i, verdict, " | ".join(notes)] +
                       [r.get(c, "") for c in cols])
    logger.info("Review sheet: %s", output_path)
    return output_path


def print_review(findings: list[dict], total_rows: int = 0) -> bool:
    """Print findings. Returns True when nothing BLOCKs.

    A clean pass over ZERO rows is not a pass — an empty batch returns False
    rather than a misleading green.
    """
    if not total_rows:
        logger.warning("REVIEW: no rows to check - nothing to create")
        return False

    blocks = [f for f in findings if f["severity"] == BLOCK]
    warns = [f for f in findings if f["severity"] == WARN]
    excluded = [f for f in findings if f["severity"] == EXCLUDE]

    logger.info("REVIEW: %d row(s), %d blocking, %d warning(s), %d excluded",
                total_rows, len(blocks), len(warns), len(excluded))

    # Bare land gets its own banner. The user asked to be told about every
    # empty lot even though none of them go to the CRM — buried in a list of
    # warnings is the same as not being told.
    if excluded:
        logger.warning("")
        logger.warning("=== EMPTY LOTS - NOT UPLOADED, BUT YOU SHOULD KNOW (%d) ===",
                       len(excluded))
        for f in excluded:
            logger.warning("  row %d: %s", f["row"], f["value"] or f["message"])
        logger.warning("  These are estate assets with no house on them. They do not "
                       "become CRM records; note them if you are valuing the whole estate.")
        logger.warning("")

    for f in blocks + warns:
        logger.log(logging.ERROR if f["severity"] == BLOCK else logging.WARNING,
                   "  [%s] row %d %s: %s%s", f["severity"], f["row"], f["field"],
                   f["message"], f" ({f['value']})" if f["value"] else "")
    if blocks:
        logger.error("REVIEW FAILED: %d blocking issue(s). Fix the sheet and "
                     "re-run before creating records.", len(blocks))
        return False
    logger.info("REVIEW PASSED: no blocking issues across %d row(s).", total_rows)
    return True
