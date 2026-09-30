"""Merge petition-extractor JSON rows into the batch spreadsheet.

The petition-extractor agent writes one JSON file per petition to
output/petition_rows/. This validates every value against the types the
petition-info-extraction skill specifies, writes the batch .xlsx with the
skill's formatting, and can compare rows against an already-verified sheet.

    python src/merge_petition_rows.py --out output/petition_batch.xlsx
    python src/merge_petition_rows.py --only <stem>,<stem> --out output/petition_test.xlsx \
        --compare output/petition_batch.xlsx          # field + Message Board diff

--compare renders the CRM Notes/Message Board text for each matching case with
the SAME _format_petition_notes() the pipeline uses, from both sheets, and
diffs them line by line. Nothing here touches DataSift.

Overwriting output/petition_batch.xlsx first archives the old sheet as
petition_batch_prev_<date>.xlsx, so batch_ocr.py's already-processed check
keeps seeing every earlier case.
"""

import argparse
import difflib
import glob
import json
import os
import re
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import openpyxl  # noqa: E402
from openpyxl.styles import Alignment, Font, PatternFill  # noqa: E402

from datasift_formatter import _format_petition_notes  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROWS_DIR = os.path.join(ROOT, "output", "petition_rows")

COLUMNS = [
    "Property Street", "Property City", "Property State", "Property Zip",
    "First Name", "Last Name", "Date of Mortgage/Note", "Original Loan Amount",
    "Unpaid Principal Balance", "Interest Rate", "Date of Last Payment",
    "Date Foreclosure Filed", "Case Number", "Court County", "Plaintiff",
    "Co-Defendants", "Legal Description", "Plat Number", "Original Lender",
    "Initial Interest Rate", "Original Monthly Payment", "Mortgage Recorded Date",
    "Mortgage Document Number", "Date of Default", "Loan Modification Count",
    "Loan Modification History", "Junior Lienholders", "Owner Status",
    "Co-Borrower First Name", "Co-Borrower Last Name", "Co-Borrower Relationship",
    "Owner Alive",
]
DATES = {"Date of Mortgage/Note", "Date of Last Payment", "Date Foreclosure Filed",
         "Mortgage Recorded Date", "Date of Default"}
MONEY = {"Original Loan Amount", "Unpaid Principal Balance", "Original Monthly Payment"}
RATES = {"Interest Rate", "Initial Interest Rate"}
INTS = {"Loan Modification Count"}
CASE_RX = re.compile(r"^CJ-(20\d\d)-0*(\d+)$", re.IGNORECASE)


def _blank(v):
    return v is None or (isinstance(v, str) and not v.strip())


def coerce(row: dict) -> tuple[dict, list[str]]:
    """Typed values as the skill specifies. Returns (row, problems)."""
    out, problems = {}, []
    for col in COLUMNS:
        v = row.get(col)
        if _blank(v):
            out[col] = None
            continue
        try:
            if col in DATES:
                out[col] = v if isinstance(v, datetime) else datetime.strptime(str(v).strip()[:10], "%Y-%m-%d")
            elif col in MONEY:
                out[col] = float(str(v).replace("$", "").replace(",", ""))
            elif col in RATES:
                r = float(str(v).replace("%", ""))
                if r >= 1:  # 7.125 given as a percent instead of a fraction
                    problems.append(f"{col} {v} looked like a percent; stored as {r / 100}")
                    r = r / 100
                out[col] = r
            elif col in INTS:
                out[col] = int(float(v))
            elif col == "Property Zip":
                out[col] = str(v).strip().split(".")[0].zfill(5)
            elif col == "Case Number":
                m = CASE_RX.match(str(v).strip())
                out[col] = f"CJ-{m.group(1)}-{int(m.group(2))}" if m else str(v).strip()
                if not m:
                    problems.append(f"Case Number {v!r} is not CJ-YYYY-N")
            else:
                out[col] = str(v).strip()
        except (TypeError, ValueError):
            problems.append(f"{col} {v!r} could not be read as the expected type; left blank")
            out[col] = None
    for col in ("Property Street", "Last Name", "Unpaid Principal Balance", "Date Foreclosure Filed", "Case Number"):
        if out[col] is None:
            problems.append(f"{col} is blank")
    if (out["Owner Alive"] or "").lower() not in ("yes", "no"):
        problems.append(f"Owner Alive is {out['Owner Alive']!r}, expected Yes/No")
    # 9:29 sheets carried no Owner Alive column, so their Message Boards never
    # show an "Owner Alive: Yes" line. Keep the board identical: only "No"
    # (the spend gate main.py acts on) is written.
    if (out["Owner Alive"] or "").lower() == "yes":
        out["Owner Alive"] = None
    return out, problems


OCR_DIR = os.path.join(ROOT, "output", "petition_ocr")


def _squash(s: str) -> str:
    """Lowercase alphanumerics only, so line breaks and OCR spacing don't matter."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _value_tokens(col: str, v) -> list[str]:
    """What must appear inside the quoted evidence for the value to be supported."""
    if v is None:
        return []
    if col in MONEY:
        whole = f"{v:,.2f}"
        return [_squash(whole)]
    if col in DATES:
        return [str(v.year), str(v.day)]
    if col == "Property Street":
        m = re.match(r"\s*(\d+)", str(v))
        return [m.group(1)] if m else []
    if col == "Case Number":
        return [str(v).rsplit("-", 1)[-1]]
    return []


def check_evidence(row: dict, evidence: dict) -> list[str]:
    """Each quoted snippet must exist in the petition's OCR text and contain the value.

    Automatic, so a human only reads the rows this flags instead of every row."""
    problems = []
    path = os.path.join(OCR_DIR, row["_source_file"] + ".txt")
    hdr = os.path.join(OCR_DIR, row["_source_file"] + ".hdr.txt")
    if not os.path.exists(path):
        return [f"no OCR text at {path} to check evidence against"]
    text = open(path, encoding="utf-8").read()
    if os.path.exists(hdr):
        text += open(hdr, encoding="utf-8").read()
    source = _squash(text)
    for col in ("Property Street", "Original Loan Amount", "Unpaid Principal Balance",
                "Date of Default", "Date Foreclosure Filed"):
        if row.get(col) is None:
            continue
        quote = str((evidence or {}).get(col) or "")
        # Pieces: several quotes joined with "/", or one shortened with "...".
        # Page labels and the agent's own descriptions sit outside quote marks.
        quoted = re.findall(r"[\"'“‘](.+?)[\"'”’](?=\s*(?:/|$|\.\.\.|…|;|,\s*[A-Za-z]))", quote)
        if not quoted:
            quoted = [re.sub(r"^\s*(hdr\s*)?PAGE\s*[\d/ ]+(\([^)]*\))?\s*:\s*", "", quote, flags=re.IGNORECASE)]
        pieces = [p for q in quoted for p in re.split(r"\.\.\.|…", q) if len(_squash(p)) >= 12]
        if not pieces:
            problems.append(f"{col}: no usable evidence quote given")
            continue
        absent = [p for p in pieces if _squash(p) not in source]
        if absent:
            problems.append(f"{col}: quoted evidence not found in the OCR text: {absent[0][:60]!r}")
            continue
        quote = " ".join(pieces)
        missing = [t for t in _value_tokens(col, row[col]) if _squash(t) not in _squash(quote)]
        if missing:
            problems.append(f"{col}: value {row[col]!r} not supported by its quote ({', '.join(missing)} absent)")
    return problems


def load_json_rows(only: set | None) -> list[dict]:
    rows = []
    for path in sorted(glob.glob(os.path.join(ROWS_DIR, "*.json"))):
        stem = os.path.splitext(os.path.basename(path))[0]
        if only and stem not in only:
            continue
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        row, problems = coerce(raw)
        row["_source_file"] = stem
        row["_problems"] = problems + check_evidence(row, raw.get("_evidence") or {})
        row["_notes"] = raw.get("_notes") or ""
        rows.append(row)
    return rows


def write_xlsx(rows: list[dict], out_path: str) -> None:
    if os.path.basename(out_path) == "petition_batch.xlsx" and os.path.exists(out_path):
        stamp = datetime.fromtimestamp(os.path.getmtime(out_path)).strftime("%Y-%m-%d")
        archive = os.path.join(os.path.dirname(out_path), f"petition_batch_prev_{stamp}.xlsx")
        if not os.path.exists(archive):
            shutil.copy2(out_path, archive)
            print(f"archived previous sheet -> {os.path.basename(archive)}")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Petitions"
    ws.append(COLUMNS)
    head_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="1F3864")
    for cell in ws[1]:
        cell.font, cell.fill = head_font, head_fill
        cell.alignment = Alignment(horizontal="center")
    body_font = Font(name="Calibri", size=11)
    for row in rows:
        ws.append([row[c] for c in COLUMNS])
        for col_idx, col in enumerate(COLUMNS, start=1):
            cell = ws.cell(row=ws.max_row, column=col_idx)
            cell.font = body_font
            if col in DATES:
                cell.number_format = "mm/dd/yyyy"
            elif col in MONEY:
                cell.number_format = "$#,##0.00"
            elif col in RATES:
                cell.number_format = "0.000%"
            elif col == "Property Zip":
                cell.number_format = "@"
    for col_idx, col in enumerate(COLUMNS, start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(col_idx)].width = max(12, min(45, len(col) + 4))
    tmp = out_path + ".tmp.xlsx"
    wb.save(tmp)
    os.replace(tmp, out_path)


def read_xlsx(path: str) -> list[dict]:
    ws = openpyxl.load_workbook(path, read_only=True).active
    it = ws.iter_rows(values_only=True)
    header = list(next(it))
    return [dict(zip(header, r)) for r in it if any(v not in (None, "") for v in r)]


def compare(rows: list[dict], ref_path: str) -> int:
    ref = {}
    ref_rows = read_xlsx(ref_path)
    ref_cols = set(ref_rows[0]) if ref_rows else set()
    for r in ref_rows:
        row, _ = coerce(r)
        if row["Case Number"]:
            ref[row["Case Number"]] = row
    mismatches = 0
    for row in rows:
        case = row["Case Number"]
        print(f"\n===== {case}  ({row['_source_file']}) =====")
        if case not in ref:
            print(f"  not in {os.path.basename(ref_path)}; nothing to compare")
            continue
        want = ref[case]
        field_diffs = []
        for col in COLUMNS:
            a, b = want.get(col), row.get(col)
            if isinstance(a, float) and isinstance(b, float):
                same = abs(a - b) < 0.005
            else:
                same = (a or None) == (b or None)
            # Older verified sheets have no Owner Alive column at all.
            if not same and not (col == "Owner Alive" and col not in ref_cols):
                field_diffs.append((col, a, b))
        for col, a, b in field_diffs:
            print(f"  FIELD {col}\n     verified: {a!r}\n     agent:    {b!r}")
        board_a = _format_petition_notes(dict(want), "foreclosure").splitlines()
        board_b = _format_petition_notes(dict(row), "foreclosure").splitlines()
        diff = list(difflib.unified_diff(board_a, board_b, "verified", "agent", lineterm="", n=0))
        if diff:
            print("  MESSAGE BOARD DIFF:")
            for line in diff[2:]:
                print("    " + line)
        else:
            print(f"  Message Board text: IDENTICAL ({len(board_a)} lines)")
        if field_diffs or diff:
            mismatches += 1
    print(f"\n{mismatches} of {len(rows)} case(s) differ from the verified sheet")
    return mismatches


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(ROOT, "output", "petition_batch.xlsx"))
    ap.add_argument("--only", help="comma-separated stems to include")
    ap.add_argument("--compare", help="verified .xlsx to diff against (fields + Message Board)")
    args = ap.parse_args()

    only = set(s.strip() for s in args.only.split(",")) if args.only else None
    rows = load_json_rows(only)
    if not rows:
        print(f"No JSON rows in {ROWS_DIR}")
        return 1

    cases = {}
    for r in rows:
        cases.setdefault(r["Case Number"], []).append(r["_source_file"])
    for case, stems in cases.items():
        if case and len(stems) > 1:
            for r in rows:
                if r["Case Number"] == case:
                    r["_problems"].append(f"same case number as {', '.join(s for s in stems if s != r['_source_file'])}")

    # batch_ocr's summary screens repeats off a garbled stamp and can miss them;
    # this re-check uses the case number as actually read.
    from batch_ocr import known_cases
    done_before = known_cases()
    for r in rows:
        if r["Case Number"] in done_before:
            r["_problems"].append("ALREADY PROCESSED in an earlier batch; drop before --create")

    rows.sort(key=lambda r: (r["Date Foreclosure Filed"] or datetime.max, r["Case Number"] or ""))
    write_xlsx(rows, args.out)
    print(f"wrote {len(rows)} row(s) -> {args.out}")
    flagged = [r for r in rows if r["_problems"] or r["_notes"]]
    for r in flagged:
        print(f"\n  {r['Case Number'] or '?'}  {r['First Name'] or ''} {r['Last Name'] or ''}  ({r['_source_file']})")
        for p in r["_problems"]:
            print(f"    CHECK: {p}")
        if r["_notes"]:
            print(f"    agent note: {r['_notes']}")
    if args.compare:
        return 1 if compare(rows, args.compare) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
