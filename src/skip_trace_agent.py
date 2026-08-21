"""Skip-trace enrichment: resolve -> merge -> score -> write back to DataSift.

Adapted from Tyler Austin's FCRE `skip-trace-agent` plugin (stages 3-6), which
is the part of his pipeline that is provider-agnostic. His stages 1-2
(SmartSkip export + DirectSkip API) are deliberately NOT ported: DirectSkip is
an account we do not have, and Tracerfy cannot substitute for it because it is a
contact lookup, not a relatives-discovery service. Our discovery is whatever
source feeds `subjects` here.

WHY THE MULTI-SOURCE SHAPE SURVIVES ON A SINGLE SOURCE
------------------------------------------------------
Every person carries `sources: [...]` as a LIST and every phone carries its own
`sources: [...]`, even though today only "Tracerfy" ever appears in them. This
looks like pointless generality and is not: collapsing it to a scalar is the one
change that would make adding DirectSkip/SmartSkip later a rewrite instead of a
new adapter. The merge step likewise keeps its cross-source overlap logic, which
is currently a no-op. Leave both alone.

To add a source later: write an adapter emitting `Subject`/`Person` as below,
pass it in the `by_source` dict, and the merge, scoring and writeback need no
changes. `skills/skip-trace-agent/scripts/directskip_trace.py` is kept in the
tree unmodified for exactly that purpose and is never called today.

CONTRACT (matches his parse_smartskip.py output so his scripts stay drop-in)
---------------------------------------------------------------------------
Subject: {
  property_uuid, owner_uuid, first, last, name,
  property_address, property_city, property_state, property_zip,
  people: [Person], has_results: bool,
}
Person: {
  first, last, name, key, relationship, age, deceased, is_primary,
  mailing_street, mailing_city, mailing_state,
  sources: ["Tracerfy", ...],
  phones: [{number, sources: [...], type_raw, tier, score}],
  emails: [str],
}
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Iterable

import config
import datasift_api as _api

logger = logging.getLogger(__name__)

SOURCE_TRACERFY = "Tracerfy"
SOURCE_DATASIFT = "DataSift"          # found by DataSift's own skip trace

#: The DOUBLE SKIP TRACE, verified end to end on a real record 2026-08-21.
#: Tracerfy and DataSift genuinely return different numbers - on the proving
#: run Tracerfy found two live Tulsa mobiles (both scored 100) and DataSift
#: found an OKC number Tracerfy missed. Running only one loses real coverage,
#: which is why both are in the default sequence. Each number is tagged with
#: the source that found it, so a caller can always tell where it came from.
#:
#: Order matters: Tracerfy FIRST, then DataSift, then score EVERYTHING. Scoring
#: before the second source means paying Trestle twice or leaving the second
#: source's numbers untiered.

#: Property tags written after each stage. `TrestleIQ Scored` is applied ONLY
#: when numbers were actually scored — never on a zero-result record. That is
#: his rule and it matters: the tag is how you find records that still need
#: scoring, so a false positive hides work.
TAG_TRACERFY_SKIPPED = "Tracerfy Skipped"
TAG_TRESTLE_SCORED = "TrestleIQ Scored"

_PHONE_FIELDS = ["primary_phone", "mobile_1", "mobile_2", "mobile_3", "mobile_4",
                 "mobile_5", "landline_1", "landline_2", "landline_3"]
_EMAIL_FIELDS = ["email_1", "email_2", "email_3", "email_4", "email_5"]


# ── helpers ───────────────────────────────────────────────────────────

def norm_phone(raw) -> str | None:
    """10-digit form, or None. Strips a leading US 1."""
    d = re.sub(r"\D", "", str(raw or ""))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else None


def person_key(first: str, last: str) -> str:
    return f"{(first or '').strip().upper()}|{(last or '').strip().upper()}"


def _phone_type_from_field(field: str) -> str:
    if field.startswith("mobile"):
        return "Mobile"
    if field.startswith("landline"):
        return "Landline"
    return ""


# ── Stage 3: resolve CRM records ──────────────────────────────────────

def resolve_subjects(rows: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """Map each input row to its CRM record and build the Subject shell.

    `rows` need `street`, and may carry `city`, `first`, `last`.

    Uses `datasift_api.find_property_by_address()`, which is a real server-side
    search (POST-as-GET). It is read-only — importantly NOT the duplicate-400
    trick, which creates an invisible orphan whenever the address turns out not
    to exist, i.e. exactly when a lookup should simply return nothing.

    Returns (subjects, unresolved).
    """
    subjects, unresolved = [], []
    for row in rows:
        street = (row.get("street") or "").strip()
        city = (row.get("city") or "").strip()
        if not street:
            unresolved.append({**row, "reason": "no street"})
            continue

        hit = _api.find_property_by_address(street, city)
        if not hit:
            unresolved.append({**row, "reason": "no CRM record matched"})
            logger.warning("resolve: no record for %r / %r", street, city)
            continue

        # The search result is a LIST object and does NOT carry the owner's
        # phones — re-read the detail. Skipping this makes an already-traced
        # record look like it has no numbers, which silently drops existing
        # phones from the merge and posts a "no numbers returned" board post
        # over real data. Caught by a dry run 2026-08-21.
        try:
            rec = _api.get_property(hit["uuid"])
        except _api.DataSiftAPIError as e:
            logger.warning("resolve: detail read failed for %s: %s", hit.get("uuid"), e)
            unresolved.append({**row, "reason": f"detail read failed: {e}"})
            continue

        addr = rec.get("address") or {}
        owner = rec.get("owner") or {}
        first = (row.get("first") or owner.get("first_name") or "").strip()
        last = (row.get("last") or owner.get("last_name") or "").strip()

        subjects.append({
            "property_uuid": rec.get("uuid"),
            "owner_uuid": owner.get("uuid"),
            "first": first, "last": last,
            "name": f"{first} {last}".strip(),
            "property_address": addr.get("street") or street,
            "property_city": addr.get("city") or city,
            "property_state": addr.get("state") or "",
            "property_zip": addr.get("postal_code") or "",
            "existing_phones": _existing_phones(rec),
            "people": [],
            "has_results": False,
        })
    logger.info("resolve: %d subject(s), %d unresolved", len(subjects), len(unresolved))
    return subjects, unresolved


def _existing_phones(rec: dict) -> list[dict]:
    """Numbers already on the record. Tagged `DataSift` on writeback so a
    caller can tell pre-existing bulk data from what we just found."""
    out = []
    for p in ((rec.get("owner") or {}).get("phones") or []):
        n = norm_phone(p.get("number") if isinstance(p, dict) else p)
        if n:
            out.append({"number": n, "sources": [SOURCE_DATASIFT],
                        "type_raw": (p.get("type") or "") if isinstance(p, dict) else "",
                        "tier": None, "score": None})
    return out


# ── Adapter: Tracerfy -> the contract ─────────────────────────────────

def tracerfy_source(subjects: list[dict], *, dry_run: bool = True) -> dict[str, list[dict]]:
    """Run Tracerfy for each subject's owner and emit people in the contract.

    BILLED: Tracerfy charges ~$0.02 per record SUBMITTED — on misses too. The
    count is logged before anything is sent.

    Returns {property_uuid: [Person]}. Tracerfy returns contacts for the person
    asked about and does NOT return relatives, so each subject yields exactly
    one Person, `is_primary=True`, with no relationship tag (his rule: the
    owner's own numbers carry source + tier only, never a relationship).
    """
    from tracerfy_skip_tracer import trace_contacts

    contacts, index = [], {}
    for s in subjects:
        if not (s["first"] and s["last"]):
            logger.warning("tracerfy: skipping %s - no owner name", s["property_address"])
            continue
        contacts.append({
            "first_name": s["first"], "last_name": s["last"],
            "address": s["property_address"], "city": s["property_city"],
            "state": s["property_state"], "zip": s["property_zip"],
        })
        index[person_key(s["first"], s["last"])] = s

    if not contacts:
        return {}

    logger.warning("BILLED: submitting %d record(s) to Tracerfy (~$%.2f at $0.02/record)",
                    len(contacts), len(contacts) * 0.02)
    if dry_run:
        logger.info("DRY RUN - not calling Tracerfy")
        return {}

    records = trace_contacts(contacts)
    logger.info("tracerfy: %d record(s) returned", len(records))

    people_by_subject: dict[str, list[dict]] = {}
    for rec in records:
        first = (rec.get("first_name") or "").strip()
        last = (rec.get("last_name") or "").strip()
        subj = index.get(person_key(first, last))
        if not subj:
            logger.warning("tracerfy: result for %s %s matched no subject", first, last)
            continue

        phones = []
        for field in _PHONE_FIELDS:
            n = norm_phone(rec.get(field))
            if n and not any(p["number"] == n for p in phones):
                phones.append({"number": n, "sources": [SOURCE_TRACERFY],
                               "type_raw": _phone_type_from_field(field),
                               "tier": None, "score": None})
        emails = [e for e in (rec.get(f) for f in _EMAIL_FIELDS) if e]

        people_by_subject.setdefault(subj["property_uuid"], []).append({
            "first": first, "last": last, "name": f"{first} {last}".strip(),
            "key": person_key(first, last),
            "relationship": None,        # owner: source + tier only
            "age": rec.get("age") or "",
            "deceased": False,
            "is_primary": True,
            "mailing_street": rec.get("address") or "",
            "mailing_city": rec.get("city") or "",
            "mailing_state": rec.get("state") or "",
            "sources": [SOURCE_TRACERFY],
            "phones": phones,
            "emails": emails,
        })
    return people_by_subject


def datasift_source(subjects: list[dict], *, dry_run: bool = True,
                     poll_seconds: float = 15.0,
                     timeout_seconds: float = 300.0) -> dict[str, list[dict]]:
    """DataSift's own skip trace, SCOPED to these records. Second half of the
    double skip trace.

    BILLED: prepaid credits, ~$0.12/owner. Runs the free estimate first and
    REFUSES if it would touch more records than we asked for — the payload has
    to be nested correctly or the endpoint silently goes account-wide (see
    datasift_api._skip_trace_body).

    Returns {property_uuid: [Person]} for numbers that are NEW after the trace,
    so they get the DataSift source tag rather than Tracerfy's.

    Asynchronous and not fast: observed 12s on one record and 150s on another.
    Poll, don't assume failure.
    """
    targets = [s for s in subjects if s.get("property_uuid")]
    if not targets:
        return {}

    uuids = [s["property_uuid"] for s in targets]
    before = {s["property_uuid"]: {ph["number"] for p in s["people"] for ph in p["phones"]}
              for s in targets}

    est = _api.estimate_skip_trace(uuids, address_prefix=targets[0]["property_address"]
                                    if len(targets) == 1 else "")
    logger.warning("BILLED: DataSift skip trace - %s record(s), est. cost %s",
                    est.get("number_of_records"), est.get("cost"))
    if dry_run:
        logger.info("DRY RUN - not submitting DataSift skip trace")
        return {}

    _api.submit_skip_trace(uuids, max_records=len(uuids),
                            address_prefix=targets[0]["property_address"]
                            if len(targets) == 1 else "")

    import time
    deadline = time.monotonic() + timeout_seconds
    done: set[str] = set()
    while time.monotonic() < deadline and len(done) < len(targets):
        time.sleep(poll_seconds)
        for s in targets:
            if s["property_uuid"] in done:
                continue
            rec = _api.get_property(s["property_uuid"])
            if (rec.get("owner") or {}).get("skiptrace_attempts"):
                done.add(s["property_uuid"])
    if len(done) < len(targets):
        logger.warning("datasift_source: %d/%d finished within %.0fs - the rest may "
                        "still be running; re-read later rather than re-submitting "
                        "(a re-submit is a second charge)",
                        len(done), len(targets), timeout_seconds)

    out: dict[str, list[dict]] = {}
    for s in targets:
        rec = _api.get_property(s["property_uuid"])
        owner = rec.get("owner") or {}
        new_phones = []
        for ph in (owner.get("phones") or []):
            n = norm_phone(ph.get("number"))
            if n and n not in before[s["property_uuid"]]:
                new_phones.append({"number": n, "sources": [SOURCE_DATASIFT],
                                   "type_raw": (ph.get("type") or "").title(),
                                   "tier": None, "score": None})
        if not new_phones:
            continue
        out[s["property_uuid"]] = [{
            "first": s["first"], "last": s["last"], "name": s["name"],
            "key": person_key(s["first"], s["last"]),
            "relationship": None, "age": "", "deceased": False, "is_primary": True,
            "mailing_street": "", "mailing_city": "", "mailing_state": "",
            "sources": [SOURCE_DATASIFT], "phones": new_phones,
            "emails": [e for e in (owner.get("emails") or []) if isinstance(e, str)],
        }]
        logger.info("datasift_source: %s -> %d new number(s)",
                     s["property_address"], len(new_phones))
    return out


# ── Stage 4: merge ────────────────────────────────────────────────────

def merge_sources(subjects: list[dict],
                   by_source: dict[str, dict[str, list[dict]]]) -> list[dict]:
    """Fold every source's people into each subject, unioning `sources` on both
    people and phones.

    Currently there is one source, so no overlap is ever found. The logic stays
    anyway — see the module docstring. When a second source is added, a person
    found by both ends up with `sources: ["Tracerfy", "DirectSkip"]` and a phone
    found by both carries both tags side by side. There is deliberately no
    "BOTH" tag; that is his rule and it keeps the vocabulary closed.
    """
    by_uuid = {s["property_uuid"]: s for s in subjects}

    for source_name, people_by_subject in by_source.items():
        for prop_uuid, people in people_by_subject.items():
            subj = by_uuid.get(prop_uuid)
            if not subj:
                continue
            for incoming in people:
                existing = next((p for p in subj["people"]
                                 if p["key"] == incoming["key"]), None)
                if existing is None:
                    subj["people"].append(incoming)
                    continue
                # Same person from another source: union sources and phones.
                for s in incoming["sources"]:
                    if s not in existing["sources"]:
                        existing["sources"].append(s)
                for ph in incoming["phones"]:
                    match = next((q for q in existing["phones"]
                                  if q["number"] == ph["number"]), None)
                    if match:
                        for s in ph["sources"]:
                            if s not in match["sources"]:
                                match["sources"].append(s)
                    else:
                        existing["phones"].append(ph)
                for e in incoming["emails"]:
                    if e not in existing["emails"]:
                        existing["emails"].append(e)
                existing["relationship"] = existing["relationship"] or incoming["relationship"]

    for subj in subjects:
        # Pre-existing record numbers join the owner's person so they get
        # scored and tiered along with everything else.
        primary = next((p for p in subj["people"] if p["is_primary"]), None)
        if primary is None and subj.get("existing_phones"):
            # No source returned this owner (a miss, or a dry run), but the
            # record already HAS numbers. Synthesize the owner as a person so
            # they are not dropped — otherwise the board posts "no numbers
            # returned" over a record that has some. Caught by a dry run
            # 2026-08-21.
            primary = {
                "first": subj["first"], "last": subj["last"], "name": subj["name"],
                "key": person_key(subj["first"], subj["last"]),
                "relationship": None, "age": "", "deceased": False,
                "is_primary": True, "mailing_street": "", "mailing_city": "",
                "mailing_state": "", "sources": [], "phones": [], "emails": [],
            }
            subj["people"].append(primary)
        for ph in subj.get("existing_phones", []):
            if primary is None:
                break
            if not any(q["number"] == ph["number"] for q in primary["phones"]):
                primary["phones"].append(ph)
        subj["has_results"] = any(p["phones"] for p in subj["people"])
    return subjects


# ── Stage 5: Trestle scoring ──────────────────────────────────────────

def score_phones(subjects: list[dict], *, dry_run: bool = True) -> dict[str, dict]:
    """Score every unique number once, globally deduped across all subjects.

    BILLED: TrestleIQ ~$0.015 per unique number. Deduping globally is his main
    cost lever and it is free to do — a number's tier is the same wherever it
    appears, so validating it per-record just pays repeatedly for one answer.

    Writes the tier/score back onto each phone dict in place and returns
    {number: {"tier", "score", "type"}}.
    """
    from phone_validator import call_trestle, assign_tier, DEFAULT_TIERS

    unique: set[str] = set()
    for s in subjects:
        for p in s["people"]:
            for ph in p["phones"]:
                unique.add(ph["number"])

    logger.warning("BILLED: TrestleIQ scoring %d unique number(s) (~$%.2f at $0.015 each)",
                    len(unique), len(unique) * 0.015)
    if dry_run:
        logger.info("DRY RUN - not calling TrestleIQ")
        return {}

    api_key = config.TRESTLE_API_KEY
    if not api_key:
        logger.error("TRESTLE_API_KEY not set - cannot score")
        return {}

    results: dict[str, dict] = {}
    for i, number in enumerate(sorted(unique), 1):
        try:
            data = call_trestle(number, api_key) or {}
        except Exception as e:                    # noqa: BLE001 - never lose a batch to one number
            logger.warning("trestle: %s failed: %s", number, e)
            continue
        if data.get("error"):
            logger.warning("trestle: %s -> %s", number, data["error"])
            continue
        score = data.get("activity_score")
        results[number] = {
            "tier": assign_tier(score, DEFAULT_TIERS),
            "score": score,
            "type": (data.get("line_type") or "").title(),
        }
        if i % 25 == 0:
            logger.info("trestle: %d/%d", i, len(unique))

    for s in subjects:
        for p in s["people"]:
            for ph in p["phones"]:
                r = results.get(ph["number"])
                if r:
                    ph["tier"] = r["tier"]
                    ph["score"] = r["score"]
                    ph["type_raw"] = ph["type_raw"] or r["type"]
    return results


# ── Stage 6: message board + writeback ────────────────────────────────

_GROUP_ORDER = ["Son", "Daughter", "Child", "Mother", "Father", "Parent",
                "Brother", "Sister", "Sibling", "Husband", "Wife", "Spouse",
                "In-Law", "Relative"]
_GROUP_LABEL = {"Son": "Sons", "Daughter": "Daughters", "Child": "Children",
                "Mother": "Parents", "Father": "Parents", "Parent": "Parents",
                "Brother": "Siblings", "Sister": "Siblings", "Sibling": "Siblings",
                "Husband": "Spouse", "Wife": "Spouse", "Spouse": "Spouse",
                "In-Law": "In-Laws", "Relative": "Other Relatives"}


def _last4s(phones: list[dict]) -> str:
    """Last-4 display, his method: never print a full number or area code on
    the board. Falls back to last-5 when two of a person's numbers collide."""
    seen, out = {}, []
    for ph in phones:
        n = ph["number"]
        tail = n[-4:]
        if tail in seen and seen[tail] != n:
            out.append(n[-5:])
        else:
            seen[tail] = n
            out.append(tail)
    return ", ".join(out)


def build_message_board(subject: dict, *, sources: list[str]) -> str:
    """One combined post per record, in his house format.

    Names live here and NEVER on a phone tag; the last-4 method is what lets a
    caller tie a number back to a person without the board becoming a phone
    dump.
    """
    src = " + ".join(sources) if sources else "Skip trace"
    stamp = datetime.now().strftime("%m/%d/%Y")
    if not subject["has_results"]:
        return f"{src} attempted {stamp} - no numbers returned."

    lines = [f"{src} - {stamp}", ""]
    primary = next((p for p in subject["people"] if p["is_primary"]), None)
    if primary:
        lines.append(f"{primary['name'].upper()}: {_last4s(primary['phones'])}"
                     if primary["phones"] else f"{primary['name'].upper()}: - no phones")
        lines.append("")

    others = [p for p in subject["people"] if not p["is_primary"]]
    groups: dict[str, list[dict]] = {}
    for p in others:
        groups.setdefault(_GROUP_LABEL.get(p["relationship"] or "Relative",
                                            "Other Relatives"), []).append(p)

    ordered, seen = [], set()
    for rel in _GROUP_ORDER:
        label = _GROUP_LABEL[rel]
        if label in groups and label not in seen:
            ordered.append(label)
            seen.add(label)
    ordered += [g for g in groups if g not in seen]

    owner_name = (primary or subject)["name"].upper()
    for label in ordered:
        header = label if label == "Other Relatives" else f"{label} of {owner_name}"
        lines.append(f"{header}:")
        for p in groups[label]:
            tail = _last4s(p["phones"]) if p["phones"] else "- no phones"
            lines.append(f"  {p['name'].upper()} - {tail}")
            if p.get("mailing_street"):
                city = ", ".join(x for x in (p.get("mailing_city"),
                                              p.get("mailing_state")) if x)
                lines.append(f"  {p['mailing_street']}{', ' + city if city else ''}")
        lines.append("")
    return "\n".join(lines).rstrip()


def writeback(subjects: list[dict], *, sources: list[str],
               dry_run: bool = True) -> dict:
    """Write phones + tags + one message-board post per record.

    Honors `dry_run` by flipping datasift_api's global switch, so nothing can
    slip through a code path that forgot to check.
    """
    _api.set_dry_run(dry_run)
    result = {"records": 0, "phones": 0, "tagged": 0, "posted": 0, "skipped": []}
    try:
        for s in subjects:
            uuid, owner_uuid = s["property_uuid"], s["owner_uuid"]
            if not owner_uuid:
                result["skipped"].append({"street": s["property_address"],
                                           "reason": "no owner uuid"})
                continue

            # Phones and their tags are TWO separate writes. Tags passed inline
            # to upsert-phones are not applied — they go through
            # POST /api/internal/phone/add-phone-tag/ keyed by number + tag
            # uuid. Sending them inline looks like it worked and silently
            # tags nothing.
            # Phones and their tags are TWO separate writes. Tags passed inline
            # to upsert-phones are not applied — they go through
            # POST /api/internal/phone/add-phone-tag/, which takes a list whose
            # items carry a `tags` array of tag UUIDs.
            phones, scored_any = [], False
            number_to_tags: dict[str, list[str]] = {}
            for p in s["people"]:
                for ph in p["phones"]:
                    entry = {"number": ph["number"]}
                    if ph.get("type_raw"):
                        entry["type"] = ph["type_raw"]
                    phones.append(entry)

                    tags = list(ph["sources"])
                    if ph.get("tier"):
                        tags.append(ph["tier"])
                        scored_any = True
                    if p.get("relationship") and not p["is_primary"]:
                        tags.append(p["relationship"])
                    # All of a number's tags go in ONE item — the endpoint
                    # accepts several per number, and batching avoids N calls.
                    number_to_tags.setdefault(ph["number"], []).extend(tags)

            if phones:
                try:
                    _api.upsert_phones(owner_uuid, phones)
                    result["phones"] += len(phones)
                except _api.DataSiftAPIError as e:
                    logger.warning("upsert_phones failed for %s: %s", uuid, e)
                    result["skipped"].append({"street": s["property_address"],
                                               "reason": f"phones: {e}"})
                else:
                    deduped = {n: sorted(set(t)) for n, t in number_to_tags.items() if t}
                    try:
                        _api.set_phone_tags(deduped)
                        if not dry_run:
                            # The tag endpoint returns an empty body whether it
                            # worked or silently did nothing, so the record is
                            # the only trustworthy signal.
                            check = _api.verify_phone_tags(owner_uuid, deduped)
                            if not check["ok"]:
                                result["skipped"].append({
                                    "street": s["property_address"],
                                    "reason": f"phone tags did not land: {check['missing']}"})
                            else:
                                result["tagged_phones"] = (
                                    result.get("tagged_phones", 0) + len(deduped))
                    except _api.DataSiftAPIError as e:
                        logger.warning("phone tags failed on %s: %s", uuid, e)
                        result["skipped"].append({"street": s["property_address"],
                                                   "reason": f"phone tags: {e}"})

            tags = [TAG_TRACERFY_SKIPPED]
            if scored_any:
                tags.append(TAG_TRESTLE_SCORED)
            try:
                _api.add_tags(uuid, tags)
                result["tagged"] += 1
            except _api.DataSiftAPIError as e:
                logger.warning("add_tags failed for %s: %s", uuid, e)

            try:
                _api.post_message_board(owner_uuid,
                                         build_message_board(s, sources=sources))
                result["posted"] += 1
            except _api.DataSiftAPIError as e:
                logger.warning("message board failed for %s: %s", uuid, e)

            result["records"] += 1
    finally:
        _api.set_dry_run(False)
    return result


# ── The whole pipeline, in the order that was proven ──────────────────

def run_pipeline(rows: list[dict], *, dry_run: bool = True,
                  use_tracerfy: bool = True, use_datasift: bool = True,
                  score: bool = True) -> dict:
    """Resolve -> double skip trace -> score -> tag -> post. One call.

    This is the sequence verified end to end on a real foreclosure lead
    (7405 S Chestnut Ave, Broken Arrow) on 2026-08-21, at a total cost of
    $0.185 for one record, with an account-wide diff afterwards confirming
    ZERO other records were touched.

    `rows`: [{"street", "city", "first", "last"}]

    ORDER IS LOAD-BEARING:
      1. resolve      - server-side address search, read-only
      2. Tracerfy     - ~$0.02/record, billed on misses too
      3. DataSift     - ~$0.12/owner, estimate-gated, async (up to ~2.5 min)
      4. Trestle      - $0.015 per UNIQUE number, globally deduped
      5. tags         - source + tier per phone, by TITLE
      6. notes/board  - handled by the caller (petition text etc.)

    Scoring must come AFTER both sources or the second source's numbers go
    untiered - or you pay Trestle twice.

    dry_run=True does everything free and bills nothing.
    """
    result: dict = {"subjects": [], "unresolved": [], "spend_estimate": 0.0}

    subjects, unresolved = resolve_subjects(rows)
    result["unresolved"] = unresolved
    if not subjects:
        logger.warning("run_pipeline: nothing resolved")
        return result

    by_source: dict[str, dict[str, list[dict]]] = {}
    if use_tracerfy:
        by_source[SOURCE_TRACERFY] = tracerfy_source(subjects, dry_run=dry_run)
        result["spend_estimate"] += len(subjects) * 0.02
    subjects = merge_sources(subjects, by_source)

    if use_datasift:
        ds = datasift_source(subjects, dry_run=dry_run)
        if ds:
            subjects = merge_sources(subjects, {SOURCE_DATASIFT: ds})
        result["spend_estimate"] += len(subjects) * 0.12

    if score:
        tiers = score_phones(subjects, dry_run=dry_run)
        result["tiers"] = tiers
        uniq = {ph["number"] for s in subjects for p in s["people"] for ph in p["phones"]}
        result["spend_estimate"] += len(uniq) * 0.015

    sources = [s for s in (SOURCE_TRACERFY if use_tracerfy else None,
                            SOURCE_DATASIFT if use_datasift else None) if s]
    result["writeback"] = writeback(subjects, sources=sources, dry_run=dry_run)
    result["subjects"] = subjects
    logger.info("run_pipeline: %d subject(s), est. spend $%.3f%s",
                 len(subjects), result["spend_estimate"],
                 " (DRY RUN - nothing billed)" if dry_run else "")
    return result
