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


if __name__ == "__main__":
    unittest.main()
