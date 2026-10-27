"""Offline tests for deep_prospect_run (2026-10-16). Fake record, fake SmartSkip
export, fake CRM; nothing touches DataSift or SmartSkip and nothing is billed.
Run: python tests/test_deep_prospect_run.py
"""
import argparse
import csv
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import deep_prospect_run as run  # noqa: E402
import deep_prospect_smartskip as dps  # noqa: E402
import skip_trace_agent as sta  # noqa: E402

failures = 0


def check(name, cond):
    global failures
    print(("PASS " if cond else "FAIL ") + name)
    failures += not cond


# ── who gets searched ─────────────────────────────────────────────────
check("person owner -> name", run.person_name({"first_name": "Mary", "last_name": "Smith"}) == "Mary Smith")
check("trust-held record that still names the person -> person",
      run.person_name({"first_name": "Mary", "last_name": "Smith", "company": "Smith Family Trust"})
      == "Mary Smith")
check("LLC owner -> skipped", run.person_name({"company": "Acme Homes LLC"}) is None)
check("trust written into the name -> skipped",
      run.person_name({"first_name": "Smith Family", "last_name": "Trust"}) is None)

REC = {"address": {"street": "100 Fake St", "city": "Tulsa", "state": "OK", "postal_code": "74101-1234"},
       "owner": {"first_name": "Mary", "last_name": "Smith",
                 "address": {"street": "PO Box 9", "city": "Bixby", "state": "OK", "postal_code": "74008"}}}
row = run.build_input_row("Mary Smith", REC, at_property=False)
check("living owner searched at the MAILING address", row["Mailing Address"] == "PO Box 9" and row["Mailing City"] == "Bixby")
row = run.build_input_row("Robert Smith", REC, at_property=True)
check("decedent searched at the PROPERTY address", row["Mailing Address"] == "100 Fake St")
check("zip trimmed to 5", row["Property Zip"] == "74101")


def raises(fn):
    try:
        fn()
    except SystemExit:
        return True
    return False


check("a one-word search name is refused", raises(lambda: run.build_input_row("Smith", REC, at_property=True)))

# ── finish, end to end against fakes ─────────────────────────────────
tmp = Path(tempfile.mkdtemp())
cols = ["Input Name", "Relationship", "Possible Type", "First Name", "Last Name", "Age", "Deceased",
        "Property Address", "Property City", "Property State", "Property Zip", "Mailing Address",
        "Mailing City", "Mailing State", "Mailing Zip", "Phone 1 number", "Phone 1 type"]
rows = [("Subject", "", "Robert", "Smith", "88", "true", ""),
        ("Relative", "Child", "Susan", "Smith", "55", "false", "9185550201"),
        ("Relative", "Child", "Mark", "Smith", "52", "false", "9185550301")]
with open(tmp / "smartskip_export.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=cols)
    w.writeheader()
    for rel, pt, fn, ln, age, dec, ph in rows:
        w.writerow({"Input Name": "ROBERT SMITH", "Relationship": rel, "Possible Type": pt,
                    "First Name": fn, "Last Name": ln, "Age": age, "Deceased": dec,
                    "Property Address": "100 FAKE ST", "Property City": "TULSA", "Property State": "OK",
                    "Property Zip": "74101", "Mailing Address": "", "Mailing City": "", "Mailing State": "",
                    "Mailing Zip": "", "Phone 1 number": ph, "Phone 1 type": "Wireless" if ph else ""})
(tmp / "state.json").write_text(json.dumps({
    "street": "100 Fake St", "city": "Tulsa", "state": "OK", "property_uuid": "P1",
    "owner_uuid": "O1", "searched": "Robert Smith", "searched_at": "property",
    "bulkSkipId": "B1", "paid": True, "export": "smartskip_export.csv"}))
(tmp / "obituary_check.json").write_text(json.dumps({
    "summary": "LIKELY THE OWNER'S OBITUARY (name + place match)",
    "candidates": [{"verdict": "LIKELY THE OWNER'S OBITUARY (name + place match)", "title": "Robert Smith Obituary",
                    "url": "https://example.test/obit", "dod": "August 2, 2026",
                    "survived_by": "survived by his daughter Susan and son Mark", "preceded_by": ""}]}))


OWNER_POSTS = []   # every owner-board post from any fake; must stay empty


class FakeAPI:
    DataSiftAPIError = Exception

    def __init__(self):
        self.phones, self.board, self.uploads, self.dry = [], [], [], False
        self.owner_posts = []

    def set_dry_run(self, on):
        self.dry = on

    def find_property_by_address(self, street, city="", state="", strict=False):
        self.looked_up = (street, city)
        return {"uuid": "P1"}

    def get_property(self, uuid):
        return {"owner": {"uuid": "O1", "phones": self.phones}}

    def get_owner(self, uuid):
        return {"phones": self.phones}

    def upsert_phones(self, owner_uuid, phones):
        if not self.dry:
            self.phones += [{"number": p["number"], "tags": []} for p in phones]

    def apply_phone_tags_verified(self, owner_uuid, mapping):
        if not self.dry:
            for p in self.phones:
                p["tags"] = mapping.get(p["number"], p["tags"])
        return {"ok": True, "missing": {}}

    def add_tags(self, uuid, tags):
        pass

    # self.board is the PROPERTY board; an owner-board post fails a check.
    def get_message_board(self, owner_uuid):
        return []

    def get_property_message_board(self, uuid):
        return [{"message": m} for m in self.board]

    def post_message_board(self, owner_uuid, message):
        self.owner_posts.append(message)
        OWNER_POSTS.append(message)

    def post_property_message_board(self, uuid, message):
        if not self.dry:
            self.board.insert(0, message)

    def upload_property_document(self, uuid, path):
        if self.dry:
            return {"dry_run": True}
        self.uploads.append(os.path.basename(path))
        return {"filename": os.path.basename(path)}

    def list_property_documents(self, uuid):
        return [{"filename": f} for f in self.uploads]


fake = FakeAPI()
run._api = dps._api = fake
sta.score_phones = lambda subjects, dry_run=True: {}
sta._current_phone_state = lambda uuid: {}


def finish(commit, repost=False):
    return run.cmd_finish(argparse.Namespace(
        run=str(tmp), primary=["Susan Smith"], relationship=["Susan Smith=Daughter"],
        basis="Robert died Aug 2 2026 (obituary); widowed; Susan is the daughter and PR",
        decedent=True, repost=repost, commit=commit, owner_status="dead",
        status_proof="probate PB-2026-0001 names Susan Smith as PR"))


finish(commit=False)
check("dry run: nothing written, nothing uploaded", not fake.phones and not fake.board and not fake.uploads)
check("dry run still builds the PDF", any(p.suffix == ".pdf" for p in tmp.iterdir()))
check("record resolved from the run's address, not SmartSkip's upper-case copy",
      fake.looked_up == ("100 Fake St", "Tulsa"))

finish(commit=True)
# Rule since 2026-10-20: every SmartSkip number goes on the phone list.
check("commit: every SmartSkip number on the phone list",
      sorted(p["number"] for p in fake.phones) == ["9185550201", "9185550301"])
check("commit: her number tagged Daughter",
      "Daughter" in next(p for p in fake.phones if p["number"] == "9185550201")["tags"])
check("commit: PDF uploaded to Documents", len(fake.uploads) == 1)
check("commit: board has the family post and the PDF line",
      any(dps.FAMILY_MARKER_V2 in m for m in fake.board)
      and any(run.PDF_MARKER in m for m in fake.board))
check("commit: BOTH board posts open with the verified owner status (2026-10-27)",
      len(fake.board) == 2 and all(m.startswith("OWNER STATUS: VERIFIED DEAD - Robert Smith")
                                   for m in fake.board))
check("commit: confirmed death labels the subject Decedent, not Owner",
      any("Decedent (traced subject)" in m for m in fake.board)
      and not any("Owner (traced subject)" in m for m in fake.board))
md = next(p for p in tmp.iterdir() if p.suffix == ".md").read_text(encoding="utf-8")
check("research pack names the basis and the obituary", "Susan is the daughter" in md and "example.test/obit" in md)
check("research pack: owner status sits right under the title",
      md.split("\n")[2].startswith("**OWNER STATUS: VERIFIED DEAD"))
check("research pack lists the deceased owner as a board relative", "Robert Smith" in md and "Mark Smith" in md)

n_board, n_up = len(fake.board), len(fake.uploads)
finish(commit=True)
check("finish twice: no second PDF, no second board posts",
      len(fake.uploads) == n_up and len(fake.board) == n_board)

# ── one SmartSkip search per property (2026-10-19) ───────────────────
g = Path(tempfile.mkdtemp()) / "100-fake-st"
check("fresh property, untagged -> allowed", run.prior_run_reason(g, REC) is None)
check("record tagged SmartSkip Skipped (string tags) -> refused",
      run.prior_run_reason(g, {**REC, "tags": [dps.TAG_SMARTSKIP_SKIPPED]}) is not None)
check("record tagged SmartSkip Skipped (tag objects) -> refused",
      run.prior_run_reason(g, {**REC, "tags": [{"title": dps.TAG_SMARTSKIP_SKIPPED}]}) is not None)
g.mkdir(parents=True)
(g / "state.json").write_text(json.dumps({"bulkSkipId": "B1", "paid": False, "searched": "Mary Smith",
                                          "started": "2026-10-19"}), encoding="utf-8")
why = run.prior_run_reason(g, REC)
check("unpaid earlier order on this address -> refused, names the order",
      why is not None and "B1" in why and "not paid" in why)
(g / "state.json").write_text(json.dumps({"bulkSkipId": "B1", "paid": True}), encoding="utf-8")
check("paid earlier order -> refused, says PAID", "PAID" in (run.prior_run_reason(g, REC) or ""))

import obituary_check  # noqa: E402
check("people-search sites are never fetched",
      obituary_check._people_search("https://www.truepeoplesearch.com/find/person/x")
      and obituary_check._people_search("https://www.fastpeoplesearch.com/name/mary-smith"))
check("obituary / FindAGrave / Legacy pages still allowed",
      not obituary_check._people_search("https://www.legacy.com/us/obituaries/name/mary-smith-obituary")
      and not obituary_check._people_search("https://www.findagrave.com/memorial/123/mary-smith"))

check("nothing is ever posted to an OWNER board (Jeff, 2026-10-21/23)", not OWNER_POSTS)
fake.board = ["=== DECEASED OWNER ===\nDecedent: Roland Theriot | Died: 7/6/2026 | Obituary: https://x"]
check("board name stops at the '|' of a July obituary post",
      run.decedent_from_board("O1", "P1") == "Roland Theriot")
check("split_name: plain name -> first and last word",
      run.split_name("Bonnie Marie Pingrey Baker") == ("Bonnie", "Baker"))
check("split_name: First|Last keeps a compound surname",
      run.split_name("Francisco|Perez Rodriguez") == ("Francisco", "Perez Rodriguez"))
_ST = {"street": "1 A St", "city": "Tulsa", "state": "OK", "searched": "X Y", "searched_at": "mailing"}
check("research notes reach the PDF text", "## Research notes" in run.research_pack(
    _ST, {"primary": [], "board": []}, {"summary": "", "candidates": []}, "b", {},
    "Owner is a nurse.", decedent=False))
_md = run.research_pack(_ST, {"primary": [], "board": []},
                        {"summary": "LIKELY THE OWNER'S OBITUARY", "candidates": []}, "b", {}, decedent=False)
check("living owner: the raw 'LIKELY' top finding never reaches the PDF", "LIKELY" not in _md)
check("owner deceased, traced person living: PDF says so, not 'treated as LIVING'",
      "OWNER is believed deceased" in run.research_pack(
          _ST, {"primary": [], "board": []}, {"summary": "x", "candidates": []}, "b", {},
          decedent=False, owner_dead=True))
check("living owner: name-only obituaries are left out of the PDF", "Obit Stranger" not in run.research_pack(
    _ST, {"primary": [], "board": []}, {"summary": "", "candidates": [
        {"verdict": "LIKELY", "title": "Obit Stranger", "url": "u", "dod": ""}]}, "b", {}, decedent=False))

# ── owner status is research-verified, never SmartSkip (2026-10-27) ──
# Replay of 1735 E 61st St: a name-only "PROBABLE" obituary plus SmartSkip's
# deceased flag was called a death, and the Idaho "son" was a stranger.
_probable = {"candidates": [{"verdict": "PROBABLE (Tulsa, age 71, veteran)"}]}
_confirmed = {"candidates": [{"verdict": "CONFIRMED (survivors name known family)"}]}
check("61st St replay: name-only obituary cannot make the owner VERIFIED DEAD",
      raises(lambda: dps.owner_status_line("dead", "Tulsa obituary, age 71, veteran", "John Webster",
                                           obits=_probable)))
check("SmartSkip cited as proof -> refused, either way",
      raises(lambda: dps.owner_status_line("dead", "SmartSkip flags him deceased", "J W",
                                           obits=_confirmed))
      and raises(lambda: dps.owner_status_line("alive", "smartskip says alive", "J W")))
check("dead or alive without a source -> refused",
      raises(lambda: dps.owner_status_line("dead", "", "J W", obits=_confirmed))
      and raises(lambda: dps.owner_status_line("alive", "", "J W")))
check("alive against a CONFIRMED obituary -> refused",
      raises(lambda: dps.owner_status_line("alive", "spoke with him", "J W", obits=_confirmed)))
check("dead on a CONFIRMED obituary -> VERIFIED DEAD line",
      dps.owner_status_line("dead", "Legacy obituary names his daughter", "J W", obits=_confirmed)
      .startswith("OWNER STATUS: VERIFIED DEAD - J W. Proof: Legacy"))
check("dead on a filing, no obituary -> VERIFIED DEAD line",
      "VERIFIED DEAD" in dps.owner_status_line("dead", "probate PB-2026-0760", "J W", obits=_probable))
check("alive with a source -> VERIFIED ALIVE line",
      dps.owner_status_line("alive", "owner answered Diego's call 10/27", "J W")
      .startswith("OWNER STATUS: VERIFIED ALIVE - J W"))
_unv = dps.owner_status_line("unverified", "", "John Webster", obits=_probable)
check("unverified -> NOT VERIFIED line that says not to tell anyone the owner died",
      _unv.startswith("OWNER STATUS: NOT VERIFIED") and "Do not tell anyone the owner died" in _unv)
for flag in ("decedent", "owner_deceased"):
    kw = dict(run=str(tmp), primary=["Susan Smith"], relationship=None, basis="b", repost=False,
              commit=False, owner_status="unverified", status_proof="", decedent=False,
              owner_deceased=False)
    kw[flag] = True
    check(f"--{flag.replace('_', '-')} with an unverified owner -> refused",
          raises(lambda: run.cmd_finish(argparse.Namespace(**kw))))
check("finish without --owner-status -> refused",
      raises(lambda: run.cmd_finish(argparse.Namespace(
          run=str(tmp), primary=["Susan Smith"], relationship=None, basis="b", decedent=False,
          repost=False, commit=False))))

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
