"""Offline tests for two guards added 2026-09-25.

  1. A record that has already been skip traced is never billed again
     (run_pipeline's repeat guard) unless allow_retrace=True.
  2. Probate records never get owner enrichment - the PR/heir we found must
     not be replaced by the deceased owner of record.

All data is synthetic. Every DataSift call is mocked - nothing here touches
the network or bills.
Run:  python -m unittest tests.test_trace_repeat_guard -v
"""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import datasift_api as api  # noqa: E402
import datasift_uploader as up  # noqa: E402
import skip_trace_agent as agent  # noqa: E402


def _no_network(*a, **k):
    raise AssertionError(f"unexpected network call: {a[:2]}")


def _record(uuid, *, tags=(), skiptraced=False, attempts=0, phones=()):
    return {
        "uuid": uuid,
        "address": {"street": f"{uuid} Test St", "city": "Tulsa", "state": "OK",
                    "postal_code": "74100"},
        "tags": [{"title": t} for t in tags],
        "owner": {"uuid": f"o-{uuid}", "first_name": "Pat", "last_name": "Example",
                  "skiptraced": skiptraced, "skiptrace_attempts": attempts,
                  "address": {}, "phones": [{"number": n, "tags": []} for n in phones]},
    }


class AlreadyTracedTests(unittest.TestCase):
    def test_fresh_record_is_not_traced(self):
        self.assertEqual(agent.already_traced(_record("1")), [])

    def test_tracerfy_tag_counts(self):
        self.assertTrue(agent.already_traced(_record("1", tags=["Tracerfy Skipped"])))

    def test_datasift_skiptraced_counts(self):
        self.assertTrue(agent.already_traced(_record("1", skiptraced=True, attempts=1)))
        self.assertTrue(agent.already_traced(_record("1", attempts=2)))

    def test_hand_added_phone_alone_does_not_count(self):
        # A people-search number the user added by hand before any trace ran.
        rec = _record("1", tags=["true people search"], phones=["9185550100"])
        self.assertEqual(agent.already_traced(rec), [])


class RunPipelineRepeatGuardTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(api, "_request", side_effect=_no_network)
        p.start()
        self.addCleanup(p.stop)
        self.recs = {"A": _record("A", tags=["Tracerfy Skipped"], skiptraced=True, attempts=1),
                     "B": _record("B")}
        for name, fn in (
            ("find_property_by_address",
             lambda street, city="", *a, **k: {"uuid": street.split()[0]}),
            ("get_property", lambda u: self.recs[u]),
        ):
            p = mock.patch.object(api, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)
        self.tracerfy = mock.patch.object(agent, "tracerfy_source", return_value={}).start()
        self.datasift = mock.patch.object(agent, "datasift_source", return_value={}).start()
        mock.patch.object(agent, "score_phones", return_value={}).start()
        mock.patch.object(agent, "writeback", return_value={}).start()
        self.addCleanup(mock.patch.stopall)
        self.rows = [{"street": "A Test St", "city": "Tulsa"},
                     {"street": "B Test St", "city": "Tulsa"}]

    def _traced_uuids(self, m):
        return [s["property_uuid"] for s in m.call_args.args[0]]

    def test_already_traced_record_is_skipped_and_reported(self):
        res = agent.run_pipeline(self.rows, dry_run=False)
        self.assertEqual(self._traced_uuids(self.tracerfy), ["B"])
        self.assertEqual(self._traced_uuids(self.datasift), ["B"])
        self.assertEqual([a["uuid"] for a in res["already_traced"]], ["A"])
        self.assertAlmostEqual(res["spend_estimate"], 0.02 + 0.12)

    def test_all_already_traced_bills_nothing(self):
        res = agent.run_pipeline(self.rows[:1], dry_run=False)
        self.tracerfy.assert_not_called()
        self.datasift.assert_not_called()
        self.assertEqual(len(res["already_traced"]), 1)

    def test_allow_retrace_overrides(self):
        res = agent.run_pipeline(self.rows, dry_run=False, allow_retrace=True)
        self.assertEqual(self._traced_uuids(self.tracerfy), ["A", "B"])
        self.assertEqual(res["already_traced"], [])


class ProbateNeverGetsOwnerEnrichmentTests(unittest.TestCase):
    """The gated group (probate etc.) must be sent with BOTH owner toggles off."""

    def _run(self, rows):
        calls = []

        def enrich(uuids, **kw):
            calls.append((list(uuids), kw))
            return {"count": len(uuids)}

        uuid_map = {up._uuid_map_key(r["Owner Last Name"], r["Property Street Address"]): r["_uuid"]
                    for r in rows}
        with mock.patch.object(up, "_read_csv_rows", return_value=rows), \
             mock.patch.object(up, "_load_uuid_map", return_value=uuid_map), \
             mock.patch.object(up._api, "enrich_properties", side_effect=enrich), \
             mock.patch.object(up, "_restore_people"):
            asyncio.run(up.enrich_records(None, "x.csv", enrich_owner=True,
                                          replace_owner=True))
        return calls

    def _row(self, uuid, notice_type):
        return {"_uuid": uuid, "Notice Type": notice_type,
                "Owner First Name": "Pat", "Owner Last Name": f"Example{uuid}",
                "Property Street Address": f"{uuid} Test St", "Property City": "Tulsa"}

    def test_probate_row_gets_property_enrichment_only(self):
        calls = self._run([self._row("P", "probate"), self._row("F", "foreclosure")])
        by_uuid = {u[0]: kw for u, kw in calls}
        self.assertEqual(by_uuid["P"]["enrich_owner"], False)
        self.assertEqual(by_uuid["P"]["replace_owner"], False)
        self.assertEqual(by_uuid["P"]["enrich_property"], True)
        self.assertEqual(by_uuid["F"]["replace_owner"], True)

    def test_batch_level_lock_in_main(self):
        # _create_records_for_batch must pass replace_owner=False for probate.
        src = open(os.path.join(os.path.dirname(__file__), "..", "src", "main.py"),
                   encoding="utf-8").read()
        self.assertIn('replace_owner=(notice_type != "probate")', src)


if __name__ == "__main__":
    unittest.main()
