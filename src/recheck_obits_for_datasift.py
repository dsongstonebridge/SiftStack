"""Re-verify Echovita CSV addresses + combine with funeral_direct for DataSift upload.

Steps:
1. Read preprobate (Echovita) CSV via data_formatter.read_csv()
2. For records WITH parcel_id: clear address and re-run Tulsa Assessor lookup
   with the fixed matcher (surname gate + DM cross-validation added 2026-06-30)
3. Read funeral_direct CSV, construct NoticeData objects
4. Deduplicate on decedent_name (prefer funeral_direct — richer DM data)
5. Write combined DATASIFT_COLUMNS CSV via datasift_formatter.write_datasift_csv()
   → Owner First/Last Name = DM for deceased records (DataSift skip traces DM)
"""

import asyncio
import csv
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

# Add src/ to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from data_formatter import read_csv
from datasift_formatter import write_datasift_csv
from notice_parser import NoticeData

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

PREPROBATE_CSV = config.OUTPUT_DIR / "ok_obits_preprobate_2026-06-29_140107.csv"
FUNERAL_CSV    = config.OUTPUT_DIR / "ok_obits_funeral_direct_2026-06-30.csv"


def load_funeral_direct(csv_path: Path) -> list[NoticeData]:
    """Load funeral_direct CSV and construct NoticeData objects.

    funeral_direct schema differs from SIFT_COLUMNS — build manually.
    The 'full_name' column is the decedent name (not DM), so we use
    decision_maker_name for the DM.
    """
    notices = []
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            dm_name = row.get("decision_maker_name", "").strip()
            # Derive DM first/last from the dm name
            dm_parts = dm_name.split() if dm_name else []
            dm_first = row.get("first_name", "").strip() or (dm_parts[0] if dm_parts else "")
            dm_last  = row.get("last_name", "").strip() or (" ".join(dm_parts[1:]) if len(dm_parts) > 1 else "")

            n = NoticeData(
                owner_name=row.get("decedent_name", "").strip(),  # decedent = property owner
                address=row.get("address", "").strip(),
                city=row.get("city", "").strip(),
                state=row.get("state", "OK").strip(),
                zip=row.get("zip", "").strip(),
                date_added=_normalize_date(row.get("Date Added", "")),
                notice_type=row.get("notice_type", "probate").strip(),
                county=row.get("county", "Tulsa").strip(),
                decedent_name=row.get("decedent_name", "").strip(),
                owner_deceased=row.get("owner_deceased", "yes").strip(),
                date_of_death=row.get("date_of_death", "").strip(),
                decision_maker_name=dm_name,
                decision_maker_relationship=row.get("decision_maker_relationship", "").strip(),
                dm_confidence=row.get("dm_confidence", "").strip(),
                dm_confidence_reason=row.get("dm_confidence_reason", "").strip(),
                source_url=row.get("source_url", "").strip(),
            )
            notices.append(n)

    logger.info("Loaded %d funeral_direct records", len(notices))
    return notices


def _clean_dm_name(name: str) -> str:
    """Strip joint DM secondary names and location suffixes from a DM name.

    'Tommy Goad and Michelle Zink' -> 'Tommy Goad'
    'Jim Hill of Owasso'           -> 'Jim Hill'
    """
    if not name:
        return name
    if " and " in name.lower():
        name = name[: name.lower().index(" and ")].strip()
    name = re.sub(r"\s+of\s+\S.*$", "", name, flags=re.IGNORECASE).strip()
    return name


def _normalize_date(date_str: str) -> str:
    """Convert M/D/YYYY or YYYY-MM-DD to YYYY-MM-DD."""
    if not date_str.strip():
        return ""
    # Try YYYY-MM-DD
    try:
        datetime.strptime(date_str.strip(), "%Y-%m-%d")
        return date_str.strip()
    except ValueError:
        pass
    # Try M/D/YYYY
    try:
        dt = datetime.strptime(date_str.strip(), "%m/%d/%Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return date_str.strip()


async def _recheck_assessor_addresses(notices: list[NoticeData]) -> None:
    """Re-run Tulsa Assessor lookup on records that previously got a parcel_id match.

    Clears address fields first so lookup_addresses_tulsa will re-process them.
    The fixed matcher (surname gate + DM corroboration) will catch wrong-address
    matches that the old algorithm would have accepted.
    """
    from tulsa_assessor import lookup_addresses_tulsa

    # Records with parcel_id were Assessor-matched → may have wrong addresses
    needs_recheck = [n for n in notices if n.parcel_id and n.parcel_id.strip()]
    if not needs_recheck:
        logger.info("No Assessor-matched records to recheck")
        return

    logger.info("Rechecking %d Assessor-matched addresses with fixed matcher...", len(needs_recheck))

    # Save originals for comparison and restoration
    originals = {
        id(n): (n.address, n.parcel_id, n.city, n.state, n.zip)
        for n in needs_recheck
    }

    # Clear address so lookup_addresses_tulsa will re-look them up
    for n in needs_recheck:
        n.address = ""
        n.parcel_id = ""
        n.city = n.city or "Tulsa"  # keep city hint

    found, not_found = await lookup_addresses_tulsa(needs_recheck, headless=True, delay_sec=1.0)

    # Report changes and restore Smarty-standardized addresses when parcel matches
    same_parcel = 0
    changed = 0
    flagged = 0
    for n in needs_recheck:
        orig_addr, orig_pid, orig_city, orig_state, orig_zip = originals[id(n)]
        new_addr = n.address or ""
        new_pid  = n.parcel_id or ""

        if not new_addr:
            logger.warning(
                "  CLEARED: %s (was '%s' pid=%s) — new matcher found no match",
                n.decedent_name or n.owner_name, orig_addr, orig_pid,
            )
            flagged += 1

        elif new_pid == orig_pid:
            # Same parcel — address difference is just Assessor vs Smarty formatting.
            # Restore original Smarty-standardized address (better for DataSift upload).
            n.address = orig_addr
            n.city    = orig_city
            n.state   = orig_state
            n.zip     = orig_zip
            n.parcel_id = orig_pid
            same_parcel += 1
            logger.info("  OK (same parcel): %s '%s'", n.decedent_name or n.owner_name, orig_addr)

        else:
            # Different parcel = real address change (old matcher was wrong)
            logger.warning(
                "  CHANGED: %s '%s' -> '%s' (pid %s -> %s)",
                n.decedent_name or n.owner_name,
                orig_addr, new_addr, orig_pid, new_pid,
            )
            changed += 1

    logger.info(
        "Recheck complete: %d/%d same parcel (Smarty addresses kept), "
        "%d address changed, %d cleared (no new match)",
        same_parcel, len(needs_recheck), changed, flagged,
    )


async def _reverse_verify_people_search(notices: list[NoticeData]) -> tuple[int, int]:
    """Verify people-search addresses by reverse-looking up each in the Assessor.

    Takes records that got addresses from Tier 6 (people search, no parcel_id).
    Searches the Assessor by street address, checks if any returned owner's
    surname matches the decedent's surname.

    Confirmed → parcel_id + tax_owner_name populated.
    Unverified → address cleared so the record is dropped by the post-step filter
    (likely a rental property or address in another county).

    Returns (confirmed_count, unverified_count).
    """
    from tulsa_assessor import _search_assessor, _extract_surname
    from playwright.async_api import async_playwright

    targets = [n for n in notices if not (n.parcel_id and n.parcel_id.strip())]
    if not targets:
        logger.info("People-search reverse verify: no records to check")
        return 0, 0

    logger.info(
        "People-search reverse verify: checking %d addresses against Assessor...",
        len(targets),
    )

    confirmed = unverified = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        context.set_default_timeout(20_000)
        page = await context.new_page()

        for i, n in enumerate(targets, 1):
            decedent = (n.decedent_name or n.owner_name or "").strip()
            # Decedent surname = last word of the decedent's name
            decedent_parts = decedent.split()
            decedent_surname = decedent_parts[-1].upper() if decedent_parts else ""

            # Search Assessor by street address only (drop city/state/zip)
            street = n.address.strip()
            logger.info("[%d/%d] %s: searching '%s'", i, len(targets), decedent, street)

            results = await _search_assessor(page, street)

            match_found = False
            for r in results:
                owner_surname = _extract_surname(r.get("owner_name", "")).upper()
                if not owner_surname or not decedent_surname:
                    continue
                if owner_surname == decedent_surname:
                    # Surname match — this property belongs to the decedent
                    n.parcel_id      = r.get("account_no", "")
                    n.tax_owner_name = r.get("owner_name", "")
                    # Update address to Assessor's canonical format
                    if r.get("street"):
                        n.address = r["street"]
                        n.city    = r.get("city") or n.city
                        n.zip     = r.get("zip") or n.zip
                    # Clear any prior unverified flag
                    flags = n.missing_data_flags or ""
                    n.missing_data_flags = flags.replace("unverified_address", "").strip(",").strip()
                    match_found = True
                    confirmed += 1
                    logger.info(
                        "  CONFIRMED: owner '%s' surname matches decedent '%s' (parcel %s)",
                        r["owner_name"], decedent, n.parcel_id,
                    )
                    break

            if not match_found:
                # No Assessor ownership confirmed — clear address so the record
                # gets dropped downstream (likely rental or out-of-county)
                owner_names = [r.get("owner_name", "") for r in results[:3]]
                if owner_names:
                    logger.warning(
                        "  DROP: '%s' @ '%s' -- Assessor shows %s (no surname match for '%s')",
                        decedent, street, owner_names, decedent_surname,
                    )
                else:
                    logger.warning(
                        "  DROP: '%s' @ '%s' -- Assessor returned no results (out-of-county or bad address)",
                        decedent, street,
                    )
                n.address = ""
                unverified += 1

            if i < len(targets):
                await asyncio.sleep(1.0)

        await browser.close()

    logger.info(
        "People-search reverse verify: %d confirmed (Assessor ownership proven), "
        "%d dropped (rental/out-of-county -- address cleared)",
        confirmed, unverified,
    )
    return confirmed, unverified


def _build_tags(n: NoticeData) -> str:
    """Build DataSift tag string for combined obituary records."""
    tags = [
        "Courthouse Data",
        "Pre-Probate",
        "obituary",
        (n.county or "tulsa").lower(),
        datetime.now().strftime("%Y-%m"),
        "deceased",
    ]
    if n.dm_confidence:
        tags.append(f"{n.dm_confidence}_confidence")
    # Source tag
    src = (n.source_url or "").lower()
    if "echovita" in src:
        tags.append("echovita")
    elif "funeral_home_direct" in src:
        tags.append("funeral_home_direct")
    return ",".join(tags)


def main() -> None:
    # ── Step 1: Load Echovita preprobate CSV ──
    if not PREPROBATE_CSV.exists():
        logger.error("Preprobate CSV not found: %s", PREPROBATE_CSV)
        sys.exit(1)

    echovita_notices = read_csv(PREPROBATE_CSV)
    logger.info("Loaded %d Echovita/preprobate records", len(echovita_notices))

    # ── Step 2a: Re-check Assessor addresses for records with parcel_id ──
    asyncio.run(_recheck_assessor_addresses(echovita_notices))

    # Drop records where re-check found no valid address
    valid_echovita = [n for n in echovita_notices if n.address and n.address.strip()]
    dropped = len(echovita_notices) - len(valid_echovita)
    if dropped:
        logger.warning("Dropped %d Echovita records with no verified address after recheck", dropped)

    # ── Step 2b: Reverse-verify the 10 people-search addresses ──
    # These have no parcel_id yet — look up each address in the Assessor to
    # confirm the decedent actually owned that property in Tulsa County.
    # Records where the Assessor shows a different owner (rental) or no result
    # (out-of-county) have their address cleared and are dropped below.
    asyncio.run(_reverse_verify_people_search(valid_echovita))

    # Drop people-search records that failed Assessor reverse-verify
    before = len(valid_echovita)
    valid_echovita = [n for n in valid_echovita if n.address and n.address.strip()]
    people_search_dropped = before - len(valid_echovita)
    if people_search_dropped:
        logger.warning(
            "Dropped %d people-search records: Assessor found no matching ownership "
            "(likely renters or out-of-county addresses)",
            people_search_dropped,
        )

    # ── Step 3: Load funeral_direct CSV ──
    if not FUNERAL_CSV.exists():
        logger.error("Funeral direct CSV not found: %s", FUNERAL_CSV)
        sys.exit(1)

    funeral_notices = load_funeral_direct(FUNERAL_CSV)

    # ── Step 3b: Reverse-verify funeral_direct addresses against Assessor ──
    # The funeral_direct CSV has no parcel_id column — we don't know whether
    # addresses came from Assessor name-search (Tiers 1-5, ownership confirmed)
    # or people search (Tier 6, potentially renters or out-of-county).
    # Run the same ownership check we did on Echovita people-search records.
    asyncio.run(_reverse_verify_people_search(funeral_notices))
    before_funeral = len(funeral_notices)
    funeral_notices = [n for n in funeral_notices if n.address and n.address.strip()]
    funeral_dropped = before_funeral - len(funeral_notices)
    if funeral_dropped:
        logger.warning(
            "Dropped %d funeral_direct records: Assessor found no matching ownership",
            funeral_dropped,
        )

    # ── Step 4: Deduplicate (prefer funeral_direct — richer DM data) ──
    funeral_decedents = {
        (n.decedent_name or "").strip().lower()
        for n in funeral_notices
        if (n.decedent_name or "").strip()
    }

    dupes = 0
    filtered_echovita = []
    for n in valid_echovita:
        key = (n.decedent_name or n.owner_name or "").strip().lower()
        if key and key in funeral_decedents:
            logger.info("  DEDUP: '%s' in both sources — keeping funeral_direct", key)
            dupes += 1
        else:
            filtered_echovita.append(n)

    combined = funeral_notices + filtered_echovita

    logger.info(
        "\nMerge summary:\n"
        "  Echovita (Assessor-confirmed):        %d\n"
        "  Funeral direct (Assessor-confirmed):  %d\n"
        "  Duplicates removed:                   %d\n"
        "  Combined before DM cleanup:           %d",
        len(valid_echovita), len(funeral_notices), dupes, len(combined),
    )

    # ── Step 4b: Clean DM names ──
    # Strip secondary joint DM names ("and [Name]") and location suffixes
    # ("of [City]") so DataSift skip-traces the correct single person.
    # Then drop records where the cleaned DM has no last name (single-word
    # names like "Ethan" produce empty Owner Last Name → skip trace finds nothing).
    cleaned_combined = []
    dm_name_issues = []
    for n in combined:
        cleaned = _clean_dm_name(n.decision_maker_name or "")
        if cleaned != (n.decision_maker_name or ""):
            logger.info(
                "  DM cleaned: '%s' -> '%s' (%s)",
                n.decision_maker_name, cleaned, n.decedent_name or n.owner_name,
            )
        n.decision_maker_name = cleaned
        # Require at least 2 name tokens (first + last) for a useful skip trace
        if len(cleaned.split()) < 2:
            dm_name_issues.append((n.decedent_name or n.owner_name, cleaned))
            logger.warning(
                "  DROP (single-name DM '%s'): %s -- skip trace needs first+last",
                cleaned, n.decedent_name or n.owner_name,
            )
        else:
            cleaned_combined.append(n)

    if dm_name_issues:
        logger.warning(
            "Dropped %d records with single-name DMs: %s",
            len(dm_name_issues),
            [name for _, name in dm_name_issues],
        )
    combined = cleaned_combined

    # ── Step 5: Write DATASIFT_COLUMNS format CSV ──
    # Apply tags to each record (tags are usually set during the pipeline run)
    for n in combined:
        # Set notice_type for DataSift list mapping (probate → "Probate" list)
        if not n.notice_type:
            n.notice_type = "probate"
        # Apply the combined pre-probate tags if not already set
        # (funeral_direct had Tags column but NoticeData doesn't store it directly)
        # The datasift_formatter._build_tags() will build tags from NoticeData fields

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_filename = f"datasift_combined_preprobate_{timestamp}.csv"
    out_path = write_datasift_csv(combined, filename=out_filename)
    logger.info("\nDataSift-ready CSV written: %s", out_path)
    logger.info("Upload this file to DataSift manually.")
    logger.info("Owner First/Last Name = DM name for deceased records (DataSift will skip-trace DM).")


if __name__ == "__main__":
    main()
