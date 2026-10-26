"""Offline tests for Jeff's rule (2026-10-26): no call is transcribed or scored
unless he names it. No network, no spend: the paid step is a stub that records
what it was asked to send.

Run from SiftStack root:  py -3 call-coaching/tests/test_say_so.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import grade  # noqa: E402
import transcribe  # noqa: E402

PASS, FAILED = 0, 0


def check(name, cond):
    global PASS, FAILED
    if cond:
        PASS += 1
    else:
        FAILED += 1
        print(f"FAIL: {name}")


def setup(d: Path) -> Path:
    rec = d / "rec.mp3"
    rec.write_bytes(b"fake")
    calls = [{"call_id": cid, "caller": "Diego", "duration_seconds": 120,
              "recording_file": str(rec)} for cid in ("CA1", "CA2", "CA3")]
    cj = d / "calls.json"
    cj.write_text(json.dumps(calls), encoding="utf-8")
    transcribe.CALLS_JSON = cj
    transcribe.TR_DIR = d / "transcripts"
    grade.CALLS_JSON = cj
    grade.QUEUE_JSON = d / "queue.json"
    grade.REPORT_DIR = d / "reports"
    return cj


def stub():
    sent = []

    def send(c):
        sent.append(c["call_id"])
        return ("AGENT: hi\nSELLER: hi\n", {"pipeline": "cold_call", "report_type": "full",
                                             "outcome": "test", "summary": "test"})
    return sent, send


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        cj = setup(d)

        # 1. --commit with no calls named: refused, nothing sent
        sent, send = stub()
        rc = transcribe.run(commit=True, ids=[], send=send)
        check(f"commit without ids refused (rc={rc})", rc == 3 and sent == [])

        # 2. --commit naming a call that does not exist: refused, nothing sent at all
        sent, send = stub()
        rc = transcribe.run(commit=True, ids=["CA1", "CAnope"], send=send)
        check(f"unknown id refuses the whole run (rc={rc}, sent={sent})", rc == 3 and sent == [])

        # 3. dry run (no --commit), with or without ids: nothing sent
        sent, send = stub()
        transcribe.run(commit=False, ids=[], send=send)
        transcribe.run(commit=False, ids=["CA1"], send=send)
        check(f"dry run sends nothing (sent={sent})", sent == [])

        # 4. --commit naming CA2 only: only CA2 is sent
        sent, send = stub()
        rc = transcribe.run(commit=True, ids=["CA2"], send=send)
        check(f"only the named call is sent (rc={rc}, sent={sent})", rc == 0 and sent == ["CA2"])

        # 5. transcribing does NOT approve scoring: CA2 is not in the grading queue
        grade.cmd_queue()
        q = json.loads(grade.QUEUE_JSON.read_text(encoding="utf-8"))
        check(f"transcribed but unapproved call not queued (queue={q})", q == [])

        # 6. a report for an unapproved call is rejected and never marked graded
        rp = d / "report_CA2.md"
        rp.write_text("anything", encoding="utf-8")
        calls = json.loads(cj.read_text(encoding="utf-8"))
        ca2 = next(c for c in calls if c["call_id"] == "CA2")
        check("unapproved report rejected", grade.check_report(rp, ca2) == [grade.NOT_APPROVED])
        check("report with no matching call rejected", grade.check_report(rp, None) != [])

        # 7. approve with nothing named / an unknown id: refused, nothing approved
        check("approve with no ids refused", grade.cmd_approve([]) == 3)
        check("approve unknown id refused", grade.cmd_approve(["CA2", "CAnope"]) == 3)
        calls = json.loads(cj.read_text(encoding="utf-8"))
        check("nothing approved after refusals", not any(c.get("score_approved") for c in calls))

        # 8. approving CA2 puts only CA2 in the queue
        check("approve CA2", grade.cmd_approve(["CA2"]) == 0)
        grade.cmd_queue()
        q = json.loads(grade.QUEUE_JSON.read_text(encoding="utf-8"))
        check(f"only the approved call is queued (queue={[x['call_id'] for x in q]})",
              [x["call_id"] for x in q] == ["CA2"])
        calls = json.loads(cj.read_text(encoding="utf-8"))
        others = [c["call_id"] for c in calls if c.get("score_approved") and c["call_id"] != "CA2"]
        check(f"no other call approved ({others})", others == [])

    print(f"{PASS} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
