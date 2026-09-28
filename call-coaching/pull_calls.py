"""pull_calls.py - list smrtPhone calls from the DataSift activity log.

smrtPhone is integrated with DataSift, so every call lands on the property's
activity log as `owner.call.*` events (verified live 2026-09-28):
  - owner.call.made      the dial: call SID, caller, numbers. No recording.
  - owner.call.answered  same SID, plus duration and the recording URL.
The two are paired by the call SID (`payload.call.external_id`).

DataSift has no account-wide call list, so this reads each record's activity
log. It is read-only and free. The API rate-limits quickly, so it paces itself
and retries; about 1-2 minutes per 200 records.

USAGE (from SiftStack root):
  py -3 call-coaching/pull_calls.py                    # calls since 2026-09-28
  py -3 call-coaching/pull_calls.py --since 2026-09-01
  py -3 call-coaching/pull_calls.py --days 7           # last 7 days

Output: call-coaching/output/calls.json (merged with earlier runs, keyed by SID).
"""
from __future__ import annotations

import argparse
import time
from datetime import date, timedelta

from cc_common import (CALLING_START, CALLS_JSON, ds_request, fmt_local, load_json, log,
                       save_json, utc_to_local)

RECORD_URL = "https://app.reisift.io/records/properties/{uuid}/details"
CALL_EVENTS = {"owner.call.made", "owner.call.answered", "owner.call.noanswer",
               "owner.call.received", "owner.call.missed"}


def all_properties() -> list[dict]:
    props = []
    for ptype in ("clean", "incomplete"):
        offset = 0
        while True:
            body = {"limit": 200, "offset": offset, "ordering": "-list_count",
                    "query": {"must": {"property_type": ptype}}}
            r = ds_request("/api/internal/property/", method="POST", body=body, override="GET")
            rows = r.get("results") or r.get("data") or []
            props += rows
            offset += 200
            if offset >= r.get("count", 0) or not rows:
                break
    return props


def property_logs(uuid: str) -> list[dict]:
    events, offset = [], 0
    while True:
        r = ds_request(f"/api/internal/property/{uuid}/logs/", params={"limit": 250, "offset": offset})
        page = r.get("results") or r.get("data") or []
        events += page
        offset += 250
        if len(page) < 250:
            return events


def _addr(p: dict) -> str:
    a = p.get("address") or {}
    if isinstance(a, dict):
        parts = [a.get("street"), a.get("city"), a.get("state")]
        return ", ".join(x for x in parts if x)
    return str(a)


def caller_of(call: dict) -> str:
    u = call.get("user") or {}
    name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
    return name or (call.get("external_user") or {}).get("name") or "Unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description="List smrtPhone calls from DataSift activity")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD (default 2026-09-28)")
    ap.add_argument("--days", type=int, help="only the last N days")
    args = ap.parse_args()
    since = args.since or CALLING_START
    if args.days:
        since = (date.today() - timedelta(days=args.days)).isoformat()

    t0 = time.time()
    props = all_properties()
    log(f"Scanning activity on {len(props)} DataSift records for calls since {since} ...")

    existing = {c["call_id"]: c for c in load_json(CALLS_JSON, [])}
    found = {}
    for i, p in enumerate(props, 1):
        uuid = p.get("uuid")
        try:
            events = property_logs(uuid)
        except RuntimeError as e:
            log(f"  WARNING: could not read activity for record {uuid}: {e}")
            continue
        for ev in events:
            if ev.get("event_type") not in CALL_EVENTS:
                continue
            call = (ev.get("payload") or {}).get("call") or {}
            sid = call.get("external_id") or call.get("uuid")
            created = call.get("created") or ev.get("timestamp")
            if not sid or (created or "")[:10] < since:
                continue
            row = found.setdefault(sid, {
                "call_id": sid,
                "property_uuid": uuid,
                "record_url": RECORD_URL.format(uuid=uuid),
                "address": _addr(p),
                "owner_name": " ".join(filter(None, [
                    ((ev.get("payload") or {}).get("extra_info") or {}).get("owner", {}).get("first_name"),
                    ((ev.get("payload") or {}).get("extra_info") or {}).get("owner", {}).get("last_name")])),
                "caller": caller_of(call),
                "direction": call.get("direction"),
                "created_utc": created,
                "created_local": fmt_local(utc_to_local(created)),
                "phone": call.get("phone") or call.get("destination_number"),
                "duration_seconds": None,
                "status": None,
                "recording_url": None,
                "events": [],
            })
            row["events"].append(ev.get("event_type"))
            if call.get("duration") is not None:
                row["duration_seconds"] = max(row["duration_seconds"] or 0, int(call["duration"]))
            if call.get("recording"):
                row["recording_url"] = call["recording"]
            status = (call.get("extra") or {}).get("status")
            if status:
                row["status"] = status
        if i % 25 == 0:
            log(f"  {i}/{len(props)} records scanned, {len(found)} calls so far")
        time.sleep(0.35)

    for sid, row in found.items():
        row["events"] = sorted(set(row["events"]))
        if not row["status"]:
            row["status"] = "answered" if "owner.call.answered" in row["events"] else "not answered"
        prior = existing.get(sid, {})
        # Keep pipeline progress (download/transcribe/grade flags) from earlier runs.
        existing[sid] = {**prior, **row}

    calls = sorted(existing.values(), key=lambda c: c.get("created_utc") or "")
    save_json(CALLS_JSON, calls)
    with_rec = sum(1 for c in found.values() if c.get("recording_url"))
    log(f"Found {len(found)} calls since {since} ({with_rec} with a recording) in {time.time() - t0:.0f}s.")
    by_caller = {}
    for c in found.values():
        by_caller[c["caller"]] = by_caller.get(c["caller"], 0) + 1
    for name, n in sorted(by_caller.items()):
        log(f"  {name}: {n}")
    log(f"-> {CALLS_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
