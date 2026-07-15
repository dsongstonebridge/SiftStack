"""One-off: run the Tulsa Assessor lookup on the 6 verified-clean Acclaim
foreclosure records (Timmons, Keeton, Burkart, Richards, Pitton, S. Johnson)
to backfill property characteristics (sqft, year built, bathrooms, structure
type), then apply the real buy-box filter. These 6 already have a confirmed
address + parcel ID from the original 07-01 scrape -- they were never
missing data, just never checked against the buy box (that only ran on the
OCR-bug-flagged 6, which are a separate, still-blocked rescrape).

Uses the Tulsa County Assessor site directly -- unrelated to the Acclaim
subscription that's currently blocking the other rescrape.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import csv

import config
from data_formatter import write_csv
from datasift_formatter import write_datasift_csv
from enrichment_pipeline import filter_buy_box
from notice_parser import NoticeData
from tulsa_assessor import lookup_addresses_tulsa

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

SRC_CSV = config.OUTPUT_DIR / "datasift_ready_acclaim_2026-07-01_DMs_clean6_2026-07-11_132913.csv"


def load_clean6() -> list[NoticeData]:
    notices = []
    with open(SRC_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            n = NoticeData(
                date_added=row.get("Date Added", ""),
                owner_name=f"{row.get('Owner First Name', '')} {row.get('Owner Last Name', '')}".strip(),
                address=row.get("Property Street Address", ""),
                city=row.get("Property City", ""),
                state=row.get("Property State", "OK"),
                zip=row.get("Property ZIP Code", ""),
                notice_type=row.get("Notice Type", "foreclosure"),
                county=row.get("County", "Tulsa"),
                parcel_id=row.get("Parcel ID", ""),
                source_url=row.get("Source URL", ""),
            )
            n.needs_assessor_lookup = True  # force lookup_addresses_tulsa to include it
            notices.append(n)
    return notices


def main() -> None:
    notices = load_clean6()
    logger.info("Loaded %d clean records, running Tulsa Assessor lookup...", len(notices))

    found, not_found = asyncio.run(lookup_addresses_tulsa(notices, headless=True))
    logger.info("Assessor: %d found, %d not found", found, not_found)

    for n in notices:
        logger.info(
            "  %s | %s, %s %s | type=%s sqft=%s year=%s baths=%s",
            n.owner_name, n.address, n.city, n.zip,
            n.property_type, n.sqft, n.year_built, n.bathrooms,
        )

    passed = filter_buy_box(notices)
    rejected = [n for n in notices if n not in passed]

    logger.info("== Buy Box Result ==")
    logger.info("  Passed: %d/%d", len(passed), len(notices))
    for n in passed:
        logger.info("    PASS: %s -- %s, %s | %s sqft=%s year=%s baths=%s",
                     n.owner_name, n.address, n.city, n.property_type, n.sqft, n.year_built, n.bathrooms)
    for n in rejected:
        logger.info("    REJECT: %s -- %s, %s | %s sqft=%s year=%s baths=%s",
                     n.owner_name, n.address, n.city, n.property_type, n.sqft, n.year_built, n.bathrooms)

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    raw_csv = config.OUTPUT_DIR / f"acclaim_clean6_assessor_raw_{timestamp}.csv"
    write_csv(notices, raw_csv)
    logger.info("Raw CSV (all 6, with Assessor data): %s", raw_csv)


if __name__ == "__main__":
    main()
