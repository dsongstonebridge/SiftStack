"""Outreach: the front half of the loop.

Everything else in this package handles replies. Nothing starts a conversation,
which means there is nothing to reply to. This does: it renders a touch from the
proven pools, registers the phone so the reply can find its record, and queues
it through the SAME outbox as every AI reply.

That last part is the design point. Seeds do not get their own send path, so
suppression, recipient-local quiet hours, per-number caps, pacing and the
sticky sender all apply to outreach exactly as they apply to replies. There is
one place in this codebase that can put a message on a stranger's phone, and it
is `worker.drain_outbox`.

    python src/sms_agent/cli.py seed --csv export.csv --touch 1          # preview
    python src/sms_agent/cli.py seed --csv export.csv --touch 1 --queue  # queue it
"""
from __future__ import annotations

import csv
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from . import config, crm, respond, sender_pool, store
from .knowledge import touches

log = logging.getLogger(__name__)

# DataSift export header spellings, first match wins (case-insensitive).
COLUMNS = {
    "street": ["property street", "street address", "property address", "street", "address"],
    "city": ["property city", "city"],
    "county": ["property county", "county"],
    "first": ["owner first name", "first name", "owner 1 first name"],
    "last": ["owner last name", "last name", "owner 1 last name"],
    "owner": ["owner name", "owner full name", "owner"],
    "phone": ["phone", "phone 1", "primary phone", "best_dial_phone", "to_number", "mobile"],
    "uuid": ["record_uuid", "uuid", "property uuid", "id"],
    "assigned": ["assigned to", "assignee", "assigned"],
}


@dataclass
class Candidate:
    phone: str
    record_uuid: str = ""
    first: str = ""
    owner_full: str = ""
    street: str = ""
    city: str = ""
    county: str = ""
    sender: str = ""
    message: str = ""
    status: str = "ready"
    touch: int = 0
    reasons: list[str] = field(default_factory=list)

    def hold(self, reason: str) -> "Candidate":
        self.status = "hold"
        self.reasons.append(reason)
        return self


def _detect(headers: list[str], key: str) -> str:
    lower = {h.strip().lower(): h for h in headers}
    for guess in COLUMNS[key]:
        if guess in lower:
            return lower[guess]
    return ""


def from_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        cols = {k: _detect(headers, k) for k in COLUMNS}
        rows = []
        for raw in reader:
            rows.append(
                {
                    k: (raw.get(cols[k]) or "").strip() if cols[k] else ""
                    for k in COLUMNS
                }
            )
        return rows


# Phone dispositions that mean "never text this number again".
SKIP_PHONE_STATUSES = {"DNC", "CORRECT_DNC", "WRONG_DNC", "WRONG", "DEAD"}

# Only the two tiers Trestle scored as most likely to reach the owner.
ALLOWED_DIAL_TIERS = {"Dial First", "Dial Second"}


# Line types we will text outright, and the ones where the field is simply
# missing so the dial tier is the better evidence.
TEXTABLE_TYPES = {"MOBILE"}
DEFERRABLE_TYPES = {"", "UNKNOWN"}


def textable_line(phone_type: str, tier: str) -> tuple[bool, str]:
    """May we text this line? (ok, reason it was refused).

    A MOBILE always passes. A line type we do not KNOW defers to the dial tier,
    because Trestle already scored that number for reachability and its verdict
    is better evidence than a field DataSift never populated. A known LANDLINE
    stays blocked even at Dial First.

    That last clause is the whole point of this function existing rather than a
    boolean. The FTM book is 604 records whose every phone is type UNKNOWN
    (measured 2026-08-31: 151 phones, zero MOBILE) while carrying real tier
    tags, so a blanket "allow non-mobile" would unlock them and let genuine
    landlines through everywhere else at the same time.
    """
    t = (phone_type or "").upper()
    if t in TEXTABLE_TYPES:
        return True, ""
    if t in DEFERRABLE_TYPES:
        if tier in ALLOWED_DIAL_TIERS:
            return True, ""
        return False, f"line type unknown and tier {tier or 'untagged'}"
    return False, f"not a mobile number ({t.lower()})"


def phone_still_textable(record_uuid: str, phone: str) -> tuple[Optional[bool], str]:
    """The send-time re-check. (True, "") to send, (False, why) to cancel,
    (None, why) when the record could not be read, which means HOLD.

    Same three rules as the build, against a FRESH read of the record.
    """
    try:
        _, obj = crm.find_phone_object(record_uuid, phone, fresh=True)
    except Exception as exc:  # noqa: BLE001 - unreadable is a hold, not a pass
        return None, f"could not re-read the number ({str(exc)[:60]})"
    if obj is None:
        if crm.get_record(record_uuid) is None:
            return None, "could not re-read the record"
        return False, "number is no longer on the record"
    status = (obj.get("status") or "").upper()
    if status in SKIP_PHONE_STATUSES:
        return False, f"number now marked {status}"
    if config.CORRECT_NUMBER_FIRST:
        verdict = _correct_number_verdict(record_uuid, phone, obj)
        if verdict is not None:
            return verdict
    tier = crm.dial_tier(record_uuid, phone)
    if tier not in ALLOWED_DIAL_TIERS:
        return False, f"dial tier now {tier or 'untagged'}"
    ok, why = textable_line(obj.get("type"), tier)
    if not ok:
        return False, why
    return True, ""


# ---------------------------------------------------------------------------
# Tulsa fork: a Correct number wins, unless we already spoke with them on it.
# (config.CORRECT_NUMBER_FIRST; Jeff, 2026-10-05.)
#
# Measured on the FTM book 2026-10-05: smrtPhone logs a VOICEMAIL as
# `owner.call.answered` (status answered, 64-79s), so the call log alone cannot
# tell a conversation from a greeting. The board note decides, exactly as in
# the KPI engine (kpi-engine/pull_kpis.py, whose patterns these are): "no
# answer, left vm, but the message said this is tim" is a voicemail and still
# gets texted; "spoke with matthew ..." is a conversation and does not.

_CENTRAL = None


def _central():
    global _CENTRAL
    if _CENTRAL is None:
        from zoneinfo import ZoneInfo
        _CENTRAL = ZoneInfo(config.CAMPAIGN_TZ)
    return _CENTRAL


_VM_NOTE = re.compile(
    r"\b(no answer|n/?a\b|didn'?t answer|did not answer|vms?|v/m|lvm|left (a )?(vms?|voicemails?|messages?|msg)|"
    r"voice ?mail|mailbox|went to (vm|voicemail)|no pick ?up|rang out|straight to (vm|voicemail)|"
    r"can'?t get a ?hold|couldn'?t (get a ?hold|reach)|unable to reach|no luck|answering (machine|service)|"
    r"nobody answered|no one answered|wasn'?t answered)\b",
    re.I)
_TALK_NOTE = re.compile(
    r"\b(talked|spoke|spoken|convo|conversation|said|says|wants|asking|told me|motivated|interested|"
    r"offer|appointment|appt|reached|hung up|picked up|when i said|answer|answered)\b", re.I)
_STRONG_TALK = re.compile(
    r"\b(talked|spoke|spoken|convo|conversation|told me|picked up|hung up|"
    r"answer|answered|got a ?hold|reached (him|her|them|owner))\b", re.I)
_NEGATED_ANSWER = re.compile(
    r"\b(no|not|never|nobody|didn'?t|did not|don'?t|won'?t|wouldn'?t|wasn'?t|"
    r"was not|doesn'?t)\s+(\w+\s+)?answer(ed|ing|s)?\b|\banswering (machine|service)\b", re.I)
# Pipeline posts (petition detail, skip-trace summaries) are not call notes.
_PIPELINE_NOTE = re.compile(r"SIGNING CHAIN|CASE:|PROPERTY:|OWNER:|PETITION|Tracerfy|Skip traced", re.I)


def _hand_note(ev: dict) -> str:
    """The text of a hand-typed board note, or "" if the event is anything else."""
    if not str(ev.get("event_type") or "").endswith(("message.added", "notes.added")):
        return ""
    if ev.get("source") == "upload" or ev.get("author") == "system":
        return ""
    pl = ev.get("payload") or {}
    text = pl.get("message") if isinstance(pl.get("message"), str) else ""
    text = text or (pl.get("note") if isinstance(pl.get("note"), str) else "")
    if not text or len(text) > 280 or _PIPELINE_NOTE.search(text):
        return ""
    return text


def is_vm_note(text: str) -> bool:
    talk = _NEGATED_ANSWER.sub(" ", text or "")
    return bool(_VM_NOTE.search(text or "")) and not _STRONG_TALK.search(talk)


def is_talk_note(text: str) -> bool:
    return (not is_vm_note(text)) and bool(_TALK_NOTE.search(_NEGATED_ANSWER.sub(" ", text or "")))


def _event_time(ev: dict) -> Optional[datetime]:
    raw = str(ev.get("timestamp") or "")
    try:
        return datetime.fromisoformat(raw.replace(" ", "T")).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _event_day(ev: dict) -> str:
    when = _event_time(ev)
    if when is None:
        return str(ev.get("timestamp") or "")[:10]
    return when.astimezone(_central()).date().isoformat()


# A note written within this long after an answered call is about THAT call.
_NOTE_PAIR_SECONDS = 30 * 60


def spoken_on(events: list, phone: str) -> str:
    """Why we count as having spoken with them on `phone`, or "" if we have not.

    Each hand-typed board note is paired with the answered call just before it
    (within 30 min), because that is the call it describes. Measured on 2442 E
    3rd St, 2026-10-01: dad's line answered -> "810 is the dad ...", then her
    line answered -> "left her a vm". Pairing by DAY read the dad conversation
    as a talk on her number (Jeff, 2026-10-05: text her).

    Spoken when:
    - a conversation note is paired with a call on `phone`, or follows no call
      at all (it cannot be pinned to another number, so it counts);
    - they called in from `phone` and it was answered;
    - we called `phone`, it was answered, and its paired note is not a
      voicemail note (no note at all counts as spoken: texting someone we
      already talked to is the worse mistake), unless an unpaired voicemail
      note was written the same day.
    """
    target = store.clean_phone(phone)
    calls, notes = [], []
    for ev in events:
        when = _event_time(ev)
        if ev.get("event_type") == "owner.call.answered":
            call = (ev.get("payload") or {}).get("call") or {}
            inbound = (call.get("direction") or "").lower() == "inbound"
            other = store.clean_phone(call.get("origin_number") if inbound else call.get("destination_number"))
            calls.append({"when": when, "number": other, "inbound": inbound,
                          "day": _event_day(ev), "note": None})
            continue
        text = _hand_note(ev)
        if text:
            notes.append({"when": when, "text": text, "day": _event_day(ev), "call": None})

    for note in notes:
        before = [c for c in calls if c["when"] and note["when"] and c["when"] <= note["when"]
                  and (note["when"] - c["when"]).total_seconds() <= _NOTE_PAIR_SECONDS]
        if before:
            call = max(before, key=lambda c: c["when"])
            note["call"] = call
            if call["note"] is None or call["note"]["when"] > note["when"]:
                call["note"] = note

    for note in notes:
        if not is_talk_note(note["text"]):
            continue
        if note["call"] is None or note["call"]["number"] == target:
            return f'board note: "{note["text"][:80]}"'

    # A voicemail note covers every un-noted answered call that day: a caller
    # dials a run of numbers and writes one "left vms" at the end (11300 N
    # 118th E Ave, 2026-09-30: five lines, one note, paired with the last call).
    vm_days = {n["day"] for n in notes if is_vm_note(n["text"])}
    for call in calls:
        if call["number"] != target:
            continue
        if call["inbound"]:
            return f'they called in from it {call["day"]} and it was answered'
        paired = call["note"]
        if paired is not None:
            if is_vm_note(paired["text"]):
                continue
            if is_talk_note(paired["text"]):
                return f'answered call {call["day"]}, note: "{paired["text"][:60]}"'
            # Neither voicemail nor conversation ("called twice", 1288 E 143rd
            # St, right after "No answer but recorded vm says Dana"): read it as
            # un-noted and let that day's voicemail note decide.
        if call["day"] not in vm_days:
            return f'answered call {call["day"]} with no voicemail note after it'
    return ""


def correct_phones(rec: dict) -> list[dict]:
    owner = rec.get("owner") if isinstance(rec.get("owner"), dict) else {}
    return [p for p in owner.get("phones") or []
            if isinstance(p, dict) and (p.get("status") or "").upper() == "CORRECT"]


def _correct_number_verdict(record_uuid: str, phone: str,
                            obj: dict) -> Optional[tuple[Optional[bool], str]]:
    """Send-time rule. None = no Correct number on the record (normal rules apply)."""
    rec = crm.get_record(record_uuid) or {}
    correct = correct_phones(rec)
    if not correct:
        return None
    target = store.clean_phone(phone)
    if target not in {store.clean_phone(p.get("number")) for p in correct}:
        return False, "a different number on the record is marked Correct"
    # The tier is irrelevant once a person has confirmed the number.
    ok, why = textable_line(obj.get("type"), "Dial First")
    if not ok:
        return False, why
    events = crm.activity_log(record_uuid)
    if events is None:
        return None, "could not read the call log / board"
    spoke = spoken_on(events, target)
    if spoke:
        return False, f"already spoke with them: {spoke}"
    return True, ""


CORRECT_DNC_REASON = "Correct number is flagged do-not-call"


def correct_dnc_note(row: dict) -> str:
    """Board note for a record skipped because its Correct number is flagged."""
    return (
        "SMS agent: no automated text sent. The number marked Correct on this "
        "record is flagged do-not-call in DataSift, so the agent will not text "
        "it, and it does not text the record's other numbers while one is "
        "marked Correct. Call it, or decide by hand whether to text it."
    )


def post_skip_notes(notes: list) -> dict:
    """Post each (record_uuid, text) board note ONCE per record, ever.

    Under DRY_RUN nothing is posted and nothing is marked, so the first real
    run still posts. A failed post is not marked either, so it retries tomorrow.
    """
    posted, skipped, failed = 0, 0, 0
    for record_uuid, text in notes:
        key = f"skip-note:{record_uuid}"
        if not record_uuid or store.get_meta(key):
            skipped += 1
            continue
        if config.DRY_RUN:
            log.info("DRY_RUN board note on %s: %s", record_uuid, text[:80])
            skipped += 1
            continue
        res = crm.post_note(record_uuid, text)
        if isinstance(res, dict) and res.get("error"):
            log.warning("board note on %s failed: %s", record_uuid, res["error"])
            failed += 1
            continue
        store.set_meta(key, store.now())
        posted += 1
    return {"posted": posted, "skipped": skipped, "failed": failed}


def apply_correct_number(row: dict, dnc_numbers: set) -> tuple[Optional[dict], str]:
    """Build-time rule. Returns (row, "") with the row UNCHANGED when the record
    has no Correct number, so a record's touch sequence never hops numbers;
    (row on the Correct number, "") when it should be texted there; or
    (None, why) when the record gets no text at all.
    """
    uuid = row.get("uuid") or ""
    rec = crm.get_record(uuid) if uuid else None
    if not rec:
        return None, "could not read the record"
    best, flagged = None, []
    for p in correct_phones(rec):
        number = store.clean_phone(p.get("number"))
        if len(number) != 10:
            continue
        ok, _ = textable_line(p.get("type"), "Dial First")
        if not ok:
            continue
        if number in dnc_numbers:
            flagged.append(number)
            continue
        rank = 0 if (p.get("type") or "").upper() == "MOBILE" else 1
        if best is None or rank < best[0]:
            best = (rank, number)
    if not correct_phones(rec):
        return row, ""
    if best is None and flagged:
        # Already talked to them: nothing for a human to decide, so no note.
        events = crm.activity_log(uuid)
        if events is None:
            return None, "could not read the call log / board"
        if any(spoken_on(events, n) for n in flagged):
            return None, "already spoke with them on the Correct number"
        return None, CORRECT_DNC_REASON
    if best is None:
        return None, "Correct number is not textable (landline or VoIP)"
    if crm.phone_is_dnc(best[1]):
        return None, CORRECT_DNC_REASON
    events = crm.activity_log(uuid)
    if events is None:
        return None, "could not read the call log / board"
    if spoken_on(events, best[1]):
        return None, "already spoke with them on the Correct number"
    out = dict(row)
    out.pop("_needs_best_phone", None)
    out["phone"] = best[1]
    out["dial_tier"] = "Correct"
    return out, ""


def from_preset(title: str, limit: int = 0, keep_unresolved: bool = False,
                stats: Optional[dict] = None) -> tuple[list[dict], str]:
    """Pull the cohort straight from a DataSift filter preset.

    This is the version that survives without a laptop: the box pulls the list
    itself, so there is no CSV to export and re-upload, and every suppression
    rule already encoded in the preset (dead statuses, Mail Only, Recently Sold,
    skiptraced, call-attempt gates) is inherited automatically. A record Ty
    edits in DataSift drops out of our sends with no second list to maintain.

    `stats` is an optional counter dict filled in place. Everything this
    function drops used to vanish without a trace, which is how a cohort of
    1,577 producing 17 sends went unnoticed for a week.

    `keep_unresolved` returns rows this function would have dropped on the
    SEARCH ROW's phone alone, marked `_needs_best_phone`, so the caller can
    look at the whole record instead. The search payload carries one
    representative phone, and on the FTM cohort it is the wrong one 52% of the
    time. Off by default, so every existing caller keeps today's behaviour at
    zero extra CRM reads.
    """
    counts = stats if stats is not None else {}

    def drop(reason: str, row: Optional[dict] = None) -> Optional[dict]:
        counts[reason] = counts.get(reason, 0) + 1
        if keep_unresolved and row is not None:
            row["_needs_best_phone"] = reason
            return row
        return None

    must, matched = crm.resolve_preset(title)
    if not must:
        return [], ""
    # Learned once and cached, so this is a dict lookup per record.
    tier_uuids = set((crm.dial_tier_uuids() or {}).values())
    rows = []
    cohort = crm.fetch_cohort(must, limit=limit)
    counts["cohort"] = len(cohort)
    for rec in cohort:
        phone = rec.get("phone") if isinstance(rec.get("phone"), dict) else {}
        number = store.clean_phone(phone.get("number") or "")

        # The DNC flag lives ONLY on this search row: the full record comes back
        # with the field absent, not false (verified live 2026-08-28). So this
        # is the one place it can be seen, and the caller needs the number even
        # when the row is kept for a deeper look.
        if phone.get("doNotCall"):
            if number:
                counts.setdefault("_dnc_numbers", set()).add(number)
            # Tulsa fork: the flag belongs to THIS number, not the owner. Keep
            # the record for a look at its other numbers; resolve_best_phone
            # skips every number in the collected DNC set.
            if not (config.DNC_TRY_OTHER_NUMBERS and keep_unresolved):
                drop("phone flagged do-not-call")
                continue
            flagged_dnc = True
        else:
            flagged_dnc = False

        deferred = "phone flagged do-not-call" if flagged_dnc else None
        # Tier filtered here, on the search payload, so it costs nothing. Doing
        # it per candidate meant one record fetch each, which is tolerable for
        # 300 records and impossible for the 9,000 the cadences actually hold.
        row_tier = ""
        if tier_uuids:
            tags = set(phone.get("tags") or [])
            if not tags & tier_uuids:
                deferred = deferred or "search-row tier not first or second"
            else:
                row_tier = "verified"

        # The phone's own disposition is the suppression. An opt-out writes
        # DNC / CORRECT_DNC / WRONG_DNC onto the number in Sift, so honouring
        # it here is what makes that stick across every future campaign,
        # including ones this code did not build. WRONG and DEAD are excluded
        # for the same reason: someone already established the line is no good.
        if (phone.get("status") or "").upper() in SKIP_PHONE_STATUSES:
            deferred = deferred or "phone status DNC/WRONG/DEAD"

        ok, why = textable_line(phone.get("type"), "Dial First" if row_tier else "")
        if not ok:
            deferred = deferred or why

        addr = rec.get("address") or {}
        owner = rec.get("owner") or {}
        row = {
            "phone": phone.get("number") or "",
            "dial_tier": row_tier,
            "uuid": rec.get("uuid") or "",
            "street": addr.get("street") or "",
            "city": addr.get("city") or "",
            "county": "",  # not on the slim search record; resolved per candidate
            "first": owner.get("first_name") or "",
            "last": owner.get("last_name") or "",
            "owner": owner.get("company")
            or " ".join(x for x in (owner.get("first_name"), owner.get("last_name")) if x),
            "assigned": "",  # resolved from the full record by _resolve_sender
        }

        if deferred:
            kept = drop(deferred, row)
            if kept is None:
                continue
            counts["deferred to the full record"] = counts.get("deferred to the full record", 0) + 1
            rows.append(kept)
            continue

        counts["kept"] = counts.get("kept", 0) + 1
        rows.append(row)
    return rows, matched


_TIER_RANK = {"Dial First": 0, "Dial Second": 1}


def resolve_best_phone(row: dict, dnc_numbers: set) -> tuple[Optional[dict], str]:
    """Swap the search row's phone for the best textable one on the record.

    Returns (row, "") or (None, reason).

    The search payload carries ONE representative phone per record, and it is
    frequently not the good one. Measured on the FTM cohort 2026-08-31: 29 of
    30 records hold a Dial First/Second non-DNC number, but for 15 of those 29
    it is a different number than the one the search row returned. Judging a
    record by that one phone throws away half a reachable book.

    Same shape `dispo_campaign.rows_from_registry` already uses for the dispo
    list, and for the same measured reason. Nothing in `build()` is relaxed:
    every candidate still goes through the tier, suppression and copy checks.
    """
    uuid = row.get("uuid") or ""
    if not uuid:
        return None, "no record uuid"
    rec = crm.get_record(uuid)
    if not rec:
        return None, "could not read the record"

    owner = rec.get("owner") if isinstance(rec.get("owner"), dict) else {}
    best = None
    for p in owner.get("phones") or []:
        if not isinstance(p, dict):
            continue
        number = store.clean_phone(p.get("number"))
        if len(number) != 10:
            continue
        # The DNC flag is absent from the full record, so the only place it can
        # be known is the search rows collected across every swept preset.
        if number in dnc_numbers:
            continue
        if (p.get("status") or "").upper() in SKIP_PHONE_STATUSES:
            continue
        tier = ""
        for tag in (p.get("tags") or []):
            name = (tag.get("title") or tag.get("name") or tag.get("tag")
                    if isinstance(tag, dict) else str(tag))
            if name in ALLOWED_DIAL_TIERS:
                tier = name
                break
        if not tier:
            continue
        ok, _ = textable_line(p.get("type"), tier)
        if not ok:
            continue
        # Dial First over Second, then a known mobile over an unknown line.
        rank = (_TIER_RANK.get(tier, 9),
                0 if (p.get("type") or "").upper() == "MOBILE" else 1,
                0 if p.get("is_connected", True) else 1)
        if best is None or rank < best[0]:
            best = (rank, number, tier)

    if best is None:
        return None, "no Dial First or Second phone on the record"

    # The do-not-call flag exists ONLY on a search row's representative phone.
    # Measured 2026-08-31 across the full record, the owner endpoint and a
    # targeted search: for any other number on the record it is unavailable,
    # not merely unread. So this probe answers for some numbers and returns
    # None for exactly the ones deep resolution exists to reach.
    #
    # A real opt-out is not affected either way: that writes DNC / CORRECT_DNC
    # / WRONG_DNC into the phone STATUS, which is on every phone and was
    # already refused above. This governs the registry scrub alone.
    flagged = crm.phone_is_dnc(best[1])
    if flagged:
        return None, "best phone is flagged do-not-call"
    if flagged is None and config.REQUIRE_VISIBLE_DNC:
        return None, "could not verify do-not-call status"

    out = dict(row)
    out.pop("_needs_best_phone", None)
    out["phone"] = best[1]
    out["dial_tier"] = best[2]
    return out, ""


def build(rows: Iterable[dict], touch: int, sender_fallback: str = "",
          new_deal: bool = False) -> list[Candidate]:
    """Turn export rows into vetted, rendered candidates. Sends nothing."""
    out: list[Candidate] = []
    # One human, one text. Owners routinely hold several properties in the same
    # preset, and the live Hottest pull had one phone attached to six records.
    # Sending a touch per property is six texts to the same person, which is
    # how a number gets reported rather than answered.
    already_queued_this_run: set[str] = set()
    for row in rows:
        phone = store.clean_phone(row.get("phone"))
        cand = Candidate(
            phone=phone,
            record_uuid=row.get("uuid") or "",
            street=row.get("street") or "",
            city=row.get("city") or "",
            county=row.get("county") or "",
            owner_full=(row.get("owner") or f"{row.get('first','')} {row.get('last','')}").strip(),
        )

        if len(phone) != 10:
            out.append(cand.hold("no usable phone"))
            continue
        if not cand.street:
            out.append(cand.hold("no street address"))
            continue

        reason = store.is_suppressed(phone)
        if reason:
            out.append(cand.hold(f"suppressed ({reason})"))
            continue

        # Dial First and Dial Second only (Ty, 2026-08-11). Trestle already
        # scored every number and the tier IS that verdict, so texting a Dial
        # Fourth or a Drop spends a segment on a line somebody already judged
        # unlikely to reach the owner. Untagged counts as unqualified: an
        # allow-list, not a block-list, because the failure mode of guessing
        # wrong is texting a stranger.
        #
        # The first live run went out without this check: 24 of 84 sends landed
        # on Third, Fourth or Drop, and 7 more on untagged numbers.
        # Tulsa fork: a number a person marked Correct skips the tier gate
        # (apply_correct_number already checked line type and do-not-call).
        if cand.record_uuid and crm.client() and row.get("dial_tier") != "Correct":
            tier, checked = crm.dial_tier_checked(cand.record_uuid, phone)
            if not checked:
                # Could not read the record. Held rather than sent, but named
                # differently: a CRM outage must not be mistaken for a list of
                # genuinely low-tier numbers.
                out.append(cand.hold("could not read dial tier from the CRM"))
                continue
            if tier not in ALLOWED_DIAL_TIERS:
                out.append(cand.hold(f"dial tier {tier or 'untagged'}, not first or second"))
                continue

        if phone in already_queued_this_run:
            out.append(cand.hold("same phone already has a touch in this batch"))
            continue

        conv = store.get_conversation(phone)
        state = (conv or {}).get("state")
        # A NEW DEAL is a fresh reason to reach out, and a `paused` thread
        # means a human took over the LAST one. On blast 2 that dropped all
        # 16 buyers who engaged on blast 1: the most valuable audience on the
        # list, excluded precisely because they had been good leads.
        # `opted_out` and `closed` are never overridden, and suppression is a
        # separate gate that still applies.
        reopenable = new_deal and state == "paused"
        if state not in ("active", None, "") and not reopenable:
            out.append(cand.hold(f"conversation is {state}"))
            continue
            continue
        if conv and conv.get("last_inbound") and not new_deal:
            # They already replied. A scheduled touch landing on a live
            # conversation is the same failure as two people texting at once.
            out.append(cand.hold("already replied; touch would interrupt a live thread"))
            continue
        if config.STOP_ON_ANY_REPLY and not new_deal and _record_replied(cand.record_uuid):
            # Any line on this record answered. The owner replying from their
            # second number is still the owner replying.
            out.append(cand.hold("another number on this record already replied"))
            continue

        # Entities and initials-only owners get owner-of-the-address wording.
        # Only a real owner FIRST NAME may become a greeting. Falling back to
        # the full owner string mines a company name for a first name, which is
        # how "Willis Ailene B Life Est" became "Hey Willis" on a live preview.
        if touches.is_entity(cand.owner_full):
            cand.first = ""
        elif row.get("first"):
            cand.first = touches.clean_first(row["first"])
        else:
            cand.first = touches.clean_first(cand.owner_full)

        if cand.record_uuid and not cand.county:
            cand.county = crm.deal_context(cand.record_uuid).get("county", "")
        cand.sender = _resolve_sender(row, cand.record_uuid, sender_fallback)
        if not cand.sender:
            out.append(cand.hold("no assigned caller name; a touch is signed or it is not sent"))
            continue

        if config.TOUCH_SOURCE == "fields":
            # Send the reviewed copy on the record, verbatim. Never fall back
            # to the pool: an unreviewed text is exactly what this mode exists
            # to prevent, so a blank or unreadable field holds the record.
            copy, why = crm.text_touch(cand.record_uuid, touch)
            if not copy:
                out.append(cand.hold(why))
                continue
            cand.message = copy
        else:
            cand.message = touches.render(
                touch,
                seed=f"{cand.street}|{cand.owner_full}".lower(),
                first=cand.first,
                addr=cand.street,
                city=cand.city,
                sender=cand.sender,
            )

        # The record's own street line is exempt from the zip check: Tulsa
        # house numbers run to five digits ("16547 E 2nd Pl"), and without the
        # exemption every such owner was silently held (15 of 78 FTM records,
        # 2026-10-03). A zip anywhere else in the text is still blocked.
        ok, problems = respond.validate(
            cand.message, max_questions=2,
            allowed_address=touches.fix_ordinals(cand.street))
        if not ok:
            out.append(cand.hold("copy failed the human-voice check: " + "; ".join(problems)))
            continue

        already_queued_this_run.add(phone)
        out.append(cand)
    return out


def _record_replied(record_uuid: str) -> bool:
    """True when a reply on any line stopped this whole record.

    Set by engine._stop_on_reply. An opt-out or wrong-number reply does not
    set it: those stop only their own line, which the per-phone checks above
    already hold."""
    return bool(record_uuid and store.get_meta(f"record_replied:{record_uuid}"))


def _resolve_sender(row: dict, record_uuid: str, fallback: str) -> str:
    """Whose name signs this touch: the person assigned to the record."""
    assigned = (row.get("assigned") or "").strip()
    if assigned:
        mapped = config.senders().get(assigned)
        if mapped:
            return str(mapped).split()[0]
        # An export's "Assigned To" is usually already a human name.
        if "@" not in assigned and len(assigned.split()) <= 3:
            return assigned.split()[0].title()
    if record_uuid:
        name = crm.deal_context(record_uuid).get("assigned_name") or ""
        if name:
            return name
    return fallback or config.SENDER_NAME


def schedule(candidates: list[Candidate]) -> list[tuple[Candidate, str, str]]:
    """Assign each ready candidate a sending number and a send time.

    Three things have to be true at once for a batch to look like a person
    rather than a broadcast:

      1. **Spread.** Consecutive sends are 60-180 seconds apart, randomised.
         A fixed cadence is itself a machine signature, so the gap varies.
      2. **Rotation.** Numbers are taken least-loaded-first, so 52 messages
         spread across 19 numbers instead of hammering one.
      3. **Per-number rest.** The same number will not send twice inside ten
         minutes, no matter where it lands in the queue. That is the constraint
         a carrier actually watches.

    Recipient-local quiet hours still apply: anything that would land outside
    8am-9pm their time is pushed to the next morning.

    Returns [(candidate, from_number, not_before_iso)].
    """
    ready = [c for c in candidates if c.status == "ready"]
    if not ready:
        return []

    pool_by_owner: dict[str, list[str]] = {}
    last_used: dict[str, datetime] = {}
    sticky: dict[str, str] = {}  # phone -> number, held for this run
    now = datetime.now(timezone.utc)
    cursor = now
    out: list[tuple[Candidate, str, str]] = []

    for cand in ready:
        numbers = pool_by_owner.setdefault(cand.sender, sender_pool.pool(cand.sender))
        if not numbers:
            continue
        if cand.phone in sticky:
            # Same person twice in one batch: same number, no question.
            numbers = [sticky[cand.phone]]

        # Least recently used number that has had its rest, else the one whose
        # rest expires soonest.
        def ready_at(n: str) -> datetime:
            last = last_used.get(n)
            return now if last is None else last + timedelta(seconds=config.MIN_SEND_GAP_SECONDS)

        # A thread keeps its number for life. Whatever already texted this
        # person wins over the pool's least-recently-used pick, because two
        # numbers texting one owner about one house reads as a spam farm to the
        # person receiving it, which matters more than balancing the pool.
        existing = (store.get_conversation(cand.phone) or {}).get("from_number") or ""
        if existing in numbers:
            from_number = existing
        else:
            from_number = min(numbers, key=lambda n: (ready_at(n), last_used.get(n, now)))
        sticky[cand.phone] = from_number

        slot = max(cursor, ready_at(from_number))

        # Never outside the recipient's own waking hours. A deferral belongs to
        # that one person: the cursor advances from the slot below, so nobody
        # else waits out someone else's timezone.
        send_at = slot
        if not sender_pool.within_quiet_hours(cand.phone, send_at):
            send_at = sender_pool.next_send_window(cand.phone, send_at)

        last_used[from_number] = send_at
        cursor = slot + timedelta(
            seconds=random.randint(config.SEND_SPACING_MIN, config.SEND_SPACING_MAX)
        )
        out.append((cand, from_number, send_at.isoformat(timespec="seconds")))
    return out


def reschedule_held() -> dict:
    """Re-time every staged message as ONE sequence.

    `queue()` schedules the batch it is given, so calling it once per touch
    produced four independent timelines all starting at "now". Staged together
    that collapsed to a 2-second minimum gap and twenty minutes carrying more
    than one send: a burst, which is exactly the pattern we are avoiding.

    This re-lays the whole staged set end to end, preserving the same rules:
    randomised 60-180s spacing, ten minutes of rest per number, and the
    recipient's own 8am-9pm window.
    """
    rows = [
        dict(r)
        for r in store._conn().execute(
            "SELECT id, phone, from_number FROM outbox WHERE status='held' ORDER BY id"
        )
    ]
    if not rows:
        return {"rescheduled": 0}

    now = datetime.now(timezone.utc)
    cursor = now
    last_used: dict[str, datetime] = {}
    assigned: list[datetime] = []
    updates = []

    def space_out(when: datetime) -> datetime:
        """Nudge a time until it is not on top of one already assigned.

        A message deferred to its own timezone re-enters the day at whatever
        hour suits the recipient, which may be the middle of the batch. Without
        this it could land on the same second as another send.
        """
        moved = True
        while moved:
            moved = False
            for taken in assigned:
                if abs((when - taken).total_seconds()) < config.SEND_SPACING_MIN:
                    when = taken + timedelta(
                        seconds=random.randint(config.SEND_SPACING_MIN, config.SEND_SPACING_MAX)
                    )
                    moved = True
        return when

    for row in rows:
        number = row["from_number"] or ""
        ready = now if number not in last_used else (
            last_used[number] + timedelta(seconds=config.MIN_SEND_GAP_SECONDS)
        )
        slot = max(cursor, ready)

        send_at = slot
        if not sender_pool.within_quiet_hours(row["phone"], send_at):
            send_at = sender_pool.next_send_window(row["phone"], send_at)
        send_at = space_out(send_at)

        assigned.append(send_at)
        last_used[number] = send_at

        # Advance from the SLOT, never from a quiet-hours deferral. Otherwise a
        # single out-of-state recipient drags the entire day behind them: one
        # Los Angeles number at the front of a 9am Eastern batch pushed every
        # Tennessee message that followed it to 11:24, because the cursor
        # inherited a wait that belonged to one person's timezone alone.
        cursor = slot + timedelta(
            seconds=random.randint(config.SEND_SPACING_MIN, config.SEND_SPACING_MAX)
        )
        updates.append((send_at.isoformat(timespec="seconds"), row["id"]))

    with store.tx() as c:
        for not_before, row_id in updates:
            c.execute("UPDATE outbox SET not_before=? WHERE id=?", (not_before, row_id))

    # Sorted, because the times are no longer monotonic by row id: a deferred
    # recipient re-enters the day later than rows queued after them. The gap
    # that matters is between consecutive SENDS, not consecutive rows.
    times = sorted(datetime.fromisoformat(t) for t, _ in updates)
    gaps = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]
    return {
        "rescheduled": len(updates),
        "first": times[0].astimezone().strftime("%H:%M"),
        "last": times[-1].astimezone().strftime("%H:%M"),
        "span_minutes": round((times[-1] - times[0]).total_seconds() / 60),
        "min_gap_seconds": round(min(gaps)) if gaps else 0,
        "avg_gap_seconds": round(sum(gaps) / len(gaps)) if gaps else 0,
    }


def schedule_summary(plan: list[tuple[Candidate, str, str]]) -> dict:
    """Human-readable shape of a schedule, for review before release."""
    if not plan:
        return {"count": 0}
    times = [datetime.fromisoformat(t) for _, _, t in plan]
    per_number: dict[str, int] = {}
    for _, number, _ in plan:
        per_number[number] = per_number.get(number, 0) + 1
    gaps = [
        (times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)
    ]
    span = (times[-1] - times[0]).total_seconds() / 60 if len(times) > 1 else 0
    return {
        "count": len(plan),
        "first_send": times[0].astimezone().strftime("%H:%M"),
        "last_send": times[-1].astimezone().strftime("%H:%M"),
        "span_minutes": round(span),
        "avg_gap_seconds": round(sum(gaps) / len(gaps)) if gaps else 0,
        "numbers_used": len(per_number),
        "max_per_number": max(per_number.values()),
        "per_number": dict(sorted(per_number.items())),
    }


def queue(candidates: list[Candidate], touch: int, new_deal: bool = False) -> dict:
    """Register the mappings and queue the ready candidates.

    Queued as `held` so nothing leaves without `--commit`, which is the same
    gate a drafted reply passes through.

    TWO GUARDS, both from staging the same batch twice on 2026-08-31 and
    ending up with 312 held rows for 156 buyers.

      * SMS_AGENT_DRY_RUN never reached this function. It gates sends and
        CRM writes, so a dry run still wrote the whole batch to the local
        outbox while reporting itself as a dry run. Staging is a write.
      * Nothing stopped a second identical row. store.queue_message is a
        plain INSERT, and the in-batch duplicate check in build() cannot
        see a batch staged an hour ago. Released, that would have texted
        every buyer twice, which is the single worst outcome for a cold
        number.

    Returns `duplicates` so a caller can say what it skipped instead of
    silently queueing fewer than it reported.
    """
    if config.DRY_RUN:
        log.info("DRY_RUN: not staging %d candidates", len(candidates))
        return {"queued": 0, "held_back": len(candidates), "dry_run": True}

    pending = {
        store.clean_phone(r["phone"])
        for r in store._conn().execute(
            "SELECT phone FROM outbox WHERE status IN ('held','queued')"
        )
    }
    # Schedule first: spacing, rotation and per-number rest are decided for the
    # batch as a whole, not per message.
    plan = {c.phone: (n, t) for c, n, t in schedule(candidates)}
    queued = 0
    duplicates = 0
    for cand in candidates:
        if cand.status != "ready":
            continue
        if store.clean_phone(cand.phone) in pending:
            duplicates += 1
            continue
        context = {
            "owner_first": cand.first,
            "street": cand.street,
            "city": cand.city,
            "county": cand.county,
            "assigned_name": cand.sender,
        }
        ctx = {k: v for k, v in context.items() if v}
        store.map_phone(
            cand.phone,
            record_uuid=cand.record_uuid,
            first_name=cand.first,
            address=cand.street,
            context=ctx,
        )
        # Map their OTHER lines too. The June backfill found that 5 of 9 real
        # replies came from a different number than the one we texted, so
        # mapping only the target would leave most replies unroutable.
        if cand.record_uuid:
            crm.map_all_phones(cand.record_uuid, ctx)
        from_number, not_before = plan.get(cand.phone, ("", ""))
        if not from_number:
            continue
        store.ensure_conversation(cand.phone, from_number=from_number,
                                  record_uuid=cand.record_uuid)
        if new_deal:
            # Reopen a thread a human paused on the PREVIOUS deal. Without
            # this the outreach goes out but the reply lands on a paused
            # conversation and is never classified or escalated, which is
            # the worst combination: we spend the text and drop the answer.
            conv = store.get_conversation(cand.phone) or {}
            if conv.get("state") == "paused":
                store.update_conversation(
                    cand.phone, state="active",
                    paused_reason="reopened for a new deal")
        store.queue_message(
            cand.phone,
            cand.message,
            from_number=from_number,
            not_before=not_before,
            status="held",
            intent=f"seed_touch_{touch}",
            confidence=1.0,
        )
        queued += 1
    return {"queued": queued, "duplicates": duplicates,
            "held_back": sum(1 for c in candidates if c.status != "ready")}


def release(touch: int, limit: int = 0) -> int:
    """Move held seed touches into the send queue. The deliberate go/no-go."""
    intent = f"seed_touch_{touch}"
    rows = list(
        store._conn().execute(
            "SELECT id FROM outbox WHERE status='held' AND intent=? ORDER BY id"
            + (" LIMIT ?" if limit else ""),
            (intent, limit) if limit else (intent,),
        )
    )
    with store.tx() as c:
        for row in rows:
            c.execute("UPDATE outbox SET status='queued' WHERE id=?", (row["id"],))
    return len(rows)


def reason_key(reason: str) -> str:
    """Collapse a hold reason to the bucket it should be counted under.

    "dial tier Dial Fourth, not first or second" and "suppressed (opt_out)"
    are one bucket each, not one per record. Three hand-rolled copies of this
    line existed; this is the one.
    """
    return str(reason).split(":")[0].split("(")[0].strip()


def summary(candidates: list[Candidate]) -> dict:
    ready = [c for c in candidates if c.status == "ready"]
    reasons: dict[str, int] = {}
    for cand in candidates:
        for reason in cand.reasons:
            key = reason_key(reason)
            reasons[key] = reasons.get(key, 0) + 1
    return {
        "total": len(candidates),
        "ready": len(ready),
        "held": len(candidates) - len(ready),
        "held_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "capacity": sender_pool.capacity_today(),
    }
