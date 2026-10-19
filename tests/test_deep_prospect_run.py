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


class FakeAPI:
    DataSiftAPIError = Exception

    def __init__(self):
        self.phones, self.board, self.uploads, self.dry = [], [], [], False

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

    def get_message_board(self, owner_uuid):
        return [{"message": m} for m in self.board]

    def post_message_board(self, owner_uuid, message):
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
        repost=repost, commit=commit))


finish(commit=False)
check("dry run: nothing written, nothing uploaded", not fake.phones and not fake.board and not fake.uploads)
check("dry run still builds the PDF", any(p.suffix == ".pdf" for p in tmp.iterdir()))
check("record resolved from the run's address, not SmartSkip's upper-case copy",
      fake.looked_up == ("100 Fake St", "Tulsa"))

finish(commit=True)
check("commit: only the top heir's number on the phone list", [p["number"] for p in fake.phones] == ["9185550201"])
check("commit: her number tagged Daughter", "Daughter" in fake.phones[0]["tags"])
check("commit: PDF uploaded to Documents", len(fake.uploads) == 1)
check("commit: board has the relatives post and the PDF line",
      any(m.startswith(dps.BOARD_MARKER) for m in fake.board)
      and any(m.startswith(run.PDF_MARKER) for m in fake.board))
md = next(p for p in tmp.iterdir() if p.suffix == ".md").read_text(encoding="utf-8")
check("research pack names the basis and the obituary", "Susan is the daughter" in md and "example.test/obit" in md)
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

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
