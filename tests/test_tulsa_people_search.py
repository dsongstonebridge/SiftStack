"""Offline tests for tulsa_people_search.py: TruePeopleSearch results-card
and detail-page parsing, plus the corroboration orchestration.

Markup fixtures here are trimmed excerpts of REAL pages captured live
2026-09-16 (search "William Fulton" / "Tulsa, OK" - a genuine common-name
collision case: 5 distinct real "William Fulton" candidates). One of the
real previous addresses on the detail page fixture, 13113 E 17th Pl, Tulsa,
OK 74108, is used to prove address_corroborates() actually fires on a real
address shape, not a synthetic one.

Nothing here touches the network or 2Captcha.
Run:  python tests/test_tulsa_people_search.py -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import tulsa_people_search as tps  # noqa: E402


# ── Real captured results-listing markup (trimmed to 2 cards) ───────────

_RESULTS_HTML = """
<html><body>
<div class="card card-body shadow-form card-summary pt-3 mb-2" data-detail-link="/find/person/px906r0u69ul246086n90">
    <div class="row">
        <div class="col-md-8">
            <div class="content-header">
                William Fulton
            </div>
            <div>
                <span class="">Age </span>
                <span class="content-value">
60                </span><span> - </span><span class="content-value">Foyil, OK</span>
            </div>
                    <div class="mt-2">
                        <span class="content-label">Used to live in </span>
                        <span class="content-value">Tulsa, OK, Lubbock TX, Edmond OK, Plant C...  </span>
                    </div>
                <div class="">
                    <span class="content-label">Related to </span>
                    <span class="content-value">Jennifer Fulton, Misti Caldwell, Paula Koo...</span>
                </div>
        </div>
    </div>
</div>
<br>
<div class="card card-body shadow-form card-summary pt-3 mb-2" data-detail-link="/find/person/p8nllu0u0088n00r4nu2">
    <div class="row">
        <div class="col-md-8">
            <div class="content-header">
                William Fulton
            </div>
            <div>
                <span class="">Age </span>
                <span class="content-value">
93                </span><span> - </span><span class="content-value">Katy, TX</span>
            </div>
                    <div class="mt-2">
                        <span class="content-label">Used to live in </span>
                        <span class="content-value">Tulsa OK, Richmond TX</span>
                    </div>
                <div class="">
                    <span class="content-label">Related to </span>
                    <span class="content-value">Judith Fulton, Andrew Fulton, Helen Fulton...</span>
                </div>
        </div>
    </div>
</div>
<br>
<div class="card card-body shadow-form card-summary pt-3 d-none" data-detail-link="/find/person/p1144gfruekc4fkry39odjfgu">
    <div class="content-header">Hidden Duplicate</div>
</div>
</body></html>
"""


# ── Real captured detail-page markup (trimmed to the sections that matter) ──

_DETAIL_HTML = """
<html><body>
<div class="row pl-md-1">
    <div class="col-12 col-sm-11 pl-sm-1">
        <div class="row"><div class="col">
            <h2 class="h5">Also Seen As</h2>
            <div class="small pl-md-2 pb-2 mb-md-0">Includes all names used in any public records filings for William Fulton.</div>
        </div></div>
        <div class="row pl-sm-2"><div class="col"><div>
<span>William Guy Fulton</span>, <span>Billy Gay Fulton</span>                        </div></div></div>
    </div>
</div>
<div class="row pl-md-1">
    <div class="col-12 col-sm-11 pl-sm-1">
        <div class="row"><div class="col"><h2 class="h5">Current Address</h2></div></div>
        <div class="row pl-sm-2"><div class="col-12"><div>
                <a href="/find/address/po-box-145_foyil-ok-74031" class="dt-hd link-to-more olnk" data-link-to-more="address">PO Box 145<br>Foyil, OK 74031</a>
            <div class="mt-1 dt-ln">
<span class="dt-sb">Rogers County</span><br>                                <span class="dt-sb">
                                    (Mar 2009 - Sep 2026)
                                </span>
            </div>
        </div></div></div>
    </div>
</div>
<div class="row pl-md-1">
    <div class="col-12 col-sm-11 pl-sm-1">
        <div class="row"><div class="col"><h2 class="h5">Previous Addresses</h2></div></div>
        <div class="row pl-sm-2">
            <div class="col-12 col-md-6 mb-3"><div>
                    <a href="/find/address/13113-e-17th-pl_tulsa-ok-74108" class="dt-hd link-to-more olnk" data-link-to-more="address">13113 E 17th Pl<br>Tulsa, OK 74108</a>
                <div class="mt-1 dt-ln">
<span class="dt-sb">Tulsa County</span><br>                                        <span class="dt-sb">
                                            (May 2008 - Aug 2010)
                                        </span>
                </div>
            </div></div>
            <div class="col-12 col-md-6 mb-3"><div>
                    <a href="/find/address/9804-jordan-ave-a_lubbock-tx-79423" class="dt-hd link-to-more olnk" data-link-to-more="address">9804 Jordan Ave #A<br>Lubbock, TX 79423</a>
                <div class="mt-1 dt-ln">
<span class="dt-sb">Lubbock County</span><br>                                        <span class="dt-sb">
                                            (Jul 2011 - Oct 2011)
                                        </span>
                </div>
            </div></div>
        </div>
    </div>
</div>
<div id="toc-relatives"></div>
<a href="/find/person/p2l08ur2u92r6800llnn" class="link-to-more olnk dt-hd" data-link-to-more="relative"><span>Jennifer Fulton</span></a>
<a href="/find/person/p6lr66lu029rnu848u46" class="link-to-more olnk dt-hd" data-link-to-more="relative"><span>Misti Caldwell</span></a>
</body></html>
"""


class ParseSearchResultsTests(unittest.TestCase):
    def test_parses_visible_cards_only(self):
        results = tps._parse_search_results(_RESULTS_HTML)
        # The 3rd card is "d-none" (a hidden duplicate, seen on the real page) - excluded.
        self.assertEqual(len(results), 2)

    def test_common_name_collision_is_visible_as_distinct_candidates(self):
        """The whole point of this tool: a name search can return several
        real, distinct people. Never collapse or dedupe by name alone."""
        results = tps._parse_search_results(_RESULTS_HTML)
        ages = {r["age"] for r in results}
        self.assertEqual(ages, {"60", "93"})
        self.assertNotEqual(results[0]["detail_href"], results[1]["detail_href"])

    def test_card_fields(self):
        r = tps._parse_search_results(_RESULTS_HTML)[0]
        self.assertEqual(r["name"], "William Fulton")
        self.assertEqual(r["current_city"], "Foyil, OK")
        self.assertEqual(r["detail_href"], "/find/person/px906r0u69ul246086n90")
        # used_to_live_in / related_to are truncated on this page - present
        # but never asserted as complete.
        self.assertIn("Tulsa", r["used_to_live_in"])


class ParsePersonDetailTests(unittest.TestCase):
    def setUp(self):
        self.detail = tps._parse_person_detail(_DETAIL_HTML)

    def test_current_address(self):
        self.assertEqual(self.detail["current_address"], {
            "street": "PO Box 145", "city": "Foyil", "state": "OK", "zip": "74031",
            "county": "Rogers County", "date_range": "Mar 2009 - Sep 2026",
        })

    def test_previous_addresses_in_order(self):
        prev = self.detail["previous_addresses"]
        self.assertEqual(len(prev), 2)
        self.assertEqual(prev[0]["street"], "13113 E 17th Pl")
        self.assertEqual(prev[0]["city"], "Tulsa")
        self.assertEqual(prev[0]["zip"], "74108")
        self.assertEqual(prev[0]["county"], "Tulsa County")

    def test_relatives_are_untruncated(self):
        self.assertEqual(self.detail["relatives"], ["Jennifer Fulton", "Misti Caldwell"])

    def test_aliases_have_no_stray_punctuation(self):
        # A naive text-split once produced a lone "," entry - regression guard.
        self.assertEqual(self.detail["also_seen_as"],
                         ["William Guy Fulton", "Billy Gay Fulton"])
        self.assertNotIn(",", self.detail["also_seen_as"])


class OrchestrationTests(unittest.TestCase):
    """find_property_via_people_search() - corroboration is the hard gate,
    never a name-only accept, per the STOP-AND-ASK common-name rule already
    enforced for Assessor/Treasurer matches."""

    def test_corroborated_when_a_previous_address_matches(self):
        candidates = [{"name": "William Fulton", "age": "60", "current_city": "Foyil, OK",
                       "used_to_live_in": [], "related_to": [], "detail_href": "/find/person/a"}]
        detail = {
            "current_address": {"street": "PO Box 145", "city": "Foyil", "state": "OK",
                                "zip": "74031", "county": "Rogers County", "date_range": ""},
            "previous_addresses": [{"street": "13113 E 17th Pl", "city": "Tulsa", "state": "OK",
                                    "zip": "74108", "county": "Tulsa County", "date_range": ""}],
            "relatives": [], "also_seen_as": [],
        }
        with mock.patch("tulsa_people_search.search_person", return_value=candidates), \
             mock.patch("tulsa_people_search.get_person_detail", return_value=detail), \
             mock.patch("time.sleep"), \
             mock.patch("config.CAPTCHA_API_KEY", "fake-key"):
            report = tps.find_property_via_people_search(
                "William", "Fulton", ["13113 E 17th Pl"], max_candidates=5)
        self.assertIsNotNone(report["corroborated_hit"])
        self.assertTrue(report["candidates"][0]["corroborated"])

    def test_no_known_address_means_never_corroborated(self):
        """A common-name collision (5 real 'William Fulton's found live) must
        never be accepted on name alone - no known address means no hit,
        full stop, even with real candidates and real detail data."""
        candidates = [{"name": "William Fulton", "age": "60", "current_city": "Foyil, OK",
                       "used_to_live_in": [], "related_to": [], "detail_href": "/find/person/a"}]
        detail = {
            "current_address": {"street": "PO Box 145", "city": "Foyil", "state": "OK",
                                "zip": "74031", "county": "Rogers County", "date_range": ""},
            "previous_addresses": [], "relatives": [], "also_seen_as": [],
        }
        with mock.patch("tulsa_people_search.search_person", return_value=candidates), \
             mock.patch("tulsa_people_search.get_person_detail", return_value=detail), \
             mock.patch("time.sleep"), \
             mock.patch("config.CAPTCHA_API_KEY", "fake-key"):
            report = tps.find_property_via_people_search(
                "William", "Fulton", [], max_candidates=5)
        self.assertIsNone(report["corroborated_hit"])
        self.assertFalse(report["candidates"][0]["corroborated"])

    def test_unrelated_known_address_does_not_corroborate(self):
        candidates = [{"name": "William Fulton", "age": "93", "current_city": "Katy, TX",
                       "used_to_live_in": [], "related_to": [], "detail_href": "/find/person/b"}]
        detail = {
            "current_address": {"street": "1 Main St", "city": "Katy", "state": "TX",
                                "zip": "77450", "county": "Harris County", "date_range": ""},
            "previous_addresses": [], "relatives": [], "also_seen_as": [],
        }
        with mock.patch("tulsa_people_search.search_person", return_value=candidates), \
             mock.patch("tulsa_people_search.get_person_detail", return_value=detail), \
             mock.patch("time.sleep"), \
             mock.patch("config.CAPTCHA_API_KEY", "fake-key"):
            report = tps.find_property_via_people_search(
                "William", "Fulton", ["9999 Unrelated Ave"], max_candidates=5)
        self.assertIsNone(report["corroborated_hit"])

    def test_no_candidates_short_circuits(self):
        with mock.patch("tulsa_people_search.search_person", return_value=[]), \
             mock.patch("config.CAPTCHA_API_KEY", "fake-key"):
            report = tps.find_property_via_people_search("Nobody", "Real", ["1 Main St"])
        self.assertEqual(report, {"candidates": [], "corroborated_hit": None})

    def test_max_candidates_caps_detail_lookups(self):
        candidates = [
            {"name": f"Person {i}", "age": "50", "current_city": "Tulsa, OK",
             "used_to_live_in": [], "related_to": [], "detail_href": f"/find/person/{i}"}
            for i in range(10)
        ]
        detail_calls = []

        def _fake_detail(href, **kwargs):
            detail_calls.append(href)
            return {"current_address": None, "previous_addresses": [],
                    "relatives": [], "also_seen_as": []}

        with mock.patch("tulsa_people_search.search_person", return_value=candidates), \
             mock.patch("tulsa_people_search.get_person_detail", side_effect=_fake_detail), \
             mock.patch("time.sleep"), \
             mock.patch("config.CAPTCHA_API_KEY", "fake-key"):
            report = tps.find_property_via_people_search(
                "Person", "X", ["1 Main St"], max_candidates=3)
        self.assertEqual(len(detail_calls), 3)
        self.assertEqual(len(report["candidates"]), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
