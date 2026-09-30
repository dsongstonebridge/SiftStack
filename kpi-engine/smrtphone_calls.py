"""smrtphone_calls.py - read the smrtPhone call log with the saved login session.

Session: SiftStack/smrtphone_state.json (made by smrtphone_login.bat), or the
SMRTPHONE_STATE environment variable (the same file's contents, for GitHub).
The login lasts about a month; when it expires this raises SessionExpired and the
report says to re-run smrtphone_login.bat.
"""
from __future__ import annotations
import datetime, json, os, urllib.parse, urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://phone.smrt.studio"
TZ = ZoneInfo("America/Chicago")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
COLUMNS = ["id", "user", "user_id", "created_at", "direction", "status", "disposition", "from_num",
           "to_num", "price", "duration", "podio_id", "recording_sid", "sid", "call_agent_id"]
STATE_FILE = Path(__file__).resolve().parent.parent / "smrtphone_state.json"


class SessionExpired(Exception):
    pass


def _cookie_header() -> str:
    raw = os.environ.get("SMRTPHONE_STATE", "").strip()
    if raw:
        st = json.loads(raw.lstrip("﻿"))
    elif STATE_FILE.exists():
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    else:
        raise SessionExpired("no smrtPhone login saved - run smrtphone_login.bat")
    return "; ".join(f"{c['name']}={c['value']}" for c in st.get("cookies", [])
                     if "smrt.studio" in c.get("domain", ""))


def _form(start: int, length: int) -> dict:
    f = {"draw": "1", "start": str(start), "length": str(length),
         "order[0][column]": "3", "order[0][dir]": "desc", "search[value]": "", "search[regex]": "false"}
    for i, c in enumerate(COLUMNS):
        f.update({f"columns[{i}][data]": c, f"columns[{i}][name]": "", f"columns[{i}][searchable]": "true",
                  f"columns[{i}][orderable]": "true", f"columns[{i}][search][value]": "",
                  f"columns[{i}][search][regex]": "false"})
    return f


def _page(cookie: str, start: int, length: int) -> dict:
    req = urllib.request.Request(BASE + "/logs/calls/filtered", method="POST",
        data=urllib.parse.urlencode(_form(start, length)).encode(),
        headers={"cookie": cookie, "user-agent": UA, "x-requested-with": "XMLHttpRequest",
                 "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "origin": BASE, "referer": BASE + "/logs/calls"})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read().decode("utf-8", "replace")
    if not body.lstrip().startswith("{"):
        raise SessionExpired("smrtPhone login expired - run smrtphone_login.bat")
    return json.loads(body)


def _local(row) -> datetime.datetime | None:
    c = row.get("created_at")
    s = c.get("date") if isinstance(c, dict) else c
    try:
        return (datetime.datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
                .replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ))
    except Exception:
        return None


def _name(v):
    return (v.get("name") if isinstance(v, dict) else v) or ""


def _num(v, key):
    return (v.get(key) if isinstance(v, dict) else v) or ""


def calls_since(day_from: str, page_size: int = 200) -> list[dict]:
    """All calls from day_from (YYYY-MM-DD, Central) to now, newest first, normalized."""
    cookie = _cookie_header()
    out, start = [], 0
    while True:
        data = _page(cookie, start, page_size)
        rows = data.get("data") or []
        stop = False
        for r in rows:
            dt = _local(r)
            if not dt:
                continue
            if dt.date().isoformat() < day_from:
                stop = True
                break
            out.append({
                "id": r.get("id"), "sid": r.get("sid") or "", "when": dt, "day": dt.date().isoformat(),
                "user": _name(r.get("user")) or _name(r.get("user_id")) or "Unassigned",
                "direction": r.get("direction") or "", "status": r.get("status") or "",
                "disposition": r.get("disposition") or "", "duration": int(r.get("duration") or 0),
                "contact": _num(r.get("to_num") if r.get("direction") == "outbound" else r.get("from_num"), "contactName"),
                "number": _num(r.get("to_num"), "toNum") if r.get("direction") == "outbound" else _num(r.get("from_num"), "fromNum"),
                "crm_link": r.get("podio_id") or "",
            })
        start += page_size
        if stop or not rows or start >= int(data.get("recordsFiltered") or 0):
            break
    return out


DISPO_COLS = [  # (column in the SmrtDialer tab, test on the lower-cased disposition)
    ("DNC Correct", lambda d: "dnc" in d and "correct" in d),
    ("DNC Wrong", lambda d: "dnc" in d and "wrong" in d),
    ("DNC Unknown", lambda d: "dnc" in d and "unknown" in d),
    ("DNC", lambda d: "dnc" in d or "do not call" in d),
    ("New Lead", lambda d: "lead" in d),
    ("Dead Number", lambda d: "dead" in d or "disconnected" in d),
    ("Follow-Up Later", lambda d: "follow" in d),
    ("Callback", lambda d: "call back" in d or "callback" in d),
    ("Wrong Number", lambda d: "wrong" in d),
    ("Not Interested", lambda d: "not interested" in d),
    ("New Buyer", lambda d: "buyer" in d),
    ("Sold", lambda d: "sold" in d),
    ("Listed", lambda d: "listed" in d),
    ("Just Correct Number", lambda d: "correct" in d),
    ("No Status", lambda d: True),   # "No Disposition" and anything unrecognized
]


def smrtdialer_rows(calls: list[dict], days: list[str]) -> dict:
    """{(caller, day): {SmrtDialer column: value}} for the given days."""
    by = {}
    for c in calls:
        if c["day"] not in days or c["direction"] != "outbound":
            continue
        by.setdefault((c["user"], c["day"]), []).append(c)
    rows = {}
    for (user, day), cs in by.items():
        talk = sum(c["duration"] for c in cs)
        connected = [c["duration"] for c in cs if c["duration"] > 0]
        first, last = min(c["when"] for c in cs), max(c["when"] for c in cs)
        session_min = (last - first).total_seconds() / 60 + (cs[0]["duration"] / 60 if len(cs) == 1 else 0)
        row = {"Campaign Type": "Click-to-call", "Dials": len(cs),
               "Avg Call Duriation": round(sum(connected) / len(connected)) if connected else 0,
               "Talk Jaye (hour)": round(talk / 3600, 2),
               "Dial Jaye (minutes)": round(talk / 60, 1),
               "Session Duriation (hours)": round(session_min / 60, 2),
               "Idle Jaye (minutes)": round(max(session_min - talk / 60, 0), 1)}
        for col, _ in DISPO_COLS:
            row[col] = 0
        for c in cs:
            d = c["disposition"].lower()
            for col, test in DISPO_COLS:
                if test(d):
                    row[col] += 1
                    break
        rows[(user, day)] = row
    return rows
