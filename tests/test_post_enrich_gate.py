"""Offline tests for the post-enrichment exclusion gate (2026-09-25).

Fixtures are the six 2026-09-22 records as read live on 2026-09-25, plus the
MLS positive control (2441 S Norwood Ave, read "Listed"). Every DataSift call
is a fake - nothing here touches the network.
Run:  python -m unittest tests.test_post_enrich_gate -v
"""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from post_enrich_gate import (MLS_TAG, apply_post_enrich_gate, check_property,  # noqa: E402
                              mls_listed)

TODAY = date(2026, 9, 25)


def prop(uuid, equity=None, sold=None, mls="Off Market", phones=()):
    return {"uuid": uuid, "equity_percent": equity, "last_sold": sold, "mls": mls,
            "owner": {"phones": [{"number": p} for p in phones]}}


LIVE = {
    "Quick": prop("q", "6.52", "2024-11-26"),
    "Vivas": prop("v", "8.23", "2023-10-03"),
    "Fry": prop("f", "8.98", None),
    "Alexander": prop("a", "9.90", "2022-10-28"),
    "Miller": prop("m", "36.35", None),
    "Watkins": prop("w", "25.18", "2024-09-20"),
}


class CheckProperty(unittest.TestCase):
    def test_live_batch(self):
        fails = {k for k, p in LIVE.items() if check_property(p, today=TODAY)}
        # Watkins (sold 2024-09-20) passed under the 2-year window and fails
        # under the 3-year one that foreclosure uses from 2026-10-09.
        self.assertEqual(fails, {"Quick", "Vivas", "Fry", "Alexander", "Watkins"})
        # Probate keeps the 2-year window, so Watkins would pass there.
        self.assertEqual(check_property(LIVE["Watkins"], today=TODAY,
                                        notice_type="probate"), [])

    def test_mls_listed_is_never_a_reason(self):
        # 2026-10-09: listed foreclosures are kept and flagged, not excluded.
        listed = prop("n", "88.50", None, mls="Listed")
        self.assertEqual(check_property(listed, today=TODAY), [])
        self.assertTrue(mls_listed(listed))
        self.assertFalse(mls_listed(prop("n", "88.50", None, mls="Off Market")))
        self.assertFalse(mls_listed(prop("n", "88.50", None, mls="Weird")))

    def test_fails_open_on_missing_or_junk(self):
        self.assertEqual(check_property({}, today=TODAY), [])
        self.assertEqual(check_property(prop("x", "", "", mls=""), today=TODAY), [])
        self.assertEqual(check_property(prop("x", "n/a", "garbage", mls="Weird"),
                                        today=TODAY), [])

    def test_boundaries(self):
        self.assertEqual(check_property(prop("x", "15.00"), today=TODAY), [])
        self.assertTrue(check_property(prop("x", "14.99"), today=TODAY))
        # foreclosure: exactly 3 years ago is outside the window; one day later is inside
        self.assertEqual(check_property(prop("x", "50", "2023-09-25"), today=TODAY), [])
        self.assertTrue(check_property(prop("x", "50", "2023-09-26"), today=TODAY))
        # probate: still 2 years
        self.assertEqual(check_property(prop("x", "50", "2024-09-25"), today=TODAY,
                                        notice_type="probate"), [])
        self.assertTrue(check_property(prop("x", "50", "2024-09-26"), today=TODAY,
                                       notice_type="probate"))


def typed(st, units=None, use="Residential", **kw):
    p = prop("t", "60.00", None, **kw)
    p.update(structure_type=st, units=units, building_use_code=use)
    return p


class ProbateRules(unittest.TestCase):
    """2026-10-08: probate allows MLS-listed, and excludes condos / mobile
    homes / 3+ units off DataSift's structure_type. Foreclosure is unchanged."""

    def check(self, p, nt="probate"):
        return check_property(p, today=TODAY, notice_type=nt)

    def test_mls_listed_allowed_for_both(self):
        listed = prop("n", "88.50", None, mls="Listed")
        self.assertEqual(self.check(listed), [])
        self.assertEqual(self.check(listed, "foreclosure"), [])

    def test_live_types(self):
        # Values read live 2026-10-08.
        self.assertTrue(self.check(typed(
            "Mobile/Manufactured Home (regardless of Land ownership)", 1)))     # Cape
        self.assertEqual(self.check(typed("Single Family Residential", 1)), [])
        self.assertEqual(self.check(typed("Duplex (2 units, any combination)", 2)), [])
        self.assertEqual(self.check(typed("Residential-Vacant Land")), [])     # Sherman

    def test_condo_units_and_non_residential(self):
        self.assertTrue(self.check(typed("Condominium")))
        self.assertTrue(self.check(typed("Triplex (3 units, any combination)", 3)))
        self.assertTrue(self.check(typed("Something New", 4)))
        self.assertTrue(self.check(typed("Office Building", use="Commercial")))

    def test_type_check_is_probate_only(self):
        self.assertEqual(self.check(typed("Condominium"), "foreclosure"), [])

    def test_fails_open(self):
        self.assertEqual(self.check(typed(None, use=None)), [])
        self.assertEqual(self.check(typed("Some Unknown Type")), [])

    def test_vacant_land_kept_with_board_note(self):
        p = typed("Residential-Vacant Land")
        p["owner"]["uuid"] = "own-1"
        posts = []
        kept, excluded = apply_post_enrich_gate(
            [{"Property Street": "3012 S 12th St", "Property City": "Broken Arrow"}],
            find_property=lambda *a: {"uuid": "t"}, get_property=lambda u: p,
            delete_property=lambda u: self.fail("must not delete"),
            today=TODAY, notice_type="probate",
            post_board=lambda o, m: posts.append((o, m)))
        self.assertEqual(len(kept), 1)
        self.assertEqual(excluded, [])
        self.assertEqual(posts[0][0], "own-1")
        self.assertIn("Residential-Vacant Land", posts[0][1])


class ApplyGate(unittest.TestCase):
    def run_gate(self, props, fail_delete=()):
        rows = [{"Property Street": f"{i} Test St", "Property City": "Tulsa",
                 "Last Name": name} for i, name in enumerate(props, start=1)]
        by_street = {r["Property Street"]: props[r["Last Name"]] for r in rows}
        deleted, forgotten = [], []

        def find(street, city, state):
            p = by_street.get(street)
            return {"uuid": p["uuid"]} if p else None

        def get(uuid):
            return next(p for p in props.values() if p and p["uuid"] == uuid)

        def delete(uuid):
            if uuid in fail_delete:
                raise RuntimeError("500")
            deleted.append(uuid)

        kept, excluded = apply_post_enrich_gate(
            rows, find_property=find, get_property=get, delete_property=delete,
            forget_uuids=forgotten.extend, today=TODAY)
        return kept, excluded, deleted, forgotten

    def test_deletes_failures_and_forgets_them(self):
        kept, excluded, deleted, forgotten = self.run_gate(LIVE)
        self.assertEqual([r["Last Name"] for r in kept], ["Miller"])
        self.assertEqual(sorted(deleted), ["a", "f", "q", "v", "w"])
        self.assertEqual(sorted(forgotten), sorted(deleted))
        self.assertTrue(all(e["_gate_action"] == "deleted" for e in excluded))

    def test_never_deletes_a_worked_record(self):
        props = {"Old": prop("o", "5.00", phones=["9185550100"])}
        kept, excluded, deleted, forgotten = self.run_gate(props)
        self.assertEqual(kept, [])                 # still not traced
        self.assertEqual(deleted, [])
        self.assertEqual(forgotten, [])
        self.assertIn("already has phones", excluded[0]["_gate_action"])

    def test_unresolvable_record_is_kept(self):
        kept, excluded, deleted, _ = self.run_gate({"Ghost": None})
        self.assertEqual(len(kept), 1)
        self.assertEqual(excluded, [])

    def test_failed_delete_is_still_excluded_and_not_forgotten(self):
        kept, excluded, deleted, forgotten = self.run_gate(
            {"Quick": LIVE["Quick"]}, fail_delete={"q"})
        self.assertEqual(kept, [])
        self.assertTrue(excluded[0]["_gate_action"].startswith("delete FAILED"))
        self.assertEqual(forgotten, [])


class MlsFlag(unittest.TestCase):
    def run_one(self, p, nt="foreclosure"):
        p["owner"]["uuid"] = "own-1"
        tags, posts = [], []
        kept, excluded = apply_post_enrich_gate(
            [{"Property Street": "2441 S Norwood Ave", "Property City": "Tulsa"}],
            find_property=lambda *a: {"uuid": p["uuid"]}, get_property=lambda u: p,
            delete_property=lambda u: self.fail("must not delete"),
            today=TODAY, notice_type=nt,
            post_board=lambda o, m: posts.append((o, m)),
            add_tags=lambda u, t: tags.append((u, t)))
        return kept, excluded, tags, posts

    def test_listed_foreclosure_kept_tagged_and_posted(self):
        kept, excluded, tags, posts = self.run_one(prop("n", "88.50", None, mls="Listed"))
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0]["_mls_listed"])
        self.assertEqual(excluded, [])
        self.assertEqual(tags, [("n", [MLS_TAG])])
        self.assertEqual(MLS_TAG, "MLS Listed")
        self.assertEqual(posts[0][0], "own-1")
        self.assertIn("MLS LISTED", posts[0][1])

    def test_off_market_gets_nothing(self):
        kept, _, tags, posts = self.run_one(prop("n", "88.50", None))
        self.assertEqual(len(kept), 1)
        self.assertNotIn("_mls_listed", kept[0])
        self.assertEqual((tags, posts), ([], []))

    def test_listed_but_failing_is_still_deleted_untagged(self):
        kept, excluded, tags, posts = self.run_one(prop("n", "5.00", None, mls="Listed"))
        self.assertEqual(kept, [])
        self.assertEqual(len(excluded), 1)
        self.assertEqual((tags, posts), ([], []))

    def test_probate_is_never_flagged(self):
        _, _, tags, posts = self.run_one(prop("n", "88.50", None, mls="Listed"), "probate")
        self.assertEqual((tags, posts), ([], []))


if __name__ == "__main__":
    unittest.main()
