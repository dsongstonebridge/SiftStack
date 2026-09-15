"""Offline tests: STOP-AND-ASK is PER-ROW, not per-batch.

User, 2026-09-15: "I don't want the stop and ask properties to slow down the
work on the easy ones... the tougher ones can be saved for my manual review
last while the easy ones are running." Before this fix, ANY row with a BLOCK
finding halted `_create_records_for_batch()` for the WHOLE batch - a single
messy case held up every clean one behind it. Now only rows that actually
earned a BLOCK are held back; everything else proceeds to creation in the
same run.

Heavy dependencies (review_batch's real content, the DataSift API, the CSV
writer) are mocked out here on purpose - this file tests ONLY the
partitioning logic in `_create_records_for_batch()`, not those pieces
individually (each already has its own test coverage elsewhere).

Nothing here touches the network.
Run:  python tests/test_batch_creation_gate.py -v
"""

import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402


def _args(**kw):
    ns = types.SimpleNamespace(notice_type="foreclosure", county="Tulsa",
                               trial_tag=None, list_name=None)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _row(n: int) -> dict:
    return {"Property Street": f"{100 + n} Main St", "Property City": "Tulsa",
           "First Name": "Test", "Last Name": f"Owner{n}", "Vacant Lot": "No",
           "AcctType": "Residential"}


class PerRowGateTests(unittest.TestCase):
    def setUp(self):
        self.rows = [_row(1), _row(2), _row(3)]
        self._csv_calls: list[list[dict]] = []

    def _run(self, findings):
        async def fake_upload(**kwargs):
            return {"success": True, "message": "ok"}

        def fake_build_csv(rows, *a, **kw):
            self._csv_calls.append(rows)
            return "fake.csv"

        with mock.patch("main._read_property_template", return_value=self.rows), \
             mock.patch("buy_box.apply_buy_box", return_value=(self.rows, [])), \
             mock.patch("batch_review.review_batch", return_value=findings), \
             mock.patch("batch_review.write_review_sheet", return_value="REVIEW.csv"), \
             mock.patch("batch_review.print_review", return_value=False), \
             mock.patch("datasift_formatter.build_datasift_csv_from_template",
                        side_effect=fake_build_csv), \
             mock.patch("datasift_uploader.upload_to_datasift", side_effect=fake_upload):
            return main._create_records_for_batch(_args(), Path("fake_input.csv"))

    def test_no_blocks_creates_every_row(self):
        result = self._run(findings=[])
        self.assertEqual(len(self._csv_calls[0]), 3)
        self.assertEqual(len(result), 3)

    def test_one_blocked_row_does_not_hold_up_the_other_two(self):
        # Row 2 (1-indexed) is the only one with a BLOCK finding.
        findings = [{"row": 2, "severity": "BLOCK", "field": "Title Holder of Record",
                    "message": "NOT STRAIGHTFORWARD", "value": ""}]
        result = self._run(findings)
        created = self._csv_calls[0]
        self.assertEqual(len(created), 2)
        self.assertNotIn(self.rows[1], created)          # row 2 held back
        self.assertIn(self.rows[0], created)
        self.assertIn(self.rows[2], created)
        self.assertEqual(len(result), 2)

    def test_warn_only_rows_are_never_held_back(self):
        # A WARN is not a BLOCK - every row still proceeds.
        findings = [{"row": 2, "severity": "WARN", "field": "Mailing Street",
                    "message": "no mailing address", "value": ""}]
        result = self._run(findings)
        self.assertEqual(len(self._csv_calls[0]), 3)
        self.assertEqual(len(result), 3)

    def test_multiple_blocked_rows_all_held_together(self):
        findings = [
            {"row": 1, "severity": "BLOCK", "field": "X", "message": "m1", "value": ""},
            {"row": 3, "severity": "BLOCK", "field": "X", "message": "m2", "value": ""},
        ]
        result = self._run(findings)
        created = self._csv_calls[0]
        self.assertEqual(len(created), 1)
        self.assertIn(self.rows[1], created)
        self.assertEqual(len(result), 1)

    def test_every_row_blocked_stops_with_nothing_created(self):
        findings = [{"row": i, "severity": "BLOCK", "field": "X", "message": "m", "value": ""}
                   for i in (1, 2, 3)]
        result = self._run(findings)
        self.assertIsNone(result)
        self.assertEqual(self._csv_calls, [])   # never even reached the upload step


if __name__ == "__main__":
    unittest.main(verbosity=2)
