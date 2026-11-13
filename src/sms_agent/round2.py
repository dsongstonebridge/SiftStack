"""Tier 2 SMS Prospecting: touches 5-10 for records we still have not reached.

Jeff, 2026-11-13. A "week" is the 5 call attempts, never calendar days.

  1. Prospecting week: 5 call attempts + touches 1-4 on the original numbers.
  2. No answers: the record goes to deep prospecting (DP).
  3. DP week: 5 call attempts again. The NEW numbers get touches 1-4; the old
     numbers get nothing that week (deliberate, do not change it).
  4. After the DP week the team sets ROUND_STATUS on the record. Every
     no-answer number, OLD AND NEW, gets 6 more texts, Mon/Wed/Fri.
  5. A number marked Correct from its voicemail greeting, where nobody has
     talked to them: the record skips DP and gets the status after step 1.

These are UNCONTACTED records, not leads; reaching them is the whole point.

The status is the opt-in, and this module double-checks every record before a
text: still has the status, nobody replied, nobody talked to them on any
number, not mid deep prospect, and every number rule that touches 1-4 obey
(seed.build, seed.apply_correct_number, seed.all_textable_rows, the dead-name
guards). A number whose touch 4 promised "last text from me" never gets the
round. The worker re-checks status, replies and talks right before each send.

Round texts are counted by the touch number stored with each send
(outbox/messages intent "seed_touch_N"), not by their wording.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import campaign, config, crm, dp_numbers, seed, store
from .knowledge import touches

log = logging.getLogger(__name__)

_DAY = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

PROMISED = "touch 4 promised it was the last text"
BACKLOG = "touch 4 went out before the round existed"
NOT_FINISHED_1_4 = "touches 1-4 not finished"
FINISHED = "finished the round"
STAGED_DAYS_KEY = "round2_staged_days"
# Records whose touch 4 went out within this many days are checked for the
# "looks eligible, no status" list. Bounded on purpose: only records we texted.
SUGGEST_LOOKBACK_DAYS = 21
SUGGEST_MAX_RECORDS = 60


def round_days() -> set:
    return {_DAY[d.strip().lower()[:3]] for d in config.ROUND_DAYS.split(",")
            if d.strip().lower()[:3] in _DAY}


def round_count() -> int:
    return max(0, min(config.ROUND_TEXTS, len(touches.ROUND_UNKNOWN)))


def last_round_touch() -> int:
    return 4 + round_count()


def is_round_status(status) -> bool:
    return seed.lead_status_name(status).strip().lower() == config.ROUND_STATUS.strip().lower()


# --------------------------------------------------------------- history

def touch_of(intent: Optional[str]) -> int:
    """The touch number stored with one of OUR touch sends ("seed_touch_7" ->
    7), else 0. Only that prefix counts, so no other intent can pass for one."""
    intent = intent or ""
    if not intent.startswith("seed_touch_"):
        return 0
    tail = intent[len("seed_touch_"):]
    return int(tail) if tail.isdigit() else 0


def history(sms_rows: Optional[list] = None) -> dict:
    """phone -> {"touches": {n}, "last": iso, "t4": iso, "promised": bool}.

    Our own sends carry their touch number (intent "seed_touch_N"); that wins.
    Anything else (hand-typed texts, smrtPhone's log) falls back to the wording
    match touches 1-4 use. ANY outbound text that promised to stop marks the
    number, whichever touch it was. `t4` is when touch 4 last went out.
    """
    out: dict = {}

    def note(phone: str, touch: int, when: str, body: str) -> None:
        phone = store.clean_phone(phone)
        if not phone:
            return
        e = out.setdefault(phone, {"touches": set(), "last": "", "t4": "", "promised": False})
        if touch:
            e["touches"].add(touch)
            if when and when > e["last"]:
                e["last"] = when
            if touch == 4 and when and when > e["t4"]:
                e["t4"] = when
        if touches.promised_stop(body):
            e["promised"] = True

    for r in store._conn().execute(
            "SELECT phone, body, intent, created_at FROM messages WHERE direction='out'"):
        t = touch_of(r["intent"]) or campaign.identify_touch(r["body"]) or 0
        note(r["phone"], t, str(r["created_at"] or ""), r["body"])

    from . import reconcile

    for r in sms_rows or []:
        if (r.get("direction") or "").lower() != "outbound":
            continue
        body = reconcile._clean(r.get("content"))
        note(r.get("toNum"), campaign.identify_touch(body) or 0,
             str(r.get("date") or r.get("createdAt") or ""), body)
    return out


def next_round_touch(entry: Optional[dict], today: date) -> tuple[Optional[int], str]:
    """Which round touch (5-10) this number is due today, or why not.

    Mon/Wed/Fri only, at least ROUND_MIN_GAP_DAYS after the previous text. With
    touch 4 on a Thursday that is Mon, Wed, Fri, Mon, Wed, Fri.
    """
    had = (entry or {}).get("touches") or set()
    done = max(had) if had else 0
    if done < 4:
        return None, NOT_FINISHED_1_4
    if (entry or {}).get("promised"):
        return None, PROMISED
    t4 = ((entry or {}).get("t4") or "")[:10]
    if config.ROUND_TOUCH4_AFTER and (not t4 or t4 < config.ROUND_TOUCH4_AFTER):
        return None, BACKLOG
    if done >= last_round_touch():
        return None, FINISHED
    if today.weekday() not in round_days():
        return None, "not a round day"
    last = (entry or {}).get("last") or ""
    if last:
        try:
            when = campaign._local_date(datetime.fromisoformat(last.replace("Z", "+00:00")))
        except ValueError:
            when = None
        if when and (today - when).days < config.ROUND_MIN_GAP_DAYS:
            return None, f"touch {done} was {(today - when).days}d ago"
    return done + 1, ""


# --------------------------------------------------------------- the plan

@dataclass
class RoundPlan:
    candidates: list = field(default_factory=list)
    # (record_uuid, street, reason): has the status but nothing went out.
    held: list = field(default_factory=list)
    # (record_uuid, street, why): looks like it belongs, no status yet.
    suggested: list = field(default_factory=list)
    # (record_uuid, street): every number finished touch 10, no reply.
    finished: list = field(default_factory=list)
    error: str = ""
    skipped: str = ""


def _smartskip_numbers(rec: dict) -> set:
    owner = rec.get("owner") if isinstance(rec.get("owner"), dict) else {}
    return {store.clean_phone(p.get("number")) for p in owner.get("phones") or []
            if isinstance(p, dict) and dp_numbers.SMARTSKIP_TAG in dp_numbers._tag_names(p)}


# A talk on a number since marked Wrong or Dead was a talk with a stranger:
# the owner is still uncontacted.
_STRANGER_STATUSES = {"WRONG", "WRONG_DNC", "DEAD"}


def _talked(events: list, rec: dict) -> str:
    owner = rec.get("owner") if isinstance(rec.get("owner"), dict) else {}
    for p in owner.get("phones") or []:
        if not isinstance(p, dict) or (p.get("status") or "").upper() in _STRANGER_STATUSES:
            continue
        ph = store.clean_phone(p.get("number"))
        why = seed.spoken_on(events, ph) if len(ph) == 10 else ""
        if why:
            return f"talked to them on ...{ph[-4:]} ({why})"
    return ""


def _replied(record_uuid: str) -> str:
    """Record-wide: only a reply that settled the owner (engine sets
    record_replied). A wrong number or STOP stops only its own line (Jeff
    2026-10-07), which seed.build checks number by number."""
    if seed._record_replied(record_uuid):
        return "a reply settled this record"
    return ""


def _dp_call_uuids() -> tuple[set, bool]:
    """Records sitting in a deep prospect call preset (DP week not over), and
    whether every such preset could be read."""
    uuids, ok = set(), True
    for src in campaign.SOURCES:
        if not src.new_numbers_only:
            continue
        rows, matched = seed.from_preset(src.title, keep_unresolved=True, stats={})
        if not matched:
            ok = False
            continue
        uuids |= {r.get("uuid") for r in rows if r.get("uuid")}
    return uuids, ok


def _first_texted() -> dict:
    """phone -> first date we texted it (what dp_numbers.classify expects)."""
    out: dict = {}
    for q in ("SELECT phone, min(created_at) w FROM messages WHERE direction='out' GROUP BY phone",
              "SELECT phone, min(sent_at) w FROM sends GROUP BY phone"):
        for r in store._conn().execute(q):
            ph, w = store.clean_phone(r["phone"]), str(r["w"] or "")[:10]
            if ph and w and (ph not in out or w < out[ph]):
                out[ph] = w
    return out


def _vet_record(row: dict, rec: dict, events: list, hist: dict, today: date,
                dnc: set, dp_busy: set, dp_ok: bool, texted: dict,
                already: set) -> tuple[list, str, bool]:
    """(candidates, why none, finished) for one record with the round status."""
    uuid = row.get("uuid") or ""
    if not is_round_status(rec.get("status")):
        return [], f"status is now {seed.lead_status_name(rec.get('status')) or 'blank'}", False
    if seed.lead_status_blocks(rec.get("status")):
        return [], f"status {seed.lead_status_name(rec.get('status'))} blocks texting", False
    run = dp_numbers.run_for(uuid)
    # Deep prospected but the run folder is missing (Jeff 2026-11-13: still
    # texted). We cannot tell the owner's SmartSkip numbers from a relative's,
    # so those numbers get no name rather than the owner's.
    unknown_holder = _smartskip_numbers(rec) if run is None else set()
    if uuid in dp_busy:
        return [], "still in the deep prospect call attempts", False
    if run and not dp_ok:
        return [], "could not read the deep prospect presets to confirm the DP week is over", False
    why = _replied(uuid) or _talked(events, rec)
    if why:
        return [], why, False

    new = dp_numbers.new_numbers(dp_numbers.classify(rec, run, texted, events)) if run else frozenset()

    # Same number rules as touches 1-4: a Correct + primary number narrows the
    # record to that number, otherwise every Dial First/Second textable number.
    base = dict(row)
    swapped, why = seed.apply_correct_number(base, dnc)
    if swapped is None:
        return [], why, False
    rows = [swapped] if swapped is not base else seed.all_textable_rows(base, dnc)
    if not rows:
        return [], "no Dial First or Second phone", False

    out, reasons = [], []
    for r in rows:
        phone = store.clean_phone(r.get("phone"))
        if phone in already:
            reasons.append("texted by touches 1-4 today")
            continue
        touch, why = next_round_touch(hist.get(phone), today)
        if touch is None:
            reasons.append(why)
            continue
        r = dict(r)
        r.pop("_needs_best_phone", None)
        if phone in new:
            # A deep prospect's own number: its person decides name + copy.
            r["_dp_new"] = new
        elif run and run.get("owner_dead"):
            r["_variant"] = "heir"
        elif phone in unknown_holder:
            # Whose number this is is unknown: no name, never the owner's.
            r["first"] = r["last"] = r["owner"] = ""
        if r.get("dial_tier") == "Correct" and r.get("_variant") != "heir":
            r["_variant"] = "known"
        built = seed.build([r], touch=touch)
        cand = built[0] if built else None
        if not cand or cand.status != "ready":
            reasons.extend(cand.reasons if cand else ["seed produced nothing"])
            continue
        cand.touch = touch  # type: ignore[attr-defined]
        out.append(cand)
    finished = not out and bool(reasons) and all(x == FINISHED for x in reasons)
    return out, ("; ".join(sorted(set(reasons))) if not out else ""), finished


def build(today: Optional[date] = None, sms_rows: Optional[list] = None,
          already: Optional[set] = None, suggest: bool = True) -> RoundPlan:
    """Assemble today's round. Sends nothing, stages nothing."""
    plan = RoundPlan()
    today = today or campaign._local_date(datetime.now(timezone.utc))
    already = already or set()
    if today.weekday() not in round_days():
        plan.skipped = f"{today:%A} is not a round day"
        return plan
    if not config.ROUND_PRESET:
        plan.error = "SMS_AGENT_ROUND_PRESET is not set (the preset listing the round status)"
        return plan

    stats: dict = {}
    rows, matched = seed.from_preset(config.ROUND_PRESET, keep_unresolved=True, stats=stats)
    if not matched:
        plan.error = f"round preset '{config.ROUND_PRESET}' not found"
        return plan
    dnc = stats.pop("_dnc_numbers", set())
    if sms_rows is None:
        # Texts typed by hand count toward the gap and the promise check.
        from . import reconcile
        try:
            sms_rows = reconcile.fetch_log(pages=6)
        except Exception as exc:  # noqa: BLE001 - same fallback as campaign.build
            log.warning("could not read the SMS log (%s); using our history only", exc)
            sms_rows = []
    hist = history(sms_rows)
    dp_busy, dp_ok = _dp_call_uuids()
    texted = _first_texted()

    seen: set = set()
    for row in rows:
        uuid = row.get("uuid") or ""
        if not uuid or uuid in seen:
            continue
        seen.add(uuid)
        street = row.get("street") or ""
        rec = crm.get_record(uuid)
        if not rec:
            plan.held.append((uuid, street, "could not read the record"))
            continue
        events = crm.activity_log(uuid)
        if events is None:
            plan.held.append((uuid, street, "could not read the activity log"))
            continue
        cands, why, finished = _vet_record(row, rec, events, hist, today, dnc,
                                           dp_busy, dp_ok, texted, already)
        if cands:
            plan.candidates.extend(cands)
        elif finished:
            plan.finished.append((uuid, street))
        else:
            plan.held.append((uuid, street, why))

    if suggest:
        plan.suggested = suggestions(hist, seen | dp_busy, dp_busy, today)
    return plan


def suggestions(hist: dict, skip_uuids: set, dp_busy: set, today: date) -> list:
    """Records that look like they belong in the round but do not have the
    status. Suggestions only, for a person to judge; never texted.

    Bounded to records whose touch 4 went out in the last SUGGEST_LOOKBACK_DAYS.
    """
    cutoff = (today - timedelta(days=SUGGEST_LOOKBACK_DAYS)).isoformat()
    uuids: list = []
    for phone, e in hist.items():
        if 4 not in e["touches"] or max(e["touches"]) > 4 or e["promised"]:
            continue
        if (e["last"] or "")[:10] < cutoff or (e["t4"] or "")[:10] < config.ROUND_TOUCH4_AFTER:
            continue
        uuid = (store.lookup_phone(phone) or {}).get("record_uuid") or ""
        if uuid and uuid not in skip_uuids and uuid not in uuids:
            uuids.append(uuid)
    out = []
    for uuid in uuids[:SUGGEST_MAX_RECORDS]:
        rec = crm.get_record(uuid)
        if not rec or is_round_status(rec.get("status")) or seed.lead_status_blocks(rec.get("status")):
            continue
        events = crm.activity_log(uuid)
        if events is None:
            continue
        if _replied(uuid) or _talked(events, rec):
            continue
        street = ((rec.get("address") or {}).get("street") if isinstance(rec.get("address"), dict)
                  else "") or ""
        status = seed.lead_status_name(rec.get("status")) or "no status"
        if dp_numbers.run_for(uuid):
            out.append((uuid, street, f"deep prospect week looks done ({status})"))
        elif seed.correct_phones(rec) or any(
                (p.get("status") or "").upper() == "CORRECT"
                for p in ((rec.get("owner") or {}).get("phones") or []) if isinstance(p, dict)):
            attempts = rec.get("predictivecall_attempts")
            done = "" if attempts is None else f", {attempts} call attempts"
            out.append((uuid, street, f"Correct number, never talked to them{done} ({status})"))
    return out


# --------------------------------------------------------------- send time

def still_ok(record_uuid: str, phone: str) -> tuple[Optional[bool], str]:
    """Send-time re-check for a round text. (True, ""), (False, why) to cancel,
    (None, why) to hold when something could not be read."""
    if not record_uuid:
        return False, "no record to re-check"
    rec = crm.get_record(record_uuid, fresh=True)
    if rec is None:
        return None, "could not re-read the record"
    if not is_round_status(rec.get("status")):
        return False, f"status is now {seed.lead_status_name(rec.get('status')) or 'blank'}"
    why = _replied(record_uuid)
    if why:
        return False, why
    if (store.get_conversation(phone) or {}).get("last_inbound"):
        return False, "this number replied"
    events = crm.activity_log(record_uuid)
    if events is None:
        return None, "could not read the activity log"
    why = _talked(events, rec)
    if why:
        return False, why
    return True, ""


# --------------------------------------------------------------- staging

def note_staged_day(today: date) -> int:
    """Record that a round was staged today; returns how many days so far."""
    try:
        days = set(json.loads(store.get_meta(STAGED_DAYS_KEY) or "[]"))
    except ValueError:
        days = set()
    days.add(today.isoformat())
    store.set_meta(STAGED_DAYS_KEY, json.dumps(sorted(days)))
    return len(days)


def claim_finished(plan: RoundPlan) -> None:
    """Keep only records not listed as finished before, and remember them, so
    each one is posted once. The status itself is left alone (Jeff's call)."""
    fresh = []
    for uuid, street in plan.finished:
        if store.get_meta(f"round2_finished:{uuid}") != "1":
            store.set_meta(f"round2_finished:{uuid}", "1")
            fresh.append((uuid, street))
    plan.finished = fresh


def release_allowed(staged_days: int) -> bool:
    """The first ROUND_HELD_DAYS round days stay HELD for Jeff to release."""
    return staged_days > config.ROUND_HELD_DAYS


def review_text(plan: RoundPlan, per_touch: dict, held_for_review: bool) -> str:
    def line(items, n=40):
        rows = [f"  {s or u[:8]}: {w}" for u, s, w in items[:n]]
        if len(items) > n:
            rows.append(f"  ... and {len(items) - n} more")
        return "\n".join(rows) or "  none"

    staged = ", ".join(f"touch {t}: {n}" for t, n in sorted(per_touch.items())) or "nothing"
    parts = [
        f"Staged: {staged}.",
        ("HELD for review. Read the lists, then release with "
         "`python src/sms_agent/cli.py release --touch N` for each touch above."
         if held_for_review and per_touch else ""),
        f"\nHas the status but held ({len(plan.held)}):\n{line(plan.held)}",
        f"\nLooks eligible, no status yet ({len(plan.suggested)}):\n{line(plan.suggested)}",
        f"\nFinished the round, no reply ({len(plan.finished)}):\n"
        + ("\n".join(f"  {s or u[:8]}" for u, s in plan.finished[:40]) or "  none"),
    ]
    return "\n".join(p for p in parts if p)
