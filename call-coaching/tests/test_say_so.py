"""Offline tests for Jeff's rule (2026-10-26): no call is transcribed unless he
names it (the billed step), and every transcribed call is scored (free), while
a call with no transcript never is. No network, no spend: the paid step is a
stub that records what it was asked to send.

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

        # 5. the transcribed call is queued for scoring; the untranscribed ones are not
        grade.cmd_queue()
        q = json.loads(grade.QUEUE_JSON.read_text(encoding="utf-8"))
        check(f"only the transcribed call is queued (queue={[x['call_id'] for x in q]})",
              [x["call_id"] for x in q] == ["CA2"])

        # 6. a call with no transcript is never scored, even if it carries a rubric
        calls = json.loads(cj.read_text(encoding="utf-8"))
        ca3 = next(c for c in calls if c["call_id"] == "CA3")
        ca3.update({"pipeline": "cold_call", "transcript_file": str(d / "missing.md")})
        cj.write_text(json.dumps(calls), encoding="utf-8")
        grade.cmd_queue()
        q = json.loads(grade.QUEUE_JSON.read_text(encoding="utf-8"))
        check("untranscribed call not queued", "CA3" not in [x["call_id"] for x in q])
        rp = d / "report_CA3.md"
        rp.write_text("anything", encoding="utf-8")
        check("untranscribed report rejected", grade.check_report(rp, ca3) == [grade.NOT_APPROVED])
        check("report with no matching call rejected", grade.check_report(rp, None) != [])

        # 7. a transcript that exists, however it got there, is scored ("no matter what")
        (d / "missing.md").write_text("AGENT: hi", encoding="utf-8")
        grade.cmd_queue()
        q = json.loads(grade.QUEUE_JSON.read_text(encoding="utf-8"))
        check(f"every transcribed call queued ({[x['call_id'] for x in q]})",
              sorted(x["call_id"] for x in q) == ["CA2", "CA3"])

        # 8. a failed transcription leaves no transcript, so nothing to score
        def boom(c):
            raise RuntimeError("network down")
        transcribe.run(commit=True, ids=["CA1"], send=boom)
        calls = json.loads(cj.read_text(encoding="utf-8"))
        ca1 = next(c for c in calls if c["call_id"] == "CA1")
        check("failed transcription not scorable", not grade.is_transcribed(ca1))
        check("no approve command", not hasattr(grade, "cmd_approve"))

    print(f"{PASS} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
