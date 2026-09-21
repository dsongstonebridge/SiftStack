"""Owner first names: middle initials and punctuation make DataSift type the
owner (and its property) "incomplete". Live: Joel Cape, first name "Joel E.",
2026-09-21 - editing it to "Joel" made the record clean.

Nothing here touches the network.  Run: python -m unittest tests.test_owner_name_clean -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import datasift_formatter as f  # noqa: E402


class CleanOwnerFirstNameTests(unittest.TestCase):
    def test_the_real_case(self):
        self.assertEqual(f.clean_owner_first_name("Joel E."), "Joel")

    def test_initials_with_and_without_periods(self):
        self.assertEqual(f.clean_owner_first_name("Larry A"), "Larry")
        self.assertEqual(f.clean_owner_first_name("R. Wayne"), "Wayne")
        self.assertEqual(f.clean_owner_first_name("J. R."), "J. R")

    def test_real_double_given_names_are_kept(self):
        self.assertEqual(f.clean_owner_first_name("Mary Ann"), "Mary Ann")
        self.assertEqual(f.clean_owner_first_name("Wendy Jean"), "Wendy Jean")

    def test_plain_names_untouched(self):
        self.assertEqual(f.clean_owner_first_name("Dallas"), "Dallas")
        self.assertEqual(f.clean_owner_first_name("Jean-Luc"), "Jean-Luc")

    def test_trailing_punctuation_stripped(self):
        self.assertEqual(f.clean_owner_first_name("NICHOLAS,"), "NICHOLAS")

    def test_blank_stays_blank(self):
        self.assertEqual(f.clean_owner_first_name(""), "")
        self.assertEqual(f.clean_owner_first_name(None), "")

    def test_payload_uses_the_cleaned_name(self):
        payload = f.build_api_payload({
            "Property Street Address": "17021 N Memorial Dr E", "Property City": "Collinsville",
            "Property State": "OK", "Property ZIP Code": "74021",
            "Owner First Name": "Joel E.", "Owner Last Name": "Cape",
            "Mailing Street Address": "1752 N Barrington Dr", "Mailing City": "Fayetteville",
            "Mailing State": "AR", "Mailing ZIP Code": "72701",
        })
        self.assertEqual(payload["owner"]["first_name"], "Joel")
        self.assertEqual(payload["owner"]["last_name"], "Cape")


if __name__ == "__main__":
    unittest.main(verbosity=2)
