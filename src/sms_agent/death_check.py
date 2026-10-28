"""Never put a dead person's name in a text (Jeff, 2026-10-26).

Two misses in one day, each from trusting ONE source:
  - 4527 W Madison Pl: SmartSkip listed the daughter's numbers under the dead
    owner first, and the greeting took the first row ("Hi Bonnie").
  - 2719 S 80th E Ave: the deep prospect summary said the owner was "believed
    LIVING" while the property Message Board said "Decedent: Carolyn J. Gray |
    Died: 6/9/2026". Nothing read the board.

So a first name may go in a text only when NO source on the record ties that
name to a death. Every source is read: the property Message Board, the owner
Message Board, the record's notes and tags, the activity log's notes, and for a
deep-prospected record the research summary, the DP report, the research
notes, the obituary check and SmartSkip's deceased flags.

Rules, all leaning toward NO name:
  - a sentence that mentions a death bans every candidate first name in it
    (survivor lists are skipped: "survived by his wife Hannah" names the
    living);
  - "owner deceased" with no name bans the owner's and the traced subject's
    first names;
  - a record tagged deceased / obituary bans the owner's first name;
  - an obituary check that does not clear the owner bans the subject;
  - if any CRM source cannot be read, NO first name is allowed at all.

Over-banning costs a no-name greeting, which reads fine. Under-banning texts a
family their dead mother's name.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Optional

import requests

from . import config, crm, dp_numbers

log = logging.getLogger(__name__)

DEATH_RX = re.compile(
    r"\b(died|dies|passed away|passed on|deceased|decedent|date of death|dod|funeral|"
    r"in memory of|predeceased|late (?:husband|wife|mother|father|son|daughter)|"
    r"obituar\w*|estate of|death)\b", re.I)
SURVIVOR_RX = re.compile(
    r"\b(survived by|is survived|leaves behind|survivors|her children|his children)\b", re.I)
OWNER_DEAD_RX = re.compile(
    r"\b(owner|borrower)\b[^.\n]{0,40}\b(deceased|died|passed away|is dead)\b", re.I)
DECEASED_TAGS = {"deceased", "owner deceased", "obituary"}
ALL = "*"  # sentinel: nothing could be verified, no first name at all

_TTL = 600
_cache: dict = {}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def _sentences(text: str):
    for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])|\n+|\s\|\s(?=[A-Z][a-z]+:)", text or ""):
        s = s.strip()
        if s:
            yield s


# Template text the deep prospect writes on EVERY record. It mentions death
# but names nobody dead; left in, it bans every owner on every DP record. The
# obituary verdict itself is handled by dp_numbers' owner_dead rule.
BOILERPLATE_RX = re.compile(
    r"\(living owner:[^)]*\)|^\s*OBITUARY CHECK:.*$", re.I | re.M)


def banned_from_texts(texts, candidates: set, subject_names: set) -> set:
    """First names (normalized) a death mention ties to, from free text."""
    out: set = set()
    for text in texts:
        text = BOILERPLATE_RX.sub(" ", text or "")
        for s in _sentences(text):
            if not DEATH_RX.search(s):
                continue
            if SURVIVOR_RX.search(s) and not re.search(r"\b(died|deceased|decedent)\b", s, re.I):
                continue
            words = {_norm(w) for w in re.findall(r"[A-Za-z']+", s)}
            out |= {c for c in candidates if c in words}
            if OWNER_DEAD_RX.search(s):
                out |= subject_names
    return out


def _get(path: str):
    key = (config.REISIFT_API_KEY or "").strip()
    if not key:
        raise RuntimeError("no CRM key")
    url = "https://apiv2.reisift.io" + path
    for attempt in range(4):
        r = requests.get(url, headers={"Authorization": f"Api-Key {key}"}, timeout=30)
        if r.status_code == 429 and attempt < 3:
            m = re.search(r"(\d+) seconds", r.text)
            time.sleep(min(int(m.group(1)) if m else 5, 30))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"429 on {path}")


def _items(body) -> list:
    if isinstance(body, dict):
        return body.get("results") or body.get("data") or []
    return body if isinstance(body, list) else []


def board_texts(record_uuid: str, owner_uuid: str) -> list:
    """Every post on both Message Boards. Raises if either cannot be read."""
    texts = [m.get("message") or "" for m in _items(_get(
        f"/api/internal/property/{record_uuid}/message/?limit=100"))]
    if owner_uuid:
        texts += [m.get("message") or "" for m in _items(_get(
            f"/api/internal/owner/{owner_uuid}/message/?limit=100"))]
    return texts


def run_texts(run: Optional[dict]) -> list:
    """The deep prospect's own written research for this record."""
    out = []
    for name in (run or {}).get("folders") or []:
        folder = Path(config.DP_RUNS_DIR) / name
        for f in list(folder.glob("*.md")) + [folder / "research_notes.txt",
                                              folder / "obituary_check.txt"]:
            try:
                out.append(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    out.append((run or {}).get("basis") or "")
    return out


def forbidden_names(record_uuid: str, rec: Optional[dict] = None) -> set:
    """Normalized first names that must never go in a text for this record.
    Contains ALL when the record could not be fully read."""
    hit = _cache.get(record_uuid)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    banned: set = set()
    try:
        rec = rec or crm.get_record(record_uuid)
        if not rec:
            raise RuntimeError("record unreadable")
        owner = rec.get("owner") if isinstance(rec.get("owner"), dict) else {}
        run = dp_numbers.run_for(record_uuid)
        people = [owner] + [o for o in rec.get("secondary_owners") or [] if isinstance(o, dict)]
        candidates = {_norm(p.get("first_name")) for p in people}
        subject = {_norm(owner.get("first_name"))}
        for p in (run or {}).get("people") or []:
            candidates.add(_norm(p.get("first")))
            if (p.get("relationship") or "").lower() == "subject":
                subject.add(_norm(p.get("first")))
        candidates.discard("")
        subject.discard("")

        texts = board_texts(record_uuid, owner.get("uuid") or "")
        notes = rec.get("notes")
        texts.append(notes if isinstance(notes, str) else str(notes or ""))
        events = crm.activity_log(record_uuid)
        if events is None:
            raise RuntimeError("activity log unreadable")
        for ev in events:
            pl = ev.get("payload") or {}
            for k in ("message", "note", "notes"):
                if isinstance(pl.get(k), str):
                    texts.append(pl[k])
        texts += run_texts(run)
        banned |= banned_from_texts(texts, candidates, subject)

        tags = {((t.get("title") or "") if isinstance(t, dict) else str(t)).strip().lower()
                for t in rec.get("tags") or []}
        if tags & DECEASED_TAGS:
            banned |= subject
        if run:
            banned |= dp_numbers.dead_first_names(run)
            if run.get("owner_dead"):
                banned |= subject
    except Exception as exc:  # noqa: BLE001 - unreadable means unverified
        log.warning("death check for %s could not read everything (%s): no names", record_uuid, exc)
        banned = {ALL}
    _cache[record_uuid] = (time.time(), banned)
    return banned


def name_allowed(record_uuid: str, first: str, rec: Optional[dict] = None,
                 heir_namesake: bool = False) -> bool:
    """heir_namesake: the name belongs to a living heir who shares it with the
    dead owner (dp_numbers.heir_namesake_ok), so a ban on that name is the
    owner's death, not theirs. An unreadable record still allows no name."""
    if not first:
        return True
    if not record_uuid:
        return False
    banned = forbidden_names(record_uuid, rec)
    if ALL in banned:
        return False
    return heir_namesake or _norm(first) not in banned
