"""Offline tests for the foreclosure value-spread stop-and-ask (2026-10-09).

Rule: SiftMap estimated value minus the petition's unpaid principal balance.
Over $100k -> traced. $100k or less, or not calculable -> held on a numbered
list until the user answers "keep 1,3 drop 2". Nothing in the batch is traced
on --commit while any answer is outstanding.
Run:  python -m unittest tests.test_spread_review -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import created_rows  # noqa: E402
import spread_review as sr  # noqa: E402
from post_enrich_gate import apply_post_enrich_gate  # noqa: E402
from test_created_rows import ReRunTests, _args, _row  # noqa: E402


class Math(unittest.TestCase):
    def v(self, value, balance):
        return sr.value_spread({"estimate_value": value}, {"Unpaid Principal Balance": balance})

    def test_over_100k_passes(self):
        r = self.v("250000.00", "$149,999.99")
        self.assertTrue(r["ok"])
        self.assertAlmostEqual(r["spread"], 100000.01)

    def test_exactly_100k_is_held(self):
        self.assertFalse(self.v("250000", "150000")["ok"])

    def test_under_is_held(self):
        r = self.v(146000, 90000)
        self.assertFalse(r["ok"])
        self.assertEqual(r["spread"], 56000)

    def test_missing_numbers_are_held_and_noted(self):
        r = self.v(None, "90000")
        self.assertFalse(r["ok"])
        self.assertIn("no SiftMap estimated value", r["note"])
        r = self.v("146000", "")
        self.assertIn("no unpaid principal balance", r["note"])

    def test_junior_liens_are_ignored(self):
        r = sr.value_spread({"estimate_value": 300000},
                            {"Unpaid Principal Balance": 100000,
                             "Junior Lienholders": "HUD $80,000; IRS"})
        self.assertEqual(r["spread"], 200000)
        self.assertTrue(r["ok"])


class Answers(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(sr.parse_answers("keep 1,3,4 but drop 2,5", {1, 2, 3, 4, 5}),
                         {1: "keep", 3: "keep", 4: "keep", 2: "drop", 5: "drop"})
        self.assertEqual(sr.parse_answers("drop all", {1, 2}), {1: "drop", 2: "drop"})
        self.assertEqual(sr.parse_answers("keep 1-3 toss 4", {1, 2, 3, 4}),
                         {1: "keep", 2: "keep", 3: "keep", 4: "drop"})

    def test_bad_answers_refused(self):
        with self.assertRaises(ValueError):
            sr.parse_answers("keep 9", {1, 2})
        with self.assertRaises(ValueError):
            sr.parse_answers("keep 1 drop 1", {1, 2})
        with self.assertRaises(ValueError):
            sr.parse_answers("1,2", {1, 2})

    def test_numbers_stay_stable_and_restart_when_answered(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            sr.add([("a", {}), ("b", {})], path=p)
            sr.add([("a", {}), ("c", {})], path=p)          # a keeps its number
            self.assertEqual([(k, e["num"]) for k, e in sr.pending("abc", path=p)],
                             [("a", 1), ("b", 2), ("c", 3)])
            for k in "abc":
                sr.record_answer(k, "keep", path=p)
            sr.add([("d", {})], path=p)
            self.assertEqual(sr.pending("d", path=p)[0][1]["num"], 1)


class SafeDelete(unittest.TestCase):
    INFO = {"uuid": "u1", "street": "100 E Main St"}

    def run_it(self, prop, hit, info=None):
        deleted = []
        out = sr.safe_delete(info or self.INFO, get_property=lambda u: prop,
                             delete_property=deleted.append, find_property=lambda s: hit)
        return out, deleted

    def test_deletes_the_record_we_created(self):
        out, deleted = self.run_it({"uuid": "u1", "owner": {"phones": []}}, {"uuid": "u1"})
        self.assertEqual((out, deleted), ("deleted", ["u1"]))

    def test_never_deletes_one_with_phones(self):
        out, deleted = self.run_it({"uuid": "u1", "owner": {"phones": [{"number": "1"}]}},
                                   {"uuid": "u1"})
        self.assertEqual(deleted, [])
        self.assertIn("phone", out)

    def test_never_deletes_when_address_points_elsewhere(self):
        out, deleted = self.run_it({"uuid": "u1", "owner": {}}, {"uuid": "OTHER"})
        self.assertEqual(deleted, [])
        self.assertIn("left untouched", out)

    def test_no_uuid_no_delete(self):
        out, deleted = self.run_it({}, None, info={"street": "x"})
        self.assertEqual(deleted, [])


class GateFlags(unittest.TestCase):
    def gate(self, prop, row_extra, nt="foreclosure"):
        prop = {"uuid": "u1", "owner": {"uuid": "o1"}, "mls": "Off Market", **prop}
        row = {"Property Street": "1 A St", "Property City": "Tulsa", **row_extra}
        kept, _ = apply_post_enrich_gate(
            [row], find_property=lambda *a: {"uuid": "u1"}, get_property=lambda u: prop,
            delete_property=lambda u: self.fail("must not delete"), notice_type=nt,
            post_board=lambda o, m: None, set_status=lambda u, s: None)
        return kept[0]

    def test_thin_spread_flagged(self):
        r = self.gate({"estimate_value": "146000.00"}, {"Unpaid Principal Balance": 90000})
        self.assertEqual(r["_spread_review"]["uuid"], "u1")

    def test_wide_spread_not_flagged(self):
        r = self.gate({"estimate_value": "300000"}, {"Unpaid Principal Balance": 90000})
        self.assertNotIn("_spread_review", r)

    def test_listed_is_not_asked_about(self):
        r = self.gate({"estimate_value": "100000", "mls": "Listed"},
                      {"Unpaid Principal Balance": 90000})
        self.assertNotIn("_spread_review", r)

    def test_probate_never_flagged(self):
        r = self.gate({"estimate_value": "100000"}, {}, nt="probate")
        self.assertNotIn("_spread_review", r)


class Flow(ReRunTests):
    # _run() stubs every address lookup to record "U", and the fake gate gives
    # each held row that uuid, so the delete guard's lookup matches.
    """Dry run -> numbered list; --commit without answers traces NOTHING;
    --commit with answers traces passing + keeps as one unit."""

    def setUp(self):
        super().setUp()
        self.deleted = []
        for target, kw in (
                ("datasift_api.get_property",
                 dict(side_effect=lambda u: {"uuid": u, "owner": {"phones": []}})),
                ("datasift_api.delete_property", dict(side_effect=self.deleted.append)),
                ("datasift_uploader.forget_uuid_map_entries", dict())):
            p = mock.patch(target, **kw)
            p.start()
            self.addCleanup(p.stop)

    ROWS = [_row(1), _row(2), _row(3)]

    def test_dry_run_lists_and_returns_only_passing(self):
        out = self._run(self.ROWS, thin=("Owner2", "Owner3"))
        self.assertEqual([r["last"] for r in out], ["Owner1"])
        nums = [e["num"] for _, e in sr.pending(
            [created_rows.row_key(r, "foreclosure") for r in self.ROWS])]
        self.assertEqual(nums, [1, 2])

    def test_commit_without_answers_traces_nothing(self):
        self._run(self.ROWS, thin=("Owner2", "Owner3"))
        out = self._run(self.ROWS, thin=("Owner2", "Owner3"), args=_args(commit=True))
        self.assertIsNone(out)

    def test_commit_with_partial_answers_traces_nothing(self):
        self._run(self.ROWS, thin=("Owner2", "Owner3"))
        out = self._run(self.ROWS, args=_args(commit=True, spread_answers="keep 1"))
        self.assertIsNone(out)
        self.assertEqual(self.deleted, [])

    def test_commit_with_answers_traces_one_unit_and_drops_one(self):
        self._run(self.ROWS, thin=("Owner2", "Owner3"))
        out = self._run(self.ROWS, args=_args(commit=True, spread_answers="keep 1 drop 2"))
        self.assertEqual(sorted(r["last"] for r in out), ["Owner1", "Owner2"])
        self.assertEqual(self.deleted, ["U"])   # only #2 (Owner3) dropped
        ledger = created_rows.load()
        self.assertEqual(sorted(e["status"] for e in ledger.values()),
                         ["excluded", "trace", "trace"])
        # A later run has nothing open and traces the same two.
        again = self._run(self.ROWS, args=_args(commit=True))
        self.assertEqual(sorted(r["last"] for r in again), ["Owner1", "Owner2"])

    def test_answers_on_a_dry_run_change_nothing(self):
        self._run(self.ROWS, thin=("Owner2",))
        self._run(self.ROWS, args=_args(spread_answers="drop 1"))
        self.assertEqual(self.deleted, [])
        self.assertEqual(len(sr.pending([created_rows.row_key(r, "foreclosure")
                                         for r in self.ROWS])), 1)


del ReRunTests   # imported only as a base class; don't run its tests twice here

if __name__ == "__main__":
    unittest.main()
