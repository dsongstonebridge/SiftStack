"""One-off rerun of datasift_ready_preprobate_obituary_2026-07-01_084300.csv.

Re-fetches the same 23 obituaries (13 Echovita by direct URL, 10 funeral-home-direct
by name match against each home's current listing) and re-runs them through the
current Assessor lookup + DM resolution logic (main.resolve_obit_leads), so the
output reflects every pipeline fix made since 2026-07-01 (Assessor matcher surname
gate + DM cross-validation, DM extraction fixes, leading-first-name score penalty).

Per user instruction (2026-07-11): skip Zillow, skip Tracerfy, do NOT upload to
DataSift -- writes local CSVs only, for manual review/upload.

Funeral-home-direct obituaries have no stored detail URL in the original CSV
(only decedent name + funeral home), so those are matched by name against each
home's current listing. If a home no longer lists an obituary from 11 days ago,
that record is logged as missing rather than silently dropped.
"""

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests

import config
import tulsa_obituary_scraper as tos
from data_formatter import write_csv
from datasift_formatter import write_datasift_csv
from enrichment_pipeline import run_enrichment_pipeline, PipelineOptions
from funeral_home_scraper import scrape_funeral_home, FUNERAL_HOMES
from main import resolve_obit_leads

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ---- Target list: the 23 records from datasift_ready_preprobate_obituary_2026-07-01_084300.csv ----

ECHOVITA_TARGETS = [
    ("Doyle Barry Kerr", "https://www.echovita.com/us/obituaries/ok/tulsa/doyle-kerr-21728916"),
    ("Daniel Owen Berg", "https://www.echovita.com/us/obituaries/ok/tulsa/daniel-berg-21717775"),
    ("Michael Aldi Apostolides", "https://www.echovita.com/us/obituaries/ok/tulsa/michael-apostolides-21707569"),
    ("Willa M Robinson", "https://www.echovita.com/us/obituaries/ok/tulsa/willa-robinson-21703704"),
    ("Charles Martin Gunkel", "https://www.echovita.com/us/obituaries/ok/tulsa/charles-gunkel-21694762"),
    ("Kay Robinson", "https://www.echovita.com/us/obituaries/ok/tulsa/kay-robinson-21693776"),
    ("Ronald Hall", "https://www.echovita.com/us/obituaries/ok/tulsa/ronald-hall-21683150"),
    ("James Henry Vaughn", "https://www.echovita.com/us/obituaries/ok/tulsa/james-henry-vaughn-21677814"),
    ("Judith Carolyn Hill", "https://www.echovita.com/us/obituaries/ok/tulsa/judith-hill-21677407"),
    ("Elaine Kay Arnold", "https://www.echovita.com/us/obituaries/ok/tulsa/elaine-arnold-21676541"),
    ("Greta Sue Partridge", "https://www.echovita.com/us/obituaries/ok/tulsa/greta-partridge-21674393"),
    ("Carolyn J. Gray", "https://www.echovita.com/us/obituaries/ok/tulsa/carolyn-gray-21666598"),
    ("Steven Paul Mirkin", "https://www.echovita.com/us/obituaries/ok/tulsa/steven-mirkin-21660543"),
]

FUNERAL_HOME_TARGETS = {
    "Floral Haven Funeral Home": [
        "Kim Pringle", "Shirley Creekmore", "William Schmees",
        "Barbara Whittlesey", "Barbara Lawson",
    ],
    "Moore Funeral Home": ["Linda Thompson", "Janet Stunkard"],
    "Fitzgerald Funeral Service": ["Patricia Sargent", "Michael Heald", "Jack Mcnulty"],
}


def _norm_name(name: str) -> str:
    return " ".join(name.lower().split())


def _fetch_echovita_obits() -> list[dict]:
    """Directly re-fetch the 13 known Echovita detail pages by URL.

    Bypasses the front-page listing scrape + seen-file dedup entirely --
    these obituaries are 11 days old and long off Echovita's rolling listing.
    """
    obits = []
    for name, url in ECHOVITA_TARGETS:
        logger.info("Echovita: refetching '%s' -> %s", name, url)
        try:
            resp = requests.get(url, headers=tos._HEADERS, timeout=15)
            if resp.status_code != 200:
                logger.warning("  HTTP %d for %s -- skipping", resp.status_code, name)
                continue
        except Exception as e:
            logger.warning("  fetch error for %s: %s -- skipping", name, e)
            continue

        obit = {
            "name": name, "detail_url": url, "date_of_death_raw": "", "age": None,
            "source": "echovita",
        }
        tos._parse_detail_page(resp.text, obit)
        if obit.get("is_template"):
            logger.warning("  %s: detail page now reads as an empty template -- skipping", name)
            continue
        if not obit.get("survived_by_raw", "").strip():
            logger.warning("  %s: no survived-by text on refetch -- skipping", name)
            continue
        obits.append(obit)

    logger.info("Echovita: %d/%d re-fetched successfully", len(obits), len(ECHOVITA_TARGETS))
    return obits


async def _fetch_funeral_home_obits(headless: bool = True) -> list[dict]:
    """Re-scrape the 3 funeral homes' CURRENT listing pages and pick out the
    10 target decedents by name match.

    If a home's listing only shows recent obituaries and a target has rolled
    off in the 11 days since 07-01, it won't be found -- logged explicitly.
    """
    from playwright.async_api import async_playwright

    found: list[dict] = []
    missing: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            for home in FUNERAL_HOMES:
                targets = FUNERAL_HOME_TARGETS.get(home["name"])
                if not targets:
                    continue
                target_norm = {_norm_name(t) for t in targets}

                logger.info("%s: scraping current listing (looking for %d targets)...",
                            home["name"], len(targets))
                # Empty seen dict -> scrape_funeral_home treats every listing
                # link as new and fetches its detail page (no disk state touched
                # -- we never call _save_seen on this dict).
                try:
                    results = await scrape_funeral_home(browser, home, seen={})
                except Exception as e:
                    logger.error("%s: scrape failed: %s", home["name"], e)
                    missing.extend(f"{home['name']}: {t}" for t in targets)
                    continue

                matched_names = set()
                for obit in results:
                    if _norm_name(obit["name"]) in target_norm:
                        found.append(obit)
                        matched_names.add(_norm_name(obit["name"]))

                still_missing = [t for t in targets if _norm_name(t) not in matched_names]
                if still_missing:
                    missing.extend(f"{home['name']}: {t}" for t in still_missing)
                    logger.warning("%s: %d/%d targets NOT found on current listing: %s",
                                   home["name"], len(still_missing), len(targets), still_missing)
                else:
                    logger.info("%s: all %d targets found", home["name"], len(targets))

                await asyncio.sleep(8)
        finally:
            await browser.close()

    if missing:
        total = sum(len(v) for v in FUNERAL_HOME_TARGETS.values())
        logger.warning(
            "Funeral home direct: %d/%d targets missing overall (likely rolled off "
            "the listing in the 11 days since 07-01): %s",
            len(missing), total, missing,
        )
    return found


def main() -> None:
    logger.info("=== Rerun: datasift_ready_preprobate_obituary_2026-07-01_084300.csv (23 records) ===")
    logger.info("Skip Zillow, skip Tracerfy, no DataSift upload (local CSVs only) per instruction.")

    echovita_obits = _fetch_echovita_obits()
    funeral_obits = asyncio.run(_fetch_funeral_home_obits(headless=True))

    obits = echovita_obits + funeral_obits
    logger.info("Total re-fetched: %d/23 (%d echovita, %d funeral_home_direct)",
                len(obits), len(echovita_obits), len(funeral_obits))
    if not obits:
        logger.error("Nothing re-fetched -- aborting")
        return

    property_owners = resolve_obit_leads(obits, headless=True)
    if not property_owners:
        logger.warning("No property-owning / buy-box-passing leads after resolution")
        return

    logger.info("Running enrichment (Smarty only -- Zillow + Tracerfy skipped)...")
    opts = PipelineOptions(
        skip_smarty=False,
        skip_zillow=True,
        skip_obituary=True,  # already have DOD + heirs from obituary
        skip_tax=True,
        skip_entity_filter=False,
        source_label="rerun-daily-obits",
    )
    enriched = run_enrichment_pipeline(property_owners, opts)
    if not enriched:
        logger.warning("No records after enrichment")
        return

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    raw_csv = config.OUTPUT_DIR / f"ok_obits_preprobate_rerun_{timestamp}.csv"
    write_csv(enriched, raw_csv)
    logger.info("Raw CSV: %s (%d leads)", raw_csv, len(enriched))

    ready_csv = write_datasift_csv(
        enriched, filename=f"datasift_ready_preprobate_obituary_rerun_{timestamp}.csv",
    )
    logger.info("DataSift-ready CSV: %s", ready_csv)
    logger.info("NOT uploaded to DataSift -- review and upload manually.")

    with_dm = sum(1 for n in enriched if n.decision_maker_name)
    logger.info("== Rerun Summary ==")
    logger.info("  Original file: 23 records")
    logger.info("  Re-fetched obituaries: %d/23", len(obits))
    logger.info("  Property owners confirmed: %d", len(property_owners))
    logger.info("  After enrichment: %d", len(enriched))
    logger.info("  With decision maker: %d", with_dm)


if __name__ == "__main__":
    main()
