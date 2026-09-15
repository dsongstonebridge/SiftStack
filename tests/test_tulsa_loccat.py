"""Offline tests for tulsa_loccat.py: LOCCAT's owner/parcel search (tight
matching) and document/grantor-grantee search (LOOSE matching, needs
verification).

The verification logic was WRONG on first pass: token-overlap (>= 2 shared
words) matched "L & S GROUP LLC" against "STONE INVESTMENT GROUP LLC" and
1,153 of 1,315 raw hits for that query, because "GROUP"/"LLC" are common
filler words in Oklahoma LLC names. Fixed to normalized substring
containment. Data shapes here are the REAL responses captured live
2026-09-15 (L&S Group LLC genuinely owns/owned 12 Tulsa County parcels,
confirming a fact already on record in CLAUDE.md from the original Johnson
incident).

Nothing here touches the network.
Run:  python tests/test_tulsa_loccat.py -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import tulsa_loccat as loccat  # noqa: E402


def _mock_post(json_body):
    resp = mock.Mock()
    resp.raise_for_status = mock.Mock()
    resp.json.return_value = json_body
    return resp


class SearchParcelTests(unittest.TestCase):
    def test_tight_owner_match(self):
        hit = {"type": "Feature", "properties": {
            "PARCELNB": "82285840109760", "OWNER": "KAISER, LARRY A",
            "PROP_ADD": "2001 N 22 ST E MOUNTAIN VIEW"}}
        with mock.patch("requests.post", return_value=_mock_post([hit])) as p:
            results = loccat.search_parcel(owner_name="KAISER,LARRY")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["properties"]["OWNER"], "KAISER, LARRY A")
        body = p.call_args.kwargs["json"]
        self.assertEqual(body["ownerName"], "KAISER,LARRY")

    def test_no_terms_makes_no_network_call(self):
        with mock.patch("requests.post") as p:
            results = loccat.search_parcel()
        p.assert_not_called()
        self.assertEqual(results, [])

    def test_parcel_number_uppercased_and_dashes_stripped(self):
        with mock.patch("requests.post", return_value=_mock_post([])) as p:
            loccat.search_parcel(parcel="r301-750-32811080")
        body = p.call_args.kwargs["json"]
        self.assertEqual(body["parcelAccountSub"], "R30175032811080")


class SearchAdvancedTests(unittest.TestCase):
    """The real L&S Group LLC flood: 1,315 raw hits for one company name."""

    _JOHNSON_HIT = {"properties": {
        "PARCELNB": "12145940944450", "DOCUMENTNB": "2014002906",
        "GRANTOR": "KAISER LARRY A,KAISER SHELLY A", "GRANTEE": "L&S GROUP",
        "RECORDINGDATE": "01/13/2014 20:07:04"}}
    _FALSE_HIT = {"properties": {
        "PARCELNB": "00825931900690", "DOCUMENTNB": "2025011901",
        "GRANTOR": "STONE INVESTMENT GROUP LLC", "GRANTEE": "MOORE JAMI S,MOORE ANDREW",
        "RECORDINGDATE": "02/12/2025 20:57:56"}}
    _LLC_SUFFIX_DROPPED_HIT = {"properties": {
        "PARCELNB": "07415940729390", "DOCUMENTNB": "2020018651",
        "GRANTOR": "MARQUETTE BRENTON,MARQUETTE ELIZABETH ANN", "GRANTEE": "L&S GROUP"}}

    def test_genuine_match_survives_verification(self):
        with mock.patch("requests.post",
                        return_value=_mock_post([self._JOHNSON_HIT, self._FALSE_HIT])):
            results = loccat.search_advanced(doc_grantor_grantee="L & S GROUP LLC")
        verified = loccat.verified_hits(results)
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]["properties"]["DOCUMENTNB"], "2014002906")

    def test_common_word_flood_is_rejected(self):
        # STONE INVESTMENT GROUP LLC shares "GROUP"/"LLC" with the query but
        # is not the same entity - this is the bug that was fixed.
        with mock.patch("requests.post", return_value=_mock_post([self._FALSE_HIT])):
            results = loccat.search_advanced(doc_grantor_grantee="L & S GROUP LLC")
        self.assertFalse(results[0]["verified"])
        self.assertEqual(loccat.verified_hits(results), [])

    def test_llc_suffix_dropped_in_one_document_still_matches(self):
        # A real variant: the grantee was recorded as "L&S GROUP" (no "LLC")
        # in this particular document. Still the same entity.
        with mock.patch("requests.post",
                        return_value=_mock_post([self._LLC_SUFFIX_DROPPED_HIT])):
            results = loccat.search_advanced(doc_grantor_grantee="L & S GROUP LLC")
        self.assertTrue(results[0]["verified"])

    def test_patricia_pruitt_false_hit_rejected(self):
        # The video's exact gotcha: LOCCAT surfaces a "hit" whose real name
        # (here, other unrelated Pruitts) doesn't actually match the query.
        unrelated = {"properties": {"GRANTOR": "PRUITT MARILYN KAY",
                                    "GRANTEE": "CAGLE JAMES S,CAGLE JANET M"}}
        with mock.patch("requests.post", return_value=_mock_post([unrelated])):
            results = loccat.search_advanced(doc_grantor_grantee="Patricia Pruitt")
        self.assertFalse(results[0]["verified"])

    def test_no_terms_makes_no_network_call(self):
        with mock.patch("requests.post") as p:
            results = loccat.search_advanced()
        p.assert_not_called()
        self.assertEqual(results, [])


class NamesOverlapTests(unittest.TestCase):
    def test_ampersand_variant_matches(self):
        self.assertTrue(loccat._names_overlap("L & S GROUP LLC", "L&S GROUP LLC"))

    def test_common_business_words_alone_do_not_match(self):
        self.assertFalse(loccat._names_overlap("L & S GROUP LLC", "STONE INVESTMENT GROUP LLC"))
        self.assertFalse(loccat._names_overlap("L & S GROUP LLC", "ELLISON INVESTMENT GROUP LLC"))

    def test_shorter_candidate_prefix_still_matches(self):
        self.assertTrue(loccat._names_overlap("L & S GROUP LLC", "L & S GROUP"))


if __name__ == "__main__":
    unittest.main()
