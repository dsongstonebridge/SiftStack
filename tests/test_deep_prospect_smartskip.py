"""Offline tests for deep_prospect_smartskip (2026-10-16).

Jeff's rule (2026-10-20): every SmartSkip number, owner and relatives, goes on
the phone list and gets Trestle scored; the board names who is who and who to
call first. `promote` stays for records written under the old 2026-10-16 rule. Fake export, fake people, every API call stubbed -- nothing
touches the CRM and nothing is billed.
Run: python tests/test_deep_prospect_smartskip.py
"""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import deep_prospect_smartskip as dp  # noqa: E402
import dp_board_split as split  # noqa: E402
import skip_trace_agent as sta  # noqa: E402
import smartskip_parse  # noqa: E402

failures = 0


def check(name, cond):
    global failures
    print(("PASS " if cond else "FAIL ") + name)
    failures += not cond


def raises(fn):
    try:
        fn()
    except SystemExit:
        return True
    return False


# ── a fake SmartSkip export (vertical format) ─────────────────────────
COLS = ["Input Name", "Relationship", "Possible Type", "First Name", "Last Name", "Age",
        "Deceased", "Property Address", "Property City", "Property State", "Property Zip",
        "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
        "Phone 1 number", "Phone 1 type", "Phone 2 number", "Phone 2 type"]
OWNER = "9185550101"
SHARED = "9185550102"      # owner's household landline, also returned for the daughter
DAUGHTER = "9185550201"
SON = "9185550301"
NEPHEW = "9185550401"


def row(rel, ptype, first, last, age, p1="", t1="", p2="", t2="", deceased="false"):
    return {"Input Name": "ROBERT TESTMAN", "Relationship": rel, "Possible Type": ptype,
            "First Name": first, "Last Name": last, "Age": age, "Deceased": deceased,
            "Property Address": "100 Fake St", "Property City": "Tulsa",
            "Property State": "OK", "Property Zip": "74101",
            "Mailing Address": "", "Mailing City": "Tulsa", "Mailing State": "OK",
            "Mailing Zip": "", "Phone 1 number": p1, "Phone 1 type": t1,
            "Phone 2 number": p2, "Phone 2 type": t2}


ROWS = [row("Subject", "", "Robert", "Testman", "81", OWNER, "Wireless", SHARED, "Landline"),
        row("Relative", "Child", "Susan", "Testman", "52", DAUGHTER, "Wireless", SHARED, "Landline"),
        row("Relative", "Child", "Mark", "Testman", "49", SON, "Wireless"),
        row("Relative", "Other Relative", "Kyle", "Testman", "30", NEPHEW, "Wireless"),
        row("Relative", "Child", "Nobody", "Testman", "40"),
        row("Relative", "Parent", "Old", "Testman", "130", "9185550999", "Wireless"),
        row("Associate", "", "Pat", "Neighbor", "60", "9185550888", "Wireless")]

tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="", encoding="utf-8")
w = csv.DictWriter(tmp, fieldnames=COLS)
w.writeheader()
w.writerows(ROWS)
tmp.close()
REC = dp.pick_record(smartskip_parse.parse(tmp.name), None)
OVR = {"Susan Testman": "Daughter", "Mark Testman": "Son"}

# ── plan ──────────────────────────────────────────────────────────────
plan = dp.build_plan(REC, ["subject"], OVR)
prim = {ph["number"] for p in plan["primary"] for ph in p["phones"]}
board = {n for b in plan["board"] for n in b["phones"]}
check("primary=subject: only the owner's numbers go in the phone list", prim == {OWNER, SHARED})
check("relatives are board-only", board == {DAUGHTER, SON, NEPHEW})
check("associate dropped", all(b["name"] != "Pat Neighbor" for b in plan["board"]))
check("age >100 dropped", all(b["name"] != "Old Testman" for b in plan["board"]))
check("shared household line is not repeated on a board line", SHARED not in board)
check("the owner's own numbers carry no relationship tag",
      all(not ph["relationship_tag"] for p in plan["primary"] for ph in p["phones"]))
check("a relative with no number is still listed",
      any(b["name"] == "Nobody Testman" and not b["phones"] for b in plan["board"]))

heir = dp.build_plan(REC, ["Susan Testman"], OVR)
tags = {ph["number"]: ph["relationship_tag"] for p in heir["primary"] for ph in heir["primary"][0]["phones"]}
check("top heir as primary: her own number tagged Daughter", tags.get(DAUGHTER) == "Daughter")
check("top heir as primary: the shared line gets no relationship tag", tags.get(SHARED) == "")
check("top heir as primary: deceased owner goes to the board",
      any(b["relationship"].startswith("Owner") for b in heir["board"]))
dead = dp.build_plan(REC, ["Susan Testman"], OVR, subject_deceased=True)
subj = [b for b in dead["board"] if b["name"] == "Robert Testman"][0]
check("confirmed death: subject labelled Decedent, not Owner",
      subj["relationship"] == "Decedent (traced subject)")
fam = dp.build_family_post(dead, today="2026-10-20")
check("confirmed death: no 'flagged deceased, unverified' note on the decedent",
      "unverified" not in [l for l in fam.splitlines() if "Robert Testman" in l][0])
check("confirmed death: decedent's numbers carry no relationship tag",
      all(not ph["relationship_tag"] for ph in subj["phone_list"]))
check("--primary is required", raises(lambda: dp.build_plan(REC, [], OVR)))
check("an unknown --primary name is refused", raises(lambda: dp.build_plan(REC, ["Jane Nobody"], OVR)))
_rec2 = dict(REC, relatives=REC["relatives"] + [dict(REC["relatives"][0], name="S TESTMAN", phones=[])])
check("an exact name wins over an initial-only namesake (Shirley Coats vs M COATS)",
      dp.build_plan(_rec2, ["Susan Testman"], OVR)["primary"][0]["name"] == "Susan Testman")
check("Other Relative maps to the existing Relative tag", dp.account_rel_tag("Other Relative") == "Relative")
check("Brother maps to Relative (no new tag)", dp.account_rel_tag("Brother") == "Relative")

# ── board post round trip ─────────────────────────────────────────────
post = dp.build_board_post(plan, today="2026-10-16")
back = dp.parse_board_post(post)
check("board post starts with the marker", post.startswith(dp.BOARD_MARKER))
check("every board number parses back", {r["number"] for r in back} == board)
check("relationships survive the round trip",
      {r["number"]: r["relationship"] for r in back}.get(SON) == "Son")
html_post = "<p>" + post.replace("\n", "<br>") + "</p>"
check("an HTML-wrapped post parses the same", {r["number"] for r in dp.parse_board_post(html_post)} == board)
check("no person's name is ever a phone tag",
      all(t in dp.ACCOUNT_REL_TAGS or t == "" for p in plan["primary"]
          for t in (ph["relationship_tag"] for ph in p["phones"])))


# ── fake CRM + fake Trestle ───────────────────────────────────────────
class FakeAPI:
    DataSiftAPIError = Exception

    def __init__(self, phones=None, board=None):
        self.phones = phones or []            # [{"number", "tags"}]
        self.board = board or []
        self.upserts, self.tag_calls, self.prop_tags, self.owner_posts = [], [], [], []
        self.removed = []

    def set_dry_run(self, on):
        pass

    def find_property_by_address(self, street, city="", state="", strict=False):
        return {"uuid": "P1"}

    def get_property(self, uuid):
        return {"owner": {"uuid": "O1", "phones": self.phones}}

    def upsert_phones(self, owner_uuid, phones):
        self.upserts.append(phones)
        have = {p["number"] for p in self.phones}
        self.phones += [{"number": p["number"], "tags": []} for p in phones if p["number"] not in have]

    def remove_phones(self, owner_uuid, numbers):
        self.removed.append(list(numbers))
        self.phones = [p for p in self.phones if p["number"] not in numbers]

    def apply_phone_tags_verified(self, owner_uuid, mapping):
        self.tag_calls.append(mapping)
        for p in self.phones:
            if p["number"] in mapping:
                p["tags"] = list(set(p.get("tags") or []) | set(mapping[p["number"]]))
        return {"ok": True, "missing": {}}

    def add_tags(self, uuid, tags):
        self.prop_tags.append(tags)

    # self.board is the PROPERTY board, where every deep prospecting post goes;
    # anything posted to an owner board lands in owner_posts and fails a check.
    def get_message_board(self, owner_uuid):
        return []

    def get_property_message_board(self, uuid):
        return [{"message": m, "created": "x"} for m in self.board]

    def post_message_board(self, owner_uuid, message):
        self.owner_posts.append(message)
        OWNER_POSTS.append(message)

    def post_property_message_board(self, uuid, message):
        self.board.insert(0, message)


OWNER_POSTS = []   # every owner-board post from any fake; must stay empty
scored_numbers = []
TIER_BY = {}
RESEARCH = {"status": ["Robert Testman: DEAD, died 08/02/2026 (filing)"], "basis": "b",
            "labels": {"Susan Testman": "Daughter, PR (filing)"}}


def fake_score(subjects, dry_run=True):
    out = {}
    for s in subjects:
        for p in s["people"]:
            for ph in p["phones"]:
                scored_numbers.append(ph["number"])
                ph["tier"] = TIER_BY.get(ph["number"], "Dial First")
                ph["litigator_risk"] = ph["number"] == SON
                out[ph["number"]] = ph
    return out


dp._api = fake = FakeAPI()
sta.score_phones = fake_score
sta._current_phone_state = lambda uuid: {}

ALL = {OWNER, SHARED, DAUGHTER, SON, NEPHEW}
# Trestle scope (Jeff, 2026-10-23). Deceased owner: never the dead owner's own
# numbers; close family (daughter, son) yes; catch-all relative (nephew) no.
CLOSE_FAMILY = {SHARED, DAUGHTER, SON}        # SHARED reaches the list through Susan
res = dp.write_record(REC, dead, research=RESEARCH, dry_run=False)
sent = [p["number"] for p in fake.upserts[0]]
check("deceased owner: only close family's Dial First/Second numbers upserted, a litigator number stays off",
      set(sent) == CLOSE_FAMILY - {SON})
check("deceased owner: the dead owner's own number is NOT scored", OWNER not in scored_numbers)
check("deceased owner: a catch-all relative (nephew) is NOT scored", NEPHEW not in scored_numbers)
check("deceased owner: Trestle scored exactly the close family", set(scored_numbers) == CLOSE_FAMILY)
check("write: each number upserted once", len(sent) == len(set(sent)))
check("write: each number scored once", len(scored_numbers) == len(set(scored_numbers)))
check("write: daughter's number tagged Daughter", "Daughter" in fake.tag_calls[0][DAUGHTER])
check("write: shared household line carries no relationship tag",
      not set(fake.tag_calls[0][SHARED]) & dp.ACCOUNT_REL_TAGS)
check("write: a litigator number never reaches the record", SON not in fake.tag_calls[0])
check("write: property tags SmartSkip Skipped + TrestleIQ Scored",
      fake.prop_tags[0] == [dp.TAG_SMARTSKIP_SKIPPED, sta.TAG_TRESTLE_SCORED])
post_lines = fake.board[0].splitlines()
check("write: post on the PROPERTY board, owner of record first",
      post_lines[0].startswith("OWNER OF RECORD: ") and any(split.POST_MARKER in l for l in post_lines))
check("write: research status is on the post", "STATUS: Robert Testman: DEAD, died 08/02/2026 (filing)" in post_lines)
check("write: post names everyone with a number, scored or not",
      all(dp._fmt(n) in fake.board[0] for n in ALL) and "Kyle Testman" in fake.board[0])
check("write: research label replaces SmartSkip's",
      any(l.startswith("Susan Testman, Daughter, PR (filing): ") for l in post_lines))
check("write: each person named once",
      sum(l.startswith("Susan Testman,") for l in post_lines) == 1)
check("write: post shows each scored number's dial tier",
      f"{dp._fmt(DAUGHTER)} (Dial First)" in fake.board[0])
check("write: post leaves an unscored number with no tier",
      any(dp._fmt(NEPHEW) in l and "(" not in l.split(": ", 1)[1] for l in post_lines))
check("write: post shows a litigator number as DO NOT CALL",
      f"{dp._fmt(SON)} ({sta.PHONE_TAG_LITIGATOR_RISK})" in fake.board[0])
check("write: post is not mistaken for an old relatives post",
      dp.find_relatives_post([{"message": fake.board[0]}]) is None)
n_posts = len(fake.board)
dp.write_record(REC, dead, research=RESEARCH, dry_run=False)
check("write twice: no second family post", len(fake.board) == n_posts)
check("an old 10-20 family post still counts as already posted",
      dp.find_family_post([{"message": dp.FAMILY_MARKER + " (2026-10-20)"}]) is not None)

# Living owner: subject + spouse only; relatives never scored.
live_fake = FakeAPI()
dp._api = live_fake
scored_numbers[:] = []
dp.write_record(REC, plan, research=RESEARCH, dry_run=False)
check("living owner: only the owner's numbers go on the list",
      {p["number"] for p in live_fake.upserts[0]} == {OWNER, SHARED})
check("living owner: no relative is ever scored", set(scored_numbers) == {OWNER, SHARED})
check("living owner: owner's tags = SmartSkip + tier", live_fake.tag_calls[0][OWNER] == ["Dial First", "SmartSkip"])
spouse_rec = dict(REC, relatives=REC["relatives"] + [dict(REC["relatives"][0], name="Jane Testman",
                  canon_rel="Wife", phones=[{"number": "9185550555", "type": "Wireless"}])])
sp = dp.build_plan(spouse_rec, ["subject"], OVR)
check("living owner: the spouse IS scored", "Jane Testman" in sp["scored_names"])
check("living owner: children are NOT scored", "Susan Testman" not in sp["scored_names"])
check("--score adds a co-owner SmartSkip calls 'Relative'",
      "Kyle Testman" in dp.build_plan(REC, ["subject"], OVR, score_add=["Kyle Testman"])["scored_names"])
check("--no-score drops someone the default would score",
      "Mark Testman" not in dp.build_plan(REC, ["Susan Testman"], OVR, subject_deceased=True,
                                          score_drop=["Mark Testman"])["scored_names"])

od = dp.build_plan(REC, ["subject"], OVR, owner_deceased=True)
check("--owner-deceased: close family scored, traced subject still labelled Owner",
      "Susan Testman" in od["scored_names"] and "Robert Testman" in od["scored_names"]
      and "Kyle Testman" not in od["scored_names"])
check("Name@Relationship picks one of two same-named people",
      dp._match("Mark Testman@Son", [{"name": "Mark Testman", "relationship": "Son"},
                                    {"name": "Mark Testman", "relationship": "Parent"}])[0]["relationship"] == "Son")

heir_fake = FakeAPI()
dp._api = heir_fake
scored_numbers[:] = []
dp.write_record(REC, dead, research=RESEARCH, dry_run=False)
check("top heir as primary: owner's own number carries no relationship tag",
      OWNER not in heir_fake.tag_calls[0] or not set(heir_fake.tag_calls[0][OWNER]) & dp.ACCOUNT_REL_TAGS)

# a record already skip traced and dialed before deep prospecting
worked = FakeAPI(phones=[
    {"number": OWNER, "tags": ["Tracerfy", "Dial Second"], "status": "DEAD"},
    {"number": DAUGHTER, "tags": ["DataSift"], "status": "NO_ANSWER"}])
dp._api = worked
scored_numbers[:] = []
# Merge mechanics, not scope: score everyone so every path is exercised.
worked_plan = dp.build_plan(REC, ["subject"], OVR,
                            score_add=["Susan Testman", "Mark Testman", "Kyle Testman"])
res = dp.write_record(REC, worked_plan, research=RESEARCH, dry_run=False)
sent = [p["number"] for p in worked.upserts[0]]
check("worked record: numbers already on it are NOT re-upserted (no duplicate, no reset)",
      OWNER not in sent and DAUGHTER not in sent)
check("worked record: new numbers are added, except the litigator one", set(sent) == ALL - {OWNER, DAUGHTER, SON})
check("worked record: an already-tiered number is not scored again", OWNER not in scored_numbers)
check("worked record: a number already in the CRM is never scored, even untiered (Jeff, 2026-10-23)",
      DAUGHTER not in scored_numbers)
check("worked record: existing tiered number only gains the SmartSkip source tag",
      worked.tag_calls[0][OWNER] == ["SmartSkip"])
check("worked record: no relationship tag on a number already on the record",
      "Daughter" not in worked.tag_calls[0][DAUGHTER])
status = {p["number"]: p.get("status") for p in worked.phones}
check("worked record: statuses untouched (still DEAD / NO_ANSWER)",
      status.get(OWNER) == "DEAD" and status.get(DAUGHTER) == "NO_ANSWER")

# ── Dial First/Second only on a deep prospected record (Jeff, 2026-11-02) ──
TIER_BY.update({DAUGHTER: "Dial Third", SHARED: "Dial First"})
low = FakeAPI()
dp._api = low
scored_numbers[:] = []
res = dp.write_record(REC, dead, research=RESEARCH, dry_run=False)
check("a new number below Dial Second never reaches the record",
      DAUGHTER not in {p["number"] for p in low.phones} and SHARED in {p["number"] for p in low.phones})
check("but it is on the board, with its tier", f"{dp._fmt(DAUGHTER)} (Dial Third)" in low.board[0])
TIER_BY.clear()

old_low = FakeAPI(phones=[{"number": "9185559001", "tags": ["DataSift", "Dial Fourth"], "status": "UNKNOWN"},
                          {"number": "9185559002", "tags": ["Tracerfy", "Dial First"], "status": "UNKNOWN"},
                          {"number": "9185559003", "tags": ["Dial Third"], "status": "CORRECT"}])
dp._api = old_low
res = dp.write_record(REC, dead, research=RESEARCH, dry_run=False)
check("an old number below Second comes off, whatever found it", old_low.removed == [["9185559001"]])
check("a caller's Correct number is never removed", "9185559003" in {p["number"] for p in old_low.phones})
check("the removed number is on the post", "918-555-9001 (Dial Fourth)" in old_low.board[0])

only_low = FakeAPI(phones=[{"number": "9185559004", "tags": ["Dial Fourth"], "status": "UNKNOWN"}])
dp._api = only_low
only_rec = dict(REC, relatives=[], subject_phones=[])
res = dp.write_record(only_rec, dp.build_plan(only_rec, ["subject"], OVR), research=RESEARCH, dry_run=False)
check("the phone list is never emptied", not only_low.removed and only_low.phones)

class NoPost(FakeAPI):
    def get_property_message_board(self, uuid):
        return []


unconfirmed = NoPost(phones=[{"number": "9185559001", "tags": ["Dial Fourth"], "status": "UNKNOWN"},
                             {"number": "9185559002", "tags": ["Dial First"], "status": "UNKNOWN"}])
dp._api = unconfirmed
res = dp.write_record(REC, dead, research=RESEARCH, dry_run=False)
check("no number comes off unless the post is confirmed", not unconfirmed.removed and res["ok"] is False)

# Research trumps SmartSkip.
ruled = dp.build_plan(REC, ["Susan Testman"], OVR, subject_deceased=True, score_drop=["Mark Testman", "Not Here"])
check("research ruling someone out stops the scoring", "Mark Testman" not in ruled["scored_names"])
rows_ok = split.people_from_plan(dead, {}, RESEARCH)
check("complete research passes", not split.research_problems(RESEARCH, rows_ok, ["Susan Testman"]))
check("no basis is refused", split.research_problems({"status": ["x"]}, rows_ok, ["x"]))
check("a call-first label with no source is refused", split.research_problems(
    {"status": ["x"], "basis": "b"}, split.people_from_plan(dead, {}, {}), ["Susan Testman"]))
check("a label for someone not on the list is refused", split.research_problems(
    dict(RESEARCH, labels={"Susan Testman": "Daughter (filing)", "No Body": "x"}), rows_ok, ["x"]))
check("a dead call-first person is refused", split.research_problems(
    dict(RESEARCH, dead=["Susan Testman"]), rows_ok, ["Susan Testman"]))
outside = dict(RESEARCH, call_first_outside=[{"name": "Gerald T", "label": "Brother, PR (filing)", "numbers": ["9185551000"]}],
               labels={})
empty_plan = dp.build_plan(dict(REC, relatives=[]), [], OVR, subject_deceased=True, outside_primary=True)
check("a call-first person SmartSkip missed is allowed", not split.research_problems(
    outside, split.people_from_plan(empty_plan, {}, outside), []))

# promote (old-rule records): a pre-2026-10-20 relatives post on the board
fake = FakeAPI(phones=[{"number": OWNER, "tags": ["SmartSkip", "Dial First"]},
                       {"number": SHARED, "tags": ["SmartSkip", "Dial Second"]}],
               board=[dp.build_board_post(plan, today="2026-10-16")])
dp._api = fake
fake.upserts, fake.tag_calls, scored_numbers[:] = [], [], []
res = dp.promote_record("100 Fake St", "Tulsa", "OK", only={"Son", "Daughter"}, dry_run=False)
sent = {p["number"] for p in fake.upserts[0]}
check("promote --only Son,Daughter: nephew left on the board, a litigator number stays off", sent == {DAUGHTER})
check("promote: Trestle scored only the promoted numbers", set(scored_numbers) == {DAUGHTER, SON})
check("promote: daughter's number tagged Daughter", "Daughter" in fake.tag_calls[0][DAUGHTER])
check("promote: a litigator number never reaches the record", SON not in fake.tag_calls[0])
check("promote: note posted", fake.board[0].startswith(dp.PROMOTED_MARKER))

fake.phones += [{"number": DAUGHTER, "tags": ["SmartSkip", "Dial First"]},
                {"number": SON, "tags": ["SmartSkip"]}]
fake.upserts, scored_numbers[:] = [], []
n_posts = len(fake.board)
res = dp.promote_record("100 Fake St", "Tulsa", "OK", only={"Son", "Daughter"}, dry_run=False)
check("promote again: nothing re-added, nothing scored, no note",
      not fake.upserts and not scored_numbers and len(fake.board) == n_posts)

fake2 = FakeAPI(board=["just a petition post"])
dp._api = fake2
check("promote with no relatives post is refused",
      raises(lambda: dp.promote_record("100 Fake St", "Tulsa", "OK", dry_run=False)))

check("nothing is ever posted to an OWNER board (Jeff, 2026-10-21/23)", not OWNER_POSTS)

os.unlink(tmp.name)
print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
