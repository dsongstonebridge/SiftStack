"""Offline tests for deep_prospect_smartskip (2026-10-16).

Jeff's rule: only the primary person's numbers go in the phone list and get
Trestle scored; every relative goes on the Message Board until he asks to
promote them. Fake export, fake people, every API call stubbed -- nothing
touches the CRM and nothing is billed.
Run: python tests/test_deep_prospect_smartskip.py
"""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import deep_prospect_smartskip as dp  # noqa: E402
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
check("--primary is required", raises(lambda: dp.build_plan(REC, [], OVR)))
check("an unknown --primary name is refused", raises(lambda: dp.build_plan(REC, ["Jane Nobody"], OVR)))
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
        self.upserts, self.tag_calls, self.prop_tags = [], [], []

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

    def apply_phone_tags_verified(self, owner_uuid, mapping):
        self.tag_calls.append(mapping)
        return {"ok": True, "missing": {}}

    def add_tags(self, uuid, tags):
        self.prop_tags.append(tags)

    def get_message_board(self, owner_uuid):
        return [{"message": m, "created": "x"} for m in self.board]

    def post_message_board(self, owner_uuid, message):
        self.board.insert(0, message)


scored_numbers = []


def fake_score(subjects, dry_run=True):
    out = {}
    for s in subjects:
        for p in s["people"]:
            for ph in p["phones"]:
                scored_numbers.append(ph["number"])
                ph["tier"] = "Dial First"
                ph["litigator_risk"] = ph["number"] == SON
                out[ph["number"]] = ph
    return out


dp._api = fake = FakeAPI()
sta.score_phones = fake_score
sta._current_phone_state = lambda uuid: {}

res = dp.write_record(REC, plan, dry_run=False)
sent = {p["number"] for p in fake.upserts[0]}
check("write: only primary numbers upserted", sent == {OWNER, SHARED})
check("write: Trestle scored only primary numbers", set(scored_numbers) == {OWNER, SHARED})
check("write: phone tags = SmartSkip + tier", fake.tag_calls[0][OWNER] == ["Dial First", "SmartSkip"])
check("write: property tags SmartSkip Skipped + TrestleIQ Scored",
      fake.prop_tags[0] == [dp.TAG_SMARTSKIP_SKIPPED, sta.TAG_TRESTLE_SCORED])
check("write: relatives post on the board", fake.board and fake.board[0].startswith(dp.BOARD_MARKER))
n_posts = len(fake.board)
dp.write_record(REC, plan, dry_run=False)
check("write twice: no second relatives post", len(fake.board) == n_posts)

# promote: the owner numbers are now on the record (and tiered)
fake.phones = [{"number": OWNER, "tags": ["SmartSkip", "Dial First"]},
               {"number": SHARED, "tags": ["SmartSkip", "Dial Second"]}]
fake.upserts, fake.tag_calls, scored_numbers[:] = [], [], []
res = dp.promote_record("100 Fake St", "Tulsa", "OK", only={"Son", "Daughter"}, dry_run=False)
sent = {p["number"] for p in fake.upserts[0]}
check("promote --only Son,Daughter: nephew left on the board", sent == {DAUGHTER, SON})
check("promote: Trestle scored only the promoted numbers", set(scored_numbers) == {DAUGHTER, SON})
check("promote: daughter's number tagged Daughter", "Daughter" in fake.tag_calls[0][DAUGHTER])
check("promote: litigator REPLACES the tier",
      sta.PHONE_TAG_LITIGATOR_RISK in fake.tag_calls[0][SON] and "Dial First" not in fake.tag_calls[0][SON])
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

os.unlink(tmp.name)
print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
