"""Daily number-health post for the cold caller (Jeff, 2026-11-18).

The cold caller has no NumberVerifier login, so every day from 9 AM this posts
the carrier spam flags for every monitored number to the team channel and tags
him. NumberVerifier checks each number once a day, and a number's check for
today can land after 9 AM, so:

  - first run at/after 9 AM: post what we have. Numbers whose latest check is
    from an earlier day are marked with that day, and the post says an update
    will follow.
  - every hour after that until 7 PM: if any of those numbers now has today's check,
    post the full list again, headed "Updated", with the new ones marked.
  - nothing left waiting, or 7 PM: quiet until tomorrow.

Run by Windows task "Number health post" hourly, at logon, on unlock and on
wake; a run within ~an hour of the last check does nothing. State in
output/number_health_post_state.json. `--dry-run` prints instead of posting.

Config (.env): NUMBER_HEALTH_SLACK_WEBHOOK (team communication channel) and
NUMBER_HEALTH_TAG_SLACK_IDS (comma-separated Slack member ids to tag).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

if __package__ in (None, ""):  # allow `python src/sms_agent/number_health_post.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "sms_agent"

import requests

from . import config, number_health

log = logging.getLogger("number_health_post")

STATE = config.ROOT / "output" / "number_health_post_state.json"
START_HOUR = 9
STOP_HOUR = 19
MIN_GAP = 55 * 60         # seconds between checks once today's first post is out
ERROR_POST_HOUR = 11     # still unreadable by then and nothing posted: say so once


def _fmt(n: str) -> str:
    return f"({n[:3]}) {n[3:6]}-{n[6:]}"


def _day(d: str) -> str:
    return dt.date.fromisoformat(d).strftime("%b %d").replace(" 0", " ")


def _load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def render(data: dict, today: str, tags: list[str], updated: set[str] = frozenset()) -> str:
    """The Slack text for one snapshot. `updated` marks numbers new since the last post."""
    head = "Updated number health" if updated else "Number health"
    when = dt.date.fromisoformat(today).strftime("%a %b %d").replace(" 0", " ")
    mention = " ".join(f"<@{t}>" for t in tags)
    lines = [f"{mention} *{head}, {when}*".strip()]
    waiting = []
    rows = sorted(data.items(), key=lambda kv: (sum(1 for f in kv[1]["flags"] if f > 0), kv[0]))
    for number, row in rows:
        names = [c for c, f in zip(number_health.CARRIERS, row["flags"]) if f > 0]
        dot = (":large_green_circle:", ":large_yellow_circle:")[len(names)] if len(names) < 2 else ":red_circle:"
        what = f"flagged by {', '.join(names)}" if names else "clean"
        line = f"{dot} {_fmt(number)}  {what}"
        if row["day"] != today:
            waiting.append(number)
            line += f"  _(from {_day(row['day'])}, today's check not in yet)_"
        elif number in updated:
            line += "  _(new today)_"
        lines.append(line)
    lines.append("Green = no carrier flags it as spam. Yellow = 1 carrier does. Red = 2 or more do.")
    if waiting:
        n = len(waiting)
        lines.append(f"Today's check isn't in yet for {n} number{'s' if n > 1 else ''}. "
                     "I'll post again when it comes in.")
    return "\n".join(lines)


def post(text: str, dry: bool) -> bool:
    if dry:
        print(text)
        return True
    url = os.getenv("NUMBER_HEALTH_SLACK_WEBHOOK", "").strip()
    if not url:
        log.warning("NUMBER_HEALTH_SLACK_WEBHOOK not set; nothing posted")
        return False
    try:
        r = requests.post(url, json={"text": text}, timeout=15)
    except requests.RequestException as exc:
        log.warning("post failed: %s", exc)
        return False
    if r.status_code >= 300:
        log.warning("post failed: HTTP %s %s", r.status_code, r.text[:160])
        return False
    return True


def run(now: dt.datetime, dry: bool = False, fetch=number_health.fetch) -> str:
    """One check. Returns what it did (for the log and the tests)."""
    if now.hour < START_HOUR or now.hour >= STOP_HOUR:
        return "outside 9 AM - 7 PM"
    today = now.date().isoformat()
    state = _load()
    if state.get("date") != today:
        state = {"date": today, "shown": {}, "complete": False, "error_posted": False}
    if state["complete"]:
        return "done for today"
    # Hourly (Jeff, 2026-11-18). Logon/unlock/wake still fire the task, so
    # once today's first post is out, skip runs within ~an hour of the last check.
    last = state.get("last_check")
    if state["shown"] and last and (now - dt.datetime.fromisoformat(last)).total_seconds() < MIN_GAP:
        return "checked within the hour"
    state["last_check"] = now.isoformat(timespec="seconds")
    if not dry:
        _save(state)
    tags = [t.strip() for t in os.getenv("NUMBER_HEALTH_TAG_SLACK_IDS", "").split(",") if t.strip()]

    try:
        data = fetch()
        if not data:
            raise RuntimeError("NumberVerifier returned no monitored numbers")
    except Exception as exc:  # noqa: BLE001 - retried on the next run
        if not state["shown"] and not state["error_posted"] and now.hour >= ERROR_POST_HOUR:
            mention = " ".join(f"<@{t}>" for t in tags)
            if post(f"{mention} Couldn't read today's number health from NumberVerifier yet. "
                    "I'll keep trying and post it as soon as it works.".strip(), dry):
                state["error_posted"] = True
                if not dry:
                    _save(state)
        return f"unreadable: {str(exc)[:160]}"

    if not state["shown"]:
        updated: set[str] = set()
    else:
        updated = {n for n, row in data.items()
                   if row["day"] == today and state["shown"].get(n) != today}
        if not updated:
            return "no new checks yet"
    if not post(render(data, today, tags, updated), dry):
        return "post failed, will retry"
    state["shown"] = {n: row["day"] for n, row in data.items()}
    state["complete"] = all(row["day"] == today for row in data.values())
    if not dry:
        _save(state)
    return ("posted update" if updated else "posted") + (", all current" if state["complete"] else ", waiting")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="print the post, save nothing")
    ap.add_argument("--force", action="store_true", help="ignore the 9 AM - 7 PM window")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    now = dt.datetime.now()
    if a.force:
        now = now.replace(hour=max(START_HOUR, min(now.hour, STOP_HOUR - 1)))
    log.info("number health post: %s", run(now, dry=a.dry_run))


if __name__ == "__main__":
    main()
