"""Did our texts actually arrive? (Jeff, 2026-10-09)

"Sent" in the outbox only means smrtPhone accepted the message. Whether the
carrier delivered it is a separate answer that arrives later, and nothing here
read it, so a wording carriers blocked as spam went out 19 times over three
days ("I hope I'm not being a bother ... Would a short call work?", 16 blocked
with 30007) and every report said "sent".

smrtPhone's SMS log carries the answer per message in `delivery_status`
(delivered / undelivered / sent) and `delivery_code` (the carrier error, Twilio
numbering). The log only returns columns that are asked for, which is why the
reply poll never saw them.

Each pass:
  * records every newly undelivered outbound text (once, by log id);
  * posts one #SMS message listing them, with what each code means;
  * names any wording carriers have blocked as spam (30007) 3+ times in 7
    days, so it can be retired before it burns more sends;
  * adds numbers the carrier says can never take a text (30005 unknown number,
    30006 landline) to the do-not-text list, like a smrtPhone landline refusal.
Read-only toward smrtPhone; the only writes are local (table + suppression)
and the Slack post.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone

from . import config, store
from .knowledge import touches

log = logging.getLogger(__name__)

EXTRA_COLUMNS = ["delivery_status", "delivery_code"]

CODE_MEANING = {
    "30003": "phone unreachable (off, out of service area)",
    "30004": "recipient blocked us",
    "30005": "number does not exist",
    "30006": "landline or carrier cannot take texts",
    "30007": "BLOCKED BY CARRIER AS SPAM (message content)",
    "30008": "unknown carrier error",
}
NEVER_TEXTABLE = {"30005", "30006"}
SPAM_CODE = "30007"
WORDING_ALARM = 3       # spam blocks of one wording within WORDING_DAYS
WORDING_DAYS = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_failures (
    log_id      INTEGER PRIMARY KEY,
    phone       TEXT,
    from_number TEXT,
    status      TEXT,
    code        TEXT,
    body        TEXT,
    wording     TEXT,
    seen_at     TEXT NOT NULL,
    posted      INTEGER NOT NULL DEFAULT 0
)
"""


def _templates() -> list[tuple[str, str]]:
    """(label, literal mark) for every owner/heir touch wording."""
    out = []
    pools = list(touches.POOLS) + list(getattr(touches, "HEIR_POOLS", []))
    for n, (named, noname) in enumerate(pools, start=1):
        touch_no = (n - 1) % 4 + 1
        for template in list(named) + list(noname):
            literals = [p.strip() for p in re.split(r"\{[a-z]+\}", template)]
            mark = max(literals, key=len).lower()
            if len(mark) >= 18:
                out.append((f"touch {touch_no}: \"{template[:70]}...\"", mark[:60]))
    return out


_TEMPLATES = _templates()


def wording_of(body: str) -> str:
    text = re.sub(r"\s+", " ", (body or "").lower())
    for label, mark in _TEMPLATES:
        if mark in text:
            return label
    # Not a current pool wording (a retired one, or a hand-typed text). Key it
    # on the words after the greeting, so "Hey Billy! I hope..." and "Hey
    # Deborah! I hope..." count as the same wording.
    words = text.split()[2:]
    key = " ".join(w for w in words if not any(ch.isdigit() for ch in w))[:45]
    return f"other: \"...{key}...\""


def _fetch(pages: int) -> list[dict]:
    from . import reconcile

    session = reconcile._session()
    cols = list(reconcile.COLUMNS) + EXTRA_COLUMNS
    rows: list[dict] = []
    for page in range(pages):
        form = {"draw": "1", "start": str(page * 200), "length": "200",
                "search[value]": "", "search[regex]": "false",
                "order[0][column]": "0", "order[0][dir]": "desc"}
        for i, col in enumerate(cols):
            form[f"columns[{i}][data]"] = col
            form[f"columns[{i}][name]"] = col
            form[f"columns[{i}][searchable]"] = "true"
            form[f"columns[{i}][orderable]"] = "true"
            form[f"columns[{i}][search][value]"] = ""
            form[f"columns[{i}][search][regex]"] = "false"
        resp = session.post(reconcile.BASE + reconcile.LOG_PATH, data=form, timeout=90)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} from the SMS log")
        try:
            page_rows = resp.json().get("data") or []
        except ValueError:
            raise RuntimeError("smrtPhone session expired") from None
        if not page_rows:
            break
        rows.extend(page_rows)
    return rows


def record(rows: list[dict]) -> list[dict]:
    """Store newly undelivered outbound texts. Returns the new ones."""
    new = []
    with store.tx() as c:
        c.execute(SCHEMA)
        for r in rows:
            if (r.get("direction") or "").lower() != "outbound":
                continue
            status = (r.get("delivery_status") or "").lower()
            if status not in ("undelivered", "failed"):
                continue
            body = html.unescape(r.get("content") or "")
            item = {
                "log_id": int(r.get("id") or 0),
                "phone": store.clean_phone(r.get("toNum")),
                "from_number": r.get("fromNum") or "",
                "status": status,
                "code": str(r.get("delivery_code") or ""),
                "body": body,
                "wording": wording_of(body),
            }
            if not item["log_id"]:
                continue
            cur = c.execute(
                "INSERT OR IGNORE INTO delivery_failures"
                " (log_id, phone, from_number, status, code, body, wording, seen_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (item["log_id"], item["phone"], item["from_number"], item["status"],
                 item["code"], item["body"], item["wording"], store.now()))
            if cur.rowcount == 1:
                new.append(item)
    return new


def blocked_wordings() -> dict[str, int]:
    """Spam-blocked (30007) count per wording over the last WORDING_DAYS."""
    since = (datetime.now(timezone.utc) - timedelta(days=WORDING_DAYS)).isoformat(timespec="seconds")
    with store.tx() as c:
        c.execute(SCHEMA)
        rows = c.execute(
            "SELECT wording, count(*) FROM delivery_failures WHERE code=? AND seen_at>=?"
            " GROUP BY wording", (SPAM_CODE, since)).fetchall()
    return {w: n for w, n in rows}


def _message(new: list[dict]) -> str:
    lines = [f"{len(new)} text(s) NOT delivered:"]
    for it in new:
        mapped = store.lookup_phone(it["phone"]) or {}
        who = mapped.get("address") or it["phone"]
        meaning = CODE_MEANING.get(it["code"], f"code {it['code'] or 'none'}")
        lines.append(f"- {who} ({it['phone'][-4:]}) from ...{it['from_number'][-4:]}: {meaning}"
                     f" | {it['wording']}")
    hot = {w: n for w, n in blocked_wordings().items() if n >= WORDING_ALARM}
    for w, n in sorted(hot.items(), key=lambda x: -x[1]):
        lines.append(f"WORDING BLOCKED AS SPAM {n}x in {WORDING_DAYS} days, retire it: {w}")
    suppressed = [it for it in new if it["code"] in NEVER_TEXTABLE]
    if suppressed:
        lines.append(f"{len(suppressed)} number(s) can never take a text and were added to the"
                     " do-not-text list.")
    return "\n".join(lines)


def run(pages: int = 2) -> dict:
    """One delivery pass. Never raises into the worker."""
    try:
        rows = _fetch(pages)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    new = record(rows)
    for it in new:
        if it["code"] in NEVER_TEXTABLE and it["phone"]:
            store.suppress(it["phone"], f"carrier {it['code']}: {CODE_MEANING[it['code']]}")
            store.cancel_queued(it["phone"], f"carrier {it['code']}, cannot take sms")
    if new:
        from . import escalate

        ok = escalate.alert("SMS delivery problem", _message(new), kind="delivery")
        if ok:
            with store.tx() as c:
                c.executemany("UPDATE delivery_failures SET posted=1 WHERE log_id=?",
                              [(it["log_id"],) for it in new])
        log.warning("delivery: %s new undelivered text(s)", len(new))
    return {"new_failures": len(new), "checked": len(rows)}


def summary_today(since_utc: str) -> dict:
    """For check-ins: undelivered since a UTC timestamp, by code and wording."""
    with store.tx() as c:
        c.execute(SCHEMA)
        rows = c.execute("SELECT code, wording FROM delivery_failures WHERE seen_at>=?",
                         (since_utc,)).fetchall()
    out: dict = {}
    for code, wording in rows:
        out.setdefault(code, {}).setdefault(wording, 0)
        out[code][wording] += 1
    return out
