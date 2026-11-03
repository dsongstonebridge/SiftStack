"""Offline tests for the next-morning KPI check. No network, nothing posted."""
import datetime, json, sys, tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "kpi-engine"))
import kpi_recheck as r

TZ = ZoneInfo("America/Chicago")
fails = 0


def check(name, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + name)
    fails += not ok


def snap(folder, day_from, day_to, taken, **extra):
    d = {"from": day_from, "to": day_to, "taken_at": taken, "numbers": {"Dials": 100}, "checked": False, **extra}
    (folder / f"kpi_snapshot_{day_from}_{day_to}.json").write_text(json.dumps(d))


def at(day, hour):
    return datetime.datetime.fromisoformat(f"{day}T{hour:02d}:00:00").replace(tzinfo=TZ)


with tempfile.TemporaryDirectory() as t:
    f = Path(t)
    snap(f, "2026-11-02", "2026-11-02", "2026-11-02T18:33:00-06:00")
    days = lambda now: [s["to"] for _, s in r.waiting(f, now)]
    check("a 6:30 post waits until the next day", days(at("2026-11-02", 22)) == [])
    check("nothing before 6 AM", days(at("2026-11-03", 5)) == [])
    check("checked from 6 AM the next day", days(at("2026-11-03", 6)) == ["2026-11-02"])
    check("still waiting if the PC stays closed for days", days(at("2026-11-05", 9)) == ["2026-11-02"])

    path, s = r.waiting(f, at("2026-11-03", 7))[0]
    r.mark_checked(path, s, "no change")
    check("checked only once", days(at("2026-11-03", 8)) == [])

    snap(f, "2026-10-26", "2026-10-26", "2026-10-27T07:52:00-05:00")
    check("a catch-up post made the next day has nothing to update", days(at("2026-10-28", 9)) == [])

    snap(f, "2026-10-26", "2026-10-30", "2026-10-30T18:33:00-05:00", note="Weekly summary")
    check("a Friday weekly summary is checked too", days(at("2026-10-31", 9)) == ["2026-10-30"])

check("same numbers = no change", r.changes({"Dials": 100, "Talk time": "22m17s"}, {"Dials": 100, "Talk time": "22m17s"}) == [])
check("a moved number is named", r.changes({"Dials": 100, "Wrong": 12}, {"Dials": 124, "Wrong": 12}) == ["Dials 100 -> 124"])
check("a blank and a zero are the same", r.changes({"Leads": None}, {"Leads": 0}) == [])
check("a caller new since the post shows up", r.changes({}, {"Sam dials": 3}) == ["Sam dials 0 -> 3"])

h = r.header_for({"from": "2026-11-02", "to": "2026-11-02", "taken_at": "2026-11-02T18:33:00-06:00"}, ["Dials 100 -> 124"])
check("header says Updated KPI's for the day", h[0] == "*Updated KPI's for Mon Nov 2*")
check("header says what changed", h[1] == "_Changed since the 6:33 PM post: Dials 100 -> 124_")
h = r.header_for({"from": "2026-10-26", "to": "2026-10-30", "taken_at": "2026-10-30T18:33:00-05:00"}, ["x"])
check("weekly header names the week", h[0] == "*Updated KPI's for the week Mon Oct 26 to Fri Oct 30*")

print("FAILED" if fails else "all passed")
sys.exit(1 if fails else 0)
