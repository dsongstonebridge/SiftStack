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

# ---- Correct number first, unless we already spoke with them (2026-10-05)
# Event shapes copied from real FTM records (numbers swapped for fakes).
def _call(num, ts, direction="outbound"):
    key = "destination_number" if direction == "outbound" else "origin_number"
    return {"event_type": "owner.call.answered", "source": "smrtphone", "author": "system",
            "timestamp": ts, "payload": {"call": {"direction": direction, key: num,
                                                  "extra": {"status": "answered"}, "duration": 70}}}


def _note(text, ts, source="internal-api", kind="property.message.added"):
    return {"event_type": kind, "source": source, "author": "kdhoemann@gmail.com",
            "timestamp": ts, "payload": {"message": text}}


C1 = "9185550160"
check("VM note read as voicemail",
      seed.is_vm_note("no answer, left vm, but the message said this is tim"))
check("VM greeting with a name is still voicemail",
      seed.is_vm_note("no answer but vm says this is Brian. Left vm"))
check("'spoke with' is a conversation", seed.is_talk_note("spoke with matthew and he said its his primary residence"))
check("VM note is not a conversation", not seed.is_talk_note("no answer, left vm, but the message said this is tim"))
check("answered call + same-day VM note -> not spoken",
      seed.spoken_on([_call(C1, "2026-09-30 21:20:57"),
                      _note("no answer, left vm, but the message said this is tim", "2026-09-30 21:21:20")], C1) == "")
check("answered call, no note -> spoken",
      seed.spoken_on([_call(C1, "2026-09-30 21:20:57")], C1) != "")
check("talk note anywhere -> spoken",
      seed.spoken_on([_note("spoke with matthew, not interested", "2026-09-30 21:56:38")], C1) != "")
check("2442 E 3rd St: dad talk note + her VM note, same day -> not spoken on hers",
      seed.spoken_on([_call("9185550810", "2026-10-01 20:54:18"),
                      _note("810 is the dad and says if i want to reach her", "2026-10-01 20:55:56"),
                      _call(C1, "2026-10-01 20:58:43"),
                      _note("left her a vm", "2026-10-01 20:59:04")], C1) == "")
check("2442 E 3rd St: the dad's line DOES count as spoken",
      seed.spoken_on([_call("9185550810", "2026-10-01 20:54:18"),
                      _note("810 is the dad and says if i want to reach her", "2026-10-01 20:55:56")],
                     "9185550810") != "")
check("talk note the same day this number was answered -> spoken",
      seed.spoken_on([_call(C1, "2026-10-01 18:00:00"),
                      _note("spoke with her, call back friday", "2026-10-01 18:05:00")], C1) != "")
check("11300 N 118th E Ave: one 'left vms' after a dialing run covers the earlier calls",
      seed.spoken_on([_call(C1, "2026-09-30 22:24:40"),
                      _call("9185550657", "2026-09-30 22:32:06"),
                      _note("left vms", "2026-09-30 22:32:18")], C1) == "")
check("1288 E 143rd St: VM note, then 'called twice' -> not spoken",
      seed.spoken_on([_call(C1, "2026-10-01 15:31:59"),
                      _note("No answer but recorded vm says Dana. mailbox is full", "2026-10-01 15:32:37"),
                      _call(C1, "2026-10-01 15:33:28"),
                      _note("called twice", "2026-10-01 15:33:39")], C1) == "")
check("neutral note with no voicemail that day -> still spoken",
      seed.spoken_on([_call(C1, "2026-10-01 15:33:28"),
                      _note("called twice", "2026-10-01 15:33:39")], C1) != "")
check("inbound answered call -> spoken",
      seed.spoken_on([_call(C1, "2026-09-30 15:00:00", "inbound")], C1) != "")
check("answered call on ANOTHER number does not count",
      seed.spoken_on([_call("9185550999", "2026-09-30 21:20:57")], C1) == "")
check("pipeline upload post ignored",
      seed.spoken_on([_note("Skip traced via Tracerfy. Petition info said ...", "2026-08-19 22:05:42",
                            source="upload")], C1) == "")
check("late-evening UTC call and VM note on the same Central day pair up",
      seed.spoken_on([_call(C1, "2026-10-01 02:10:00"),          # 9:10pm Central, Sep 30
                      _note("lvm", "2026-09-30 21:00:00")], C1) == "")

config.CORRECT_NUMBER_FIRST = True
RECS, LOGS = {}, {}
crm.get_record = lambda rec, fresh=False: RECS.get(rec)
crm.activity_log = lambda rec: LOGS.get(rec)
crm.phone_is_dnc = lambda phone: False


def _rec(*phones):
    return {"owner": {"phones": [{"number": n, "status": s, "type": t, "tags": tags}
                                 for n, s, t, tags in phones]}}


RECS["r-c1"] = _rec(("9185550161", "UNKNOWN", "MOBILE", ["Dial First"]),
                    ("9185550162", "CORRECT", "MOBILE", ["Dial Third"]))
LOGS["r-c1"] = []
got, why = seed.apply_correct_number({"uuid": "r-c1", "phone": "9185550161"}, set())
check("Correct number replaces the search-row number", bool(got) and got["phone"] == "9185550162", str((got, why)))
check("Correct number ignores its Dial Third tier", bool(got) and got["dial_tier"] == "Correct")

LOGS["r-c1"] = [_call("9185550162", "2026-09-30 21:00:00"),
                _note("talked to him, call back next week", "2026-09-30 21:05:00")]
got, why = seed.apply_correct_number({"uuid": "r-c1", "phone": "9185550161"}, set())
check("spoke on the Correct number -> no text to the record", got is None and "spoke" in why, why)

RECS["r-c2"] = _rec(("9185550163", "UNKNOWN", "MOBILE", ["Dial First"]))
row = {"uuid": "r-c2", "phone": "9185550163"}
got, _ = seed.apply_correct_number(row, set())
check("no Correct number -> row unchanged (no number hopping)", got is row)

RECS["r-c3"] = _rec(("9185550164", "UNKNOWN", "MOBILE", ["Dial First"]),
                    ("9185550165", "CORRECT", "LANDLINE", ["Dial First"]))
got, why = seed.apply_correct_number({"uuid": "r-c3", "phone": "9185550164"}, set())
check("Correct landline -> no text, never falls back to the other number", got is None, why)

RECS["r-c4"] = _rec(("9185550166", "CORRECT", "MOBILE", ["Dial First"]))
got, why = seed.apply_correct_number({"uuid": "r-c4", "phone": "9185550166"}, set())
check("unreadable call log -> held", got is None and "could not read" in why, why)

# Send time: phone_still_textable on the same rules.
crm.find_phone_object = lambda rec, phone, fresh=False: (
    "own", next((p for p in (RECS.get(rec) or {}).get("owner", {}).get("phones", [])
                 if p["number"] == phone), None))
crm.dial_tier = lambda rec, phone: "Dial First"
LOGS["r-c1"] = []
ok, why = seed.phone_still_textable("r-c1", "9185550161")
check("send-time: another number marked Correct -> cancelled", ok is False and "Correct" in why, why)
ok, why = seed.phone_still_textable("r-c1", "9185550162")
check("send-time: the Correct number (Dial Third) -> sent", ok is True, why)
LOGS["r-c1"] = [_note("spoke with him", "2026-10-05 15:00:00")]
ok, why = seed.phone_still_textable("r-c1", "9185550162")
check("send-time: spoke since queueing -> cancelled", ok is False and "spoke" in why, why)
LOGS.pop("r-c1")
ok, why = seed.phone_still_textable("r-c1", "9185550162")
check("send-time: call log unreadable -> held", ok is None, why)
config.CORRECT_NUMBER_FIRST = False

# ---- a do-not-call flag skips that NUMBER, not the record (2026-10-05)
crm.resolve_preset = lambda title: ({"x": 1}, title)
crm.dial_tier_uuids = lambda *a, **k: {"Dial First": "t1"}
crm.fetch_cohort = lambda must, limit=0: [{
    "uuid": "r-dnc", "address": {"street": "1 Test St"}, "owner": {"first_name": "Ann"},
    "phone": {"number": "9185550170", "doNotCall": True, "tags": ["t1"], "type": "MOBILE"}}]
config.DNC_TRY_OTHER_NUMBERS = False
stats = {}
rows, _ = seed.from_preset("FTM", keep_unresolved=True, stats=stats)
check("switch off: flagged search phone drops the record (upstream)", rows == [], str(rows))
config.DNC_TRY_OTHER_NUMBERS = True
stats = {}
rows, _ = seed.from_preset("FTM", keep_unresolved=True, stats=stats)
check("switch on: record kept for its other numbers",
      len(rows) == 1 and rows[0].get("_needs_best_phone") == "phone flagged do-not-call", str(rows))
check("flagged number still collected as DNC", "9185550170" in stats.get("_dnc_numbers", set()))
RECS["r-dnc"] = _rec(("9185550170", "UNKNOWN", "MOBILE", ["Dial First"]),
                     ("9185550171", "UNKNOWN", "MOBILE", ["Dial Second"]))
crm.phone_is_dnc = lambda phone: None   # other numbers' flags are invisible
got, why = seed.resolve_best_phone(rows[0], {"9185550170"})
check("best OTHER number picked, flagged one skipped", bool(got) and got["phone"] == "9185550171", str((got, why)))
config.DNC_TRY_OTHER_NUMBERS = False

# ---- a flagged Correct number: no text, one board note saying why
config.CORRECT_NUMBER_FIRST = True
RECS["r-cd"] = _rec(("9185550180", "CORRECT", "MOBILE", ["Dial First"]),
                    ("9185550181", "UNKNOWN", "MOBILE", ["Dial First"]))
LOGS["r-cd"] = []
got, why = seed.apply_correct_number({"uuid": "r-cd", "phone": "9185550180"}, {"9185550180"})
check("flagged Correct number -> no text, own reason", got is None and why == seed.CORRECT_DNC_REASON, why)
config.CORRECT_NUMBER_FIRST = False

NOTES_POSTED = []
crm.post_note = lambda uuid, text, pinned=False: NOTES_POSTED.append(uuid) or {"ok": True}
note = [("r-cd", seed.correct_dnc_note({}))]
config.DRY_RUN = True
r = seed.post_skip_notes(note)
check("dry run: board note not posted", NOTES_POSTED == [] and r["posted"] == 0, str(r))
config.DRY_RUN = False
crm.post_note = lambda uuid, text, pinned=False: {"error": "boom"}
r = seed.post_skip_notes(note)
check("failed post is not marked (retries tomorrow)", r["failed"] == 1 and not store.get_meta("skip-note:r-cd"), str(r))
crm.post_note = lambda uuid, text, pinned=False: NOTES_POSTED.append(uuid) or {"ok": True}
seed.post_skip_notes(note)
check("board note posted once", NOTES_POSTED == ["r-cd"], str(NOTES_POSTED))
seed.post_skip_notes(note)
check("never posted twice for the same record", NOTES_POSTED == ["r-cd"], str(NOTES_POSTED))
check("note names the reason and has no dashes",
      "do-not-call" in note[0][1] and "—" not in note[0][1] and "–" not in note[0][1])
config.DRY_RUN = True

# ---- text EVERY qualifying number; ignore DataSift's do-not-call flag
RECS["r-all"] = _rec(("9185550190", "UNKNOWN", "MOBILE", ["Dial Second"]),
                     ("9185550191", "UNKNOWN", "MOBILE", ["Dial First"]),
                     ("9185550192", "WRONG", "MOBILE", ["Dial First"]),
                     ("9185550193", "DNC", "MOBILE", ["Dial First"]),
                     ("9185550194", "UNKNOWN", "LANDLINE", ["Dial First"]),
                     ("9185550195", "UNKNOWN", "MOBILE", ["Dial Third"]),
                     ("9185550196", "UNKNOWN", "UNKNOWN", ["Dial First"]),
                     ("9185550197", "UNKNOWN", "MOBILE", ["Dial First"]))
config.IGNORE_DNC_FLAG = False
got = [r["phone"] for r in seed.all_textable_rows({"uuid": "r-all"}, {"9185550197"})]
check("all numbers: Dial First mobile, then unknown, then Second; bad status/landline/Third out; flag honoured",
      got == ["9185550191", "9185550196", "9185550190"], str(got))
config.IGNORE_DNC_FLAG = True
got = [r["phone"] for r in seed.all_textable_rows({"uuid": "r-all"}, {"9185550197"})]
check("ignore flag: the flagged Dial First mobile is texted too",
      got == ["9185550191", "9185550197", "9185550196", "9185550190"], str(got))
check("ignore flag never un-blocks a DNC / WRONG status", "9185550192" not in got and "9185550193" not in got)
crm.fetch_cohort = lambda must, limit=0: [{
    "uuid": "r-dnc", "address": {"street": "1 Test St"}, "owner": {"first_name": "Ann"},
    "phone": {"number": "9185550170", "doNotCall": True, "tags": ["t1"], "type": "MOBILE"}}]
rows, _ = seed.from_preset("FTM", keep_unresolved=True, stats={})
check("ignore flag: flagged search phone kept as a normal row",
      len(rows) == 1 and not rows[0].get("_needs_best_phone"), str(rows))
config.IGNORE_DNC_FLAG = False

# ---- NumberVerifier carrier flags: >1 flagged carrier = number pulled
from sms_agent import number_health, sender_pool  # noqa: E402
import datetime as _dt  # noqa: E402
TODAY = _dt.date.today().isoformat()
OLD = (_dt.date.today() - _dt.timedelta(days=9)).isoformat()
ALERTS = []
escalate.alert = lambda title, detail="", **k: ALERTS.append(title) or True
config.NUMBER_HEALTH_MAX_FLAGS = 1
SNAP = {"9180000001": {"day": TODAY, "flags": [0, 0, 0]},
        "9180000002": {"day": TODAY, "flags": [1, 0, 0]},
        "9180000003": {"day": TODAY, "flags": [1, 1, 0]},
        "9180000004": {"day": OLD, "flags": [0, 0, 0]}}
store.set_meta(number_health.CACHE_KEY, "")
number_health.fetch = lambda: SNAP
check("0 flags -> may send", number_health.check("+19180000001") == (True, ""))
check("1 flag -> may send (more than 1 pulls)", number_health.check("9180000002")[0] is True)
ok, why = number_health.check("9180000003")
check("2 flags -> pulled, names the carriers", ok is False and "AT&T, T-Mobile" in why, why)
check("pulled number alerts #SMS once", ALERTS.count("SMS agent: sending number pulled") == 1, str(ALERTS))
number_health.check("9180000003")
check("...and only once a day", ALERTS.count("SMS agent: sending number pulled") == 1, str(ALERTS))
check("stale data -> hold", number_health.check("9180000004")[0] is None)
check("unmonitored number -> hold", number_health.check("9180000009")[0] is None)


def _boom():
    raise RuntimeError("login failed")


store.set_meta(number_health.CACHE_KEY, "")
number_health.fetch = _boom
ok, why = number_health.check("9180000001")
check("NumberVerifier unreadable -> hold", ok is None and "unreadable" in why, why)
check("unreadable alerts #SMS", "SMS agent: number health check is failing" in ALERTS, str(ALERTS))
number_health.fetch = lambda: SNAP
check("failure is cached for the hour (no login storm)", number_health.check("9180000001")[0] is None)
store.set_meta(number_health.CACHE_KEY, "")
import importlib  # noqa: E402
sender_pool = importlib.reload(sender_pool)  # an earlier test stubbed available()
config.NUMBER_HEALTH_CHECK = True
check("available(): flagged number refused", sender_pool.available("9180000003")[0] is False)
check("available(): clean number allowed", sender_pool.available("9180000001")[0] is True)
config.NUMBER_HEALTH_CHECK = False

print()
print(f"{len(FAILS)} failed" if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
