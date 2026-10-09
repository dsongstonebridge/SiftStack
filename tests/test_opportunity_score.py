"""Opportunity score (2026-10-09). Offline: pure scoring, no DataSift or Assessor calls.

Each case is a real record from the 2026-10-08 probate run, cut to the fields
the score reads.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import opportunity_score as O  # noqa: E402


def rec(sheet, lists=("Free & Clear",), value="250000.00", eq="100.00", df=3, ds=0, deeds=()):
    return {"sheet": sheet, "lists": list(lists), "estimate_value": value,
            "equity_percent": eq, "DF": df, "DS": ds, "deeds": list(deeds)}


class LivingSpouse(unittest.TestCase):
    def test_no_surviving_spouse_is_not_a_spouse(self):
        # 7360 E King Pl read as a living spouse in the first draft.
        self.assertFalse(O._living_spouse({"Marital Status": "No surviving spouse",
                                            "PR Relationship": "Daughter"}))

    def test_surviving_wife_caps_weak(self):
        r = rec({"Marital Status": "Married - surviving wife Michelle A. Ford",
                 "Heir Count": 1, "Property Zip": "74012"}, df=10)
        out = O.score_record(r)
        self.assertIn("living spouse", out["caps"])
        self.assertLessEqual(out["score"], 49)
        self.assertEqual(out["tier"], "Weak")

    def test_two_decedents_is_no_spouse(self):
        self.assertFalse(O._living_spouse({"Marital Status": "Husband and wife",
                                            "Date of Death": "Smiley 09/27/1980; Lillie 08/05/2012"}))

    def test_spouse_pr(self):
        self.assertTrue(O._living_spouse({"Marital Status": "Not stated", "PR Relationship": "Wife"}))


class OwnedSince(unittest.TestCase):
    def test_family_quit_claims_do_not_restart_clock(self):
        deeds = [{"sale_date": "7/9/2021", "grantor": "ALLISON, PHYLLIS M",
                  "grantee": "ALLISON, PHYLLIS M & PATRICK C", "sale_price": 0, "deed_type": "Quit Claim Deed"},
                 {"sale_date": "7/9/2021", "grantor": "ALLISON, RONALD L AND PHYLLIS M",
                  "grantee": "ALLISON, PHYLLIS M", "sale_price": 0, "deed_type": "Affidavit Of Surviving Joint Tenant"}]
        self.assertEqual(O.owned_since(deeds, "ALLISON, PHYLLIS M")[0], None)

    def test_paperwork_by_other_name_skipped_then_purchase(self):
        deeds = [{"sale_date": "11/18/2021", "grantor": "PARKS, JOHNNIE", "grantee": "HOFFMEISTER, JAMES",
                  "sale_price": 0, "deed_type": "Quit Claim Deed"},
                 {"sale_date": "12/1/1992", "grantor": "SKOCDOPOLE FINLEY", "grantee": "HOFFMEISTER JAMES",
                  "sale_price": 15000, "deed_type": "History"}]
        self.assertEqual(O.owned_since(deeds, "HOFFMEISTER, JAMES H")[0], 1992)

    def test_unknown_history_row_is_oldest_date(self):
        deeds = [{"sale_date": "11/27/2018", "grantor": "VANN, LOUIS", "grantee": "VANN, LOUIS AND LA TIA",
                  "sale_price": 0, "deed_type": "Quit Claim Deed"},
                 {"sale_date": "5/23/1978", "grantor": "", "grantee": "", "sale_price": 0,
                  "deed_type": "History - Unknown"}]
        self.assertEqual(O.owned_since(deeds, "VANN, LOUIS")[0], 1978)

    def test_no_parcel_id_is_unknown_not_old(self):
        r = rec({"Owner Alive": "Yes"})
        r["deeds"] = None
        out = O.score_record(r, "foreclosure")
        self.assertEqual(out["parts"]["owned"], 5)
        self.assertEqual(r["owned_label"], "Unknown")
        self.assertIn("no deed history", out["flags"])

    def test_no_deeds_is_before_1980s(self):
        self.assertEqual(O.owned_since([], "PALMER, KENNETH"), (None, "before the 1980s"))


class Scoring(unittest.TestCase):
    def test_one_heir_absentee_top_zip_is_fantastic(self):
        r = rec({"Marital Status": "Martha not married at death", "Heir Count": 1,
                 "Property Zip": "74114", "Date of Death": "Martha 05/04/2019; Laura 03/17/2025"},
                lists=("Free & Clear", "Absentee Owners"), value="247000.00")
        out = O.score_record(r)
        self.assertGreaterEqual(out["score"], 90)
        self.assertEqual(out["tier"], "Fantastic")

    def test_contested_caps_weak(self):
        r = rec({"Marital Status": "Not survived by a spouse", "Heir Count": 2, "Property Zip": "74114",
                 "Testate": "CONTESTED - will dated 08/04/2026"})
        self.assertEqual(O.score_record(r)["tier"], "Weak")

    def test_name_only_penalized(self):
        base = {"Marital Status": "Single", "Heir Count": 2, "Property Zip": "74107"}
        clean = O.score_record(rec(dict(base, **{"Title Holder of Record": "SIMPSON, JOHN E"})))
        named = O.score_record(rec(dict(base, **{"Title Holder of Record":
                                                  "SIMPSON, JOHN E (NAME-ONLY match to the decedent)"})))
        self.assertEqual(clean["score"] - named["score"], O.NAME_ONLY_PENALTY)
        self.assertIn("name-only property match", named["flags"])

    def test_zero_phones_flagged(self):
        out = O.score_record(rec({"Heir Count": 2, "Property Zip": "74106"}, df=0, ds=0))
        self.assertEqual(out["parts"]["reach"], 0)
        self.assertTrue(any("0 good numbers" in f for f in out["flags"]))

    def test_no_property_data_flagged(self):
        out = O.score_record(rec({"Heir Count": 7, "Property Zip": "74136"}, lists=(), value=None, eq=None))
        self.assertIn("no property data in DataSift", out["flags"])
        self.assertIn("equity unknown", out["flags"])

    def test_zip_points(self):
        top = O.score_record(rec({"Heir Count": 1, "Property Zip": "74012"}))["parts"]["zip"]
        last = O.score_record(rec({"Heir Count": 1, "Property Zip": "74063"}))["parts"]["zip"]
        none = O.score_record(rec({"Heir Count": 1, "Property Zip": "74129"}))["parts"]["zip"]
        self.assertEqual((top, last, none), (20, 7, 4))


class Foreclosure(unittest.TestCase):
    def test_debt_over_90pct_caps_weak(self):
        r = rec({"Unpaid Principal Balance": "190000", "Owner Alive": "Yes", "Property Zip": "74012"},
                value="200000.00", df=10)
        out = O.score_record(r, "foreclosure")
        self.assertIn("debt over 90% of value", out["caps"])
        self.assertEqual(out["tier"], "Weak")

    def test_equity_from_petition_balance(self):
        r = rec({"Unpaid Principal Balance": "100000", "Owner Alive": "Yes"}, value="200000.00")
        self.assertEqual(O.score_record(r, "foreclosure")["parts"]["equity"], 7.5)

    def test_deceased_owner_low_situation(self):
        r = rec({"Owner Alive": "No"})
        self.assertEqual(O.score_record(r, "foreclosure")["parts"]["situation"], 5)

    def test_mod_exhausted_bonus(self):
        a = O.score_record(rec({"Owner Alive": "Yes", "Loan Modification Count": 0}), "foreclosure")
        b = O.score_record(rec({"Owner Alive": "Yes", "Loan Modification Count": 3}), "foreclosure")
        self.assertEqual(b["parts"]["situation"] - a["parts"]["situation"], 5)


if __name__ == "__main__":
    unittest.main()
