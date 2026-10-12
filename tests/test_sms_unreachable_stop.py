"""Offline: a number unreachable (30003) twice stops getting texts; once does not."""
import os
import sys
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["SMS_AGENT_DB"] = str(tmp / "t.db")
os.environ["SMS_AGENT_DRY_RUN"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sms_agent import config, crm, delivery, escalate, sender_pool, store  # noqa: E402
from sms_agent import number_health  # noqa: E402

FAILS = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        FAILS.append(name)


escalate.alert = lambda *a, **k: True
NOTES = []
crm.post_note = lambda uuid, msg, pinned=False: NOTES.append((uuid, msg)) or {"ok": True}
config.DRY_RUN = False   # the stub above is the only CRM edge


def row(log_id, phone, code):
    return {"id": log_id, "direction": "outbound", "delivery_status": "undelivered",
            "delivery_code": code, "toNum": phone, "fromNum": "+19180000140",
            "content": "Hi Kim! My name is Diego."}


store.init()
with store.tx() as c:
    c.execute("INSERT OR REPLACE INTO phone_map(phone, record_uuid, updated_at) VALUES (?,?,?)",
              ("9185550001", "rec-1", store.now()))
store.queue_message("9185550001", "touch 4", status="queued")
with store.tx() as c:
    check("setup: one text queued", c.execute(
        "SELECT count(*) FROM outbox WHERE phone='9185550001' AND status='queued'").fetchone()[0] == 1)

# one 30003: could just be a phone switched off
delivery._fetch = lambda pages: [row(1, "9185550001", "30003")]
delivery.run()
check("one unreachable does not suppress", not store.is_suppressed("9185550001"))

# second 30003 on the same number: out of service
delivery._fetch = lambda pages: [row(1, "9185550001", "30003"), row(2, "9185550001", "30003")]
delivery.run()
check("two unreachable suppresses", bool(store.is_suppressed("9185550001")))
with store.tx() as c:
    left = c.execute("SELECT count(*) FROM outbox WHERE phone='9185550001' AND status IN"
                     " ('queued','held')").fetchone()[0]
check("queued texts to it are cancelled", left == 0)
check("reason is texts only, not do-not-call", "unreachable" in (store.is_suppressed("9185550001") or ""))
check("one board note on the record", len(NOTES) == 1 and NOTES[0][0] == "rec-1"
      and "Calling is NOT affected" in NOTES[0][1])
delivery._fetch = lambda pages: [row(1, "9185550001", "30003"), row(2, "9185550001", "30003"),
                                 row(5, "9185550001", "30003")]
delivery.run()
check("board note is not repeated", len(NOTES) == 1)

# a different number with a spam block (30007) twice is NOT out of service
delivery._fetch = lambda pages: [row(3, "9185550002", "30007"), row(4, "9185550002", "30007")]
delivery.run()
check("spam blocks do not suppress the number", not store.is_suppressed("9185550002"))

# the alert text names it
msg = delivery._message([{"phone": "9185550001", "from_number": "+19180000140",
                          "code": "30003", "wording": "touch 1"}])
check("alert says texts stopped, calls not", "calls" in msg and "...0001" in msg)

# touch 1 only from a clean number (Jeff, 2026-10-12)
flags = {"9180000140": 0, "9180001187": 1, "9180007981": 1}
number_health.flag_count = lambda n: flags.get(store.clean_phone(n))
number_health.check = lambda n: (True, "")
config.NUMBER_HEALTH_CHECK = True
config.NEW_START_MAX_FLAGS = 0
pool = ["9180001187", "9180000140", "9180007981"]
check("touch 1 starts on the clean number", sender_pool.daily_number(pool) == "9180000140")
flags["9180000140"] = 1
check("no clean number = nobody new starts", sender_pool.daily_number(pool) == "")
config.NEW_START_MAX_FLAGS = -1
check("limit off = old behaviour", sender_pool.daily_number(pool) in pool)

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
