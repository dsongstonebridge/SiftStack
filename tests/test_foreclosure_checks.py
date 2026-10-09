"""Offline tests for the foreclosure pre-create checks (2026-10-09).

Every CRM and Assessor call is a fake. The Assessor owner strings are real
formats read live: "FULTON, JOHNNIE SR C/O FULTON, ADDIOUS D REV LIVING TRUST",
"ROSS, CHARLES R II", "LEELLALL, LLC".
Run:  python -m unittest tests.test_foreclosure_checks -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import foreclosure_checks as fc  # noqa: E402
from siftmap_address import parse_street  # noqa: E402


def row(street, last="Smith", **kw):
    return {"Property Street": street, "Property City": "Tulsa", "First Name": "Pat",
            "Last Name": last, "Case Number": f"CJ-{street[:4]}", **kw}


class CrmDuplicates(unittest.TestCase):
    def test_existing_is_skipped_new_goes_on(self):
        crm = {"100 E Main St": {"uuid": "u1", "address": {"street": "100 E Main St"}}}
        new, dupes, failed = fc.crm_duplicate_check(
            [row("100 E Main St"), row("200 E Main St")], find_property=crm.get)
        self.assertEqual([r["Property Street"] for r in new], ["200 E Main St"])
        self.assertEqual(dupes[0]["_dup_uuid"], "u1")
        self.assertEqual(failed, [])

    def test_failed_lookup_is_held_not_created(self):
        def boom(street):
            raise RuntimeError("503")
        new, dupes, failed = fc.crm_duplicate_check([row("100 E Main St")],
                                                    find_property=boom)
        self.assertEqual((new, dupes), ([], []))
        self.assertIn("503", failed[0]["_dup_reason"])

    def test_repeat_within_batch_dropped(self):
        new, dupes, _ = fc.crm_duplicate_check(
            [row("100 E Main St"), row("100  e main st", last="Jones")],
            find_property=lambda s: None)
        self.assertEqual(len(new), 1)
        self.assertIn("repeats", dupes[0]["_dup_reason"])

    def test_strict_lookup_raises_on_api_error(self):
        import datasift_api as api
        with mock.patch.object(api, "search_by_address",
                               side_effect=api.DataSiftAPIError("429")):
            self.assertIsNone(api.find_property_by_address("100 E Main St"))
            with self.assertRaises(api.DataSiftAPIError):
                api.find_property_by_address("100 E Main St", strict=True)


def assessor(*recs):
    return lambda terms: [dict(AccountNo=a, FullPropertyStreet=s, FullPrimaryOwnerName=o)
                          for a, s, o in recs]


class OwnerOfRecord(unittest.TestCase):
    def check(self, rows, search):
        with mock.patch.object(fc.time, "sleep"):
            return fc.owner_of_record_check(rows, search=search, parse_street=parse_street)

    def test_matching_owner_goes_on(self):
        go, held, unchecked = self.check(
            [row("9921 East 114th Place South", last="Ross")],
            assessor(("R1", "9921 E 114 PL S", "ROSS, CHARLES R II"),
                     ("R2", "123 E 21 ST S", "URBAN PASTURE LLC")))
        self.assertEqual((len(go), held, unchecked), (1, [], []))

    def test_different_owner_is_held(self):
        go, held, _ = self.check(
            [row("2950 S Woodward Blvd", last="Smith")],
            assessor(("R3", "2950 S WOODWARD BV E", "LEELLALL, LLC")))
        self.assertEqual(go, [])
        self.assertEqual(held[0]["_owner_of_record"], "LEELLALL, LLC")

    def test_owner_confirmed_releases_a_held_row(self):
        search = mock.Mock(side_effect=AssertionError("must not search"))
        go, held, _ = self.check([row("2950 S Woodward Blvd", **{"Owner Confirmed": "Yes"})],
                                 search)
        self.assertEqual((len(go), held), (1, []))

    def test_deceased_owner_named_in_owner_status_matches(self):
        r = row("4503 N Iroquois Ave", last="Faulk",
                **{"Owner Status": "Deceased; Johnnie Fulton Sr., heirs unknown"})
        go, held, _ = self.check(
            [r], assessor(("R4", "4503 N IROQUOIS AV E",
                           "FULTON, JOHNNIE SR C/O FULTON, ADDIOUS D REV LIVING TRUST")))
        self.assertEqual((len(go), held), (1, []))

    def test_co_borrower_surname_matches(self):
        r = row("100 E Main St", last="Smith", **{"Co-Borrower Last Name": "Garcia"})
        go, held, _ = self.check([r], assessor(("R5", "100 E MAIN ST", "GARCIA, MARIA")))
        self.assertEqual((len(go), held), (1, []))

    def test_a_first_name_does_not_count_as_a_match(self):
        # Owner's FIRST name equals the defendant's last name: still a different owner.
        go, held, _ = self.check([row("100 E Main St", last="James")],
                                 assessor(("R6", "100 E MAIN ST", "WRIGHT, JAMES")))
        self.assertEqual(go, [])
        self.assertEqual(len(held), 1)

    def test_fails_open_when_no_or_ambiguous_parcel(self):
        go, held, unchecked = self.check([row("100 E Main St")], assessor())
        self.assertEqual((len(go), held, len(unchecked)), (1, [], 1))
        go, held, unchecked = self.check(
            [row("100 E Main St")],
            assessor(("A", "100 E MAIN ST", "X, Y"), ("B", "100 E MAIN ST", "Z, Q")))
        self.assertEqual((len(go), held, len(unchecked)), (1, [], 1))

    def test_wrong_house_or_direction_is_not_a_match(self):
        go, held, unchecked = self.check(
            [row("100 E Main St")],
            assessor(("A", "1000 E MAIN ST", "X, Y"), ("B", "100 W MAIN ST", "Z, Q")))
        self.assertEqual((len(go), held, len(unchecked)), (1, [], 1))


class BackfillOwnership(unittest.TestCase):
    """--backfill: owner of record + deeds since filing, unchecked = held."""

    def check(self, rows, search, history=(), hold_unchecked=True):
        with mock.patch.object(fc.time, "sleep"):
            return fc.owner_of_record_check(
                rows, search=search, parse_street=parse_street,
                sales_history=lambda acct: list(history), hold_unchecked=hold_unchecked)

    SEARCH = staticmethod(assessor(("R1", "100 E MAIN ST", "SMITH, PAT")))

    def test_sheriff_deed_after_filing_is_held(self):
        r = row("100 E Main St", **{"Date Foreclosure Filed": "03/14/2025"})
        go, held, _ = self.check([r], self.SEARCH, history=[
            {"sale_date": "11/2/2025", "grantor": "SMITH, PAT", "deed_type": "Sheriff's Deed",
             "grantee": "LAKEVIEW LOAN SERVICING LLC", "sale_price": 150000},
            {"sale_date": "6/1/2015", "grantor": "JONES, A", "grantee": "SMITH, PAT",
             "deed_type": "Warranty Deed", "sale_price": 120000}])
        self.assertEqual(go, [])
        self.assertIn("Sheriff's Deed", held[0]["_hold_reason"])
        self.assertNotIn("2015", held[0]["_hold_reason"])

    def test_only_older_deeds_goes_on(self):
        from datetime import datetime
        r = row("100 E Main St", **{"Date Foreclosure Filed": datetime(2025, 3, 14)})
        go, held, _ = self.check([r], self.SEARCH, history=[
            {"sale_date": "6/1/2015", "grantor": "JONES, A", "grantee": "SMITH, PAT"}])
        self.assertEqual((len(go), held), (1, []))

    def test_deed_on_the_filing_day_counts(self):
        r = row("100 E Main St", **{"Date Foreclosure Filed": "2025-03-14"})
        _, held, _ = self.check([r], self.SEARCH, history=[
            {"sale_date": "3/14/2025", "grantor": "SMITH, PAT", "grantee": "X LLC"}])
        self.assertEqual(len(held), 1)

    def test_street_type_breaks_a_tie(self):
        # Live 2026-10-09: two parcels at 3431 S 116th, a Place and an Avenue.
        search = assessor(
            ("R74905942000600", "3431 S 116 PL E", "GUZMAN, ABIMAEL CORREA & JOHANNA SALAZAR CEDENO"),
            ("R74905942000710", "3431 S 116 AV E", "MCCLENDON PROPERTIES LLC 116TH STREET SERIES"))
        go, held, _ = self.check([row("3431 S 116th Place", last="Correa Guzman")], search)
        self.assertEqual((len(go), held), (1, []))
        _, held, _ = self.check([row("3431 S 116th E Ave", last="Correa Guzman")], search)
        self.assertIn("not on the petition", held[0]["_hold_reason"])

    def test_unchecked_is_held_in_backfill(self):
        go, held, unchecked = self.check([row("100 E Main St")], assessor())
        self.assertEqual((go, len(held), len(unchecked)), ([], 1, 1))
        self.assertIn("could not check", held[0]["_hold_reason"])

    def test_owner_mismatch_reason(self):
        _, held, _ = self.check([row("100 E Main St", last="Jones")], self.SEARCH)
        self.assertIn("not on the petition", held[0]["_hold_reason"])


class OnlyOnBackfill(unittest.TestCase):
    def test_daily_run_never_touches_the_assessor(self):
        import main
        with mock.patch("main._crm_duplicate_check", side_effect=lambda rs: (rs, [], [])),              mock.patch("tulsa_assessor.search_assessor",
                        side_effect=AssertionError("daily must not hit the Assessor")):
            out = main._foreclosure_precreate_checks([row("100 E Main St")], mock.Mock())
        self.assertEqual(len(out), 1)

    def test_backfill_runs_it(self):
        import main
        with mock.patch("main._crm_duplicate_check", side_effect=lambda rs: (rs, [], [])),              mock.patch("tulsa_assessor.search_assessor", return_value=[]) as s,              mock.patch.object(fc.time, "sleep"),              mock.patch("foreclosure_checks.write_owner_review", return_value="r.csv"):
            out = main._foreclosure_precreate_checks([row("100 E Main St")], mock.Mock(),
                                                     backfill=True)
        self.assertTrue(s.called)
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
