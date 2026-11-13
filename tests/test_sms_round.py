"""Offline: Tier 2 SMS Prospecting, touches 5-10 (Jeff, 2026-11-13).
Throwaway DB and run folder, every CRM call stubbed. Sends nothing.

    python tests/test_sms_round.py
"""
import csv
import importlib.util
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="sms_round_test_"))
RUNS = TMP / "runs"
os.environ.update({
    "SMS_AGENT_DB": str(TMP / "t.db"),
    "SMS_AGENT_DATA_DIR": str(TMP),
    "SMS_AGENT_DRY_RUN": "1",
    "SMS_AGENT_STOP_ON_ANY_REPLY": "1",
    "SMS_AGENT_TOUCH_SOURCE": "pool",
    "SMS_AGENT_SLACK_WEBHOOK": "",
    "SMS_AGENT_SLACK_FALLBACK": "0",
    "SMS_AGENT_SENDER_NAME": "",
    "SMS_AGENT_DP_RUNS_DIR": str(RUNS),
    "SMS_AGENT_CAMPAIGN_SOURCES": "09 DP R2C|0.5|deep|newnums;FTM- 02 Ready to Call|0.5|deep",
    "SMS_AGENT_CORRECT_NUMBER_FIRST": "1",
    "SMS_AGENT_ROUND_ON": "0",
    "SMS_AGENT_ROUND_PRESET": "Tier 2 SMS Prospecting",
    "SMS_AGENT_TOUCH4_V2": "0",
    "SMS_AGENT_ROUND_TOUCH4_AFTER": "2026-11-01",
    "ANTHROPIC_API_KEY": "",
    "SMRTPHONE_NUMBERS": '["+19180000001"]',
})
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sms_agent import campaign, config, crm, death_check, respond, round2, seed, store  # noqa: E402
from sms_agent.knowledge import touches  # noqa: E402

death_check.forbidden_names = lambda u, rec=None: set()
config.TOUCH_SOURCE = "pool"
config.IGNORE_DNC_FLAG = True

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


store.init()

THU, FRI, SAT = date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 14)
MON, TUE, WED = date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)


def sent(phone, touch, when, body=None):
    """A text we sent, stored the way the worker stores it."""
    body = body or f"test touch {touch} body"
    with store.tx() as cx:
        cx.execute("INSERT INTO messages(phone, direction, body, from_number, author, intent, created_at)"
                   " VALUES (?,?,?,?,?,?,?)",
                   (phone, "out", body, "+19180000001", "seed", f"seed_touch_{touch}",
                    f"{when}T15:00:00+00:00"))


# ---- 1. the schedule: Mon/Wed/Fri, 2 days apart, stops after touch 10
e = {"touches": {1, 2, 3, 4}, "last": "2026-11-12T15:00:00+00:00", "t4": "2026-11-12T15:00:00+00:00",
     "promised": False}
check("touch 4 Thursday -> nothing Friday (1 day)", round2.next_round_touch(e, FRI)[0] is None)
check("nothing on a Saturday", round2.next_round_touch(e, SAT) == (None, "not a round day"))
check("touch 4 Thursday -> touch 5 Monday", round2.next_round_touch(e, MON)[0] == 5)
check("nothing on a Tuesday", round2.next_round_touch(e, TUE)[0] is None)
e5 = {"touches": {1, 2, 3, 4, 5}, "last": "2026-11-16T15:00:00+00:00", "t4": "2026-11-12T15:00:00+00:00",
      "promised": False}
check("touch 5 Monday -> touch 6 Wednesday", round2.next_round_touch(e5, WED)[0] == 6)
check("touches 1-4 not finished -> no round",
      round2.next_round_touch({"touches": {1, 2}, "last": ""}, MON)[1] == round2.NOT_FINISHED_1_4)
check("touch 4 that promised -> never",
      round2.next_round_touch(dict(e, promised=True), MON)[1] == round2.PROMISED)
check("after touch 10 -> finished",
      round2.next_round_touch({"touches": set(range(1, 11)), "last": "2026-11-13",
                               "t4": "2026-11-02"}, MON)[1]
      == round2.FINISHED)
check("touch 4 before the cutover (the backlog) -> never",
      round2.next_round_touch(dict(e, t4="2026-10-30T15:00:00+00:00"), MON)[1] == round2.BACKLOG)
check("only our seed_touch_ intents count", round2.touch_of("seed_touch_7") == 7
      and round2.touch_of("dispo_touch_7") == 0 and round2.touch_of("") == 0)
check("6 round texts by default", round2.round_count() == 6 and round2.last_round_touch() == 10)

# ---- 2. history: stored touch number beats the wording, promises are caught
P1 = "9185550201"
sent(P1, 4, "2026-11-12", "Hey Ann, last one from me, I promise! If selling 1 Main St is ever on your mind...")
P2 = "9185550202"
sent(P2, 4, "2026-11-12", "Hi Bo, I've sent a few texts about 1 Main St and haven't heard back.")
# A round text that reads like touch 1: counted as 5 because of its stored number.
sent(P2, 5, "2026-11-16", "Hi Bo! I hope your week is going great. My name is Diego, I was looking at 1 Main St")
h = round2.history()
check("promise wording marks the number", h[P1]["promised"] is True)
check("plain touch 4 is not a promise", h[P2]["promised"] is False)
check("stored touch number wins over similar wording", max(h[P2]["touches"]) == 5, h[P2])
for line in ("Hey X, last one from me, I promise!", "Hi, Diego here one last time about 1 Main St.",
             "Hey there, last text from me!", "Hi X, I'll stop bugging you after this!",
             "Hi X, Diego here. I'll stop texting after this one.", "Hey X, last note from me about 1 Main"):
    check(f"promise caught: {line[:28]}", touches.promised_stop(line))

# ---- 3. wording: every new line passes the same gates as touches 1-4
bad = []
for name, pool in (("TOUCH4_V2", touches.TOUCH4_V2), ("TOUCH4_V2_NONAME", touches.TOUCH4_V2_NONAME),
                   ("HEIR_TOUCH4_V2", touches.HEIR_TOUCH4_V2),
                   ("HEIR_TOUCH4_V2_NONAME", touches.HEIR_TOUCH4_V2_NONAME)):
    for tpl in pool:
        if touches.promised_stop(tpl):
            bad.append(f"{name} promises: {tpl}")
        txt = tpl.format(first="Ann", addr="12345 E 21st St", city="Tulsa", sender="Diego")
        if campaign.identify_touch(txt) != 4:
            bad.append(f"{name} not recognised as touch 4: {tpl}")
for variant, pools in touches.ROUND_POOLS.items():
    for t, (named, noname) in enumerate(pools, start=5):
        for tpl in list(named) + list(noname):
            txt = tpl.format(first="Ann", addr="12345 E 21st St", city="Tulsa", sender="Diego")
            ok, problems = respond.validate(txt, max_questions=2, allowed_address="12345 E 21st St")
            if not ok:
                bad.append(f"{variant} {t}: {problems} -> {tpl}")
            if touches.promised_stop(tpl):
                bad.append(f"{variant} {t} promises: {tpl}")
            low = tpl.lower()
            if "local buyer" in low or "last" in low.replace("last couple", "").replace("last few", ""):
                bad.append(f"{variant} {t} says local buyer / last: {tpl}")
            if variant == "heir" and any(w in low for w in ("passed", "estate", "probate", "loss", "sorry for")):
                bad.append(f"{variant} {t} hints at a death: {tpl}")
            if variant == "known" and "yours" in low:
                bad.append(f"known {t} asks whether it is theirs: {tpl}")
            if "{sender}" not in tpl:
                bad.append(f"{variant} {t} unsigned: {tpl}")
check("all round + touch 4 v2 lines pass every gate", not bad, "; ".join(bad[:3]))
check("each round pool has 6 touches", all(len(p) == 6 for p in touches.ROUND_POOLS.values()))

for variant in ("owner", "known", "heir"):
    for t in range(5, 11):
        msg = touches.render(t, "seed", "Ann", "1 Main St", "Tulsa", "Diego", variant=variant)
        if not msg or "{" in msg:
            bad.append(f"render {variant} {t}")
check("render covers touches 5-10 for every variant", not bad)
check("touch 4 renders the live wording while TOUCH4_V2 is off",
      touches.render(4, "x", "Ann", "1 Main St", "Tulsa", "Diego")
      in [s.format(first="Ann", addr="1 Main St", city="Tulsa", sender="Diego") for s in touches.TOUCH4])
config.TOUCH4_V2 = True
check("touch 4 renders the v2 wording when switched on",
      touches.render(4, "x", "Ann", "1 Main St", "Tulsa", "Diego")
      in [s.format(first="Ann", addr="1 Main St", city="Tulsa", sender="Diego") for s in touches.TOUCH4_V2])
config.TOUCH4_V2 = False

# ---- 4. the skill copy matches the agent copy
spec = importlib.util.spec_from_file_location(
    "ttb", ROOT / "skills" / "text-touch-builder" / "scripts" / "build_text_touches.py")
ttb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ttb)
same = all([
    ttb.TOUCH4_V2 == touches.TOUCH4_V2, ttb.TOUCH4_V2_NONAME == touches.TOUCH4_V2_NONAME,
    ttb.HEIR_TOUCH4_V2 == touches.HEIR_TOUCH4_V2, ttb.HEIR_TOUCH4_V2_NONAME == touches.HEIR_TOUCH4_V2_NONAME,
    [(list(a), list(b)) for a, b in ttb.ROUND_UNKNOWN] == [(list(a), list(b)) for a, b in touches.ROUND_UNKNOWN],
    [(list(a), list(b)) for a, b in ttb.ROUND_KNOWN] == [(list(a), list(b)) for a, b in touches.ROUND_KNOWN],
    [(list(a), list(b)) for a, b in ttb.ROUND_HEIR] == [(list(a), list(b)) for a, b in touches.ROUND_HEIR],
])
check("text-touch-builder skill pools match the agent's", same)
check("skill pool audit passes", not ttb.audit_pools(), ttb.audit_pools()[:2])

# ---- 5. the status: not blocked, not a lead
check("round status is not blocked by the Mail Only / DNC words",
      not seed.lead_status_blocks(config.ROUND_STATUS))
kpi_leads = [s.lower() for s in ["Lead", "New Lead", "new_lead", "No Contact New Lead", "Nurture New Lead",
                                 "Cold Lead", "Warm Lead", "Hot Lead", "Ghosting Lead"]]
kpi_src = (ROOT / "kpi-engine" / "pull_kpis.py").read_text(encoding="utf-8")
check("round status is not one of the KPI lead statuses",
      config.ROUND_STATUS.lower() not in kpi_leads and f'"{config.ROUND_STATUS}"' not in kpi_src)
check("status match ignores case", round2.is_round_status({"title": "tier 2 sms prospecting"}))

# ---- 6. vetting a record with the status (FTM record, no deep prospect)
REC = "prop-r2-1"
STREET = "12308 N 107th East Pl"


def ph(n, *tags, status="UNKNOWN", typ="MOBILE"):
    return {"number": n, "type": typ, "status": status, "tags": [{"title": t} for t in tags]}


A, B, C = "9185550301", "9185550302", "9185550303"
PHONES = [ph(A, "Tracerfy", "Dial First"), ph(B, "DataSift", "Dial Second"), ph(C, "Tracerfy", "Dial Fourth")]
RECORD = {"uuid": REC, "status": {"title": config.ROUND_STATUS},
          "owner": {"first_name": "Mark", "last_name": "Hayes", "phones": PHONES},
          "secondary_owners": []}
RECORDS = {REC: RECORD}
EVENTS: dict = {REC: []}
crm.client = lambda: object()
crm.get_record = lambda uuid, fresh=False: RECORDS.get(uuid)
crm.activity_log = lambda uuid: EVENTS.get(uuid, [])
crm.dial_tier_checked = lambda uuid, phone: (next(
    (t["title"] for r in RECORDS.values() for p in r["owner"]["phones"] if p["number"] == phone
     for t in p["tags"] if t["title"].startswith("Dial ")), ""), True)
crm.deal_context = lambda uuid: {"county": "Tulsa"}
crm.find_phone_object = lambda uuid, phone, fresh=False: ("", None)

for p in (A, B):
    for t, d in ((1, "2026-11-09"), (2, "2026-11-10"), (3, "2026-11-11"), (4, "2026-11-12")):
        sent(p, t, d, body=f"plain touch {t} to {p}")
ROW = {"uuid": REC, "street": STREET, "city": "Tulsa", "first": "Mark", "last": "Hayes",
       "owner": "Mark Hayes", "assigned": "Diego", "phone": A}


def vet(row=ROW, today=MON, busy=frozenset(), dp_ok=True, already=frozenset()):
    return round2._vet_record(dict(row), RECORDS[row["uuid"]], EVENTS.get(row["uuid"], []),
                              round2.history(), today, set(), set(busy), dp_ok, {}, set(already))


cands, why, fin = vet()
got = {c.phone: c for c in cands}
check("both Dial First/Second numbers get touch 5", set(got) == {A, B} and
      all(c.touch == 5 for c in cands), (sorted(got), why))
check("the Dial Fourth number is left out", C not in got)
check("touch 5 is the unknown-number copy, signed by the assignee",
      got[A].message in [s.format(first="Mark", addr=STREET, city="Tulsa", sender="Diego")
                         for s in touches.ROUND_UNKNOWN[0][0]] and "Diego" in got[A].message,
      got[A].message)
check("a number already texted by touches 1-4 today is skipped",
      {c.phone for c in vet(already={A})[0]} == {B})

RECORD["status"] = {"title": "Not Interested"}
check("status changed -> held", vet()[0] == [] and "status is now Not Interested" in vet()[1])
RECORD["status"] = {"title": config.ROUND_STATUS}

EVENTS[REC] = [{"event_type": "owner.call.answered", "timestamp": "2026-11-13 15:00:00",
                "payload": {"call": {"direction": "inbound", "origin_number": B}}}]
check("they called in and we answered -> held", vet()[0] == [] and "talked to them" in vet()[1], vet()[1])
PHONES[1]["status"] = "WRONG"
check("a talk on a number since marked Wrong is a stranger, not contact",
      "talked to them" not in vet()[1] and {c.phone for c in vet()[0]} == {A}, vet()[1])
PHONES[1]["status"] = "UNKNOWN"
EVENTS[REC] = []

store.set_meta(f"record_replied:{REC}", B)
check("a reply that settled the record -> held", vet()[0] == [] and "settled" in vet()[1])
store.set_meta(f"record_replied:{REC}", "")

check("record still in a DP call preset -> held",
      vet(busy={REC})[0] == [] and "deep prospect call attempts" in vet(busy={REC})[1])

# Correct + primary: only that number, and the "known" copy.
PHONES[1]["status"] = "CORRECT"
RECORD["owner"]["primary_phone"] = B
cands, why, _ = vet()
check("Correct + primary narrows to that number", [c.phone for c in cands] == [B], (why,))
check("a Correct number gets the known copy (no 'is it yours')",
      cands and cands[0].message in [s.format(first="Mark", addr=STREET, city="Tulsa", sender="Diego")
                                     for s in touches.ROUND_KNOWN[0][0]], cands and cands[0].message)
PHONES[1]["status"] = "UNKNOWN"
RECORD["owner"]["primary_phone"] = ""

# A number whose touch 4 promised to stop never gets the round.
P_PROM = "9185550304"
PHONES.append(ph(P_PROM, "Tracerfy", "Dial First"))
for t, d in ((1, "2026-11-09"), (2, "2026-11-10"), (3, "2026-11-11")):
    sent(P_PROM, t, d)
sent(P_PROM, 4, "2026-11-12", "Hey Mark, last one from me, I promise! If selling ... Take care! Diego")
check("promised number left out, the others still go",
      {c.phone for c in vet()[0]} == {A, B})
PHONES.pop()

# ---- 7. a deep-prospected record after its DP week: old AND new numbers
DREC = "prop-r2-dp"
DSTREET = "4527 W Madison Pl"
folder = RUNS / "4527-w-madison-pl"
folder.mkdir(parents=True)
(folder / "state.json").write_text(json.dumps({
    "street": DSTREET, "city": "Tulsa", "property_uuid": DREC,
    "started": "2026-11-02", "export": "smartskip_export.csv"}), encoding="utf-8")
(folder / "obituary_check.json").write_text(json.dumps({"summary": "NO OBITUARY FOUND"}), encoding="utf-8")
with (folder / "smartskip_export.csv").open("w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["First Name", "Last Name", "Relationship", "Phone 1 number", "Phone 1 type"])
    w.writerow(["Ruth", "Okafor", "Subject", "918-555-0401", "Mobile"])
    w.writerow(["Grace", "Okafor", "Daughter", "918-555-0402", "Mobile"])
    w.writerow(["Paul", "Okafor", "Son", "918-555-0403", "Mobile"])
OLD, NEW_OWNER, NEW_REL, NEW_MID = "9185550400", "9185550401", "9185550402", "9185550403"
DPHONES = [ph(OLD, "Tracerfy", "Dial First"), ph(NEW_OWNER, "SmartSkip", "Dial First"),
           ph(NEW_REL, "SmartSkip", "Dial Second", "Daughter"), ph(NEW_MID, "SmartSkip", "Dial First")]
RECORDS[DREC] = {"uuid": DREC, "status": {"title": config.ROUND_STATUS},
                 "owner": {"first_name": "Ruth", "last_name": "Okafor", "phones": DPHONES},
                 "secondary_owners": []}
EVENTS[DREC] = []
for t, d in ((1, "2026-10-29"), (2, "2026-10-30"), (3, "2026-10-31"), (4, "2026-11-01")):
    sent(OLD, t, d)                                   # old number, before the DP
for p in (NEW_OWNER, NEW_REL):
    for t, d in ((1, "2026-11-09"), (2, "2026-11-10"), (3, "2026-11-11"), (4, "2026-11-12")):
        sent(p, t, d)                                 # new numbers, during the DP week
sent(NEW_MID, 1, "2026-11-12")                        # a new number still mid touches 1-4
DROW = {"uuid": DREC, "street": DSTREET, "city": "Tulsa", "first": "Ruth", "last": "Okafor",
        "owner": "Ruth Okafor", "assigned": "Diego", "phone": OLD}
from sms_agent import dp_numbers  # noqa: E402
config.DP_RUNS_DIR = ""                              # no run folders at all
_nr, _nr_why, _ = round2._vet_record(dict(DROW), RECORDS[DREC], [], round2.history(), MON,
                                     set(), set(), True, {}, set())
_nr = {c.phone: c for c in _nr}
check("no run folder: the record is still texted", dp_numbers.run_for(DREC) is None
      and {OLD, NEW_OWNER, NEW_REL} <= set(_nr), (sorted(_nr), _nr_why))
check("no run folder: SmartSkip numbers get no name, never the owner's",
      all("Ruth" not in _nr[p].message for p in (NEW_OWNER, NEW_REL) if p in _nr),
      [_nr[p].message for p in (NEW_OWNER, NEW_REL) if p in _nr])
check("no run folder: the owner's own old number keeps her name",
      OLD in _nr and "Ruth" in _nr[OLD].message, OLD in _nr and _nr[OLD].message)
config.DP_RUNS_DIR = str(RUNS)
dp_numbers._index["at"] = 0                          # the folder exists now; reload it
texted = {OLD: "2026-10-29", NEW_OWNER: "2026-11-09", NEW_REL: "2026-11-09", NEW_MID: "2026-11-12"}
cands, why, _ = round2._vet_record(dict(DROW), RECORDS[DREC], [], round2.history(), MON,
                                   set(), set(), True, texted, set())
got = {c.phone: c for c in cands}
check("after the DP week the OLD number gets the round", OLD in got, why)
check("the new numbers that finished touch 4 get the round", {NEW_OWNER, NEW_REL} <= set(got), why)
check("a new number still mid touches 1-4 waits", NEW_MID not in got)
# Jeff 2026-10-07: a wrong-number reply stops only that number.
store.ensure_conversation(OLD, from_number="+19180000001", record_uuid=DREC)
store.update_conversation(OLD, last_inbound=store.now())
cands2, why2, _ = round2._vet_record(dict(DROW), RECORDS[DREC], [], round2.history(), MON,
                                     set(), set(), True, texted, set())
got2 = {c.phone for c in cands2}
check("an old number replied (wrong number): only that number stops",
      OLD not in got2 and {NEW_OWNER, NEW_REL} <= got2, (sorted(got2), why2))
check("send time: the number that replied is cancelled", round2.still_ok(DREC, OLD)[0] is False)
check("send time: the record's other numbers still go", round2.still_ok(DREC, NEW_OWNER) == (True, ""))
store.update_conversation(OLD, last_inbound=None)
check("the relative's number gets heir copy by her own name",
      NEW_REL in got and "Grace" in got[NEW_REL].message
      and got[NEW_REL].message in [s.format(first="Grace", addr=DSTREET, city="Tulsa", sender="Diego")
                                   for s in touches.ROUND_HEIR[0][0]], got.get(NEW_REL) and got[NEW_REL].message)
check("the owner's old number gets owner copy by her name",
      OLD in got and "Ruth" in got[OLD].message, got.get(OLD) and got[OLD].message)
(folder / "obituary_check.json").write_text(json.dumps({"summary": "LIKELY THE OWNER'S OBITUARY"}),
                                            encoding="utf-8")
dp_numbers._index["at"] = 0                           # reload the run folder
cands, _, _ = round2._vet_record(dict(DROW), RECORDS[DREC], [], round2.history(), MON,
                                 set(), set(), True, texted, set())
got = {c.phone: c for c in cands}
check("owner maybe dead: her old number gets heir copy and no name",
      OLD in got and "Ruth" not in got[OLD].message
      and got[OLD].message in [s.format(first="", addr=DSTREET, city="Tulsa", sender="Diego")
                               for s in touches.ROUND_HEIR[0][1]], got.get(OLD) and got[OLD].message)
check("could not read the DP presets -> a DP record is held",
      round2._vet_record(dict(DROW), RECORDS[DREC], [], round2.history(), MON,
                         set(), set(), False, texted, set())[0] == [])

# ---- 8. send-time re-check
check("send time: all good", round2.still_ok(REC, A) == (True, ""))
RECORD["status"] = {"title": "Warm Lead"}
ok, why = round2.still_ok(REC, A)
check("send time: status moved -> cancel", ok is False and "Warm Lead" in why, why)
RECORD["status"] = {"title": config.ROUND_STATUS}
check("send time: record unreadable -> hold", round2.still_ok("nope", A)[0] is None)
crm.activity_log = lambda uuid: None
check("send time: activity log unreadable -> hold", round2.still_ok(REC, A)[0] is None)
crm.activity_log = lambda uuid: EVENTS.get(uuid, [])

# ---- 9. first round days stay held
check("day 1 held", not round2.release_allowed(round2.note_staged_day(MON)))
check("day 2 held", not round2.release_allowed(round2.note_staged_day(WED)))
check("same day twice still counts once", round2.note_staged_day(WED) == 2)
check("day 3 releases", round2.release_allowed(round2.note_staged_day(date(2026, 11, 20))))

# ---- 10. finished records are listed once
plan = round2.RoundPlan(finished=[(REC, STREET)])
round2.claim_finished(plan)
check("finished record listed the first time", plan.finished == [(REC, STREET)])
plan = round2.RoundPlan(finished=[(REC, STREET)])
round2.claim_finished(plan)
check("and not again", plan.finished == [])

# ---- 11. the scheduler: round texts staged HELD on the first round days
from sms_agent import escalate, scheduler  # noqa: E402

POSTS = []
escalate.alert = lambda title, detail="", **kw: POSTS.append((title, detail)) or True
round_cands, _, _ = vet()                              # two touch-5 texts from section 6
campaign.build = lambda **kw: campaign.Plan()          # touches 1-4: nothing today
round2.build = lambda today=None, already=None, **kw: round2.RoundPlan(
    candidates=list(round_cands), held=[("x", "1 Held St", "talked to them")],
    suggested=[("y", "2 Maybe Ave", "Correct number, never talked to them")])
config.ROUND_ON, config.DRY_RUN, config.CAMPAIGN_ENABLED = True, False, True
# The live .env's number-health switches need NumberVerifier; not tested here.
config.ONE_NUMBER_PER_DAY = config.DP_SPREAD_NUMBERS = config.NUMBER_HEALTH_CHECK = False
store.set_meta(round2.STAGED_DAYS_KEY, "[]")


def outbox(touch):
    return [dict(r) for r in store._conn().execute(
        "SELECT status FROM outbox WHERE intent=?", (f"seed_touch_{touch}",))]


res = scheduler.run(force=True)
check("round texts queued", res.get("queued") == 2, res)
check("first round day: staged HELD, not released",
      [r["status"] for r in outbox(5)] == ["held", "held"], outbox(5))
review = [p for p in POSTS if p[0].startswith("SMS Tier 2 round")]
check("review posted, saying held for review",
      review and "HELD for review" in review[0][0], [p[0] for p in POSTS])
check("review lists held and suggested records",
      review and "1 Held St: talked to them" in review[0][1] and "2 Maybe Ave" in review[0][1])
store.set_meta(round2.STAGED_DAYS_KEY, json.dumps(["2026-11-01", "2026-11-02"]))
with store.tx() as cx:
    cx.execute("DELETE FROM outbox")
    cx.execute("DELETE FROM notifications")
scheduler.run(force=True)
check("after the held days, round texts are released",
      sorted(r["status"] for r in outbox(5)) == ["queued", "queued"], outbox(5))
config.ROUND_ON = False
with store.tx() as cx:
    cx.execute("DELETE FROM outbox")
    cx.execute("DELETE FROM notifications")
POSTS.clear()
scheduler.run(force=True)
check("round OFF: nothing staged, no round post",
      not outbox(5) and not [p for p in POSTS if "Tier 2" in p[0]])

# Round texts take the slots AFTER touches 1-4 (those records are being called).
with store.tx() as cx:
    cx.execute("DELETE FROM outbox")
store.queue_message("9185550901", "round text", from_number="+19180000001", status="held",
                    intent="seed_touch_5", confidence=1.0)
store.queue_message("9185550902", "touch 4 text", from_number="+19180000001", status="held",
                    intent="seed_touch_4", confidence=1.0)
seed.reschedule_held()
nb = {r["intent"]: r["not_before"] for r in store._conn().execute("SELECT intent, not_before FROM outbox")}
check("touch 4 is laid out before a round text", nb["seed_touch_4"] < nb["seed_touch_5"], nb)
with store.tx() as cx:
    cx.execute("DELETE FROM outbox")
config.DRY_RUN = True

# ---- 12. off by default
check("round is OFF by default", config.ROUND_ON is False)
check("touch 4 v2 is OFF by default", os.environ["SMS_AGENT_TOUCH4_V2"] == "0" and not config.TOUCH4_V2)

print()
print(f"{len(FAILS)} failure(s)" if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
