"""Offline tests: `skip-trace --create` is safe to re-run ("same command, add --commit").

Before 2026-10-05 a second `--create` over the same batch re-created every row
and re-posted its Notes and Message Board, so the billed half had to be run
trace-only from datasift_ready_*.csv, a path with no gates. Now rows an earlier
`--create` made are skipped and go straight to the trace.

Everything that touches DataSift is mocked; the ledgers live in a temp dir.
Run:  python tests/test_created_rows.py -v
"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import created_rows  # noqa: E402
import main  # noqa: E402
import processed_cases  # noqa: E402


def _args(**kw):
    ns = types.SimpleNamespace(notice_type="foreclosure", county="Tulsa",
                               trial_tag=None, list_name=None, recreate=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _row(n, **extra):
    r = {"Property Street": f"{100 + n} Main St", "Property City": "Tulsa",
         "First Name": "Test", "Last Name": f"Owner{n}", "Case Number": f"CJ-2026-0{n}",
         "Vacant Lot": "No", "AcctType": "Residential", "Owner Alive": "Yes"}
    r.update(extra)
    return r


class RowKeyTests(unittest.TestCase):
    def test_case_number_leading_zeros(self):
        a = created_rows.row_key({"Case Number": "PB-2026-0761"}, "probate")
        b = created_rows.row_key({"Case Number": "pb-2026-761"}, "probate")
        self.assertEqual(a, b)

    def test_merged_cases_order_does_not_matter(self):
        a = created_rows.row_key({"Case Number": "PB-2026-778; PB-2026-780"}, "probate")
        b = created_rows.row_key({"Case Number": "PB-2026-780 (Marian); PB-2026-778"}, "probate")
        self.assertEqual(a, b)

    def test_notice_type_is_part_of_the_key(self):
        r = {"Case Number": "CJ-2026-5"}
        self.assertNotEqual(created_rows.row_key(r, "probate"),
                            created_rows.row_key(r, "foreclosure"))

    def test_falls_back_to_street_and_last_name(self):
        k = created_rows.row_key({"Property Street": "12 Elm St.", "Last Name": "Doe"}, "foreclosure")
        self.assertEqual(k, "foreclosure|addr:12 elm st|doe")

    def test_nothing_to_key_on(self):
        self.assertIsNone(created_rows.row_key({"Last Name": "Doe"}, "foreclosure"))

    def test_corrupt_ledger_is_loud(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text("{not json", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                created_rows.load(p)


class ReRunTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for target, name in ((created_rows, "created.json"), (processed_cases, "processed.json")):
            p = mock.patch.object(target, "LEDGER_PATH", self.dir / name)
            p.start()
            self.addCleanup(p.stop)
        self.uploads: list[list[str]] = []

    def _run(self, rows, *, findings=(), gate_out=(), args=None, probate_lookup=None,
             listed=()):
        """One `--create`. `gate_out` = last names the post-enrichment gate removes."""
        rows = [dict(r) for r in rows]          # a fresh read of the file each run
        built: list[list[dict]] = []

        def fake_build(rs, *a, **kw):
            built.append(rs)
            return "fake.csv"

        async def fake_upload(**kw):
            self.uploads.append([r["Last Name"] for r in built[-1]])
            return {"success": True, "message": "ok"}

        def fake_align(rs, **kw):
            # SiftMap rewording happens in place - the key must not depend on it.
            for r in rs:
                r["Property Street"] = r["Property Street"].upper() + " REWORDED"
            return {}

        def fake_gate(rs, **kw):
            kept = [r for r in rs if r["Last Name"] not in gate_out]
            for r in kept:
                if r["Last Name"] in listed:
                    r["_mls_listed"] = True
            out = [{**r, "_gate_reasons": ["x"], "_gate_uuid": "u", "_gate_action": "deleted"}
                   for r in rs if r["Last Name"] in gate_out]
            return kept, out

        patches = [
            mock.patch("main._read_property_template", return_value=rows),
            mock.patch("buy_box.apply_buy_box", side_effect=lambda rs: (rs, [])),
            mock.patch("batch_review.review_batch", return_value=list(findings)),
            mock.patch("batch_review.write_review_sheet", return_value="REVIEW.csv"),
            mock.patch("batch_review.print_review", return_value=False),
            mock.patch("siftmap_address.align_rows_to_siftmap", side_effect=fake_align),
            mock.patch("datasift_formatter.build_datasift_csv_from_template", side_effect=fake_build),
            mock.patch("datasift_uploader.upload_to_datasift", side_effect=fake_upload),
            mock.patch("post_enrich_gate.apply_post_enrich_gate", side_effect=fake_gate),
            mock.patch("datasift_api.find_property_by_address", return_value={"uuid": "U"}),
            mock.patch("main._enrich_probate_rows", side_effect=probate_lookup or (lambda rs: rs)),
            mock.patch("main._foreclosure_precreate_checks", side_effect=lambda rs, t, **kw: rs),
            mock.patch("processed_cases.crm_check_rows", side_effect=lambda rs, f, g: (rs, [])),
        ]
        for p in patches:
            p.start()
        try:
            return main._create_records_for_batch(args or _args(), Path("batch.xlsx"))
        finally:
            for p in reversed(patches):
                p.stop()

    def test_first_run_records_each_outcome(self):
        rows = [_row(1), _row(2, **{"Owner Alive": "No"}), _row(3)]
        out = self._run(rows, gate_out=("Owner3",))
        self.assertEqual([r["last"] for r in out], ["Owner1"])
        ledger = created_rows.load()
        statuses = sorted(e["status"] for e in ledger.values())
        self.assertEqual(statuses, ["excluded", "no_trace", "trace"])

    def test_mls_listed_is_created_but_never_traced(self):
        rows = [_row(1), _row(2)]
        out = self._run(rows, listed=("Owner2",))
        self.assertEqual([r["last"] for r in out], ["Owner1"])
        self.assertEqual(self.uploads, [["Owner1", "Owner2"]])
        self.assertEqual(sorted(e["status"] for e in created_rows.load().values()),
                         ["listed", "trace"])
        again = self._run(rows, listed=("Owner2",))
        self.assertEqual([r["last"] for r in again], ["Owner1"])
        self.assertEqual(len(self.uploads), 1)

    def test_rerun_creates_nothing_and_posts_nothing(self):
        rows = [_row(1), _row(2, **{"Owner Alive": "No"}), _row(3)]
        first = self._run(rows, gate_out=("Owner3",))
        self.assertEqual(len(self.uploads), 1)
        again = self._run(rows, gate_out=("Owner3",))
        self.assertEqual(len(self.uploads), 1, "the re-run created records again")
        # Same trace row, including the SiftMap wording from the first run.
        self.assertEqual(again, first)
        self.assertTrue(again[0]["street"].endswith("REWORDED"))

    def test_rerun_with_a_new_row_creates_only_the_new_row(self):
        self._run([_row(1)])
        out = self._run([_row(1), _row(4)])
        self.assertEqual(self.uploads, [["Owner1"], ["Owner4"]])
        self.assertEqual(sorted(r["last"] for r in out), ["Owner1", "Owner4"])

    def test_recreate_creates_again(self):
        self._run([_row(1)])
        self._run([_row(1)], args=_args(recreate=True))
        self.assertEqual(self.uploads, [["Owner1"], ["Owner1"]])

    def test_held_row_is_not_recorded_and_is_created_on_the_rerun(self):
        hold = [{"row": 2, "severity": "BLOCK", "field": "x", "message": "m", "value": ""}]
        out = self._run([_row(1), _row(2)], findings=hold)
        self.assertEqual([r["last"] for r in out], ["Owner1"])
        out = self._run([_row(1), _row(2)])            # reviewed, nothing blocks now
        self.assertEqual(self.uploads, [["Owner1"], ["Owner2"]])
        self.assertEqual(sorted(r["last"] for r in out), ["Owner1", "Owner2"])

    def test_probate_rerun_is_traced_not_held_as_already_processed(self):
        args = _args(notice_type="probate")
        row = _row(1, **{"Case Number": "PB-2026-0901"})
        first = self._run([row], args=args)
        self.assertIn("PB-2026-901", processed_cases.load_ledger())
        boom = mock.Mock(side_effect=AssertionError("looked up a created row again"))
        again = self._run([row], args=args, probate_lookup=boom)
        self.assertEqual(again, first)
        self.assertEqual(len(self.uploads), 1)

    def test_every_row_already_created_but_none_traceable(self):
        self._run([_row(2, **{"Owner Alive": "No"})])
        self.assertIsNone(self._run([_row(2, **{"Owner Alive": "No"})]))
        self.assertEqual(len(self.uploads), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
