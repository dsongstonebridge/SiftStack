"""Slack alert when a DataSift property is assigned to someone we watch.

Jeff, 2026-11-24: when a property's assignee changes to Diego, tag Diego in the
private assignments channel (webhook ASSIGNEE_SLACK_WEBHOOK in .env).

How: every run reads the records currently assigned to each watched person
(a filtered records search, read-only), compares with the set saved on the last
run, and posts the NEW ones. The first run for a person only saves the set and
posts nothing, so the existing backlog never floods the channel. A record taken
off someone and given back later alerts again (that is a new assignment).

A read that fails or comes back short never updates the saved set, so a bad
read can never turn into a burst of false "new" alerts on the next run.

DataSift's own webhook (sequence action) was not used: its body cannot be
shaped into a Slack message and it needs a public receiver this PC does not
have. Cost of this route: up to ~15 min delay, and nothing runs while the PC
sleeps (the next run catches up, since it compares sets).

Run:  python src/assignee_alerts.py            (check + post)
      python src/assignee_alerts.py --dry-run  (check, print, post nothing, save nothing)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import datasift_api as d  # noqa: E402

STATE = ROOT / "output" / "assignee_alerts_state.json"
LOG = ROOT / "logs" / "assignee_alerts.log"

# name -> (DataSift user uuid, Slack member id). Add Jeff / Eric here later.
WATCH = {
    "Diego": ("49dc300f-a1e1-422d-a7f6-c78991a40905", "U0BDZ4DS1B4"),
}
PAGE = 100
MAX_LISTED = 15


def _env(key: str) -> str:
    v = os.environ.get(key, "")
    if v:
        return v
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def assigned_records(user_uuid: str) -> dict[str, dict]:
    """Every record assigned to user_uuid, keyed by property uuid.
    Raises if any page fails or the total does not match the server's count."""
    out: dict[str, dict] = {}
    for ptype in ("clean", "incomplete"):
        offset, total, seen = 0, None, 0
        while True:
            h = d._headers(has_body=True, auth="api_key")
            h["x-http-method-override"] = "GET"
            d._pace()
            r = requests.post(
                f"{d.CORE_BASE}/api/internal/property/", headers=h, timeout=45,
                json={"limit": PAGE, "offset": offset, "ordering": "-updated",
                      "query": {"must": {"property_type": ptype, "assigned_to": user_uuid}}})
            if not r.ok:
                raise RuntimeError(f"search {ptype} offset {offset} -> {r.status_code}: {r.text[:200]}")
            j = r.json() or {}
            total = j.get("count") if total is None else total
            items = d._page_items(j)
            for it in items:
                out[it["uuid"]] = it
            seen += len(items)
            offset += PAGE
            if not items or offset >= (total or 0):
                break
        if total is None or seen < total:
            raise RuntimeError(f"search {ptype}: read {seen} of {total}")
    return out


def _fix_street(s: str) -> str:
    # DataSift title-cases ordinals: "76Th" -> "76th"
    return re.sub(r"(\d)(St|Nd|Rd|Th)\b", lambda m: m.group(1) + m.group(2).lower(), s or "")


def describe(rec: dict) -> str:
    a = rec.get("address") or {}
    street = _fix_street(a.get("street") or "(no street)")
    place = ", ".join(x for x in (street, a.get("city")) if x)
    o = rec.get("owner") or {}
    owner = o.get("company") or " ".join(x for x in (o.get("first_name"), o.get("last_name")) if x)
    link = f"https://app.reisift.io/records/properties/{rec['uuid']}/details"
    line = f"<{link}|{place}>"
    return f"{line} ({owner})" if owner else line


def message(slack_id: str, recs: list[dict]) -> str:
    n = len(recs)
    head = (f"<@{slack_id}> A property was just assigned to you:" if n == 1
            else f"<@{slack_id}> {n} properties were just assigned to you:")
    lines = [f"- {describe(r)}" for r in recs[:MAX_LISTED]]
    if n > MAX_LISTED:
        lines.append(f"...and {n - MAX_LISTED} more.")
    return head + "\n" + "\n".join(lines)


def post(webhook: str, text: str) -> None:
    r = requests.post(webhook, json={"text": text}, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Slack -> {r.status_code}: {r.text[:200]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true",
                    help="post one real alert, marked TEST, for each person's most recently "
                         "updated record; changes nothing in DataSift or the saved state")
    args = ap.parse_args()

    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=LOG, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("assignee_alerts")

    webhook = _env("ASSIGNEE_SLACK_WEBHOOK")
    if not webhook and not args.dry_run:
        log.error("ASSIGNEE_SLACK_WEBHOOK not set")
        print("ASSIGNEE_SLACK_WEBHOOK not set")
        return 2

    state = json.loads(STATE.read_text(encoding="utf-8-sig")) if STATE.exists() else {}
    rc = 0
    for name, (uuid, slack_id) in WATCH.items():
        try:
            current = assigned_records(uuid)
        except Exception as e:  # a failed read never updates the saved set
            log.error("%s: read failed, nothing changed: %s", name, e)
            print(f"{name}: read failed: {e}")
            rc = 1
            continue
        if args.test:
            sample = list(current.values())[:1]  # newest by -updated
            if sample:
                post(webhook, "TEST - " + message(slack_id, sample))
                print(f"{name}: test alert posted for {sample[0]['uuid']}")
            continue
        prev = state.get(uuid)
        if prev is None:
            print(f"{name}: first run, saved {len(current)} records, posted nothing")
            log.info("%s: first run, seeded %d", name, len(current))
            if not args.dry_run:
                state[uuid] = sorted(current)
            continue
        new = [current[u] for u in current if u not in set(prev)]
        print(f"{name}: {len(current)} assigned, {len(new)} new")
        log.info("%s: %d assigned, %d new", name, len(current), len(new))
        if new:
            text = message(slack_id, new)
            if args.dry_run:
                print(text)
            else:
                try:
                    post(webhook, text)
                    log.info("%s: posted %d new: %s", name, len(new), [r["uuid"] for r in new])
                except Exception as e:  # not saved -> retried next run
                    log.error("%s: Slack post failed, will retry: %s", name, e)
                    print(f"{name}: Slack post failed: {e}")
                    rc = 1
                    continue
        if not args.dry_run:
            state[uuid] = sorted(current)
    if not args.dry_run:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1))
    return rc


if __name__ == "__main__":
    sys.exit(main())
