"""Fixes from the 2026-10-08 probate run (53 PDFs, 33 records). Offline: every
DataSift call is stubbed, nothing is billed or written.

1. Buy box: "Not stated..." was rejected as if the filing denied real property.
2. Address match: "449 S 112th East Ave" vs stored "449 S 112th Ave E".
3. Phone tags: a second, slower re-send when the first one does not land.
4. batch_ocr reads PB case numbers.
5. A "Spouse" DM Relationship gets Wife/Husband from the marital wording.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import batch_ocr  # noqa: E402
import buy_box  # noqa: E402
import datasift_api as D  # noqa: E402
import skip_trace_agent as sta  # noqa: E402


def _row(rp):
    return {"Property Street": "123 Main St", "Real Property Stated": rp}


class BuyBoxRealPropertyWording(unittest.TestCase):
    def rejected(self, rp):
        ok, reasons = buy_box.check_buy_box(_row(rp))
        return any("NO real property" in r for r in reasons)

    def test_not_stated_passes(self):
        self.assertFalse(self.rejected("Not stated in the petition; county title shows the house"))

    def test_silent_passes(self):
        self.assertFalse(self.rejected("Petition is silent on real property"))

    def test_carter_two_estates_passes(self):
        self.assertFalse(self.rejected(
            "Martha's estate: REAL PROPERTY less than $200,000, personal property $0. "
            "Laura's estate: real property $0, personal property less than $1,000"))

    def test_real_and_personal_passes(self):
        self.assertFalse(self.rejected("left an estate consisting of real and personal property"))

    def test_real_negatives_rejected(self):
        for rp in ("No real property", "none", "None.", "no interest in real estate",
                   "personal property only", "leaving personal property",
                   "Estate is checking account and pension (personal property)"):
            self.assertTrue(self.rejected(rp), rp)


class TulsaAvenueOrder(unittest.TestCase):
    def test_both_orders_same_key(self):
        pairs = [("449 S 112th East Ave", "449 S 112th Ave E"),
                 ("109 S 169th Ave E", "109 S 169th East Ave"),
                 ("3316 S 144th Ave E", "3316 S 144th East Ave"),
                 ("11300 N 118th E Ave", "11300 N 118Th East Ave")]
        for a, b in pairs:
            self.assertEqual(D.address_key(a, "Tulsa"), D.address_key(b, "Tulsa"), (a, b))

    def test_named_street_untouched(self):
        # Directional before the type is only reordered after a NUMBERED name.
        self.assertEqual(D._norm_street("100 N Main E St"), "100 N MAIN E ST")
        self.assertNotEqual(D.address_key("1752 E 56th St S", "Tulsa"),
                            D.address_key("1752 E 56th St N", "Tulsa"))

    def test_find_property_matches_swapped_order(self):
        stored = [{"uuid": "u1", "address": {"street": "449 S 112th Ave E", "city": "Tulsa"}}]
        with mock.patch.object(D, "search_by_address", return_value=stored), \
             mock.patch.object(D, "_pick_best", side_effect=lambda r: r[0]):
            hit = D.find_property_by_address("449 S 112th East Ave", "Tulsa", "OK")
        self.assertEqual(hit["uuid"], "u1")


class SecondTagResend(unittest.TestCase):
    def _bare(self):
        return {"ok": False, "missing": {"9185551234": ["Dial First"]},
                "on_record": {"9185551234": []}}

    def test_second_resend_lands(self):
        good = {"ok": True, "missing": {}, "on_record": {"9185551234": ["Dial First"]}}
        reads = [self._bare(), self._bare(), self._bare(), good]
        with mock.patch.object(D, "_DRY_RUN", False), \
             mock.patch.object(D, "set_phone_tags") as send, \
             mock.patch.object(D, "verify_phone_tags", side_effect=reads), \
             mock.patch.object(D.time, "sleep") as slp:
            out = D.apply_phone_tags_verified("o1", {"9185551234": ["Dial First"]})
        self.assertTrue(out["ok"])
        self.assertEqual(send.call_count, 3)  # first send + two re-sends
        self.assertIn(40.0, [c.args[0] for c in slp.call_args_list])

    def test_partial_never_resent(self):
        partial = {"ok": False, "missing": {"9185551234": ["Tracerfy"]},
                   "on_record": {"9185551234": ["Dial First"]}}
        with mock.patch.object(D, "_DRY_RUN", False), \
             mock.patch.object(D, "set_phone_tags") as send, \
             mock.patch.object(D, "verify_phone_tags", return_value=partial), \
             mock.patch.object(D.time, "sleep"):
            D.apply_phone_tags_verified("o1", {"9185551234": ["Dial First", "Tracerfy"]})
        self.assertEqual(send.call_count, 1)


class ProbateCaseNumbers(unittest.TestCase):
    def test_pb_read(self):
        self.assertEqual(batch_ocr.find_case("CaseNo. PB- 2026-872"), "PB-2026-872")
        self.assertEqual(batch_ocr.find_case("Case Number PB-2026-0870"), "PB-2026-870")
        self.assertEqual(batch_ocr.find_case("P8-2026-871"), "PB-2026-871")

    def test_cj_unchanged(self):
        self.assertEqual(batch_ocr.find_case("CJ-2026-04287"), "CJ-2026-4287")
        self.assertEqual(batch_ocr.find_case("GJ - 2026 - 4287"), "CJ-2026-4287")


class SpouseTag(unittest.TestCase):
    def subj(self, rel, marital=""):
        return {"name": "Mary Pope", "decision_maker": "Mary Louise Pope",
                "dm_relationship": rel, "marital_status": marital}

    def test_spouse_with_surviving_wife(self):
        self.assertEqual(sta._primary_relationship_tag(
            self.subj("Spouse", "Married - surviving wife Mary Louise Pope")), "Wife")

    def test_spouse_with_surviving_husband(self):
        self.assertEqual(sta._primary_relationship_tag(
            self.subj("Spouse (co-owner)", "Married - surviving husband")), "Husband")

    def test_spouse_unknown_gets_no_tag(self):
        self.assertIsNone(sta._primary_relationship_tag(self.subj("Spouse", "Married")))

    def test_wife_unchanged(self):
        self.assertEqual(sta._primary_relationship_tag(self.subj("Wife")), "Wife")

    def test_niece_still_relative(self):
        self.assertEqual(sta._primary_relationship_tag(self.subj("Niece")), "Relative")


if __name__ == "__main__":
    unittest.main()
