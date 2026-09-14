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
from datetime import datetime, timedelta, timezone
from typing import Iterable

import config
import datasift_api as _api

logger = logging.getLogger(__name__)

SOURCE_TRACERFY = "Tracerfy"
SOURCE_DATASIFT = "DataSift"          # found by DataSift's own skip trace
#: Already on the record before this run - origin genuinely unknown. NOT a
#: provider. Kept distinct from SOURCE_DATASIFT so that provider hit-rate
#: comparisons stay honest: a number tagged `DataSift` must mean DataSift's
#: skip trace returned it, never "it happened to be sitting there already".
SOURCE_PREEXISTING = "Pre-existing"

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

        prop_street = addr.get("street") or street
        prop_city = addr.get("city") or city
        prop_state = addr.get("state") or ""
        prop_zip = addr.get("postal_code") or ""

        # THE ADDRESS WE SKIP TRACE ON is the address of the PERSON, which is
        # not always the address of the PROPERTY.
        #
        # On probate they are usually different: the owner/contact is the
        # personal representative or an heir, who almost never lives in the
        # decedent's house. Tracing "Jennifer Faulk" at 4503 N Iroquois (a
        # house her nephew lives in) asks the vendor to match a person against
        # an address they have no connection to — and Tracerfy bills $0.02
        # whether it hits or misses. Her real address, off the court filing,
        # is 2240 W Newton.
        #
        # `owner.address` on the CRM record is the mailing address, which
        # build_api_payload() sources from the CSV's Mailing Street Address —
        # for probate, the PR's address from the Order. Prefer it; fall back
        # to a row-supplied mailing; fall back to the property last (correct
        # for foreclosure, where the owner does live there).
        owner_addr = owner.get("address") or {}
        trace_street = ((owner_addr.get("street") or "").strip()
                        or (row.get("mail_street") or "").strip())
        if trace_street:
            trace_city = ((owner_addr.get("city") or "").strip()
                          or (row.get("mail_city") or "").strip())
            trace_state = ((owner_addr.get("state") or "").strip()
                           or (row.get("mail_state") or "").strip())
            trace_zip = ((owner_addr.get("postal_code") or "").strip()
                         or (row.get("mail_zip") or "").strip())
            trace_source = "owner mailing"
        else:
            trace_street, trace_city = prop_street, prop_city
            trace_state, trace_zip = prop_state, prop_zip
            trace_source = "property (no owner mailing on record)"

        if trace_street.strip().lower() != (prop_street or "").strip().lower():
            logger.info("resolve: %s %s will be traced at their OWN address %r, "
                        "not the property %r", first, last, trace_street, prop_street)

        subjects.append({
            "property_uuid": rec.get("uuid"),
            "owner_uuid": owner.get("uuid"),
            "first": first, "last": last,
            "name": f"{first} {last}".strip(),
            "property_address": prop_street,
            "property_city": prop_city,
            "property_state": prop_state,
            "property_zip": prop_zip,
            # what the skip trace actually uses
            "trace_street": trace_street,
            "trace_city": trace_city,
            "trace_state": trace_state,
            "trace_zip": trace_zip,
            "trace_address_source": trace_source,
            "existing_phones": _existing_phones(rec),
            "people": [],
            "has_results": False,
            # Probate context for the Message Board's SIGNING CHAIN block.
            # Empty dict for foreclosure, which just omits the block.
            **_probate_context(rec, row),
        })
    logger.info("resolve: %d subject(s), %d unresolved", len(subjects), len(unresolved))
    return subjects, unresolved


#: CRM custom-field label -> the subject key the Message Board's SIGNING CHAIN
#: block reads. Only probate records carry these; a foreclosure record has none
#: of them and the block is simply omitted.
_PROBATE_SUBJECT_FIELDS = {
    "Personal Representative": "personal_representative",
    "PR Status":               "pr_status",
    "Decedent Name":           "decedent_name",
    "Date of Death":           "date_of_death",
    "Heir Count":              "heir_count",
    "Decision Maker":          "decision_maker",
    "DM Relationship":         "dm_relationship",
    "Title Holder of Record":  "title_holder",
}


def _probate_context(rec: dict, row: dict) -> dict:
    """Probate detail for the SIGNING CHAIN block, from the row we were handed
    or, failing that, off the CRM record's custom fields.

    The row wins because `skip-trace --create` has the freshly-extracted court
    data in hand; reading the record back covers the plain `skip-trace` case
    where the record already existed from an earlier run.
    """
    out: dict = {}
    for label, key in _PROBATE_SUBJECT_FIELDS.items():
        v = row.get(label) or row.get(key)
        if str(v or "").strip():
            out[key] = v
    for extra in ("heirs", "heirs_deceased", "heirs_address_unknown",
                  "additional_parcels"):
        v = row.get(extra) or row.get(extra.replace("_", " ").title())
        if str(v or "").strip():
            out[extra] = v
    if out:
        return out

    # Fall back to the record's own custom fields. The value row nests the
    # field at item["custom_field"], NOT a flat field_uuid — reading the wrong
    # key finds nothing and silently yields an empty block.
    for item in (rec.get("custom_field_values") or rec.get("custom_fields") or []):
        if not isinstance(item, dict):
            continue
        label = ((item.get("custom_field") or {}).get("label")
                 or item.get("label") or "")
        key = _PROBATE_SUBJECT_FIELDS.get(label)
        if key and str(item.get("value") or "").strip():
            out[key] = item["value"]
    return out


def _existing_phones(rec: dict) -> list[dict]:
    """Numbers already on the record, tagged `Pre-existing` on writeback.

    These were NOT found by this run, and their true origin is unknown — prior
    bulk data, an earlier run, a manual entry. They used to be tagged
    `DataSift`, which was a lie whenever DataSift's skip trace had not in fact
    returned them, and it corrupted exactly the number the tags exist to
    support: which provider actually earns its money.

    The leak was worst when a source was skipped (`use_tracerfy=False` on a
    resumed run), where EVERY number on the record fell through here and came
    out labelled `DataSift`. But it fires on any record that already carries
    phones a source does not re-return. Caught 2026-08-24.

    `datasift_source()` is the only thing allowed to assign SOURCE_DATASIFT,
    and it already does so correctly — via a before/after diff, so only
    genuinely new numbers get the tag.
    """
    out = []
    for p in ((rec.get("owner") or {}).get("phones") or []):
        n = norm_phone(p.get("number") if isinstance(p, dict) else p)
        if n:
            out.append({"number": n, "sources": [SOURCE_PREEXISTING],
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

    # ONE contact per unique person, not per record.
    #
    # A probate PR routinely appears on several cases at once (Jennifer Faulk
    # was PR on three Fulton estates filed the same day). Submitting her once
    # per record bills $0.02 each time for identical data. Worse, `index` used
    # to be keyed person -> ONE subject and overwrote on collision, so the
    # results came back and were applied to only the LAST record sharing that
    # person; the others silently got nothing despite being paid for.
    contacts: list[dict] = []
    index: dict[str, list[dict]] = {}
    for s in subjects:
        if not (s["first"] and s["last"]):
            logger.warning("tracerfy: skipping %s - no owner name", s["property_address"])
            continue
        key = person_key(s["first"], s["last"])
        if key in index:
            index[key].append(s)
            logger.info("tracerfy: %s %s already queued (also on %s) - not billing twice",
                        s["first"], s["last"], s["property_address"])
            continue
        index[key] = [s]
        # The PERSON's address, not the property's — see resolve(). Older
        # subjects (or a caller building them by hand) may lack the trace_*
        # keys, so fall back rather than KeyError.
        contacts.append({
            "first_name": s["first"], "last_name": s["last"],
            "address": s.get("trace_street") or s["property_address"],
            "city": s.get("trace_city") or s["property_city"],
            "state": s.get("trace_state") or s["property_state"],
            "zip": s.get("trace_zip") or s["property_zip"],
            "_subject": s,
        })

    if not contacts:
        return {}

    dupes = sum(len(v) - 1 for v in index.values())
    if dupes:
        logger.warning("tracerfy: %d record(s) share a person with another - "
                        "billing %d trace(s) instead of %d, saving $%.2f",
                        dupes, len(contacts), len(contacts) + dupes, dupes * 0.02)

    # Print exactly who is being traced at which address, before spending.
    # The user asked to be certain the PR/heir is traced at their real mailing
    # address; this is the line that proves it, and it prints on a dry run too.
    logger.warning("BILLED: submitting %d record(s) to Tracerfy (~$%.2f at $0.02/record)",
                    len(contacts), len(contacts) * 0.02)
    for c in contacts:
        s = c["_subject"]
        logger.warning("  trace: %s %s @ %s, %s %s %s  [%s]",
                        c["first_name"], c["last_name"], c["address"], c["city"],
                        c["state"], c["zip"], s.get("trace_address_source", "property"))
    for c in contacts:
        c.pop("_subject", None)
    if dry_run:
        logger.info("DRY RUN - not calling Tracerfy")
        return {}

    records = trace_contacts(contacts)
    logger.info("tracerfy: %d record(s) returned", len(records))

    people_by_subject: dict[str, list[dict]] = {}
    for rec in records:
        first = (rec.get("first_name") or "").strip()
        last = (rec.get("last_name") or "").strip()
        matched = index.get(person_key(first, last)) or []
        if not matched:
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

        # One paid lookup, credited to EVERY record that shares this person.
        # A repeat PR is billed once (see the dedupe above) but must still
        # populate all of their records — the old code kept only the last
        # subject per person, so the others got nothing despite being paid for.
        # Each subject gets its own copy: downstream stages mutate these dicts
        # (tier/score on phones), so a shared object would cross-contaminate.
        if len(matched) > 1:
            logger.info("tracerfy: applying %s %s's result to %d records",
                        first, last, len(matched))
        for subj in matched:
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
                "phones": [dict(p, sources=list(p["sources"])) for p in phones],
                "emails": list(emails),
            })
    return people_by_subject


#: Skip-trace job statuses that mean "still going". Anything else ("complete",
#: or a failure state) ends the wait.
_JOB_RUNNING = {"processing", "pending", "queued", "new", None}


def _find_skip_trace_job(submitted_at: datetime, n_records: int) -> dict | None:
    """The DataSift job this run just submitted, or None if not visible yet.

    Newest skip-trace job created at or after the submission (2 minutes of
    clock allowance) whose size matches. Size is `meta.total_properties`, not
    `total`: on 2026-08-31 a 9-property job reported total=8 because one owner
    was repeated. Matched on time + size because the submit response's shape
    was never captured (it is logged from 2026-09-11 on).
    """
    floor = submitted_at - timedelta(minutes=2)
    try:
        jobs = _api.list_skip_trace_jobs()
    except _api.DataSiftAPIError as e:
        logger.warning("datasift_source: could not read the skip-trace job feed: %s", e)
        return None
    for j in jobs:                                   # newest first
        try:
            created = datetime.fromisoformat(
                str(j.get("created") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created < floor:
            break
        size = (j.get("meta") or {}).get("total_properties", j.get("total"))
        if size in (None, n_records):
            return j
    return None


def datasift_source(subjects: list[dict], *, dry_run: bool = True,
                     poll_seconds: float = 20.0,
                     timeout_seconds: float = 1800.0) -> dict[str, list[dict]]:
    """DataSift's own skip trace, SCOPED to these records. Second half of the
    double skip trace.

    BILLED: prepaid credits, ~$0.12/owner. Runs the free estimate first and
    REFUSES if it would touch more records than we asked for — the payload has
    to be nested correctly or the endpoint silently goes account-wide (see
    datasift_api._skip_trace_body).

    Returns {property_uuid: [Person]} for numbers that are NEW after the trace,
    so they get the DataSift source tag rather than Tracerfy's.

    Asynchronous and not fast: observed 12s on one record, 150s on another,
    and ~11 MINUTES for a 2-record job on 2026-09-11. So the wait is on the
    job's own status (_api.list_skip_trace_jobs), for up to 30 minutes, and a
    job still running at the deadline marks its records `datasift_pending`
    rather than reading as "found nothing". Poll, don't assume failure.
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

    submitted_at = datetime.now(timezone.utc)
    _api.submit_skip_trace(uuids, max_records=len(uuids),
                            address_prefix=targets[0]["property_address"]
                            if len(targets) == 1 else "")

    # WAIT ON THE JOB, not on a fixed clock. On 2026-09-11 the job took ~11
    # minutes; the old 300s wait gave up at 5, scored and tagged without
    # DataSift's numbers, and posted "no numbers returned" on a record DataSift
    # then filled with two. The job's own status is readable
    # (_api.list_skip_trace_jobs), so poll it until it stops "processing".
    # The per-record signal stays as the fallback for when the job cannot be
    # found in the feed.
    import time
    deadline = time.monotonic() + timeout_seconds
    job: dict | None = None
    done: set[str] = set()
    while time.monotonic() < deadline:
        time.sleep(poll_seconds)
        job = _find_skip_trace_job(submitted_at, len(uuids)) or job
        if job is not None:
            if job.get("status") not in _JOB_RUNNING:
                break
            continue
        for s in targets:
            if s["property_uuid"] in done:
                continue
            rec = _api.get_property(s["property_uuid"])
            if (rec.get("owner") or {}).get("skiptrace_attempts"):
                done.add(s["property_uuid"])
        if len(done) == len(targets):
            break
    finished = ((job is not None and job.get("status") not in _JOB_RUNNING)
                or len(done) == len(targets))
    if job is not None:
        meta = job.get("meta") or {}
        logger.info("datasift_source: job %s %s, %s/%s processed, final cost %s "
                     "(estimate %s)", job.get("uuid"), job.get("status"),
                     job.get("processed"), job.get("total"),
                     meta.get("final_cost"), meta.get("initial_cost"))

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

    if not finished:
        # Never let a still-running job read as "DataSift found nothing". Each
        # unfinished subject is flagged so build_message_board() says the trace
        # is pending instead of posting "no numbers returned" - which went onto
        # a live record on 2026-09-11 six minutes before DataSift filled it.
        pending = [s for s in targets
                   if s["property_uuid"] not in out and s["property_uuid"] not in done]
        for s in pending:
            s["datasift_pending"] = True
        logger.warning("datasift_source: job still running after %.0fs - %d record(s) "
                        "marked PENDING. Its numbers will land later UNSCORED and "
                        "UNTAGGED. Do not re-submit (a second charge): once the job "
                        "completes, score and tag only the numbers that are new.",
                        timeout_seconds, len(pending))
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

    # An unusually large phone count on one record is a real signal worth
    # surfacing -- usually a common-name mismatch, occasionally a data quality
    # problem. The retired skip-and-score-upload mode stopped and prompted
    # here; this pipeline is DRY RUN by default and prints total spend before
    # --commit, so the anomaly is logged rather than turned into an
    # interactive prompt that would break unattended runs.
    OUTLIER_PHONES_PER_RECORD = 12
    for s in subjects:
        n = sum(len(p["phones"]) for p in s["people"])
        if n > OUTLIER_PHONES_PER_RECORD:
            logger.warning("OUTLIER: %s / %s has %d phone number(s) (threshold %d) "
                            "- check for a common-name mismatch before committing",
                            s.get("name") or "?", s.get("property_address") or "?",
                            n, OUTLIER_PHONES_PER_RECORD)

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


def _signing_chain_block(subject: dict) -> str:
    """SIGNING CHAIN section for the Message Board post.

    Probate's central risk is contacting someone who is a good lead but cannot
    convey alone. The Fulton estate has ten heirs; Castellanos has three, one
    with no known address; Berry has a sole heir who can sign by himself.
    Those are three completely different conversations and the caller has to
    know which one they are in before they discuss price.

    Everything here comes off the court filing for free — no skip trace, no
    enrichment. Returns "" for records that carry no probate context (i.e.
    every foreclosure record), so this is additive and safe for both types.
    """
    pr = (subject.get("personal_representative") or "").strip()
    pr_status = (subject.get("pr_status") or "").strip()
    decedent = (subject.get("decedent_name") or "").strip()
    if not (pr or decedent):
        return ""

    try:
        heir_count = int(subject.get("heir_count") or 0)
    except (TypeError, ValueError):
        heir_count = 0
    heirs = (subject.get("heirs") or "").strip()
    unknown = (subject.get("heirs_address_unknown") or "").strip()
    deceased_heirs = (subject.get("heirs_deceased") or "").strip()
    dm = (subject.get("decision_maker") or subject.get("name") or "").strip()
    dm_rel = (subject.get("dm_relationship") or "").strip()

    lines = ["SIGNING CHAIN"]
    if decedent:
        dod = (subject.get("date_of_death") or "").strip()
        lines.append(f"  Decedent: {decedent}" + (f" (d. {dod})" if dod else ""))
    # The PR as the COURT FILING names them, and who actually holds title.
    # These can be three different parties: on Johnson (2026-09-11) the PR was
    # the creditor's manager, the contact was the daughter, and title sat with
    # the lease-to-own seller. A caller has to see all of them.
    # A petition only ASKS for a PR - nobody holds that role until the court's
    # Order, so a bare name reads as appointed when it may just be proposed.
    # pr_status carries that distinction ("Petitioner - not yet appointed
    # (hearing MM/DD/YYYY)" vs "Appointed MM/DD/YYYY") when it is known.
    pr_line = f"  Personal Rep (per the probate filing): {pr or 'not named in the filing'}"
    if pr and pr_status:
        pr_line += f" - {pr_status}"
    lines.append(pr_line)
    title = (subject.get("title_holder") or "").strip()
    if title:
        if decedent and _same_person(decedent, title):
            note = " - the decedent; title has not passed yet"
        elif decedent:
            note = (" - NOT the decedent; confirm what the estate actually owns "
                    "before making an offer")
        else:
            note = ""
        lines.append(f"  Title holder of record (county assessor): {title}{note}")
    if dm:
        lines.append(f"  Talking to: {dm}" + (f" ({dm_rel})" if dm_rel else ""))

    # The contact's OWN mailing address, stated plainly and labelled as theirs.
    # This is where we mail them and what the skip trace matched on — it is NOT
    # the property. The property is the decedent's house, which is the thing we
    # are trying to buy, and the two must never read as interchangeable.
    mail = (subject.get("trace_street") or "").strip()
    if mail:
        loc = ", ".join(x for x in (subject.get("trace_city"),
                                     subject.get("trace_state")) if x)
        zc = (subject.get("trace_zip") or "").strip()
        line = f"  Their mailing address: {mail}" + (f", {loc}" if loc else "")
        lines.append(line + (f" {zc}" if zc else ""))
        prop = (subject.get("property_address") or "").strip()
        if prop and mail.lower() != prop.lower():
            lines.append(f"  (Estate property is {prop} - do NOT mail there; "
                         f"the contact does not live at it)")

    if heir_count == 1:
        lines.append("  Heirs: 1 - SOLE HEIR, can convey alone once appointed.")
    elif heir_count > 1:
        lines.append(f"  Heirs: {heir_count} - MULTIPLE HEIRS. The PR conveys on "
                     f"behalf of the estate; individual heirs cannot sell alone.")
    if heirs:
        lines.append(f"  Named: {heirs}")
    if deceased_heirs:
        lines.append(f"  Deceased heirs (their share passes to THEIR heirs): {deceased_heirs}")
    if unknown:
        lines.append(f"  NO ADDRESS ON FILE: {unknown} - must be located before closing.")

    # Extra parcels the estate owns that have NO county-assigned street
    # address (vacant/unplatted lots). Verified 2026-09-04: the assessor's
    # Situs Address really is blank for these, and the treasurer's list API
    # cannot be queried, so no lookup will produce a street address that does
    # not exist. They must not become their own CRM records — a parcel number
    # is not an address — so they ride on the estate's addressed record here,
    # which keeps the asset visible without creating junk.
    extra = (subject.get("additional_parcels") or "").strip()
    if extra:
        lines.append("")
        lines.append("ALSO OWNED BY THIS ESTATE (no street address assigned):")
        for part in [p.strip() for p in extra.split(";") if p.strip()]:
            lines.append(f"  {part}")
        lines.append("  Not separate CRM records by design - verify acreage/value "
                     "at the assessor before making an offer on the whole estate.")
    return "\n".join(lines)


def build_message_board(subject: dict, *, sources: list[str]) -> str:
    """One combined post per record, in his house format.

    Names live here and NEVER on a phone tag; the last-4 method is what lets a
    caller tie a number back to a person without the board becoming a phone
    dump.
    """
    stamp = datetime.now().strftime("%m/%d/%Y")
    signing = _signing_chain_block(subject)
    pending = bool(subject.get("datasift_pending"))
    pending_note = ""
    if pending:
        # DataSift's job had not finished when this ran. Do not claim it as a
        # source of the numbers below, and never say it returned nothing.
        sources = [x for x in sources if x != SOURCE_DATASIFT]
        pending_note = (f"DataSift skip trace still processing at {stamp} - its "
                        f"numbers are NOT listed here and will arrive untiered.")
    src = " + ".join(sources) if sources else "Skip trace"
    if not subject["has_results"]:
        base = (f"{src} attempted {stamp} - no numbers yet. {pending_note}"
                if pending else f"{src} attempted {stamp} - no numbers returned.")
        return f"{base}\n\n{signing}" if signing else base

    lines = [f"{src} - {stamp}", ""]
    if pending:
        lines += [pending_note, ""]
    # Who can actually sign, before the phone list. A caller needs to know
    # "this person signs alone" vs "one of ten heirs" BEFORE they talk price;
    # "best available contact" is not the same as "can convey clean title".
    if signing:
        lines += [signing, ""]
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


# ── Probate relationship tag ─────────────────────────────────────────

#: Numbers a skip trace found. Only these can carry a person's relationship;
#: a Pre-existing number's true owner is unknown.
_TRACE_SOURCES = {SOURCE_TRACERFY, SOURCE_DATASIFT}

#: Relationship phone tags that EXIST in the account (checked 2026-09-11:
#: Daughter, Son, Wife, Husband, Grandchild, Relative). A filing's wording maps
#: onto one of these, and any other family relationship becomes "Relative" -
#: user: "just use relative and I can create niece and nephew another time".
#: Never create a new relationship tag implicitly: phone tags are append-only,
#: so a near-duplicate title can never be cleaned up.
_REL_SPECIFIC = [
    (re.compile(r"\bgrand(son|daughter|child|children|kid)s?\b"), "Grandchild"),
    (re.compile(r"\bstep-?(son|daughter|child|children|mother|father|parent|brother|sister)s?\b"
                r"|\b\w+[- ]in[- ]law\b"), "Relative"),
    (re.compile(r"\bdaughters?\b"), "Daughter"),
    (re.compile(r"\bsons?\b"), "Son"),
    (re.compile(r"\b(wife|widow)\b"), "Wife"),
    (re.compile(r"\b(husband|widower)\b"), "Husband"),
]
_REL_FAMILY = re.compile(
    r"\b(niece|nephew|brother|sister|sibling|cousin|aunt|uncle|mother|father|"
    r"parent|child|children|spouse|grand\w*|relative|kin)s?\b")


def relationship_tag(text: str) -> str | None:
    """Map a filing's relationship wording onto an EXISTING phone tag, or None.

    "Daughter (first listed heir ...)"         -> "Daughter"
    "Niece (Personal Representative)"          -> "Relative"
    "Creditor - manager of L & S Group, LLC"   -> None (not family, no tag)

    Word-bounded throughout: "Personal Representative" contains "son".
    """
    t = (text or "").lower()
    for rx, tag in _REL_SPECIFIC:
        if rx.search(t):
            return tag
    return "Relative" if _REL_FAMILY.search(t) else None


def _same_person(a: str, b: str) -> bool:
    """Loose name identity: the shorter name's first and last tokens both
    appear in the longer. "Wendy Johnson" == "Wendy Jean Johnson" ==
    "Johnson, Wendy"."""
    drop = {"JR", "SR", "II", "III", "IV"}
    ta = [t for t in re.findall(r"[A-Za-z]{2,}", a.upper()) if t not in drop]
    tb = [t for t in re.findall(r"[A-Za-z]{2,}", b.upper()) if t not in drop]
    if not ta or not tb:
        return False
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return short[0] in long_ and short[-1] in long_


def _primary_relationship_tag(subject: dict) -> str | None:
    """Relationship tag for the traced subject's own numbers, or None.

    Only when the record carries a probate DM Relationship AND the traced owner
    is the Decision Maker it describes: a relationship belongs to one person,
    and on anyone else's numbers it would be a permanent false label.
    """
    rel = relationship_tag(subject.get("dm_relationship") or "")
    if not rel:
        return None
    dm = (subject.get("decision_maker") or "").strip()
    if dm and not _same_person(dm, subject.get("name") or ""):
        logger.warning("relationship tag skipped for %s: the filing's Decision "
                        "Maker is %s", subject.get("name"), dm)
        return None
    return rel


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
            # Probate: the traced subject IS an heir or the PR, so the numbers
            # a skip trace found for them get the relationship the filing
            # states (user, 2026-09-11). Tyler Austin's rule - the owner's own
            # numbers carry source + tier only - still holds everywhere else:
            # a foreclosure owner is nobody's relative, and this returns None.
            rel_tag = _primary_relationship_tag(s)
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
                    elif (p["is_primary"] and rel_tag
                          and set(ph.get("sources") or []) & _TRACE_SOURCES):
                        # Only numbers a trace found for THIS person. A
                        # Pre-existing number's owner is unknown, and the tag
                        # is permanent.
                        tags.append(rel_tag)
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
                        # Sent, read back, and re-sent ONCE to any number that
                        # came back with no tags at all - see
                        # _api.apply_phone_tags_verified. The endpoint returns
                        # an empty body whether it worked or silently did
                        # nothing, so the record is the only trustworthy signal.
                        check = _api.apply_phone_tags_verified(owner_uuid, deduped)
                        if check.get("retried"):
                            result.setdefault("tag_retries", []).extend(check["retried"])
                        if not check["ok"]:
                            result["skipped"].append({
                                "street": s["property_address"],
                                "reason": f"phone tags did not land: {check['missing']}"})
                        elif not dry_run:
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
