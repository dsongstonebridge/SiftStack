"""One-off rescrape of the 6 NEEDS_RESCRAPE_OCR_FIX records in
output/datasift_ready_acclaim_2026-07-01_DMs.csv.

These 6 (Agha, Oconnell, Dargatz, Palmer, Kinkead, Huerta) were wrongly
dropped/left incomplete by a NOTICE-type OCR keyword bug that has since been
fixed (see feedback_acclaim_foreclosure_detection_fix memory, 2026-07-10):
the OCR keyword check was a redundant secondary filter on top of the reliable
structured Grantor-is-a-lender check, and Tulsa County's real document title
never literally says "Lis Pendens" -- causing real, already-lender-confirmed
foreclosures to be dropped. The fix now fails open (keeps the record, flags
needs_assessor_lookup=True) instead of dropping on a keyword miss.

All 6 were recorded 6/22-6/24/2026. Acclaim's date search has no upper bound
in the original scraper (only since_date), so this run adds/uses the new
until_date param on scrape_acclaimed() to scope the search tightly to that
3-day window instead of pulling three weeks of unrelated NOTICE filings.

Direct navigation to the stored Source URL (DocumentDetail?instrumentNumber=)
404s on the live site -- confirmed by recon 2026-07-11. It's a synthesized
fallback URL, not a real route. The only way back into a specific document is
via the search grid + row-click (captures Acclaim's internal itemId), so this
re-runs the NOTICE search rather than fetching by URL.

Per user instruction (2026-07-11): skip Smarty, skip Zillow, skip Tracerfy.
Tulsa Assessor lookup still runs (separate from Smarty) since it's how
needs_assessor_lookup records get sqft/year-built for the buy-box filter.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from acclaimed_scraper import scrape_acclaimed
from data_formatter import write_csv
from datasift_formatter import write_datasift_csv
from enrichment_pipeline import run_enrichment_pipeline, PipelineOptions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

TARGET_INSTRUMENTS = {
    "2026056944",  # Najib Agha
    "2026056946",  # M Oconnell
    "2026057838",  # Jan Dargatz
    "2026057840",  # Garee Palmer
    "2026057841",  # Kaysley Kinkead
    "2026057845",  # Robert Huerta
}


def _instrument(source_url: str) -> str:
    import re
    m = re.search(r"instrumentNumber=(\d+)", source_url or "")
    return m.group(1) if m else ""


def main() -> None:
    logger.info("=== Rescrape: 6 NEEDS_RESCRAPE_OCR_FIX records from 2026-07-01 Acclaim batch ===")
    logger.info("Window: 2026-06-22 to 2026-06-24, doc_type=NOTICE, skip Smarty/Zillow/Tracerfy")

    notices = asyncio.run(scrape_acclaimed(
        since_date="2026-06-22",
        until_date="2026-06-24",
        email=config.ACCLAIM_EMAIL,
        password=config.ACCLAIM_PASSWORD,
        county="Tulsa",
        headless=False,
        doc_types=["NOTICE"],
        max_records=100,
        verify_pdf=True,
        seen_ids={},  # isolated -- don't touch the real acclaim_seen_ids.json cache
    ))
    logger.info("Acclaim search returned %d NOTICE records in window", len(notices))

    matched = [n for n in notices if _instrument(n.source_url) in TARGET_INSTRUMENTS]
    found_instruments = {_instrument(n.source_url) for n in matched}
    missing = TARGET_INSTRUMENTS - found_instruments
    logger.info("Matched %d/%d target records", len(matched), len(TARGET_INSTRUMENTS))
    if missing:
        logger.warning("NOT found in rescrape: %s", missing)
    if not matched:
        logger.error("Nothing matched -- aborting")
        return

    for n in matched:
        logger.info("  %s | %s, %s | needs_assessor_lookup=%s | addr=%r",
                     n.owner_name, n.city, n.zip,
                     getattr(n, "needs_assessor_lookup", False), n.address)

    # Tulsa Assessor lookup -- fills sqft/year_built/etc for buy-box filter,
    # confirms/corrects address. Separate from Smarty (which we're skipping).
    tulsa_no_addr = [n for n in matched if not n.address.strip() or getattr(n, "needs_assessor_lookup", False)]
    if tulsa_no_addr:
        from tulsa_assessor import lookup_addresses_tulsa
        logger.info("Tulsa Assessor: looking up %d records...", len(tulsa_no_addr))
        found, missed_n = asyncio.run(lookup_addresses_tulsa(tulsa_no_addr))
        logger.info("Tulsa Assessor: %d found, %d not found", found, missed_n)

    before_drop = len(matched)
    matched = [n for n in matched if n.address.strip()]
    dropped_no_addr = before_drop - len(matched)
    if dropped_no_addr:
        logger.warning("Dropped %d records with no address after Assessor lookup", dropped_no_addr)
    if not matched:
        logger.error("No records with a confirmed address -- aborting")
        return

    opts = PipelineOptions(
        skip_smarty=True,
        skip_zillow=True,
        skip_tax=True,           # Knox-only feature, not applicable to Tulsa
        skip_obituary=True,      # living foreclosure owners, not deceased
        skip_ancestry=True,
        skip_entity_filter=False,
        source_label="rescrape-acclaim-ocr-fix",
    )
    enriched = run_enrichment_pipeline(matched, opts)
    if not enriched:
        logger.warning("No records after enrichment pipeline")
        return

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    raw_csv = config.OUTPUT_DIR / f"acclaim_rescrape_raw_{timestamp}.csv"
    write_csv(enriched, raw_csv)
    logger.info("Raw CSV: %s (%d records)", raw_csv, len(enriched))

    ready_csv = write_datasift_csv(
        enriched, filename=f"acclaim_rescrape_datasift_{timestamp}.csv",
    )
    logger.info("DataSift-ready CSV: %s", ready_csv)

    logger.info("== Rescrape Summary ==")
    logger.info("  Target: 6 flagged records")
    logger.info("  Matched in search: %d/6", len(matched) + dropped_no_addr)
    logger.info("  Confirmed address: %d", len(matched))
    logger.info("  After enrichment/buy-box: %d", len(enriched))
    for n in enriched:
        logger.info("    %s: %s, %s %s", n.owner_name, n.address, n.city, n.zip)


if __name__ == "__main__":
    main()
