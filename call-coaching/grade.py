"""grade.py - the grading step: build the queue, then check every report.

Grading itself is done by Claude inside a Claude Code session (no extra API
cost), reading each transcript against its rubric and writing one report per
call. This script is the part that keeps that honest:

  queue   list every sorted call that still needs a report, with the rubric,
          the transcript, and the exact path the report must be written to.
  check   validate reports and refuse any that break the rules:
            - every quote in the evidence, strengths, improvements and
              "what happened" must appear WORD FOR WORD in the transcript
            - every criterion row carries a quote or a stated absence
            - N/A only where the rubric allows it, each with a reason
            - category averages, the weighted total (with N/A weight
              redistributed) and the band are recomputed and must match
              the SCORES JSON, and the tables must match the JSON
            - short calls carry no total or band
            - closing: 2.6 always scored; 4.2 capped at 2 when an Oklahoma
              disclosure was missed
          A report that passes is marked graded in calls.json.

USAGE (from SiftStack root):
  py -3 call-coaching/grade.py queue
  py -3 call-coaching/grade.py check                 # every report
  py -3 call-coaching/grade.py check <report.md> ... # specific reports
Rules for the grader: call-coaching/GRADING.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from cc_common import (CALLS_JSON, PIPELINES, QUEUE_JSON, REPORT_DIR, RUBRICS as RUBRIC_DIR,
                       display_name, is_excluded, load_json, log, save_json, slug)
from rubric_spec import RUBRICS, band_for, compute

CRIT_ROW = re.compile(r"^\|\s*(\d\.\d)\b(.*)$")
QUOTE = re.compile(r"[\"“]([^\"“”]{3,}?)[\"”]")


# ---------------------------------------------------------------- paths

def report_path(c: dict) -> Path:
    day = (c.get("created_local") or "")[:10] or "undated"
    return REPORT_DIR / c["pipeline"] / f"{day}_{slug(display_name(c.get('caller')))}_{c['call_id']}.md"


# ---------------------------------------------------------------- queue

def cmd_queue() -> int:
    calls = load_json(CALLS_JSON, [])
    todo = []
    for c in calls:
        if is_excluded(c.get("caller")) or c.get("pipeline") not in PIPELINES:
            continue
        rp = report_path(c)
        if c.get("graded") and rp.exists():
            continue
        todo.append({
            "call_id": c["call_id"], "caller": display_name(c.get("caller")),
            "pipeline": c["pipeline"], "report_type": c.get("report_type") or "full",
            "when": c.get("created_local"), "duration_seconds": c.get("duration_seconds"),
            "outcome": c.get("outcome"), "recording_url": c.get("recording_url"),
            "rubric": str(RUBRIC_DIR / RUBRICS[c["pipeline"]]["file"]),
            "transcript": c.get("transcript_file"), "report": str(rp),
            "status": "written, fails check" if rp.exists() else "not written",
        })
    save_json(QUEUE_JSON, todo)
    log(f"{len(todo)} call(s) need a report. Rules: call-coaching/GRADING.md")
    for t in todo:
        log(f"  {t['call_id']}  {t['caller']:<8} {t['pipeline']:<16} {t['report_type']:<5} "
            f"{t['when']}  [{t['status']}]\n      transcript: {t['transcript']}\n      report:     {t['report']}")
    log(f"-> {QUEUE_JSON}")
    return 0


# ---------------------------------------------------------------- verbatim quotes

def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("—", " ").replace("–", " ")
    s = re.sub(r"\[[^\]]*\]", " ", s)                                   # delivery notes
    s = re.sub(r"(?m)^\s*(AGENT_VM|AGENT|SELLER|VOICEMAIL|OTHER)\s*:", " ", s)
    s = re.sub(r"\b(AGENT_VM|AGENT|SELLER|OTHER)\s*:", " ", s)
    s = s.lower()
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return " " + " ".join(s.split()) + " "


def transcript_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    body = text.split("## Transcript", 1)[-1]
    return body.split("DELIVERY SUMMARY", 1)[0]


def quote_found(quote: str, norm_transcript: str) -> bool:
    for frag in re.split(r"\.\.\.|…", quote):
        nf = _norm(frag).strip()
        if len(nf) < 2:
            continue
        if f" {nf} " not in norm_transcript:
            return False
    return True


def transcript_quotes(report_body: str) -> list[str]:
    """Quotes that claim to come from the call. Excludes prescribed playbook
    lines ('Use instead: ...') and the drill, which quote the rubric."""
    quotes, section = [], ""
    for line in report_body.splitlines():
        head = line.strip().upper()
        if head.startswith(("TOP 3 STRENGTHS", "TOP 3 IMPROVEMENT", "WHAT HAPPENED", "ONE FIX",
                            "CRITERION SCORES", "OPENER", "PILLAR")):
            section = head
        elif head.startswith(("ONE DRILL", "SCORES", "CONVERSION ATTEMPT", "```")):
            section = "skip"
        if section == "skip" or not section:
            continue
        text = re.split(r"Use instead\s*:", line, maxsplit=1, flags=re.I)[0]
        quotes += [q.strip() for q in QUOTE.findall(text)]
    return quotes


# ---------------------------------------------------------------- report parsing

def parse_report(path: Path) -> tuple[str, dict | None]:
    text = path.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if not blocks:
        return text, None
    try:
        return text[: text.rfind("```json")], json.loads(blocks[-1])
    except json.JSONDecodeError as e:
        return text, {"_json_error": str(e)}


def table_rows(body: str) -> dict:
    """{criterion id: (score cell, evidence cell)} from the CRITERION SCORES / OPENER tables."""
    rows = {}
    for line in body.splitlines():
        m = CRIT_ROW.match(line.strip())
        if not m:
            continue
        cells = [x.strip() for x in line.strip().strip("|").split("|")]
        score = next((x for x in cells[1:] if re.fullmatch(r"(?i)[0-5]|n/?a", x)), None)
        evidence = cells[-1] if len(cells) >= 3 else ""
        rows[m.group(1)] = (score, evidence)
    return rows


# ---------------------------------------------------------------- the checks

def check_report(path: Path, call: dict | None) -> list[str]:
    errs = []
    body, js = parse_report(path)
    if js is None:
        return ["no ```json SCORES block at the end of the report"]
    if "_json_error" in js:
        return ["SCORES JSON does not parse: " + js["_json_error"]]
    pipe = js.get("pipeline") or (call or {}).get("pipeline")
    if pipe not in RUBRICS:
        return [f"SCORES JSON 'pipeline' must be one of {list(RUBRICS)}"]
    spec = RUBRICS[pipe]
    if call and js.get("call_id") != call["call_id"]:
        errs.append(f"call_id {js.get('call_id')} does not match the call {call['call_id']}")
    crit = js.get("criteria") or {}
    ctype = js.get("call_type")
    na_reasons = js.get("na_reasons") or {}

    for k, v in crit.items():
        if v is not None and (not isinstance(v, int) or not 0 <= v <= 5):
            errs.append(f"criterion {k} score {v!r} must be a whole number 0-5 or null")

    if ctype == "short":
        short = spec["short"]
        if not short:
            errs.append("the closing rubric has no short report; grade it as a full call")
        else:
            allowed = set(short["opener"]) | {short["conversion"]}
            extra = set(crit) - allowed
            if extra:
                errs.append(f"short report may only score {sorted(allowed)}; also has {sorted(extra)}")
            if js.get("total") is not None or js.get("band") is not None:
                errs.append("short calls carry total: null and band: null (never ranked with full calls)")
            op = [crit.get(k) for k in short["opener"] if crit.get(k) is not None]
            want = round(sum(op) / len(op), 2) if op else None
            if want != (round(js["opener"], 2) if js.get("opener") is not None else None):
                errs.append(f"'opener' should be {want} (average of the scored opener criteria)")
            if js.get("conversion") != crit.get(short["conversion"]):
                errs.append(f"'conversion' must equal criterion {short['conversion']}")
    elif ctype == "full":
        missing = set(spec["criteria"]) - set(crit)
        extra = set(crit) - set(spec["criteria"])
        if missing:
            errs.append(f"criteria missing from JSON: {sorted(missing)}")
        if extra:
            errs.append(f"criteria not in this rubric: {sorted(extra)}")
        nulls = {k for k, v in crit.items() if v is None}
        for k in nulls & spec["never_na"]:
            errs.append(f"{k} is always scored in this rubric; N/A is not allowed")
        if not spec["na_any"]:
            non_dm = bool(js.get("non_decision_maker"))
            ok = set(spec["na_allowed"])
            if non_dm:
                ok |= {k for k in spec["criteria"] if k.split(".")[0] in spec["non_dm_categories"]}
            for k in sorted(nulls - ok):
                errs.append(f"{k} cannot be N/A in this rubric (allowed: {sorted(ok)})")
        for k in sorted(nulls):
            if not str(na_reasons.get(k, "")).strip():
                errs.append(f"{k} is N/A but has no reason in 'na_reasons'")
        cats, total = compute(pipe, crit)
        for cat, val in cats.items():
            got = (js.get("categories") or {}).get(cat)
            if (val is None) != (got is None) or (val is not None and abs(val - float(got)) > 0.011):
                errs.append(f"category {cat} should be {val} (average of its scored criteria), JSON has {got}")
        if total is None or js.get("total") is None or abs(total - float(js["total"])) > 0.15:
            errs.append(f"total should be {total} (weighted, N/A weight redistributed), JSON has {js.get('total')}")
        band = "FAIL" if str(js.get("auto_fail", "PASS")).upper() != "PASS" else (band_for(total) if total is not None else None)
        if js.get("band") != band:
            errs.append(f"band should be {band!r}, JSON has {js.get('band')!r}")
        if not re.search(r"(?i)N/A criteria and weight redistribution\s*:", body):
            errs.append("missing the line 'N/A criteria and weight redistribution: ...'")
        if pipe == "closing":
            flag = js.get("ok_disclosure_missed", "absent")
            if flag == "absent":
                errs.append("closing JSON needs 'ok_disclosure_missed' (null / true / false)")
            elif (crit.get("4.2") is None) != (flag is None):
                errs.append("'ok_disclosure_missed' is null exactly when 4.2 is N/A (no agreement stage)")
            if flag is True and crit.get("4.2") is not None and crit["4.2"] > 2:
                errs.append("4.2 is capped at 2 when an Oklahoma disclosure was missed")
            if flag is True and "OK disclosure missed" not in body:
                errs.append("report must carry the flag text 'OK disclosure missed'")
    else:
        errs.append("call_type must be 'full' or 'short'")

    # tables must match the JSON, and every row needs evidence
    rows = table_rows(body)
    for k, v in crit.items():
        if k not in rows:
            errs.append(f"criterion {k} has no row in the criterion table")
            continue
        score, evidence = rows[k]
        want = "N/A" if v is None else str(v)
        if (score or "").upper().replace("NA", "N/A") != want:
            errs.append(f"table shows {k} = {score}, JSON has {want}")
        if not QUOTE.search(evidence) and not re.match(r"(?i)\s*(absent|n/a)\b", evidence):
            errs.append(f"criterion {k} evidence must quote the call or start with ABSENT: / N/A:")

    # every claimed transcript quote must be word for word
    tpath = Path((call or {}).get("transcript_file") or "")
    if tpath.is_file():
        norm_t = _norm(transcript_body(tpath))
        for q in transcript_quotes(body):
            if not quote_found(q, norm_t):
                errs.append(f"quote not found word for word in the transcript: \"{q[:90]}\"")
    else:
        errs.append("transcript file not found, quotes cannot be verified")
    return errs


def cmd_check(paths: list[str]) -> int:
    calls = load_json(CALLS_JSON, [])
    by_id = {c["call_id"]: c for c in calls}
    reports = [Path(p) for p in paths] if paths else sorted(REPORT_DIR.glob("*/*.md"))
    if not reports:
        log("No reports to check.")
        return 0
    bad = 0
    for rp in reports:
        m = re.search(r"(CA[0-9a-f]{32}|[A-Za-z0-9-]{20,})\.md$", rp.name)
        call = by_id.get(m.group(1)) if m else None
        errs = check_report(rp, call)
        if call:
            _, js = parse_report(rp)
            call["graded"] = not errs
            call["grade_errors"] = errs
            call["report_file"] = str(rp)
            if not errs:
                call["scores"] = js
        if errs:
            bad += 1
            log(f"FAIL  {rp.name}")
            for e in errs:
                log(f"      - {e}")
        else:
            log(f"PASS  {rp.name}")
    save_json(CALLS_JSON, calls)
    log(f"{len(reports) - bad} of {len(reports)} report(s) pass.")
    return 0 if not bad else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Grading queue + report checker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("queue")
    c = sub.add_parser("check")
    c.add_argument("reports", nargs="*")
    args = ap.parse_args()
    return cmd_queue() if args.cmd == "queue" else cmd_check(args.reports)


if __name__ == "__main__":
    sys.exit(main())
