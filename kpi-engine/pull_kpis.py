"""
pull_kpis.py - self-contained DataSift (REISift) KPI puller for the kpi-engine skill.

Reads YOUR account's per-record activity log and produces a graded KPI report:
dials, answer/conversation/contact rates, correct numbers, dispositions, leads
(including new_lead statuses), talk time, per caller and per day, with funnel
pacing. Markdown + CSV always; Excel with --xlsx (needs openpyxl); optional
Slack digest with --slack <webhook-url>.

AUTH (your own login, ~48h token):
  app.reisift.io -> DevTools (F12) -> Network -> any apiv2.reisift.io request ->
  copy the authorization Bearer JWT. Then either:
    set REISIFT_TOKEN=<jwt>          (env var), or
    save it in reisift_token.txt next to this script.

USAGE
  python pull_kpis.py --days 7
  python pull_kpis.py --from 2026-07-06 --to 2026-07-16 --xlsx
  python pull_kpis.py --days 1 --slack https://hooks.slack.com/services/...
  python pull_kpis.py --show-benchmarks
  python pull_kpis.py --days 7 --tz America/Chicago

Benchmarks: defaults below; override any key in a benchmarks.json next to this
script (add your admin logins to excluded_callers, custom lead statuses, etc.).
Read-only against DataSift. Standard library only.
"""
from __future__ import annotations

import argparse
import re
import csv
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import sms_kpis   # text-message section of the post (same folder)

HERE = Path(__file__).resolve().parent
API = "https://apiv2.reisift.io"
HEADERS_BASE = {
    "accept": "application/json, text/plain, */*",
    "origin": "https://app.reisift.io",
    "referer": "https://app.reisift.io/",
    "x-reisift-ui-version": "2022.02.01.7",
    "user-agent": "Mozilla/5.0",
}

DEFAULT_BENCHMARKS = {
    "dials_floor_per_caller": 150,
    "dials_target_per_caller": 200,
    "conversations_floor_per_caller": 5,
    "conversation_min_seconds": 60,
    "meaningful_conversation_min_seconds": 120,
    "voicemail_max_seconds": 30,
    "dials_per_correct_scored": 9,
    "dials_per_correct_blind": 32,
    "correct_numbers_per_deal": 100,
    "leads_per_caller_day": [2, 3],
    "leads_per_contract": [15, 20],
    "appointment_take_rate": 0.25,
    # Leads (Jeff, 2026-10-30): exactly these eight statuses. Not Interested is NOT a lead.
    # DataSift writes them as lowercase titles in the log ("ghosting lead"), plus the
    # system slug "new_lead"; matching is case-insensitive.
    # Qualified = Cold, Warm or Hot only (same as the sheet's "New Leads Qualified").
    "lead_statuses": ["Lead", "New Lead", "new_lead", "No Contact New Lead", "Nurture New Lead",
                      "Cold Lead", "Warm Lead", "Hot Lead", "Ghosting Lead"],
    "qualified_statuses": ["Cold Lead", "Warm Lead", "Hot Lead"],
    "excluded_callers": [],
}

CORRECT_STATES = {"CORRECT", "CORRECT_DNC"}
WRONG_STATES = {"WRONG", "WRONG_DNC"}
CALL_EVENTS = {"owner.call.made", "owner.call.answered", "owner.call.noanswer",
               "owner.call.received", "owner.call.missed"}


def log(msg: str) -> None:
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def load_benchmarks() -> dict:
    bench = dict(DEFAULT_BENCHMARKS)
    f = HERE / "benchmarks.json"
    if f.exists():
        bench.update(json.loads(f.read_text(encoding="utf-8")))
    return bench


def _read_env_value(path: Path, key: str) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def get_token() -> str:
    # Preferred: the DataSift API key (never expires). Looked up in the env var,
    # then this folder's .env, then the SiftStack project's .env one folder up.
    key = os.environ.get("DATASIFT_API_KEY", "").strip()
    for env_file in (HERE / ".env", HERE.parent / ".env"):
        if not key:
            key = _read_env_value(env_file, "DATASIFT_API_KEY")
    if key:
        return "ApiKey:" + key
    tok = os.environ.get("REISIFT_TOKEN", "").strip()
    if not tok:
        f = HERE / "reisift_token.txt"
        if f.exists():
            tok = f.read_text(encoding="utf-8").strip()
    if not tok:
        sys.exit("No token. Set REISIFT_TOKEN or create reisift_token.txt (see script docstring).")
    return tok.removeprefix("Bearer ").strip()


def req(token: str, path: str, *, method="GET", body=None, method_override=None, params=None):
    url = API + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    headers = dict(HEADERS_BASE)
    if token.startswith("ApiKey:"):
        headers["authorization"] = "Api-Key " + token[len("ApiKey:"):]
    else:
        headers["authorization"] = f"Bearer {token}"
    if body is not None:
        headers["content-type"] = "application/json"
    if method_override:
        headers["x-http-method-override"] = method_override
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    time.sleep(0.03)
    # DataSift rate-limits bursts (429), e.g. right after the sheet fill made hundreds
    # of calls. Wait and retry instead of failing the whole post (2026-10-01 incident).
    for attempt in range(8):
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503, 504) or attempt == 7:
                return _http_fail(e, path)
            wait = _retry_wait(e, attempt)
            log(f"DataSift said {e.code} on {path}; waiting {wait:.0f}s and retrying ({attempt + 1}/7)")
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 7:
                raise
            log(f"network error on {path} ({e}); retrying")
            time.sleep(5 * (attempt + 1))


def _retry_wait(e, attempt: int) -> float:
    hdr = (e.headers or {}).get("Retry-After") if hasattr(e, "headers") else None
    try:
        if hdr:
            return min(max(float(hdr), 1.0), 120.0)
    except ValueError:
        pass
    try:   # DataSift's body says "Expected available in N seconds."
        import re as _r
        m = _r.search(r"available in (\d+)", e.read().decode("utf-8", "replace"))
        if m:
            return min(float(m.group(1)) + 1, 120.0)
    except Exception:
        pass
    return min(5.0 * 2 ** attempt, 60.0)


def _http_fail(e, path):
    if e.code == 401:
        sys.exit("DataSift said 401 (not authorized) on " + path +
                 " - the API key or token was rejected for this endpoint.")
    if e.code == 403:
        sys.exit("DataSift said 403 (forbidden) on " + path +
                 " - this endpoint does not accept these credentials.")
    raise e


def search_updated(token: str, day_from: str, day_to_excl: str) -> list[dict]:
    out, offset, limit = [], 0, 200
    while True:
        body = {"limit": limit, "offset": offset, "ordering": "-list_count",
                "query": {"must": {"property_type": "clean", "updated": [day_from, day_to_excl]}}}
        r = req(token, "/api/internal/property/", method="POST", body=body, method_override="GET")
        rows = r.get("results") or r.get("data") or []
        out.extend(rows)
        total = r.get("count", len(out))
        offset += limit
        if offset >= total or not rows or offset > 20000:
            break
    return out


def get_logs(token: str, uuid: str) -> list[dict]:
    r = req(token, f"/api/internal/property/{uuid}/logs/", params={"limit": 250, "offset": 0})
    return r.get("results") or r.get("data") or []


# ---- event helpers ----
def local_dt(ts: str, tz):
    try:
        return (datetime.datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                .replace(tzinfo=ZoneInfo("UTC")).astimezone(tz))
    except Exception:
        return None


def caller_of(ev):
    call = (ev.get("payload") or {}).get("call") or {}
    if call.get("direction") == "inbound":
        return ("inbound", "Inbound")
    eu = call.get("external_user") or {}
    return (eu.get("email") or "unknown", eu.get("name") or eu.get("email") or "Unknown")


def author_of(ev):
    info = ev.get("author_extra_info") or {}
    email = info.get("email") or ev.get("author") or "system"
    return (email, (f"{info.get('first_name', '')} {info.get('last_name', '')}".strip() or email))


def detect_new_index(events, key):
    """Status payloads carry [a,b] with no guaranteed order; vote by chaining."""
    by_tgt = defaultdict(list)
    for ev in events:
        obj = (ev.get("payload") or {}).get("owner" if key == "phone" else "property")
        if not isinstance(obj, dict):
            continue
        pair = obj.get("status")
        tgt = obj.get("phone") if key == "phone" else ev.get("resource")
        if isinstance(pair, list) and len(pair) == 2 and tgt:
            by_tgt[tgt].append((ev.get("timestamp", ""), pair))
    old_new = new_old = 0
    for seq in by_tgt.values():
        seq.sort()
        for (_, e1), (_, e2) in zip(seq, seq[1:]):
            if e1[1] == e2[0] and e1[1] != e2[1]:
                old_new += 1
            elif e1[0] == e2[1] and e1[0] != e2[0]:
                new_old += 1
    return 0 if new_old > old_new else 1


def new_status(ev, key, idx):
    pair = ((ev.get("payload") or {}).get("owner" if key == "phone" else "property") or {}).get("status")
    if isinstance(pair, list) and len(pair) == 2:
        return pair[idx]
    return pair[-1] if isinstance(pair, list) and pair else None


def _flat_address(a):
    if isinstance(a, dict):
        parts = [a.get("street"), a.get("city"), a.get("state"), a.get("zip5") or a.get("postal_code")]
        return ", ".join(str(x) for x in parts if x)
    return a or ""


def _flat_status(st):
    if isinstance(st, dict):
        return st.get("title") or st.get("name") or st.get("label") or ""
    return st or ""


PROBE = {"on": False}


def _digits10(v):
    d = "".join(ch for ch in str(v or "") if ch.isdigit())
    return d[-10:] if len(d) >= 10 else ""


def call_number(call: dict) -> str:
    """The OTHER party's number on a call (the owner's phone), last 10 digits."""
    n = _digits10(call.get("phone"))  # DataSift/smrtPhone put the owner's number here
    if n:
        return n
    inbound = call.get("direction") == "inbound"
    keys = (("from", "from_number", "caller", "phone", "phone_number", "number") if inbound
            else ("to", "to_number", "callee", "phone", "phone_number", "number"))
    for k in keys:
        v = call.get(k)
        if isinstance(v, dict):
            v = v.get("number") or v.get("phone")
        n = _digits10(v)
        if n:
            return n
    return ""


import re as _re
_VM_NOTE = _re.compile(
    r"\b(no answer|n/?a\b|didn'?t answer|did not answer|vms?|v/m|lvm|left (a )?(vms?|voicemails?|messages?|msg)|"
    r"voice ?mail|mailbox|went to (vm|voicemail)|no pick ?up|rang out|straight to (vm|voicemail)|"
    r"can'?t get a ?hold|couldn'?t (get a ?hold|reach)|unable to reach|no luck|answering (machine|service)|"
    r"nobody answered|no one answered|wasn'?t answered)\b",
    _re.I)

# "VM TXT" = voicemail text. Jeff only writes it when the owner did not answer
# (2026-10-07), so it always means voicemail, whatever else the note says.
_VM_TXT_NOTE = _re.compile(r"\bvm\s*[/+&,-]?\s*(txt|text)(ed|s)?\b", _re.I)


# A note that clearly describes a real conversation wins over a stray "vm" mention.
_TALK_NOTE_RE = _re.compile(r"\b(talked|spoke|spoken|convo|conversation|said|says|wants|asking|told me|motivated|interested|offer|appointment|appt|reached|hung up|picked up|when i said|answer|answered)\b", _re.I)


class _TalkNote:
    """Same .search() interface as before, but ignores negated 'answer' phrases."""
    @staticmethod
    def search(text):
        return _TALK_NOTE_RE.search(_talk_words(text))


_TALK_NOTE = _TalkNote()

_STRONG_TALK = _re.compile(r"\b(talked|spoke|spoken|convo|conversation|told me|picked up|hung up|"
                           r"answer|answered|got a ?hold|reached (him|her|them|owner))\b", _re.I)
# "answer(ed)" means a person picked up (Jeff, 2026-10-02) - but never in a negated phrase.
_NEGATED_ANSWER = _re.compile(r"\b(no|not|never|nobody|didn'?t|did not|don'?t|won'?t|wouldn'?t|wasn'?t|"
                              r"was not|doesn'?t)\s+(\w+\s+)?answer(ed|ing|s)?\b|\banswering (machine|service)\b", _re.I)


def _talk_words(text: str) -> str:
    """The note with negated 'answer' phrases removed, so only real talk words remain."""
    return _NEGATED_ANSWER.sub(" ", text or "")


def _is_vm_note(text: str) -> bool:
    # The skip-trace pipeline posts long structured notes (CASE / PROPERTY / SIGNING
    # CHAIN ...) as the same user; those can contain "N/A". Only short, hand-typed
    # call notes are read.
    if len(text) > 280 or _re.search(r"SIGNING CHAIN|CASE:|PROPERTY:|OWNER:|PETITION", text):
        return False
    if _VM_TXT_NOTE.search(text):
        return True
    # A voicemail note is only overruled by words that mean a person was actually on the
    # line. Soft words ("said", "wants") are not enough: "VM greeting said his name" is a
    # voicemail (Jeff, 2026-10-02).
    return bool(_VM_NOTE.search(text)) and not _STRONG_TALK.search(_talk_words(text))


def _message_text(ev) -> str:
    pl = ev.get("payload") or {}
    for obj in (pl.get("message"), pl.get("note"), pl):
        if isinstance(obj, dict):
            for k in ("message", "text", "content", "body", "note"):
                v = obj.get(k)
                if isinstance(v, str) and v.strip():
                    return v
        elif isinstance(obj, str) and obj.strip():
            return obj
    return ""


def _human_author(ev) -> bool:
    a = ev.get("author")
    return bool(a) and a != "system" and (ev.get("source") or "") not in ("smrtphone", "api", "system")


def length_band(sec: int) -> str:
    """Which call-length bucket a LIVE pickup falls in (Jeff, 2026-10-30: voicemails are
    never put in a bucket, so the four always add up to Reached)."""
    return ("len_lt30" if sec < 30 else "len_30_60" if sec < 60
            else "len_60_120" if sec < 120 else "len_120p")


def blank():
    return {"dials": 0, "answered": 0, "noanswer": 0, "talk_seconds": 0, "sms_sent": 0,
            "sms_received": 0, "conversations": 0, "meaningful_conversations": 0,
            "band_vm": 0, "band_brief": 0, "voicemails": 0, "non_owner_talks": 0, "reached": 0,
            "inbound_owner_talks": 0, "inbound_calls": 0, "correct_numbers": 0, "wrong_numbers": 0,
            "dead_numbers": 0, "dnc_numbers": 0, "leads": 0, "qualified": 0, "not_interested": 0,
            "follow_ups": 0, "appointments": 0, "records": set(), "days": set(),
            "first_call": None, "last_call": None,
            # call length of LIVE pickups only (voicemails never reach these)
            "len_lt30": 0, "len_30_60": 0, "len_60_120": 0, "len_120p": 0,
            "dial_seconds": 0, "correct_live": 0, "missed_callbacks": 0, "missed_not_returned": 0,
            "leads_by_status": Counter()}


def pull(token: str, day_from: str, day_to: str, tz, bench: dict) -> dict:
    lead_set = {s.lower() for s in bench["lead_statuses"]}  # case-insensitive: reisift mixes casings (Ty fix)
    qual_set = {s.lower() for s in bench.get("qualified_statuses", ["Cold Lead", "Warm Lead", "Hot Lead"])}
    excluded = {e.lower() for e in bench["excluded_callers"]}
    conv_s, mean_s, vm_s = (bench["conversation_min_seconds"],
                            bench["meaningful_conversation_min_seconds"],
                            bench["voicemail_max_seconds"])
    to_excl = (datetime.date.fromisoformat(day_to) + datetime.timedelta(days=1)).isoformat()
    # Search from day_from through TODAY (not just the window): a record worked on the
    # window's days but touched again since has a later "updated" stamp and would
    # otherwise be missed. Events are still filtered to the window below.
    today_excl = (datetime.datetime.now(tz).date() + datetime.timedelta(days=1)).isoformat()
    cand = search_updated(token, day_from, max(to_excl, today_excl))
    log(f"{len(cand)} candidate records updated in window; fetching activity logs...")

    rec_events, rec_meta, rec_all = {}, {}, {}
    for i, r in enumerate(cand, 1):
        uuid = r.get("uuid")
        if not uuid:
            continue
        try:
            evs = get_logs(token, uuid)
        except Exception:
            continue
        if PROBE["on"]:
            PROBE.setdefault("all_types", Counter()).update(e.get("event_type") for e in evs)
            if "sample_record" not in PROBE:
                PROBE["sample_record"] = {k: r[k] for k in r if k in ("status", "address", "updated", "created", "tags")}
                PROBE["sample_record_keys"] = sorted(r.keys())
            if evs and len(PROBE.setdefault("sample_events", [])) < 12:
                PROBE["sample_events"].extend(evs[:4])
        keep = []
        for e in evs:
            dt = local_dt(e.get("timestamp", ""), tz)
            if dt and day_from <= dt.date().isoformat() <= day_to:
                keep.append((dt, e))
        if keep:
            rec_events[uuid] = keep
            rec_all[uuid] = evs
            rec_meta[uuid] = {"address": _flat_address(r.get("address")), "status": _flat_status(r.get("status"))}
        if i % 50 == 0:
            log(f"  {i}/{len(cand)} scanned...")
    log(f"{len(rec_events)} records had activity in window")
    if PROBE["on"]:
        PROBE["window_types"] = Counter(e.get("event_type") for evs in rec_events.values() for _, e in evs)
        PROBE["window_call_events"] = [
            {"record": uuid, "address": rec_meta[uuid]["address"], "local_time": str(dt), "event": e}
            for uuid, evs in rec_events.items() for dt, e in evs
            if str(e.get("event_type", "")).startswith(("owner.call", "owner.sms", "owner.phone.status", "property.message"))]
        dump = {k: (dict(v) if isinstance(v, Counter) else v) for k, v in PROBE.items() if k != "on"}
        (HERE / "reports").mkdir(exist_ok=True)
        (HERE / "reports" / "probe.json").write_text(json.dumps(dump, indent=2, default=str), encoding="utf-8")
        log("wrote reports/probe.json")

    all_phone = [e for evs in rec_events.values() for _, e in evs
                 if e.get("event_type") == "owner.phone.status.updated"]
    all_prop = [e for evs in rec_events.values() for _, e in evs
                if e.get("event_type") == "property.status.updated"]
    p_idx = detect_new_index(all_phone, "phone")
    s_idx = detect_new_index(all_prop, "property")

    # Jeff's rule (2026-09-28): an answered call is a real conversation only when the
    # number called is dispositioned Correct on that record. Otherwise the pickup was a
    # voicemail. Uses the record's whole history, so a number marked Correct on an
    # earlier day still counts. Inbound calls are always real (the owner called us).
    correct_by_rec, wrong_by_rec = {}, {}
    for uuid, evs in rec_all.items():
        last = {}
        for e in evs:
            if e.get("event_type") != "owner.phone.status.updated":
                continue
            ph = _digits10(((e.get("payload") or {}).get("owner") or {}).get("phone"))
            st = new_status(e, "phone", p_idx)
            ts = e.get("timestamp", "")
            if ph and st and (ph not in last or ts >= last[ph][0]):
                last[ph] = (ts, st)
        correct_by_rec[uuid] = {ph for ph, (_, st) in last.items() if str(st).upper() in CORRECT_STATES}
        wrong_by_rec[uuid] = {ph for ph, (_, st) in last.items() if str(st).upper() in WRONG_STATES}

    # Jeff's second rule: on a record with a Correct number, a same-day Message Board note
    # (written by a person, not the pipeline) saying "no answer / VM / voicemail / left
    # message" means that day's pickups were voicemails after all.
    # A record can have several numbers: if ANY same-day note describes reaching someone,
    # the day is not treated as voicemail-only.
    vm_note_days, talk_note_days = set(), set()
    for uuid, evs in rec_events.items():
        for dt, e in evs:
            if str(e.get("event_type", "")).startswith("property.message") and _human_author(e):
                txt, key = _message_text(e), (uuid, dt.date().isoformat())
                if _is_vm_note(txt):
                    vm_note_days.add(key)
                elif _TALK_NOTE.search(txt):
                    talk_note_days.add(key)
    vm_note_days -= talk_note_days
    # Jeff, 2026-10-02: a short call is still a conversation when the record shows it was one:
    # a talk note that day, or a status he set that day (Not interested, a lead, ...).
    # Only an OWNER outcome counts (Not interested or a lead status) - not moving a record
    # to exhausted / deep prospecting after wrong numbers.
    outcome_set = lead_set | {"not_interested", "not interested"}
    outcome_days = set(talk_note_days)
    for uuid, evs in rec_events.items():
        for dt, e in evs:
            if e.get("event_type") == "property.status.updated" and author_of(e)[0] != "system":
                if str(new_status(e, "property", s_idx) or "").lower() in outcome_set:
                    outcome_days.add((uuid, dt.date().isoformat()))

    num_recs = defaultdict(set)       # number -> DataSift records it was called on
    for uuid, evs in rec_events.items():
        for _, e in evs:
            if e.get("event_type") in CALL_EVENTS:
                n = call_number((e.get("payload") or {}).get("call") or {})
                if n:
                    num_recs[n].add(uuid)
    acct, per, daily = blank(), defaultdict(blank), defaultdict(blank)
    names, phone_final, prop_final, seen = {}, {}, {}, set()
    prop_first = {}   # uuid -> (dt, status before the day's first change)
    task_seen = set()
    live_pickups = set()      # (number, day): a real person picked up that number that day
    rpc_cands = set()         # (record uuid, number): the OWNER picked up / called us
    dial_span = {}            # (caller, day) -> [first dial, last dial]

    def bump(email, day, field, amt=1):
        acct[field] += amt
        per[email][field] += amt
        daily[day][field] += amt

    for uuid, evs in rec_events.items():
        for dt, ev in evs:
            et, day = ev.get("event_type"), dt.date().isoformat()
            if et in CALL_EVENTS or et in ("owner.sms.sent", "owner.sms.received"):
                o = (ev.get("payload") or {}).get("call") or (ev.get("payload") or {}).get("sms") or {}
                k = o.get("uuid") or o.get("external_id")
                if k is not None:
                    if (et, k) in seen:
                        continue
                    seen.add((et, k))
            if et in CALL_EVENTS:
                email, name = caller_of(ev)
                names[email] = name
                call = (ev.get("payload") or {}).get("call") or {}
                if et == "owner.call.made":
                    bump(email, day, "dials")
                    sp = dial_span.setdefault((email, day), [dt, dt])
                    sp[0], sp[1] = min(sp[0], dt), max(sp[1], dt)
                    for scope in (acct, per[email]):
                        scope["records"].add(uuid)
                        scope["days"].add(day)
                        if scope["first_call"] is None or dt < scope["first_call"]:
                            scope["first_call"] = dt
                        if scope["last_call"] is None or dt > scope["last_call"]:
                            scope["last_call"] = dt
                elif et == "owner.call.answered":
                    dur = int(call.get("duration") or 0)
                    inbound = call.get("direction") == "inbound"
                    if inbound:
                        bump(email, day, "inbound_calls")
                        # The owner called us, so it is a real conversation whatever the
                        # number's status: callbacks often come from a number that is not
                        # marked Correct, or not on the record at all (Jeff, 2026-10-02).
                        # Credit it to whoever answered, when DataSift says who.
                        who = (call.get("external_user") or {}).get("email")
                        if who:
                            email = who
                            names[who] = (call.get("external_user") or {}).get("name") or who
                    else:
                        # Outbound live = the RIGHT owner picked up: the number is
                        # dispositioned Correct and no same-day "no answer / VM" note.
                        num, vm_day = call_number(call), (uuid, day) in vm_note_days
                        if num in correct_by_rec.get(uuid, set()) and not vm_day:
                            bump(email, day, "answered")
                            rpc_cands.add((uuid, num))
                        elif num in wrong_by_rec.get(uuid, set()) and not vm_day:
                            # A person answered but it was not the owner (relative, new
                            # tenant...). Still a real conversation: Jeff, 2026-10-02, after
                            # a 7-minute "Wrong Number" call went uncounted.
                            bump(email, day, "non_owner_talks")
                            rpc_cands.add((uuid, num))
                        else:
                            bump(email, day, "voicemails")
                            continue
                    bump(email, day, "reached")
                    if inbound:
                        num = call_number(call)
                        bump(email, day, "inbound_owner_talks")
                        rpc_cands.add((uuid, num))
                    live_pickups.add((num, day))
                    bump(email, day, length_band(dur))
                    bump(email, day, "talk_seconds", dur)
                    if dur >= mean_s:
                        bump(email, day, "meaningful_conversations")
                    owner_talk = inbound or num in correct_by_rec.get(uuid, set())
                    if dur >= conv_s or (owner_talk and (uuid, day) in outcome_days):
                        bump(email, day, "conversations")
                    elif dur >= vm_s:
                        bump(email, day, "band_brief")
                    else:
                        bump(email, day, "band_vm")
                elif et == "owner.call.noanswer":
                    bump(email, day, "noanswer")
            elif et == "owner.sms.sent":
                eu = ((ev.get("payload") or {}).get("sms") or {}).get("external_user") or {}
                bump(eu.get("email") or author_of(ev)[0], day, "sms_sent")
            elif et == "owner.sms.received":
                acct["sms_received"] += 1
            elif et == "owner.phone.status.updated":
                phone = ((ev.get("payload") or {}).get("owner") or {}).get("phone")
                ns = new_status(ev, "phone", p_idx)
                if phone and ns:
                    prev = phone_final.get(phone)
                    if prev is None or dt >= prev[0]:
                        phone_final[phone] = (dt, ns, author_of(ev)[0])
            elif et == "property.status.updated":
                ns = new_status(ev, "property", s_idx)
                if ns:
                    prev = prop_final.get(uuid)
                    if prev is None or dt >= prev[0]:
                        prop_final[uuid] = (dt, ns, author_of(ev)[0])
                    first = prop_first.get(uuid)
                    if first is None or dt < first[0]:
                        prop_first[uuid] = (dt, new_status(ev, "property", 1 - s_idx))
            elif et == "task.created":
                # Jeff has no "follow up" status; follow-up work shows up as tasks
                # (cold/warm/hot follow-up, hung up follow up, offer follow-up, ...)
                task = (ev.get("payload") or {}).get("task") or {}
                tk = task.get("uuid") or (uuid, task.get("title"), str(dt))
                if "follow" in str(task.get("title") or "").lower() and tk not in task_seen:
                    task_seen.add(tk)
                    bump(author_of(ev)[0], day, "follow_ups")
            elif et == "task.completed":
                title = (((ev.get("payload") or {}).get("task") or {}).get("title") or "").lower()
                if any(k in title for k in ("appoint", "appt", "meeting", "consult")):
                    bump(author_of(ev)[0], day, "appointments")

    correct_marked = []       # (number, day, caller) for "Correct, live pickup"
    for phone, (dt, ns, email) in phone_final.items():
        day = dt.date().isoformat()
        if ns in CORRECT_STATES:
            bump(email, day, "correct_numbers")
            correct_marked.append((_digits10(phone), day, email))
        elif ns in WRONG_STATES:
            bump(email, day, "wrong_numbers")
        elif ns == "DEAD":
            bump(email, day, "dead_numbers")
        elif ns == "DNC":
            bump(email, day, "dnc_numbers")
    status_contacts = set()   # records newly set to a lead status or Not Interested
    for uuid, (dt, ns, email) in prop_final.items():
        day = dt.date().isoformat()
        started = str((prop_first.get(uuid) or (None, ""))[1] or "").lower()
        if ns.lower() in lead_set and started not in lead_set:   # newly became a lead
            bump(email, day, "leads")
            for scope in (acct, per[email], daily[day]):
                scope["leads_by_status"][ns.lower()] += 1
            status_contacts.add(uuid)
        if ns.lower() in ("not_interested", "not interested"):
            status_contacts.add(uuid)
        if ns.lower() in qual_set and started not in qual_set:   # newly Cold/Warm/Hot
            bump(email, day, "qualified")
        if ns == "not_interested":
            bump(email, day, "not_interested")

    for (email, day), (first, last) in dial_span.items():
        bump(email, day, "dial_seconds", int((last - first).total_seconds()))

    # Manual additions: real calls DataSift never saw (e.g. an owner calling from a number
    # that wasn't on any record yet). One row per call in manual_calls.csv:
    #   date,direction,owner_name,minutes,note     (date = YYYY-MM-DD, direction = inbound/outbound)
    mf = HERE / "manual_calls.csv"
    if mf.exists():
        with open(mf, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                d = (row.get("date") or "").strip()
                if not (day_from <= d <= day_to):
                    continue
                secs = int(float(row.get("minutes") or 0) * 60)
                who = "inbound" if (row.get("direction") or "").strip().lower() == "inbound" else "manual"
                if who == "inbound":
                    bump(who, d, "inbound_calls")
                else:
                    bump(who, d, "answered")
                bump(who, d, "talk_seconds", secs)
                if secs >= mean_s:
                    bump(who, d, "meaningful_conversations")
                if secs >= conv_s:
                    bump(who, d, "conversations")
                log(f"manual call added: {d} {row.get('owner_name', '')} {row.get('minutes')} min")

    # pull excluded/admin callers out of rollups
    for email in list(per):
        if email in ("inbound", "manual"):
            # Inbound calls belong to the account (someone called the business), so they
            # stay in the totals; they just aren't a caller row in the per-caller table.
            per.pop(email)
            continue
        if email.lower() in excluded or email in ("system", "unknown"):
            c = per.pop(email)
            for k, v in c.items():
                if isinstance(v, int):
                    acct[k] = max(0, acct[k] - v)

    # record-level detail (one row per worked record)
    details = []
    for uuid, evs in rec_events.items():
        d = {"address": rec_meta[uuid]["address"], "status": rec_meta[uuid]["status"],
             "dials": 0, "answered": 0, "talk_seconds": 0, "callers": set(),
             "final_phone_dispo": "", "status_set_to": "", "status_set_by": "",
             "is_lead": False, "last_activity": ""}
        for dt, ev in sorted(evs, key=lambda x: x[0]):
            et = ev.get("event_type")
            d["last_activity"] = dt.strftime("%Y-%m-%d %H:%M")
            if et == "owner.call.made":
                d["dials"] += 1
                d["callers"].add(caller_of(ev)[1])
            elif et == "owner.call.answered":
                d["answered"] += 1
                d["talk_seconds"] += int(((ev.get("payload") or {}).get("call") or {}).get("duration") or 0)
        if uuid in prop_final:
            _, ns, email = prop_final[uuid]
            d["status_set_to"] = ns
            d["status_set_by"] = names.get(email, email)
            d["is_lead"] = (ns or "").lower() in lead_set
        details.append(d)
    details.sort(key=lambda d: (-d["is_lead"], -d["dials"]))
    return {"from": day_from, "to": day_to, "account_totals": acct,
            "callers": dict(per), "daily": dict(daily), "names": names,
            "vm_note_days": vm_note_days, "num_recs": dict(num_recs), "outcome_days": outcome_days,
            "marked_correct": set().union(*correct_by_rec.values()) if correct_by_rec else set(),
            "marked_wrong": set().union(*wrong_by_rec.values()) if wrong_by_rec else set(),
            "records_detail": details, "live_pickups": live_pickups, "rpc_cands": rpc_cands,
            "correct_marked": correct_marked, "status_contacts": status_contacts,
            "dial_span": dial_span, "known_records": set(rec_events), "rec_logs": rec_all}


# ---- smrtPhone: the call log is the source of truth for talk time ----
# Jeff, 2026-10-02: use BOTH sources. DataSift says who the owner is (numbers he marked
# Correct / Wrong, leads, statuses); smrtPhone hears every call, including callbacks from
# numbers that are on no record. So conversations, minutes and inbound come from smrtPhone,
# and "Correct numbers" stays exactly what he marked in DataSift.
CALL_FIELDS = ("talk_seconds", "conversations", "meaningful_conversations", "inbound_calls",
               "non_owner_talks", "band_brief", "band_vm", "answered", "voicemails",
               "reached", "inbound_owner_talks", "len_lt30", "len_30_60", "len_60_120",
               "len_120p", "dial_seconds", "missed_callbacks", "missed_not_returned")
MISSED_STATUSES = ("missed", "no-answer", "no answer", "busy", "canceled", "cancelled", "failed")


def _sp_is_talk(c: dict, marked_correct: set, marked_wrong: set,
                vm_note_days: set = frozenset(), num_recs: dict | None = None):
    """'owner' / 'other' / None for one smrtPhone call."""
    if c["duration"] <= 0 or str(c["status"]).lower() not in ("completed", "answered", ""):
        return None
    d = str(c["disposition"]).lower()
    num = _digits10(c["number"])
    if c["direction"] == "inbound":
        return "other" if "wrong" in d else "owner"     # they called us: always a real call
    if "no answer" in d or "dead" in d or "busy" in d:
        return None                                      # voicemail / never reached anyone
    # Jeff, 2026-10-02: a number gets marked Correct (or Wrong) just because the VOICEMAIL
    # greeting said a name. The message board decides: a same-day "no answer / VM" note on
    # the record means this was a voicemail, whatever the disposition says.
    recs = (num_recs or {}).get(num, set())
    import re as _r
    m = _r.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", str(c.get("crm_link") or ""))
    if m:
        recs = set(recs) | {m.group(0)}
    if any((u, c["day"]) in vm_note_days for u in recs):
        return "vm"
    if "wrong" in d:
        return "other"
    if d in ("", "no disposition"):                      # not dispositioned: ask DataSift
        if num in marked_correct:
            return "owner"
        if num in marked_wrong:
            return "other"
        return None
    return "owner"                                       # Correct, Not interested, Lead, Callback...


def _sp_miss_reason(c: dict, kind) -> str:
    """Why a smrtPhone call was left out of 'reached' (Jeff, 2026-10-02: audit line)."""
    d = str(c["disposition"]).lower()
    if str(c["status"]).lower() not in ("completed", "answered", ""):
        return f"call status {c['status']}"
    if kind == "vm":
        return "same-day VM/no answer note on the record"
    for w in ("no answer", "dead", "busy"):
        if w in d:
            return f"dispositioned {c['disposition']}"
    if d in ("", "no disposition"):
        return "no disposition and number not marked Correct/Wrong in DataSift"
    return "not counted"


def add_smrtphone(res: dict, day_from: str, day_to: str, bench: dict) -> None:
    res["call_source"] = "DataSift only"
    try:
        import smrtphone_calls as sp
        all_calls = sp.calls_since(day_from)          # to NOW: shows whether a missed call was returned
        calls = [c for c in all_calls if day_from <= c["day"] <= day_to]
    except Exception as ex:                              # session expired, network...
        log(f"smrtPhone not used ({ex}); conversations come from DataSift alone")
        res["call_source"] = f"DataSift only - smrtPhone unavailable ({str(ex)[:80]})"
        return
    acct, per, daily, names = res["account_totals"], res["callers"], res["daily"], res["names"]
    by_name = {str(n).strip().lower(): e for e, n in names.items()}
    for scope in [acct, *per.values(), *daily.values()]:
        for f in CALL_FIELDS:
            scope[f] = 0
    conv_s, mean_s, vm_s = (bench["conversation_min_seconds"],
                            bench["meaningful_conversation_min_seconds"], bench["voicemail_max_seconds"])
    # smrtPhone decides who picked up, so the pickups DataSift alone suggested are replaced
    # (otherwise "Correct, live pickup" could count a call that Reached does not)
    res["live_pickups"], res["rpc_cands"] = set(), set()
    out_dials = 0
    missed = []                                          # 2 min+ calls left out of "reached"
    span = {}                                            # (user, day) -> [first, last] dial
    missed_in = {}                                       # number -> (day, user, earliest missed time)
    for c in calls:
        user = str(c["user"] or "").strip()
        if c["direction"] == "outbound":
            out_dials += 1
            sp_ = span.setdefault((user, c["day"]), [c["when"], c["when"]])
            sp_[0], sp_[1] = min(sp_[0], c["when"]), max(sp_[1], c["when"])
        elif (c["duration"] <= 0 or str(c["status"]).lower() in MISSED_STATUSES) and _digits10(c["number"]):
            n = _digits10(c["number"])
            if n not in missed_in or c["when"] < missed_in[n][2]:
                missed_in[n] = (c["day"], user, c["when"])
        kind = _sp_is_talk(c, res.get("marked_correct", set()), res.get("marked_wrong", set()),
                           res.get("vm_note_days", set()), res.get("num_recs", {}))
        if c["duration"] >= mean_s and kind in (None, "vm"):
            missed.append({"when": c["when"], "duration": c["duration"], "direction": c["direction"],
                           "number": _digits10(c["number"]), "reason": _sp_miss_reason(c, kind)})
        if kind == "vm" or (c["direction"] == "outbound" and "no answer" in str(c["disposition"]).lower()):
            for sc in [acct, daily.setdefault(c["day"], blank())]:
                sc["voicemails"] += 1
        if kind == "vm":
            kind = None
        kind_inbound = (c["direction"] == "inbound" and c["duration"] > 0
                        and str(c["status"]).lower() != "missed")
        if not kind and not kind_inbound:
            continue
        recs = set(res.get("num_recs", {}).get(_digits10(c["number"]), set()))
        m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", str(c.get("crm_link") or ""))
        if m:
            recs.add(m.group(0))
        noted = kind == "owner" and any((u, c["day"]) in res.get("outcome_days", set()) for u in recs)
        if kind:
            res.setdefault("live_pickups", set()).add((_digits10(c["number"]), c["day"]))
        # Every live pickup / callback is a contact CANDIDATE: finalize() decides from the
        # number's status whether it was a right-party (Correct + primary) or secondary
        # (Correct, not primary) contact. Only PROPERTY ids the pull saw count: smrtPhone's
        # crm link also carries the owner's own id (once counted every owner twice).
        for u in recs & res.get("known_records", set()):
            res.setdefault("rpc_cands", set()).add((u, _digits10(c["number"])))
        scopes = [acct, daily.setdefault(c["day"], blank())]
        user = str(c["user"] or "").strip()
        if user and user != "Unassigned":
            who = by_name.get(user.lower()) or ("sp:" + user)
            names.setdefault(who, user)
            scopes.append(per.setdefault(who, blank()))
        for sc in scopes:
            if kind_inbound:
                sc["inbound_calls"] += 1
            if not kind:
                continue
            # every counted call lands in exactly ONE bucket, so the buckets add up to "reached"
            sc["reached"] += 1
            if kind == "other":
                sc["non_owner_talks"] += 1
            elif c["direction"] == "inbound":
                sc["inbound_owner_talks"] += 1
            else:
                sc["answered"] += 1
            d = c["duration"]
            sc["talk_seconds"] += d
            if d >= mean_s:
                sc["meaningful_conversations"] += 1
            sc[length_band(d)] += 1
            if d >= conv_s or noted:
                sc["conversations"] += 1
            elif d >= vm_s:
                sc["band_brief"] += 1
            else:
                sc["band_vm"] += 1
    # Dial hours: first dial to last dial, per caller per day (a lunch break counts)
    for (user, day), (first, last) in span.items():
        secs = int((last - first).total_seconds())
        scopes = [acct, daily.setdefault(day, blank())]
        if user and user != "Unassigned":
            who = by_name.get(user.lower()) or ("sp:" + user)
            scopes.append(per.setdefault(who, blank()))
        for sc in scopes:
            sc["dial_seconds"] += secs
    # Missed callbacks: someone called us and nobody picked up (one per number). Returned =
    # we called that number afterwards, or they called again and got through.
    for n, (day, user, when) in missed_in.items():
        returned = any(_digits10(c["number"]) == n and c["when"] > when and
                       (c["direction"] == "outbound" or c["duration"] > 0)
                       for c in all_calls)
        for sc in (acct, daily.setdefault(day, blank())):
            sc["missed_callbacks"] += 1
            sc["missed_not_returned"] += not returned
    res["call_source"] = "smrtPhone call log + DataSift"
    res["sp_outbound"] = out_dials
    res["sp_missed"] = missed
    log(f"smrtPhone: {len(calls)} call(s) in window; conversations/talk time taken from smrtPhone")


# ---- rendering ----
def fmt_hms(sec):
    h, r = divmod(int(sec), 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def pct(n, d):
    return f"{n / d * 100:.1f}%" if d else "0.0%"


def missed_line(res: dict) -> str:
    """'smrtPhone calls over 2 min not counted: time + reason' - so misses surface the same night."""
    if "sp_missed" not in res:                           # smrtPhone not used this run
        return ""
    items = [f"{m['when'].strftime('%I:%M %p').lstrip('0')} {m['direction']} {fmt_hms(m['duration'])} "
             f"(...{m['number'][-4:]}) - {m['reason']}" for m in res["sp_missed"]]
    return "smrtPhone calls over 2 min not counted: " + ("; ".join(items) if items else "none")


def render_md(res: dict, bench: dict) -> str:
    a = res["account_totals"]
    lead_lo, lead_hi = bench["leads_per_caller_day"]
    c_lo, c_hi = bench["leads_per_contract"]
    take = bench["appointment_take_rate"]
    dials = a["dials"]
    out = [f"# KPI Report, {res['from']} to {res['to']}", "",
           f"- Dials: {dials}  |  Voicemail / no answer: {a['voicemails']}",
           f"- Reached a person: {a['reached']} = owner {a['answered']} + inbound callback "
           f"{a['inbound_owner_talks']} + someone else (wrong #) {a['non_owner_talks']}",
           f"- Conversations (1 min+ or noted on the message board): {a['conversations']}  |  brief pickups {a['reached'] - a['conversations']}"
           f"  |  2 min+ {a['meaningful_conversations']}  |  Calls & talk time: {res.get('call_source', 'DataSift only')}",
           f"- Correct numbers: {a['correct_numbers']} ({pct(a['correct_numbers'], dials)} right-party)"
           f"  |  Wrong {a['wrong_numbers']}  Dead {a['dead_numbers']}  DNC {a['dnc_numbers']}",
           f"- Leads: {a['leads']} (qualified Cold/Warm/Hot: {a['qualified']})  |  Not interested: {a['not_interested']}  |  "
           f"Follow-up tasks: {a['follow_ups']}  |  Appointments logged: {a['appointments']}",
           f"- Talk time: {fmt_hms(a['talk_seconds'])}  |  Texts: {a['sms_sent']} out / {a['sms_received']} in"]
    if missed_line(res):
        out.append(f"- {missed_line(res)}")
    out += ["", "## By caller", "",
           "| Caller | Days | Dials | Dials/day | Live% | Convos | Correct | NI | Leads | Floor |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for email, c in sorted(res["callers"].items(), key=lambda kv: -kv[1]["dials"]):
        if not any(c[k] for k in ("dials", "correct_numbers", "leads", "not_interested")):
            continue
        nd = len(c["days"]) or 1
        floor = bench["dials_floor_per_caller"] * len(c["days"])
        out.append(f"| {res['names'].get(email, email)} | {len(c['days'])} | {c['dials']} | "
                   f"{c['dials'] / nd:.0f} | {pct(c['answered'], c['dials'])} | {c['conversations']} | "
                   f"{c['correct_numbers']} | {c['not_interested']} | {c['leads']} | "
                   f"{'MET' if c['dials'] >= floor and floor else 'BELOW'} |")
    out += ["", "## Daily trend", "", "| Day | Dials | Answered | Convos | Correct | NI | Leads |",
            "|---|---|---|---|---|---|---|"]
    for day in sorted(res["daily"]):
        d = res["daily"][day]
        out.append(f"| {day} | {d['dials']} | {d['answered']} | {d['conversations']} | "
                   f"{d['correct_numbers']} | {d['not_interested']} | {d['leads']} |")
    dpc = f"{dials / a['correct_numbers']:.1f}" if a["correct_numbers"] else "n/a"
    out += ["", "## Funnel and pacing", "",
            f"- Dials per correct number: {dpc} (scored target ~{bench['dials_per_correct_scored']}, "
            f"blind ~{bench['dials_per_correct_blind']})",
            f"- Correct toward next deal: {a['correct_numbers']} / {bench['correct_numbers_per_deal']}",
            f"- Leads: {a['leads']} (baseline {lead_lo}-{lead_hi}/caller/day)  ->  "
            f"~{round(a['leads'] * take)} appointments at {take * 100:.0f}% take  ->  "
            f"{a['leads'] / c_hi:.1f}-{a['leads'] / c_lo:.1f} contracts at 1 per {c_lo}-{c_hi} leads"]
    return "\n".join(out)


def write_csv(res: dict, path: Path) -> None:
    fields = ["caller", "days", "dials", "answered", "conversations",
              "meaningful_conversations", "correct_numbers", "wrong_numbers",
              "not_interested", "leads", "appointments", "talk_seconds", "sms_sent"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(fields)
        for email, c in sorted(res["callers"].items(), key=lambda kv: -kv[1]["dials"]):
            w.writerow([res["names"].get(email, email), len(c["days"])] +
                       [c[f] for f in fields[2:]])


def write_xlsx(res: dict, bench: dict, path: Path) -> bool:
    try:
        from openpyxl import Workbook
    except ImportError:
        log("openpyxl not installed; skipping Excel (pip install openpyxl)")
        return False
    wb = Workbook()
    ws = wb.active
    ws.title = "By Caller"
    ws.append(["Caller", "Days", "Dials", "Answered", "Convos", "Correct", "NI", "Leads", "Talk"])
    for email, c in sorted(res["callers"].items(), key=lambda kv: -kv[1]["dials"]):
        ws.append([res["names"].get(email, email), len(c["days"]), c["dials"], c["answered"],
                   c["conversations"], c["correct_numbers"], c["not_interested"], c["leads"],
                   fmt_hms(c["talk_seconds"])])
    ws2 = wb.create_sheet("Daily")
    ws2.append(["Day", "Dials", "Answered", "Convos", "Correct", "NI", "Leads"])
    for day in sorted(res["daily"]):
        d = res["daily"][day]
        ws2.append([day, d["dials"], d["answered"], d["conversations"],
                    d["correct_numbers"], d["not_interested"], d["leads"]])
    if res.get("records_detail"):
        wsr = wb.create_sheet("Records Detail")
        wsr.append(["Address", "Current status", "Dials", "Answered", "Talk", "Callers",
                    "Status set to", "Set by", "Lead", "Last activity"])
        for d in res["records_detail"]:
            wsr.append([d["address"], d["status"], d["dials"], d["answered"],
                        fmt_hms(d["talk_seconds"]), ", ".join(sorted(d["callers"])),
                        d["status_set_to"], d["status_set_by"],
                        "YES" if d["is_lead"] else "", d["last_activity"]])
    ws3 = wb.create_sheet("Benchmarks")
    for k, v in bench.items():
        ws3.append([k, json.dumps(v) if isinstance(v, (list, dict)) else v])
    wb.save(path)
    return True


def text_stats(day_from: str, day_to: str) -> tuple[dict | None, str]:
    """(text KPIs for exactly this window, error). In GitHub they arrive from the PC as the
    workflow's `texts` input (TEXT_KPIS, base64 JSON: a day, plus Monday..day on Fridays);
    on the PC itself they are worked out here."""
    got = sms_kpis.from_env_b64(os.environ.get("TEXT_KPIS", ""))
    if not got and not os.environ.get("GITHUB_ACTIONS"):
        weekly = day_from != day_to
        try:
            got = sms_kpis.build(day_to, weekly, sms_kpis.DB_DEFAULT)
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"
    if not got:
        return None, "this run was not started from the PC"
    for key in ("day", "week"):
        st = got.get(key) or {}
        if st and (st.get("from"), st.get("to")) == (day_from, day_to):
            return st, ""
    return None, got.get("error") or f"the PC sent no text stats for {day_from} to {day_to}"


def finalize(res: dict, token: str, texts: dict | None) -> None:
    """Numbers that need BOTH sources (DataSift + smrtPhone, calls + texts)."""
    acct, daily, per = res["account_totals"], res["daily"], res["callers"]
    live = res.get("live_pickups", set())
    for num, day, email in res.get("correct_marked", []):
        if (num, day) in live:
            for sc in (acct, daily.setdefault(day, blank()), per.setdefault(email, blank())):
                sc["correct_live"] += 1

    # Contacts (Jeff, 2026-10-30): a person reached by a pickup / callback is a right-party
    # contact when that number is Correct AND primary, a secondary contact when Correct but
    # not primary. Status and primary are read now, right before the post.
    by_rec = defaultdict(set)
    for uuid, num in res.get("rpc_cands", set()):
        by_rec[uuid].add(num)
    call_rpc, call_sec = set(), set()
    for uuid, nums in by_rec.items():
        try:
            rec = req(token, f"/api/internal/property/{uuid}/")
        except Exception as e:
            log(f"could not read a reached record ({e}); left out of contacts")
            continue
        roles = {sms_kpis.contact_role(rec, res.get("rec_logs", {}).get(uuid, []), n) for n in nums}
        if "rpc" in roles:
            call_rpc.add(sms_kpis.rec_hash(uuid))
        elif "secondary" in roles:
            call_sec.add(sms_kpis.rec_hash(uuid))
    t = texts or {}
    text_rpc = set(t.get("text_rpc_records") or [])
    text_sec = set(t.get("text_secondary_records") or [])
    rpc = call_rpc | text_rpc
    secondary = (call_sec | text_sec) - rpc        # a record with a right-party contact counts there
    dialed_h = {sms_kpis.rec_hash(u) for u in acct["records"]}
    status_h = {sms_kpis.rec_hash(u) for u in res.get("status_contacts", set())}
    # records worked = dialed, texted, or reached / given an outcome in the window
    texted_h = set(t.get("texted_records") or [])
    worked = dialed_h | texted_h | status_h | rpc | secondary
    res["contact"] = {
        "rpc": len(rpc), "call": len(call_rpc), "text_only": len(text_rpc - call_rpc),
        "secondary": len(secondary), "worked": len(worked), "texts_known": texts is not None,
        "status_contacts": len(status_h),
        # records we reached out to (Jeff, 2026-10-30: the first line of the post)
        "outreach": len(dialed_h | texted_h), "called": len(dialed_h),
        "texted_only": len(texted_h - dialed_h)}
    res["_ledger_day"] = {"worked": sorted(worked), "reached": sorted(rpc | status_h)}


def update_ledger(path: Path, res: dict) -> dict:
    """Records reached over time: every record worked (dialed or texted) since the ledger
    began, and which of them we have ever reached. Hashed ids only (GitHub keeps this file
    in its cache between runs)."""
    led = {"since": res["from"], "worked": [], "reached": []}
    if path.exists():
        try:
            led = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            log("ledger unreadable; starting a new one")
    day = res.get("_ledger_day") or {}
    led["since"] = min(led.get("since") or res["from"], res["from"])
    led["worked"] = sorted(set(led.get("worked", [])) | set(day.get("worked", [])))
    led["reached"] = sorted(set(led.get("reached", [])) | set(day.get("reached", [])))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(led), encoding="utf-8")
    return led


LEAD_ORDER = [("new lead", "New"), ("new_lead", "New"), ("no contact new lead", "No Contact New"),
              ("nurture new lead", "Nurture"), ("lead", "Lead"), ("cold lead", "Cold"),
              ("warm lead", "Warm"), ("hot lead", "Hot"), ("ghosting lead", "Ghosting")]


def _goal(n: float, lo: float, hi: float | None = None) -> str:
    """'goal 5 ✅' / 'goal 2-3 ⚠️': met when n reaches the low end."""
    rng = f"{lo:g}" if hi is None else f"{lo:g}-{hi:g}"
    return f"goal {rng} {'✅' if n >= lo else '⚠️'}"


def slack_lines(res: dict, bench: dict, texts: tuple, note: str = "", ledger: dict | None = None) -> list[str]:
    """The post: every number with what it means in brackets (Jeff, 2026-10-30)."""
    a = res["account_totals"]
    dials = a["dials"]
    weekly = res["from"] != res["to"]
    period = res["from"] if not weekly else f"week {res['from']} to {res['to']}"
    # caller-days: goals scale with how many people dialed on how many days
    cdays = sum(len(c["days"]) for c in res["callers"].values()) or 1
    lines = [f"*📞 Prospecting KPIs*  |  {period}" + (f"  |  _{note}_" if note else "")]
    ct0 = res.get("contact") or {}
    if ct0:
        lines.append(f"*Records reached out to: {ct0['outreach']}* _(records we called or texted "
                     f"{'this week' if weekly else 'today'})_" + ("" if ct0["texts_known"] else "  · _texts not available_"))
    lines += ["", "*CALLS*",
             f"• Dials: *{dials}* _(outbound calls placed)_",
             f"• Dial hours: *{fmt_hms(a['dial_seconds'])}* _(first call to last call, per caller per day)_",
             f"• Voicemail / no answer: *{a['voicemails']}* _(nobody live: dispositioned no answer, busy or dead, "
             f"or a same-day \"VM / no answer\" note on the record)_",
             f"• Reached: *{a['reached']}* _(ANYONE live picked up: owner, relative, wrong number or a callback; "
             f"the voicemails above are already removed)_",
             f"    owner {a['answered']} · callback {a['inbound_owner_talks']} · someone else / wrong # {a['non_owner_talks']}",
             f"    Call length, live pickups only: under 30s {a['len_lt30']} · 30-60s {a['len_30_60']} · "
             f"1-2 min {a['len_60_120']} · 2 min+ {a['len_120p']}",
             f"• Conversations: *{a['conversations']}* _(a live person for 1 min+, or a talk note / outcome "
             f"on the record that day)_  ·  {_goal(a['conversations'], 5 * cdays)}",
             f"• Talk time: *{fmt_hms(a['talk_seconds'])}* _(total time on live calls)_"]
    if "sp_outbound" in res:
        lines.append(f"• Missed callbacks: *{a['missed_callbacks']}* _(people who called us and nobody "
                     f"answered, one per number)_  ·  {a['missed_not_returned']} not called back yet")
    dpc = dials / a["correct_numbers"] if a["correct_numbers"] else None
    when = "this week" if weekly else "today"
    lines += ["", "*NUMBERS*",
              f"• Correct, all: *{a['correct_numbers']}* _(numbers newly marked Correct {when}; includes ones "
              f"confirmed by a voicemail greeting; a number marked earlier is never counted again)_",
              f"• Correct, live pickup: *{a['correct_live']}* _(of those, a real person picked up that number "
              f"the same day it was marked)_",
              f"• Dials per Correct: *{f'{dpc:.1f}' if dpc else 'n/a'}* _(dials ÷ Correct, all; lower is better)_  ·  "
              f"goal 8-10 {'✅' if dpc and dpc <= 10 else '⚠️'}",
              f"• Wrong {a['wrong_numbers']} · Dead {a['dead_numbers']} · DNC {a['dnc_numbers']} _(numbers newly marked {when})_"]
    ct = res.get("contact") or {}
    if ct:
        span = "this week" if weekly else "today"
        lines += ["", f"*CONTACT: {'this week (Mon-Fri)' if weekly else 'today only'}*",
                  f"• Right-party contacts: *{ct['rpc']}* _(people reached {span} on a number marked Correct AND "
                  f"primary; primary checked right before this post)_",
                  f"    Call: *{ct['call']}* _(reached by a pickup or callback; includes anyone who also texted)_",
                  f"    Text only: *{ct['text_only']}* _(reached by text reply only, no call)_"
                  + ("" if ct["texts_known"] else "  · _texts not available_"),
                  f"• Secondary contacts: *{ct['secondary']}* _(people reached {span} on a number marked Correct but "
                  f"NOT primary: relatives or secondary decision makers; not in the contact rate)_",
                  f"• Contact rate: *{pct(ct['rpc'], ct['outreach'])}* _(right-party contacts ÷ records reached "
                  f"out to {span}: {ct['rpc']} of {ct['outreach']})_",
                  f"• Contact check: *{pct(ct['status_contacts'], ct['outreach'])}* _(records set to a lead status or "
                  f"Not Interested {span} ÷ records reached out to {span}: {ct['status_contacts']} of {ct['outreach']})_"]
        if ledger and weekly:
            w, r = set(ledger["worked"]), set(ledger["reached"]) & set(ledger["worked"])
            lines.append(f"• Records reached over time: *{pct(len(r), len(w))}* _(of every record dialed or texted "
                         f"since {ledger['since']}, how many we have ever reached: {len(r)} of {len(w)})_")
    lo, hi = bench["leads_per_caller_day"]
    lbs = a["leads_by_status"]
    labels = []
    for _, label in LEAD_ORDER:
        if label not in labels:
            labels.append(label)
    parts = [f"{label} {sum(lbs.get(k, 0) for k, lab in LEAD_ORDER if lab == label)}" for label in labels]
    lines += ["", "*LEADS*",
              f"• Leads: *{a['leads']}* _(records newly set to New, No Contact New, Nurture, Lead, Cold, Warm, "
              f"Hot or Ghosting)_  ·  {_goal(a['leads'], lo * cdays, hi * cdays)}",
              "    " + " · ".join(parts),
              f"• Not interested: *{a['not_interested']}* _(not a lead)_  ·  Follow-up tasks: {a['follow_ups']}"]
    sp_out = res.get("sp_outbound")
    if sp_out is not None and dials and abs(sp_out - dials) > max(5, 0.05 * dials):
        lines.append(f"• ⚠️ Check: smrtPhone shows {sp_out} outbound calls vs {dials} dials logged in DataSift")
    if missed_line(res):
        lines.append(f"• {'⚠️ ' if res['sp_missed'] else ''}{missed_line(res)}")
    lines.append(f"_Calls & talk time: {res.get('call_source', 'DataSift only')}. Correct numbers: as marked in DataSift._")
    callers = [(e, c) for e, c in sorted(res["callers"].items(), key=lambda kv: -kv[1]["dials"])
               if any(c[k] for k in ("dials", "correct_numbers", "leads", "not_interested"))]
    if callers:
        lines += ["", "*BY CALLER*"]
        for email, c in callers:
            if not c["dials"]:       # marked numbers / set statuses but didn't dial: no dial goals
                lines.append(f"• {res['names'].get(email, email)}: no dials · {c['correct_numbers']} correct "
                             f"marked · {c['leads']} leads")
                continue
            nd = len(c["days"]) or 1
            floor = bench["dials_floor_per_caller"] * nd
            lines.append(f"• {res['names'].get(email, email)}: {c['dials']} dials "
                         f"({'✅' if c['dials'] >= floor else '⚠️'} floor {floor}) · "
                         f"{c['conversations']} convos ({'✅' if c['conversations'] >= 5 * nd else '⚠️'} goal {5 * nd}) · "
                         f"{c['correct_numbers']} correct · {c['leads']} leads · {fmt_hms(c['dial_seconds'])} dialing")
    # Texts: their own section, separate from the call numbers (Jeff, 2026-10-16)
    lines += ["", f"*💬 TEXTS*  |  {period}"]
    lines.extend(sms_kpis.render_lines(texts[0], texts[1]))
    return lines


def post_slack(res: dict, webhook: str, bench: dict, note: str = "", texts: tuple = (None, ""),
               ledger: dict | None = None) -> None:
    lines = slack_lines(res, bench, texts, note, ledger)
    if webhook == "print":           # --slack print: show the post, send nothing
        print("\n".join(lines))
        return
    r = urllib.request.Request(webhook, data=json.dumps({"text": "\n".join(lines)}).encode(),
                               headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=15):
        pass
    log("Slack digest posted")


def main() -> int:
    ap = argparse.ArgumentParser(description="DataSift KPI puller (kpi-engine skill)")
    ap.add_argument("--from", dest="day_from")
    ap.add_argument("--to", dest="day_to")
    ap.add_argument("--days", type=int, help="trailing N days ending today")
    ap.add_argument("--tz", default="America/Chicago")
    ap.add_argument("--xlsx", action="store_true")
    ap.add_argument("--detail", action="store_true",
                    help="also write a record-level detail CSV (one row per worked record)")
    ap.add_argument("--slack", metavar="WEBHOOK_URL")
    ap.add_argument("--show-benchmarks", action="store_true")
    ap.add_argument("--note", default="", help="short note added to the Slack header")
    ap.add_argument("--probe", action="store_true", help="write reports/probe.json with raw event samples")
    ap.add_argument("--ledger", metavar="PATH",
                    help="records-reached-over-time ledger to update (and show on a weekly post)")
    ap.add_argument("--week-of", metavar="DATE",
                    help="the Monday-to-DATE week (the Friday weekly summary)")
    args = ap.parse_args()

    bench = load_benchmarks()
    if args.show_benchmarks:
        print(json.dumps(bench, indent=2))
        return 0
    tz = ZoneInfo(args.tz)
    today = datetime.datetime.now(tz).date()
    if args.days:
        day_from = (today - datetime.timedelta(days=args.days - 1)).isoformat()
        day_to = today.isoformat()
    elif args.week_of:
        d = datetime.date.fromisoformat(args.week_of)
        day_from = (d - datetime.timedelta(days=d.weekday())).isoformat()
        day_to = d.isoformat()
    elif args.day_from:
        day_from, day_to = args.day_from, args.day_to or today.isoformat()
    else:
        day_from = day_to = today.isoformat()

    PROBE["on"] = args.probe
    token = get_token()
    res = pull(token, day_from, day_to, tz, bench)
    add_smrtphone(res, day_from, day_to, bench)
    texts = text_stats(day_from, day_to)
    finalize(res, token, texts[0])
    ledger = update_ledger(Path(args.ledger), res) if args.ledger else None
    md = render_md(res, bench)
    print("\n" + md)
    out_dir = HERE / "reports"
    out_dir.mkdir(exist_ok=True)
    stem = f"kpi_{day_from}_{day_to}"
    (out_dir / f"{stem}.md").write_text(md, encoding="utf-8")
    write_csv(res, out_dir / f"{stem}.csv")
    log(f"wrote {stem}.md and {stem}.csv")
    if args.detail:
        with open(out_dir / f"{stem}_records.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["address", "current_status", "dials", "answered", "talk",
                        "callers", "status_set_to", "status_set_by", "is_lead", "last_activity"])
            for d in res["records_detail"]:
                w.writerow([d["address"], d["status"], d["dials"], d["answered"],
                            fmt_hms(d["talk_seconds"]), ", ".join(sorted(d["callers"])),
                            d["status_set_to"], d["status_set_by"],
                            "YES" if d["is_lead"] else "", d["last_activity"]])
        log(f"wrote {stem}_records.csv ({len(res['records_detail'])} records)")
    if args.xlsx and write_xlsx(res, bench, out_dir / f"{stem}.xlsx"):
        log(f"wrote {stem}.xlsx")
    if args.slack:
        webhook = args.slack
        if webhook == "auto":  # reuse the sheet bot's webhook (same Slack channel)
            webhook = (os.environ.get("KPI_SLACK_WEBHOOK_URL", "").strip()
                       or _read_env_value(HERE.parent / "kpi-bot" / ".env", "KPI_SLACK_WEBHOOK_URL"))
            if not webhook:
                sys.exit("--slack auto: no KPI_SLACK_WEBHOOK_URL found in ../kpi-bot/.env")
        post_slack(res, webhook, bench, args.note, texts, ledger)
    return 0


if __name__ == "__main__":
    sys.exit(main())
