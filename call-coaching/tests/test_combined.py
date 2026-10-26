"""Offline tests for the combined rubric (a cold call that rolls into lead
management on the same call) and for the user setting a call's type. No
network, no spend: everything is synthetic.

Run from SiftStack root:  py -3 call-coaching/tests/test_combined.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import grade  # noqa: E402
import transcribe  # noqa: E402
from cc_common import PIPELINES  # noqa: E402
from rubric_spec import RUBRICS, band_for, compute, compute_parts  # noqa: E402

TRANSCRIPT = """# Call CAtest
## Transcript

AGENT: Hey, is this Pat? I know this is a little random, but any plans for the place on Maple?
SELLER: Honestly we've thought about selling.
AGENT: Do you have about 10 minutes right now? I'd love to learn a little more.
SELLER: Sure, go ahead.
"""

PASS, FAILED = 0, 0


def check(name, cond):
    global PASS, FAILED
    if cond:
        PASS += 1
    else:
        FAILED += 1
        print("FAIL:", name)


def report(criteria: dict, **over) -> str:
    cats, total = compute("combined", criteria)
    js = {"call_id": "CAtest", "pipeline": "combined", "caller": "Diego", "call_type": "full",
          "duration_seconds": 600, "outcome": "booked offer call", "auto_fail": "PASS",
          "total": total, "band": band_for(total), "categories": cats,
          "parts": compute_parts("combined", criteria), "criteria": criteria,
          "na_reasons": {k: "never came up" for k, v in criteria.items() if v is None},
          "recording_url": "x"}
    js.update(over)
    rows = "\n".join(f"| {k} {n} | {'N/A' if criteria[k] is None else criteria[k]} | "
                     f"{'N/A: never came up' if criteria[k] is None else chr(34) + 'any plans for the place on Maple' + chr(34)} |"
                     for k, n in RUBRICS["combined"]["criteria"].items())
    return ("N/A criteria and weight redistribution: none\n\nCRITERION SCORES\n"
            "| Criterion | Score | Evidence |\n|---|---|---|\n" + rows +
            "\n\n```json\n" + json.dumps(js) + "\n```\n")


def check_text(txt: str) -> list[str]:
    with tempfile.TemporaryDirectory() as d:
        t = Path(d) / "t.md"
        t.write_text(TRANSCRIPT, encoding="utf-8")
        rp = Path(d) / "report_CAtest.md"
        rp.write_text(txt, encoding="utf-8")
        return grade.check_report(rp, {"call_id": "CAtest", "pipeline": "combined", "transcript_file": str(t)})


def main() -> int:
    spec = RUBRICS["combined"]
    check("combined is a gradeable type", "combined" in PIPELINES)
    check(f"weights sum to 100 ({sum(spec['weights'].values())})", sum(spec["weights"].values()) == 100)
    check("every criterion is in a weighted category",
          all(k.split(".")[0] in spec["weights"] for k in spec["criteria"]))
    check("rubric file exists", (Path(grade.RUBRIC_DIR) / spec["file"]).is_file())
    md = (Path(grade.RUBRIC_DIR) / spec["file"]).read_text(encoding="utf-8")
    missing = [k for k, n in spec["criteria"].items() if f"| {k} {n} |" not in md]
    check(f"every criterion appears in combined.md ({missing})", missing == [])

    # Reused criteria carry the same names as the source rubrics (catches drift)
    cold, lm = RUBRICS["cold_call"]["criteria"], RUBRICS["lead_management"]["criteria"]
    pairs = ([(f"1.{i}", cold[f"1.{i}"]) for i in range(1, 5)] +
             [(f"2.{i}", cold[f"3.{i}"]) for i in range(1, 6)] +
             [(f"{c + 2}.{i}", lm[f"{c}.{i}"]) for c in (2, 3, 4, 5) for i in range(1, 6) if f"{c}.{i}" in lm])
    drift = [k for k, name in pairs if spec["criteria"][k] != name]
    check(f"reused criteria match their source names ({drift})", drift == [])

    base = {k: 4 for k in spec["criteria"]}

    # 1. a correct combined report passes
    errs = check_text(report(dict(base)))
    check(f"valid combined report passes: {errs}", errs == [])

    # 2. wrong part score caught
    bad = dict(compute_parts("combined", base), opening=12.0)
    check("wrong part caught", any("part 'opening'" in e for e in check_text(report(dict(base), parts=bad))))

    # 3. Part A (cold-call) criterion may be N/A with a reason
    a = dict(base, **{"2.1": None})
    errs = check_text(report(a))
    check(f"Part A N/A allowed: {errs}", errs == [])

    # 4. the switch is never N/A
    s = dict(base, **{"3.2": None})
    check("switch N/A caught", any("3.2 is always scored" in e for e in check_text(report(s))))

    # 5. Part C: only 7.4 may be N/A
    c = dict(base, **{"4.1": None})
    check("Part C N/A outside 7.4 caught", any("4.1 cannot be N/A" in e for e in check_text(report(c))))
    c = dict(base, **{"7.4": None})
    errs = check_text(report(c))
    check(f"7.4 N/A allowed: {errs}", errs == [])

    # 6. no short version
    short = report(dict(base), call_type="short", total=None, band=None)
    check("combined short rejected", any("no short report" in e for e in check_text(short)))

    # 7. part math: opening only covers categories 1 and 2
    p = dict(base, **{k: 5 for k in spec["criteria"] if k[0] in "12"})
    parts = compute_parts("combined", p)
    check(f"opening part is 100 when cats 1-2 are all 5s ({parts})", parts["opening"] == 100.0)
    check("lead-management part unaffected", parts["lead_management"] == 80.0)

    # 8. the user's type: parsing
    ids, types, bad = transcribe.parse_specs(["CA1:combined", "CA2", "CA3:lm", "CA4:cold"])
    check("ids parsed", ids == ["CA1", "CA2", "CA3", "CA4"])
    check(f"types parsed {types}", types == {"CA1": "combined", "CA3": "lead_management", "CA4": "cold_call"})
    _, _, bad = transcribe.parse_specs(["CA1:combo", "CA2:banana"])
    check("unknown type flagged", bad == ["CA2:banana"])

    # 9. the user's type wins over the sorting step, and both are recorded
    tri = transcribe.apply_user_type({"pipeline": "cold_call", "report_type": "full", "switch_at": None},
                                     "combined")
    check(f"user type wins {tri}", tri["pipeline"] == "combined" and tri["type_source"] == "user"
          and tri["ai_pipeline"] == "cold_call" and tri["report_type"] == "full")
    tri = transcribe.apply_user_type({"pipeline": "combined", "report_type": "full",
                                      "switch_at": "02:14 Do you have about 10 minutes"}, None)
    check("detected type kept", tri["pipeline"] == "combined" and tri["type_source"] == "detected"
          and tri["switch_at"])
    tri = transcribe.apply_user_type({"pipeline": "cold_call", "report_type": "short"}, "combined")
    check("combined is never short", tri["report_type"] == "full")

    # 10. an unknown type refuses the whole run before anything is sent
    with tempfile.TemporaryDirectory() as d:
        cj = Path(d) / "calls.json"
        rec = Path(d) / "r.mp3"
        rec.write_bytes(b"x")
        cj.write_text(json.dumps([{"call_id": "CA1", "duration_seconds": 120, "recording_file": str(rec)}]),
                      encoding="utf-8")
        transcribe.CALLS_JSON, transcribe.TR_DIR = cj, Path(d) / "tr"
        sent = []

        def send(c):
            sent.append(c["call_id"])
            return "AGENT: hi", {"pipeline": "cold_call", "report_type": "full", "outcome": "x"}

        rc = transcribe.run(True, ["CA1:banana"], send=send)
        check(f"bad type refused, nothing sent (rc={rc}, sent={sent})", rc == 3 and sent == [])
        rc = transcribe.run(True, ["CA1:combined"], send=send)
        calls = json.loads(cj.read_text(encoding="utf-8"))
        check(f"named call stored as combined by the user ({calls[0].get('pipeline')})",
              rc == 0 and calls[0]["pipeline"] == "combined" and calls[0]["type_source"] == "user")
        head = Path(calls[0]["transcript_file"]).read_text(encoding="utf-8")
        check("transcript header says the user set it", "type set by: the user (sorting step said cold_call)" in head)

    print(f"{PASS} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
