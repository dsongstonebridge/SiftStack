"""call_review.py - nightly call-review PDF of Eric's live calls, posted to the KPI channel.

Jeff, 2026-11-25. Every weekday at 8 AM, for the previous weekday: pick Eric's calls where he reached a person
(no voicemails), build a 2-page-max phone PDF (tap the name to play the recording, a
notes box under each), and upload it to the KPI Slack channel with the kpi_bot token.

Which calls (read-only on DataSift):
  * a record counts when Eric wrote a board post that day that is a real talk note
    (not a voicemail note), or he set a lead / not-interested status on it that day;
  * the call is the one the note names (a phone number in the note), else the number he
    dispositioned within 30 min before the post, else his longest answered call then;
  * inbound callbacks on that record get their own link (recording from the smrtPhone log);
  * more than MAX_CALLS: leads first, then not interested, callbacks, the rest; longest first.

Usage:
  python call_review.py                 # post whatever weekday is due (the Windows task)
  python call_review.py --date 2026-11-24 --dry-run   # build the PDF only, no post
  python call_review.py --date 2026-11-24 --force     # post even if already posted
Settings (environment, then kpi-engine/.env, then the SiftStack .env one folder up and
C:/Users/dsong/OneDrive/Desktop/SiftStack/.env): SLACK_BOT_TOKEN, SLACK_KPI_CHANNEL_ID.
"""
from __future__ import annotations
import argparse, datetime, os, re, sys
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from reportlab.lib.utils import simpleSplit
from reportlab.lib import colors

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pull_kpis as P            # noqa: E402
import smrtphone_calls as S      # noqa: E402

TZ = ZoneInfo("America/Chicago")
ERIC_UUID = "b3923cfc-44ac-4094-8ef4-cf9c751d5529"
ERIC_NAME = "Eric Legaspi"
MAX_CALLS = 10
POST_AFTER = datetime.time(8, 0)  # Jeff 11-25: post the next weekday morning at 8 AM
STATE = HERE / "reports" / "call_review_last.txt"
OUTDIR = HERE / "reports" / "call_review"
LIVE_DISPOS = {"CORRECT", "WRONG", "WRONG_DNC", "CORRECT_DNC", "DNC"}
LEAD_STATUSES = ("lead",)  # new_lead, cold lead, warm lead, hot lead
ENV_FILES = (HERE / ".env", HERE.parent / ".env",
             Path(r"C:\Users\dsong\OneDrive\Desktop\SiftStack\.env"))


def setting(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if v:
        return v
    for f in ENV_FILES:
        if f.exists():
            for line in f.read_text(encoding="utf-8-sig").splitlines():
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


# ---------------------------------------------------------------- picking calls
def _status_of(pl) -> str:
    st = (pl.get("property") or {}).get("status")
    if isinstance(st, list):
        st = st[-1] if st else ""
    return str(st or "").lower()


def _talk_post(text: str) -> bool:
    return bool(text.strip()) and not P._is_vm_note(text)


def _note_numbers(text: str) -> list[str]:
    out = []
    for m in re.finditer(r"\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}", text):
        d = re.sub(r"\D", "", m.group())
        if len(d) == 10:
            out.append(d)
    return out


def _who(text: str) -> str:
    for pat in (r"\b(?:spoke|spoken|talked)\s+(?:with|to)\s+(?:a |an |the )?([A-Z][a-z]+)",
                r"^([A-Z][a-z]+)\s+(?:called|says|said|is)\b",
                r"\bher name is ([A-Z][a-z]+)", r"\bhis name is ([A-Z][a-z]+)"):
        m = re.search(pat, text)
        if m and m.group(1).lower() not in ("someone", "the", "a", "lady", "guy", "man", "woman", "different"):
            return m.group(1)
    t = text.lower()
    if "wrong" in t:
        return "Wrong number"
    if "not interested" in t:
        return "Not interested"
    if "hung up" in t:
        return "Hung up"
    if "interested" in t:
        return "Interested"
    return "Person"


def _fmt(sec) -> str:
    sec = int(sec or 0)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def smrtphone_day(day: str) -> list[dict]:
    """Inbound calls answered that day with a recording, from the smrtPhone log."""
    cookie = S._cookie_header()
    out, start = [], 0
    while start < 5000:
        data = S._page(cookie, start, 200)
        rows = data.get("data") or []
        stop = False
        for r in rows:
            dt = S._local(r)
            if not dt:
                continue
            if dt.date().isoformat() < day:
                stop = True
                break
            if dt.date().isoformat() != day or r.get("direction") != "inbound":
                continue
            rec = r.get("recording_sid")
            rec = rec.get("hasRec") if isinstance(rec, dict) else None
            num = re.sub(r"\D", "", str(S._num(r.get("from_num"), "fromNum")))[-10:]
            out.append({"when": dt, "number": num, "duration": int(r.get("duration") or 0),
                        "user": S._name(r.get("user")), "rec": rec})
        start += 200
        if stop or not rows:
            break
    return out


def pick_calls(day: str) -> list[dict]:
    tok = P.get_token()
    # "updated" holds the LAST change, so a record Eric worked that day and someone touched
    # again later only shows up if the window runs to now.
    nxt = (datetime.datetime.now(TZ).date() + datetime.timedelta(days=1)).isoformat()
    cands = P.search_updated(tok, day, nxt)
    P.log(f"{len(cands)} records updated {day}")
    try:
        inbound = smrtphone_day(day)
    except Exception as e:  # session expired etc: callbacks just get no link
        P.log(f"smrtPhone log unreadable ({e}); callbacks without recordings")
        inbound = []
    picks = []
    for r in cands:
        try:
            evs = P.get_logs(tok, r.get("uuid"))
        except Exception:
            continue
        day_evs = []
        for e in evs:
            d = P.local_dt(e.get("timestamp", ""), TZ)
            if d and d.date().isoformat() == day:
                day_evs.append((d, e))
        day_evs.sort(key=lambda x: x[0])
        posts = [(d, P._message_text(e)) for d, e in day_evs
                 if str(e.get("event_type", "")).startswith("property.message") and P.author_of(e)[1] == ERIC_NAME]
        statuses = [_status_of(e.get("payload") or {}) for d, e in day_evs
                    if e.get("event_type") == "property.status.updated" and P.author_of(e)[1] == ERIC_NAME]
        set_lead = any(any(k in s for k in LEAD_STATUSES) for s in statuses)
        set_ni = any("not_interested" in s or "not interested" in s for s in statuses)
        talk = [(d, t) for d, t in posts if _talk_post(t)]
        if not talk and (set_lead or set_ni):
            talk = [(d, t) for d, t in posts if t.strip()][-1:]
        if not talk:
            continue
        post_at, note = talk[-1]
        calls, dispos = [], {}
        for d, e in day_evs:
            pl = e.get("payload") or {}
            if e.get("event_type") == "owner.call.answered":
                c = pl.get("call") or {}
                if c.get("direction") == "outbound" and (c.get("user") or {}).get("uuid") != ERIC_UUID:
                    continue
                calls.append({"when": d, "dir": c.get("direction"), "number": P._digits10(c.get("phone") or P.call_number(c)),
                              "duration": c.get("duration"), "rec": c.get("recording")})
            elif e.get("event_type") == "owner.phone.status.updated" and P.author_of(e)[1] == ERIC_NAME:
                o = pl.get("owner") or {}
                st = o.get("status")
                st = st[-1] if isinstance(st, list) and st else st
                if str(st or "").upper() in LIVE_DISPOS:
                    dispos[P._digits10(o.get("phone"))] = (d, str(st).upper())
        out_calls = [c for c in calls if c["dir"] == "outbound" and c["rec"] and c["when"] <= post_at + datetime.timedelta(minutes=2)]
        win = [c for c in out_calls if c["when"] >= post_at - datetime.timedelta(minutes=30)]
        main = None
        for num in _note_numbers(note):
            hit = [c for c in out_calls if c["number"] == num]
            if hit:
                main = hit[-1]; break
        if not main:
            dn = [n for n, (d, s) in dispos.items() if post_at - datetime.timedelta(minutes=30) <= d <= post_at + datetime.timedelta(minutes=2)]
            hit = [c for c in out_calls if c["number"] in dn]
            if hit:
                main = max(hit, key=lambda c: c["duration"] or 0)
        if not main and win:
            main = max(win, key=lambda c: c["duration"] or 0)
        # inbound callbacks on this record, with recordings from smrtPhone
        cbs = []
        for c in calls:
            if c["dir"] != "inbound":
                continue
            m = [x for x in inbound if x["number"] == c["number"] and abs((x["when"] - c["when"]).total_seconds()) < 300 and x["rec"]]
            if m and m[0]["duration"] >= 20:
                cbs.append({"when": m[0]["when"], "number": c["number"], "duration": m[0]["duration"], "rec": m[0]["rec"]})
        if not main and cbs:
            main = max(cbs, key=lambda c: c["duration"]); cbs = [c for c in cbs if c is not main]
            main["callback"] = True
        if not main:
            continue
        st_now = P._flat_status(r.get("status")) or ""
        prio = 0 if set_lead else 1 if set_ni else 2 if (cbs or main.get("callback")) else 3
        who = _who(note)
        if main.get("callback") and "callback" not in who.lower():
            who += " (callback)"
        picks.append({"who": who, "address": P._flat_address(r.get("address")).split(", OK")[0],
                      "main": main, "callbacks": cbs[:1], "prio": prio, "status": st_now, "note": note})
    picks.sort(key=lambda p: (p["prio"], -(p["main"]["duration"] or 0)))
    picks = picks[:MAX_CALLS]
    picks.sort(key=lambda p: p["main"]["when"])
    return picks


# ---------------------------------------------------------------- PDF
def build_pdf(day: str, picks: list[dict], path: Path, title: str = "", height: float = 9.2) -> None:
    W, H = 4.25 * inch, height * inch
    M = 0.22 * inch
    NAVY, LINK = colors.HexColor("#1F3A5F"), colors.HexColor("#1A5FB4")
    label = title or datetime.date.fromisoformat(day).strftime("%a %m-%d")
    c = canvas.Canvas(str(path), pagesize=(W, H))
    c.setTitle(f"Eric call review {day}")
    def header():
        y = H - M - 14
        c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 15)
        c.drawString(M, y, f"Eric call review - {label}")
        c.setFont("Helvetica", 9); c.setFillColor(colors.grey)
        c.drawString(M, y - 13, "Tap a name to play the call.")
        return y - 30

    y = header()
    if not picks:
        c.setFillColor(colors.black); c.setFont("Helvetica", 12)
        c.drawString(M, y, "No live calls from Eric this day.")
    n = 0
    for page in (picks,):
        for p in page:
            summ = simpleSplit(summary(p), "Helvetica", 10, W - 2 * M)[:4]
            need = 26 + 12 * len(summ) + 12
            if y - need < M:
                c.showPage(); y = header()
            n += 1
            c.setFillColor(LINK); c.setFont("Helvetica-Bold", 13)
            t = f"{n}. {p['who']}"
            c.drawString(M, y, t)
            tw = c.stringWidth(t, "Helvetica-Bold", 13)
            c.setStrokeColor(LINK); c.line(M, y - 2, M + tw, y - 2)
            c.linkURL(p["main"]["rec"], (M - 2, y - 5, M + tw + 2, y + 13), relative=0)
            for cb in p["callbacks"]:
                x = M + tw + 10
                et = f"callback {_fmt(cb['duration'])}"
                c.setFont("Helvetica-Bold", 11)
                ew = c.stringWidth(et, "Helvetica-Bold", 11)
                c.drawString(x, y, et); c.line(x, y - 2, x + ew, y - 2)
                c.linkURL(cb["rec"], (x - 2, y - 5, x + ew + 2, y + 13), relative=0)
            c.setFillColor(colors.dimgrey); c.setFont("Helvetica", 9)
            c.drawString(M, y - 12, f"{p['address']}  ({_fmt(p['main']['duration'])})")
            # Jeff 11-25: no note boxes (Slack never saved them); a short summary instead.
            c.setFillColor(colors.black); c.setFont("Helvetica", 10)
            yy = y - 26
            for ln in summ:
                c.drawString(M, yy, ln); yy -= 12
            y = yy - 12
    c.showPage()
    c.save()


_VM_LEAD = re.compile(r"^\s*(no answers?\s*)?(-\s*)?(left (a )?vms?|vms?)?\s*[-,.]*\s*", re.I)


def summary(p: dict) -> str:
    """What to expect: Eric's own board note, minus the voicemail preamble, plus the outcome."""
    note = re.sub(r"\s+", " ", p.get("note") or "").strip()
    note = _VM_LEAD.sub("", note) if re.match(r"\s*no answer", note, re.I) else note
    note = note[:1].upper() + note[1:].rstrip(" -")
    if len(note) > 150:
        note = note[:150].rsplit(" ", 1)[0] + "..."
    elif note and note[-1] not in ".!?":
        note += "."
    st = (p.get("status") or "").replace("_", " ").strip()
    outcome = f" Status: {st.title()}." if st and st.lower() != "prospecting" else ""
    if p["main"].get("callback"):
        outcome = " They called Eric back." + outcome
    return (note or "No note from Eric.") + outcome


# ---------------------------------------------------------------- Slack
def post_pdf(day: str, path: Path, count: int) -> None:
    tok, chan = setting("SLACK_BOT_TOKEN"), setting("SLACK_KPI_CHANNEL_ID")
    if not tok or not chan:
        raise SystemExit("SLACK_BOT_TOKEN / SLACK_KPI_CHANNEL_ID not set")
    h = {"Authorization": "Bearer " + tok}
    data = path.read_bytes()
    r = requests.post("https://slack.com/api/files.getUploadURLExternal", headers=h,
                      data={"filename": path.name, "length": len(data)}, timeout=60).json()
    if not r.get("ok"):
        raise SystemExit(f"Slack upload URL failed: {r.get('error')}")
    up = requests.post(r["upload_url"], files={"file": (path.name, data, "application/pdf")}, timeout=120)
    up.raise_for_status()
    # Jeff 11-25: the post is ONLY the PDF, no message text.
    done = requests.post("https://slack.com/api/files.completeUploadExternal", headers=h, json={
        "files": [{"id": r["file_id"], "title": path.stem}], "channel_id": chan},
        timeout=60).json()
    if not done.get("ok"):
        raise SystemExit(f"Slack post failed: {done.get('error')}")


def due_days(now: datetime.datetime) -> list[str]:
    """Past weekdays not yet posted, oldest first (at most 3). Posts from 8 AM on a
    weekday, covering the days before it (Monday morning covers Friday)."""
    last = STATE.read_text().strip() if STATE.exists() else ""
    if now.weekday() >= 5 or now.time() < POST_AFTER:
        return []
    out, d = [], now.date() - datetime.timedelta(days=1)
    while len(out) < 3:
        if d.weekday() < 5:
            if last and d.isoformat() <= last:
                break
            out.append(d.isoformat())
        d -= datetime.timedelta(days=1)
    return sorted(out) if last else sorted(out)[-1:]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    days = [a.date] if a.date else due_days(datetime.datetime.now(TZ))
    if not days:
        return
    OUTDIR.mkdir(parents=True, exist_ok=True)
    for day in days:
        if not a.force and not a.dry_run and STATE.exists() and STATE.read_text().strip() >= day:
            continue
        picks = pick_calls(day)
        path = OUTDIR / f"Eric Call Review {day}.pdf"
        build_pdf(day, picks, path)
        P.log(f"{day}: {len(picks)} call(s) -> {path}")
        for p in picks:
            P.log(f"  {p['main']['when']:%H:%M} {p['who']} | {p['address']} | {_fmt(p['main']['duration'])} | prio {p['prio']}")
        if a.dry_run:
            continue
        post_pdf(day, path, len(picks))
        if not STATE.exists() or STATE.read_text().strip() < day:
            STATE.write_text(day)
        P.log(f"{day}: posted to Slack")


if __name__ == "__main__":
    main()
