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
                "dead": (row.get("Deceased") or "").strip().lower() == "true",
                "phones": phones,
            })
    return people


def _dead_from_basis(basis: str) -> set:
    """{(first, last)} named as having died in the run's research basis.

    Needs first AND last in the phrase, so "Lenord Sr. very likely died" does
    not mark his living namesake son (the export's Subject) dead.
    """
    out = set()
    for m in re.finditer(r"([A-Z][A-Za-z.' ]{2,60}?)\s+(?:died|passed away|is deceased)", basis):
        words = [_norm_name(w) for w in m.group(1).split() if _norm_name(w)]
        if len(words) >= 2:
            out.add((words[0], words[-1]))
    return out


# Obituary-check verdicts that say nothing ties an obituary to the owner.
# Anything else (POSSIBLE, PROBABLE, LIKELY, CONFIRMED, unreadable, missing)
# means we are not confident the owner is alive.
OWNER_ALIVE_VERDICTS = ("NO OBITUARY FOUND", "DIFFERENT PERSON", "UNRELATED", "OWNER IS A SURVIVOR")


def _owner_maybe_dead(folder: Path) -> bool:
    path = folder / "obituary_check.json"
    try:
        summary = str(json.loads(path.read_text(encoding="utf-8")).get("summary") or "")
    except (OSError, ValueError):
        return True
    return not summary.upper().startswith(OWNER_ALIVE_VERDICTS)


def _is_subject(person: dict) -> bool:
    return (person.get("relationship") or "").strip().lower() == "subject"


def is_dead(run: Optional[dict], person: Optional[dict]) -> bool:
    if not person:
        return False
    if person.get("dead"):
        return True
    if (run or {}).get("owner_dead") and _is_subject(person):
        return True
    key = (_norm_name(person.get("first")), _norm_name(person.get("last")))
    return key in ((run or {}).get("dead") or set())


def heir_namesake_ok(run: Optional[dict], phone: str) -> bool:
    """May this number's living heir be greeted by a first name a dead person
    on the record also had? (Jeff 2026-10-28: never a dead person's name unless
    it is also the heir's name.)

    Only when the number belongs to a living non-subject relative, no dead
    person is listed on the same number (it could be the decedent's old
    phone), and every dead person with that first name is the subject the deep
    prospect was about. A dead uncle or a death we cannot place keeps no name.

    The heir's LAST name must differ from every dead namesake's. SmartSkip
    often lists the decedent a second time as a "Relative" under her own full
    name (2719 S 80th E Ave: Carolyn Gray, dead owner, also a Carolyn Gray
    relative row), and a same-full-name son cannot be told apart from that.
    """
    person = person_for(run or {}, phone)
    if not person or _is_subject(person) or is_dead(run, person):
        return False
    first = _norm_name(person.get("first"))
    last = _norm_name(person.get("last"))
    if not first or not last:
        return False
    people = (run or {}).get("people") or []
    dead_keys = {(_norm_name(p.get("first")), _norm_name(p.get("last")))
                 for p in people if is_dead(run, p)} | set((run or {}).get("dead") or set())
    if any(f == first and (l == last or not l) for f, l in dead_keys):
        return False
    n = store.clean_phone(phone)
    if any(n in p["phones"] and is_dead(run, p) for p in people):
        return False
    key = lambda p: (_norm_name(p.get("first")), _norm_name(p.get("last")))  # noqa: E731
    subjects = {key(p) for p in people if _is_subject(p)}
    dead_same = [p for p in people if is_dead(run, p) and _norm_name(p.get("first")) == first]
    basis_same = {k for k in ((run or {}).get("dead") or set()) if k[0] == first}
    if not dead_same and not basis_same:
        return False
    # The death must be CONFIRMED (SmartSkip flag or "<name> died" in the
    # research), not merely an obituary check that could not clear the owner:
    # 200 Oak Ridge Dr's owner Kimberly Welch is "believed living", and its
    # Kimberly Wilson relative may be her under her maiden name.
    if any(not (p.get("dead") or key(p) in ((run or {}).get("dead") or set())) for p in dead_same):
        return False
    # The heir's surname inside the decedent's own full name ("Bonnie Marie
    # Pingrey Baker died") means a maiden name: likely the decedent herself.
    for m in re.finditer(r"([A-Z][A-Za-z.' ]{2,60}?)\s+(?:died|passed away|is deceased)",
                         str((run or {}).get("basis") or "")):
        words = {_norm_name(w) for w in m.group(1).split()}
        if first in words and last in words:
            return False
    # A dead "Relative" row under the subject's own full name is the subject.
    return (all(_is_subject(p) or key(p) in subjects for p in dead_same)
            and all(k in subjects for k in basis_same))


def dead_first_names(run: Optional[dict]) -> set:
    """Lower-case first names of everyone on this run known to be dead."""
    names = {f for f, _ in ((run or {}).get("dead") or set())}
    for p in (run or {}).get("people") or []:
        if is_dead(run, p):
            names.add(_norm_name(p.get("first")))
    return names - {""}


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
        run = runs.setdefault(uuid, {"started": "", "people": [], "folders": [], "dead": set(),
                                     "owner_dead": False})
        # Who died, per the deep prospect's own research: "<First ... Last> died".
        # Jeff, 2026-10-26: never greet a dead person by name.
        run["dead"] |= _dead_from_basis(str(state.get("basis") or ""))
        run["basis"] = " ".join(x for x in (run.get("basis", ""), str(state.get("basis") or "")) if x)
        # The obituary check outranks the research summary (Jeff 2026-10-26,
        # 2719 S 80th E Ave: summary said "believed LIVING", obituary check
        # said "LIKELY THE OWNER'S OBITUARY", and the owner had died). Any
        # verdict short of "nothing found for the owner" means the owner may
        # be dead, so the owner's name never goes in a text.
        if _owner_maybe_dead(state_path.parent):
            run["owner_dead"] = True
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
    """Who this number belongs to. A number SmartSkip lists under several
    people (the decedent AND a daughter, 4527 W Madison Pl) goes to a LIVING
    person; the first row is often the dead subject. Only when every holder is
    dead is a dead person returned, and addressee() then sends it with no name."""
    n = store.clean_phone(phone)
    holders = [p for p in run.get("people") or [] if n in p["phones"]]
    for p in holders:
        if not is_dead(run, p):
            return p
    return holders[0] if holders else None


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
        # A dead or possibly dead owner's own number is still texted when it
        # passes the normal rules (Jeff 2026-10-27: a death only ever takes the
        # NAME out of the text, never the text itself; addressee() handles that).
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


def _is_owner(person: dict, rec: dict, run: Optional[dict] = None) -> bool:
    """Is this SmartSkip person an owner of record (primary or secondary)?

    A relative who shares a DEAD owner's name (John Webster's son John
    Webster, 1735 E 61st St) is a namesake, not the owner."""
    first, last = _norm_name(person.get("first")), _norm_name(person.get("last"))
    if not first or not last:
        return False
    if (person.get("relationship") or "").lower() != "subject" and any(
            is_dead(run, p) and _norm_name(p.get("first")) == first and _norm_name(p.get("last")) == last
            for p in (run or {}).get("people") or []):
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
    if is_dead(run, person):
        # Known dead: whoever answers now is family, so the heir copy, no name.
        # Only POSSIBLY dead (an obituary check short of a clear answer): they
        # may well be the one who answers, so the owner copy, still no name.
        possibly = (run or {}).get("owner_dead") and _is_subject(person) and not (
            person.get("dead") or (_norm_name(person.get("first")), _norm_name(person.get("last")))
            in ((run or {}).get("dead") or set()))
        return "", "owner" if possibly and _is_owner(person, rec, run) else "heir"
    variant = "owner" if _is_owner(person, rec, run) else "heir"
    first = person.get("first") or ""
    # Jeff, 2026-10-26: never put a dead person's name in a text. A living
    # relative who shares a decedent's first name gets no name either, unless
    # heir_namesake_ok() places that death on the subject and not on this
    # number (Jeff 2026-10-28: fine when it is also the heir's name).
    if _norm_name(first) in dead_first_names(run) and not heir_namesake_ok(run, phone):
        first = ""
    return first, variant
