"""Offline tests for processed_cases.py - the "have we already done this
probate case?" check (user, 2026-09-21: the Probates folder will sometimes not
be cleared, and the pipeline must catch a repeat itself).

Nothing here touches the CRM, the network, or the real ledger.
Run:  python -m unittest tests.test_processed_cases -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import processed_cases as pc  # noqa: E402


class NormalizeTests(unittest.TestCase):
    def test_leading_zeros_and_spacing_all_match(self):
        for raw in ("PB-2026-0761", "PB-2026-761", "pb-2026-00761", "PB 2026 761", "PB2026761"):
            self.assertEqual(pc.normalize_case(raw), "PB-2026-761", raw)

    def test_no_case_number(self):
        self.assertIsNone(pc.normalize_case("CJ-2026-1854"))
        self.assertIsNone(pc.normalize_case(""))
        self.assertIsNone(pc.normalize_case(None))

    def test_merged_row_yields_every_case_once(self):
        text = "PB-2026-778 (Edwin Russell Cape); PB-2026-780 (Marian Johanna Cape); PB-2026-778"
        self.assertEqual(pc.extract_cases(text), ["PB-2026-778", "PB-2026-780"])


class _Ledgered(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "ledger.json"


class LedgerTests(_Ledgered):
    def test_record_and_read_back(self):
        pc.record_case("PB-2026-0761", decedent="Tina Fay Johnson", path=self.path)
        self.assertIn("PB-2026-761", pc.load_ledger(self.path))

    def test_missing_ledger_is_empty_not_an_error(self):
        self.assertEqual(pc.load_ledger(self.path), {})

    def test_corrupt_ledger_is_loud_never_silently_empty(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            pc.load_ledger(self.path)

    def test_seed_holds_the_five_known_cases(self):
        pc.seed(self.path)
        self.assertEqual(sorted(pc.load_ledger(self.path)),
                         ["PB-2026-760", "PB-2026-761", "PB-2026-777", "PB-2026-778", "PB-2026-780"])

    def test_record_row_records_every_case_on_a_merged_row(self):
        got = pc.record_row({"Case Number": "PB-2026-778 (Edwin); PB-2026-780 (Marian)",
                              "Decedent Name": "Edwin Russell Cape",
                              "Property Street": "17021 N MEMORIAL DR E"}, uuid="u1", path=self.path)
        self.assertEqual(got, ["PB-2026-778", "PB-2026-780"])
        self.assertEqual(pc.load_ledger(self.path)["PB-2026-780"]["uuid"], "u1")


class CheckRowsTests(_Ledgered):
    def setUp(self):
        super().setUp()
        pc.seed(self.path)

    def test_the_two_planted_duplicates_are_caught(self):
        rows = [{"Case Number": "PB-2026-0760", "Decedent Name": "Clifton Lee Ross"},
                {"Case Number": "PB-2026-0761", "Decedent Name": "Tina Fay Johnson"},
                {"Case Number": "PB-2026-779", "Decedent Name": "Mark Anthony O'Guin"}]
        fresh, already = pc.check_rows(rows, path=self.path)
        self.assertEqual([r["Decedent Name"] for r in fresh], ["Mark Anthony O'Guin"])
        self.assertEqual(len(already), 2)
        self.assertFalse(already[0]["_already_processed"]["partial"])

    def test_zero_padding_does_not_hide_a_repeat(self):
        fresh, already = pc.check_rows([{"Case Number": "PB-2026-00777"}], path=self.path)
        self.assertEqual((len(fresh), len(already)), (0, 1))

    def test_merged_row_with_one_case_done_is_partial_and_held(self):
        path2 = Path(self._tmp.name) / "l2.json"
        pc.record_case("PB-2026-778", path=path2)
        fresh, already = pc.check_rows(
            [{"Case Number": "PB-2026-778 (Edwin); PB-2026-780 (Marian)"}], path=path2)
        self.assertEqual(fresh, [])
        self.assertTrue(already[0]["_already_processed"]["partial"])
        self.assertIn("PARTIALLY", pc.describe(already[0]))

    def test_row_with_no_case_number_passes_through_but_is_not_lost(self):
        fresh, already = pc.check_rows([{"Decedent Name": "No Case"}], path=self.path)
        self.assertEqual((len(fresh), len(already)), (1, 0))

    def test_nothing_is_dropped_silently(self):
        rows = [{"Case Number": "PB-2026-760"}, {"Case Number": "PB-2026-999"}, {"Decedent Name": "x"}]
        fresh, already = pc.check_rows(rows, path=self.path)
        self.assertEqual(len(fresh) + len(already), len(rows))


class CrmCheckTests(unittest.TestCase):
    ROW = {"Case Number": "PB-2026-0761", "Property Street": "1916 S 140th East Ave",
           "Property City": "Tulsa", "Property State": "OK"}

    def _run(self, found, notes, row=None, find_raises=False):
        def find(street, city, state):
            if find_raises:
                raise RuntimeError("down")
            return found
        return pc.crm_check_rows([dict(row or self.ROW)], find, lambda u: {"notes": notes})

    def test_record_whose_notes_carry_the_case_is_a_repeat(self):
        fresh, already = self._run({"uuid": "u9"}, "PROBATE FILING\n  Case Number: PB-2026-0761\n")
        self.assertEqual((len(fresh), len(already)), (0, 1))
        self.assertIn("u9", already[0]["_already_processed"]["where"])

    def test_same_address_but_a_different_case_is_a_different_lead(self):
        fresh, already = self._run({"uuid": "u9"}, "FORECLOSURE PETITION\n  Case Number: CJ-2026-100\n")
        self.assertEqual((len(fresh), len(already)), (1, 0))

    def test_address_not_in_the_crm_is_fresh(self):
        fresh, already = self._run(None, "")
        self.assertEqual((len(fresh), len(already)), (1, 0))

    def test_lookup_failure_never_loses_the_row(self):
        fresh, already = self._run(None, "", find_raises=True)
        self.assertEqual((len(fresh), len(already)), (1, 0))

    def test_row_without_an_address_is_left_for_the_ledger(self):
        fresh, already = self._run({"uuid": "u9"}, "Case Number: PB-2026-761",
                                   row={"Case Number": "PB-2026-761"})
        self.assertEqual((len(fresh), len(already)), (1, 0))


class PipelineWiringTests(_Ledgered):
    """The create step must stop BEFORE any lookup when every row is a repeat."""

    def test_all_repeats_stops_before_the_assessor(self):
        import main
        pc.seed(self.path)
        rows = [{"Property Street": "x", "Last Name": "Ross", "Case Number": "PB-2026-0760"},
                {"Property Street": "y", "Last Name": "Johnson", "Case Number": "PB-2026-0761"}]

        class Args:
            notice_type = "probate"
            county = "Tulsa"
            trial_tag = None
            list_name = None

        with mock.patch.object(pc, "LEDGER_PATH", self.path), \
             mock.patch.object(main, "_read_property_template", return_value=rows), \
             mock.patch.object(main, "_enrich_probate_rows",
                               side_effect=AssertionError("looked up an already-processed case")):
            out = main._create_records_for_batch(Args(), Path("batch.xlsx"))
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
