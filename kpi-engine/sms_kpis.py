"""sms_kpis.py - text-message KPIs for the daily and Friday Slack posts (Jeff, 2026-10-16).

Runs on Jeff's PC, because two of its three sources only exist there:
  * smrtPhone's SMS log (same saved login as the call log): every text sent or
    received on the account, who sent it (source "api" = the SMS agent,
    "browser" = typed by hand in smrtPhone), and the carrier's delivery answer.
    The timestamp lives in the `created` column; `created_at` is always empty.
  * the SMS agent's database (output/sms_agent/sms_agent.db): which touch each
    agent text was, and how each reply was sorted (opt-out, wrong number, ...).
  * DataSift: ONLY the records whose owner texted back (a handful a day, never
    an account-wide read), to see whether the record became a lead or the
    number was marked Correct after the reply.

trigger_kpi_post.ps1 runs this before pressing "Run workflow" and passes the
result (base64 JSON) as the workflow's `texts` input. The posts in GitHub's
cloud only render it (render_lines); they cannot reach the PC's database.

  python sms_kpis.py --date 2026-10-15                 # that day
  python sms_kpis.py --date 2026-10-16 --week          # + Monday..that day
  python sms_kpis.py --date 2026-10-15 --out x.json --b64-out x.b64

Read-only everywhere. Standard library only.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import html
import json
import sqlite3
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
TZ = ZoneInfo("America/Chicago")
DB_DEFAULT = HERE.parent / "output" / "sms_agent" / "sms_agent.db"
LOG_COLUMNS = ["id", "created", "fromNum", "toNum", "content", "direction",
               "delivery_status", "delivery_code", "source"]
# a reply this long before a status change still counts as the reason for it
REPLY_LOOKBACK_DAYS = 14

CODE_LABEL = {
    "30003": "unreachable",
    "30004": "blocked us",
    "30005": "number doesn't exist",
    "30006": "landline",
    "30007": "spam block",
    "30008": "carrier error",
}
# reply sorting: one outcome per person, strongest first
OUTCOME_ORDER = [("OPT_OUT", "opt_out"), ("WRONG_NUMBER", "wrong_number"),
                 ("NOT_INTERESTED", "not_interested"), ("INTERESTED", "interested"),
                 ("POSITIVE", "interested")]


def _phone(v) -> str:
    d = "".join(ch for ch in str(v or "") if ch.isdigit())
    return d[-10:] if len(d) >= 10 else ""


def _local(iso: str) -> datetime.datetime | None:
    """Agent DB timestamps are ISO UTC ('2026-10-16T14:30:18+00:00')."""
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(TZ)


# ---------------------------------------------------------------- smrtPhone log
def fetch_log(since: str) -> list[dict]:
    """SMS log rows created on/after `since` (YYYY-MM-DD, Tulsa time), newest first."""
    import smrtphone_calls as sc

    cookie = sc._cookie_header()
    rows: list[dict] = []
    for page in range(30):          # 200 a page; the log only keeps ~30 days anyway
        form = {"draw": "1", "start": str(page * 200), "length": "200",
                "search[value]": "", "search[regex]": "false",
                "order[0][column]": "0", "order[0][dir]": "desc"}
        for i, col in enumerate(LOG_COLUMNS):
            form.update({f"columns[{i}][data]": col, f"columns[{i}][name]": col,
                         f"columns[{i}][searchable]": "true", f"columns[{i}][orderable]": "true",
                         f"columns[{i}][search][value]": "", f"columns[{i}][search][regex]": "false"})
        req = urllib.request.Request(
            sc.BASE + "/logs/sms/filtered", method="POST",
            data=urllib.parse.urlencode(form).encode(),
            headers={"cookie": cookie, "user-agent": sc.UA, "x-requested-with": "XMLHttpRequest",
                     "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                     "origin": sc.BASE, "referer": sc.BASE + "/logs/sms"})
        with urllib.request.urlopen(req, timeout=90) as r:
            body = r.read().decode("utf-8", "replace")
        if not body.lstrip().startswith("{"):
            raise sc.SessionExpired("smrtPhone login expired - run smrtphone_login.bat")
        page_rows = json.loads(body).get("data") or []
        oldest = None
        for r in page_rows:
            created = (r.get("created") or {}).get("date") or ""   # local Chicago time
            r["_day"], r["_time"] = created[:10], created[:19]
            if r["_day"] >= since:
                rows.append(r)
            oldest = r["_day"] or oldest
        if not page_rows or (oldest and oldest < since):
            break
    return rows


# ---------------------------------------------------------------- agent database
def load_agent(db: Path) -> dict | None:
    if not db.exists():
        return None
    c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        sends = []
        for phone, intent, sent_at, body in c.execute(
                "SELECT phone, intent, sent_at, body FROM outbox WHERE status='sent' AND sent_at IS NOT NULL"):
            touch = int(intent[-1]) if str(intent or "").startswith("seed_touch_") else 0
            dt = _local(sent_at)
            if dt:
                sends.append({"phone": _phone(phone), "touch": touch, "dt": dt, "body": body or ""})
        replies = []
        for phone, intent, created in c.execute(
                "SELECT phone, intent, created_at FROM messages WHERE direction='in'"):
            dt = _local(created)
            if dt:
                replies.append({"phone": _phone(phone), "intent": str(intent or "").upper(), "dt": dt})
        records = {_phone(p): r for p, r in c.execute("SELECT phone, record_uuid FROM phone_map") if r}
        wording = {int(i): w for i, w in c.execute("SELECT log_id, wording FROM delivery_failures") if i}
    finally:
        c.close()
    return {"sends": sends, "replies": replies, "records": records, "wording": wording}


# ---------------------------------------------------------------- DataSift
_LOG_CACHE: dict = {}


def crm_outcomes(reply_times: dict, records: dict, day_from: str, day_to: str) -> dict:
    """Leads and Correct numbers that followed a text reply.

    reply_times: phone -> sorted reply datetimes. Reads the activity log of only
    the records those phones belong to.
    """
    import pull_kpis as pk

    lead_set = {s.lower() for s in pk.load_benchmarks()["lead_statuses"]}
    by_rec = defaultdict(set)
    for ph in reply_times:
        if records.get(ph):
            by_rec[records[ph]].add(ph)
    if not by_rec:
        return {"leads": 0, "correct": 0, "records_checked": 0}
    token = pk.get_token()
    for uuid in by_rec:              # one read per record per run (the sheet backfill asks per day)
        if uuid not in _LOG_CACHE:
            _LOG_CACHE[uuid] = pk.get_logs(token, uuid)
    logs = {uuid: _LOG_CACHE[uuid] for uuid in by_rec}
    every = [e for evs in logs.values() for e in evs]
    p_idx = pk.detect_new_index([e for e in every if e.get("event_type") == "owner.phone.status.updated"], "phone")
    s_idx = pk.detect_new_index([e for e in every if e.get("event_type") == "property.status.updated"], "property")

    def replied_before(phones, dt) -> bool:
        lo = dt - datetime.timedelta(days=REPLY_LOOKBACK_DAYS)
        return any(lo <= t <= dt for ph in phones for t in reply_times.get(ph, []))

    leads, correct = 0, set()
    for uuid, evs in logs.items():
        phones = by_rec[uuid]
        became_lead = False
        for e in evs:
            dt = pk.local_dt(e.get("timestamp", ""), TZ)
            if not dt or not (day_from <= dt.date().isoformat() <= day_to):
                continue
            et = e.get("event_type")
            if et == "property.status.updated" and not became_lead:
                new = str(pk.new_status(e, "property", s_idx) or "").lower()
                old = str(pk.new_status(e, "property", 1 - s_idx) or "").lower()
                if new in lead_set and old not in lead_set and replied_before(phones, dt):
                    became_lead = True
            elif et == "owner.phone.status.updated":
                ph = _phone(((e.get("payload") or {}).get("owner") or {}).get("phone"))
                new = str(pk.new_status(e, "phone", p_idx) or "").upper()
                old = str(pk.new_status(e, "phone", 1 - p_idx) or "").upper()
                if (ph in phones and new in pk.CORRECT_STATES and old not in pk.CORRECT_STATES
                        and replied_before({ph}, dt)):
                    correct.add(ph)
        leads += became_lead
    # Counts only, no addresses: the result rides into a run of a PUBLIC repo.
    return {"leads": leads, "correct": len(correct), "records_checked": len(by_rec)}


# ---------------------------------------------------------------- the numbers
def compute(day_from: str, day_to: str, log_rows: list[dict], agent: dict | None) -> dict:
    s: dict = {"from": day_from, "to": day_to, "notes": []}
    rows = [r for r in log_rows if day_from <= r["_day"] <= day_to]
    out = [r for r in rows if (r.get("direction") or "").lower() == "outbound"]
    inb = [r for r in rows if (r.get("direction") or "").lower() == "inbound"]

    # 1, 8: sent, agent vs by hand. A text Diego types in DataSift is logged with
    # source "api" too (24 of them 10/12-10/14), so "agent" means a text that
    # matches one the agent's own outbox sent: same number, same wording.
    s["sent"] = len(out)
    s["people_texted"] = len({_phone(r.get("toNum")) for r in out} - {""})
    if agent is not None:
        mine = {(x["phone"], x["body"].strip()) for x in agent["sends"]}
        is_agent = lambda r: (_phone(r.get("toNum")), html.unescape(r.get("content") or "").strip()) in mine
    else:
        is_agent = lambda r: (r.get("source") or "").lower() == "api"
    s["agent"] = sum(1 for r in out if is_agent(r))
    s["by_hand"] = s["sent"] - s["agent"]

    # 9, 10, 12: delivery
    status = Counter((r.get("delivery_status") or "").lower() for r in out)
    s["delivered"] = status["delivered"]
    s["undelivered"] = status["undelivered"] + status["failed"]
    s["pending"] = s["sent"] - s["delivered"] - s["undelivered"]
    bad = [r for r in out if (r.get("delivery_status") or "").lower() in ("undelivered", "failed")]
    reasons = Counter(CODE_LABEL.get(str(r.get("delivery_code") or ""), "other") for r in bad)
    s["undelivered_reasons"] = dict(reasons.most_common())

    # 13: spam blocks by wording
    wording = (agent or {}).get("wording", {})
    spam = Counter()
    for r in bad:
        if str(r.get("delivery_code") or "") == "30007":
            w = wording.get(int(r.get("id") or 0))
            if not w:
                w = "agent text (wording not matched)" if is_agent(r) else "typed by hand"
            spam[w] += 1
    s["spam_by_wording"] = spam.most_common()

    # 16: replies
    s["replies"] = len(inb)
    repliers = {_phone(r.get("fromNum")) for r in inb} - {""}
    s["repliers"] = len(repliers)

    if agent is None:
        s["notes"].append("SMS agent database not found: touch numbers and reply sorting left out")
        return s

    end = datetime.datetime.combine(datetime.date.fromisoformat(day_to), datetime.time.max, TZ)
    sends = [x for x in agent["sends"] if day_from <= x["dt"].date().isoformat() <= day_to]
    # every reply time per phone: the agent's own record plus the smrtPhone log
    reply_times = defaultdict(list)
    for x in agent["replies"]:
        reply_times[x["phone"]].append(x["dt"])
    for r in log_rows:
        if (r.get("direction") or "").lower() == "inbound" and r.get("_time"):
            t = datetime.datetime.fromisoformat(r["_time"]).replace(tzinfo=TZ)
            reply_times[_phone(r.get("fromNum"))].append(t)
    for ph in reply_times:
        reply_times[ph].sort()

    # 6: new starts vs follow-ups (agent texts)
    s["new_starts"] = sum(1 for x in sends if x["touch"] == 1)
    s["follow_ups"] = sum(1 for x in sends if x["touch"] in (2, 3, 4))

    # 19: reply rate by touch = people sent touch N in the period who replied
    # after that text and before the period ended
    by_touch = {}
    for n in (1, 2, 3, 4):
        sent_to = {}
        for x in sends:
            if x["touch"] == n:
                sent_to[x["phone"]] = min(sent_to.get(x["phone"], x["dt"]), x["dt"])
        replied = sum(1 for ph, t in sent_to.items()
                      if any(t < rt <= end for rt in reply_times.get(ph, [])))
        by_touch[str(n)] = [replied, len(sent_to)]
    s["reply_rate_by_touch"] = by_touch

    # 24-28: what people said (one outcome per person who replied in the period)
    said = defaultdict(set)
    for x in agent["replies"]:
        if day_from <= x["dt"].date().isoformat() <= day_to:
            said[x["phone"]].add(x["intent"])
            repliers.add(x["phone"])
    outcomes = Counter()
    for ph in repliers:
        label = next((lab for intent, lab in OUTCOME_ORDER if intent in said.get(ph, set())), "unclear")
        outcomes[label] += 1
    s["repliers"] = len(repliers)
    s["outcomes"] = {k: outcomes[k] for k in ("interested", "not_interested", "wrong_number", "opt_out", "unclear")}

    # 29, 31: what happened in DataSift after a reply
    lo = (datetime.date.fromisoformat(day_from) - datetime.timedelta(days=REPLY_LOOKBACK_DAYS))
    recent = {ph: ts for ph, ts in reply_times.items()
              if ph and any(lo <= t.date() and t <= end for t in ts)}
    try:
        crm = crm_outcomes(recent, agent["records"], day_from, day_to)
        s["leads_from_texts"] = crm["leads"]
        s["correct_from_texts"] = crm["correct"]
    except SystemExit as e:          # pull_kpis exits on a missing / rejected key
        s["notes"].append(f"DataSift not read ({e}): leads and Correct numbers from texts left out")
    except Exception as e:
        s["notes"].append(f"DataSift not read ({type(e).__name__}): leads and Correct numbers from texts left out")
    return s


def build(day: str, week: bool, db: Path) -> dict:
    d = datetime.date.fromisoformat(day)
    monday = (d - datetime.timedelta(days=d.weekday())).isoformat()
    result: dict = {"day": None, "week": None}
    try:
        log_rows = fetch_log(monday if week else day)
    except Exception as e:
        result["error"] = f"smrtPhone text log not read: {e}"
        return result
    agent = load_agent(db)
    result["day"] = compute(day, day, log_rows, agent)
    if week:
        result["week"] = compute(monday, day, log_rows, agent)
    return result


def build_days(days: list[str], db: Path = DB_DEFAULT) -> dict:
    """{day: stats} for several days off one read of the log (the sheet's Texts tab)."""
    log_rows = fetch_log(min(days))
    agent = load_agent(db)
    return {d: compute(d, d, log_rows, agent) for d in days}


SHEET_COLUMNS = [
    "Date", "Sent", "People Texted", "Agent", "By Hand", "New Starts", "Follow-ups",
    "Delivered", "Delivered %", "Undelivered", "Undelivered %", "Still Pending",
    "Undelivered Reasons", "Spam Blocks", "Replies", "People Replied",
    "T1 Sent", "T1 Replied", "T1 Reply %", "T2 Sent", "T2 Replied", "T2 Reply %",
    "T3 Sent", "T3 Replied", "T3 Reply %", "T4 Sent", "T4 Replied", "T4 Reply %",
    "Interested", "Not Interested", "Wrong #", "Unclear", "Opt-outs", "Opt-out %",
    "Leads from Text Reply", "Correct after Text Reply",
]


def sheet_row(s: dict) -> dict:
    """One day's stats as the Texts tab's columns (Date is the key, not included)."""
    o = s.get("outcomes") or {}
    row = {
        "Sent": s["sent"], "People Texted": s["people_texted"], "Agent": s["agent"],
        "By Hand": s["by_hand"], "New Starts": s.get("new_starts", ""), "Follow-ups": s.get("follow_ups", ""),
        "Delivered": s["delivered"], "Delivered %": _pct(s["delivered"], s["sent"]) if s["sent"] else "",
        "Undelivered": s["undelivered"], "Undelivered %": _pct(s["undelivered"], s["sent"]) if s["sent"] else "",
        "Still Pending": s["pending"],
        "Undelivered Reasons": ", ".join(f"{n} {why}" for why, n in s["undelivered_reasons"].items()),
        "Spam Blocks": "; ".join(f"{n}x {w}" for w, n in s["spam_by_wording"]),
        "Replies": s["replies"], "People Replied": s["repliers"],
        "Interested": o.get("interested", ""), "Not Interested": o.get("not_interested", ""),
        "Wrong #": o.get("wrong_number", ""), "Unclear": o.get("unclear", ""), "Opt-outs": o.get("opt_out", ""),
        "Opt-out %": _pct(o["opt_out"], s["people_texted"]) if o and s["people_texted"] else "",
        "Leads from Text Reply": s.get("leads_from_texts", ""),
        "Correct after Text Reply": s.get("correct_from_texts", ""),
    }
    for t, (replied, sent) in (s.get("reply_rate_by_touch") or {}).items():
        row[f"T{t} Sent"], row[f"T{t} Replied"] = sent, replied
        row[f"T{t} Reply %"] = _pct(replied, sent) if sent else ""
    return row


# ---------------------------------------------------------------- Slack lines
def _pct(n, d) -> str:
    return f"{100 * n / d:.0f}%" if d else "n/a"


def render_lines(s: dict | None, error: str = "") -> list[str]:
    """Slack lines for one period. Shared by the daily and the Friday post."""
    if not s:
        return [f"• _Text stats not available: {error or 'this run was not started from the PC'}_"]
    sent = s["sent"]
    if not sent and not s["replies"]:
        return ["• No texts sent or received"] + [f"• _{n}_" for n in s.get("notes", [])]
    lines = [
        f"• Sent: *{sent}* to {s['people_texted']} people"
        + (f"  |  new starts {s['new_starts']} / follow-ups {s['follow_ups']}" if "new_starts" in s else "")
        + f"  |  agent {s['agent']} / by hand {s['by_hand']}",
        f"• Delivered: *{s['delivered']}* ({_pct(s['delivered'], sent)})  |  Undelivered: *{s['undelivered']}*"
        f" ({_pct(s['undelivered'], sent)})" + (f"  |  Still pending: {s['pending']}" if s["pending"] else ""),
    ]
    if s["undelivered_reasons"]:
        lines.append("    Undelivered: " + ", ".join(f"{n} {why}" for why, n in s["undelivered_reasons"].items()))
    for w, n in s["spam_by_wording"]:
        lines.append(f"    ⚠️ Spam blocked {n}x: {w}")
    rate = ""
    if any(n for _, n in (s.get("reply_rate_by_touch") or {}).values()):
        rate = "  |  Reply rate by touch: " + " / ".join(
            f"T{t} {_pct(r, n)} ({r}/{n})" for t, (r, n) in s["reply_rate_by_touch"].items() if n)
    who = "1 person" if s["repliers"] == 1 else f"{s['repliers']} people"
    lines.append(f"• Replies: *{s['replies']}* from {who}{rate}")
    if "outcomes" in s:
        o = s["outcomes"]
        lines.append(f"• Interested {o['interested']}  |  Not interested {o['not_interested']}  |  "
                     f"Wrong # {o['wrong_number']}  |  Unclear {o['unclear']}  |  "
                     f"Opt-outs {o['opt_out']} ({_pct(o['opt_out'], s['people_texted'])} of people texted)")
    if "leads_from_texts" in s:
        lines.append(f"• Leads from a text reply: *{s['leads_from_texts']}*  |  "
                     f"Numbers marked Correct after a text reply: *{s['correct_from_texts']}*")
    for n in s.get("notes", []):
        lines.append(f"• _{n}_")
    return lines


def from_env_b64(raw: str) -> dict:
    """Decode the workflow's `texts` input. {} when blank or unreadable."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception:
        return {"error": "the text stats sent by the PC could not be read"}


def main() -> int:
    ap = argparse.ArgumentParser(description="Text KPIs for the Slack posts")
    ap.add_argument("--date", default=datetime.datetime.now(TZ).date().isoformat())
    ap.add_argument("--week", action="store_true", help="also Monday..date (the Friday post)")
    ap.add_argument("--db", type=Path, default=DB_DEFAULT)
    ap.add_argument("--out", type=Path, help="write the JSON here")
    ap.add_argument("--b64-out", type=Path, help="write base64 JSON here (for the workflow input)")
    args = ap.parse_args()
    result = build(args.date, args.week, args.db)
    text = json.dumps(result, separators=(",", ":"))
    if args.out:
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if args.b64_out:
        args.b64_out.write_text(base64.b64encode(text.encode()).decode(), encoding="ascii")
    for period in ("day", "week"):
        if result.get(period) or (period == "day"):
            print(f"--- {period} ---")
            print("\n".join(render_lines(result.get(period), result.get("error", ""))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
