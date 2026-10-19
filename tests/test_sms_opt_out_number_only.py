"""Offline: an opt-out blocks THAT NUMBER only, never tags the record Do Not Market.

Jeff, 2026-10-19: a stranger's "THIS IS NOT AMY ... stop texting" tagged the
owner's whole record Do Not Market. With SMS_AGENT_NO_RECORD_OPT_OUT_TAG on, the
number is still suppressed, DNC'd and noted; the record gets no tag.
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

tmp = Path(tempfile.mkdtemp())
os.environ["SMS_AGENT_DB"] = str(tmp / "t.db")
os.environ["SMS_AGENT_DRY_RUN"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sms_agent import config, crm, engine, escalate, smrtphone, store  # noqa: E402

FAILS = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        FAILS.append(name)


TAGS, NOTES, STATUSES = [], [], []
crm.add_tags = lambda uuid, tags: TAGS.append((uuid, list(tags))) or {"ok": True}
crm.post_note = lambda uuid, msg, pinned=False: NOTES.append((uuid, msg)) or {"ok": True}
crm.set_phone_status = lambda uuid, phone, status: STATUSES.append((uuid, phone, status)) or {"ok": True}
crm.dnc_status = lambda uuid, phone, wrong_number=False: "WRONG_DNC" if wrong_number else "DNC"
smrtphone.add_to_dnt = lambda phone: (True, "")
escalate.alert = lambda *a, **k: True
config.DRY_RUN = False
config.PHASE = 2

store.init()
MATCH = [{"record_uuid": "rec-1"}]


def opt_tags():
    return [t for t in TAGS if config.TAG_OPT_OUT in t[1]]


def reset():
    TAGS.clear(); NOTES.clear(); STATUSES.clear()


# --- switch ON (the Tulsa setting) ---
config.NO_RECORD_OPT_OUT_TAG = True

engine._do_opt_out("9185550001", "rec-1", MATCH, body="Stop")
check("STOP: number suppressed", bool(store.is_suppressed("9185550001")))
check("STOP: phone marked DNC on the record", ("rec-1", "9185550001", "DNC") in STATUSES)
check("STOP: note posted", any(n[0] == "rec-1" for n in NOTES))
check("STOP: record NOT tagged Do Not Market", not opt_tags())

reset()
hostile = SimpleNamespace(rationale="harassment claim")
engine._do_sensitive("9185550002", "rec-1", "this is harassment, stop", hostile, MATCH)
check("hostile: number suppressed", bool(store.is_suppressed("9185550002")))
check("hostile: record NOT tagged Do Not Market", not opt_tags())

reset()
engine._do_sensitive("9185550003", "rec-1", "this is harassment, stop", hostile, MATCH)
check("hostile (second number): still no record tag", not opt_tags())

reset()
config.SENSITIVE_SLACK_LEGAL_ONLY = True
sad = SimpleNamespace(rationale="bereavement")
engine._do_sensitive("9185550005", "rec-1", "my mother passed away last year", sad, MATCH)
check("bereaved: record NOT tagged Mail Only", not [t for t in TAGS if config.TAG_MAIL_ONLY in t[1]])
check("bereaved: record NOT tagged Do Not Market", not opt_tags())

# --- switch OFF (Ty's upstream behaviour unchanged) ---
reset()
config.NO_RECORD_OPT_OUT_TAG = False
engine._do_opt_out("9185550004", "rec-2", [{"record_uuid": "rec-2"}], body="Stop")
check("upstream: record tagged Do Not Market", ("rec-2", [config.TAG_OPT_OUT]) in TAGS)

print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
