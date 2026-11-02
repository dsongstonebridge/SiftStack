"""SmartSkip: one order, paid from the balance, never the card. Offline: a fake
session and fake record, so nothing touches SmartSkip or DataSift and nothing is billed."""
import argparse
import csv
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import deep_prospect_run as run  # noqa: E402
import smartskip_trace  # noqa: E402

INPUT_FIELDS = ["First Name", "Last Name", "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
                "Property Address", "Property City", "Property State", "Property Zip"]


class FakeSess:
    def __init__(self, balance=1000, reply=None):
        self.balance, self.reply, self.posts = balance, reply, []

    def call(self, method, path, body=None, **kw):
        if path == "/wallet/balance":
            return 200, {"balance": self.balance}, ""
        self.posts.append((path, body))
        return 200, self.reply or {"status": "succeeded", "walletTransactionId": "w1"}, ""


class CardIsOff(unittest.TestCase):
    def test_card_pay_refuses_without_any_call(self):
        sess = mock.Mock()
        with self.assertRaises(SystemExit):
            smartskip_trace.pay(sess, "b1")
        sess.call.assert_not_called()

    def test_balance_payment_names_no_card(self):
        s = FakeSess()
        run.pay_from_balance(s, "b1", 3)
        self.assertEqual(s.posts, [("/bulk-skip/payment-intent", {"bulkSkipId": "b1", "paymentSource": "wallet"})])

    def test_short_balance_stops_before_any_payment(self):
        s = FakeSess(balance=130)
        with self.assertRaises(SystemExit) as e:
            run.pay_from_balance(s, "b1", 10)
        self.assertEqual(s.posts, [])
        self.assertIn("card was not touched", str(e.exception))

    def test_exact_balance_pays(self):
        s = FakeSess(balance=150)
        run.pay_from_balance(s, "b1", 10)
        self.assertEqual(len(s.posts), 1)

    def test_card_style_or_failed_reply_stops(self):
        for reply in ({"clientSecret": "x", "status": "requires_action"}, {"status": "failed"}):
            with self.assertRaises(SystemExit):
                run.pay_from_balance(FakeSess(reply=reply), "b1", 1)


class Orders(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for p in (mock.patch.object(run, "RUNS_DIR", self.tmp),
                  mock.patch.object(run, "ORDERS_DIR", self.tmp / "_orders")):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _ns(street, person="Mary Smith"):
        return argparse.Namespace(street=street, city="Tulsa", state="OK", search_person=person,
                                  search_at="mailing", spouse=None, known=None, restart=False)

    def _order(self, streets):
        """Open an order with the record + obituary steps stubbed. Returns the fake SmartSkip module."""
        ss = mock.Mock()
        ss.submit.return_value = {"bulkSkipId": "b1", "entities": len(streets)}
        rec = {"address": {"street": "x", "city": "Tulsa", "state": "OK", "postal_code": "74101"},
               "owner": {"first_name": "Pat", "last_name": "Heir", "address": {}}}

        def row(person, record, at_property):
            r = dict.fromkeys(INPUT_FIELDS, "")
            r.update({"Mailing Address": self.current, "Property Address": self.current, "Property City": "Tulsa"})
            return r

        def prepare(a):
            self.current = a.street
            return real(a)
        real = run._prepare
        with mock.patch.object(run, "_smartskip", return_value=ss), \
                mock.patch.object(run.dps, "_resolve", return_value=("u", "o", rec)), \
                mock.patch.object(run, "prior_run_reason", return_value=None), \
                mock.patch.object(run, "build_input_row", side_effect=row), \
                mock.patch.object(run, "decedent_from_board", return_value=None), \
                mock.patch.object(run, "known_family_from_board", return_value=[]), \
                mock.patch.object(run.obituary_check, "check", return_value={"candidates": []}), \
                mock.patch.object(run.obituary_check, "report", return_value="obit"), \
                mock.patch.object(run, "_prepare", side_effect=prepare):
            code = run._open_order([self._ns(s) for s in streets])
        self.assertEqual(code, 0)
        return ss

    def test_many_records_make_one_order(self):
        ss = self._order(["1 A St", "2 B St", "3 C St"])
        self.assertEqual(ss.submit.call_count, 1)
        order_dir = next((self.tmp / "_orders").iterdir())
        rows = list(csv.DictReader(open(order_dir / "smartskip_input.csv", encoding="utf-8")))
        self.assertEqual([r["Property Address"] for r in rows], ["1 A St", "2 B St", "3 C St"])
        order = json.loads((order_dir / "order.json").read_text(encoding="utf-8"))
        self.assertEqual((order["bulkSkipId"], order["rows"], order["paid"], len(order["runs"])), ("b1", 3, False, 3))
        state = json.loads((self.tmp / "1-a-st" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual((state["bulkSkipId"], state["order"], state["paid"]), ("b1", order_dir.name, False))

    def test_start_refused_without_only_one(self):
        a = self._ns("1 A St")
        a.only_one = False
        with mock.patch.object(run, "_open_order") as op:
            self.assertEqual(run.cmd_start(a), 7)
        op.assert_not_called()

    def test_start_only_one_is_an_order_of_one(self):
        a = self._ns("1 A St")
        a.only_one = True
        with mock.patch.object(run, "_open_order", return_value=0) as op:
            self.assertEqual(run.cmd_start(a), 0)
        self.assertEqual(op.call_args.args[0], [a])

    def test_a_run_of_singles_is_refused(self):
        self._order(["1 A St"])
        a = self._ns("2 B St")
        a.only_one = True
        with mock.patch.object(run, "_open_order") as op:
            self.assertEqual(run.cmd_start(a), 7)
        op.assert_not_called()
        self.assertIsNone(run._recent_order(now=time.time() + run.ONE_ORDER_WINDOW_SECONDS + 5))

    def _export(self, rows):
        p = self.tmp / "export.csv"
        with open(p, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["Input Name", "Property Address", "Property City"])
            w.writeheader()
            w.writerows(rows)
        return p

    def _run_dirs(self, streets):
        out = []
        for s in streets:
            d = self.tmp / s.replace(" ", "-")
            d.mkdir()
            with open(d / "smartskip_input.csv", "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=["Property Address", "Property City"])
                w.writeheader()
                w.writerow({"Property Address": s, "Property City": "Tulsa"})
            out.append(d)
        return out

    def test_export_splits_to_each_record_and_flags_an_empty_one(self):
        runs = self._run_dirs(["1 A St", "2 B St", "3 C St"])
        empty = run.split_export(self._export([
            {"Input Name": "Mary", "Property Address": "1 A St", "Property City": "Tulsa"},
            {"Input Name": "Mary", "Property Address": "1 a st", "Property City": "TULSA"},
            {"Input Name": "Bob", "Property Address": "2 B St", "Property City": "Tulsa"}]), runs)
        self.assertEqual(empty, [runs[2]])
        self.assertEqual(len(list(csv.DictReader(open(runs[0] / "smartskip_export.csv", encoding="utf-8")))), 2)
        self.assertEqual(len(list(csv.DictReader(open(runs[1] / "smartskip_export.csv", encoding="utf-8")))), 1)
        self.assertFalse((runs[2] / "smartskip_export.csv").exists())

    def test_a_row_no_record_claims_is_an_error(self):
        runs = self._run_dirs(["1 A St"])
        with self.assertRaises(SystemExit):
            run.split_export(self._export([
                {"Input Name": "Mary", "Property Address": "1 A St", "Property City": "Tulsa"},
                {"Input Name": "Zed", "Property Address": "9 Z St", "Property City": "Tulsa"}]), runs)

    def test_paid_order_does_not_pay_twice(self):
        order_dir = self.tmp / "_orders" / "o1"
        order_dir.mkdir(parents=True)
        runs = self._run_dirs(["1 A St"])
        (runs[0] / "state.json").write_text(json.dumps(
            {"street": "1 A St", "searched": "Mary", "paid": True}), encoding="utf-8")
        (order_dir / "order.json").write_text(json.dumps(
            {"bulkSkipId": "b1", "rows": 1, "paid": True, "runs": [str(runs[0])]}), encoding="utf-8")
        ss = mock.Mock()
        ss.fetch_status.return_value = [{"status": "completed"}]
        ss.download.side_effect = lambda sess, bid, out, fmt: shutil.copy(self._export(
            [{"Input Name": "Mary", "Property Address": "1 A St", "Property City": "Tulsa"}]), out)
        a = argparse.Namespace(order=str(order_dir), run=None, wait=30)
        with mock.patch.object(run, "_smartskip", return_value=ss), \
                mock.patch.object(run, "pay_from_balance") as pay, \
                mock.patch.object(run.smartskip_parse, "parse", return_value=[]), \
                mock.patch.object(run.dps, "pick_record", return_value={"subject_phones": [], "relatives": []}):
            self.assertEqual(run.cmd_charge(a), 0)
        pay.assert_not_called()


if __name__ == "__main__":
    unittest.main()
