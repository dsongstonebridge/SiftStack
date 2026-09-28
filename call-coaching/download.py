"""download.py - download call recordings (free).

The recording URL DataSift logs (rec.smrtphone.io/RE....mp3) is public once
known, so no login is needed (verified 2026-09-28: 200 audio/mp3). When a call
has a SID but no recording URL, the smrtPhone API is tried as a backup:
  POST https://phone.smrt.studio/api/getRecordingUrl?call_sid=<SID>
  header X-Auth-smrtPhone: <SMRTPHONE_API_TOKEN>

USAGE (from SiftStack root):
  py -3 call-coaching/download.py
Output: call-coaching/output/recordings/<SID>.mp3
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from cc_common import CALLS_JSON, REC_DIR, env, is_excluded, load_json, log, save_json

SMRT_URL = "https://phone.smrt.studio/api/getRecordingUrl?call_sid={sid}"


def smrtphone_recording_url(sid: str) -> str | None:
    token = env("SMRTPHONE_API_TOKEN", required=False)
    if not token:
        return None
    req = urllib.request.Request(SMRT_URL.format(sid=sid), method="POST",
                                 headers={"X-Auth-smrtPhone": token, "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        log(f"  smrtPhone lookup for {sid} failed: HTTP {e.code}")
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = raw
    m = re.search(r"https?://[^\"'\s]+\.(?:mp3|wav)[^\"'\s]*", json.dumps(data) if not isinstance(data, str) else data)
    return m.group(0).replace("\\/", "/") if m else None


def fetch(url: str, dest) -> int:
    req = urllib.request.Request(url, headers={"user-agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    dest.write_bytes(data)
    return len(data)


def main() -> int:
    calls = load_json(CALLS_JSON, [])
    if not calls:
        log("No calls yet. Run pull_calls.py first.")
        return 1
    REC_DIR.mkdir(parents=True, exist_ok=True)
    got = skipped = failed = 0
    for c in calls:
        if is_excluded(c.get("caller")):
            continue
        dest = REC_DIR / f"{c['call_id']}.mp3"
        if dest.exists() and dest.stat().st_size > 1024:
            c["recording_file"] = str(dest)
            skipped += 1
            continue
        url = c.get("recording_url")
        if not url and c.get("status") == "answered":
            url = smrtphone_recording_url(c["call_id"])
            if url:
                c["recording_url"] = url
                c["recording_source"] = "smrtphone_api"
        if not url:
            c["recording_file"] = None
            continue
        try:
            n = fetch(url, dest)
            c["recording_file"] = str(dest)
            got += 1
            log(f"  {c['call_id']}  {n // 1024} KB")
        except Exception as e:  # noqa: BLE001
            failed += 1
            log(f"  FAILED {c['call_id']}: {e}")
    save_json(CALLS_JSON, calls)
    no_rec = sum(1 for c in calls if not c.get("recording_file"))
    log(f"Downloaded {got}, already had {skipped}, failed {failed}, no recording {no_rec}.")
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
