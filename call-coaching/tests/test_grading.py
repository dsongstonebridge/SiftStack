"""Offline tests for grade.py check, rubric math and tie tiers. No network, no spend.

Run from SiftStack root:  py -3 call-coaching/tests/test_grading.py
Everything here is synthetic: a made-up call, never a real seller.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import grade  # noqa: E402
from export import tiers  # noqa: E402
from rubric_spec import RUBRICS, compute  # noqa: E402

TRANSCRIPT = """# Call CAtest
## Transcript

AGENT: Hey, is this Pat? [warm tone]
SELLER: Yeah, who's this?
AGENT: This is Diego. I know this is a little random, but I was just calling about the property on Maple, wanted to see if you had any plans for it?
SELLER: Um, not really. My mom lived there and she passed last year.
AGENT: Man, I'm sorry to hear that.
SELLER: No, I'm not interested in selling right now.
AGENT: Okay, no worries. Now, is that no now or just no never?
SELLER: Just not now.

DELIVERY SUMMARY:
- pace: conversational
"""

PASS, FAILED = 0, 0


def check(name, cond):
    global PASS, FAILED
    if cond:
        PASS += 1
    else:
        FAILED += 1
        print("FAIL:", name)


def full_cold_report(criteria: dict, **over) -> str:
    cats, total = compute("cold_call", criteria)
    js = {"call_id": "CAtest", "pipeline": "cold_call", "caller": "Diego", "call_type": "full",
          "duration_seconds": 60, "outcome": "not now", "auto_fail": "PASS", "total": total,
          "band": over.pop("band", None), "categories": cats, "criteria": criteria,
          "na_reasons": {k: "seller never raised it" for k, v in criteria.items() if v is None},
          "recording_url": "x"}
    if js["band"] is None:
        from rubric_spec import band_for
        js["band"] = band_for(total)
    js.update(over)
    rows = []
    for k, name in RUBRICS["cold_call"]["criteria"].items():
        v = criteria.get(k)
        ev = over.get("_evidence", {}).get(k) if "_evidence" in over else None
        if ev is None:
            ev = "N/A: seller never raised it" if v is None else '"is that no now or just no never"'
        rows.append(f"| {k} {name} | {'N/A' if v is None else v} | {ev} |")
    js.pop("_evidence", None)
    return ("CALL GRADE REPORT\nN/A criteria and weight redistribution: none\n\n"
            "CRITERION SCORES\n| Criterion | Score | Evidence |\n|---|---|---|\n" + "\n".join(rows) +
            '\n\nTOP 3 STRENGTHS\n1. "I know this is a little random": pattern interrupt\n\n'
            'TOP 3 IMPROVEMENT AREAS\n1. They said: "Okay, no worries". Use instead: "Do you know someone who might be interested".\n\n'
            'ONE DRILL FOR THIS WEEK\n10 reps of "a line that is not in the call".\n\n'
            "```json\n" + json.dumps(js) + "\n```\n")


def run(report_text: str) -> list[str]:
    with tempfile.TemporaryDirectory() as d:
        t = Path(d) / "CAtest.md"
        t.write_text(TRANSCRIPT, encoding="utf-8")
        r = Path(d) / "report_CAtest.md"
        r.write_text(report_text, encoding="utf-8")
        return grade.check_report(r, {"call_id": "CAtest", "pipeline": "cold_call", "transcript_file": str(t)})


def main() -> int:
    base = {k: 3 for k in RUBRICS["cold_call"]["criteria"]}
    base["3.1"] = None

    # 1. a correct report passes
    errs = run(full_cold_report(dict(base)))
    check(f"valid report passes: {errs}", errs == [])

    # 2. a paraphrased quote is caught; drill and 'Use instead' quotes are not checked
    bad = full_cold_report(dict(base)).replace('"I know this is a little random"', '"I know this seems random"')
    check("paraphrase caught", any("word for word" in e for e in run(bad)))

    # 3. quotes may skip delivery notes and speaker labels, and join pieces with ...
    ok = full_cold_report(dict(base)).replace('"I know this is a little random"',
                                              '"Hey, is this Pat? ... I know this is a little random"')
    check("ellipsis + bracket notes accepted", run(ok) == [])

    # 4. wrong total is caught
    wrong = full_cold_report(dict(base), total=99.9, band="Elite")
    check("wrong total caught", any("total should be" in e for e in run(wrong)))

    # 5. missing evidence caught
    no_ev = full_cold_report(dict(base)).replace('| 2.1 Motivation asked soft, then mined | 3 | "is that no now or just no never" |',
                                                 "| 2.1 Motivation asked soft, then mined | 3 | seemed fine |")
    check("missing evidence caught", any("2.1 evidence" in e for e in run(no_ev)))

    # 6. N/A with no reason caught
    r = full_cold_report(dict(base))
    r = r.replace('"na_reasons": {"3.1": "seller never raised it"}', '"na_reasons": {}')
    check("N/A without reason caught", any("no reason" in e for e in run(r)))

    # 7. short report with a total is caught
    short_js = {"call_id": "CAtest", "pipeline": "cold_call", "caller": "Diego", "call_type": "short",
                "duration_seconds": 20, "outcome": "no", "auto_fail": "PASS", "total": 40.0, "band": "Needs Work",
                "opener": 3.0, "conversion": 3, "conversion_attempted": True,
                "criteria": {"1.1": 3, "1.2": 3, "1.3": 3, "1.4": None, "3.3": 3}, "recording_url": "x"}
    rows = ("| 1.1 x | 3 | \"is this Pat\" |\n| 1.2 x | 3 | \"any plans for it\" |\n| 1.3 x | 3 | \"a little random\" |\n"
            "| 1.4 x | N/A | N/A: no hesitation |\n| 3.3 x | 3 | \"no now or just no never\" |")
    short_txt = "SHORT CALL REPORT\nOPENER\n| Criterion | Score | Evidence |\n|---|---|---|\n" + rows + "\n\n```json\n"
    errs = run(short_txt + json.dumps(short_js) + "\n```\n")
    check("short with total caught", any("never ranked" in e for e in errs))
    short_js.update(total=None, band=None)
    errs = run(short_txt + json.dumps(short_js) + "\n```\n")
    check(f"valid short passes: {errs}", errs == [])

    # 8. closing: 4.2 cap and 2.6 never N/A
    crit = {k: 4 for k in RUBRICS["closing"]["criteria"]}
    crit["2.6"] = None
    cats, total = compute("closing", crit)
    js = {"call_id": "CAtest", "pipeline": "closing", "call_type": "full", "auto_fail": "PASS",
          "total": total, "band": "Strong", "categories": cats, "criteria": crit,
          "na_reasons": {"2.6": "x"}, "ok_disclosure_missed": True}
    rows = "\n".join(f"| {k} x | {'N/A' if v is None else v} | \"just not now\" |" for k, v in crit.items())
    txt = "N/A criteria and weight redistribution: 2.6\n| Criterion | Score | Evidence |\n" + rows + "\n```json\n" + json.dumps(js) + "\n```\n"
    with tempfile.TemporaryDirectory() as d:
        t = Path(d) / "t.md"
        t.write_text(TRANSCRIPT, encoding="utf-8")
        rp = Path(d) / "r.md"
        rp.write_text(txt, encoding="utf-8")
        errs = grade.check_report(rp, {"call_id": "CAtest", "pipeline": "closing", "transcript_file": str(t)})
    check("2.6 N/A caught", any("2.6 is always scored" in e for e in errs))
    check("4.2 cap caught", any("capped at 2" in e for e in errs))
    check("OK flag text required", any("OK disclosure missed" in e for e in errs))

    # 9. N/A redistribution math: a fully N/A category drops out of the weights
    c = {k: 5 for k in RUBRICS["cold_call"]["criteria"]}
    for k in ("3.1", "3.2", "3.3", "3.4", "3.5"):
        c[k] = None
    cats, total = compute("cold_call", c)
    check("redistributed total is 100", total == 100.0 and cats["3"] is None)

    # 10. lead management: N/A outside 1.4/1.5/5.4 rejected
    lm = {k: 3 for k in RUBRICS["lead_management"]["criteria"]}
    lm["2.3"] = None
    cats, total = compute("lead_management", lm)
    js = {"call_id": "CAtest", "pipeline": "lead_management", "call_type": "full", "auto_fail": "PASS",
          "total": total, "band": "Developing", "categories": cats, "criteria": lm, "na_reasons": {"2.3": "x"}}
    rows = "\n".join(f"| {k} x | {'N/A' if v is None else v} | \"just not now\" |" for k, v in lm.items())
    txt = "N/A criteria and weight redistribution: 2.3\n" + rows + "\n```json\n" + json.dumps(js) + "\n```\n"
    with tempfile.TemporaryDirectory() as d:
        t = Path(d) / "t.md"
        t.write_text(TRANSCRIPT, encoding="utf-8")
        rp = Path(d) / "r.md"
        rp.write_text(txt, encoding="utf-8")
        errs = grade.check_report(rp, {"call_id": "CAtest", "pipeline": "lead_management", "transcript_file": str(t)})
    check("LM disallowed N/A caught", any("2.3 cannot be N/A" in e for e in errs))

    # 11. tie tiers: within 5 points = tied
    t = tiers([("a", 80), ("b", 76), ("c", 70), ("d", 50)])
    check(f"tiers {t}", t == {"a": "T1", "b": "T1", "c": "3", "d": "4"})

    print(f"{PASS} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
