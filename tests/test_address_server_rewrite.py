"""The server can drop a leading directional or re-space a street name on write.
Such a record must be found, and a changed direction must never match. Offline."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import datasift_api as d  # noqa: E402

N = d._norm_street


def rec(street, city="Tulsa", uuid="u"):
    return {"uuid": uuid, "address": {"street": street, "city": city}}


class RewriteRuleTests(unittest.TestCase):
    def test_the_shapes_seen_live(self):
        for sent, stored in [("100 S MAPLE ST W", "100 Maple St"),
                             ("200 W OAKPARK BV S", "200 W Oak Park Blvd"),
                             ("250 W SOUTHPARK BV S", "250 W South Park Blvd"),
                             ("300 N RIDGE DR W", "300 Ridge Dr"),
                             ("400 W KATY ST S", "400 Katy St"),
                             ("500 S 12 AV E", "500 S 12Th Ave")]:
            self.assertTrue(d._is_server_rewrite(N(sent), N(stored)), sent)

    def test_a_changed_direction_never_matches(self):
        for sent, stored in [("100 E MAPLE ST N", "100 W Maple St"),
                             ("100 E 56 ST N", "100 E 56Th St S"),
                             ("100 MAPLE ST", "100 N Maple St")]:
            self.assertFalse(d._is_server_rewrite(N(sent), N(stored)), sent)

    def test_other_house_or_street_never_matches(self):
        self.assertFalse(d._is_server_rewrite(N("100 S MAPLE ST"), N("101 Maple St")))
        self.assertFalse(d._is_server_rewrite(N("100 S MAPLE ST"), N("100 Elm St")))


class FindTests(unittest.TestCase):
    def _find(self, results, city="Tulsa"):
        def search(house, property_type="clean", limit=50):
            return results if property_type == "clean" else []
        with mock.patch.object(d, "search_by_address", side_effect=search):
            return d.find_property_by_address("100 S MAPLE ST W", city)

    def test_found_when_unique(self):
        self.assertEqual(self._find([rec("100 Maple St")])["uuid"], "u")

    def test_two_candidates_refuse_to_guess(self):
        self.assertIsNone(self._find([rec("100 Maple St", uuid="a"), rec("100 Maple Ave", uuid="b")]))

    def test_other_city_is_not_a_match(self):
        self.assertIsNone(self._find([rec("100 Maple St", city="Sand Springs")]))

    def test_changed_direction_is_not_a_match(self):
        self.assertIsNone(self._find([rec("100 N Maple St")]))


class ScanTests(unittest.TestCase):
    def test_rewritten_record_found_on_the_page(self):
        k = d.address_key("100 S MAPLE ST W", "Tulsa")
        r = rec("100 Maple St")
        self.assertEqual(d._scan_page_matches([r], {k}), [(k, r)])

    def test_two_records_on_the_page_are_left_alone(self):
        k = d.address_key("100 S MAPLE ST W", "Tulsa")
        self.assertEqual(d._scan_page_matches(
            [rec("100 Maple St", uuid="a"), rec("100 Maple Rd", uuid="b")], {k}), [])

    def test_two_awaited_for_one_record_are_left_alone(self):
        keys = {d.address_key("100 S MAPLE ST W", "Tulsa"), d.address_key("100 N MAPLE ST W", "Tulsa")}
        self.assertEqual(d._scan_page_matches([rec("100 Maple St")], keys), [])

    def test_exact_match_still_wins(self):
        k = d.address_key("100 S Maple St", "Tulsa")
        r = rec("100 S Maple St")
        self.assertEqual(d._scan_page_matches([r], {k}), [(k, r)])


if __name__ == "__main__":
    unittest.main()
