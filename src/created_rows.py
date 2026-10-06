"""Which input rows has `skip-trace --create` already turned into CRM records?

The `--create` dry run creates and enriches every record and posts its Notes and
Message Board (none of that is billed). Re-running the SAME command with
`--commit` used to do all of it again: notes and boards posted a second time on
every record. So the billed half had to be run as a separate trace-only step
from `datasift_ready_*.csv`, and that path applies none of the gates (buy box,
Owner Alive, already-processed check) and loses the co-borrower columns.

This ledger fixes that. After a successful create it records, per input row,
what happened and the exact row to hand the tracer. A later `--create` over the
same rows skips creation for them and goes straight to the trace, so the
pipeline works as "same command, add --commit" for foreclosure and probate.

Rows are keyed on the INPUT row, read before any lookup rewrites it:
  - the case number(s) when the row has one ("PB-2026-0761" == "PB-2026-761",
    a merged row's cases are all part of its key)
  - otherwise the street + owner last name.

Statuses:
  trace     created and passed every gate -> traced on a re-run
  no_trace  created but Owner Alive = No   -> never traced
  excluded  created, then removed by the post-enrichment gate -> never traced

Rows held for review or rejected by the buy box are NOT recorded: they were
never created, so a re-run sends them through the full chain again (which is
how a held row gets created once you mark it Property Confirmed = Yes).

`--recreate` ignores this ledger for one run. Nothing here touches the network.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

LEDGER_PATH = Path(__file__).resolve().parent.parent / "output" / ".created_rows.json"

_CASE_RE = re.compile(r"\b([A-Z]{2})\s*-?\s*(\d{4})\s*-?\s*0*(\d+)\b", re.IGNORECASE)


def _street_norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split())


def row_key(row: dict, notice_type: str) -> Optional[str]:
    """Stable identity of an input row, or None when there is nothing to key on."""
    cases = []
    for m in _CASE_RE.finditer(str(row.get("Case Number") or "")):
        c = f"{m.group(1).upper()}-{m.group(2)}-{int(m.group(3))}"
        if c not in cases:
            cases.append(c)
    if cases:
        return f"{notice_type}|case:{'+'.join(sorted(cases))}"
    street = _street_norm(str(row.get("Property Street") or row.get("Property Street Address") or ""))
    last = str(row.get("Last Name") or row.get("Owner Last Name") or "").strip().lower()
    if not street:
        return None
    return f"{notice_type}|addr:{street}|{last}"


def load(path: Optional[Path] = None) -> dict:
    path = Path(path or LEDGER_PATH)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as e:
        # Loud on purpose: treating a corrupt ledger as empty would re-create
        # every row and re-post every note and board.
        raise RuntimeError(f"created-rows ledger unreadable at {path}: {e}") from e


def split(rows: list[dict], notice_type: str, *,
          path: Optional[Path] = None) -> tuple[list[dict], list[tuple[dict, dict]]]:
    """Split input rows into (fresh, done). `done` pairs each row with its
    ledger entry. A row with no usable key is always fresh."""
    ledger = load(path)
    fresh, done = [], []
    for r in rows:
        k = row_key(r, notice_type)
        if k and k in ledger:
            done.append((r, ledger[k]))
        else:
            fresh.append(r)
    return fresh, done


def record(entries: list[tuple[dict, str, Optional[dict]]], notice_type: str, *,
           path: Optional[Path] = None) -> int:
    """Record (input_row, status, trace_row) triples. Returns how many were written.
    `input_row` must be the row as READ, keyed before any rewrite; callers pass
    the key-bearing original."""
    path = Path(path or LEDGER_PATH)
    ledger = load(path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    n = 0
    for row, status, trace_row in entries:
        k = row_key(row, notice_type)
        if not k:
            logger.warning("created-rows: no key for %r - a re-run would create it again",
                           row.get("Property Street") or row.get("Last Name") or "?")
            continue
        ledger[k] = {
            "status": status,
            "trace_row": trace_row,
            "label": f"{row.get('First Name', '')} {row.get('Last Name', '')}".strip()
                     + (f" | {row.get('Case Number')}" if row.get("Case Number") else ""),
            "recorded": now,
        }
        n += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(path)
    return n
