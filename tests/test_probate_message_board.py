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

    def test_minimal_name_match_is_labeled_possible_not_confirmed(self):
        """A bare first+last match (2 tokens) has nothing on the
        sales-history table to corroborate it against (no address, unlike
        tulsa_treasurer.address_corroborates()) - same common-name collision
        risk as 'Tina Johnson'/'Elizabeth Coleman'. It still flags (never
        silently passes), but as POSSIBLE, not a settled fact."""
        row = {"Personal Representative": "Larry Kaiser"}
        with mock.patch("time.sleep"):
            self.main._check_insider_transfer(row, "R12145940944450",
                                              lambda acct: self._JOHNSON_HISTORY)
        self.assertIn("POSSIBLE insider transfer", row["Insider Transfer"])

    def test_three_token_match_is_labeled_confirmed(self):
        """A match on 3+ tokens (e.g. a middle name lining up too) carries
        more identifying detail than a bare first+last hit, so it's reported
        as a confirmed finding rather than downgraded."""
        row = {"Personal Representative": "Larry Allen Kaiser"}
        history = [{"sale_date": "12/31/2013", "grantor": "KAISER, LARRY ALLEN",
                    "grantee": "L & S GROUP LLC", "sale_price": 0,
                    "deed_type": "Quit Claim Deed", "document_number": "2014002906"}]
        with mock.patch("time.sleep"):
            self.main._check_insider_transfer(row, "R12145940944450", lambda acct: history)
        self.assertIn("INSIDER TRANSFER", row["Insider Transfer"])
        self.assertNotIn("POSSIBLE", row["Insider Transfer"])

    def test_partial_three_token_overlap_no_longer_matches(self):
        """The old '>= 2 of N shared tokens' rule let a 3-token name match on
        only 2 of its 3 tokens against an unrelated person. Full containment
        of the shorter name closes that gap."""
        row = {"Personal Representative": "James Robert Wilson"}
        history = [{"sale_date": "1/1/2020", "grantor": "WILSON, JAMES MICHAEL",
                    "grantee": "SOME THIRD PARTY LLC", "sale_price": 0,
                    "deed_type": "Warranty Deed", "document_number": "X"}]
        with mock.patch("time.sleep"):
            self.main._check_insider_transfer(row, "R00000000000000", lambda acct: history)
        self.assertNotIn("Insider Transfer", row)

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


class TreasurerTrueNegativeTests(unittest.TestCase):
    """Video, 2026-09-15 ("Using Tulsa County Treasurer for Probate
    Properties"): when the Assessor finds nothing for anyone named in the
    filing, the Treasurer's independent owner-name index is a fallback
    corroboration source, not a replacement. A name-only hit is never
    trusted (common-name collision, user-flagged live 2026-09-15) - it needs
    an address match to something already known from the filing, and a hit
    under an heir's own name additionally needs the decedent to show up in
    that parcel's own tax-payer history or it's presumed to be the heir's
    own unrelated property (the Coleman/Lewis case from the same video)."""

    def setUp(self):
        import main
        self.main = main

    def test_uncorroborated_name_hit_is_never_trusted(self):
        # The actual live discrepancy this guards against: a real hit under
        # the decedent's own name, but nothing to say it's the same person.
        row = {"Decedent Name": "Elizabeth Coleman", "PR Address": "999 N Nowhere Ave"}
        hits = [{"tax_data_id": "1", "parcel_id": "82150-84-21-02630",
                 "owner_name": "COLEMAN, ELIZABETH", "tax_type": "Real Estate"}]
        detail = {"owner_street": "1500 S Unrelated St", "property_street": "",
                  "parcel_id": "82150-84-21-02630"}
        with mock.patch("tulsa_treasurer.search_owner_name", return_value=hits), \
             mock.patch("tulsa_treasurer.get_parcel_detail", return_value=detail), \
             mock.patch("time.sleep"):
            self.main._treasurer_true_negative_check(
                row, [("decedent", "Elizabeth Coleman")])
        self.assertNotIn("Treasurer Check", row)

    def test_corroborated_decedent_hit_is_recorded(self):
        row = {"Decedent Name": "Johnnie Fulton Sr.", "PR Address": "1807 N Main"}
        hits = [{"tax_data_id": "1", "parcel_id": "40800-02-13-05520",
                 "owner_name": "FULTON, JOHNNIE SR", "tax_type": "Real Estate"}]
        detail = {"owner_name": "FULTON, JOHNNIE SR",
                  "owner_street": "1807 N Main", "property_street": "4503 N Iroquois Av E",
                  "property_city": "Tulsa",
                  "parcel_id": "40800-02-13-05520", "legal_description": "LT 36 BK 3 SUBURBAN ACRES AMD"}
        history = [{"tax_year": 2025, "owner_name": "FULTON, JOHNNIE SR"}]
        with mock.patch("tulsa_treasurer.search_owner_name", return_value=hits), \
             mock.patch("tulsa_treasurer.get_parcel_detail", return_value=detail), \
             mock.patch("tulsa_treasurer.get_owner_history", return_value=history), \
             mock.patch("time.sleep"):
            self.main._treasurer_true_negative_check(
                row, [("decedent", "Johnnie Fulton Sr.")])
        self.assertIn("Treasurer Check", row)
        self.assertIn("4503 N Iroquois Av E", row["Treasurer Check"])
        self.assertNotIn("CHAIN", row["Treasurer Check"])  # newest payer IS still the decedent
        # The critical part: this discovery must flow into the REAL fields the
        # rest of the pipeline reads, or check_buy_box excludes the row before
        # STOP-AND-ASK ever sees the Treasurer Check note.
        self.assertEqual(row["Property Street"], "4503 N Iroquois Av E")
        self.assertEqual(row["Property City"], "Tulsa")
        self.assertEqual(row["Property State"], "OK")
        self.assertEqual(row["Parcel ID"], "40800-02-13-05520")
        self.assertEqual(row["Title Holder of Record"], "FULTON, JOHNNIE SR")
        self.assertIn("Treasurer fallback", row["Assessor Matched On"])

    def test_never_overwrites_an_already_known_property_street(self):
        row = {"Decedent Name": "Johnnie Fulton Sr.", "PR Address": "1807 N Main",
               "Property Street": "ALREADY SET ELSEWHERE"}
        hits = [{"tax_data_id": "1", "parcel_id": "40800-02-13-05520",
                 "owner_name": "FULTON, JOHNNIE SR", "tax_type": "Real Estate"}]
        detail = {"owner_street": "1807 N Main", "property_street": "4503 N Iroquois Av E",
                  "parcel_id": "40800-02-13-05520"}
        with mock.patch("tulsa_treasurer.search_owner_name", return_value=hits), \
             mock.patch("tulsa_treasurer.get_parcel_detail", return_value=detail), \
             mock.patch("tulsa_treasurer.get_owner_history", return_value=[]), \
             mock.patch("time.sleep"):
            self.main._treasurer_true_negative_check(
                row, [("decedent", "Johnnie Fulton Sr.")])
        self.assertEqual(row["Property Street"], "ALREADY SET ELSEWHERE")

    def test_corroborated_hit_with_no_street_records_note_only(self):
        # An unplatted parcel: corroborated, real, but no street to write -
        # Property Street stays unset (never a guess), Treasurer Check still
        # records the finding as evidence.
        row = {"Decedent Name": "Johnnie Fulton Sr.", "PR Address": "1807 N Main"}
        hits = [{"tax_data_id": "1", "parcel_id": "90328-03-28-15610",
                 "owner_name": "FULTON, JOHNNIE L", "tax_type": "Real Estate"}]
        detail = {"owner_street": "1807 N Main", "property_street": "",
                  "parcel_id": "90328-03-28-15610", "legal_description": "UNPLATTED"}
        with mock.patch("tulsa_treasurer.search_owner_name", return_value=hits), \
             mock.patch("tulsa_treasurer.get_parcel_detail", return_value=detail), \
             mock.patch("tulsa_treasurer.get_owner_history", return_value=[]), \
             mock.patch("time.sleep"):
            self.main._treasurer_true_negative_check(
                row, [("decedent", "Johnnie Fulton Sr.")])
        self.assertIn("Treasurer Check", row)
        self.assertNotIn("Property Street", row)

    def test_heir_hit_without_decedent_in_history_is_rejected(self):
        # The Coleman/Lewis case: the heir has his OWN property, corroborated
        # by his own address, but the decedent never shows up in its history -
        # it's not an inheritance.
        row = {"Decedent Name": "Elizabeth Coleman", "PR Address": "500 Lewis Home St"}
        hits = [{"tax_data_id": "2", "parcel_id": "11111-11-11-11111",
                 "owner_name": "COLEMAN, LEWIS", "tax_type": "Real Estate"}]
        detail = {"owner_street": "500 Lewis Home St", "property_street": "",
                  "parcel_id": "11111-11-11-11111"}
        history = [{"tax_year": 2025, "owner_name": "COLEMAN, LEWIS"},
                   {"tax_year": 2020, "owner_name": "COLEMAN, LEWIS"}]
        with mock.patch("tulsa_treasurer.search_owner_name", return_value=hits), \
             mock.patch("tulsa_treasurer.get_parcel_detail", return_value=detail), \
             mock.patch("tulsa_treasurer.get_owner_history", return_value=history), \
             mock.patch("time.sleep"):
            self.main._treasurer_true_negative_check(
                row, [("heir", "Lewis Coleman")])
        self.assertNotIn("Treasurer Check", row)

    def test_heir_hit_with_decedent_in_history_is_recorded(self):
        row = {"Decedent Name": "Elizabeth Coleman", "PR Address": "500 Family Home St"}
        hits = [{"tax_data_id": "3", "parcel_id": "22222-22-22-22222",
                 "owner_name": "COLEMAN, LEWIS", "tax_type": "Real Estate"}]
        detail = {"owner_street": "500 Family Home St", "property_street": "",
                  "parcel_id": "22222-22-22-22222"}
        history = [{"tax_year": 2025, "owner_name": "COLEMAN, LEWIS"},
                   {"tax_year": 2015, "owner_name": "COLEMAN, ELIZABETH"}]
        with mock.patch("tulsa_treasurer.search_owner_name", return_value=hits), \
             mock.patch("tulsa_treasurer.get_parcel_detail", return_value=detail), \
             mock.patch("tulsa_treasurer.get_owner_history", return_value=history), \
             mock.patch("time.sleep"):
            self.main._treasurer_true_negative_check(
                row, [("heir", "Lewis Coleman")])
        self.assertIn("Treasurer Check", row)

    def test_no_hits_anywhere_leaves_row_untouched(self):
        row = {"Decedent Name": "Nobody Real"}
        with mock.patch("tulsa_treasurer.search_owner_name", return_value=[]), \
             mock.patch("time.sleep"):
            self.main._treasurer_true_negative_check(row, [("decedent", "Nobody Real")])
        self.assertNotIn("Treasurer Check", row)

    def test_blank_candidate_names_are_skipped_without_searching(self):
        row = {}
        with mock.patch("tulsa_treasurer.search_owner_name",
                        side_effect=AssertionError("searched with a blank name")), \
             mock.patch("time.sleep"):
            self.main._treasurer_true_negative_check(row, [("decedent", ""), ("PR", None)])
        self.assertNotIn("Treasurer Check", row)

    def test_heir_pr_search_skipped_when_decedent_already_found_something(self):
        # User, 2026-09-15: "checking the treasurer for the heir/pr is less
        # valuable than checking for the decedent" - heir/PR search must not
        # even run once the decedent search already found a corroborated hit.
        row = {"Decedent Name": "Johnnie Fulton Sr.", "PR Address": "1807 N Main"}
        decedent_hit = [{"tax_data_id": "1", "parcel_id": "40800-02-13-05520",
                         "owner_name": "FULTON, JOHNNIE SR", "tax_type": "Real Estate"}]
        detail = {"owner_name": "FULTON, JOHNNIE SR", "owner_street": "1807 N Main",
                  "property_street": "4503 N Iroquois Av E", "parcel_id": "40800-02-13-05520"}

        def fake_search(last, first):
            if last.upper() == "FULTON":
                return decedent_hit
            raise AssertionError(f"heir/PR search should not have run: {last} {first}")

        with mock.patch("tulsa_treasurer.search_owner_name", side_effect=fake_search), \
             mock.patch("tulsa_treasurer.get_parcel_detail", return_value=detail), \
             mock.patch("tulsa_treasurer.get_owner_history",
                        return_value=[{"tax_year": 2025, "owner_name": "FULTON, JOHNNIE SR"}]), \
             mock.patch("time.sleep"):
            self.main._treasurer_true_negative_check(
                row, [("decedent", "Johnnie Fulton Sr."), ("PR", "Jennifer Faulk")])
        self.assertIn("Treasurer Check", row)

    def test_heir_pr_search_runs_when_decedent_search_is_dry(self):
        row = {"Decedent Name": "Johnnie Fulton Sr.", "PR Address": "500 Faulk Home St"}
        heir_hit = [{"tax_data_id": "9", "parcel_id": "99999-99-99-99999",
                     "owner_name": "FAULK, JENNIFER", "tax_type": "Real Estate"}]
        detail = {"owner_street": "500 Faulk Home St", "property_street": "500 Faulk Home St",
                  "parcel_id": "99999-99-99-99999"}
        history = [{"tax_year": 2025, "owner_name": "FAULK, JENNIFER"},
                   {"tax_year": 2010, "owner_name": "FULTON, JOHNNIE SR"}]

        def fake_search(last, first):
            return [] if last.upper() == "FULTON" else heir_hit

        with mock.patch("tulsa_treasurer.search_owner_name", side_effect=fake_search), \
             mock.patch("tulsa_treasurer.get_parcel_detail", return_value=detail), \
             mock.patch("tulsa_treasurer.get_owner_history", return_value=history), \
             mock.patch("time.sleep"):
            self.main._treasurer_true_negative_check(
                row, [("decedent", "Johnnie Fulton Sr."), ("PR", "Jennifer Faulk")])
        self.assertIn("Treasurer Check", row)
        self.assertIn("Jennifer Faulk", row["Treasurer Check"])

    def test_chain_note_when_property_has_since_changed_hands(self):
        row = {"Decedent Name": "Johnnie Fulton Sr.", "PR Address": "1807 N Main"}
        hits = [{"tax_data_id": "1", "parcel_id": "40800-02-13-05520",
                 "owner_name": "FULTON, JOHNNIE SR", "tax_type": "Real Estate"}]
        detail = {"owner_name": "FULTON, JOHNNIE SR", "owner_street": "1807 N Main",
                  "property_street": "4503 N Iroquois Av E", "parcel_id": "40800-02-13-05520"}
        history = [{"tax_year": 2025, "owner_name": "NEW BUYER LLC"},
                   {"tax_year": 2015, "owner_name": "FULTON, JOHNNIE SR"}]
        with mock.patch("tulsa_treasurer.search_owner_name", return_value=hits), \
             mock.patch("tulsa_treasurer.get_parcel_detail", return_value=detail), \
             mock.patch("tulsa_treasurer.get_owner_history", return_value=history), \
             mock.patch("time.sleep"):
            self.main._treasurer_true_negative_check(
                row, [("decedent", "Johnnie Fulton Sr.")])
        self.assertIn("CHAIN", row["Treasurer Check"])
        self.assertIn("NEW BUYER LLC", row["Treasurer Check"])

    def test_zero_improvements_flags_for_manual_check_never_excludes(self):
        # Live-tested 2026-09-15: Improvements == $0 showed on the KNOWN real
        # Fulton house too - never a reliable vacant-lot signal by itself.
        row = {"Decedent Name": "Johnnie Fulton Sr.", "PR Address": "1807 N Main"}
        hits = [{"tax_data_id": "1", "parcel_id": "40800-02-13-05520",
                 "owner_name": "FULTON, JOHNNIE SR", "tax_type": "Real Estate"}]
        detail = {"owner_name": "FULTON, JOHNNIE SR", "owner_street": "1807 N Main",
                  "property_street": "4503 N Iroquois Av E", "parcel_id": "40800-02-13-05520",
                  "improvements_value": 0.0}
        with mock.patch("tulsa_treasurer.search_owner_name", return_value=hits), \
             mock.patch("tulsa_treasurer.get_parcel_detail", return_value=detail), \
             mock.patch("tulsa_treasurer.get_owner_history",
                        return_value=[{"tax_year": 2025, "owner_name": "FULTON, JOHNNIE SR"}]), \
             mock.patch("time.sleep"):
            self.main._treasurer_true_negative_check(
                row, [("decedent", "Johnnie Fulton Sr.")])
        self.assertIn("CHECK IMPROVEMENTS", row["Treasurer Check"])
        self.assertIn("Zillow", row["Treasurer Check"])
        self.assertNotIn("Vacant Lot", row)   # never auto-excluded on this signal
        self.assertEqual(row["Property Street"], "4503 N Iroquois Av E")  # still created

    def test_nonzero_improvements_has_no_check_flag(self):
        row = {"Decedent Name": "Johnnie Fulton Sr.", "PR Address": "1807 N Main"}
        hits = [{"tax_data_id": "1", "parcel_id": "40800-02-13-05520",
                 "owner_name": "FULTON, JOHNNIE SR", "tax_type": "Real Estate"}]
        detail = {"owner_name": "FULTON, JOHNNIE SR", "owner_street": "1807 N Main",
                  "property_street": "4503 N Iroquois Av E", "parcel_id": "40800-02-13-05520",
                  "improvements_value": 45000.0}
        with mock.patch("tulsa_treasurer.search_owner_name", return_value=hits), \
             mock.patch("tulsa_treasurer.get_parcel_detail", return_value=detail), \
             mock.patch("tulsa_treasurer.get_owner_history",
                        return_value=[{"tax_year": 2025, "owner_name": "FULTON, JOHNNIE SR"}]), \
             mock.patch("time.sleep"):
            self.main._treasurer_true_negative_check(
                row, [("decedent", "Johnnie Fulton Sr.")])
        self.assertNotIn("CHECK IMPROVEMENTS", row["Treasurer Check"])

    def test_newest_year_kept_per_parcel_not_oldest(self):
        # The real bug found live 2026-09-15: the Fulton reference parcel
        # showed Improvements=$0 for 2019-2022 and a real nonzero figure from
        # 2023 on. The site returns oldest-year-first, so picking the FIRST
        # occurrence of a parcel (not the newest) reads stale data and would
        # wrongly flag a known real house.
        row = {"Decedent Name": "Johnnie Fulton Sr.", "PR Address": "1807 N Main"}
        hits = [
            {"tax_data_id": "old", "parcel_id": "40800-02-13-05520", "tax_year": 2019,
             "owner_name": "FULTON, JOHNNIE SR", "tax_type": "Real Estate"},
            {"tax_data_id": "new", "parcel_id": "40800-02-13-05520", "tax_year": 2025,
             "owner_name": "FULTON, JOHNNIE SR", "tax_type": "Real Estate"},
        ]
        details = {
            "old": {"owner_name": "FULTON, JOHNNIE SR", "owner_street": "1807 N Main",
                   "property_street": "4503 N Iroquois Av E", "parcel_id": "40800-02-13-05520",
                   "improvements_value": 0.0},
            "new": {"owner_name": "FULTON, JOHNNIE SR", "owner_street": "1807 N Main",
                   "property_street": "4503 N Iroquois Av E", "parcel_id": "40800-02-13-05520",
                   "improvements_value": 3664.0},
        }
        with mock.patch("tulsa_treasurer.search_owner_name", return_value=hits), \
             mock.patch("tulsa_treasurer.get_parcel_detail",
                        side_effect=lambda tax_data_id, **kw: details[tax_data_id]), \
             mock.patch("tulsa_treasurer.get_owner_history",
                        return_value=[{"tax_year": 2025, "owner_name": "FULTON, JOHNNIE SR"}]), \
             mock.patch("time.sleep"):
            self.main._treasurer_true_negative_check(
                row, [("decedent", "Johnnie Fulton Sr.")])
        self.assertNotIn("CHECK IMPROVEMENTS", row["Treasurer Check"])

    def test_improvements_note_names_the_specific_property_not_the_candidate(self):
        # A reviewer seeing "decedent: X" for a flag is useless when X shows
        # up on several parcels - the note must name the actual property.
        row = {"Decedent Name": "Johnnie Fulton Sr.", "PR Address": "1807 N Main"}
        hits = [
            {"tax_data_id": "a", "parcel_id": "40800-02-13-05520", "tax_year": 2025,
             "owner_name": "FULTON, JOHNNIE SR", "tax_type": "Real Estate"},
            {"tax_data_id": "b", "parcel_id": "90328-03-28-15610", "tax_year": 2025,
             "owner_name": "FULTON, JOHNNIE SR", "tax_type": "Real Estate"},
        ]
        details = {
            "a": {"owner_name": "FULTON, JOHNNIE SR", "owner_street": "1807 N Main",
                 "property_street": "4503 N Iroquois Av E", "parcel_id": "40800-02-13-05520",
                 "improvements_value": 3664.0},
            "b": {"owner_name": "FULTON, JOHNNIE L", "owner_street": "1807 N Main",
                 "property_street": "", "parcel_id": "90328-03-28-15610",
                 "improvements_value": 0.0},
        }
        with mock.patch("tulsa_treasurer.search_owner_name", return_value=hits), \
             mock.patch("tulsa_treasurer.get_parcel_detail",
                        side_effect=lambda tax_data_id, **kw: details[tax_data_id]), \
             mock.patch("tulsa_treasurer.get_owner_history",
                        return_value=[{"tax_year": 2025, "owner_name": "FULTON, JOHNNIE SR"}]), \
             mock.patch("time.sleep"):
            self.main._treasurer_true_negative_check(
                row, [("decedent", "Johnnie Fulton Sr.")])
        self.assertIn("90328-03-28-15610", row["Treasurer Check"])   # the flagged one, named
        self.assertNotIn("4503 N Iroquois Av E", row["Treasurer Check"].split("CHECK IMPROVEMENTS")[1])


if __name__ == "__main__":
    unittest.main()
