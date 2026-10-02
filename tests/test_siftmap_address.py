"""Offline tests for siftmap_address (stubbed SiftMap, no network)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from siftmap_address import align_rows_to_siftmap, parse_street, pick_match  # noqa: E402


def _r(addr):
    return {"address": addr, "state": "OK"}


class StubClient:
    def __init__(self, table):
        self.table, self.queries = table, []

    def autocomplete(self, q, limit=10):
        self.queries.append(q)
        return [_r(a) for a in self.table.get(q, [])]


def test_parse_variants_share_a_core():
    for s in ["10007 South 87th East Avenue", "10007 S 87Th East Ave",
              "10007 S 87th Ave E", "10007 S 87 E Ave"]:
        p = parse_street(s)
        assert (p["number"], p["dir"], p["core"]) == ("10007", "S", "87th"), s
    assert parse_street("334 S Avenue G")["core"] == "g"
    assert parse_street("334 S G Ave E")["core"] == "g"
    assert parse_street("1035 E 57th Pl Unit 9")["unit"] == "9"
    assert parse_street("PO Box 12") is None


def test_petition_wording_becomes_siftmap_wording():
    got = pick_match("10007 South 87th East Avenue", "Tulsa", "74136",
                     [_r("10007 S 87th Ave E, Tulsa, OK 74133")])
    assert got == "10007 S 87th Ave E"


def test_siftmap_east_ave_wording_is_kept():
    got = pick_match("13406 S 93rd Ave E", "Bixby", "74008",
                     [_r("13406 S 93rd East Ave, Bixby, OK 74008")])
    assert got == "13406 S 93rd East Ave"


def test_rejects_wrong_number_direction_city_and_state():
    assert pick_match("10007 S 87th East Ave", "Tulsa", "", [_r("10009 S 87th Ave E, Tulsa, OK 74133")]) is None
    assert pick_match("10007 S 87th East Ave", "Tulsa", "", [_r("10007 N 87th Ave E, Tulsa, OK 74133")]) is None
    assert pick_match("10007 S 87th East Ave", "Tulsa", "74133", [_r("10007 S 87th Ave E, Owasso, OK 74055")]) is None
    assert pick_match("14845 Lakewood", "Bixby", "", [{"address": "14845 Mccabe Rd, Lakewood, WI 54138", "state": "WI"}]) is None


def test_units_must_match_and_ambiguity_is_refused():
    many = [_r("1035 E 57th Pl # B9, Tulsa, OK 74105"), _r("1035 E 57th Pl Unit 12, Tulsa, OK 74105")]
    assert pick_match("1035 E 57th Pl", "Tulsa", "", many) is None
    assert pick_match("1035 E 57th Pl Unit 12", "Tulsa", "", many) == "1035 E 57th Pl Unit 12"
    assert pick_match("6737 E 32nd Pl", "Tulsa", "", [_r("6737 E 32nd Pl # 7481, Tulsa, OK 74145")]) is None


def test_align_rows_rewrites_property_and_matching_mailing_only():
    client = StubClient({"10007 S 87th Tulsa": ["10007 S 87th Ave E, Tulsa, OK 74133"]})
    rows = [
        {"Property Street": "10007 South 87th East Avenue", "Property City": "Tulsa",
         "Property Zip": "74136", "Mailing Street": "10007 South 87th East Avenue"},
        {"Property Street": "14845 S Lakewood Ave", "Property City": "Bixby",
         "Mailing Street": "1 Elsewhere Rd"},
    ]
    out = align_rows_to_siftmap(rows, client=client)
    assert rows[0]["Property Street"] == "10007 S 87th Ave E"
    assert rows[0]["Mailing Street"] == "10007 S 87th Ave E"
    assert rows[1]["Property Street"] == "14845 S Lakewood Ave"
    assert rows[1]["Mailing Street"] == "1 Elsewhere Rd"
    assert out["changed"] == [("10007 South 87th East Avenue", "10007 S 87th Ave E")]
    assert out["unmatched"] == ["14845 S Lakewood Ave"]


def test_search_error_never_blocks():
    class Boom:
        def autocomplete(self, q, limit=10):
            raise RuntimeError("down")
    rows = [{"Property Street": "10007 S 87th East Ave", "Property City": "Tulsa"}]
    out = align_rows_to_siftmap(rows, client=Boom())
    assert rows[0]["Property Street"] == "10007 S 87th East Ave"
    assert out["unmatched"] == ["10007 S 87th East Ave"]


def test_street_type_breaks_a_same_number_tie():
    res = [_r("6310 E 4th St, Tulsa, OK 74112"), _r("6310 E 4th Pl, Tulsa, OK 74112"),
           _r("6310 E 4th Te S, Tulsa, OK 74112")]
    assert pick_match("6310 E 4Th St", "Tulsa", "", res) == "6310 E 4th St"
    assert pick_match("6310 E 4th Terrace S", "Tulsa", "", res) == "6310 E 4th Te S"
    assert pick_match("6310 E 4th", "Tulsa", "", res) is None
    res = [_r("8705 N 124th Ave E, Owasso, OK 74055"), _r("8705 N 124th East Pl, Owasso, OK 74055")]
    assert pick_match("8705 N 124Th Pl E", "Owasso", "", res) == "8705 N 124th East Pl"
    assert pick_match("8705 North 124th East Avenue", "Owasso", "", res) == "8705 N 124th Ave E"
    assert pick_match("334 S Avenue G", "Collinsville", "", [_r("334 S G Ave E, Collinsville, OK 74021"),
                                                             _r("334 S G St, Collinsville, OK 74021")]) == "334 S G Ave E"
