"""Next-morning check on the 6:30 PM KPI post (Jeff, 2026-11-03).

Calls keep going and replies keep coming after the post. The first time the PC is open the
next morning (from 6 AM), this re-pulls each posted day and compares it with the numbers the
post sent (reports/kpi_snapshot_*.json, written by pull_kpis.py). Same numbers = nothing happens.
Different numbers = a fresh post headed "Updated KPI's for <day>", and the sheet is refilled
from that day (the normal fill only reaches back one calendar day, so Friday night was lost).

Each snapshot is checked once. A run that can't read smrtPhone or the texts leaves the day
waiting and tries again on the next 15-minute pass, because a partial read would look like a change.

USAGE  py -3 kpi_recheck.py                     # what the Windows task runs
       py -3 kpi_recheck.py --dry-run           # shows the post, sends and saves nothing
"""
from __future__ import annotations

import argparse, datetime, json, os, subprocess, sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pull_kpis as k

HERE = Path(__file__).parent
TZ = ZoneInfo("America/Chicago")
OPEN_HOUR = 6        # a PC left on overnight shouldn't post at midnight
GIVE_UP_DAYS = 7     # e.g. the smrtPhone login expired and nobody renewed it


def waiting(folder: Path, now: datetime.datetime) -> list[tuple[Path, dict]]:
    """Snapshots still to check: the day is over and the post went out before it ended
    (a catch-up post made the next day already has the full day, so it has nothing to update)."""
    if now.hour < OPEN_HOUR:
        return []
    out = []
    for path in sorted(folder.glob(k.SNAPSHOT_GLOB)):
        try:
            snap = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        taken_day = str(snap.get("taken_at", ""))[:10]
        if not snap.get("checked") and taken_day <= snap["to"] < now.date().isoformat():
            out.append((path, snap))
    return out


def changes(old: dict, new: dict) -> list[str]:
    """'Dials 100 -> 124' for every number that moved."""
    return [f"{name} {old.get(name) if old.get(name) is not None else 0} -> {value if value is not None else 0}"
            for name, value in new.items() if (old.get(name) or 0) != (value or 0)]


def _day_label(day: str) -> str:
    d = datetime.date.fromisoformat(day)
    return f"{d.strftime('%a %b')} {d.day}"


def header_for(snap: dict, moved: list[str]) -> list[str]:
    same_day = snap["from"] == snap["to"]
    what = _day_label(snap["to"]) if same_day else f"the week {_day_label(snap['from'])} to {_day_label(snap['to'])}"
    posted = datetime.datetime.fromisoformat(snap["taken_at"]).strftime("%I:%M %p").lstrip("0")
    return [f"*Updated KPI's for {what}*", f"_Changed since the {posted} post: {', '.join(moved)}_", ""]


def read_window(snap: dict, token: str, bench: dict) -> tuple[dict, tuple]:
    res = k.pull(token, snap["from"], snap["to"], TZ, bench)
    k.add_smrtphone(res, snap["from"], snap["to"], bench)
    texts = k.text_stats(snap["from"], snap["to"])
    k.finalize(res, token, texts[0])
    return res, texts


def mark_checked(path: Path, snap: dict, result: str) -> None:
    snap.update(checked=True, result=result,
                checked_at=datetime.datetime.now(TZ).isoformat(timespec="seconds"))
    path.write_text(json.dumps(snap, indent=1), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Next-morning check on the KPI post")
    ap.add_argument("--dry-run", action="store_true", help="show the post; send and save nothing")
    ap.add_argument("--folder", type=Path, default=HERE / "reports", help="where the snapshots live")
    args = ap.parse_args()

    now = datetime.datetime.now(TZ)
    todo = waiting(args.folder, now)
    if not todo:
        return 0
    bench = k.load_benchmarks()
    token = k.get_token()
    webhook = "print" if args.dry_run else (
        os.environ.get("KPI_SLACK_WEBHOOK_URL", "").strip()
        or k._read_env_value(HERE.parent / "kpi-bot" / ".env", "KPI_SLACK_WEBHOOK_URL"))
    if not webhook:
        sys.exit("no KPI_SLACK_WEBHOOK_URL found in ../kpi-bot/.env")

    failed = False
    refill_from = None
    for path, snap in todo:
        label = f"{snap['from']} to {snap['to']}"
        age = (now.date() - datetime.date.fromisoformat(snap["to"])).days
        try:
            res, texts = read_window(snap, token, bench)
            missing = []
            if res.get("call_source", "").startswith("DataSift only"):
                missing.append(f"smrtPhone ({res['call_source']})")
            if texts[0] is None:
                missing.append(f"texts ({texts[1]})")
            if missing:
                raise RuntimeError("could not read " + ", ".join(missing))
            moved = changes(snap["numbers"], k.post_numbers(res, texts[0]))
            if moved:
                ledger = k.update_ledger(HERE / "reports" / "ledger.json", res) if not args.dry_run else None
                k.post_slack(res, webhook, bench, snap.get("note", ""), texts, ledger, header_for(snap, moved))
                k.log(f"{label}: updated post sent ({len(moved)} numbers changed)")
                if snap["from"] == snap["to"]:
                    refill_from = min(refill_from or snap["to"], snap["to"])
            else:
                k.log(f"{label}: same numbers as the post; nothing sent")
            if not args.dry_run:
                mark_checked(path, snap, f"{len(moved)} changed" if moved else "no change")
        except Exception as ex:
            k.log(f"{label}: check failed, will try again: {type(ex).__name__}: {ex}")
            failed = True
            if age >= GIVE_UP_DAYS and not args.dry_run:
                mark_checked(path, snap, f"gave up: {ex}")
    if refill_from and not args.dry_run:
        rc = subprocess.run([sys.executable, str(HERE / "fill_sheet.py"), "--since", refill_from], cwd=HERE).returncode
        k.log(f"sheet refilled from {refill_from}" if rc == 0 else f"sheet refill from {refill_from} failed (exit {rc})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
