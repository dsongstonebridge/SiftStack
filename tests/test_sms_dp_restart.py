"""Offline: deep-prospected records text ONLY their new SmartSkip numbers
(Jeff, 2026-10-26). Throwaway DB and run folder, every CRM call stubbed.

    python tests/test_sms_dp_restart.py
"""
import csv
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="sms_dp_test_"))
RUNS = TMP / "runs"
os.environ.update({
    "SMS_AGENT_DB": str(TMP / "t.db"),
    "SMS_AGENT_DATA_DIR": str(TMP),
    "SMS_AGENT_DRY_RUN": "1",
    "SMS_AGENT_STOP_ON_ANY_REPLY": "1",
    "SMS_AGENT_TOUCH_SOURCE": "pool",
    "SMS_AGENT_SLACK_WEBHOOK": "",
    "SMS_AGENT_SLACK_FALLBACK": "0",
    "SMS_AGENT_SENDER_NAME": "Diego",
    "SMS_AGENT_DP_RUNS_DIR": str(RUNS),
    "SMS_AGENT_CAMPAIGN_SOURCES": "09 DP R2C|0.5|deep|newnums;FTM- 02 Ready to Call|0.5|deep",
    "ANTHROPIC_API_KEY": "",
    "SMRTPHONE_NUMBERS": '["+19180000001"]',
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sms_agent import campaign, config, crm, dp_numbers, seed, store  # noqa: E402
from sms_agent import death_check  # noqa: E402

# Offline: no CRM to read, so the full death check would (correctly) allow no
# names at all. Sections 1-13 test the other rules; section 14 tests this one.
_real_forbidden = death_check.forbidden_names
death_check.forbidden_names = lambda u, rec=None: set()
from sms_agent.knowledge import touches  # noqa: E402

config.TOUCH_SOURCE = "pool"
config.IGNORE_DNC_FLAG = True

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


store.init()
REC = "prop-dp-1"
STREET = "10202 N 119th Ave E"

# ---- a deep prospect run folder, as deep_prospect_run.py writes it
folder = RUNS / "10202-n-119th-ave-e"
folder.mkdir(parents=True)
(folder / "state.json").write_text(json.dumps({
    "street": STREET, "city": "Owasso", "property_uuid": REC,
    "started": "2026-10-23", "export": "smartskip_export.csv"}), encoding="utf-8")
(folder / "obituary_check.json").write_text(json.dumps({"summary": "NO OBITUARY FOUND"}), encoding="utf-8")
with (folder / "smartskip_export.csv").open("w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["First Name", "Last Name", "Relationship", "Phone 1 number", "Phone 1 type",
                "Phone 2 number", "Phone 2 type", "Phone 3 number", "Phone 3 type"])
    w.writerow(["Lucas", "Salisbury", "Subject", "918-555-0101", "Mobile", "918-555-0102", "Mobile",
                "918-555-0105", "Mobile"])
    w.writerow(["Deborah", "Salisbury", "Spouse", "918-555-0103", "Mobile", "918-555-0104", "Mobile",
                "918-555-0107", "Mobile"])


def ph(n, *tags, status="UNKNOWN", typ="MOBILE"):
    return {"number": n, "type": typ, "status": status, "tags": [{"title": t} for t in tags]}


PHONES = [
    ph("9185550100", "Tracerfy", "Dial First"),                    # old skip trace
    ph("9185550101", "SmartSkip", "Dial First"),                   # NEW, owner
    ph("9185550102", "SmartSkip", "Pre-existing", "Dial First"),   # SmartSkip re-returned an old one
    ph("9185550103", "SmartSkip", "Dial Second", "Wife"),          # NEW, relative
    ph("9185550104", "SmartSkip", "Dial First"),                   # texted before -> old
    ph("9185550105", "SmartSkip", "Dial First"),                   # called before the DP -> old
    ph("9185550106", "SmartSkip", "Dial First"),                   # not in the export
    ph("9185550107", "SmartSkip", "Dial Fourth"),                  # new but not a good tier
]
RECORD = {"uuid": REC, "owner": {"first_name": "Lucas", "last_name": "Salisbury", "phones": PHONES},
          "secondary_owners": []}
EVENTS = [
    {"event_type": "owner.call.made", "timestamp": "2026-10-01 15:00:00",
     "payload": {"call": {"direction": "outbound", "destination_number": "9185550105"}}},
    {"event_type": "owner.call.made", "timestamp": "2026-10-24 15:00:00",   # after the DP: fine
     "payload": {"call": {"direction": "outbound", "destination_number": "9185550101"}}},
]

crm.client = lambda: object()
crm.get_record = lambda uuid, fresh=False: RECORD if uuid == REC else None
crm.activity_log = lambda uuid: EVENTS
crm.dial_tier_checked = lambda uuid, phone: (
    next((t["title"] for p in PHONES if p["number"] == phone for t in p["tags"]
          if t["title"].startswith("Dial ")), ""), True)
crm.deal_context = lambda uuid: {"county": "Tulsa"}
crm.find_phone_object = lambda uuid, phone, fresh=False: ("", None)

# ---- 1. which numbers are new
v = dp_numbers.classify(RECORD, dp_numbers.run_for(REC), {"9185550104": "2026-10-01", "9185550101": "2026-10-26"}, EVENTS)
new = dp_numbers.new_numbers(v)
check("new SmartSkip numbers are new", {"9185550101", "9185550103"} <= new, sorted(new))
check("old Tracerfy number is old", v["9185550100"] != "")
check("old number SmartSkip re-returned is old", "before SmartSkip" in v["9185550102"], v["9185550102"])
check("a number we texted before the DP is old", "texted 2026-10-01" in v["9185550104"], v["9185550104"])
check("a number called before the DP is old", "called 2026-10-01" in v["9185550105"], v["9185550105"])
check("a call after the DP does not make it old", v["9185550101"] == "")
check("this campaign's own texts (after the DP) do not make it old", v["9185550101"] == "")
EV2 = EVENTS + [{"event_type": "owner.phones.added", "timestamp": "2026-07-08 13:08:21",
                 "payload": {"phones": ["9185550103"]}}]
v2 = dp_numbers.classify(RECORD, dp_numbers.run_for(REC), {}, EV2)
check("a number added to the record before the DP is old (no source tag)",
      "before the deep prospect" in v2["9185550103"], v2["9185550103"])
config.DP_ALLOW_NUMBERS = {"9185550103"}
v3 = dp_numbers.classify(RECORD, dp_numbers.run_for(REC), {}, EV2)
check("a number Jeff cleared by hand is new", v3["9185550103"] == "")
config.DP_ALLOW_NUMBERS = set()
check("a SmartSkip tag not in the export is old", "not in this record" in v["9185550106"])
nr = dp_numbers.classify(RECORD, None, set(), None)
check("no run folder -> nothing is new", not dp_numbers.new_numbers(nr)
      and set(nr.values()) == {dp_numbers.NO_RUN_REASON})

# ---- 2. only the new Dial First/Second numbers get rows
rows = seed.all_textable_rows({"uuid": REC}, set(), only_numbers=new)
check("expansion keeps only new good-tier numbers",
      [r["phone"] for r in rows] == ["9185550101", "9185550103"], [r["phone"] for r in rows])
check("without the limit the old numbers would be in", "9185550100" in
      [r["phone"] for r in seed.all_textable_rows({"uuid": REC}, set())])

# ---- 3. Correct numbers
PHONES[0]["status"] = "CORRECT"                     # an OLD number marked Correct
RECORD["owner"]["primary_phone"] = "9185550100"     # Correct + primary (Jeff 2026-10-29)
got, why = seed.apply_correct_number({"uuid": REC, "phone": "9185550100"}, set(), only_numbers=new)
check("only an old Correct number -> held for review", got is None and why == seed.OLD_CORRECT_REASON, why)
PHONES[0]["status"] = "UNKNOWN"
PHONES[3]["status"] = "CORRECT"                     # a NEW number marked Correct
RECORD["owner"]["primary_phone"] = "9185550103"
crm.activity_log = lambda uuid: []
got, why = seed.apply_correct_number({"uuid": REC, "phone": "9185550100"}, set(), only_numbers=new)
check("a new Correct number wins", got is not None and got["phone"] == "9185550103", why)
PHONES[3]["status"] = "UNKNOWN"
crm.activity_log = lambda uuid: EVENTS

# ---- 4. greeting + copy
first, variant = dp_numbers.addressee(RECORD, dp_numbers.run_for(REC), "9185550101")
check("the owner's own new number gets owner copy", (first, variant) == ("Lucas", "owner"))
first, variant = dp_numbers.addressee(RECORD, dp_numbers.run_for(REC), "9185550103")
check("a relative gets heir copy and their own name", (first, variant) == ("Deborah", "heir"))
RECORD["secondary_owners"] = [{"first_name": "Deborah", "last_name": "Salisbury"}]
check("a spouse on title gets owner copy",
      dp_numbers.addressee(RECORD, dp_numbers.run_for(REC), "9185550103")[1] == "owner")
RECORD["secondary_owners"] = []

base = {"uuid": REC, "street": STREET, "city": "Owasso", "first": "Lucas", "last": "Salisbury",
        "owner": "Lucas Salisbury", "assigned": "Diego", "_dp_new": new}
built = seed.build([dict(base, phone="9185550103"), dict(base, phone="9185550101"),
                    dict(base, phone="9185550100")], touch=1)
by = {c.phone: c for c in built}
check("relative touch 1 is the heir copy, by her name",
      by["9185550103"].status == "ready" and "Deborah" in by["9185550103"].message
      and campaign.identify_touch(by["9185550103"].message) == 1, by["9185550103"].message)
check("owner touch 1 is the standard copy",
      by["9185550101"].status == "ready" and "Lucas" in by["9185550101"].message
      and "Deborah" not in by["9185550101"].message, by["9185550101"].message)
check("an old number that slips through is held",
      by["9185550100"].status == "hold" and "old number" in by["9185550100"].reasons[0])

# ---- 5. replies
store.set_meta(f"record_replied:{REC}", "9185550100")      # settled from an OLD number
c = seed.build([dict(base, phone="9185550101")], touch=1)[0]
check("old-number settle holds the record for review",
      c.status == "hold" and "before the deep prospect" in c.reasons[0], c.reasons)
store.set_meta(f"record_replied:{REC}", "9185550103")      # settled from a NEW number
c = seed.build([dict(base, phone="9185550101")], touch=1)[0]
check("new-number settle stops the record", c.status == "hold" and "already replied" in c.reasons[0])
store.set_meta(f"record_replied:{REC}", "")

# ---- 6. sending number: a fresh start, not the old FTM number
with store.tx() as cx:
    for p in ("9185550100", "9185550101"):
        cx.execute("INSERT INTO phone_map(phone, record_uuid, updated_at) VALUES (?,?,?)",
                   (p, REC, store.now()))
store.record_send("+19180000099", "9185550100")              # FTM texted the old number
check("plain lookup still finds the FTM number", store.record_from_number(REC) == "+19180000099")
check("DP lookup ignores the FTM number", store.record_from_number(REC, new) == "")
store.record_send("+19180000001", "9185550101")
check("DP lookup follows the number that texted a new phone",
      store.record_from_number(REC, new) == "+19180000001")

# ---- 7. the touch history recognises every owner AND heir variant
bad = []
for pools in (touches.POOLS, touches.HEIR_POOLS):
    for t, (pool, noname) in enumerate(pools, start=1):
        for tpl in list(pool) + list(noname):
            txt = tpl.format(first="Ann", addr=STREET, city="Tulsa", sender="Diego")
            if campaign.identify_touch(txt) != t:
                bad.append((t, txt[:50], campaign.identify_touch(txt)))
check("every variant maps to its own touch", not bad, bad[:3])

# ---- 8. the source flag parses
src = campaign.SOURCES[0]
check("newnums flag parses", src.title == "09 DP R2C" and src.new_numbers_only and src.deep)
check("FTM source is untouched", not campaign.SOURCES[1].new_numbers_only)
try:
    campaign._sources_from_env("X|1|newnum")
    check("misspelled flag is refused", False)
except ValueError:
    check("misspelled flag is refused", True)

# ---- 9. allow list: an old Correct number no longer blocks a cleared record
PHONES[0]["status"] = "CORRECT"
RECORD["owner"]["primary_phone"] = "9185550100"
config.DP_ALLOW_RECORDS = {REC}
got, why = seed.apply_correct_number({"uuid": REC, "phone": "9185550100"}, set(), only_numbers=new)
check("cleared record goes ahead past an old Correct number", got is not None and why == "", why)
check("...and the old Correct number is still not texted",
      [r["phone"] for r in seed.all_textable_rows({"uuid": REC}, set(), only_numbers=new)]
      == ["9185550101", "9185550103"])
store.set_meta(f"record_replied:{REC}", "9185550100")
c = seed.build([dict(base, phone="9185550101")], touch=1)[0]
check("cleared record is texted despite an old-number settle", c.status == "ready", c.reasons)
store.set_meta(f"record_replied:{REC}", "")
config.DP_ALLOW_RECORDS = set()
PHONES[0]["status"] = "UNKNOWN"

# ---- 10. split across healthy numbers, least loaded first
from sms_agent import scheduler, sender_pool  # noqa: E402
config.NUMBER_HEALTH_CHECK = False
config.NEW_START_MAX_FLAGS = -1
nums = ["+19180000001", "+19180000002", "+19180000003"]
load: dict = {}
for _ in range(9):
    n = sender_pool.spread_number(nums, load)
    load[n] = load.get(n, 0) + 1
check("spread splits evenly", sorted(load.values()) == [3, 3, 3], load)

# ---- 11. cap alert fires when queued texts exceed a number's cap
ALERTS = []
scheduler.escalate.alert = lambda title, body, kind="": ALERTS.append((title, body))
config.DAILY_CAP_PER_NUMBER = 2
config.ONE_NUMBER_PER_DAY = False
config.POOL_CAPS = {}
with store.tx() as cx:
    for i in range(3):
        cx.execute("INSERT INTO outbox(phone, from_number, body, status, not_before, created_at) "
                   "VALUES (?,?,?,?,?,?)", (f"91855509{i:02d}", "+19180000007", "x", "queued",
                                            store.now(), store.now()))
scheduler._warn_number_caps()
check("cap overload alerts with the count",
      ALERTS and "...0007: 3 planned today, cap 2 (1 would be held)" in ALERTS[0][1], ALERTS)

# ---- 12. NEVER greet a dead person (Jeff 2026-10-26, 4527 W Madison Pl:
# "Hi Bonnie" went to her daughter's number because SmartSkip listed it under
# the dead subject first).
run_d = {"started": "2026-10-23", "dead": dp_numbers._dead_from_basis(
             "Bonnie Marie Pingrey Baker died 01/07/2026 (obituary)."),
         "people": [
             {"first": "Bonnie", "last": "Baker", "relationship": "Subject", "dead": True,
              "phones": ["9185102350", "9182311374"]},
             {"first": "MONET", "last": "RODRIGUES", "relationship": "Relative", "dead": False,
              "phones": ["9185198534", "9185102350"]}]}
rec_d = {"owner": {"first_name": "Bonnie", "last_name": "Baker", "phones": [
    ph("9185102350", "SmartSkip", "Dial First"), ph("9182311374", "SmartSkip", "Dial First")]}}
first, variant = dp_numbers.addressee(rec_d, run_d, "9185102350")
check("a number shared with the decedent greets the living holder", first == "MONET", (first, variant))
check("never the dead subject's name", "bonnie" not in first.lower())
vd = dp_numbers.classify(rec_d, run_d, {}, [])
check("the decedent's own number is still texted (Jeff 10/27)", vd["9182311374"] == "", vd)
check("...with no name and heir copy",
      dp_numbers.addressee(rec_d, run_d, "9182311374") == ("", "heir"),
      dp_numbers.addressee(rec_d, run_d, "9182311374"))
check("the shared number is still textable", vd["9185102350"] == "", vd)
check("'Lenord Sr. likely died' keeps the name Lenord out of every text (not sure = no name)",
      "lenord" in dp_numbers.dead_first_names(
          {"dead": dp_numbers._dead_from_basis("Lenord Sr. very likely died (see notes)")}))
check("basis with first and last marks the decedent dead",
      ("bonnie", "baker") in run_d["dead"], run_d["dead"])
run_w = {"started": "2026-10-23", "dead": set(), "people": [
    {"first": "John", "last": "Webster", "relationship": "Subject", "dead": True, "phones": ["2083057824"]},
    {"first": "JOHN", "last": "WEBSTER", "relationship": "Relative", "dead": False,
     "phones": ["2083057824", "2082848474"]}]}
rec_w = {"owner": {"first_name": "John", "last_name": "Webster", "phones": []}}
first, variant = dp_numbers.addressee(rec_w, run_w, "2082848474")
# Jeff 2026-10-28: a dead person's name is fine when it is also the heir's.
# SAME full name: could be the decedent listed twice (2719 S 80th E Ave), so no name.
check("same-full-name 'son' of a dead owner: still no name, heir copy",
      (first, variant) == ("", "heir"), (first, variant))
check("...the namesake exception does not apply to a same-full-name relative",
      not dp_numbers.heir_namesake_ok(run_w, "2082848474"))
run_cg = {"started": "2026-10-23", "dead": dp_numbers._dead_from_basis("Carolyn J Gray died 6/9/2026."),
          "people": [
              {"first": "Carolyn", "last": "Gray", "relationship": "Subject", "dead": False, "phones": ["9180001111"]},
              {"first": "CAROLYN", "last": "GRAY", "relationship": "Relative", "dead": False, "phones": ["9180002222"]},
              {"first": "Carolyn", "last": "Price", "relationship": "Relative", "dead": False, "phones": ["9180003333"]}]}
rec_cg = {"owner": {"first_name": "Carolyn", "last_name": "Gray"}}
check("2719 S 80th E: a 'Relative' row under the dead owner's full name never gets her name",
      dp_numbers.addressee(rec_cg, run_cg, "9180002222")[0] == ""
      and not dp_numbers.heir_namesake_ok(run_cg, "9180002222"))
# Jeff 2026-10-28: a dead person's name is fine when it is also the heir's.
check("living heir Carolyn PRICE of dead owner Carolyn Gray: her name (Jeff 10/28)",
      dp_numbers.addressee(rec_cg, run_cg, "9180003333") == ("Carolyn", "heir")
      and dp_numbers.heir_namesake_ok(run_cg, "9180003333"),
      dp_numbers.addressee(rec_cg, run_cg, "9180003333"))
run_ok = {"started": "2026-10-23", "owner_dead": True, "dead": set(),
          "basis": "Kimberly Welch is the borrower and is believed living.", "people": [
              {"first": "Kimberly", "last": "Welch", "relationship": "Subject", "dead": False, "phones": ["9180004444"]},
              {"first": "KIMBERLY", "last": "WILSON", "relationship": "Relative", "dead": False, "phones": ["9180005555"]}]}
check("200 Oak Ridge: owner only POSSIBLY dead -> a namesake relative gets no name",
      not dp_numbers.heir_namesake_ok(run_ok, "9180005555")
      and dp_numbers.addressee({"owner": {"first_name": "Kimberly"}}, run_ok, "9180005555")[0] == "")
run_bp = {"started": "2026-10-23", "basis": "Bonnie Marie Pingrey Baker died 01/07/2026 (obituary).",
          "dead": dp_numbers._dead_from_basis("Bonnie Marie Pingrey Baker died 01/07/2026 (obituary)."),
          "people": [
              {"first": "Bonnie", "last": "Baker", "relationship": "Subject", "dead": True, "phones": ["9180006666"]},
              {"first": "BONNIE", "last": "PINGREY", "relationship": "Relative", "dead": False, "phones": ["9180007777"]}]}
check("maiden name: 'Bonnie Pingrey' relative of 'Bonnie Marie Pingrey Baker died' gets no name",
      not dp_numbers.heir_namesake_ok(run_bp, "9180007777"))
run_cs = dict(run_cg, people=run_cg["people"][:2] + [dict(run_cg["people"][2], phones=["9180001111"])])
check("...but not on a number the dead owner also held",
      not dp_numbers.heir_namesake_ok(run_cs, "9180001111"))
run_u = {"started": "2026-10-23", "dead": set(), "people": [
    {"first": "Mary", "last": "Webster", "relationship": "Subject", "dead": True, "phones": ["2083050001"]},
    {"first": "John", "last": "Webster", "relationship": "Relative", "dead": True, "phones": ["2083050002"]},
    {"first": "John", "last": "Price", "relationship": "Relative", "dead": False, "phones": ["2083050003"]}]}
check("a namesake of a dead NON-owner relative still gets no name",
      dp_numbers.addressee({"owner": {"first_name": "Mary"}}, run_u, "2083050003")[0] == ""
      and not dp_numbers.heir_namesake_ok(run_u, "2083050003"))
check("no dead namesake at all: the exception does not apply",
      not dp_numbers.heir_namesake_ok(run_d, "9185198534"))
check("seed guard: deceased-tagged record never greets its owner",
      seed.dead_name({"tags": ["deceased"], "owner": {"first_name": "Bonnie"}}, "Bonnie"))
check("seed guard: untagged record keeps the owner's name",
      not seed.dead_name({"tags": [], "owner": {"first_name": "Bonnie"}}, "Bonnie"))
check("seed guard: a name a deep prospect found dead is never used",
      seed.dead_name({}, "Bonnie", run_d))

# ---- 13. the obituary check outranks the summary (2719 S 80th E Ave: summary
# said "believed LIVING", obituary check said "LIKELY THE OWNER'S OBITUARY").
import tempfile, json as _json
from pathlib import Path as _P
for verdict, want in [("LIKELY THE OWNER'S OBITUARY (name + place match)", True),
                      ("POSSIBLE (name matches, place not confirmed)", True),
                      ("CONFIRMED (survivors include)", True),
                      ("UNRELATED / WEAK (no name match)", False),
                      ("NO OBITUARY FOUND", False),
                      ("OWNER IS A SURVIVOR (someone else's obituary)", False)]:
    with tempfile.TemporaryDirectory() as td:
        (_P(td) / "obituary_check.json").write_text(_json.dumps({"summary": verdict}), encoding="utf-8")
        check(f"obituary verdict {verdict[:20]!r} -> owner maybe dead = {want}",
              dp_numbers._owner_maybe_dead(_P(td)) is want)
with tempfile.TemporaryDirectory() as td:
    check("missing obituary check -> not confident the owner is alive", dp_numbers._owner_maybe_dead(_P(td)))
run_g = {"started": "2026-10-23", "dead": set(), "owner_dead": True, "people": [
    {"first": "Carolyn", "last": "Gray", "relationship": "Subject", "dead": False,
     "phones": ["9188847404", "9186711890"]},
    {"first": "DONALD", "last": "BARNES", "relationship": "Relative", "dead": False,
     "phones": ["9186711890"]}]}
rec_g = {"owner": {"first_name": "Carolyn", "last_name": "Gray", "phones": [
    ph("9188847404", "SmartSkip", "Dial First"), ph("9186711890", "SmartSkip", "Dial First")]}}
first, variant = dp_numbers.addressee(rec_g, run_g, "9186711890")
check("owner not cleared: her son's shared number greets the son", first == "DONALD", (first, variant))
vg = dp_numbers.classify(rec_g, run_g, {}, [])
check("owner not cleared: her own number is still texted (Jeff 10/27)", vg["9188847404"] == "", vg)
check("...owner copy, no name (possibly dead, may be the one answering)",
      dp_numbers.addressee(rec_g, run_g, "9188847404") == ("", "owner"),
      dp_numbers.addressee(rec_g, run_g, "9188847404"))
# 11516 E 179th St N (Jeff 10/27): obituary check only POSSIBLE, so Jason's own
# number 918-289-1770 was held all day. It must be texted, just without his name.
run_j = {"started": "2026-10-23", "dead": set(), "owner_dead": True, "people": [
    {"first": "Jason", "last": "Rosser", "relationship": "Subject", "dead": False,
     "phones": ["9186390488", "9182891770"]}]}
rec_j = {"owner": {"first_name": "Jason", "last_name": "Rosser", "phones": [
    ph("9182891770", "SmartSkip", "Dial First")]}}
check("POSSIBLE obituary: owner's own number is texted",
      dp_numbers.classify(rec_j, run_j, {}, [])["9182891770"] == "")
check("POSSIBLE obituary: owner copy, no name",
      dp_numbers.addressee(rec_j, run_j, "9182891770") == ("", "owner"),
      dp_numbers.addressee(rec_j, run_j, "9182891770"))
check("seed guard: owner not cleared -> never the owner's name",
      seed.dead_name({"owner": {"first_name": "Carolyn"}}, "Carolyn", run_g))

# ---- 14. every source is read: a Message Board line alone bans the name
from sms_agent import death_check as dc
cands = {"carolyn", "deanna", "donald", "hannah", "lucas"}
check("board 'Decedent: Carolyn J. Gray | Died: 6/9/2026' bans Carolyn",
      "carolyn" in dc.banned_from_texts(["Decedent: Carolyn J. Gray | Died: 6/9/2026"], cands, set()))
check("survivor list does not ban the living",
      "hannah" not in dc.banned_from_texts(
          ["Survived by: is survived by his wife, Hannah Hoffmeister of Broken Arrow"], cands, set()))
check("'OWNER DECEASED' with no name bans the owner",
      "deanna" in dc.banned_from_texts(["OWNER DECEASED -- the petition names no living heir."],
                                       cands, {"deanna"}))
check("deep prospect template text bans nobody",
      not dc.banned_from_texts([
          "Numbers marked 'board only' were not scored (living owner: owner + spouse only; "
          "deceased owner: close family only).", "OBITUARY CHECK: Lucas Salisbury (Owasso, OK)"],
          cands, {"lucas"}))
_saved = dc.forbidden_names
dc.forbidden_names = lambda u, rec=None: {dc.ALL}
check("a record that could not be fully read gets no name", not dc.name_allowed("x", "Donald"))
dc.forbidden_names = lambda u, rec=None: {"carolyn"}
check("a banned name is refused, a cleared one allowed",
      not dc.name_allowed("x", "Carolyn") and dc.name_allowed("x", "Donald"))
check("no record uuid -> no name", not dc.name_allowed("", "Donald"))
check("heir namesake: the dead owner's name is allowed for the living heir",
      dc.name_allowed("x", "Carolyn", heir_namesake=True))
dc.forbidden_names = lambda u, rec=None: {dc.ALL}
check("heir namesake on an unreadable record: still no name",
      not dc.name_allowed("x", "Carolyn", heir_namesake=True))
dc.forbidden_names = _saved

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
