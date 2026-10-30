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
DB_DEFAULT = Path(__import__("os").environ.get("KPI_SMS_DB") or
                  HERE.parent / "output" / "sms_agent" / "sms_agent.db")
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


def rec_hash(uuid: str) -> str:
    """A record id as it rides to GitHub: the call side (pull_kpis) hashes its own
    records the same way, so 'reached by call AND by text' can be matched in a
    public repo's run without sending record ids."""
    import hashlib
    return hashlib.sha256(("kpi:" + str(uuid)).encode()).hexdigest()[:16]


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
        # Carrier flags per sending number, as the agent last cached them (it refreshes
        # hourly while running). Read here, never fetched: a KPI run must not log in to
        # NumberVerifier or set off the agent's own #SMS alerts.
        health = None
        try:
            row = c.execute("SELECT v FROM meta WHERE k='number_health'").fetchone()
            health = json.loads(row[0]) if row and row[0] else None
        except sqlite3.Error:
            pass
    finally:
        c.close()
    return {"sends": sends, "replies": replies, "records": records, "wording": wording,
            "health": health}


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
        return {"leads": 0, "correct": 0, "records_checked": 0, "confirmed": []}
    token = pk.get_token()
    for uuid in by_rec:              # one read per record per run (the sheet backfill asks per day)
        if uuid not in _LOG_CACHE:
            try:
                _LOG_CACHE[uuid] = pk.get_logs(token, uuid)
            except urllib.error.HTTPError as e:
                if e.code != 404:          # a deleted record has no log; anything else is real
                    raise
                _LOG_CACHE[uuid] = []
    logs = {uuid: _LOG_CACHE[uuid] for uuid in by_rec}
    every = [e for evs in logs.values() for e in evs]
    p_idx = pk.detect_new_index([e for e in every if e.get("event_type") == "owner.phone.status.updated"], "phone")
    s_idx = pk.detect_new_index([e for e in every if e.get("event_type") == "property.status.updated"], "property")

    def replied_before(phones, dt) -> bool:
        lo = dt - datetime.timedelta(days=REPLY_LOOKBACK_DAYS)
        return any(lo <= t <= dt for ph in phones for t in reply_times.get(ph, []))

    leads, correct, confirmed = 0, set(), set()
    for uuid, evs in logs.items():
        phones = by_rec[uuid]
        became_lead = False
        for e in evs:
            dt = pk.local_dt(e.get("timestamp", ""), TZ)
            if not dt or not (day_from <= dt.date().isoformat() <= day_to):
                continue
            et = e.get("event_type")
            if et == "property.status.updated":
                new = str(pk.new_status(e, "property", s_idx) or "").lower()
                old = str(pk.new_status(e, "property", 1 - s_idx) or "").lower()
                if not replied_before(phones, dt):
                    continue
                if new in lead_set and old not in lead_set and not became_lead:
                    became_lead = True
                # Text confirmed (Jeff, 2026-10-30): after a text reply the record became a
                # lead or Not Interested - only the owner gives either answer.
                if new in lead_set or new in ("not_interested", "not interested"):
                    confirmed.add(uuid)
            elif et == "owner.phone.status.updated":
                ph = _phone(((e.get("payload") or {}).get("owner") or {}).get("phone"))
                new = str(pk.new_status(e, "phone", p_idx) or "").upper()
                old = str(pk.new_status(e, "phone", 1 - p_idx) or "").upper()
                if (ph in phones and new in pk.CORRECT_STATES and old not in pk.CORRECT_STATES
                        and replied_before({ph}, dt)):
                    # ...or the number they replied from was marked Correct after the
                    # reply, by hand (an unclear reply Jeff checked) or by the agent (a yes).
                    correct.add(ph)
                    confirmed.add(uuid)
        leads += became_lead
    # Counts and hashed record ids only, no addresses: the result rides into a run of a
    # PUBLIC repo.
    return {"leads": leads, "correct": len(correct), "records_checked": len(by_rec),
            "confirmed": sorted(rec_hash(u) for u in confirmed)}


# ---------------------------------------------------------------- the numbers
def compute(day_from: str, day_to: str, log_rows: list[dict], agent: dict | None,
            calls: list[dict] | None = None) -> dict:
    """calls: smrtPhone call log from day_from to NOW (smrtphone_calls.calls_since), for
    reply response time and replies that turned into a call. None = left out."""
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
    # Reply rate counts people whose text was DELIVERED (Jeff, 2026-10-30): a blocked
    # text can't be answered, so dividing by sent reads low on a bad carrier day.
    delivered_to = {_phone(r.get("toNum")) for r in out
                    if (r.get("delivery_status") or "").lower() == "delivered"} - {""}
    s["people_delivered"] = len(delivered_to)

    # Sending numbers: what each one sent in the period, and its carrier flags
    by_from = defaultdict(lambda: [0, 0])
    for r in out:
        f = _phone(r.get("fromNum"))
        if f:
            by_from[f][0] += 1
            by_from[f][1] += (r.get("delivery_status") or "").lower() == "delivered"
    health = (agent or {}).get("health") or {}
    flags = health.get("data") or {}
    carriers = ("AT&T", "T-Mobile", "Verizon")
    s["senders"] = []
    for num, (n_sent, n_deliv) in sorted(by_from.items(), key=lambda kv: -kv[1][0]):
        row = flags.get(num)
        s["senders"].append({
            "last4": num[-4:], "sent": n_sent, "delivered": n_deliv,
            "flags": ([c for c, n in zip(carriers, row["flags"]) if n > 0] if row else None),
            "flag_day": row["day"] if row else ""})

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

    # 19: reply rate by touch = people whose touch N was DELIVERED in the period and who
    # replied after that text and before the period ended
    status_of = {}
    for r in log_rows:
        if (r.get("direction") or "").lower() == "outbound":
            key = (_phone(r.get("toNum")), html.unescape(r.get("content") or "").strip())
            status_of[key] = (r.get("delivery_status") or "").lower()
    by_touch = {}
    for n in (1, 2, 3, 4):
        sent_to = {}
        for x in sends:
            if x["touch"] == n and status_of.get((x["phone"], x["body"].strip())) == "delivered":
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

    # Records texted in the period (hashed): half of "records worked" for the contact rate
    records = agent["records"]
    texted = {records[_phone(r.get("toNum"))] for r in out if records.get(_phone(r.get("toNum")))}
    s["texted_records"] = sorted(rec_hash(u) for u in texted)

    # Finished all 4 touches: records that got touch 4 in the period
    fin = {records.get(x["phone"]) or ("ph:" + x["phone"]) for x in sends if x["touch"] == 4}
    s["finished_4"] = len(fin)

    # Reply response time + replies that turned into a call (Jeff, 2026-10-30).
    # One clock per person: their first reply in the period. Opt-outs and wrong numbers
    # are left out (nobody should answer those). A response is our first call to them or
    # a text typed by hand after the reply; the agent's own texts don't count.
    first_reply = {}
    for r in inb:
        ph = _phone(r.get("fromNum"))
        if ph and r.get("_time"):
            t = datetime.datetime.fromisoformat(r["_time"]).replace(tzinfo=TZ)
            first_reply[ph] = min(first_reply.get(ph, t), t)
    skip = {ph for ph, said_ in said.items() if said_ & {"OPT_OUT", "WRONG_NUMBER"}}
    first_reply = {ph: t for ph, t in first_reply.items() if ph not in skip}
    if calls is None:
        s["notes"].append("smrtPhone call log not read: response time and replies that turned into a call left out")
    else:
        hand_texts = defaultdict(list)
        for r in log_rows:
            if (r.get("direction") or "").lower() == "outbound" and r.get("_time") and not is_agent(r):
                hand_texts[_phone(r.get("toNum"))].append(
                    datetime.datetime.fromisoformat(r["_time"]).replace(tzinfo=TZ))
        out_calls, live_calls = defaultdict(list), defaultdict(list)
        for c in calls:
            ph = _phone(c.get("number"))
            if c.get("direction") == "outbound":
                out_calls[ph].append(c["when"])
            # a real call: they called us and someone answered, or our call ran 1 min+
            inbound_live = (c.get("direction") == "inbound" and c.get("duration", 0) > 0
                            and str(c.get("status") or "").lower() != "missed")
            if inbound_live or (c.get("direction") == "outbound" and c.get("duration", 0) >= 60):
                live_calls[ph].append(c["when"])
        waits, unanswered, turned = [], 0, 0
        for ph, t in first_reply.items():
            after = [x for x in out_calls.get(ph, []) + hand_texts.get(ph, []) if x > t]
            if after:
                waits.append((min(after) - t).total_seconds() / 60)
            else:
                unanswered += 1
            turned += any(x > t for x in live_calls.get(ph, []))
        waits.sort()
        s["response"] = {"people": len(first_reply), "answered": len(waits), "unanswered": unanswered,
                         "median_min": (waits[len(waits) // 2] if waits else None),
                         "within_1h": sum(1 for w in waits if w <= 60)}
        s["turned_into_call"] = [turned, len(first_reply)]

    # 29, 31: what happened in DataSift after a reply
    lo = (datetime.date.fromisoformat(day_from) - datetime.timedelta(days=REPLY_LOOKBACK_DAYS))
    recent = {ph: ts for ph, ts in reply_times.items()
              if ph and any(lo <= t.date() and t <= end for t in ts)}
    try:
        crm = crm_outcomes(recent, agent["records"], day_from, day_to)
        s["leads_from_texts"] = crm["leads"]
        s["correct_from_texts"] = crm["correct"]
        s["confirmed_records"] = crm["confirmed"]
        s["text_confirmed"] = len(crm["confirmed"])
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
    calls = _calls(monday if week else day)
    result["day"] = compute(day, day, log_rows, agent, calls)
    if week:
        result["week"] = compute(monday, day, log_rows, agent, calls)
    return result


def _calls(since: str) -> list[dict] | None:
    """smrtPhone call log from `since` to now; None when it can't be read."""
    try:
        import smrtphone_calls as sc
        return sc.calls_since(since)
    except Exception:
        return None


def build_days(days: list[str], db: Path = DB_DEFAULT) -> dict:
    """{day: stats} for several days off one read of the log (the sheet's Texts tab)."""
    log_rows = fetch_log(min(days))
    agent = load_agent(db)
    calls = _calls(min(days))
    return {d: compute(d, d, log_rows, agent, calls) for d in days}


SHEET_COLUMNS = [
    "Date", "Sent", "People Texted", "Agent", "By Hand", "New Starts", "Follow-ups",
    "Delivered", "Delivered %", "Undelivered", "Undelivered %", "Still Pending",
    "Undelivered Reasons", "Spam Blocks", "Replies", "People Replied",
    "T1 Sent", "T1 Replied", "T1 Reply %", "T2 Sent", "T2 Replied", "T2 Reply %",
    "T3 Sent", "T3 Replied", "T3 Reply %", "T4 Sent", "T4 Replied", "T4 Reply %",
    "Interested", "Not Interested", "Wrong #", "Unclear", "Opt-outs", "Opt-out %",
    "Leads from Text Reply", "Correct after Text Reply",
    # added 2026-10-30
    "People Delivered", "Reply % (of delivered)", "Text Confirmed", "Reply Response (median min)",
    "Responded within 1 hr", "Replies Not Answered Yet", "Replies that Became a Call",
    "Finished 4 Touches", "Sending Numbers",
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
    pdel = s.get("people_delivered")
    resp = s.get("response") or {}
    turned = s.get("turned_into_call")
    row.update({
        "People Delivered": pdel if pdel is not None else "",
        "Reply % (of delivered)": _pct(s["repliers"], pdel) if pdel else "",
        "Text Confirmed": s.get("text_confirmed", ""),
        "Reply Response (median min)": (round(resp["median_min"]) if resp.get("median_min") is not None else ""),
        "Responded within 1 hr": resp.get("within_1h", ""),
        "Replies Not Answered Yet": resp.get("unanswered", ""),
        "Replies that Became a Call": (f"{turned[0]} of {turned[1]}" if turned else ""),
        "Finished 4 Touches": s.get("finished_4", ""),
        "Sending Numbers": "; ".join(
            f"...{x['last4']} {x['sent']} sent/{x['delivered']} delivered, "
            + ("flags unknown" if x["flags"] is None else ("no flags" if not x["flags"] else "flagged " + ", ".join(x["flags"])))
            for x in s.get("senders") or []),
    })
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
    split = []
    if "new_starts" in s:
        split.append(f"new starts {s['new_starts']} · follow-ups {s['follow_ups']}")
    split.append(f"agent {s['agent']} · by hand {s['by_hand']}")
    lines = [
        f"• Sent: *{sent}* _(texts sent, to {s['people_texted']} people)_  ·  " + " · ".join(split),
        f"• Delivered: *{s['delivered']}* _({_pct(s['delivered'], sent)} of sent reached the phone)_  ·  "
        f"undelivered {s['undelivered']}" + (f"  ·  still pending {s['pending']}" if s["pending"] else ""),
    ]
    if s["undelivered_reasons"]:
        lines.append("    Why undelivered: " + " · ".join(f"{n} {why}" for why, n in s["undelivered_reasons"].items()))
    for w, n in s["spam_by_wording"]:
        lines.append(f"    ⚠️ Spam blocked {n}x: {w}")
    pdel = s.get("people_delivered")
    rate = f"{_pct(s['repliers'], pdel)} of people who got a text" if pdel else "no texts delivered"
    who = "1 person" if s["repliers"] == 1 else f"{s['repliers']} people"
    lines.append(f"• Replies: *{s['replies']}* from {who} _(reply rate {rate})_")
    if any(n for _, n in (s.get("reply_rate_by_touch") or {}).values()):
        lines.append("    By touch, delivered only: " + " · ".join(
            f"T{t} {_pct(r, n)} ({r}/{n})" for t, (r, n) in s["reply_rate_by_touch"].items() if n))
    if "outcomes" in s:
        o = s["outcomes"]
        lines.append(f"    What they said: interested {o['interested']} · not interested {o['not_interested']} · "
                     f"wrong # {o['wrong_number']} · unclear {o['unclear']} · "
                     f"opt-outs {o['opt_out']} ({_pct(o['opt_out'], s['people_texted'])} of people texted)")
    if "text_confirmed" in s:
        lines.append(f"• Text confirmed: *{s['text_confirmed']}* _(owners confirmed by text: the number was marked "
                     f"Correct, or the record became a lead / Not Interested, within 14 days of their reply)_")
    if "response" in s:
        r = s["response"]
        med = r["median_min"]
        med_txt = ("n/a" if med is None else f"{med:.0f} min" if med < 120 else f"{med / 60:.1f} hrs")
        lines.append(f"• Reply response time: *{med_txt}* _(median time from a reply to our first call or "
                     f"hand-typed text back; opt-outs and wrong numbers left out)_  ·  "
                     f"{r['within_1h']} of {r['people']} within 1 hr  ·  {r['unanswered']} not answered yet")
    if "turned_into_call" in s:
        t, n = s["turned_into_call"]
        lines.append(f"• Replies that became a call: *{t} of {n}* _(a live call with that person after "
                     f"their reply: they called us, or our call ran 1 min+)_")
    if "finished_4" in s:
        lines.append(f"• Finished all 4 touches: *{s['finished_4']}* _(records that got touch 4)_")
    if s.get("senders"):
        parts = []
        for x in s["senders"]:
            f = x["flags"]
            ftxt = "flags unknown" if f is None else ("no spam flags" if not f else "flagged " + ", ".join(f))
            parts.append(f"...{x['last4']} {x['sent']} sent / {x['delivered']} delivered, {ftxt}")
        day = max((x["flag_day"] for x in s["senders"]), default="")
        lines.append(f"• Sending numbers _(carrier spam flags{' as of ' + day[5:] if day else ''})_: "
                     + " · ".join(parts))
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
