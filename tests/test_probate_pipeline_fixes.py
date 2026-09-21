"""Offline tests for the four fixes from the first live probate run (2026-09-11).

  1. DataSift skip trace: wait on the JOB's status, and never report a
     still-running job as "no numbers returned".
  2. Phone tags: verify, then re-send ONCE to numbers that came back empty.
  3. Probate: the traced heir's own numbers get their relationship tag.
  4. wait_for_properties: match a trailing directional the server dropped.

Every DataSift call is mocked - nothing here touches the network or bills.
Run:  python -m unittest tests.test_probate_pipeline_fixes -v
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import datasift_api as api  # noqa: E402
import skip_trace_agent as agent  # noqa: E402


def _no_network(*a, **k):
    raise AssertionError(f"unexpected network call: {a[:2]}")


class _Offline(unittest.TestCase):
    """Every test runs with the raw HTTP layer booby-trapped and dry-run off."""

    def setUp(self):
        p = mock.patch.object(api, "_request", side_effect=_no_network)
        p.start()
        self.addCleanup(p.stop)
        api.set_dry_run(False)
        self.addCleanup(api.set_dry_run, False)


# ── 3. relationship tag ───────────────────────────────────────────────

class RelationshipTagTests(unittest.TestCase):
    CASES = {
        "Daughter (first listed heir with an address - PR address not stated)": "Daughter",
        "Niece (Personal Representative)": "Relative",
        "Son": "Son",
        "Sons": "Son",
        "Granddaughter": "Grandchild",
        "great-grandson": "Grandchild",
        "Stepdaughter": "Relative",
        "Son-in-law": "Relative",
        "Surviving Wife": "Wife",
        "Widow": "Wife",
        "Husband": "Husband",
        "Spouse": "Relative",
        "Adult/Child": "Relative",
        "Nephew": "Relative",
        "Grandmother": "Relative",
        # Not family -> no tag. "Personal" contains "son"; "Creditor" none.
        "Personal Representative": None,
        "Creditor - manager of L & S Group, LLC (largest known creditor); NOT an heir": None,
        "": None,
    }

    def test_mapping(self):
        for text, want in self.CASES.items():
            with self.subTest(text=text):
                self.assertEqual(agent.relationship_tag(text), want)

    def test_only_existing_tag_titles(self):
        existing = {"Daughter", "Son", "Wife", "Husband", "Grandchild", "Relative", None}
        for text in self.CASES:
            self.assertIn(agent.relationship_tag(text), existing)

    def test_same_person(self):
        self.assertTrue(agent._same_person("Wendy Jean Johnson", "Wendy Johnson"))
        self.assertTrue(agent._same_person("Johnson, Wendy", "Wendy Johnson"))
        self.assertTrue(agent._same_person("Gerald Buckley III", "Gerald Buckley"))
        self.assertFalse(agent._same_person("Larry Kaiser", "Wendy Johnson"))
        self.assertFalse(agent._same_person("", "Wendy Johnson"))


class WritebackRelationshipTests(_Offline):
    def _subject(self, **extra):
        subj = {
            "property_uuid": "p1", "owner_uuid": "o1", "name": "Wendy Johnson",
            "first": "Wendy", "last": "Johnson", "property_address": "1916 S 140th East Ave",
            "has_results": True,
            "people": [{
                "first": "Wendy", "last": "Johnson", "name": "Wendy Johnson",
                "key": "wendy|johnson", "relationship": None, "is_primary": True,
                "phones": [
                    {"number": "9180000001", "sources": ["Tracerfy"], "tier": "Dial First",
                     "type_raw": ""},
                    {"number": "9180000002", "sources": ["DataSift"], "tier": "Drop",
                     "type_raw": ""},
                    {"number": "9180000003", "sources": ["Pre-existing"], "tier": "Dial Fourth",
                     "type_raw": ""},
                ],
                "emails": [], "sources": ["Tracerfy"],
            }],
        }
        subj.update(extra)
        return subj

    def _run(self, subj):
        sent = {}

        def apply(owner, mapping, **k):
            sent.update(mapping)
            return {"ok": True, "missing": {}, "retried": [], "partial": {}}

        with mock.patch.object(api, "set_dry_run"), \
             mock.patch.object(api, "upsert_phones"), \
             mock.patch.object(api, "apply_phone_tags_verified", side_effect=apply), \
             mock.patch.object(api, "add_tags"), \
             mock.patch.object(api, "post_message_board"):
            agent.writeback([subj], sources=["Tracerfy", "DataSift"], dry_run=False)
        return sent

    def test_probate_heir_numbers_get_relationship(self):
        sent = self._run(self._subject(dm_relationship="Daughter (first listed heir)",
                                        decision_maker="Wendy Jean Johnson"))
        self.assertIn("Daughter", sent["9180000001"])
        self.assertIn("Daughter", sent["9180000002"])
        # A Pre-existing number's owner is unknown: no permanent relationship label.
        self.assertNotIn("Daughter", sent["9180000003"])
        self.assertEqual(sorted(sent["9180000001"]), ["Daughter", "Dial First", "Tracerfy"])

    def test_foreclosure_owner_gets_no_relationship(self):
        sent = self._run(self._subject())
        for tags in sent.values():
            self.assertFalse({"Daughter", "Relative"} & set(tags))

    def test_relationship_not_hung_on_someone_else(self):
        sent = self._run(self._subject(dm_relationship="Daughter",
                                        decision_maker="Larry Kaiser"))
        for tags in sent.values():
            self.assertNotIn("Daughter", tags)


# ── 2. phone tags: verify + one re-send to EMPTY numbers only ─────────

class ApplyPhoneTagsVerifiedTests(_Offline):
    def _owner(self, tags_by_number):
        return {"phones": [{"number": n, "tags": t} for n, t in tags_by_number.items()]}

    def test_resends_empty_numbers_once_and_verifies(self):
        want = {"111": ["Tracerfy", "Dial First"], "222": ["Tracerfy", "Drop"]}
        owners = [self._owner({"111": [], "222": []}),                  # first verify
                  self._owner({"111": want["111"], "222": want["222"]})]  # after re-send
        with mock.patch.object(api, "set_phone_tags") as st, \
             mock.patch.object(api, "get_owner", side_effect=owners), \
             mock.patch.object(api.time, "sleep"):
            res = api.apply_phone_tags_verified("o1", want)
        self.assertTrue(res["ok"])
        self.assertEqual(res["retried"], ["111", "222"])
        self.assertEqual(st.call_count, 2)
        self.assertEqual(st.call_args_list[1].args[0], want)

    def test_partially_tagged_number_is_never_resent(self):
        want = {"111": ["Tracerfy", "Dial First"], "222": ["Tracerfy", "Drop"]}
        owners = [self._owner({"111": ["Tracerfy"], "222": []}),
                  self._owner({"111": ["Tracerfy"], "222": want["222"]})]
        with mock.patch.object(api, "set_phone_tags") as st, \
             mock.patch.object(api, "get_owner", side_effect=owners), \
             mock.patch.object(api.time, "sleep"):
            res = api.apply_phone_tags_verified("o1", want)
        self.assertEqual(st.call_args_list[1].args[0], {"222": want["222"]})
        self.assertEqual(res["partial"], {"111": ["Dial First"]})
        self.assertFalse(res["ok"])

    def test_clean_first_pass_sends_once(self):
        want = {"111": ["Tracerfy"]}
        with mock.patch.object(api, "set_phone_tags") as st, \
             mock.patch.object(api, "get_owner", return_value=self._owner({"111": ["Tracerfy"]})):
            res = api.apply_phone_tags_verified("o1", want)
        self.assertTrue(res["ok"])
        self.assertEqual(st.call_count, 1)
        self.assertEqual(res["retried"], [])

    def test_dry_run_verifies_nothing(self):
        api.set_dry_run(True)
        with mock.patch.object(api, "set_phone_tags"), \
             mock.patch.object(api, "get_owner", side_effect=AssertionError("read in dry run")):
            res = api.apply_phone_tags_verified("o1", {"111": ["Tracerfy"]})
        self.assertTrue(res["ok"])


# ── 1. DataSift: wait on the job; pending is not "no numbers" ────────

def _job(created, status, total=2):
    return {"uuid": "job1", "created": created, "status": status, "total": total,
            "processed": 0 if status == "processing" else total,
            "meta": {"total_properties": total, "initial_cost": 0.24,
                     "final_cost": None if status == "processing" else 0.24}}


class FindSkipTraceJobTests(_Offline):
    def test_picks_the_job_created_after_submission(self):
        now = datetime(2026, 9, 11, 17, 18, 2, tzinfo=timezone.utc)
        jobs = [_job("2026-09-11T17:18:03.729926Z", "processing"),
                _job("2026-09-04T23:51:15.378240Z", "complete", total=12)]
        with mock.patch.object(api, "list_skip_trace_jobs", return_value=jobs):
            self.assertEqual(agent._find_skip_trace_job(now, 2)["status"], "processing")

    def test_ignores_older_jobs_and_wrong_sizes(self):
        now = datetime(2026, 9, 11, 17, 18, 2, tzinfo=timezone.utc)
        with mock.patch.object(api, "list_skip_trace_jobs",
                               return_value=[_job("2026-09-04T23:51:15Z", "complete", 2)]):
            self.assertIsNone(agent._find_skip_trace_job(now, 2))
        with mock.patch.object(api, "list_skip_trace_jobs",
                               return_value=[_job("2026-09-11T17:18:03Z", "processing", 5)]):
            self.assertIsNone(agent._find_skip_trace_job(now, 2))


class DatasiftSourceTests(_Offline):
    def _subject(self):
        return {"property_uuid": "p1", "owner_uuid": "o1", "first": "Rhonda",
                "last": "Thomas", "name": "Rhonda Thomas",
                "property_address": "4529 E Xyler St", "people": [],
                "has_results": False}

    def _patches(self, job_statuses, phones_after):
        created = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        statuses = iter(job_statuses)
        last = [job_statuses[-1]]

        def jobs():
            st = next(statuses, last[0])
            return [_job(created, st, total=1)]

        prop = {"uuid": "p1", "owner": {"phones": [{"number": n, "type": "MOBILE"}
                                                   for n in phones_after],
                                        "emails": []}}
        return [mock.patch.object(api, "estimate_skip_trace",
                                  return_value={"number_of_records": 1, "cost": 0.12}),
                mock.patch.object(api, "submit_skip_trace", return_value={}),
                mock.patch.object(api, "list_skip_trace_jobs", side_effect=jobs),
                mock.patch.object(api, "get_property", return_value=prop),
                mock.patch("time.sleep")]

    def test_waits_for_the_job_then_collects_new_numbers(self):
        subj = self._subject()
        ps = self._patches(["processing", "processing", "complete"], ["4174999934"])
        for p in ps:
            p.start()
        try:
            out = agent.datasift_source([subj], dry_run=False, poll_seconds=0,
                                        timeout_seconds=30)
        finally:
            for p in ps:
                p.stop()
        self.assertEqual([ph["number"] for ph in out["p1"][0]["phones"]], ["4174999934"])
        self.assertEqual(out["p1"][0]["phones"][0]["sources"], ["DataSift"])
        self.assertNotIn("datasift_pending", subj)

    def test_job_still_running_marks_pending_not_empty(self):
        subj = self._subject()
        ps = self._patches(["processing"], [])
        for p in ps:
            p.start()
        try:
            out = agent.datasift_source([subj], dry_run=False, poll_seconds=0,
                                        timeout_seconds=0.05)
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(out, {})
        self.assertTrue(subj["datasift_pending"])

        board = agent.build_message_board(subj, sources=["Tracerfy", "DataSift"])
        self.assertNotIn("no numbers returned", board)
        self.assertIn("still processing", board)
        self.assertFalse(board.startswith("Tracerfy + DataSift"))

    def test_finished_job_with_no_numbers_still_says_so(self):
        subj = self._subject()
        board = agent.build_message_board(subj, sources=["Tracerfy", "DataSift"])
        self.assertIn("no numbers returned", board)


# ── 4. wait_for_properties: dropped trailing directional ─────────────

def _rec(street, city="Tulsa", uuid="u1"):
    return {"uuid": uuid, "address": {"street": street, "city": city}}


class ScanPageMatchesTests(_Offline):
    def _keys(self, *streets):
        return {api.address_key(s, "Tulsa") for s in streets}

    def test_dropped_directional_matches(self):
        hits = api._scan_page_matches([_rec("4529 E Xyler St")], self._keys("4529 E Xyler St N"))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], api.address_key("4529 E Xyler St N", "Tulsa"))

    def test_exact_match_still_works(self):
        hits = api._scan_page_matches([_rec("1916 S 140Th East Ave")],
                                      self._keys("1916 S 140th East Ave"))
        self.assertEqual(len(hits), 1)

    def test_stored_with_other_directional_is_another_street(self):
        self.assertEqual(api._scan_page_matches([_rec("123 E 56Th St S")],
                                                self._keys("123 E 56th St N")), [])

    def test_two_awaited_addresses_collapse_is_ambiguous(self):
        self.assertEqual(api._scan_page_matches([_rec("1 E A St")],
                                                self._keys("1 E A St N", "1 E A St S")), [])

    def test_two_page_records_collapse_is_ambiguous(self):
        page = [_rec("1 E A St", uuid="a"), _rec("1 E A Ave", uuid="b")]
        self.assertEqual(api._scan_page_matches(page, self._keys("1 E A St N")), [])

    def test_city_must_match(self):
        self.assertEqual(api._scan_page_matches([_rec("4529 E Xyler St", city="Owasso")],
                                                self._keys("4529 E Xyler St N")), [])


class OrdinalStreetTests(_Offline):
    """The Assessor writes numbered streets without the ordinal; the server
    stores '12Th'. Sherman (PB-2026-777, 2026-09-21) read as 'never created'
    through two 300s timeouts and got no notes or Message Board."""

    def test_assessor_form_matches_server_form(self):
        sent = api._bare_street(api._norm_street("3012 S 12 ST E"))
        stored = api._bare_street(api._norm_street("3012 S 12Th St"))
        self.assertEqual(sent, stored)

    def test_ordinal_suffixes_all_strip(self):
        for a, b in (("1ST", "1"), ("22ND", "22"), ("3RD", "3"), ("56TH", "56")):
            self.assertEqual(api._norm_street(f"10 E {a} ST"), api._norm_street(f"10 E {b} ST"))

    def test_different_street_numbers_still_differ(self):
        self.assertNotEqual(api._norm_street("3012 S 12 ST E"), api._norm_street("3012 S 13 ST E"))

    def test_words_ending_in_th_are_untouched(self):
        self.assertEqual(api._norm_street("5 Smith St"), "5 SMITH ST")
        self.assertEqual(api._norm_street("5 North Ave"), "5 N AVE")


class RecordTypeScopeTests(_Offline):
    """Cape (2026-09-21) is typed 'incomplete'; a 'clean' scope estimates 0 for
    it, so the DataSift half of the double skip trace was silently skipped."""

    TYPES = {"S": "clean", "C": "incomplete", "C2": "incomplete"}

    def _run(self, fn, uuids, **kw):
        posted = []

        def fake_request(method, url, json_body=None, **k):
            if json_body is not None:
                posted.append(json_body)
                must = json_body["query"]["must"]
                # Emulate the server: the scope only sees records of its own type.
                n = sum(1 for u in must["properties"] if self.TYPES[u] == must["property_type"])
                return {"number_of_records": n, "cost": 0.12 * n, "cost_per_owner": 0.12,
                        "balance": 10.0, "count": n}
            return {}

        with mock.patch.object(api, "_request", side_effect=fake_request),              mock.patch.object(api, "get_property", side_effect=lambda u: {"type": self.TYPES[u]}):
            out = fn(uuids, **kw)
        return out, posted

    def test_incomplete_record_is_now_found_by_the_estimate(self):
        out, posted = self._run(api.estimate_skip_trace, ["C"])
        self.assertEqual(out["number_of_records"], 1)
        self.assertEqual(posted[0]["query"]["must"]["property_type"], "incomplete")

    def test_clean_record_still_uses_the_clean_scope(self):
        out, posted = self._run(api.estimate_skip_trace, ["S"])
        self.assertEqual(out["number_of_records"], 1)
        self.assertEqual(posted[0]["query"]["must"]["property_type"], "clean")

    def test_mixed_batch_is_split_and_summed(self):
        out, posted = self._run(api.estimate_skip_trace, ["S", "C"])
        self.assertEqual(out["number_of_records"], 2)
        self.assertEqual({b["query"]["must"]["property_type"] for b in posted}, {"clean", "incomplete"})
        for b in posted:                       # every call is still scoped to specific uuids
            self.assertTrue(b["query"]["must"]["properties"])

    def test_submit_mixed_batch_makes_one_guarded_call_per_type(self):
        out, posted = self._run(api.submit_skip_trace, ["S", "C", "C2"], max_records=3)
        billed = [b for b in posted if b["estimate"] == 0]
        self.assertEqual(len(billed), 2)
        sizes = sorted(len(b["query"]["must"]["properties"]) for b in billed)
        self.assertEqual(sizes, [1, 2])

    def test_explicit_type_is_respected(self):
        out, posted = self._run(api.estimate_skip_trace, ["C"], property_type="clean")
        self.assertEqual(out["number_of_records"], 0)

    def test_enrich_finds_an_incomplete_record(self):
        out, posted = self._run(api.enrich_properties, ["C"])
        self.assertEqual(out["count"], 1)

    def test_enrich_still_refuses_an_empty_list(self):
        with self.assertRaises(api.DataSiftAPIError):
            api.enrich_properties([])

    def test_unreadable_type_falls_back_to_clean(self):
        with mock.patch.object(api, "get_property", side_effect=RuntimeError("down")):
            self.assertEqual(api._group_uuids_by_record_type(["S"]), {"clean": ["S"]})


class PeopleSearchNumberTests(unittest.TestCase):
    """A number added by hand/people search BEFORE the traces keeps its tag and
    shows agreement when a provider returns it too (Copley, 2026-09-21)."""

    def _rec(self, tags):
        return {"owner": {"phones": [{"number": "(918) 555-0142", "type": "MOBILE", "tags": tags}]}}

    def test_people_search_tag_is_kept_not_relabelled_preexisting(self):
        out = agent._existing_phones(self._rec([agent.SOURCE_PEOPLE_SEARCH]))
        self.assertEqual(out[0]["sources"], [agent.SOURCE_PEOPLE_SEARCH])

    def test_untagged_existing_number_is_still_preexisting(self):
        out = agent._existing_phones(self._rec([]))
        self.assertEqual(out[0]["sources"], ["Pre-existing"])

    def test_other_tags_do_not_count_as_people_search(self):
        out = agent._existing_phones(self._rec(["Tracerfy", "Dial First"]))
        self.assertEqual(out[0]["sources"], ["Pre-existing"])

    def _subject(self, existing, incoming_sources):
        person = {"first": "Dallas", "last": "Copley", "name": "Dallas Copley", "key": "dallas|copley",
                  "relationship": None, "age": "", "deceased": False, "is_primary": True,
                  "mailing_street": "", "mailing_city": "", "mailing_state": "",
                  "sources": ["Tracerfy"], "emails": [],
                  "phones": [{"number": "9185550142", "sources": incoming_sources, "type_raw": "",
                              "tier": None, "score": None}]}
        return {"property_uuid": "p1", "first": "Dallas", "last": "Copley", "name": "Dallas Copley",
                "people": [person], "existing_phones": existing}

    def test_provider_agreement_records_both_sources(self):
        subj = self._subject(agent._existing_phones(self._rec([agent.SOURCE_PEOPLE_SEARCH])), ["Tracerfy"])
        agent.merge_sources([subj], {})
        ph = subj["people"][0]["phones"]
        self.assertEqual(len(ph), 1)
        self.assertEqual(sorted(ph[0]["sources"]), sorted([agent.SOURCE_PEOPLE_SEARCH, "Tracerfy"]))

    def test_bare_preexisting_never_adds_a_tag_to_a_provider_number(self):
        subj = self._subject(agent._existing_phones(self._rec([])), ["Tracerfy"])
        agent.merge_sources([subj], {})
        self.assertEqual(subj["people"][0]["phones"][0]["sources"], ["Tracerfy"])


class WaitForPropertiesTests(_Offline):
    def test_found_on_first_poll_without_the_fallback(self):
        page = {"results": [_rec("4529 E Xyler St", uuid="ross")]}
        with mock.patch.object(api, "_request", return_value=page), \
             mock.patch.object(api, "find_property_by_address",
                               side_effect=AssertionError("fallback should not run")):
            found = api.wait_for_properties([("4529 E Xyler St N", "Tulsa")],
                                            timeout_seconds=5, poll_seconds=0)
        self.assertEqual(list(found.values())[0]["uuid"], "ross")


# ── main._trace_row: probate columns reach run_pipeline ──────────────

class TraceRowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import main
        cls.main = main

    def test_template_row_carries_probate_columns(self):
        row = self.main._trace_row({
            "Property Street": "1916 S 140th East Ave", "Property City": "Tulsa",
            "First Name": "Wendy", "Last Name": "Johnson",
            "Decision Maker": "Wendy Jean Johnson", "DM Relationship": "Daughter",
            "Heir Count": 2, "Date of Death": datetime(2025, 2, 22),
        })
        self.assertEqual(row["street"], "1916 S 140th East Ave")
        self.assertEqual(row["DM Relationship"], "Daughter")
        self.assertEqual(row["Heir Count"], "2")
        self.assertEqual(row["Date of Death"], "02/22/2025")   # never a datetime

    def test_datasift_csv_row(self):
        row = self.main._trace_row({"Property Street Address": "4529 E Xyler St N",
                                    "Property City": "Tulsa", "Owner First Name": "Rhonda",
                                    "Owner Last Name": "Thomas", "DM Relationship": ""})
        self.assertEqual((row["first"], row["last"]), ("Rhonda", "Thomas"))
        self.assertNotIn("DM Relationship", row)                # blanks dropped

    def test_no_street_is_skipped(self):
        self.assertIsNone(self.main._trace_row({"Property City": "Tulsa"}))


if __name__ == "__main__":
    unittest.main()
