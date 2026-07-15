"""Run Tracerfy + Trestle on a DataSift export CSV, then write an updated CSV.

Workflow:
1. Read DataSift export (standard wide format: Phone 1-30, Email 1-10, Phone Tags 1-30)
2. Build NoticeData for each row (DM = First Name + Last Name)
3. Run Tracerfy batch skip trace on all records to find additional phones/emails
4. Merge Tracerfy results into empty Phone N / Email N slots (no overwrites)
5. Run Trestle phone_intel on ALL phones (DataSift + Tracerfy), scoring each one
6. Write Phone Tags N and Phone Type N back into the export rows
7. Output updated CSV + phone_tags_for_datasift.csv (for DataSift Tag Phones upload)

Usage:
    python src/run_datasift_export_trace.py
    python src/run_datasift_export_trace.py --csv output/SomeOtherExport.csv
    python src/run_datasift_export_trace.py --skip-tracerfy   # Trestle only
    python src/run_datasift_export_trace.py --batch-size 3    # Trestle batch size
"""

import argparse
import csv
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from notice_parser import NoticeData
from phone_validator import (
    DEFAULT_TIERS,
    COST_PER_PHONE,
    assign_tier,
    call_trestle,
    clean_phone,
    detect_phone_columns,
)
from tracerfy_skip_tracer import batch_skip_trace

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

_TRACERFY_PHONE_FIELDS = [
    "primary_phone", "mobile_1", "mobile_2", "mobile_3",
    "mobile_4", "mobile_5", "landline_1", "landline_2", "landline_3",
]
_TRACERFY_EMAIL_FIELDS = ["email_1", "email_2", "email_3", "email_4", "email_5"]


def _row_to_notice(row: dict) -> NoticeData:
    """Build a minimal NoticeData from a DataSift export row for Tracerfy."""
    first = row.get("First Name", "").strip()
    last = row.get("Last Name", "").strip()
    name = f"{first} {last}".strip()

    # Mailing address preferred (DM's own address), fall back to property
    address = (row.get("Mailing address") or row.get("Property address") or "").strip()
    city    = (row.get("Mailing city")    or row.get("Property city")    or "").strip()
    state   = (row.get("Mailing state")   or row.get("Property state")   or "").strip()
    zip_    = (row.get("Mailing zip5")    or row.get("Property zip5")    or "").strip()

    return NoticeData(
        owner_name=name,
        decision_maker_name=name,
        owner_deceased="yes",
        address=address,
        city=city,
        state=state or "OK",
        zip=zip_,
        notice_type="probate",
        county=row.get("Property county", "Tulsa").strip() or "Tulsa",
        # Intentionally leave phone fields blank so Tracerfy traces all records
    )


def _merge_tracerfy_results(row: dict, notice: NoticeData) -> tuple[int, int]:
    """Add Tracerfy phones/emails to empty slots in the export row.

    Never overwrites existing DataSift phones. Returns (phones_added, emails_added).
    """
    phones_added = emails_added = 0

    # Collect existing phones (cleaned) to deduplicate
    existing_phones: set[str] = set()
    for i in range(1, 31):
        p = clean_phone(row.get(f"Phone {i}", ""))
        if p:
            existing_phones.add(p)

    def _next_phone_slot() -> int | None:
        for i in range(1, 31):
            if not row.get(f"Phone {i}", "").strip():
                return i
        return None

    for field in _TRACERFY_PHONE_FIELDS:
        val = (getattr(notice, field, "") or "").strip()
        if not val:
            continue
        cleaned = clean_phone(val)
        if not cleaned or cleaned in existing_phones:
            continue
        slot = _next_phone_slot()
        if slot is None:
            break
        row[f"Phone {slot}"] = cleaned
        existing_phones.add(cleaned)
        phones_added += 1

    # Collect existing emails
    existing_emails: set[str] = set()
    for i in range(1, 11):
        e = row.get(f"Email {i}", "").strip().lower()
        if e:
            existing_emails.add(e)

    def _next_email_slot() -> int | None:
        for i in range(1, 11):
            if not row.get(f"Email {i}", "").strip():
                return i
        return None

    for field in _TRACERFY_EMAIL_FIELDS:
        val = (getattr(notice, field, "") or "").strip()
        if not val or val.lower() in existing_emails:
            continue
        slot = _next_email_slot()
        if slot is None:
            break
        row[f"Email {slot}"] = val
        existing_emails.add(val.lower())
        emails_added += 1

    return phones_added, emails_added


def _run_trestle_on_rows(
    rows: list[dict],
    api_key: str,
    batch_size: int,
) -> dict[str, dict]:
    """Score every phone across all rows via Trestle. Returns {cleaned_phone: info}."""
    # Collect all unique phones
    all_phones: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for i in range(1, 31):
            cleaned = clean_phone(row.get(f"Phone {i}", ""))
            if cleaned and cleaned not in seen:
                all_phones.append(cleaned)
                seen.add(cleaned)

    if not all_phones:
        logger.warning("No phones found to score")
        return {}

    logger.info(
        "Trestle: scoring %d unique phones (~$%.2f)",
        len(all_phones), len(all_phones) * COST_PER_PHONE,
    )

    # Load cache
    cache_path = config.OUTPUT_DIR / "phone_validation" / "trestle_scored_cache.json"
    import json
    scored_cache: dict = {}
    if cache_path.exists():
        try:
            scored_cache = json.loads(cache_path.read_text(encoding="utf-8"))
            logger.info("  Cache hit: %d previously scored phones loaded",
                        sum(1 for p in all_phones if p in scored_cache))
        except Exception:
            pass

    results: dict[str, dict] = {}
    to_score = [p for p in all_phones if p not in scored_cache]

    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    for batch_start in range(0, len(to_score), batch_size):
        batch = to_score[batch_start: batch_start + batch_size]
        with ThreadPoolExecutor(max_workers=min(batch_size, len(batch))) as ex:
            futures = {ex.submit(call_trestle, ph, api_key): ph for ph in batch}
            for fut in as_completed(futures):
                ph = futures[fut]
                try:
                    data = fut.result()
                except Exception as e:
                    logger.debug("Trestle error on %s: %s", ph, e)
                    continue
                if "error" in data and not data.get("is_valid"):
                    if data.get("error") == "Invalid API key":
                        logger.error("Invalid Trestle API key — aborting")
                        return results
                    continue
                score = data.get("activity_score")
                line_type = data.get("line_type", "")
                tier = assign_tier(score, DEFAULT_TIERS)
                results[ph] = {"score": score, "tier": tier, "line_type": line_type or ""}
                logger.debug("  %s: score=%s tier=%s type=%s", ph, score, tier, line_type)

        if batch_start + batch_size < len(to_score):
            time.sleep(0.5)

    # Merge new into cache
    all_scored = {**scored_cache}
    for ph, info in results.items():
        all_scored[ph] = {
            "phone_number": ph,
            "activity_score": info["score"],
            "assigned_tag": info["tier"],
            "line_type": info["line_type"],
        }
    for ph in all_phones:
        if ph in scored_cache and ph not in results:
            results[ph] = {
                "score": scored_cache[ph].get("activity_score"),
                "tier": scored_cache[ph].get("assigned_tag", ""),
                "line_type": scored_cache[ph].get("line_type", ""),
            }

    # Save cache
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(all_scored, indent=2), encoding="utf-8")
        logger.info("  Trestle cache updated: %d total phones", len(all_scored))
    except Exception as e:
        logger.warning("Could not save Trestle cache: %s", e)

    logger.info(
        "Trestle complete: %d scored now, %d from cache, %d total",
        len(results) - sum(1 for p in all_phones if p in scored_cache and p not in to_score),
        sum(1 for p in all_phones if p in scored_cache),
        len(results),
    )
    return results


def _apply_trestle_tags(rows: list[dict], scores: dict[str, dict]) -> int:
    """Write Trestle tier tags + line type into Phone Tags N / Phone Type N columns."""
    updated = 0
    for row in rows:
        for i in range(1, 31):
            cleaned = clean_phone(row.get(f"Phone {i}", ""))
            if not cleaned:
                continue
            info = scores.get(cleaned)
            if not info:
                continue
            old_tag = row.get(f"Phone Tags {i}", "").strip()
            new_tag = info["tier"]
            if old_tag != new_tag:
                row[f"Phone Tags {i}"] = new_tag
                updated += 1
            if info.get("line_type"):
                row[f"Phone Type {i}"] = info["line_type"]
    return updated


def _write_datasift_phone_tags(rows: list[dict], scores: dict[str, dict], out_dir: Path) -> Path:
    """Write phone_tags_for_datasift.csv for DataSift 'Tag phones by phone number' upload."""
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    path = out_dir / f"phone_tags_for_datasift_{today}.csv"

    written: set[str] = set()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Phone Number", "Phone Tag"])
        for row in rows:
            for i in range(1, 31):
                cleaned = clean_phone(row.get(f"Phone {i}", ""))
                if cleaned and cleaned in scores and cleaned not in written:
                    w.writerow([cleaned, scores[cleaned]["tier"]])
                    written.add(cleaned)

    logger.info("Phone tags CSV written: %s (%d phones)", path, len(written))
    return path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv", default=None,
        help="Path to DataSift export CSV (default: output/Obituary Data07012026.csv)",
    )
    parser.add_argument("--skip-tracerfy", action="store_true", help="Skip Tracerfy, run Trestle only")
    parser.add_argument("--batch-size", type=int, default=3, help="Trestle concurrent requests (default 3)")
    args = parser.parse_args(argv)

    csv_path = Path(args.csv) if args.csv else config.OUTPUT_DIR / "Obituary Data07012026.csv"
    if not csv_path.exists():
        logger.error("Export CSV not found: %s", csv_path)
        sys.exit(1)

    # ── Read export ──
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    logger.info("Loaded %d records from %s", len(rows), csv_path.name)

    # ── Step 1: Tracerfy batch skip trace ──
    if not args.skip_tracerfy:
        if not config.TRACERFY_API_KEY:
            logger.warning("TRACERFY_API_KEY not set — skipping Tracerfy")
        else:
            notices = [_row_to_notice(r) for r in rows]
            est_cost = len(notices) * 0.02
            logger.info("Tracerfy: submitting %d records (~$%.2f)...", len(notices), est_cost)

            stats = batch_skip_trace(
                notices,
                max_signing_traces=1,
                lookup_heir_addresses=False,
            )

            total_new_phones = total_new_emails = 0
            for row, notice in zip(rows, notices):
                p, e = _merge_tracerfy_results(row, notice)
                total_new_phones += p
                total_new_emails += e

            logger.info(
                "Tracerfy complete: %d matched, %d new phones merged, %d new emails merged "
                "(cost $%.2f)",
                stats.get("matched", 0), total_new_phones, total_new_emails,
                stats.get("cost", 0.0),
            )
    else:
        logger.info("Tracerfy skipped (--skip-tracerfy)")

    # ── Step 2: Trestle phone scoring ──
    if not config.TRESTLE_API_KEY:
        logger.warning("TRESTLE_API_KEY not set — skipping Trestle scoring")
        scores: dict[str, dict] = {}
    else:
        scores = _run_trestle_on_rows(rows, config.TRESTLE_API_KEY, args.batch_size)
        tags_updated = _apply_trestle_tags(rows, scores)
        logger.info("Trestle: %d Phone Tags N cells updated", tags_updated)

    # ── Step 3: Write updated CSV ──
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    stem = csv_path.stem.replace(" ", "_")
    out_csv = config.OUTPUT_DIR / f"{stem}_updated_{timestamp}.csv"

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    logger.info("Updated CSV written: %s", out_csv)

    # ── Step 4: Phone tags CSV for DataSift upload ──
    if scores:
        tags_path = _write_datasift_phone_tags(rows, scores, config.OUTPUT_DIR / "phone_validation")
        logger.info("Upload %s to DataSift via Update Data -> Tag phones by phone number", tags_path.name)

    # ── Summary ──
    from collections import Counter
    if scores:
        tier_counts = Counter(info["tier"] for info in scores.values())
        logger.info("\nTier breakdown:")
        for tier_name in DEFAULT_TIERS:
            logger.info("  %-20s %d", tier_name, tier_counts.get(tier_name, 0))


if __name__ == "__main__":
    main()
