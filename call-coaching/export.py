"""export.py - one Excel workbook plus a scorecard per caller.

Reads only reports that passed `grade.py check`. Per-call reports are the
graded .md files themselves (output/reports/<rubric>/). This adds:

  output/Call_Coaching_<from>_to_<to>.xlsx
    Summary          per caller x rubric: graded calls, average, band, tier
    All Calls        every call found, with where it went and why
    Cold Call / Lead Management / Closing   category + criterion scores per call
    Criterion Averages  per caller, per criterion: where to coach
    Short Calls      short "no" calls, never ranked with full calls
    Connection Log   voicemails, wrong numbers, no-contact, too-short
  output/scorecards/<caller>.md   one scorecard per person

Totals within 5 points are shown as tied (T1, T1, ...), never ranked apart.

USAGE (from SiftStack root):
  py -3 call-coaching/export.py                  # since 2026-09-28
  py -3 call-coaching/export.py --days 7
  py -3 call-coaching/export.py --since 2026-10-01 --until 2026-10-31
"""
from __future__ import annotations

import argparse
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from cc_common import (CALLING_START, CALLS_JSON, OUT, PIPELINES, SCORECARD_DIR, display_name,
                       is_excluded, load_json, log, mmss, slug)
from rubric_spec import RUBRICS, TIE_POINTS, band_for

HEAD_FILL = PatternFill("solid", fgColor="1F3A5F")
HEAD_FONT = Font(bold=True, color="FFFFFF")
BAND_FILL = {"Elite": "C6EFCE", "Strong": "DDEBF7", "Developing": "FFF2CC",
             "Needs Work": "FCE4D6", "Retrain": "F8CBAD", "FAIL": "FF9999"}


def tiers(items: list[tuple[str, float]]) -> dict[str, str]:
    """Rank labels where anything within TIE_POINTS of the tier's top is tied."""
    ranked = sorted(items, key=lambda x: -x[1])
    out, rank, top, members = {}, 0, None, []

    def flush():
        label = (f"T{rank}" if len(members) > 1 else str(rank))
        for m in members:
            out[m] = label

    for i, (key, val) in enumerate(ranked):
        if top is None or top - val > TIE_POINTS:
            if members:
                flush()
            rank, top, members = i + 1, val, []
        members.append(key)
    if members:
        flush()
    return out


def drill_of(report_file: str | None) -> str:
    if not report_file or not Path(report_file).exists():
        return ""
    text = Path(report_file).read_text(encoding="utf-8")
    m = re.search(r"ONE DRILL[^\n]*\n(.*?)(?:\n\s*\n|```)", text, re.S)
    if not m:
        m = re.search(r"ONE DRILL[^:\n]*:\s*(.+)", text)
    return " ".join(m.group(1).split()) if m else ""


def sheet(wb, title, headers, rows, widths=None, link_cols=()):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for cell in ws[1]:
        cell.fill, cell.font = HEAD_FILL, HEAD_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for r in rows:
        ws.append(r)
    for col in link_cols:
        for row in ws.iter_rows(min_row=2, min_col=col, max_col=col):
            cell = row[0]
            if isinstance(cell.value, str) and cell.value.startswith(("http", "C:", "c:")):
                cell.hyperlink = cell.value if cell.value.startswith("http") else "file:///" + cell.value.replace("\\", "/")
                cell.value = "open"
                cell.font = Font(color="0563C1", underline="single")
    band_col = headers.index("Band") + 1 if "Band" in headers else None
    if band_col:
        for row in ws.iter_rows(min_row=2, min_col=band_col, max_col=band_col):
            fill = BAND_FILL.get(row[0].value or "")
            if fill:
                row[0].fill = PatternFill("solid", fgColor=fill)
    for i, h in enumerate(headers, 1):
        w = (widths or {}).get(h) or min(45, max(10, len(str(h)) + 2))
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    return ws


def main() -> int:
    ap = argparse.ArgumentParser(description="Workbook + per-caller scorecards")
    ap.add_argument("--since", default=CALLING_START)
    ap.add_argument("--until", default=date.today().isoformat())
    ap.add_argument("--days", type=int)
    args = ap.parse_args()
    since = (date.today() - timedelta(days=args.days)).isoformat() if args.days else args.since
    until = args.until

    calls = [c for c in load_json(CALLS_JSON, [])
             if not is_excluded(c.get("caller")) and since <= (c.get("created_local") or "")[:10] <= until]
    if not calls:
        log(f"No calls between {since} and {until}.")
        return 1
    for c in calls:
        c["who"] = display_name(c.get("caller"))

    graded = [c for c in calls if c.get("graded") and c.get("scores")]
    full = [c for c in graded if c["scores"].get("call_type") == "full"]
    short = [c for c in graded if c["scores"].get("call_type") == "short"]
    # Connection log: sorted as not gradeable, or never answered / no recording.
    conn = [c for c in calls if c.get("pipeline") == "not_gradeable"
            or (not c.get("pipeline") and not c.get("recording_url"))]
    # Pending: sorted into a rubric but no passing report yet, or recorded but not transcribed yet.
    pending = [c for c in calls if (c.get("pipeline") in PIPELINES and not c.get("graded"))
               or (not c.get("pipeline") and c.get("recording_url"))]

    # call-level tiers within each rubric (full calls only, auto-fails excluded)
    call_tier = {}
    for p in PIPELINES:
        items = [(c["call_id"], c["scores"]["total"]) for c in full
                 if c["scores"].get("pipeline") == p and c["scores"].get("total") is not None]
        call_tier.update(tiers(items))

    # caller x rubric aggregates
    agg = defaultdict(lambda: {"totals": [], "fails": 0, "short": [], "ok_missed": 0,
                               "crit": defaultdict(list), "cats": defaultdict(list)})
    for c in full:
        s = c["scores"]
        a = agg[(c["who"], s["pipeline"])]
        if s.get("total") is not None:
            a["totals"].append(s["total"])
        if str(s.get("auto_fail", "PASS")).upper() != "PASS":
            a["fails"] += 1
        if s.get("ok_disclosure_missed") is True:
            a["ok_missed"] += 1
        for k, v in (s.get("criteria") or {}).items():
            if v is not None:
                a["crit"][k].append(v)
        for k, v in (s.get("categories") or {}).items():
            if v is not None:
                a["cats"][k].append(v)
    for c in short:
        agg[(c["who"], c["scores"]["pipeline"])]["short"].append(c["scores"])

    caller_tier = {}
    for p in PIPELINES:
        items = [(who, sum(a["totals"]) / len(a["totals"])) for (who, pp), a in agg.items()
                 if pp == p and a["totals"]]
        for who, t in tiers(items).items():
            caller_tier[(who, p)] = t

    wb = Workbook()
    wb.remove(wb.active)

    # Summary
    rows = []
    for (who, p), a in sorted(agg.items()):
        avg = round(sum(a["totals"]) / len(a["totals"]), 1) if a["totals"] else None
        sh = a["short"]
        rows.append([who, RUBRICS[p]["title"], len(a["totals"]), avg, band_for(avg) if avg is not None else "",
                     caller_tier.get((who, p), ""), a["fails"], len(sh),
                     round(sum(x.get("opener") or 0 for x in sh) / len(sh), 2) if sh else None,
                     f"{sum(1 for x in sh if x.get('conversion_attempted')) / len(sh):.0%}" if sh else "",
                     a["ok_missed"] if p == "closing" else ""])
    for who in sorted({c["who"] for c in calls}):
        n_conn = sum(1 for c in conn if c["who"] == who)
        n_pend = sum(1 for c in pending if c["who"] == who)
        rows.append([who, "Connection Log / not graded yet", None, None, "", "", None, None, None, "",
                     f"{n_conn} connection log, {n_pend} awaiting transcription or grading"])
    ws = sheet(wb, "Summary", ["Caller", "Rubric", "Full calls graded", "Avg total", "Band",
                               "Tier (within 5 = tie)", "Auto-fails", "Short calls", "Short: avg opener",
                               "Short: conversion attempted", "Notes / OK disclosure missed"], rows,
               widths={"Rubric": 30, "Notes / OK disclosure missed": 40})
    ws.insert_rows(1, 2)
    ws["A1"] = f"Tulsa Homebuyers call coaching, {since} to {until}"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = "Totals within 5 points are treated as equal (shown as tied tiers). Short calls are never averaged into totals."
    ws.freeze_panes = "A4"

    # All Calls
    rows = []
    for c in sorted(calls, key=lambda x: x.get("created_utc") or ""):
        s = c.get("scores") or {}
        rows.append([c.get("created_local"), c["who"], c.get("direction"), mmss(c.get("duration_seconds")),
                     RUBRICS[c["pipeline"]]["title"] if c.get("pipeline") in RUBRICS else "Not gradeable",
                     s.get("call_type") or c.get("report_type") or "", c.get("outcome") or c.get("not_gradeable_reason") or "",
                     s.get("total"), s.get("band") or "", call_tier.get(c["call_id"], ""),
                     s.get("auto_fail") or "", "MISSED" if s.get("ok_disclosure_missed") else "",
                     "graded" if c.get("graded") else
                     ("needs grading" if c.get("pipeline") in PIPELINES else
                      "needs transcription" if not c.get("pipeline") and c.get("recording_url") else "connection log"),
                     c.get("address") or "", c.get("recording_url"), c.get("record_url"), c.get("report_file"),
                     c["call_id"]])
    sheet(wb, "All Calls", ["When (Central)", "Caller", "Direction", "Length", "Rubric", "Report", "Outcome",
                            "Total", "Band", "Tier (within 5 = tie)", "Auto-fail", "OK disclosure", "Status",
                            "Property", "Recording", "DataSift record", "Report file", "Call SID"], rows,
          widths={"When (Central)": 19, "Outcome": 28, "Property": 34, "Call SID": 38}, link_cols=(15, 16, 17))

    # per-rubric score sheets
    for p in PIPELINES:
        spec = RUBRICS[p]
        crit_ids = list(spec["criteria"])
        rows = []
        for c in sorted((x for x in full if x["scores"]["pipeline"] == p), key=lambda x: x.get("created_utc") or ""):
            s = c["scores"]
            rows.append([c.get("created_local"), c["who"], s.get("total"), s.get("band"), call_tier.get(c["call_id"], ""),
                         *[s["categories"].get(k) for k in spec["weights"]],
                         *[s["criteria"].get(k) for k in crit_ids], c.get("report_file")])
        headers = (["When (Central)", "Caller", "Total", "Band", "Tier (within 5 = tie)"] +
                   [f"C{k} {spec['categories'][k]} ({w}%)" for k, w in spec["weights"].items()] +
                   [f"{k} {spec['criteria'][k]}" for k in crit_ids] + ["Report"])
        ws = sheet(wb, spec["title"], headers, rows, widths={"When (Central)": 19},
                   link_cols=(len(headers),))
        ws.row_dimensions[1].height = 75

    # Criterion averages
    rows = []
    for (who, p), a in sorted(agg.items()):
        for k, name in RUBRICS[p]["criteria"].items():
            vals = a["crit"].get(k) or []
            rows.append([who, RUBRICS[p]["title"], f"{k} {name}", len(vals),
                         round(sum(vals) / len(vals), 2) if vals else None])
    sheet(wb, "Criterion Averages", ["Caller", "Rubric", "Criterion", "Times scored", "Average (0-5)"], rows,
          widths={"Criterion": 50})

    # Short calls
    rows = []
    for c in sorted(short, key=lambda x: x.get("created_utc") or ""):
        s = c["scores"]
        rows.append([c.get("created_local"), c["who"], RUBRICS[s["pipeline"]]["title"], mmss(c.get("duration_seconds")),
                     s.get("outcome"), s.get("opener"), s.get("conversion"), s.get("conversion_attempted"),
                     c.get("recording_url"), c.get("report_file")])
    sheet(wb, "Short Calls", ["When (Central)", "Caller", "Rubric", "Length", "Outcome", "Opener avg (0-5)",
                              "Converting the no (0-5)", "Conversion attempted", "Recording", "Report"], rows,
          widths={"When (Central)": 19}, link_cols=(9, 10))

    # Connection log
    rows = [[c.get("created_local"), c["who"], c.get("direction"), mmss(c.get("duration_seconds")),
             c.get("not_gradeable_reason") or "no answer / no recording",
             c.get("outcome") or c.get("status") or "", c.get("address") or "", c.get("recording_url"), c.get("record_url")]
            for c in sorted(conn, key=lambda x: x.get("created_utc") or "")]
    sheet(wb, "Connection Log", ["When (Central)", "Caller", "Direction", "Length", "Reason", "Outcome",
                                 "Property", "Recording", "DataSift record"], rows,
          widths={"When (Central)": 19, "Property": 34, "Outcome": 28}, link_cols=(8, 9))

    OUT.mkdir(parents=True, exist_ok=True)
    xlsx = OUT / f"Call_Coaching_{since}_to_{until}.xlsx"
    tmp = OUT / f"_PENDING_{xlsx.name}"
    wb.save(tmp)
    try:
        tmp.replace(xlsx)
    except PermissionError:
        log(f"The workbook is open in Excel; saved as {tmp.name} instead. Close Excel and re-run.")
        xlsx = tmp

    # scorecards
    SCORECARD_DIR.mkdir(parents=True, exist_ok=True)
    for who in sorted({c["who"] for c in calls}):
        lines = [f"# Call Coaching Scorecard: {who}", f"Tulsa Homebuyers, {since} to {until}", "",
                 "Totals within 5 points are treated as equal.", ""]
        mine = [c for c in calls if c["who"] == who]
        lines.append(f"Calls found: {len(mine)}. Graded full: {sum(1 for c in full if c['who'] == who)}. "
                     f"Short: {sum(1 for c in short if c['who'] == who)}. "
                     f"Connection log: {sum(1 for c in conn if c['who'] == who)}. "
                     f"Awaiting transcription or grading: {sum(1 for c in pending if c['who'] == who)}.")
        for p in PIPELINES:
            a = agg.get((who, p))
            if not a:
                continue
            spec = RUBRICS[p]
            lines += ["", f"## {spec['title']}"]
            if a["totals"]:
                avg = round(sum(a["totals"]) / len(a["totals"]), 1)
                lines.append(f"- Full calls graded: {len(a['totals'])}. Average {avg} ({band_for(avg)}). "
                             f"Team tier: {caller_tier.get((who, p), '-')}. Auto-fails: {a['fails']}.")
                if p == "closing":
                    lines.append(f"- OK disclosure missed on {a['ok_missed']} call(s).")
                lines.append("- Category averages: " + "; ".join(
                    f"{spec['categories'][k]} {sum(v) / len(v):.2f}/5" for k, v in sorted(a["cats"].items())))
                crit_avg = sorted(((sum(v) / len(v), k) for k, v in a["crit"].items()))
                lines.append("- Work on first (lowest criteria): " + "; ".join(
                    f"{k} {spec['criteria'][k]} {v:.1f}" for v, k in crit_avg[:3]))
                lines.append("- Keep doing (highest criteria): " + "; ".join(
                    f"{k} {spec['criteria'][k]} {v:.1f}" for v, k in crit_avg[::-1][:3]))
            if a["short"]:
                sh = a["short"]
                lines.append(f"- Short 'no' calls: {len(sh)}, avg opener "
                             f"{sum(x.get('opener') or 0 for x in sh) / len(sh):.2f}/5, conversion attempted on "
                             f"{sum(1 for x in sh if x.get('conversion_attempted'))} of {len(sh)}.")
            lines += ["", "| When | Report | Total | Band | Tier | Outcome |", "|---|---|---|---|---|---|"]
            for c in sorted((x for x in graded if x["who"] == who and x["scores"]["pipeline"] == p),
                            key=lambda x: x.get("created_utc") or ""):
                s = c["scores"]
                lines.append(f"| {c.get('created_local')} | {s.get('call_type')} | {s.get('total') if s.get('total') is not None else '-'} "
                             f"| {s.get('band') or '-'} | {call_tier.get(c['call_id'], '-')} | {s.get('outcome') or ''} |")
            latest = max((x for x in full if x["who"] == who and x["scores"]["pipeline"] == p),
                         key=lambda x: x.get("created_utc") or "", default=None)
            if latest:
                lines += ["", f"Latest drill: {drill_of(latest.get('report_file')) or '-'}"]
        (SCORECARD_DIR / f"{slug(who)}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    log(f"{len(calls)} calls: {len(full)} graded full, {len(short)} short, {len(conn)} connection log, "
        f"{len(pending)} awaiting grading.")
    log(f"-> {xlsx}")
    log(f"-> {SCORECARD_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
