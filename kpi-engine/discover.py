"""discover.py - READ-ONLY survey for the sheet auto-fill (2026-09-30).

Writes reports/discovery.json with:
  - DataSift: statuses, custom fields (+groups), task groups, lists, tags (names only)
  - DataSift activity over the last N days: event-type counts, property status
    transitions (from -> to), task titles, custom-field labels being written
  - smrtPhone: whether the API key can read the call log (tests a few routes)
  - Google Sheet: tab names + header rows, and whether the service account can edit
Nothing is created, changed or deleted anywhere.
"""
from __future__ import annotations
import json, sys, datetime, urllib.request, urllib.parse, urllib.error
from collections import Counter
from pathlib import Path
import pull_kpis as k

HERE = Path(__file__).resolve().parent
OUT = HERE / "reports" / "discovery.json"
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
res = {"generated": datetime.datetime.now().isoformat(), "days": DAYS}

def safe(name, fn):
    try:
        res[name] = fn()
    except SystemExit as e:
        res[name] = {"error": str(e)}
    except Exception as e:
        res[name] = {"error": f"{type(e).__name__}: {e}"}
    k.log(f"{name}: done")

tok = k.get_token()
def items(body):
    if isinstance(body, list): return body
    return body.get("results") or body.get("data") or []

def get_all(path, limit=1000):
    return items(k.req(tok, path, params={"limit": limit, "offset": 0}))

safe("statuses", lambda: [{"title": s.get("title") or s.get("name"), "type": s.get("type") or s.get("status_type")} for s in get_all("/api/internal/status/")])
safe("custom_field_groups", lambda: [g.get("label") or g.get("title") for g in get_all("/api/internal/custom-fields/group/")])
safe("custom_fields", lambda: [{"label": f.get("label"), "type": f.get("field_type"),
      "group": (f.get("group") or {}).get("label") if isinstance(f.get("group"), dict) else f.get("group"),
      "options": [o.get("label") or o.get("title") or o.get("value") for o in (f.get("options") or []) if isinstance(o, dict)][:15]}
      for f in get_all("/api/internal/custom-fields/")])
safe("task_groups", lambda: [t.get("title") or t.get("name") for t in get_all("/api/internal/task-group/")])
safe("lists", lambda: [l.get("title") or l.get("name") for l in get_all("/api/internal/list/")][:200])
safe("tags_count", lambda: len(get_all("/api/internal/tag/")))

def activity():
    tz = k.ZoneInfo("America/Chicago")
    today = datetime.datetime.now(tz).date()
    frm = (today - datetime.timedelta(days=DAYS - 1)).isoformat()
    to_excl = (today + datetime.timedelta(days=1)).isoformat()
    cand = k.search_updated(tok, frm, to_excl)
    types, trans, tasks, cf = Counter(), Counter(), Counter(), Counter()
    for r in cand[:400]:
        try:
            evs = k.get_logs(tok, r["uuid"])
        except Exception:
            continue
        for e in evs:
            et = e.get("event_type", "")
            types[et] += 1
            pl = e.get("payload") or {}
            if et == "property.status.updated":
                st = (pl.get("property") or {}).get("status")
                if isinstance(st, list) and len(st) == 2:
                    trans[f"{st[0]} -> {st[1]}"] += 1
            elif et.startswith("task."):
                t = (pl.get("task") or {}).get("title")
                if t: tasks[f"{et}: {t}"] += 1
            elif et.startswith("property.customfieldvalue"):
                cfd = pl.get("custom_field") or pl.get("customfieldvalue") or {}
                lab = cfd.get("label") if isinstance(cfd, dict) else None
                cf[lab or json.dumps(pl, default=str)[:120]] += 1
    return {"records_scanned": min(len(cand), 400), "records_updated": len(cand),
            "event_types": types.most_common(), "status_transitions": trans.most_common(60),
            "tasks": tasks.most_common(60), "custom_field_writes": cf.most_common(60)}
safe("activity", activity)

def smrtphone():
    key = k._read_env_value(HERE.parent / ".env", "SMRTPHONE_API_KEY")
    if not key: return {"error": "no SMRTPHONE_API_KEY in SiftStack/.env"}
    base = "https://phone.smrt.studio"
    out = {}
    def hit(method, path, data=None):
        req = urllib.request.Request(base + path, method=method,
              data=urllib.parse.urlencode(data).encode() if data is not None else None,
              headers={"X-Auth-smrtPhone": key, "user-agent": "Mozilla/5.0",
                       "x-requested-with": "XMLHttpRequest",
                       "content-type": "application/x-www-form-urlencoded; charset=UTF-8"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                b = r.read(); return {"status": r.status, "bytes": len(b), "json": b[:1].decode(errors="ignore") in "{[", "head": b[:300].decode(errors="ignore")}
        except urllib.error.HTTPError as e:
            return {"status": e.code, "head": e.read()[:200].decode(errors="ignore")}
    out["key_check_sms_send_no_params"] = hit("POST", "/sms/send", {})   # 400 = valid key, 403 = bad; sends nothing
    form = {"draw": "1", "start": "0", "length": "5", "order[0][column]": "3", "order[0][dir]": "desc"}
    out["calls_filtered"] = hit("POST", "/logs/calls/filtered", form)
    out["numbers_filtered"] = hit("POST", "/phoneNumbers/filtered", form)
    return out
safe("smrtphone", smrtphone)

def sheet():
    import gspread
    from google.oauth2.service_account import Credentials
    bot = HERE.parent / "kpi-bot"
    sid = k._read_env_value(bot / ".env", "KPI_SPREADSHEET_ID")
    creds = Credentials.from_service_account_file(str(bot / "service_account.json"),
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    sh = gspread.authorize(creds).open_by_key(sid)
    tabs = {}
    for ws in sh.worksheets():
        rows = ws.get_values("A1:AZ3")
        tabs[ws.title] = {"rows": ws.row_count, "filled_rows": len(ws.col_values(1)), "header": rows[:3]}
    perms = None
    try:
        import json as _j
        sa = _j.loads((bot / "service_account.json").read_text())["client_email"]
        perms = sa
    except Exception: pass
    return {"title": sh.title, "service_account": perms, "tabs": tabs}
safe("sheet", sheet)

OUT.parent.mkdir(exist_ok=True)
OUT.write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
k.log(f"wrote {OUT}")
