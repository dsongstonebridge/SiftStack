"""Which numbers on a deep-prospected record are NEW (Tulsa fork, 2026-10-26).

Jeff: a record lands in the DP presets because its first skip trace gave us
bad numbers. The deep prospect's SmartSkip run then added new ones. Those new
Dial First / Dial Second numbers get the standard four touches from touch 1,
exactly like an FTM number. The old numbers are never texted again.

The `SmartSkip` phone tag alone does NOT prove a number is new: when SmartSkip
returns a number that was already on the record, deep_prospect_smartskip tags
it `SmartSkip` too (and leaves its old tier on it). So a number is new only when
ALL of these hold:

  1. it carries the `SmartSkip` phone tag;
  2. it carries no older source tag (Tracerfy, DataSift, Pre-existing);
  3. it appears in that record's own SmartSkip export (the run folder);
  4. we did not text it BEFORE the deep prospect started (our outbox, our
     sends, smrtPhone's log). Texts after that are this campaign's own touches;
     counting them would end every sequence at touch 1;
  5. nobody called it, or was called from it, before the deep prospect started;
  6. the record's activity log shows no earlier life for it: not added, skip
     traced, or given a status before the deep prospect started. Numbers from
     traces older than the source tags carry no source tag at all (17248 S
     91st East Ave: added 7/8, marked Wrong/Dead/No Answer 7/18, re-returned
     by SmartSkip 10/23 and tagged only `SmartSkip`).

A number Jeff clears by hand (SMS_AGENT_DP_ALLOW_NUMBERS) skips all of this.

No run folder for the record means we cannot prove anything, so the record is
held, never guessed at.

The export also says WHO each number belongs to. That decides the greeting
(the person's own first name) and the copy: the standard owner touches when the
person is an owner of record, the heir touches ("who looks after {addr}") for
everyone else (Jeff, 2026-10-26).
"""
from __future__ import annotations

import csv
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

from . import config, store

log = logging.getLogger(__name__)

SMARTSKIP_TAG = "SmartSkip"
OLD_SOURCE_TAGS = {"Tracerfy", "DataSift", "Pre-existing"}
CALL_EVENTS = {"owner.call.made", "owner.call.answered"}
HISTORY_EVENTS = {"owner.phones.added", "owner.sent.skiptrace", "owner.phone.status.updated"}

NO_RUN_REASON = "no deep prospect run folder for this record"
NO_NEW_REASON = "no new SmartSkip Dial First/Second number on this record"

_CACHE_TTL = 600
_index: dict = {"at": 0.0, "dir": None, "runs": {}}


def _tag_names(obj: dict) -> set:
    return {
        (t.get("title") or t.get("name") or t.get("tag")) if isinstance(t, dict) else str(t)
        for t in (obj.get("tags") or [])
    }


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def _read_export(path: Path) -> list[dict]:
    """[{first, last, relationship, phones: [10-digit]}] from a SmartSkip export."""
    people = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            phones = []
            for i in range(1, 20):
                n = store.clean_phone(row.get(f"Phone {i} number") or "")
                if len(n) == 10 and n not in phones:
                    phones.append(n)
            people.append({
                "first": (row.get("First Name") or "").strip(),
                "last": (row.get("Last Name") or "").strip(),
                "relationship": (row.get("Relationship") or row.get("Possible Type") or "").strip(),
                "phones": phones,
            })
    return people


def _load_runs(root: Path) -> dict:
    """property_uuid -> {"started": "YYYY-MM-DD", "people": [...], "folders": [...]}."""
    runs: dict = {}
    for state_path in sorted(root.glob("*/state.json")):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("unreadable deep prospect state %s: %s", state_path, exc)
            continue
        uuid = state.get("property_uuid")
        export = state_path.parent / (state.get("export") or "smartskip_export.csv")
        if not uuid or not export.exists():
            continue
        try:
            people = _read_export(export)
        except (OSError, csv.Error) as exc:
            log.warning("unreadable SmartSkip export %s: %s", export, exc)
            continue
        run = runs.setdefault(uuid, {"started": "", "people": [], "folders": []})
        started = str(state.get("started") or "")
        # Two runs on one property: the EARLIEST start decides what counts as
        # "called before the deep prospect", which is the stricter reading.
        if started and (not run["started"] or started < run["started"]):
            run["started"] = started
        run["people"].extend(people)
        run["folders"].append(state_path.parent.name)
    return runs


def run_for(record_uuid: str) -> Optional[dict]:
    """The deep prospect run(s) for this property, or None."""
    if not config.DP_RUNS_DIR or not record_uuid:
        return None
    root = Path(config.DP_RUNS_DIR)
    now = time.time()
    if _index["dir"] != str(root) or now - _index["at"] > _CACHE_TTL:
        _index.update(at=now, dir=str(root), runs=_load_runs(root) if root.is_dir() else {})
    return _index["runs"].get(record_uuid)


def person_for(run: dict, phone: str) -> Optional[dict]:
    n = store.clean_phone(phone)
    for p in run.get("people") or []:
        if n in p["phones"]:
            return p
    return None


def _called_before(events: list, phone: str, started: str) -> str:
    """The day it was called (or called in from) before `started`, or ""."""
    if not started:
        return ""
    for ev in events or []:
        if ev.get("event_type") not in CALL_EVENTS:
            continue
        call = (ev.get("payload") or {}).get("call") or {}
        nums = {store.clean_phone(call.get("destination_number")),
                store.clean_phone(call.get("origin_number"))}
        if store.clean_phone(phone) not in nums:
            continue
        day = str(ev.get("timestamp") or "")[:10]
        if day and day < started:
            return day
    return ""


def _history_before(events: list, phone: str, started: str) -> str:
    """'<event> <day>' if the record knew this number before `started`, or ""."""
    if not started:
        return ""
    for ev in events or []:
        kind = ev.get("event_type")
        if kind not in HISTORY_EVENTS:
            continue
        day = str(ev.get("timestamp") or "")[:10]
        if day and day < started and phone in json.dumps(ev.get("payload") or {}):
            return f"{kind.split('.', 1)[1].replace('.', ' ')} {day}"
    return ""


def classify(rec: dict, run: Optional[dict], texted: dict,
             events: Optional[list]) -> dict:
    """number -> "" when NEW, else why it is an old number. Every phone on the
    record gets an answer, so the review can show what was left out.

    `texted` is {phone: first date we texted it (YYYY-MM-DD)}."""
    owner = rec.get("owner") if isinstance(rec.get("owner"), dict) else {}
    out: dict = {}
    for p in owner.get("phones") or []:
        if not isinstance(p, dict):
            continue
        n = store.clean_phone(p.get("number"))
        if len(n) != 10:
            continue
        tags = _tag_names(p)
        started = (run or {}).get("started") or ""
        if n in config.DP_ALLOW_NUMBERS:
            out[n] = ""
        elif run is None:
            out[n] = NO_RUN_REASON
        elif SMARTSKIP_TAG not in tags:
            out[n] = "not from SmartSkip (old skip trace)"
        elif tags & OLD_SOURCE_TAGS:
            out[n] = "was already on the record before SmartSkip (" + ", ".join(sorted(tags & OLD_SOURCE_TAGS)) + ")"
        elif person_for(run, n) is None:
            out[n] = "not in this record's SmartSkip results"
        elif n in texted and (not started or str(texted[n])[:10] < started):
            out[n] = f"texted {str(texted[n])[:10]}, before the deep prospect"
        elif events is None:
            out[n] = "could not read the call log"
        else:
            day = _called_before(events, n, started)
            hist = "" if day else _history_before(events, n, started)
            out[n] = (f"called {day}, before the deep prospect" if day
                      else f"on the record before the deep prospect ({hist})" if hist else "")
    return out


def new_numbers(verdicts: dict) -> frozenset:
    return frozenset(n for n, why in verdicts.items() if not why)


def _is_owner(person: dict, rec: dict) -> bool:
    """Is this SmartSkip person an owner of record (primary or secondary)?"""
    first, last = _norm_name(person.get("first")), _norm_name(person.get("last"))
    if not first or not last:
        return False
    owners = []
    owner = rec.get("owner") if isinstance(rec.get("owner"), dict) else {}
    owners.append(owner)
    owners += [o for o in (rec.get("secondary_owners") or []) if isinstance(o, dict)]
    for o in owners:
        of, ol = _norm_name(o.get("first_name")), _norm_name(o.get("last_name"))
        if ol == last and of and (of == first or of.startswith(first) or first.startswith(of)):
            return True
    return False


def addressee(rec: dict, run: Optional[dict], phone: str) -> tuple[str, str]:
    """(first name to greet, "owner" | "heir") for a new number.

    The greeting is the PERSON's own first name from the export, never the
    owner's on a relative's phone. Owner copy only for an owner of record;
    anyone else gets the heir copy, which asks who looks after the house and
    never mentions a death.
    """
    person = person_for(run or {}, phone)
    if not person:
        return "", "heir"
    variant = "owner" if _is_owner(person, rec) else "heir"
    return person.get("first") or "", variant
