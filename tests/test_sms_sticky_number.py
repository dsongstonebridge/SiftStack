"""Offline: only a number that actually SENT pins a phone/record (Jeff, 2026-10-12).

10/8 staged Ready to Call on ...1187, nothing sent, and on 10/12 all 91 came
back on ...1187 after it had been carrier-flagged.
"""
import os
import sys
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["SMS_AGENT_DB"] = str(tmp / "t.db")
os.environ["SMS_AGENT_DRY_RUN"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sms_agent import store  # noqa: E402

FAILS = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        FAILS.append(name)


store.init()
OLD, NEW = "+19188771187", "+19184770140"
with store.tx() as c:
    for phone, rec in (("9185550001", "rec-1"), ("9185550002", "rec-1"), ("9185550003", "rec-2")):
        c.execute("INSERT INTO phone_map(phone, record_uuid, updated_at) VALUES (?,?,?)",
                  (phone, rec, store.now()))

# staged on OLD, never sent
store.ensure_conversation("9185550001", from_number=OLD, record_uuid="rec-1")
store.ensure_conversation("9185550002", from_number=OLD, record_uuid="rec-1")
check("staged-only phone is not pinned", store.phone_sent_from("9185550001") == "")
check("staged-only record is not pinned", store.record_from_number("rec-1") == "")

# a real send pins the phone and the whole record
store.record_send(NEW, "9185550002")
check("sent phone is pinned to its sender", store.phone_sent_from("9185550002") == NEW)
check("its record follows the real sender", store.record_from_number("rec-1") == NEW)
check("unrelated record stays free", store.record_from_number("rec-2") == "")

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
