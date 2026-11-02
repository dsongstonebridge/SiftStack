"""Offline checks for the Dial First/Second trial. No network.

Run: python tests/test_dp_board_split.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import dp_board_split as s

OLD = """Owner status: DECEASED (high)
SMARTSKIP FAMILY - WHO IS WHO (2026-11-02)
Traced: Bruce Bear. Call first: ELISE BEAR.
Name | relationship | number | dial tier. Numbers marked 'board only - not on phone list' were not added.

CALL FIRST | ELISE BEAR | Daughter | 918-331-7781 | Dial First
CALL FIRST | ELISE BEAR | Daughter | 918-336-4181 | Dial Fourth
FAMILY | Bruce Bear | Decedent (traced subject) | 918-743-0822 | board only - not on phone list
FAMILY | Bruce Bear | Decedent (traced subject) | 918-764-9859 | board only - not on phone list
FAMILY | WILLIAM BEAR | Parent | none |  | age 92, TULSA OK
FAMILY | JAMIE BEAR | Relative | 918-231-7912 | board only - not on phone list | age 36, PERKINS OK
"""


def row(number, *tags, status="UNKNOWN"):
    return {"number": number, "tags": list(tags), "status": status, "type": "MOBILE"}


FAILS = []


def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        FAILS.append(name)


rows = [row("9183317781", "SmartSkip", "Dial First", "Daughter"),
        row("9183364181", "SmartSkip", "Dial Fourth", "Daughter"),
        row("9182423770", "DataSift", "Dial Fourth", "Daughter")]
plan = s.plan_numbers(rows)
check("First/Second stay", [r["number"] for r in plan["keep"]] == ["9183317781"])
check("any source below Second goes", {r["number"] for r in plan["remove"]} == {"9183364181", "9182423770"})

odd = s.plan_numbers([row("9180000001", "DataSift"),
                      row("9180000002", "Dial Third", status="CORRECT"),
                      row("9180000003", "Drop", status="NO_ANSWER")])
check("no tier or a caller's status is held, never removed",
      not odd["remove"] and len(odd["held"]) == 3)

old = s.parse_old_post(OLD)
check("status line kept", old["status_lines"] == ["Owner status: DECEASED (high)"])
names = [p["name"] for p in old["people"]]
check("each person once", names == ["ELISE BEAR", "Bruce Bear", "WILLIAM BEAR", "JAMIE BEAR"])

check("a label for a name not on the post is caught", s.apply_labels(old, {"NOBODY": "x"}) == ["NOBODY"])
s.apply_labels(old, {"WILLIAM BEAR": "Died before him (obituary)"})
post = s.build_post(old, rows, plan, owner_of_record="BEAR, BRUCE", named=["Barbara, Mother (obituary)"],
                    statuses=["Bruce Bear: DEAD, died 05/31/2026 (filing, obituary)"],
                    today="2026-11-02")
lines = post.splitlines()
check("post opens with the owner of record", lines[0] == "OWNER OF RECORD: BEAR, BRUCE")
check("then alive or dead", lines[1] == "STATUS: Bruce Bear: DEAD, died 05/31/2026 (filing, obituary)")
check("title holder read off the owner board", s.title_holder(
    [{"message": "x"}, {"message": "DECEDENT" + chr(10) + "  Title Holder of Record: BEAR, BRUCE"}]) == "BEAR, BRUCE")
gone = s.build_post(old, rows[:1], s.plan_numbers(rows[:1]), owner_of_record="B", statuses=["x"])
check("tier survives from the old post after removal",
      "918-331-7781 (Dial First); 918-336-4181 (Dial Fourth)" in gone)
check("one line per person, semicolons, best first",
      "ELISE BEAR, Daughter: 918-331-7781 (Dial First); 918-336-4181 (Dial Fourth)" in lines)
check("unscored numbers carry no tier",
      "Bruce Bear, Decedent (traced subject): 918-743-0822; 918-764-9859" in lines)
check("corrected label replaces SmartSkip's", "WILLIAM BEAR, Died before him (obituary), age 92, TULSA OK: no numbers" in lines)
check("people SmartSkip missed are listed", "Barbara, Mother (obituary)" in lines)
check("number from another skip trace is on the post",
      any(l.startswith("918-242-3770 (Dial Fourth)") and "DataSift" in l for l in lines))
check("every removed number is on the post",
      not s.missing_from_post(post, [r["number"] for r in plan["remove"]]))
check("gap check catches a missing number", s.missing_from_post(post, ["9185551234"]) == ["9185551234"])
check("no death word on a line naming someone else",
      not any("Decedent" in l and "ELISE" in l for l in lines))

check("kept number unchanged passes", not s.changed_kept(rows[:1], rows))
check("a lost tag is caught",
      s.changed_kept(rows[:1], [row("9183317781", "SmartSkip", "Dial First")]) == ["9183317781"])
check("a lost number is caught", s.changed_kept(rows[:1], []) == ["9183317781"])

check("a record with only low numbers keeps them", s.removable_after_post([row("9180000009", "Dial Fourth")]) == [])
check("a record with a good number loses its low ones", s.removable_after_post(
    [row("9180000009", "Dial Fourth"), row("9180000008", "Dial First")]) == ["9180000009"])


class FakeAPI:
    def get_property(self, uuid):
        return {"owner": {"phones": []}, "tags": ["Courthouse Data"]}

    def get_property_message_board(self, uuid):
        return [{"message": OLD}]


import json, tempfile  # noqa: E402
regular = Path(tempfile.mkdtemp()) / "x"
regular.mkdir()
(regular / "state.json").write_text(json.dumps({"street": "1 A St", "property_uuid": "P", "owner_uuid": "O"}))
s.api = FakeAPI()
s.api.get_property = lambda uuid: {"owner": {"phones": [
    {"number": "9180000009", "tags": ["Dial Fourth"], "status": "UNKNOWN"},
    {"number": "9180000008", "tags": ["Dial First"], "status": "UNKNOWN"}]}, "tags": ["Courthouse Data"]}
check("a record that was not deep prospected is refused", s.run(regular, commit=True) == 2)

print(f"\n{len(FAILS)} failed")
raise SystemExit(1 if FAILS else 0)
