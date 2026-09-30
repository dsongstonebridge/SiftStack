"""fill_sheet.py - fill the "Tulsa Homebuyers KPIs" Google Sheet from DataSift (2026-09-30).

Fills three tabs, one row per caller per day (Leads Summary: one row per lead):
  First to Market  - calls on records NOT in a lead status at call time (prospecting)
  Lead Management  - calls on records in a lead status at call time, plus lead moves
  Leads Summary    - every record that became a lead, with its milestone dates

Rules (Jeff):
  - Qualified        = status moved to Cold Lead, Warm Lead or Hot Lead
  - Sent to AQS      = a task created in the "Acquisitions" task group
  - Live / voicemail = same rules as the KPI engine (number marked Correct, no same-day
                       "no answer / VM" note; inbound follows the same rule)
  - Hours            = first to last outbound call of the day, per caller
Only the columns listed here are written. Anything typed into other columns is kept.
Re-running a day overwrites that day's rows with fresh numbers (safe to repeat).

USAGE  py -3 fill_sheet.py               # today and yesterday
       py -3 fill_sheet.py --since 2026-09-28
       py -3 fill_sheet.py --dry-run     # print, write nothing
"""
from __future__ import annotations
import argparse, datetime, json, re
from collections import defaultdict
import pull_kpis as k

TZ = k.ZoneInfo("America/Chicago")
LEAD = {"new_lead", "new lead", "no contact new lead", "nurture new lead", "cold lead",
        "warm lead", "hot lead", "follow_up", "lead"}
QUALIFIED = {"cold lead", "warm lead", "hot lead"}
NOT_INT, GHOST, LOST, DEAD_LEAD = {"not_interested"}, {"ghosting lead"}, {"lost_deal"}, {"dead lead"}
EXHAUSTED = {"incorrect number(s)", "deep prospecting", "more deep prospecting needed"}
DEAD_PH, DNC_PH = {"DEAD"}, {"DNC", "CORRECT_DNC", "WRONG_DNC"}

def low(x): return (x or "").strip().lower()
def ts_local(e): return k.local_dt(e.get("timestamp", ""), TZ)

def status_timeline(evs, s_idx, current):
    """[(dt, status)] ascending, with the status the record had before its first change."""
    ch = []
    for e in evs:
        if e.get("event_type") == "property.status.updated":
            pair = ((e.get("payload") or {}).get("property") or {}).get("status")
            if isinstance(pair, list) and len(pair) == 2:
                ch.append((ts_local(e), pair[1 - s_idx], pair[s_idx], e))
    ch = [c for c in ch if c[0]]
    ch.sort(key=lambda c: c[0])
    start = ch[0][1] if ch else current
    return start, ch

def status_at(start, ch, when):
    st = start
    for dt, _old, new, _e in ch:
        if dt <= when: st = new
        else: break
    return st

GROUP_BY_UUID: dict[str, str] = {}
GROUP_BY_TITLE: dict[str, str] = {}

def load_task_groups(tok):
    """Walk every task group and remember each uuid inside it (group, templates,
    event types) -> group title, so a task's reference can be traced to its group."""
    body = k.req(tok, "/api/internal/task-group/", params={"limit": 1000, "offset": 0})
    groups = body if isinstance(body, list) else (body.get("results") or body.get("data") or [])
    def walk(obj, title):
        if isinstance(obj, dict):
            for key, v in obj.items():
                if key in ("uuid", "id") and isinstance(v, (str, int)):
                    GROUP_BY_UUID[str(v)] = title
                if key in ("title", "name") and isinstance(v, str) and v.strip() and v != title:
                    GROUP_BY_TITLE.setdefault(v.strip().lower(), title)
                walk(v, title)
        elif isinstance(obj, list):
            for x in obj: walk(x, title)
    for g in groups:
        gt = str(g.get("title") or g.get("name") or "")
        walk(g, gt)
        gid = g.get("uuid")
        if not gid: continue
        try:   # the task names in a group are its "task presets"
            pb = k.req(tok, f"/api/internal/task-group/{gid}/task-preset/", params={"limit": 999, "offset": 0})
            presets = pb if isinstance(pb, list) else (pb.get("results") or pb.get("data") or [])
            for pr in presets:
                t = str(pr.get("title") or pr.get("name") or "").strip().lower()
                if t: GROUP_BY_TITLE.setdefault(t, gt)
        except Exception as ex:
            k.log(f"task presets for {gt}: {ex}")

def task_group(e):
    t = ((e.get("payload") or {}).get("task") or {})
    g = t.get("group") or t.get("task_group")
    if isinstance(g, dict): g = g.get("title") or g.get("name")
    if g: return str(g), str(t.get("title") or "")
    def refs(obj):
        if isinstance(obj, dict):
            for v in obj.values(): yield from refs(v)
        elif isinstance(obj, list):
            for v in obj: yield from refs(v)
        elif isinstance(obj, (str, int)):
            yield str(obj)
    for ref in refs(t):
        if ref in GROUP_BY_UUID: return GROUP_BY_UUID[ref], str(t.get("title") or "")
    title = str(t.get("title") or "")
    return GROUP_BY_TITLE.get(title.strip().lower(), ""), title

def build(days: list[str]):
    tok = k.get_token()
    bench = k.load_benchmarks()
    try:
        load_task_groups(tok)
        k.log(f"task groups loaded: " + json.dumps({g: sorted(t for t, gg in GROUP_BY_TITLE.items() if gg == g)[:12]
                                                     for g in sorted(set(GROUP_BY_TITLE.values()))}))
    except Exception as ex:
        k.log(f"task groups not loaded: {ex}")
    first = min(days)
    today_excl = (datetime.datetime.now(TZ).date() + datetime.timedelta(days=1)).isoformat()
    cand = k.search_updated(tok, first, today_excl)
    k.log(f"{len(cand)} records touched since {first}; reading histories...")
    recs = {}
    for r in cand:
        try: recs[r["uuid"]] = (r, k.get_logs(tok, r["uuid"]))
        except Exception: pass
    allph = [e for _, evs in recs.values() for e in evs if e.get("event_type") == "owner.phone.status.updated"]
    allst = [e for _, evs in recs.values() for e in evs if e.get("event_type") == "property.status.updated"]
    p_idx, s_idx = k.detect_new_index(allph, "phone"), k.detect_new_index(allst, "property")
    conv_s, mean_s = bench["conversation_min_seconds"], bench["meaningful_conversation_min_seconds"]

    ftm = defaultdict(lambda: defaultdict(float))   # (name, day) -> col -> value
    lm = defaultdict(lambda: defaultdict(float))
    acq = defaultdict(lambda: defaultdict(float))    # Acquisition tab, from Acquisitions tasks
    aqs_sent = set()                                 # (uuid, day) counted once
    span = defaultdict(list)                         # (name, day) -> [dt of outbound calls]
    leads = {}                                       # uuid -> summary row
    names = {}
    shape_logged = False

    for uuid, (r, evs) in recs.items():
        evs_t = sorted([(ts_local(e), e) for e in evs if ts_local(e)], key=lambda x: x[0])
        start, ch = status_timeline(evs, s_idx, k._flat_status(r.get("status")))
        # phone status per number over time -> Correct set per day, and record exhausted
        ph_last = {}
        correct_ever = set()
        for dt, e in evs_t:
            if e.get("event_type") == "owner.phone.status.updated":
                ph = k._digits10(((e.get("payload") or {}).get("owner") or {}).get("phone"))
                st = str(k.new_status(e, "phone", p_idx) or "").upper()
                if ph and st:
                    ph_last[ph] = st
                    if st in k.CORRECT_STATES: correct_ever.add(ph)
        # per-day voicemail / talk notes (same rules as the engine)
        vm_days, talk_days = set(), set()
        for dt, e in evs_t:
            if str(e.get("event_type", "")).startswith("property.message") and k._human_author(e):
                txt, key = k._message_text(e), dt.date().isoformat()
                if k._is_vm_note(txt): vm_days.add(key)
                elif k._TALK_NOTE.search(txt): talk_days.add(key)
        vm_days -= talk_days
        # final status per phone per day (so a status changed twice counts once)
        last_ph_status_of_day, counted_ph = {}, set()
        for dt, e in evs_t:
            if e.get("event_type") == "owner.phone.status.updated":
                ph = k._digits10(((e.get("payload") or {}).get("owner") or {}).get("phone"))
                st = str(k.new_status(e, "phone", p_idx) or "").upper()
                if ph and st:
                    last_ph_status_of_day[(ph, dt.date().isoformat())] = (dt, st)
        # prior outbound dial count for attempt staging
        dials_before = 0
        last_stage_for_phone = {}
        rec_days_new, rec_days_fu = set(), set()
        seen = set()
        for dt, e in evs_t:
            et, day = e.get("event_type", ""), dt.date().isoformat()
            call = (e.get("payload") or {}).get("call") or {}
            if et.startswith("owner.call"):
                key = (et, call.get("uuid") or call.get("external_id"))
                if key in seen: continue
                seen.add(key)
            st_now = low(status_at(start, ch, dt))
            is_lead = st_now in LEAD
            if et == "owner.call.made":
                stage = min(dials_before, 3)
                dials_before += 1
                last_stage_for_phone[call_number(call)] = stage
            if day not in days: 
                continue
            email, name = k.caller_of(e) if et.startswith("owner.call") else k.author_of(e)
            if email == "system": name = "Automation"
            if et.startswith("owner.call"):
                names[email] = name
            who = names.get(email, name) or "Unknown"
            if et.startswith("owner.call") and email == "inbound":
                who = "Inbound"
            tab = lm if is_lead else ftm
            row = tab[(who, day)]
            if et == "owner.call.made":
                span[(who, day)].append(dt)
                stage = last_stage_for_phone.get(call_number(call), 0)
                if is_lead:
                    row["Dials Made"] += 1
                    if st_now in QUALIFIED: row["Qualified Lead Followups"] += 1
                else:
                    row[["Initial Dial", "FU 1 Dial", "FU2 Dial", "FU 3 Dial"][stage]] += 1
                    (rec_days_new if stage == 0 else rec_days_fu).add((who, day))
            elif et == "owner.call.answered":
                live = call_number(call) in correct_ever and day not in vm_days
                inbound = call.get("direction") == "inbound"
                if inbound:
                    lm[("Inbound" if who == "Inbound" else who, day)]["Inbound calls"] += 1
                if not live:
                    if not inbound:
                        row["Voicemail" if not is_lead else "Voicemails"] += 1
                else:
                    if is_lead or inbound:
                        lm[(who, day)]["Conversations"] += 1
            elif et in ("owner.call.noanswer", "owner.call.busy"):
                row["No Answer"] += 1
            elif et == "owner.sms.sent":
                row["SMS" if not is_lead else "SMS Sent"] += 1
            elif et == "owner.sms.received":
                (ftm if not is_lead else lm)[(who if who != "Unknown" else "Inbound", day)]["SMS Reply" if not is_lead else "SMS Inbound"] += 1
            elif et == "owner.phone.status.updated":
                st = str(k.new_status(e, "phone", p_idx) or "").upper()
                ph = k._digits10(((e.get("payload") or {}).get("owner") or {}).get("phone"))
                if (ph, day) in counted_ph or last_ph_status_of_day.get((ph, day)) != (dt, st):
                    continue   # only the number's final status that day counts, once
                counted_ph.add((ph, day))
                if is_lead:
                    if st in DEAD_PH: row["Dead #"] += 1
                    elif st in DNC_PH: row["DNC #"] += 1
                else:
                    if st in DEAD_PH: row["Dead"] += 1
                    elif st in DNC_PH: row["DNC"] += 1
                    elif st in k.WRONG_STATES: row["Wrong"] += 1
                    elif st in k.CORRECT_STATES:
                        stg = last_stage_for_phone.get(ph, 0)
                        row[["Correct Initial", "Correct F/U 1", "Correct F/U 2", "Correct F/U 3"][stg]] += 1
            elif et == "property.status.updated":
                pair = ((e.get("payload") or {}).get("property") or {}).get("status")
                if not (isinstance(pair, list) and len(pair) == 2): continue
                old, new = low(pair[1 - s_idx]), low(pair[s_idx])
                was_lead = old in LEAD
                if new in LEAD and not was_lead:
                    ftm[(who, day)]["New Lead"] += 1
                    lm[(who, day)]["New Leads"] += 1
                if new in QUALIFIED and old not in QUALIFIED:
                    lm[(who, day)]["New Leads Qualified"] += 1
                if new in NOT_INT: (lm if was_lead else ftm)[(who, day)]["Not interested"] += 1
                if new in GHOST: lm[(who, day)]["Ghosting Leads"] += 1
                if new in LOST: lm[(who, day)]["Lost Deal"] += 1
                if new in DEAD_LEAD: lm[(who, day)]["Dead Leads"] += 1
                if new in EXHAUSTED and old not in EXHAUSTED: ftm[(who, day)]["Full Exhausted"] += 1
            elif et == "task.created":
                grp, title = task_group(e)
                if not shape_logged:
                    k.log("sample task: " + json.dumps((e.get('payload') or {}).get('task'), default=str)[:600]
                          + f" -> group '{grp}'")
                    shape_logged = True
                if low(grp) == "acquisitions":
                    tl = low(title)
                    t = (e.get("payload") or {}).get("task") or {}
                    au = t.get("assigned_to_user") or {}
                    assignee = f"{au.get('first_name', '')} {au.get('last_name', '')}".strip() or who
                    if "send back" in tl:
                        acq[(assignee, day)]["Send back to LM"] += 1
                    else:
                        if (uuid, day) not in aqs_sent:          # one handoff per lead per day
                            aqs_sent.add((uuid, day))
                            lm[(who, day)]["Sent to AQS"] += 1
                        if "follow" in tl:
                            acq[(assignee, day)]["Offer Follow-ups "] += 1
                        else:
                            acq[(assignee, day)]["Initial Offer Leads"] += 1
        for pair in rec_days_new: ftm[pair]["New Ps #"] += 1
        for pair in rec_days_fu: ftm[pair]["Follow Up  Ps #"] += 1

        # Leads Summary: any record that ever entered a lead status
        first_lead = next((dt for dt, old, new, _ in ch if low(new) in LEAD and low(old) not in LEAD), None)
        if first_lead or low(start) in LEAD:
            addr = r.get("address") or {}
            def first_to(targets):
                d = next((dt for dt, _o, n, _e in ch if low(n) in targets), None)
                return d.date().isoformat() if d else ""
            created = next((dt for dt, e in evs_t if e.get("event_type") == "property.created"), None)
            cur = low(k._flat_status(r.get("status")))
            leads[uuid] = {
                "Date New Lead": first_lead.date().isoformat() if first_lead else "",
                "Address": addr.get("street", "") if isinstance(addr, dict) else str(addr),
                "Zip Code": (addr.get("zip5") or addr.get("postal_code") or "") if isinstance(addr, dict) else "",
                "List/Problem": ", ".join(sorted(str(l.get("title") if isinstance(l, dict) else l) for l in (r.get("lists") or []) if l))[:200],
                "Lead Tempature": cur.title() if cur in QUALIFIED else "",
                "Date Added Sift": created.date().isoformat() if created else "",
                "Date Qualified": first_to(QUALIFIED),
                "Date Not Interested": first_to(NOT_INT),
                "Date Dead Lead": first_to(DEAD_LEAD),
                "Date Ghosting Lead": first_to(GHOST),
                "Date Lost Deal": first_to(LOST),
            }
    for key, dts in span.items():
        hrs = round((max(dts) - min(dts)).total_seconds() / 3600, 2) if len(dts) > 1 else 0
        (ftm if key in ftm else lm)[key]["Hours"] = hrs
    return ftm, lm, leads, acq

def call_number(call): return k.call_number(call)

# ---------------- sheet writing ----------------
def open_sheet(readonly=False):
    import gspread
    from google.oauth2.service_account import Credentials
    bot = k.HERE.parent / "kpi-bot"
    sid = k._read_env_value(bot / ".env", "KPI_SPREADSHEET_ID")
    scope = "https://www.googleapis.com/auth/spreadsheets" + (".readonly" if readonly else "")
    creds = Credentials.from_service_account_file(str(bot / "service_account.json"), scopes=[scope])
    return gspread.authorize(creds).open_by_key(sid)

def norm_key(v):
    """Sheets shows 2026-09-28 as 9/28/2026 once stored as a date; compare as ISO."""
    v = str(v).strip()
    for fmtx in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%Y/%m/%d"):
        try: return datetime.datetime.strptime(v, fmtx).date().isoformat()
        except ValueError: pass
    return v.lower()

def upsert(ws, data: dict, key_cols: list[str], dry: bool):
    """data: {key_tuple: {col: value}} ; writes only those columns, matching rows by key."""
    import gspread
    vals = ws.get_all_values()
    header = [h for h in vals[0]]
    col = {h: i for i, h in enumerate(header) if h}
    keyidx = [col[c] for c in key_cols]
    rows, dupes = {}, []
    for n, v in enumerate(vals):
        if n == 0 or not any(v): continue
        kk = tuple(norm_key(v[i] if i < len(v) else "") for i in keyidx)
        if kk in rows: dupes.append(n + 1)     # same caller + date twice: keep the first
        else: rows[kk] = n + 1
    next_row = max([n + 1 for n, v in enumerate(vals) if any(v)] + [1]) + 1
    updates, added, changed = [], 0, 0
    for key, cols in sorted(data.items()):
        rn = rows.get(tuple(norm_key(x) for x in key))
        if rn is None:
            rn = next_row; next_row += 1; added += 1
        else:
            changed += 1
        full = dict(zip(key_cols, key)); full.update(cols)
        for c, v in full.items():
            if c in col:
                if isinstance(v, float) and v.is_integer(): v = int(v)
                updates.append({"range": gspread.utils.rowcol_to_a1(rn, col[c] + 1), "values": [[v]]})
    k.log(f"{ws.title}: {added} new row(s), {changed} updated")
    if not dry and updates:
        if ws.row_count < next_row: ws.add_rows(next_row - ws.row_count + 50)
        ws.batch_update(updates, value_input_option="USER_ENTERED")
    if dupes:
        k.log(f"{ws.title}: removing {len(dupes)} duplicate row(s)")
        if not dry:
            for rn in sorted(dupes, reverse=True):
                ws.delete_rows(rn)

def delete_empty_rows(ws, key_cols, dry):
    """Remove rows whose only content is the key (name + date): no data, just clutter."""
    vals = ws.get_all_values()
    header = vals[0]
    keyidx = {header.index(c) for c in key_cols if c in header}
    empty = [n + 1 for n, v in enumerate(vals) if n > 0 and any(v)
             and not any(x.strip() for i, x in enumerate(v) if i not in keyidx)]
    if empty:
        k.log(f"{ws.title}: removing {len(empty)} empty row(s)")
        if not dry:
            for rn in sorted(empty, reverse=True):
                ws.delete_rows(rn)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since"); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    today = datetime.datetime.now(TZ).date()
    start = datetime.date.fromisoformat(a.since) if a.since else today - datetime.timedelta(days=1)
    days = [(start + datetime.timedelta(days=i)).isoformat() for i in range((today - start).days + 1)]
    ftm, lm, leads, acq = build(days)
    FTM_COLS = ["Hours", "New Ps #", "Follow Up  Ps #", "Initial Dial", "FU 1 Dial", "FU2 Dial", "FU 3 Dial",
                "SMS", "SMS Reply", "Dead", "DNC", "No Answer", "Voicemail", "Wrong", "Correct Initial",
                "Correct F/U 1", "Correct F/U 2", "Correct F/U 3", "New Lead", "Not interested", "Full Exhausted"]
    LM_COLS = ["Hours", "New Leads", "New Leads Qualified", "Qualified Lead Followups", "Inbound calls",
               "Dials Made", "SMS Sent", "Voicemails", "SMS Inbound", "Dead #", "DNC #", "No Answer",
               "Conversations", "Not interested", "Ghosting Leads", "Lost Deal", "Dead Leads", "Sent to AQS"]
    def fmt(tab, cols):   # every owned column written (0 where none), empty rows dropped
        return {(n, d): {c: v.get(c, 0) for c in cols} for (n, d), v in tab.items()
                if n not in ("Unknown",) and any(float(x) for x in v.values())}
    f, l = fmt(ftm, FTM_COLS), fmt(lm, LM_COLS)
    ACQ_COLS = ["Initial Offer Leads", "Offer Follow-ups ", "Send back to LM"]
    q = fmt(acq, ACQ_COLS)
    for name, t in (("First to Market", f), ("Lead Management", l)):
        for (n, d), v in sorted(t.items()):
            k.log(f"{name} | {d} | {n} | " + ", ".join(f"{c}={int(x) if float(x).is_integer() else x}" for c, x in sorted(v.items()) if x))
    k.log(f"Leads Summary: {len(leads)} lead record(s)")
    sh = open_sheet(readonly=a.dry_run)
    for tab, data, keys in (("First to Market", f, ["Ninja Name ", "Date"]), ("Lead Management", l, ["Name", "Date"]),
                            ("Acquisition", q, ["Name", "Date"])):
        ws = sh.worksheet(tab)
        upsert(ws, data, keys, a.dry_run)
        delete_empty_rows(ws, keys, a.dry_run)
    upsert(sh.worksheet("Leads Summary"), {(v["Address"],): {c: x for c, x in v.items() if c != "Address"} for v in leads.values() if v["Address"]}, ["Address"], a.dry_run)
    k.log("sheet fill done" + (" (dry run - nothing written)" if a.dry_run else ""))

if __name__ == "__main__":
    main()
