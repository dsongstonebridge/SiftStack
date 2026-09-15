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

from buy_box import check_buy_box, describe as describe_buy_box

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


def _name_key_parts(name: str) -> tuple[str, str]:
    """First and last name TOKEN, generational suffixes stripped, lowercased -
    the same alpha-run tokenizer as `_title_tokens`, not a naive whitespace
    split.

    Using the same tokenizer on both sides matters: "D'Angelo Bitson" split
    naively keeps "d'angelo" as one token (apostrophe intact), while
    `_title_tokens` regex-extracts it as "d" and "angelo" - the two would
    never match a naive split's "d'angelo".

    Deliberately ignores middle names/initials - "Robert Clarence Lovelace"
    matches a title holder recorded as "LOVELACE, ROBERT C", which omits the
    middle name entirely. Requiring the full name would false-block the
    ordinary case.
    """
    toks = [t for t in re.findall(r"[a-z]+", name.lower())
            if len(t) > 1 and t not in {"jr", "sr", "ii", "iii", "iv", "v"}]
    if not toks:
        return "", ""
    return toks[0], toks[-1]


def _title_tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]+", s.lower()) if len(t) > 1}


def _heir_names(heirs_field: str) -> list[str]:
    """Names out of a "Name (Relationship); Name (Relationship)" blob."""
    names = []
    for chunk in (heirs_field or "").split(";"):
        name = chunk.split("(")[0].strip()
        if name:
            names.append(name)
    return names


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

        # ── buy box: excluded by policy, reported every time ─────────
        # Single-family homes at minimum: must have a structure and be
        # residential. A parcel can have a perfectly good street address and
        # still be an empty lot, so vacancy is checked directly rather than
        # inferred from whether an address exists —
        # `tulsa_assessor.get_parcel_improvements()` supplies that flag.
        # Rejections are reported, never silently dropped.
        ok, why = check_buy_box(r)
        if not ok:
            _flag(findings, i, EXCLUDE, "Buy Box",
                  "; ".join(why) + " - NOT uploaded to the CRM, but it IS an "
                  "estate asset worth knowing about",
                  " ".join(x for x in (str(r.get("Parcel ID") or ""),
                                       street or "(no street address)",
                                       f"land ${r.get('Land Value')}" if r.get("Land Value") else "")
                           if x))
            continue   # nothing else about this row matters; it is not becoming a record

        # ── structural: can this become a record at all ──────────────
        # NOTE: a missing property address and a filing that states no real
        # property are BUY-BOX exclusions (reported, batch continues), not
        # review defects. Blocking the whole batch on one policy-excluded row
        # stopped five good records on the 2026-09-04 test run.
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
                # A generational suffix is a REAL distinction between two
                # living-and-dead people: Gerald William Buckley Jr. is the
                # decedent, Gerald William Buckley III is his son and the PR.
                # _tokens() strips suffixes, so without this every Jr/Sr/III
                # probate blocks spuriously. Downgrade to a warning and name
                # the thing to check, rather than either blocking a valid row
                # or silently letting the real bug through.
                sfx = re.compile(r"\b(JR|SR|II|III|IV|V)\b", re.I)
                owner_sfx = set(m.upper() for m in sfx.findall(f"{first} {last}"))
                dec_sfx = set(m.upper() for m in sfx.findall(decedent))
                pr_sfx = set(m.upper() for m in sfx.findall(pr))
                if dec_sfx and (owner_sfx or pr_sfx) and (owner_sfx or pr_sfx) != dec_sfx:
                    _flag(findings, i, WARN, "Owner",
                          "Owner and decedent share a name but differ by generational "
                          "suffix - confirm the contact is the LIVING one",
                          f"{first} {last} ({'/'.join(sorted(owner_sfx or pr_sfx))}) "
                          f"vs decedent ({'/'.join(sorted(dec_sfx))})")
                else:
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

        # ── not-straightforward probate property: STOP AND ASK ───────
        # User, 2026-09-11 (Johnson incident) + 2026-09-14 (video walkthrough
        # of Chu/Malick/Bitson): "On all the situations that aren't
        # straightforward, you need to stop and ask me." Straightforward means
        # the title holder of record resolves to someone ORDINARY and
        # EXPECTED - the decedent, their own trust, or ANY named PR/heir
        # (Bitson: the living spouse held title and was never in the
        # decedent's own name at all) - AND there is no sign the property
        # passed through a transfer connected to the estate to get there
        # (Chu/Johnson: the current holder got there via a transfer FROM
        # someone named in the filing). Everything else BLOCKs until the
        # sheet marks "Property Confirmed" = Yes - set by a human, never
        # inferred by this code.
        if is_probate and street:
            title_holder = str(r.get("Title Holder of Record") or "").strip()
            insider_transfer = str(r.get("Insider Transfer") or "").strip()
            confirmed = str(r.get("Property Confirmed") or "").strip().lower() == "yes"
            sev = WARN if confirmed else BLOCK
            tail = " (user confirmed)" if confirmed else ""

            if not title_holder:
                _flag(findings, i, sev, "Title Holder of Record",
                      "NOT STRAIGHTFORWARD - no title holder of record was "
                      "found for this property; the decedent's ownership has "
                      "not been confirmed" + tail, street)
            else:
                title_tok = _title_tokens(title_holder)

                def _matches(name: str) -> bool:
                    f, l = _name_key_parts(name)
                    return bool(f) and bool(l) and f in title_tok and l in title_tok

                named = [decedent, pr, f"{first} {last}".strip()]
                named.extend(_heir_names(str(r.get("Heirs") or "")))
                title_matches_a_named_party = any(_matches(n) for n in named if n)

                if not title_matches_a_named_party:
                    _flag(findings, i, sev, "Title Holder of Record",
                          f"NOT STRAIGHTFORWARD - title holder of record is "
                          f"{title_holder!r}, not the decedent, the PR, or any "
                          f"named heir ({decedent or 'unknown'}). Confirm the "
                          "decedent's estate actually held this property "
                          "before creating the record" + tail, title_holder)
                    # The exact Johnson defect: the heir's/PR's own mailing
                    # address got used AS the decedent's property address,
                    # with no title-holder confirmation behind it.
                    if mail and street and mail.lower() == street.lower():
                        _flag(findings, i, sev, "Mailing Street",
                              "The heir's/PR's MAILING address is being used "
                              "as the PROPERTY address with no confirmed title "
                              "holder - this is the exact defect that produced "
                              "a bogus record (Johnson, 2026-09-11)" + tail,
                              mail)
                elif insider_transfer:
                    # Matching a named PR/heir is NOT the same question as
                    # "did this property reach them through an ordinary path."
                    # Video, 2026-09-14: Larry Kaiser (Johnson's petitioner)
                    # quit-claimed the property to L&S Group LLC for $0 in
                    # 2013, years before the estate existed - a transfer
                    # connected to the estate is the real messy signal, even
                    # though the eventual holder (L&S) isn't itself a named
                    # party here and even when it IS.
                    _flag(findings, i, sev, "Title Holder of Record",
                          "NOT STRAIGHTFORWARD - the property passed through a "
                          "transfer connected to this estate before reaching "
                          "its current holder: " + insider_transfer + tail,
                          title_holder)

        # NOTE: a Treasurer fallback discovery (main._treasurer_true_negative_
        # check(), 2026-09-15) writes Property Street + Title Holder of Record
        # directly when it finds a corroborated hit the Assessor missed - it
        # flows through the SAME "if is_probate and street" check above, not
        # a separate branch. A note-only field here would never be seen: any
        # row with no Property Street is already excluded by check_buy_box's
        # "no property address could be resolved" rule, above, before this
        # point in the loop.

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
        logger.warning("=== OUTSIDE THE BUY BOX - NOT UPLOADED, BUT YOU SHOULD KNOW (%d) ===",
                       len(excluded))
        for f in excluded:
            logger.warning("  row %d: %s", f["row"], f["value"] or f["message"])
            logger.warning("      reason: %s", f["message"].split(" - NOT uploaded")[0])
        logger.warning("  %s", describe_buy_box())
        logger.warning("  These are still real estate assets - note them if you are "
                       "valuing a whole estate.")
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
