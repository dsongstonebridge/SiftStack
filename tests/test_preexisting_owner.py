"""Offline tests for the owner-already-in-the-CRM check (2026-10-12).

On 2026-10-12 Kinkead, Czapansky, Carter and Huerta were created as NEW
properties, but DataSift reattached owner records (with phones) left behind
when their old properties were deleted. The property duplicate check cannot
see an owner with no property, and DataSift has no owner search, so the check
runs per record right after creation. Every CRM call here is a fake.
Run:  python -m unittest tests.test_preexisting_owner -v
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import foreclosure_checks as fc  # noqa: E402
from datasift_api import address_key  # noqa: E402


def row(**kw):
    return {"Property Street": "1629 S Ash Ave", "Property City": "Broken Arrow",
            "First Name": "Kaysley", "Last Name": "Kinkead", "Case Number": "CJ-2026-2515", **kw}


def record(phones=2, skiptraced=True, first="Kaysley", last="Kinkead",
           owner_street="1629 S Ash Ave", attempts=1):
    return {"uuid": "p1", "address": {"street": "1629 S Ash Ave"},
            "owner": {"uuid": "o1", "first_name": first, "last_name": last,
                      "phones": [{"number": f"918555000{i}"} for i in range(phones)],
                      "skiptraced": skiptraced, "skiptrace_attempts": attempts,
                      "address": {"street": owner_street}}}


def verdict(r, rec):
    return fc.preexisting_owner_verdict(r, rec, address_key=address_key)[0]


class Verdicts(unittest.TestCase):
    def test_new_owner_goes_on(self):
        self.assertEqual(verdict(row(), record(phones=0, skiptraced=None, attempts=0)), "")

    def test_same_name_at_the_property_is_skipped(self):
        self.assertEqual(verdict(row(), record()), "skip")

    def test_phones_alone_or_trace_alone_count(self):
        self.assertEqual(verdict(row(), record(phones=1, skiptraced=None, attempts=0)), "skip")
        self.assertEqual(verdict(row(), record(phones=0, skiptraced=True)), "skip")

    def test_address_spelled_differently_still_matches(self):
        self.assertEqual(verdict(row(), record(owner_street="1629 South Ash Avenue")), "skip")

    def test_owner_living_elsewhere_is_held(self):
        self.assertEqual(verdict(row(), record(owner_street="420 N Main St Ste 1")), "hold")

    def test_different_name_is_held(self):
        self.assertEqual(verdict(row(), record(first="Jennifer", last="Carter")), "hold")

    def test_not_a_duplicate_yes_releases(self):
        self.assertEqual(verdict(row(**{"Not A Duplicate": "Yes"}), record()), "")


class SkipRestoresTheCrm(unittest.TestCase):
    """A skip deletes the property we made and only OUR petition post."""

    def run_check(self, rec, posts):
        import main
        calls = {"deleted": [], "posts": []}
        with mock.patch("datasift_api.find_property_by_address", return_value={"uuid": "p1"}), \
             mock.patch("datasift_api.get_property", return_value=rec), \
             mock.patch("datasift_api.get_message_board", return_value=posts), \
             mock.patch("datasift_api.delete_property", side_effect=calls["deleted"].append), \
             mock.patch("datasift_api.delete_message_board_post",
                        side_effect=lambda o, m: calls["posts"].append(m)), \
             mock.patch("datasift_uploader.forget_uuid_map_entries"):
            out = main._skip_preexisting_owners([row()], self.since)
        return out, calls

    def setUp(self):
        self.since = datetime(2026, 10, 12, 13, 0, tzinfo=timezone.utc)

    def test_skip_deletes_property_and_only_todays_petition_post(self):
        posts = [
            {"uuid": "new", "created": "2026-10-12T13:47:44Z", "message": "FORECLOSURE PETITION\nCASE"},
            {"uuid": "old", "created": "2026-07-01T10:00:00Z", "message": "FORECLOSURE PETITION\nCASE"},
            {"uuid": "note", "created": "2026-10-12T14:00:00Z", "message": "called, left vm"},
        ]
        (go, skipped, held), calls = self.run_check(record(), posts)
        self.assertEqual((go, held), ([], []))
        self.assertEqual(len(skipped), 1)
        self.assertEqual(calls["deleted"], ["p1"])
        self.assertEqual(calls["posts"], ["new"])

    def test_hold_deletes_nothing(self):
        (go, skipped, held), calls = self.run_check(record(owner_street="9 Elsewhere Rd"), [])
        self.assertEqual((go, skipped, len(held)), ([], [], 1))
        self.assertEqual((calls["deleted"], calls["posts"]), ([], []))

    def test_unreadable_record_is_held_not_passed(self):
        import main
        with mock.patch("datasift_api.find_property_by_address", side_effect=RuntimeError("429")):
            go, skipped, held = main._skip_preexisting_owners([row()], self.since)
        self.assertEqual((go, skipped, len(held)), ([], [], 1))
        self.assertIn("429", held[0]["_owner_reason"])

    def test_new_owner_goes_on_untouched(self):
        (go, skipped, held), calls = self.run_check(record(phones=0, skiptraced=None, attempts=0), [])
        self.assertEqual((len(go), skipped, held), (1, [], []))
        self.assertEqual(calls["deleted"], [])


if __name__ == "__main__":
    unittest.main()
