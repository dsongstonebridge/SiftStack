"""Offline: one worker at a time, one send per row, never the same text twice.
Runs on a throwaway DB. No network, nothing sent."""
import os, subprocess, sys, tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["SMS_AGENT_DB"] = str(tmp / "t.db")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sms_agent import store, instance_lock  # noqa: E402

fails = 0
def check(name, ok):
    global fails
    print(("PASS " if ok else "FAIL ") + name)
    fails += 0 if ok else 1

store.init()
with store.tx() as c:
    c.execute("INSERT INTO outbox (phone, body, status, created_at) VALUES ('9180000001','hello','queued','2026-10-09T00:00:00+00:00')")
    rid = c.execute("SELECT max(id) FROM outbox").fetchone()[0]
check("first claim wins", store.claim_outbox(rid) is True)
check("second claim loses", store.claim_outbox(rid) is False)
check("interrupted send marked failed, not requeued", store.recover_interrupted_sends() == 1
      and store.outbox_status(rid) == "failed")
check("unsent text is not a duplicate", store.already_sent_text("9180000001", "hello") is False)
store.add_message("9180000001", "out", "hello", "+19180000000", author="ai")
check("same text to same number is a duplicate", store.already_sent_text("9180000001", "hello"))
check("same text to another number is fine", not store.already_sent_text("9180000002", "hello"))

lock = tmp / "worker.lock"
check("this process takes the lock", instance_lock.acquire(lock))
code = ("import sys; sys.path.insert(0, %r); from sms_agent import instance_lock;"
        " print(instance_lock.acquire(%r))") % (str(Path(__file__).resolve().parents[1] / "src"), str(lock))
out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                     env=dict(os.environ)).stdout.strip()
check("a second process is refused", out.endswith("False"))
instance_lock.release()
out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                     env=dict(os.environ)).stdout.strip()
check("after release a new process can take it", out.endswith("True"))
# --- daily progression + continue-only sources + end-of-day stop -------------
from datetime import date, datetime, timezone  # noqa: E402
from sms_agent import campaign, worker  # noqa: E402

today = date(2026, 10, 9)
check("never texted -> touch 1", campaign.next_touch(None, 1, today)[0] == 1)
for n in (1, 2, 3):
    e = {"touches": set(range(1, n + 1)), "last": "2026-10-08T15:00:00+00:00"}
    check(f"touch {n} yesterday -> touch {n+1} today", campaign.next_touch(e, 1, today)[0] == n + 1)
e = {"touches": {1, 2, 3, 4}, "last": "2026-10-08T15:00:00+00:00"}
check("touch 4 done -> nothing more", campaign.next_touch(e, 1, today)[0] is None)
e = {"touches": {2}, "last": "2026-10-09T14:00:00+00:00"}
check("texted today -> nothing more today", campaign.next_touch(e, 1, today)[0] is None)
# 11:30 PM Thursday Central is already Friday in UTC; it still counts as Thursday.
e = {"touches": {2}, "last": "2026-10-09T04:30:00+00:00"}
check("late-evening text counts as the Tulsa day", campaign.next_touch(e, 1, today)[0] == 3)
e = {"touches": {2}, "last": "2026-10-06T15:00:00+00:00"}
check("missed days still advance one touch", campaign.next_touch(e, 1, today)[0] == 3)

src = campaign._sources_from_env(
    "FTM- 02 Ready to Call|0.5|deep|continue;FTM- 03 Call Attempt 1|0.2|deep")
check("continue flag parsed", src[0].continue_only and src[0].deep and not src[1].continue_only)
try:
    campaign._sources_from_env("X|0.5|contnue")
    bad = False
except ValueError:
    bad = True
check("misspelled flag refused, not ignored", bad)

st = worker._stop_time(datetime(2026, 10, 9, 13, 0, tzinfo=timezone.utc))  # 8 AM CDT
check("8 AM start stops at 7 PM same day", st == datetime(2026, 10, 10, 0, 0, tzinfo=timezone.utc))
st = worker._stop_time(datetime(2026, 10, 10, 1, 0, tzinfo=timezone.utc))  # 8 PM CDT
check("evening start stops 7 AM next day", st == datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc))

# --- delivery check (Jeff, 2026-10-09) ----------------------------------------
from sms_agent import delivery, escalate  # noqa: E402

posted = []
escalate.alert = lambda title, detail="", **k: posted.append((title, detail)) or True
def fake_rows(*_a, **_k):
    rows = [{"id": 1000 + i, "direction": "outbound", "fromNum": "+19180000000",
             "toNum": f"+1918555{1000 + i}", "delivery_status": "undelivered", "delivery_code": "30007",
             "content": f"Hey Name{i}! I hope I&#039;m not being a bother. I&#039;m interested in {100 + i} S Main St"}
            for i in range(3)]
    rows.append({"id": 2000, "direction": "outbound", "fromNum": "+19180000000", "toNum": "+19185552000",
                 "delivery_status": "undelivered", "delivery_code": "30006", "content": "Hi there"})
    rows.append({"id": 3000, "direction": "outbound", "fromNum": "+19180000000", "toNum": "+19185553000",
                 "delivery_status": "delivered", "delivery_code": None, "content": "Hi"})
    return rows
delivery._fetch = fake_rows
r1 = delivery.run()
check("undelivered texts recorded (delivered ignored)", r1.get("new_failures") == 4)
check("one #SMS post listing them", len(posted) == 1 and "4 text(s) NOT delivered" in posted[0][1])
check("same wording with different names counts together, alarm fires",
      "WORDING BLOCKED AS SPAM 3x" in posted[0][1])
check("carrier says landline -> do-not-text list", bool(store.is_suppressed("9185552000")))
r2 = delivery.run()
check("same failures are not reported twice", r2.get("new_failures") == 0 and len(posted) == 1)
check("retired spam wording is gone from the touch pools",
      not any("not being a bother" in t for pool in campaign.touches.POOLS for lst in pool for t in lst))

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
