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

    def test_insider_transfer_is_shown_as_a_caution(self):
        b = agent._signing_chain_block(_subj(
            title_holder="Larry Kaiser",
            insider_transfer=("12/31/2013: KAISER, LARRY A AND SHELLEY A -> "
                              "L & S GROUP LLC (Quit Claim Deed)")))
        self.assertIn("CAUTION - transfer connected to this estate: "
                      "12/31/2013: KAISER, LARRY A AND SHELLEY A -> "
                      "L & S GROUP LLC (Quit Claim Deed)", b)

    def test_no_insider_transfer_prints_no_caution_line(self):
        self.assertNotIn("CAUTION", agent._signing_chain_block(_subj()))


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
             mock.patch("tulsa_assessor.get_parcel_sales_history", return_value=[]), \
             mock.patch("time.sleep"):
            self.main._enrich_probate_rows([row])
        gs.assert_called_once_with("R12145940944450")
        self.assertEqual(row["Title Holder of Record"], "L & S GROUP LLC")

    def test_non_assessor_parcel_format_is_not_looked_up(self):
        row = {"Property Street": "1916 S 140th East Ave", "Vacant Lot": "No",
               "Parcel ID": "12145-94-09-44450"}
        with mock.patch("tulsa_assessor.get_parcel_situs",
                        side_effect=AssertionError("looked up")), \
             mock.patch("tulsa_assessor.get_parcel_sales_history",
                        side_effect=AssertionError("sales history looked up")), \
             mock.patch("time.sleep"):
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
             mock.patch("tulsa_assessor.get_parcel_sales_history", return_value=[]), \
             mock.patch("time.sleep"):
            self.main._enrich_probate_rows([row])
        self.assertEqual(row["Title Holder of Record"], "ROSS, CLIFTON LEE")
        self.assertEqual(row["Property Street"], "4529 E XYLER ST N")

    def test_title_holder_reaches_the_trace(self):
        row = self.main._trace_row({"Property Street": "1916 S 140th East Ave",
                                    "Title Holder of Record": "L & S GROUP LLC"})
        self.assertEqual(row["Title Holder of Record"], "L & S GROUP LLC")


class InsiderTransferTests(unittest.TestCase):
    """batch_review's 'clean title holder' check can't see WHY the current
    holder has title - only that a named party (PR/heir) does. Video,
    2026-09-14: Larry Kaiser (Johnson's petitioner) quit-claimed the property
    to L&S Group LLC for $0 in 2013, years before the estate existed. That
    transfer is the real messy signal, whether or not the eventual holder is
    tied to anyone in the filing."""

    @classmethod
    def setUpClass(cls):
        import main
        cls.main = main

    #: The Johnson parcel's real sales history (fetched live 2026-09-14).
    _JOHNSON_HISTORY = [
        {"sale_date": "12/31/2013", "grantor": "KAISER, LARRY A AND SHELLEY A",
         "grantee": "L & S GROUP LLC", "sale_price": 0, "deed_type": "Quit Claim Deed",
         "document_number": "2014002906"},
        {"sale_date": "8/1/2002", "grantor": "VA", "grantee": "KAISER LARRY A & SHELLEY A",
         "sale_price": 0, "deed_type": "History", "document_number": "2002110783"},
    ]

    def test_flags_when_grantor_matches_the_pr(self):
        row = {"Case Number": "PB-2026-0761", "Personal Representative": "Larry Kaiser"}
        with mock.patch("time.sleep"):
            self.main._check_insider_transfer(row, "R12145940944450",
                                              lambda acct: self._JOHNSON_HISTORY)
        self.assertIn("Insider Transfer", row)
        self.assertIn("KAISER, LARRY A AND SHELLEY A", row["Insider Transfer"])
        self.assertIn("L & S GROUP LLC", row["Insider Transfer"])

    def test_flags_when_grantor_matches_an_heir(self):
        row = {"Heirs": "Wendy Johnson (Daughter); Debbie Lewis (Daughter)"}
        history = [{"sale_date": "1/1/2020", "grantor": "Wendy Johnson",
                    "grantee": "SOME THIRD PARTY LLC", "sale_price": 0,
                    "deed_type": "Quit Claim Deed", "document_number": "X"}]
        with mock.patch("time.sleep"):
            self.main._check_insider_transfer(row, "R00000000000000", lambda acct: history)
        self.assertIn("Insider Transfer", row)

    def test_no_flag_when_grantor_is_unrelated(self):
        row = {"Decedent Name": "Clifton Lee Ross", "Personal Representative": "Rhonda Thomas"}
        history = [{"sale_date": "11/1/1997", "grantor": "ENGMAN MARTIN F III TRUSTEE",
                    "grantee": "MATTHEWS MICHAEL L", "sale_price": 69000,
                    "deed_type": "History", "document_number": "1997111131"}]
        with mock.patch("time.sleep"):
            self.main._check_insider_transfer(row, "R30175032811080", lambda acct: history)
        self.assertNotIn("Insider Transfer", row)

    def test_no_flag_on_empty_history(self):
        row = {"Decedent Name": "Clifton Lee Ross"}
        with mock.patch("time.sleep"):
            self.main._check_insider_transfer(row, "R30175032811080", lambda acct: [])
        self.assertNotIn("Insider Transfer", row)

    def test_no_lookup_without_an_account_number(self):
        row = {}
        self.main._check_insider_transfer(
            row, "", lambda acct: (_ for _ in ()).throw(AssertionError("looked up with no acct")))
        self.assertNotIn("Insider Transfer", row)


class LivingSpouseAddressTests(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main

    def test_pr_relationship_spouse_triggers_it(self):
        row = {"PR Relationship": "Wife", "PR Address": "7508 South Granite"}
        self.assertEqual(self.main._living_spouse_address(row), "7508 South Granite")

    def test_marital_status_surviving_spouse_triggers_it(self):
        row = {"Marital Status": "survived by his spouse", "Mailing Street": "123 Main St"}
        self.assertEqual(self.main._living_spouse_address(row), "123 Main St")

    def test_no_spouse_signal_returns_empty(self):
        row = {"Marital Status": "was not married at the time of his death",
               "Mailing Street": "123 Main St"}
        self.assertEqual(self.main._living_spouse_address(row), "")

    def test_spouse_address_searched_before_the_name_loop(self):
        # Bitson-shaped: probate states personal property only, but the
        # living spouse (PR) holds title - never in the decedent's own name.
        row = {"Case Number": "PB-2026-9999", "Decedent Name": "Pamela Irene Finley Bitson",
               "Personal Representative": "D'Angelo Bitson Sr.", "PR Relationship": "Husband",
               "PR Address": "1234 N Somewhere Ave"}
        hit = {"AccountNo": "R99999999999999", "FullPrimaryOwnerName": "BITSON, D ANGELO",
               "FullPropertyStreet": "1234 N SOMEWHERE AVE", "PropertyCity": "TULSA",
               "PropertyZipCode": "74106", "AcctType": "Residential"}
        with mock.patch("tulsa_assessor.search_assessor") as sa, \
             mock.patch("tulsa_assessor.get_parcel_improvements",
                        return_value={"is_vacant_lot": False, "land_value": 5000}), \
             mock.patch("tulsa_assessor.get_parcel_sales_history", return_value=[]), \
             mock.patch("time.sleep"):
            sa.return_value = [hit]
            self.main._enrich_probate_rows([row])
        sa.assert_called_once_with("1234 N Somewhere Ave")
        self.assertEqual(row["Property Street"], "1234 N SOMEWHERE AVE")
        self.assertEqual(row["Title Holder of Record"], "BITSON, D ANGELO")


class TrustNameSearchTests(unittest.TestCase):
    """Video, 2026-09-15 (Scott/Coleman): a will naming a trust means the
    decedent's own name will never match a trust-titled parcel - search the
    trust FIRST. Assessor first; tulsa_loccat's document search (a different
    index) as fallback when the Assessor misses."""

    def setUp(self):
        import main
        self.main = main

    def test_no_trust_name_is_a_pure_noop(self):
        row = {"Decedent Name": "Clifton Lee Ross"}
        with mock.patch("tulsa_assessor.search_assessor",
                        side_effect=AssertionError("searched with no trust name")):
            result = self.main._trust_name_search(row, __import__("tulsa_assessor").search_assessor,
                                                   None, None, None)
        self.assertFalse(result)

    def test_trust_found_via_assessor_wins_immediately(self):
        row = {"Decedent Name": "Elizabeth S. Coleman",
               "Trust Name": "Elizabeth S. Coleman Revocable Trust"}
        hit = {"AccountNo": "R11111111111111", "FullPrimaryOwnerName": "COLEMAN, ELIZABETH S REV TRUST",
               "FullPropertyStreet": "123 E MAIN ST", "PropertyCity": "TULSA",
               "PropertyZipCode": "74103", "AcctType": "Residential"}
        with mock.patch("tulsa_assessor.search_assessor", return_value=[hit]) as sa, \
             mock.patch("tulsa_assessor.get_parcel_improvements",
                        return_value={"is_vacant_lot": False, "land_value": 8000}), \
             mock.patch("tulsa_assessor.get_parcel_sales_history", return_value=[]), \
             mock.patch("time.sleep"):
            result = self.main._trust_name_search(
                row, __import__("tulsa_assessor").search_assessor,
                __import__("tulsa_assessor").get_parcel_situs,
                __import__("tulsa_assessor").get_parcel_improvements,
                __import__("tulsa_assessor").get_parcel_sales_history)
        self.assertTrue(result)
        sa.assert_called_once_with("Elizabeth S. Coleman Revocable Trust")
        self.assertEqual(row["Property Street"], "123 E MAIN ST")
        self.assertEqual(row["Title Holder of Record"], "COLEMAN, ELIZABETH S REV TRUST")

    def test_dtd_suffix_variant_tried_when_full_name_misses(self):
        row = {"Trust Name": "John Smith Living Trust DTD 3/12/2010"}
        calls = []
        def fake_search(term):
            calls.append(term)
            if term == "John Smith Living Trust":
                return [{"AccountNo": "R22222222222222", "FullPrimaryOwnerName": "SMITH TRUST",
                         "FullPropertyStreet": "456 S ELM AVE", "PropertyCity": "TULSA",
                         "PropertyZipCode": "74105", "AcctType": "Residential"}]
            return []
        with mock.patch("tulsa_assessor.get_parcel_improvements", return_value=None), \
             mock.patch("tulsa_assessor.get_parcel_sales_history", return_value=[]), \
             mock.patch("time.sleep"):
            result = self.main._trust_name_search(
                row, fake_search, __import__("tulsa_assessor").get_parcel_situs,
                __import__("tulsa_assessor").get_parcel_improvements,
                __import__("tulsa_assessor").get_parcel_sales_history)
        self.assertTrue(result)
        self.assertIn("John Smith Living Trust DTD 3/12/2010", calls)
        self.assertIn("John Smith Living Trust", calls)
        self.assertEqual(row["Property Street"], "456 S ELM AVE")

    def test_falls_back_to_loccat_when_assessor_misses(self):
        row = {"Trust Name": "L & S Group LLC"}
        loccat_hit = {"properties": {"PARCELNB": "12145940944450", "GRANTOR": "KAISER LARRY A",
                                     "GRANTEE": "L&S GROUP"}, "verified": True}
        with mock.patch("tulsa_assessor.search_assessor", return_value=[]), \
             mock.patch("tulsa_loccat.search_advanced", return_value=[loccat_hit]), \
             mock.patch("tulsa_assessor.get_parcel_situs",
                        return_value={"street": "1916 S 140TH EAST AVE", "city": "TULSA",
                                     "zip": "74108", "owner": "L & S GROUP LLC"}) as gs, \
             mock.patch("tulsa_assessor.get_parcel_improvements", return_value=None), \
             mock.patch("tulsa_assessor.get_parcel_sales_history", return_value=[]), \
             mock.patch("time.sleep"):
            result = self.main._trust_name_search(
                row, __import__("tulsa_assessor").search_assessor,
                __import__("tulsa_assessor").get_parcel_situs,
                __import__("tulsa_assessor").get_parcel_improvements,
                __import__("tulsa_assessor").get_parcel_sales_history)
        self.assertTrue(result)
        gs.assert_called_once_with("R12145940944450")
        self.assertEqual(row["Property Street"], "1916 S 140TH EAST AVE")
        self.assertEqual(row["Title Holder of Record"], "L & S GROUP LLC")

    def test_unverified_loccat_hits_are_never_used(self):
        row = {"Trust Name": "L & S Group LLC"}
        unverified_hit = {"properties": {"PARCELNB": "99999999999999"}, "verified": False}
        with mock.patch("tulsa_assessor.search_assessor", return_value=[]), \
             mock.patch("tulsa_loccat.search_advanced", return_value=[unverified_hit]), \
             mock.patch("tulsa_assessor.get_parcel_situs",
                        side_effect=AssertionError("looked up an unverified hit")), \
             mock.patch("time.sleep"):
            result = self.main._trust_name_search(
                row, __import__("tulsa_assessor").search_assessor,
                __import__("tulsa_assessor").get_parcel_situs, None, None)
        self.assertFalse(result)

    def test_enrich_tries_trust_before_spouse_address(self):
        # A row that has BOTH a trust name AND a living-spouse signal - the
        # trust must be searched first (video: trust presence is the
        # stronger, more specific signal when stated).
        row = {"Decedent Name": "Elizabeth S. Coleman",
               "Trust Name": "Elizabeth S. Coleman Revocable Trust",
               "PR Relationship": "Spouse", "PR Address": "999 Should Not Be Searched Ave"}
        hit = {"AccountNo": "R33333333333333", "FullPrimaryOwnerName": "COLEMAN TRUST",
               "FullPropertyStreet": "789 W OAK ST", "PropertyCity": "TULSA",
               "PropertyZipCode": "74107", "AcctType": "Residential"}
        with mock.patch("tulsa_assessor.search_assessor") as sa, \
             mock.patch("tulsa_assessor.get_parcel_situs",
                        side_effect=AssertionError("known-address branch should not run")), \
             mock.patch("tulsa_assessor.get_parcel_improvements", return_value=None), \
             mock.patch("tulsa_assessor.get_parcel_sales_history", return_value=[]), \
             mock.patch("time.sleep"):
            sa.return_value = [hit]
            self.main._enrich_probate_rows([row])
        sa.assert_called_once_with("Elizabeth S. Coleman Revocable Trust")
        self.assertEqual(row["Property Street"], "789 W OAK ST")


if __name__ == "__main__":
    unittest.main()
