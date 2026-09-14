"""Offline tests: the probate Message Board names the PR per the filing and
who holds title (the county assessor's owner of record).

User, 2026-09-11: the board must show "who the PR is according to the probate
as well as the legal owner of record" - meaning who holds title. On Johnson
those were different parties from each other AND from the person we called.

Nothing here touches the network.
Run:  python tests/test_probate_message_board.py -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import skip_trace_agent as agent  # noqa: E402
from datasift_formatter import _format_petition_notes  # noqa: E402


def _subj(**kw):
    base = {"name": "Wendy Johnson", "decedent_name": "Tina Fay Johnson",
            "personal_representative": "Larry Kaiser", "date_of_death": "02/22/2025",
            "trace_street": "1916 S 140th East Ave", "trace_city": "Tulsa",
            "trace_state": "OK", "property_address": "1916 S 140Th East Ave"}
    base.update(kw)
    return base


class SigningChainTests(unittest.TestCase):
    def test_pr_is_labelled_as_per_the_filing(self):
        self.assertIn("Personal Rep (per the probate filing): Larry Kaiser",
                      agent._signing_chain_block(_subj()))

    def test_title_holder_that_is_not_the_decedent_is_flagged(self):
        b = agent._signing_chain_block(_subj(title_holder="L & S GROUP LLC"))
        self.assertIn("Title holder of record (county assessor): L & S GROUP LLC "
                      "- NOT the decedent", b)

    def test_title_still_in_the_decedents_name(self):
        b = agent._signing_chain_block(_subj(decedent_name="Clifton Lee Ross",
                                             personal_representative="Rhonda Thomas",
                                             title_holder="ROSS, CLIFTON LEE"))
        self.assertIn("ROSS, CLIFTON LEE - the decedent; title has not passed yet", b)

    def test_chain_of_deaths_root_owner_is_flagged(self):
        b = agent._signing_chain_block(_subj(decedent_name="Alfred Fulton",
                                             title_holder="FULTON, JOHNNIE SR"))
        self.assertIn("NOT the decedent", b)

    def test_decedents_own_trust_is_not_flagged(self):
        b = agent._signing_chain_block(_subj(
            decedent_name="Robert Clarence Lovelace",
            title_holder="LOVELACE, ROBERT C C/O LOVELACE, ROBERT C REV LIVING TRUST"))
        self.assertIn("the decedent; title has not passed yet", b)
        self.assertNotIn("NOT the decedent", b)

    def test_unknown_title_holder_prints_no_line(self):
        self.assertNotIn("Title holder", agent._signing_chain_block(_subj()))

    def test_pr_not_yet_named(self):
        self.assertIn("Personal Rep (per the probate filing): not named in the filing",
                      agent._signing_chain_block(_subj(personal_representative="")))

    def test_foreclosure_record_gets_no_block(self):
        self.assertEqual(agent._signing_chain_block({"name": "X", "title_holder": "Y"}), "")


class CreationNotesTests(unittest.TestCase):
    def test_notes_carry_pr_and_title_holder(self):
        notes = _format_petition_notes({
            "Case Number": "PB-2026-0761", "Personal Representative": "Larry Kaiser",
            "Real Property Stated": "QUALIFIED - lease to own",
            "Title Holder of Record": "L & S GROUP LLC"}, notice_type="probate")
        self.assertIn("Personal Representative: Larry Kaiser", notes)
        self.assertIn("Title Holder of Record: L & S GROUP LLC", notes)


class ProbateContextTests(unittest.TestCase):
    def test_row_title_holder_reaches_the_subject(self):
        ctx = agent._probate_context({}, {"Decedent Name": "Tina Fay Johnson",
                                          "Title Holder of Record": "L & S GROUP LLC"})
        self.assertEqual(ctx["title_holder"], "L & S GROUP LLC")


class EnrichTitleHolderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import main
        cls.main = main

    def test_row_with_known_address_gets_title_holder_by_parcel(self):
        row = {"Case Number": "PB-2026-0761", "Property Street": "1916 S 140th East Ave",
               "Vacant Lot": "No", "Parcel ID": "R12145940944450"}
        with mock.patch("tulsa_assessor.get_parcel_situs",
                        return_value={"owner": "L & S GROUP LLC"}) as gs, \
             mock.patch("tulsa_assessor.search_assessor",
                        side_effect=AssertionError("no name search for a known address")), \
             mock.patch("time.sleep"):
            self.main._enrich_probate_rows([row])
        gs.assert_called_once_with("R12145940944450")
        self.assertEqual(row["Title Holder of Record"], "L & S GROUP LLC")

    def test_non_assessor_parcel_format_is_not_looked_up(self):
        row = {"Property Street": "1916 S 140th East Ave", "Vacant Lot": "No",
               "Parcel ID": "12145-94-09-44450"}
        with mock.patch("tulsa_assessor.get_parcel_situs",
                        side_effect=AssertionError("looked up")), mock.patch("time.sleep"):
            self.main._enrich_probate_rows([row])
        self.assertNotIn("Title Holder of Record", row)

    def test_searched_row_records_the_matched_owner(self):
        row = {"Case Number": "PB-2026-0760", "Decedent Name": "Clifton Lee Ross",
               "Personal Representative": "Rhonda Thomas"}
        hit = {"AccountNo": "R30175032811080", "FullPrimaryOwnerName": "ROSS, CLIFTON LEE",
               "FullPropertyStreet": "4529 E XYLER ST N", "PropertyCity": "TULSA",
               "PropertyZipCode": "74115", "AcctType": "Residential"}
        with mock.patch("tulsa_assessor.search_assessor", return_value=[hit]), \
             mock.patch("tulsa_assessor.get_parcel_improvements",
                        return_value={"is_vacant_lot": False, "land_value": 10100}), \
             mock.patch("tulsa_assessor.get_parcel_situs",
                        side_effect=AssertionError("owner came with the search hit")), \
             mock.patch("time.sleep"):
            self.main._enrich_probate_rows([row])
        self.assertEqual(row["Title Holder of Record"], "ROSS, CLIFTON LEE")
        self.assertEqual(row["Property Street"], "4529 E XYLER ST N")

    def test_title_holder_reaches_the_trace(self):
        row = self.main._trace_row({"Property Street": "1916 S 140th East Ave",
                                    "Title Holder of Record": "L & S GROUP LLC"})
        self.assertEqual(row["Title Holder of Record"], "L & S GROUP LLC")


if __name__ == "__main__":
    unittest.main()
