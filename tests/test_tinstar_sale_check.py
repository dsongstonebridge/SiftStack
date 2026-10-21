"""Offline tests for the weekly TinStar sheriff-sale check (src/tinstar_sale_check.py).

Everything that touches DataSift, TinStar or the screen is mocked; state and
findings live in a temp dir. Run:  python tests/test_tinstar_sale_check.py -v
"""

import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import tinstar_sale_check as t  # noqa: E402


def _sale(addr="9107 E. 66TH ST.", city="TULSA", sale_date="2026-11-04", **kw):
    s = {"id": 1, "caseNumber": "CJ-2023-1858", "address": addr, "city": city,
         "state": "OK", "zipCode": "74133", "plaintiff": "FREEDOM MORTGAGE",
         "defendant": "SMITH, JOHN", "saleDate": sale_date,
         "appraisalTinstar": "$223,700", "recalled": None, "noSale": None}
    s.update(kw)
    return s


def _rec(uuid, street, city="Tulsa", zip_="74133", last="Smith"):
    return {"uuid": uuid, "address": {"street": street, "city": city, "postal_code": zip_},
            "owner": {"uuid": "own-" + uuid, "first_name": "John", "last_name": last}}


class Classify(unittest.TestCase):
    def test_same_house_written_differently_is_an_exact_match(self):
        status, hits = t.classify(_sale(), [_rec("a", "9107 E 66th St")])
        self.assertEqual(status, "match")
        self.assertEqual(hits[0]["record"]["uuid"], "a")

    def test_numbered_avenue_both_ways_round_matches(self):
        status, _ = t.classify(_sale("449 S. 112TH E. AVE."), [_rec("a", "449 S 112th East Ave")])
        self.assertEqual(status, "match")

    def test_different_street_type_is_flagged_not_matched(self):
        # In Tulsa E 66th St and E 66th Pl are different streets
        status, hits = t.classify(_sale(), [_rec("a", "9107 E 66th Pl")])
        self.assertEqual(status, "flag")
        self.assertIn("street type or direction differs", hits[0]["reasons"])

    def test_trailing_direction_difference_is_flagged(self):
        status, _ = t.classify(_sale("1752 E 56TH ST"), [_rec("a", "1752 E 56th St S")])
        self.assertEqual(status, "flag")

    def test_misspelling_is_flagged(self):
        status, hits = t.classify(_sale("119 W. HONOLULU ST."), [_rec("a", "119 W Honalulu St")])
        self.assertEqual(status, "flag")
        self.assertTrue(any("spelled differently" in r for r in hits[0]["reasons"]))

    def test_same_street_other_city_is_flagged(self):
        status, hits = t.classify(_sale(), [_rec("a", "9107 E 66th St", city="Broken Arrow")])
        self.assertEqual(status, "flag")
        self.assertTrue(any("city differs" in r for r in hits[0]["reasons"]))

    def test_unrelated_street_same_house_number_is_ignored(self):
        status, _ = t.classify(_sale(), [_rec("a", "9107 S Memorial Dr")])
        self.assertEqual(status, "none")

    def test_two_records_at_one_address_need_a_human(self):
        status, hits = t.classify(_sale(), [_rec("a", "9107 E 66th St"), _rec("b", "9107 E 66TH ST")])
        self.assertEqual(status, "duplicate")
        self.assertEqual(len(hits), 2)


class CrmIsReadOnly(unittest.TestCase):
    # Jeff, 2026-10-21: read-only CRM, Message Board posts the ONLY write.
    ALLOWED = {"search_by_address", "get_property", "get_property_message_board",  # reads
               "post_property_message_board",                                     # the one write
               "set_dry_run", "DataSiftAPIError", "_norm_street", "_bare_street"}

    def test_only_message_board_posts_write_to_the_crm(self):
        import re
        src = Path(t.__file__).read_text(encoding="utf-8")
        used = set(re.findall(r"_api\.([A-Za-z_]+)", src))
        self.assertTrue(used, "found no datasift_api calls at all - the scan is broken")
        self.assertEqual(used - self.ALLOWED, set())
        self.assertNotIn("import datasift_uploader", src)
        self.assertNotIn("skip_trace_agent", src)

    def test_never_touches_an_owner_record(self):
        # Jeff, 2026-10-21: posts go to the PROPERTY board; owner records are never written.
        src = Path(t.__file__).read_text(encoding="utf-8")
        for bad in ("_api.post_message_board", "_api.get_message_board", "/owner/",
                    "owner_uuid"):
            self.assertNotIn(bad, src)


class Feed(unittest.TestCase):
    def test_keeps_only_upcoming_public_columns(self):
        rows = [dict(_sale(), buyerEmail="x@y.com", buyerPhone="9185551234"),
                dict(_sale(sale_date="2026-10-14")),
                dict(_sale(sale_date="2026-11-18"), hideFromReports=True)]
        out = t.filter_upcoming(rows, date(2026, 10, 21))
        self.assertEqual(len(out), 1)
        self.assertNotIn("buyerEmail", out[0])
        self.assertNotIn("buyerPhone", out[0])

    def test_zero_upcoming_is_a_failure(self):
        with self.assertRaises(RuntimeError):
            t.filter_upcoming([_sale(sale_date="2026-01-07")], date(2026, 10, 21))


class Schedule(unittest.TestCase):
    def test_cycle_is_this_monday_after_9(self):
        self.assertEqual(t.due_cycle(datetime(2026, 10, 26, 9, 0)), "2026-10-26")
        self.assertEqual(t.due_cycle(datetime(2026, 10, 28, 15, 0)), "2026-10-26")

    def test_before_9_monday_still_belongs_to_last_week(self):
        self.assertEqual(t.due_cycle(datetime(2026, 10, 26, 8, 59)), "2026-10-19")


class Posting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [mock.patch.object(t, "OUT_DIR", d),
                        mock.patch.object(t, "STATE_PATH", d / "state.json"),
                        mock.patch.object(t, "LATEST_PATH", d / "latest.json"),
                        mock.patch.object(t, "LOG_PATH", d / "check.log")]
        for p in self.patches:
            p.start()
        self.board: dict[str, list] = {}

        def post(owner, msg):
            self.board.setdefault(owner, []).insert(0, {"message": msg})
        self.api = [mock.patch.object(t._api, "post_property_message_board", side_effect=post),
                    mock.patch.object(t._api, "get_property_message_board",
                                      side_effect=lambda o: list(self.board.get(o, [])))]
        for p in self.api:
            p.start()

    def tearDown(self):
        for p in self.patches + self.api:
            p.stop()
        self.tmp.cleanup()

    def _rec(self):
        return {"uuid": "p1"}

    def test_post_carries_date_and_appraisal_and_lands_once(self):
        state = {}
        self.assertEqual(t.post_sale(self._rec(), _sale(), state, date(2026, 10, 26)), "posted")
        msg = self.board["p1"][0]["message"]
        self.assertIn("Wednesday, November 4, 2026", msg)
        self.assertIn("$223,700", msg)
        self.assertNotIn("—", msg)
        again = t.post_sale(self._rec(), _sale(), state, date(2026, 11, 2))
        self.assertTrue(again.startswith("already posted"))
        self.assertEqual(len(self.board["p1"]), 1)

    def test_post_already_on_board_is_not_repeated(self):
        t._post_verified("p1", t.board_text(_sale(), date(2026, 10, 19)))
        self.assertEqual(t.post_sale(self._rec(), _sale(), {}, date(2026, 10, 26)),
                         "already on the board")
        self.assertEqual(len(self.board["p1"]), 1)

    def test_moved_sale_date_gets_a_change_post(self):
        state = {}
        t.post_sale(self._rec(), _sale(), state, date(2026, 10, 26))
        r = t.post_sale(self._rec(), _sale(sale_date="2026-11-18"), state, date(2026, 11, 2))
        self.assertEqual(r, "posted (date changed)")
        self.assertIn("Was: Wednesday, November 4, 2026", self.board["p1"][0]["message"])

    def test_recalled_sale_is_not_posted(self):
        r = t.post_sale(self._rec(), _sale(recalled=True), {}, date(2026, 10, 26))
        self.assertEqual(r, "not posted: sale already recalled")
        self.assertNotIn("p1", self.board)

    def test_recall_after_posting_gets_one_recall_post(self):
        state = {}
        t.post_sale(self._rec(), _sale(), state, date(2026, 10, 26))
        out = t.post_recalls([_sale(recalled=True)], state, date(2026, 11, 2))
        self.assertEqual(len(out), 1)
        t.post_recalls([_sale(recalled=True)], state, date(2026, 11, 9))
        self.assertEqual(len(self.board["p1"]), 2)
        self.assertIn(t.RECALL_MARKER, self.board["p1"][0]["message"])

    def test_post_that_does_not_land_raises(self):
        with mock.patch.object(t._api, "get_property_message_board", return_value=[]):
            with self.assertRaises(RuntimeError):
                t.post_sale(self._rec(), _sale(), {}, date(2026, 10, 26))

    def test_scheduled_runs_once_per_week_and_notifies(self):
        res = {"sales": 93, "counts": {"match": 1, "duplicate": 0, "flag": 2, "none": 90},
               "findings": [{"records": [{"post": "posted"}]}]}
        with mock.patch.object(t, "run", return_value=res) as run, \
                mock.patch.object(t, "notify") as note:
            self.assertEqual(t.scheduled(datetime(2026, 10, 26, 9, 5)), 0)
            self.assertEqual(t.scheduled(datetime(2026, 10, 26, 9, 20)), 0)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(note.call_count, 1)
            self.assertIn("2 need your OK", note.call_args[0][1])

    def test_dismissed_flag_is_never_flagged_again(self):
        import json
        f = {"n": 54, "status": "flag", "sale": _sale(), "street": "9107 E 66TH ST",
             "records": [{"uuid": "p1", "reasons": []}]}
        t.LATEST_PATH.write_text(json.dumps({"findings": [f]}), encoding="utf-8")
        t.dismiss_findings("54")
        with mock.patch.object(t, "fetch_sales", return_value=[_sale()]), \
                mock.patch.object(t, "_candidates", return_value=[_rec("p1", "9107 E 66th Pl")]), \
                mock.patch.object(t._api, "get_property", return_value={}):
            res = t.run(dry=False, today=date(2026, 10, 26))
        self.assertEqual(res["findings"][0]["status"], "dismissed")
        text = t.summary_text(res)
        self.assertIn("0 need your OK", text)
        self.assertNotIn("post --confirm", text)
        self.assertNotIn("p1", self.board)

    def test_confirmed_flag_is_not_asked_again_next_week(self):
        state = {"posted": {"p1|CJ-2023-1858|2026-11-04": {"on": "2026-10-21", "board": "property"}}}
        t.save_state(state)
        with mock.patch.object(t, "fetch_sales", return_value=[_sale()]), \
                mock.patch.object(t, "_candidates", return_value=[_rec("p1", "9107 E 66th Pl")]), \
                mock.patch.object(t._api, "get_property", return_value={}):
            res = t.run(dry=False, today=date(2026, 10, 26))
        self.assertEqual(res["findings"][0]["status"], "confirmed")
        self.assertNotIn("post --confirm", t.summary_text(res))
        self.assertNotIn("p1", self.board)

    def test_failure_notifies_once_after_three_tries(self):
        with mock.patch.object(t, "run", side_effect=RuntimeError("down")), \
                mock.patch.object(t, "notify") as note:
            for _ in range(5):
                self.assertEqual(t.scheduled(datetime(2026, 10, 26, 10, 0)), 1)
            self.assertEqual(note.call_count, 1)
            self.assertIn("FAILED", note.call_args[0][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
