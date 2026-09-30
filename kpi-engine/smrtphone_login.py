"""Opens smrtPhone in a browser window; you log in; the session is saved to
SiftStack/smrtphone_state.json (never uploaded - it's in .gitignore).
Then it reads (read-only) which smrtPhone pages hold call logs and dialer sessions,
and saves that map to reports/smrtphone_discovery.json for Claude."""
import json, re, time, sys
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "smrtphone_state.json"
OUT = Path(__file__).resolve().parent / "reports" / "smrtphone_discovery.json"
BASE = "https://phone.smrt.studio"

with sync_playwright() as p:
    b = p.chromium.launch(headless=False)
    ctx = b.new_context(storage_state=str(STATE)) if STATE.exists() else b.new_context()
    page = ctx.new_page()
    page.goto(BASE + "/login")
    print("Log in to smrtPhone in the window that opened (including the text code).")
    print("It saves by itself once you are fully in (after the text code). Leave this window open.")
    import threading
    pressed = threading.Event()
    threading.Thread(target=lambda: (input(), pressed.set()), daemon=True).start()
    COLUMNS = ["id", "user", "user_id", "created_at", "direction", "status", "disposition", "from_num",
               "to_num", "price", "duration", "podio_id", "recording_sid", "sid", "call_agent_id"]
    form = {"draw": "1", "start": "0", "length": "1", "order[0][column]": "3", "order[0][dir]": "desc",
            "search[value]": "", "search[regex]": "false"}
    for ci, col in enumerate(COLUMNS):   # same DataTables form Ty's pull_calls.py sends
        form[f"columns[{ci}][data]"] = col; form[f"columns[{ci}][name]"] = ""
        form[f"columns[{ci}][searchable]"] = "true"; form[f"columns[{ci}][orderable]"] = "true"
        form[f"columns[{ci}][search][value]"] = ""; form[f"columns[{ci}][search][regex]"] = "false"
    def logged_in():
        # Real proof, not a URL guess: the call log answers with data only once fully
        # logged in (after the text code). Before that it returns the login page.
        try:
            r = ctx.request.post(BASE + "/logs/calls/filtered", form=form,
                                 headers={"x-requested-with": "XMLHttpRequest"})
            if r.text()[:1].strip() in "{[":
                return True
            d = ctx.request.get(BASE + "/dashboard")      # not bounced back to login/code page?
            return d.ok and "/dashboard" in d.url and "login" not in d.text()[:4000].lower()
        except Exception:
            return False
    for i in range(900):                       # up to 15 minutes
        try:
            (ctx.pages[0] if ctx.pages else page).wait_for_timeout(1000)
        except Exception:
            sys.exit("Window closed before login finished - run it again.")
        if (pressed.is_set() or i % 3 == 0) and logged_in():
            break
        if pressed.is_set() and getattr(logged_in, "nudged", False):
            print("Saving now because you pressed Enter twice.")
            break
        if pressed.is_set():
            logged_in.nudged = True
            print("Not detected as fully logged in yet. Finish the text code; if your dashboard is"
                  " already showing, press Enter once more to save anyway.")
            pressed.clear()
            threading.Thread(target=lambda: (input(), pressed.set()), daemon=True).start()
    else:
        sys.exit("Timed out waiting for login.")
    (ctx.pages[0] if ctx.pages else page).wait_for_timeout(3000)
    ctx.storage_state(path=str(STATE))
    print("Session saved.")

    res = {}
    js = ctx.request.get(BASE + "/js/routing?callback=fos.Router.setData").text()
    names = sorted(set(re.findall(r'"([a-zA-Z0-9_]+)":\{"tokens"', js)))
    paths = sorted(set(re.findall(r'"(/[a-zA-Z0-9_/{}\-]*(?:log|call|session|dialer|report|stat)[a-zA-Z0-9_/{}\-]*)"', js, re.I)))
    res["route_names_matching"] = [n for n in names if re.search(r"log|call|session|dialer|report|stat|agent", n, re.I)]
    res["paths_matching"] = paths[:300]
    form["length"] = "3"
    r = ctx.request.post(BASE + "/logs/calls/filtered", form=form,
                         headers={"x-requested-with": "XMLHttpRequest"})
    body = r.text()
    res["calls_filtered"] = {"status": r.status, "json": body[:1].strip() in "{[", "sample": body[:1500]}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(res, indent=2), encoding="utf-8")
    print("Done. Tell Claude \"smrtphone done\".")
    b.close()
