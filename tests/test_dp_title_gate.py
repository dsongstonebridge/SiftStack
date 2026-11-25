"""Offline tests for the title check before SmartSkip (2026-11-25). No network.
Run: python -m unittest tests.test_dp_title_gate
"""
import os
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import dp_title_gate as g  # noqa: E402
import owner_profile as op  # noqa: E402

TODAY = date(2026, 11, 25)


def asr(owner, sales=None, account="R1"):
    return {"source": "Tulsa County Assessor", "status": "ok", "findings": [],
            "data": {"account": account, "situs": {"street": "x", "owner": owner},
                     "sales": sales if sales is not None else
                     [{"sale_date": "4/2/1989", "grantor": "PRIOR", "grantee": owner}]}}


def v(first, last, a, named=False):
    return g.verdict(first, last, a, named=named, today=TODAY)


class Clean(unittest.TestCase):
    def test_tacoma_fuller_name_on_title_is_clean_and_adds_the_middle(self):
        r = v("John", "Smith", asr("SMITH, JOHN E"))
        self.assertEqual((r["verdict"], r["middle"]), ("clean", "E"))

    def test_co_owner_second_on_title(self):
        self.assertEqual(v("Linda", "Ormsby", asr("FENWICK, PAUL & LINDA ORMSBY"))["verdict"], "clean")
        self.assertEqual(v("Linda", "Fenwick", asr("FENWICK, PAUL & LINDA"))["verdict"], "clean")

    def test_own_revocable_trust(self):
        self.assertEqual(v("John", "Smith", asr("SMITH JOHN E REV TRUST"))["verdict"], "clean")
        self.assertEqual(v("John", "Smith", asr("SMITH, JOHN E C/O SMITH, JOHN E REV LIVING TRUST"))
                         ["verdict"], "clean")

    def test_compound_surname_and_matching_suffix(self):
        self.assertEqual(v("Francisco", "Perez Rodriguez", asr("PEREZ RODRIGUEZ, FRANCISCO"))["verdict"],
                         "clean")
        self.assertEqual(v("Johnnie", "Fulton Sr", asr("FULTON, JOHNNIE SR"))["verdict"], "clean")


class Review(unittest.TestCase):
    def test_middle_initial_differs(self):
        r = v("John A", "Smith", asr("SMITH, JOHN E"))
        self.assertEqual(r["verdict"], "review")
        self.assertIn("middle name differs", r["reason"])

    def test_suffix_differs(self):
        self.assertIn("suffix differs", v("John", "Smith Jr", asr("SMITH, JOHN SR"))["reason"])

    def test_same_surname_different_first(self):
        r = v("Mary", "Smith", asr("SMITH, JOHN E"))
        self.assertEqual(r["verdict"], "review")
        self.assertIn("same last name", r["reason"])

    def test_initial_only_on_title(self):
        self.assertIn("initial", v("John", "Smith", asr("SMITH, J E"))["reason"])

    def test_family_trust_with_no_first_name(self):
        r = v("John", "Smith", asr("SMITH FAMILY TRUST"))
        self.assertEqual(r["verdict"], "review")
        self.assertIn("family trust", r["reason"])

    def test_first_matches_last_differs(self):
        self.assertIn("maiden", v("Mary", "Jones", asr("SMITH, MARY"))["reason"])

    def test_recent_deed_out_of_their_name_even_when_on_title(self):
        r = v("John", "Smith", asr("SMITH, JOHN", [{"sale_date": "3/1/2025", "grantor": "SMITH, JOHN",
                                                    "grantee": "SMITH FAMILY TRUST"}]))
        self.assertEqual(r["verdict"], "review")
        self.assertIn("changed hands recently", r["reason"])

    def test_recent_purchase_by_them_is_clean(self):
        r = v("John", "Smith", asr("SMITH, JOHN", [{"sale_date": "3/1/2025", "grantor": "A",
                                                    "grantee": "SMITH, JOHN"}]))
        self.assertEqual(r["verdict"], "clean")

    def test_deeded_to_family_like_chiu(self):
        r = v("Christopher", "Chiu", asr("CHIU, LYDIA", [{"sale_date": "5/1/2015",
                                                          "grantor": "CHIU, CHRISTOPHER",
                                                          "grantee": "CHIU, LYDIA"}]))
        self.assertEqual(r["verdict"], "review")
        self.assertIn("deeded the house away", r["reason"])

    def test_named_search_person_off_title_is_asked_not_skipped(self):
        r = v("Alfred", "Fulton", asr("JONES, BOB"), named=True)
        self.assertEqual(r["verdict"], "review")

    def test_near_address_and_assessor_error(self):
        near = {"status": "not checked", "findings": ["Not checked: no Assessor account found"],
                "data": {"near_situs": [{"street": "1518 N COLUMBIA PL", "owner": "DOE, JANE"}]}}
        self.assertEqual(v("Jane", "Doe", near)["verdict"], "review")
        err = {"status": "not checked", "findings": ["Not checked: Assessor error: timeout"], "data": {}}
        self.assertEqual(v("Jane", "Doe", err)["verdict"], "review")


class Skip(unittest.TestCase):
    def test_different_family(self):
        r = v("Mary", "Jones", asr("BROWN, ROBERT"))
        self.assertEqual(r["verdict"], "skip")

    def test_company_no_person(self):
        r = v("Mary", "Jones", asr("ACME PROPERTIES LLC"))
        self.assertEqual((r["verdict"], "company" in r["reason"]), ("skip", True))

    def test_no_parcel_at_all(self):
        none = {"status": "not checked", "findings": ["Not checked: no Assessor account found"],
                "data": {"near_situs": []}}
        self.assertEqual(v("Jane", "Doe", none)["verdict"], "skip")

    def test_sold_long_ago_to_a_stranger(self):
        r = v("Mary", "Jones", asr("BROWN, ROBERT", [{"sale_date": "6/1/2005",
                                                      "grantor": "JONES, MARY", "grantee": "BROWN, ROBERT"}]))
        self.assertEqual(r["verdict"], "skip")
        self.assertIn("sold the house", r["reason"])


class Age(unittest.TestCase):
    def test_tacoma_grandson_too_young(self):
        took = g.acquired("John", "Smith", [{"sale_date": "4/2/1989", "grantee": "SMITH, JOHN E"}])
        self.assertEqual(took, date(1989, 4, 2))
        self.assertIn("Too young", g.age_check("36", took, today=TODAY))

    def test_real_owner_and_unknowns_pass(self):
        took = date(1989, 4, 2)
        self.assertIsNone(g.age_check("78", took, today=TODAY))
        self.assertIsNone(g.age_check("", took, today=TODAY))
        self.assertIsNone(g.age_check("40", None, today=TODAY))


class Helpers(unittest.TestCase):
    def test_near_street(self):
        self.assertTrue(op.near_street("1518 N COLUMBIA PL", "1518 N Columbia Ave"))
        self.assertFalse(op.same_street("1518 N COLUMBIA PL", "1518 N Columbia Ave"))
        self.assertFalse(op.near_street("1518 N COLUMBIA AVE", "1518 N Columbia Ave"))
        self.assertFalse(op.near_street("12214 E 81 PL N", "12214 E 30th St"))

    def test_gate_names_keep_middle_and_suffix(self):
        import deep_prospect_run as run
        self.assertEqual(run.gate_names("Melvin R Monday Sr"), ("Melvin R", "Monday Sr"))
        self.assertEqual(run.gate_names("Francisco|Perez Rodriguez"), ("Francisco", "Perez Rodriguez"))

    def test_assessor_section_records_near_parcel(self):
        a = op.assessor_section("1518 N Columbia Ave", "Tulsa", "Jane Doe",
                                search=lambda q: [{"AccountNo": "R9"}],
                                situs=lambda acct: {"street": "1518 N COLUMBIA PL", "owner": "DOE, JANE"},
                                sales=lambda acct: [], info_html=lambda acct: "")
        self.assertEqual(a["data"]["near_situs"][0]["account"], "R9")
        self.assertEqual(v("Jane", "Doe", a)["verdict"], "review")

    def test_too_young_reason_reads_the_run_folder(self):
        import json
        import shutil
        import tempfile
        import deep_prospect_run as run
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / "owner_profile.json").write_text(json.dumps({"sections": [asr("SMITH, JOHN E")]}))
        (d / "smartskip_export.csv").write_text("Input Name,Relationship,First Name,Last Name,Age\n"
                                                "John Smith,Subject,John,Smith,36\n")
        self.assertIn("Too young", run.too_young_reason(d, {"searched": "John Smith"}))


if __name__ == "__main__":
    unittest.main()
