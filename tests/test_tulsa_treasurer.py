"""Offline tests for tulsa_treasurer.py: Tulsa County Treasurer owner-name
search, parcel detail, and tax-payer history.

Data shapes here are REAL responses captured live 2026-09-15 against the
Fulton reference case already documented in CLAUDE.md (Johnnie Fulton Sr.,
PB-2026-587/588/589). Searching "FULTON, JOHNNIE" found FIVE real-estate
parcels, not the two originally documented from the Assessor-only
investigation:
  - 40800-02-13-05520 -> 4503 N Iroquois Ave (the known reference property,
    R40800021305520 on the Assessor)
  - 90328-03-28-15610 -> the known unplatted metes-and-bounds parcel, no
    street address (R90328032815610 on the Assessor)
  - 02575-02-24-00480, 06100-02-26-00100, 11225-02-24-03090 -> three parcels
    the original Assessor-only investigation never surfaced

A gibberish name control (ZZZQQQNOTAREAL / NOBODY) returned 0 rows, live.

Nothing here touches the network.
Run:  python tests/test_tulsa_treasurer.py -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import tulsa_treasurer as tt  # noqa: E402


# ── Real captured owner-name search rows (trimmed to what matters) ──────

_FULTON_ROWS = [
    ["2019", "38390",
     "<a href='https://oktaxrolls.com/owner_details/Tulsa?fromTaxYear=2019&toTaxYear=2025"
     "&info=owner_name&taxDataId=1889092&lastName=X&firstName=Y' target='_blank'>"
     "FULTON,  JOHNNIE L AND ADDIOUS</a>",
     "02575-02-24-00480", "Real Estate", "342.00", "<div>PAID</div>", "<span>receipt</span>"],
    ["2025", "887810",
     "<a href='https://oktaxrolls.com/owner_details/Tulsa?fromTaxYear=2019&toTaxYear=2025"
     "&info=owner_name&taxDataId=88560&lastName=X&firstName=Y' target='_blank'>"
     "FULTON,  JOHNNIE SR</a>",
     "40800-02-13-05520", "Real Estate", "555.00", "<div>PAID</div>", "<span>receipt</span>"],
    # A non-Real-Estate row (e.g. Special Assessment) — must be filtered out.
    ["2024", "8000462",
     "<a href='https://oktaxrolls.com/owner_details/Tulsa?...&taxDataId=634042' target='_blank'>"
     "FULTON,  JOHNNIE L</a>",
     "8000462", "Special Assessment", "415.15", "<div>PAID</div>", "<span>receipt</span>"],
]

_GIBBERISH_ROWS: list = []

_DETAIL_HTML = """
<html><body>
Owner Name and Address FULTON, JOHNNIE L AND ADDIOUS 1807 N MAIN TULSA OK 74106-4149
Taxroll Information Tax Year : 2019 Property ID : 02575-02-24-00480
Location : 1047  E APACHE ST N&nbsp;&nbsp; CITY OF TULSA School District : T1A Tulsa City
Mills : 137.02 Type of Tax : Real Estate Code : Tax ID : 38390
Legal Description and Other Information: LTS 1 2 &amp; 3  BLK 1&nbsp;<br />BANFIELD ADDN
History
Assessed Valuations Amount Land 3317 Improvements 3664 Net Assessed 6981
Tax Values Amount Base Tax 358.00 Penalty 0.00 Fees 0.00 Payments 358.00
Total Due 0.00
</body></html>
"""

_HISTORY_HTML = """
<html><body>
<table class="table table-tax-data">
<thead><tr><th>Year</th><th>Tax Id</th><th>Type</th><th>Owner Name</th>
<th>Base Tax</th><th>Fees</th><th>Penalty</th><th>Total Paid</th><th>Total Due</th></tr></thead>
<tbody class="thick">
<tr><td>2025</td><td>37910</td><td>Real Estate</td>
<td class="owner-name"><a href="...">FULTON, JOHNNIE L AND ADDIOUS</a></td>
<td>358.00</td><td>0.00</td><td>0.00</td><td>358.00</td><td>0.00</td></tr>
<tr><td>2019</td><td>38390</td><td>Real Estate</td>
<td class="owner-name"><a href="...">FULTON, JOHNNIE L AND ADDIOUS</a></td>
<td>342.00</td><td>0.00</td><td>0.00</td><td>342.00</td><td>0.00</td></tr>
</tbody>
</table>
</body></html>
"""

_HISTORY_UNRELATED_HTML = """
<html><body>
<table class="table table-tax-data">
<thead><tr><th>Year</th><th>Tax Id</th><th>Type</th><th>Owner Name</th>
<th>Base Tax</th><th>Fees</th><th>Penalty</th><th>Total Paid</th><th>Total Due</th></tr></thead>
<tbody class="thick">
<tr><td>2025</td><td>99999</td><td>Real Estate</td>
<td class="owner-name"><a href="...">SMITH, JANE Q</a></td>
<td>100.00</td><td>0.00</td><td>0.00</td><td>100.00</td><td>0.00</td></tr>
</tbody>
</table>
</body></html>
"""


def _mock_post(json_body):
    resp = mock.Mock()
    resp.raise_for_status = mock.Mock()
    resp.json.return_value = {"data": json_body}
    return resp


def _mock_get(html):
    resp = mock.Mock()
    resp.raise_for_status = mock.Mock()
    resp.text = html
    return resp


class SearchOwnerNameTests(unittest.TestCase):
    def test_real_name_returns_real_estate_rows_only(self):
        with mock.patch("requests.Session.post", return_value=_mock_post(_FULTON_ROWS)):
            rows = tt.search_owner_name("FULTON", "JOHNNIE")
        self.assertEqual(len(rows), 2)  # Special Assessment row filtered out
        parcel_ids = {r["parcel_id"] for r in rows}
        self.assertIn("02575-02-24-00480", parcel_ids)
        self.assertIn("40800-02-13-05520", parcel_ids)
        for r in rows:
            self.assertEqual(r["tax_type"], "Real Estate")
            self.assertTrue(r["tax_data_id"])
            self.assertIn("taxDataId=", r["detail_url"])

    def test_gibberish_control_returns_nothing(self):
        with mock.patch("requests.Session.post", return_value=_mock_post(_GIBBERISH_ROWS)):
            rows = tt.search_owner_name("ZZZQQQNOTAREAL", "NOBODY")
        self.assertEqual(rows, [])

    def test_include_non_real_estate_when_asked(self):
        with mock.patch("requests.Session.post", return_value=_mock_post(_FULTON_ROWS)):
            rows = tt.search_owner_name("FULTON", "JOHNNIE", real_estate_only=False)
        self.assertEqual(len(rows), 3)

    def test_requires_last_name_or_business_name(self):
        with self.assertRaises(ValueError):
            tt.search_owner_name()


class ParcelDetailTests(unittest.TestCase):
    def test_parses_owner_property_and_legal_description(self):
        with mock.patch("requests.get", return_value=_mock_get(_DETAIL_HTML)):
            detail = tt.get_parcel_detail("1889092")
        self.assertEqual(detail["owner_name"], "FULTON, JOHNNIE L AND ADDIOUS")
        self.assertEqual(detail["owner_street"], "1807 N Main")
        self.assertEqual(detail["owner_city"], "Tulsa")
        self.assertEqual(detail["owner_zip"], "74106")
        self.assertEqual(detail["property_street"], "1047 E Apache St N")
        self.assertEqual(detail["property_city"], "Tulsa")
        self.assertEqual(detail["parcel_id"], "02575-02-24-00480")
        self.assertIn("BANFIELD ADDN", detail["legal_description"])
        self.assertEqual(detail["total_due"], 0.0)
        self.assertEqual(detail["improvements_value"], 3664.0)

    def test_zero_improvements_parses_as_a_real_zero_not_missing(self):
        # 0.0 (a real, meaningful signal) must be distinguishable from None
        # (couldn't find the figure at all) - see improvements_value's own
        # docstring note on this.
        html = _DETAIL_HTML.replace("Improvements 3664", "Improvements 0")
        with mock.patch("requests.get", return_value=_mock_get(html)):
            detail = tt.get_parcel_detail("1889092")
        self.assertEqual(detail["improvements_value"], 0.0)
        self.assertIsNotNone(detail["improvements_value"])

    def test_missing_improvements_figure_is_none_not_zero(self):
        html = _DETAIL_HTML.replace(
            "Assessed Valuations Amount Land 3317 Improvements 3664 Net Assessed 6981", "")
        with mock.patch("requests.get", return_value=_mock_get(html)):
            detail = tt.get_parcel_detail("1889092")
        self.assertIsNone(detail["improvements_value"])

    def test_unplatted_parcel_has_no_street_but_keeps_legal_description(self):
        html = _DETAIL_HTML.replace(
            "Location : 1047  E APACHE ST N&nbsp;&nbsp; CITY OF TULSA",
            "Location : &nbsp;&nbsp; CITY OF TULSA",
        )
        with mock.patch("requests.get", return_value=_mock_get(html)):
            detail = tt.get_parcel_detail("2128702")
        self.assertEqual(detail["property_street"], "")
        self.assertEqual(detail["property_city"], "Tulsa")

    def test_network_error_returns_empty_shell_not_a_crash(self):
        with mock.patch("requests.get", side_effect=Exception("boom")):
            detail = tt.get_parcel_detail("999")
        self.assertEqual(detail["owner_name"], "")
        self.assertEqual(detail["total_due"], 0.0)


class OwnerHistoryTests(unittest.TestCase):
    def test_parses_real_table_newest_first(self):
        with mock.patch("requests.get", return_value=_mock_get(_HISTORY_HTML)):
            rows = tt.get_owner_history("3757")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["tax_year"], 2025)
        self.assertEqual(rows[0]["owner_name"], "FULTON, JOHNNIE L AND ADDIOUS")
        self.assertEqual(rows[1]["tax_year"], 2019)
        self.assertEqual(rows[0]["total_paid"], 358.0)

    def test_missing_table_returns_empty_list(self):
        with mock.patch("requests.get", return_value=_mock_get("<html><body>no table</body></html>")):
            rows = tt.get_owner_history("3757")
        self.assertEqual(rows, [])


class HistoryContainsNameTests(unittest.TestCase):
    """This is the heir-hit disambiguator: does a parcel's tax-payer history
    ever show the decedent's (or estate-connected) name, or is a hit under
    an heir's name their own unrelated pre-existing property?"""

    def test_finds_decedent_in_history(self):
        with mock.patch("requests.get", return_value=_mock_get(_HISTORY_HTML)):
            rows = tt.get_owner_history("3757")
        self.assertTrue(tt.history_contains_name(rows, "FULTON JOHNNIE"))
        self.assertTrue(tt.history_contains_name(rows, "JOHNNIE FULTON SR"))

    def test_unrelated_history_does_not_false_positive(self):
        with mock.patch("requests.get", return_value=_mock_get(_HISTORY_UNRELATED_HTML)):
            rows = tt.get_owner_history("99999")
        self.assertFalse(tt.history_contains_name(rows, "FULTON JOHNNIE"))
        self.assertTrue(tt.history_contains_name(rows, "SMITH JANE"))

    def test_empty_history_is_never_a_match(self):
        self.assertFalse(tt.history_contains_name([], "FULTON JOHNNIE"))

    def test_empty_target_name_is_never_a_match(self):
        with mock.patch("requests.get", return_value=_mock_get(_HISTORY_HTML)):
            rows = tt.get_owner_history("3757")
        self.assertFalse(tt.history_contains_name(rows, ""))


class AddressCorroboratesTests(unittest.TestCase):
    """The common-name collision guard. A name match alone is never enough —
    live-tested 2026-09-15: "COLEMAN, ELIZABETH" returned a real, direct hit
    with no way to tell from the name alone whether it's the same Elizabeth
    Coleman as any given probate case."""

    def test_matching_house_number_and_street_word_corroborates(self):
        detail = {"owner_street": "4006 W 45Th Pl S", "property_street": ""}
        self.assertTrue(tt.address_corroborates(detail, "4006 West 45th Place South"))

    def test_property_street_also_checked(self):
        detail = {"owner_street": "", "property_street": "1047 E Apache St N"}
        self.assertTrue(tt.address_corroborates(detail, "1047 East Apache Street"))

    def test_same_house_number_different_street_does_not_corroborate(self):
        # This is the actual shape of the Coleman collision risk: a bare name
        # hit with an address that has nothing to do with the known address.
        detail = {"owner_street": "1807 N Main", "property_street": "649 E Apache St N"}
        self.assertFalse(tt.address_corroborates(detail, "1807 S Elm Ave"))

    def test_different_house_number_does_not_corroborate(self):
        detail = {"owner_street": "4008 W 45Th Pl S", "property_street": ""}
        self.assertFalse(tt.address_corroborates(detail, "4006 West 45th Place South"))

    def test_no_known_address_never_corroborates(self):
        detail = {"owner_street": "4006 W 45Th Pl S", "property_street": ""}
        self.assertFalse(tt.address_corroborates(detail, ""))

    def test_known_address_without_house_number_never_corroborates(self):
        detail = {"owner_street": "4006 W 45Th Pl S", "property_street": ""}
        self.assertFalse(tt.address_corroborates(detail, "West 45th Place South"))

    def test_coleman_style_hit_with_no_known_address_is_not_confirmed(self):
        """Regression case: a real name-only hit must never silently pass as
        confirmed ownership when there's nothing to corroborate it against."""
        detail = {"owner_street": "1807 N Main", "property_street": "649 E Apache St N"}
        self.assertFalse(tt.address_corroborates(detail, ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
