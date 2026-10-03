"""Offline tests for the Tulsa fork switches in src/sms_agent (2026-10-03).

Throwaway database, DRY_RUN on, every outbound edge stubbed: nothing here can
reach DataSift, smrtPhone, Slack or Anthropic. Run:

    python tests/test_sms_agent_tulsa.py
"""
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="sms_tulsa_test_"))
os.environ.update({
    "SMS_AGENT_DB": str(TMP / "t.db"),
    "SMS_AGENT_DATA_DIR": str(TMP),
    "SMS_AGENT_DRY_RUN": "1",
    "SMS_AGENT_PHASE": "2",
    "SMS_AGENT_STOP_ON_ANY_REPLY": "1",
    "SMS_AGENT_TOUCH_SOURCE": "fields",
    "SMS_AGENT_REPLY_CHECK_MAX_AGE": "10",
    "SMS_AGENT_SLACK_FALLBACK": "0",
    "SMS_AGENT_SLACK_WEBHOOK": "",
    "SLACK_WEBHOOK_URL": "https://hooks.example/kpi-channel",
    "SMS_AGENT_HANDOFF_NAME": "Diego",
    "SMS_AGENT_HANDOFF_SLACK_ID": "U0DIEGO",
    "SMS_AGENT_SENDER_NAME": "Diego",
    "SMS_AGENT_CAMPAIGN_SOURCES": "FTM- 03 Call Attempt 1|0.6|deep;FTM- 04 Call Attempt 2|0.4",
    "ANTHROPIC_API_KEY": "",
    "SMRTPHONE_NUMBERS": '["+19180000001"]',
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sms_agent import campaign, config, crm, engine, escalate, seed, store, transport, worker  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---- stubs: record calls, never touch the network
POSTS = []
escalate._post = lambda text, blocks=None, program="": POSTS.append(text) or True
SENT = []
transport.send = lambda *a, **k: SENT.append(a) or (_ for _ in ()).throw(AssertionError("transport hit"))
FIELDS = {}
crm.text_touch = lambda uuid, n: ((FIELDS.get((uuid, n)), "") if FIELDS.get((uuid, n))
                                  else ("", f"Text Touch {n} is blank on this record"))
crm.client = lambda: object()
crm.dial_tier_checked = lambda uuid, phone: ("Dial First", True)
crm.deal_context = lambda uuid: {"owner_first": "Richard", "street": "2318 S 103rd Ave E", "city": "Tulsa"}
crm.post_note = lambda *a, **k: {"ok": True}
crm.set_phone_status = lambda *a, **k: {"ok": True}
crm.find_records_by_phone = lambda phone, limit=10: []
for name in ("add_tags", "set_status", "assign", "add_phone_tag", "bump_sms_attempts"):
    setattr(crm, name, lambda *a, **k: {"ok": True})

store.init()

# ---- config switches
check("shared KPI webhook refused when fallback is off", config.SLACK_WEBHOOK_URL == "",
      config.SLACK_WEBHOOK_URL)
check("campaign sources come from the env, in order",
      [s.title for s in campaign.SOURCES] == ["FTM- 03 Call Attempt 1", "FTM- 04 Call Attempt 2"]
      and campaign.SOURCES[0].deep and not campaign.SOURCES[1].deep,
      str(campaign.SOURCES))
try:
    campaign._sources_from_env("FTM- 03 Call Attempt 1")
    check("malformed source entry is refused", False)
except ValueError:
    check("malformed source entry is refused", True)

# ---- any reply stops every line of the record, one alert tagging Diego
REC = "rec-1"
A, B = "9185550101", "9185550102"
store.map_phone(A, record_uuid=REC)
store.map_phone(B, record_uuid=REC)
store.ensure_conversation(A, record_uuid=REC)
store.ensure_conversation(B, record_uuid=REC)
qa = store.queue_message(A, "touch 2 to A", "+19180000001")
qb = store.queue_message(B, "touch 2 to B", "+19180000001")

out = engine.handle_inbound({"from": A, "to": "+19180000001", "message": "who is this?", "smsId": "s1"})
check("neutral reply is handed to a human", out.get("action") == "handed_to_human", str(out))
check("replying line's queued text cancelled", store.outbox_status(qa) == "cancelled", store.outbox_status(qa))
check("sibling line's queued text cancelled", store.outbox_status(qb) == "cancelled", store.outbox_status(qb))
check("replying line paused", (store.get_conversation(A) or {}).get("state") == "paused")
check("sibling line paused", (store.get_conversation(B) or {}).get("state") == "paused")
check("one Slack post", len(POSTS) == 1, str(len(POSTS)))
check("post tags Diego", bool(POSTS) and "<@U0DIEGO>" in POSTS[0], POSTS[0] if POSTS else "")
check("post carries the reply text", bool(POSTS) and "who is this?" in POSTS[0])

engine.handle_inbound({"from": A, "to": "+19180000001", "message": "hello??", "smsId": "s2"})
check("second reply does not post again", len(POSTS) == 1, str(len(POSTS)))

# ---- opt-out stops ONLY that number; the record's other line keeps going
C, C2 = "9185550103", "9185550113"
for p in (C, C2):
    store.map_phone(p, record_uuid="rec-2")
    store.ensure_conversation(p, record_uuid="rec-2")
qc2 = store.queue_message(C2, "touch 2 to C2", "+19180000001")
out = engine.handle_inbound({"from": C, "to": "+19180000001", "message": "STOP", "smsId": "s3"})
check("STOP is an opt-out", out.get("action") == "opted_out", str(out))
check("opted-out line stays opted_out", (store.get_conversation(C) or {}).get("state") == "opted_out")
check("opted-out number suppressed", bool(store.is_suppressed(C)))
check("opt-out leaves the other line's text queued", store.outbox_status(qc2) == "queued",
      store.outbox_status(qc2))
check("opt-out leaves the other line active", (store.get_conversation(C2) or {}).get("state") == "active",
      str((store.get_conversation(C2) or {}).get("state")))
check("opt-out alert says do not text this number", len(POSTS) == 2 and "do not text it" in POSTS[1],
      POSTS[-1] if POSTS else "")

# ---- wrong number also stops only that number
W, W2 = "9185550121", "9185550122"
for p in (W, W2):
    store.map_phone(p, record_uuid="rec-6")
    store.ensure_conversation(p, record_uuid="rec-6")
qw2 = store.queue_message(W2, "touch 2 to W2", "+19180000001")
out = engine.handle_inbound({"from": W, "to": "+19180000001", "message": "wrong number", "smsId": "s4"})
check("wrong number recognised", out.get("action") == "wrong_number", str(out))
check("wrong number leaves the other line's text queued", store.outbox_status(qw2) == "queued")
check("wrong number does not stop the record",
      not seed._record_replied("rec-6"))

# ---- seed uses the field copy verbatim; blank holds; a replied record holds
FIELDS[("rec-3", 1)] = "Hi Amy! My name is Diego. Is 12 Elm St yours? Thanks so much!"
rows = [
    {"phone": "9185550104", "uuid": "rec-3", "street": "12 Elm St", "city": "Tulsa", "first": "Amy"},
    {"phone": "9185550105", "uuid": "rec-4", "street": "14 Elm St", "city": "Tulsa", "first": "Bo"},
    {"phone": "9185550106", "uuid": REC, "street": "2318 S 103rd Ave E", "city": "Tulsa", "first": "Richard"},
]
FIELDS[(REC, 1)] = "would be sent if the record had not replied"
built = {c.record_uuid: c for c in seed.build(rows, touch=1, sender_fallback="Diego")}
check("field copy sent verbatim", built["rec-3"].status == "ready"
      and built["rec-3"].message == FIELDS[("rec-3", 1)], built["rec-3"].message)
check("blank field holds, never improvises", built["rec-4"].status == "hold"
      and "blank" in " ".join(built["rec-4"].reasons), str(built["rec-4"].reasons))
check("a record that replied on another line is held", built[REC].status == "hold"
      and "replied" in " ".join(built[REC].reasons), str(built[REC].reasons))

# ---- pool mode (the agent writes its own copy): ordinals fixed, human-voice checked
config.TOUCH_SOURCE = "pool"
built = seed.build([{"phone": "9185550131", "uuid": "rec-7", "street": "2318 S 103Rd Ave E",
                     "city": "Tulsa", "first": "Richard", "last": "Smith"}], touch=1, sender_fallback="Diego")
msg = built[0].message
check("pool copy is ready", built[0].status == "ready", str(built[0].reasons))
check("pool copy lowercases the ordinal", "103rd" in msg and "103Rd" not in msg, msg)
check("pool copy is signed by Diego", "Diego" in msg, msg)
built = seed.build([{"phone": "9185550132", "uuid": "rec-8", "street": "16547 E 2Nd Pl",
                     "city": "Tulsa", "first": "Sadie", "last": "Jones"}], touch=1, sender_fallback="Diego")
check("five-digit house number is not mistaken for a zip", built[0].status == "ready",
      str(built[0].reasons))
config.TOUCH_SOURCE = "fields"

# ---- fail closed: no successful reply check means nothing sends
D = "9185550107"
store.ensure_conversation(D, record_uuid="rec-5")
qd = store.queue_message(D, "touch 1 to D", "+19180000001")
res = worker.drain_outbox()
check("no reply check yet -> held, nothing sent", res.get("sent") == 0 and res.get("blind"), str(res))
check("held row still queued for later", store.outbox_status(qd) == "queued")
store.set_meta(worker.REPLY_CHECK_KEY, "2000-01-01T00:00:00+00:00")
check("stale reply check -> held", bool(worker.reply_check_stale()))
store.set_meta(worker.REPLY_CHECK_KEY, store.now())
check("fresh reply check -> clear", worker.reply_check_stale() == "")
check("transport never called (dry run)", SENT == [])

# ---- send-time re-check: a number marked bad AFTER queueing is not texted
config.SEND_TIME_PHONE_CHECK = True
PHONES = {}  # phone -> phone object as DataSift returns it; missing = unreadable
crm.find_phone_object = lambda rec, phone, fresh=False: ("own", PHONES.get(phone))
crm.get_record = lambda rec, fresh=False: ({} if rec != "rec-down" else None)
crm.dial_tier = lambda rec, phone: next(
    (t for t in ("Dial First", "Dial Second", "Dial Third", "Dial Fourth", "Drop")
     if t in (PHONES.get(phone) or {}).get("tags", [])), "")
cases = {
    "9185550141": ({"status": "WRONG", "type": "MOBILE", "tags": ["Dial First"]}, "cancelled"),
    "9185550142": ({"status": "DNC", "type": "MOBILE", "tags": ["Dial First"]}, "cancelled"),
    "9185550143": ({"status": "DEAD", "type": "MOBILE", "tags": ["Dial First"]}, "cancelled"),
    "9185550144": ({"status": "UNKNOWN", "type": "LANDLINE", "tags": ["Dial First"]}, "cancelled"),
    "9185550145": ({"status": "UNKNOWN", "type": "MOBILE", "tags": ["Dial Third"]}, "cancelled"),
    "9185550146": ({"status": "UNKNOWN", "type": "MOBILE", "tags": []}, "cancelled"),
    "9185550147": ({"status": "NO_ANSWER", "type": "MOBILE", "tags": ["Dial First"]}, "sent"),
    "9185550148": ({"status": "UNKNOWN", "type": "UNKNOWN", "tags": ["Dial Second"]}, "sent"),
}
rows_q = {}
for i, (ph, (obj, _)) in enumerate(cases.items()):
    PHONES[ph] = obj
    store.map_phone(ph, record_uuid=f"rec-sc{i}")
    store.ensure_conversation(ph, record_uuid=f"rec-sc{i}")
    rows_q[ph] = store.queue_message(ph, "touch", "+19180000001")
store.map_phone("9185550149", record_uuid="rec-down")
store.ensure_conversation("9185550149", record_uuid="rec-down")
q_down = store.queue_message("9185550149", "touch", "+19180000001")
store.set_meta(worker.REPLY_CHECK_KEY, store.now())
store.cancel_queued(D, "test cleanup")
worker.time.sleep = lambda s: None
worker.sender_pool.within_quiet_hours = lambda phone: True
worker.sender_pool.available = lambda n: (True, "")
worker.drain_outbox(limit=50)
for ph, (obj, want) in cases.items():
    got = store.outbox_status(rows_q[ph])
    check(f"send-time: {obj['status']}/{obj['type']}/{obj['tags'] or 'untagged'} -> {want}", got == want, got)
check("send-time: unreadable record -> held, not sent", store.outbox_status(q_down) == "queued",
      store.outbox_status(q_down))

print()
print(f"{len(FAILS)} failed" if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
