"""Offline tests for the SiftMap obituary check after SmartSkip (2026-11-25). No network:
SiftMap is a fake client shaped like the live detail records read that day.
Run: python -m unittest tests.test_siftmap_obituary
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import siftmap_obituary as so  # noqa: E402


class FakeSM:
    def __init__(self, detail, title="1021 Ridge Dr, Sand Springs, OK 74063", fail=None):
        self.detail, self.title, self.fail = detail, title, fail

    def autocomplete(self, q, limit=5):
        if self.fail:
            raise self.fail
        return [{"id": 123, "title": self.title}]

    def get_detail(self, pid):
        return self.detail


def detail(obits=(), owner="Sherri L Collins", secondary=(), flag=False):
    return {"obituary_history": list(obits),
            "owner_info": {"owner_name": owner, "secondary_owner_names": list(secondary)},
            "distressor_data": {"is_obituary_property": {"is_active": flag,
                                                         "date_added": 1787011200000 if flag else None}}}


SHERRI = {"death_date": "2026-08-13", "full_name": "SHERRI COLLINS", "obit_id": "62088327"}


def check(d, searched="Sherri Lynn Collins", rec=None, title="1021 Ridge Dr, Sand Springs, OK 74063"):
    sm = so.fetch("1021 Ridge Dr", "Sand Springs", client=FakeSM(d, title))
    return sm, so.compare(sm, searched, rec)


class Fetch(unittest.TestCase):
    def test_reads_obits_and_flag(self):
        sm, _ = check(detail([SHERRI], flag=True))
        self.assertEqual(sm["status"], "ok")
        self.assertEqual(sm["obits"], [{"name": "SHERRI COLLINS", "death_date": "2026-08-13", "obit_id": "62088327"}])
        self.assertTrue(sm["flag"])
        self.assertEqual(sm["flag_since"], "2026-08-18")

    def test_wrong_parcel_is_not_used(self):
        sm, c = check(detail([SHERRI]), title="1021 Ridge Rd, Tulsa, OK 74105")
        self.assertEqual(sm["status"], "no_parcel")
        self.assertIn("could NOT run", c["lines"][0])

    def test_network_error_is_error_not_none(self):
        sm = so.fetch("1021 Ridge Dr", "Sand Springs", client=FakeSM({}, fail=RuntimeError("401")))
        self.assertEqual(sm["status"], "error")
        self.assertIsNone(so.compare(sm, "Sherri Collins")["searched_dead"])


class Compare(unittest.TestCase):
    def test_searched_person_dead_and_smartskip_says_alive(self):
        _, c = check(detail([SHERRI]), rec={"deceased": False, "relatives": []})
        self.assertEqual(c["searched_dead"]["death_date"], "2026-08-13")
        self.assertIn("SmartSkip does not flag", c["lines"][0])

    def test_co_owner_dead_highfield(self):
        d = detail([{"death_date": "2026-06-03", "full_name": "SANDRA HIGHFIELD", "obit_id": "1"}],
                   owner="Aldin Rex Highfield", secondary=["Sandra Gail Highfield"])
        _, c = check(d, searched="Aldin Rex Highfield")
        self.assertIsNone(c["searched_dead"])
        self.assertEqual(len(c["co_owner_dead"]), 1)

    def test_relative_with_the_obit_name(self):
        d = detail([{"death_date": "2026-01-07", "full_name": "BONNIE BAKER", "obit_id": "9"}], owner="X Y")
        rec = {"relatives": [{"name": "BONNIE BAKER", "canon_rel": "Mother"}]}
        _, c = check(d, searched="Mary Jones", rec=rec)
        self.assertEqual(c["relative_dead"][0]["relatives"], ["BONNIE BAKER"])

    def test_flag_without_names_and_empty(self):
        _, c = check(detail(flag=True))
        self.assertIn("gives no name", c["lines"][0])
        _, c = check(detail())
        self.assertIn("does not prove anyone is alive", c["lines"][0])


class Finish(unittest.TestCase):
    def setUp(self):
        self.ok = {"siftmap": {"status": "ok"}, "searched_dead": SHERRI | {"name": "SHERRI COLLINS"},
                   "relative_dead": [], "lines": ["SiftMap: obituary for Sherri Collins (the person searched), died 2026-08-13 (obit 62088327)."]}

    def test_alive_refused_when_siftmap_has_the_obit(self):
        self.assertTrue(so.finish_problems(self.ok, "alive", {}))
        self.assertEqual(so.finish_problems(self.ok, "dead", {}), [])
        self.assertEqual(so.finish_problems(self.ok, "unverified", {}), [])
        self.assertEqual(so.finish_problems(self.ok, "alive", {}, call="Jeff: obit is her mother"), [])

    def test_check_that_did_not_run_waits(self):
        bad = {"siftmap": {"status": "error", "error": "401"}}
        self.assertIn("did not run", so.finish_problems(bad, "dead", {})[0])
        self.assertIn("did not run", so.finish_problems(None, "dead", {})[0])

    def test_dead_relative_must_be_in_research(self):
        d = {"siftmap": {"status": "ok"}, "searched_dead": None, "relative_dead": [
            {"name": "BONNIE BAKER", "death_date": "2026-01-07", "relatives": ["BONNIE BAKER"]}], "lines": []}
        self.assertTrue(so.finish_problems(d, "dead", {}))
        self.assertEqual(so.finish_problems(d, "dead", {"dead": ["Bonnie Baker"]}), [])
        self.assertEqual(so.finish_problems(d, "dead", {"not_family": ["BONNIE BAKER"]}), [])

    def test_status_note_carries_the_death_to_the_board(self):
        self.assertIn("died 2026-08-13", so.status_note(self.ok))
        self.assertEqual(so.status_note({"siftmap": {"status": "error"}}), "")


class RunCheck(unittest.TestCase):
    def test_saves_and_replaces_profile_block(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / "owner_profile.txt").write_text("OWNER PROFILE\n", encoding="utf-8")
        st = {"street": "1021 Ridge Dr", "city": "Sand Springs", "searched": "Sherri Lynn Collins"}
        for _ in range(2):
            so.run_check(d, st, {"deceased": False}, client=FakeSM(detail([SHERRI])))
        txt = (d / "owner_profile.txt").read_text(encoding="utf-8")
        self.assertEqual(txt.count("== SiftMap obituary check"), 1)
        self.assertIsNotNone(json.loads((d / so.FILE).read_text())["searched_dead"])


if __name__ == "__main__":
    unittest.main()
