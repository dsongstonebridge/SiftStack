"""Offline tests: the review step STOPS a probate row that is not straightforward.

User, 2026-09-11, after the daughter's mailing address went into the CRM as a
decedent's property: "On all the situations that aren't straightforward, you
need to stop and ask me." Straightforward = the decedent is the title holder of
record, found on the Tulsa County sites. Everything else BLOCKS until the sheet
says Property Confirmed = Yes (set only on the user's say-so).

Run:  python tests/test_probate_review_stop.py -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from batch_review import BLOCK, WARN, review_batch  # noqa: E402
import skip_trace_agent as agent  # noqa: E402


def _row(**kw):
    base = {"Property Street": "4529 E Xyler St N", "Property City": "Tulsa",
            "Property State": "OK", "Property Zip": "74115",
            "First Name": "Rhonda", "Last Name": "Thomas",
            "Mailing Street": "2945 N Fern Ct",
            "Decedent Name": "Clifton Lee Ross", "Personal Representative": "Rhonda Thomas",
            "Title Holder of Record": "ROSS, CLIFTON LEE",
            "Real Property Stated": "Yes - real and personal", "Vacant Lot": "No",
            "AcctType": "Residential"}
    base.update(kw)
    return base


def _johnson(**kw):
    base = _row(**{"Property Street": "1916 S 140th East Ave", "Property Zip": "74108",
                   "First Name": "Wendy", "Last Name": "Johnson",
                   "Mailing Street": "1916 S 140th East Ave",
                   "Decedent Name": "Tina Fay Johnson",
                   "Personal Representative": "Larry Kaiser",
                   "Title Holder of Record": "L & S GROUP LLC"})
    base.update(kw)
    return base


def _found(rows, sev, notice_type="probate"):
    return [" ".join(str(v) for v in f.values())
            for f in review_batch(rows, notice_type=notice_type) if f["severity"] == sev]


class StraightforwardTests(unittest.TestCase):
    def test_decedent_on_title_passes(self):
        self.assertEqual(_found([_row()], BLOCK), [])

    def test_decedents_own_trust_passes(self):
        row = _row(**{"Decedent Name": "Robert Clarence Lovelace",
                      "Title Holder of Record":
                      "LOVELACE, ROBERT C C/O LOVELACE, ROBERT C REV LIVING TRUST"})
        self.assertEqual(_found([row], BLOCK), [])

    def test_heir_living_in_decedents_house_is_only_a_warning(self):
        # Mailing == property is legitimate when the decedent IS on title.
        row = _row(**{"Mailing Street": "4529 E Xyler St N"})
        self.assertEqual(_found([row], BLOCK), [])
        self.assertTrue(any("equals the property address" in w for w in _found([row], WARN)))

    def test_named_heir_on_title_passes(self):
        # Bitson-shaped (video, 2026-09-14): the living spouse/PR held title,
        # never in the decedent's own name at all - still clean.
        row = _row(**{"Decedent Name": "Pamela Irene Finley Bitson",
                      "Personal Representative": "D'Angelo Bitson Sr.",
                      "Title Holder of Record": "BITSON, D ANGELO"})
        self.assertEqual(_found([row], BLOCK), [])


class NotStraightforwardTests(unittest.TestCase):
    def test_johnson_blocks_twice_over(self):
        blocks = _found([_johnson()], BLOCK)
        self.assertTrue(any("NOT STRAIGHTFORWARD" in b and "L & S GROUP LLC" in b
                            for b in blocks), blocks)
        self.assertTrue(any("MAILING address is being used as the PROPERTY" in b
                            for b in blocks), blocks)

    def test_no_title_holder_blocks(self):
        blocks = _found([_row(**{"Title Holder of Record": ""})], BLOCK)
        self.assertTrue(any("no title holder of record" in b for b in blocks), blocks)

    def test_chain_of_deaths_root_owner_blocks(self):
        row = _row(**{"Decedent Name": "Alfred Fulton",
                      "Title Holder of Record": "FULTON, JOHNNIE SR",
                      "First Name": "Jennifer", "Last Name": "Faulk"})
        self.assertTrue(_found([row], BLOCK))

    def test_user_confirmation_turns_the_stop_into_a_warning(self):
        row = _johnson(**{"Property Confirmed": "Yes"})
        self.assertEqual(_found([row], BLOCK), [])
        self.assertTrue(any("user confirmed" in w for w in _found([row], WARN)))

    def test_insider_transfer_blocks_even_when_title_matches_a_named_party(self):
        # Johnson-shaped, but with the title holder set to the PR directly so
        # the "title matches a named party" branch is the one being tested,
        # isolated from the plain "title holder is a stranger" block.
        row = _johnson(**{
            "Title Holder of Record": "Larry Kaiser",
            "Insider Transfer": ("12/31/2013: KAISER, LARRY A AND SHELLEY A -> "
                                 "L & S GROUP LLC (Quit Claim Deed)")})
        blocks = _found([row], BLOCK)
        self.assertTrue(any("NOT STRAIGHTFORWARD" in b and "transfer connected to this estate" in b
                            for b in blocks), blocks)

    def test_foreclosure_rows_are_untouched(self):
        row = {"Property Street": "7405 S Chestnut Ave", "Property City": "Broken Arrow",
               "First Name": "Andrew", "Last Name": "Nordquist", "Vacant Lot": "No",
               "AcctType": "Residential"}
        self.assertFalse(any("STRAIGHTFORWARD" in b
                             for b in _found([row], BLOCK, notice_type="foreclosure")))


class PRStatusTests(unittest.TestCase):
    def test_petitioner_is_not_presented_as_appointed(self):
        b = agent._signing_chain_block({
            "name": "Wendy Johnson", "decedent_name": "Tina Fay Johnson",
            "personal_representative": "Larry Kaiser",
            "pr_status": "Petitioner - not yet appointed (hearing 10/12/2026); a creditor, NOT an heir"})
        self.assertIn("Personal Rep (per the probate filing): Larry Kaiser - Petitioner - "
                      "not yet appointed (hearing 10/12/2026)", b)

    def test_no_status_keeps_the_plain_line(self):
        b = agent._signing_chain_block({"name": "Rhonda Thomas",
                                        "decedent_name": "Clifton Lee Ross",
                                        "personal_representative": "Rhonda Thomas"})
        self.assertIn("Personal Rep (per the probate filing): Rhonda Thomas\n", b + "\n")


if __name__ == "__main__":
    unittest.main()
