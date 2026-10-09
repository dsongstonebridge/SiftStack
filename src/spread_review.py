"""Foreclosure value-spread review: stop and ask before tracing thin deals.

User's rule (2026-10-09), every foreclosure batch, after the post-enrichment
gate and before any skip trace:

    spread = SiftMap estimated value (DataSift `estimate_value`)
             - Unpaid Principal Balance (from the OSCN petition)

  - spread > $100,000            -> goes on to skip trace, scoring and tagging
  - spread <= $100,000, or either
    number is missing            -> HELD. The user is shown a NUMBERED list
                                    and answers "keep 1,3,4 drop 2,5".

First mortgage only: junior liens and second mortgages are NOT subtracted (the
user wants them on the Message Board, not in this math). MLS-listed records are
never asked about (they are never traced anyway), nor are Owner Alive = No.
Probate does not run this. The equity-% and sold-within-3-years rules still
run first; only records that pass them can land here.

NOTHING IS TRACED WHILE AN ANSWER IS OUTSTANDING. The user wants the whole
batch (passing records plus his keeps) traced, scored and tagged as one unit,
not in two chunks. So `--commit` refuses every billed step until each held
record in the batch is answered, via `--spread-answers "keep 1,3 drop 2"`.

  keep -> traced with the rest of the batch.
  drop -> that one record is deleted from the CRM, and only when it is
          provably the record this batch created: the uuid captured for that
          row right after creation (the row had already passed the CRM
          duplicate check, so nothing was there before), the record still sits
          at that address, and its owner carries no phone numbers. Anything
          else is left untouched and reported.

State lives in `output/.spread_review.json`, keyed by the created-rows ledger
key, so numbers stay stable between the dry run and the --commit run.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SPREAD_MIN = 100_000
STORE_PATH = Path(__file__).resolve().parent.parent / "output" / ".spread_review.json"


def _money(v) -> Optional[float]:
    if v is None:
        return None
    s = str(v).replace("$", "").replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def value_spread(prop: dict, row: dict) -> dict:
    """{"value", "balance", "spread", "ok", "note"} for one record.
    `ok` is True only when both numbers exist and the spread is over $100k."""
    value = _money(prop.get("estimate_value"))
    balance = _money(row.get("Unpaid Principal Balance"))
    missing = []
    if not value:
        missing.append("no SiftMap estimated value")
    if balance is None:
        missing.append("no unpaid principal balance on the petition")
    if missing:
        return {"value": value, "balance": balance, "spread": None, "ok": False,
                "note": "CAN'T CALCULATE: " + " and ".join(missing)}
    spread = value - balance
    return {"value": value, "balance": balance, "spread": spread,
            "ok": spread > SPREAD_MIN,
            "note": "" if spread > SPREAD_MIN else f"spread is ${SPREAD_MIN:,} or less"}


# ── store ────────────────────────────────────────────────────────────────

def load(path: Optional[Path] = None) -> dict:
    path = Path(path or STORE_PATH)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as e:
        raise RuntimeError(f"spread-review store unreadable at {path}: {e}") from e


def _save(store: dict, path: Optional[Path] = None) -> None:
    path = Path(path or STORE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(path)


def add(items: list[tuple[str, dict]], *, path: Optional[Path] = None) -> None:
    """Register held records as (ledger_key, info). Each gets the next number
    after the highest still-unanswered one, so an open list keeps its numbers."""
    store = load(path)
    nxt = max([e["num"] for e in store.values() if not e.get("answer")] or [0]) + 1
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    for key, info in items:
        if key in store and not store[key].get("answer"):
            continue
        store[key] = {**info, "num": nxt, "answer": None, "added": now}
        nxt += 1
    _save(store, path)


def pending(keys, *, path: Optional[Path] = None) -> list[tuple[str, dict]]:
    """Unanswered entries among `keys`, by number."""
    store = load(path)
    out = [(k, store[k]) for k in keys if k in store and not store[k].get("answer")]
    return sorted(out, key=lambda kv: kv[1]["num"])


def parse_answers(text: str, numbers: set[int]) -> dict[int, str]:
    """"keep 1,3,4 drop 2,5" -> {1:"keep",3:"keep",4:"keep",2:"drop",5:"drop"}.
    Also "keep all" / "drop all", ranges like "1-4", "toss" for drop.
    Raises ValueError on an unknown number or one given both ways."""
    out: dict[int, str] = {}
    words = re.findall(r"[a-z]+|\d+\s*-\s*\d+|\d+", str(text or "").lower())
    verb = None
    for w in words:
        if w in ("keep", "drop", "toss", "remove"):
            verb = "keep" if w == "keep" else "drop"
            continue
        if w == "all" and verb:
            nums = sorted(numbers)
        elif re.fullmatch(r"\d+\s*-\s*\d+", w):
            a, b = (int(x) for x in re.split(r"\s*-\s*", w))
            nums = list(range(min(a, b), max(a, b) + 1))
        elif w.isdigit():
            nums = [int(w)]
        else:
            continue
        if not verb:
            raise ValueError(f"number {w} given before 'keep' or 'drop'")
        for n in nums:
            if n not in numbers:
                raise ValueError(f"#{n} is not on the open list ({sorted(numbers)})")
            if out.get(n, verb) != verb:
                raise ValueError(f"#{n} is marked both keep and drop")
            out[n] = verb
    return out


def record_answer(key: str, answer: str, outcome: str = "", *,
                  path: Optional[Path] = None) -> None:
    store = load(path)
    if key in store:
        store[key].update(answer=answer, outcome=outcome,
                          answered=datetime.now().strftime("%Y-%m-%d %H:%M"))
        _save(store, path)


def describe(info: dict) -> str:
    def m(v):
        return f"${v:,.0f}" if isinstance(v, (int, float)) else "?"
    spread = info.get("spread")
    head = (f"value {m(info.get('value'))} - balance {m(info.get('balance'))} = "
            f"{m(spread) if spread is not None else '?'}")
    return f"{head}  [{info['note']}]" if info.get("note") else head


def safe_delete(info: dict, *, get_property, delete_property, find_property) -> str:
    """Delete the one record this batch created for a dropped row, or say why
    not. Never raises."""
    uuid = info.get("uuid")
    if not uuid:
        return "NOT deleted: no record id was captured for it"
    try:
        prop = get_property(uuid)
    except Exception as e:                           # noqa: BLE001
        return f"NOT deleted: could not read the record ({e})"
    if not prop:
        return "already gone from the CRM"
    # The address must still resolve to exactly this record. Comparing strings
    # would trip over the server's own rewrites (it drops "S", expands "E").
    try:
        hit = find_property(info.get("street") or "")
    except Exception as e:                           # noqa: BLE001
        return f"NOT deleted: address lookup failed ({e}) - left untouched"
    if not hit or hit.get("uuid") != uuid:
        return (f"NOT deleted: {info.get('street')!r} no longer resolves to record "
                f"{uuid} - left untouched")
    if (prop.get("owner") or {}).get("phones"):
        return "NOT deleted: the owner already has phone numbers - left untouched"
    try:
        delete_property(uuid)
    except Exception as e:                           # noqa: BLE001
        return f"delete FAILED: {e}"
    return "deleted"
