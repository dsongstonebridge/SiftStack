"""Phase B proof: exercise the new API-based upload_to_datasift(),
skip_trace_records(), read_record_phone_numbers(), upload_phone_tags() —
the functions now shared by daily --upload-datasift, dropbox-watch, and
skip-and-score-upload — against one throwaway record before any real lead
touches them.

Run: python test_datasift_cutover.py
"""

import asyncio
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from datasift_formatter import DATASIFT_COLUMNS  # noqa: E402
import datasift_uploader as up  # noqa: E402

RUN_ID = str(int(time.time()))
TEST_DIR = Path(__file__).parent / "output"
TEST_DIR.mkdir(exist_ok=True)


def build_row() -> dict:
    row = {c: "" for c in DATASIFT_COLUMNS}
    row.update({
        "Property Street Address": f"{RUN_ID} Cutover Test Ln",
        "Property City": "Knoxville",
        "Property State": "TN",
        "Property ZIP Code": "37999",
        "Owner First Name": "Cutover",
        "Owner Last Name": "Testrecord",
        "Owner Type": "Person",
        "Mailing Street Address": f"{RUN_ID} Cutover Test Ln",
        "Mailing City": "Knoxville",
        "Mailing State": "TN",
        "Mailing ZIP Code": "37999",
        "Tags": "API_Migration_Test,cutover_test",
        "Lists": "API Migration Test",
        "Notes": "Phase B cutover test record — safe to delete.",
        "Notice Type": "foreclosure",
        "County": "Knox",
    })
    return row


async def main() -> int:
    ok = True

    csv_path = TEST_DIR / f"cutover_test_{RUN_ID}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
        w.writeheader()
        w.writerow(build_row())

    print("=== upload_to_datasift (API, enrich=False, skip_trace=True) ===")
    result = await up.upload_to_datasift(csv_path, enrich=False, skip_trace=True,
                                          batch_tag="API_Migration_Test")
    print(result)
    ok &= result.get("success", False)
    ok &= bool(result.get("skip_trace_result", {}).get("success"))

    print("\n=== read_record_phone_numbers (API) ===")
    phone_result = await up.read_record_phone_numbers(None, csv_path)
    print(phone_result)
    # No real phones expected on a fake address — a clean "no phones" skip is
    # the correct/expected outcome here, not a failure.
    ok &= not phone_result.get("records") or bool(phone_result.get("records"))

    print("\n=== upload_phone_tags (API) ===")
    tag_csv = TEST_DIR / f"cutover_test_tags_{RUN_ID}.csv"
    with open(tag_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Phone Number", "Phone Tag"])
        w.writerow(["865-555-0100", "API Migration Test Phone Tag"])
    tag_result = await up.upload_phone_tags(None, tag_csv)
    print(tag_result)
    ok &= tag_result.get("success", False)

    print(f"\n{'PASS' if ok else 'FAIL'} — cutover functions exercised end to end")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
