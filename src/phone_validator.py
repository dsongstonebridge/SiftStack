"""Phone validation via Trestle's phone_intel API with DataSift phone tag output.

Validates phone numbers from a DataSift Phone Enrichment export (Phone 1-30 columns),
scores each number by activity (0-100), assigns tier tags (Dial First through Drop),
and produces a two-column CSV for DataSift's "Update Data → Tag phones by phone number"
upload workflow.

General-purpose — works on any DataSift records, not tied to the scraping pipeline.

Usage:
    # As a module (called from main.py phone-validate subcommand)
    from phone_validator import estimate_cost, run_phone_validation

    # Estimate only
    est = estimate_cost("Phone Enrichment.csv")

    # Full validation
    results = run_phone_validation("Phone Enrichment.csv", api_key, output_dir)
"""

import csv
import json
import logging
import os
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

import config

logger = logging.getLogger(__name__)

# ── Tier Configuration ────────────────────────────────────────────────────

DEFAULT_TIERS = {
    "Dial First":  (81, 100),
    "Dial Second": (61, 80),
    "Dial Third":  (41, 60),
    "Dial Fourth": (21, 40),
    "Drop":        (0, 20),
}

COST_PER_PHONE = 0.015  # Trestle phone_intel pricing

# ── Trestle API Config ────────────────────────────────────────────────────

TRESTLE_ENDPOINT = "https://api.trestleiq.com/3.0/phone_intel"
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5  # seconds, multiplied each retry


# ── Phone Number Cleaning ─────────────────────────────────────────────────


def clean_phone(raw: str) -> str:
    """Strip a phone string down to digits, normalize to 10-digit US format."""
    if not raw:
        return ""
    digits = re.sub(r"[^\d]", "", str(raw).strip())
    # Handle 11-digit with leading 1 (country code)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    # Handle E.164 with +1
    if len(digits) > 10 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


# ── Trestle API Caller ────────────────────────────────────────────────────


def call_trestle(phone: str, api_key: str, add_litigator: bool = False) -> dict:
    """Call Trestle phone_intel API for a single phone number.

    Returns the parsed JSON response or an error dict.
    """
    params = {"phone": phone}
    if add_litigator:
        params["add_ons"] = "litigator_checks"

    headers = {
        "x-api-key": api_key,
        "Accept": "application/json",
    }

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(
                TRESTLE_ENDPOINT,
                params=params,
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                wait = RETRY_BACKOFF * (2 ** attempt)
                logger.debug("Rate limited on %s, waiting %.1fs...", phone, wait)
                time.sleep(wait)
                continue
            elif resp.status_code == 403:
                return {"error": "Invalid API key", "phone_number": phone}
            else:
                return {
                    "error": f"HTTP {resp.status_code}",
                    "phone_number": phone,
                    "detail": resp.text[:200],
                }
        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
                continue
            return {"error": "Timeout after retries", "phone_number": phone}
        except requests.exceptions.RequestException as e:
            return {"error": str(e), "phone_number": phone}

    return {"error": "Max retries exceeded", "phone_number": phone}


# ── Tier Assignment ───────────────────────────────────────────────────────


def assign_tier(score: int | None, tiers: dict) -> str:
    """Given an activity score, return the matching tier tag name."""
    if score is None:
        return "Unknown"
    for tag_name, (low, high) in tiers.items():
        if low <= score <= high:
            return tag_name
    return "Unknown"


# ── CSV Detection & Reading ──────────────────────────────────────────────


def detect_phone_columns(headers: list[str]) -> list[str]:
    """Find all columns that contain phone numbers.

    Handles the DataSift wide export format (Phone 1 through Phone 30) as well
    as simpler formats with a single Phone or Phone Number column.
    Excludes metadata columns like Phone Type N, Phone Status N, etc.
    """
    found = []
    metadata_re = re.compile(
        r"phone\s*(type|status|tags?|is\s*connected)\s*\d*",
        re.IGNORECASE,
    )

    for header in headers:
        lower = header.strip().lower()

        # Skip metadata columns
        if metadata_re.match(lower):
            continue

        # Match numbered phone columns: "Phone 1" through "Phone 30"
        if re.match(r"^phone[\s_]?\d+$", lower):
            found.append(header)
            continue

        # Match generic phone column names
        if lower in (
            "phone", "phone_number", "phone number", "phonenumber",
            "mobile", "cell", "landline", "home phone", "work phone",
            "contact phone", "primary phone",
        ):
            found.append(header)
            continue

    return found


def read_phones_from_csv(filepath: str | Path) -> tuple[list[tuple[str, str]], int, int]:
    """Read phone numbers from a CSV file.

    Returns:
        (phones_list, unique_count, total_entries)
        - phones_list: list of (raw_phone, cleaned_phone) tuples
        - unique_count: number of unique cleaned phone numbers
        - total_entries: total phone entries found (before dedup)
    """
    phones = []
    seen = set()

    with open(filepath, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        columns = detect_phone_columns(headers)

        if not columns:
            logger.error("No phone columns detected in headers: %s", headers[:20])
            return [], 0, 0

        logger.info("Detected %d phone column(s): %s%s",
                     len(columns), columns[0],
                     f" through {columns[-1]}" if len(columns) > 1 else "")

        for row in reader:
            for col in columns:
                raw = row.get(col, "").strip()
                if raw:
                    cleaned = clean_phone(raw)
                    if cleaned:
                        phones.append((raw, cleaned))
                        seen.add(cleaned)

    return phones, len(seen), len(phones)


# ── Cost Estimation ──────────────────────────────────────────────────────


def estimate_cost(filepath: str | Path) -> dict:
    """Parse CSV to count unique phones and estimate Trestle API cost.

    Returns dict with stats for display or JSON output.
    """
    phones, unique_count, total_entries = read_phones_from_csv(filepath)

    cost = unique_count * COST_PER_PHONE

    return {
        "input_file": Path(filepath).name,
        "total_entries": total_entries,
        "unique_phones": unique_count,
        "duplicates_saved": total_entries - unique_count,
        "cost_per_phone": COST_PER_PHONE,
        "estimated_cost": round(cost, 2),
    }


def print_estimate(est: dict) -> None:
    """Print a formatted cost estimate to stdout."""
    print()
    print("=" * 50)
    print("  PHONE VALIDATION COST ESTIMATE")
    print("=" * 50)
    print(f"  Input file:          {est['input_file']}")
    print(f"  Total phone entries: {est['total_entries']:,}")
    print(f"  Unique phones:       {est['unique_phones']:,}")
    print(f"  Duplicates saved:    {est['duplicates_saved']:,}")
    print(f"  Cost per phone:      ${est['cost_per_phone']:.3f}")
    print(f"  -----------------------------------------")
    print(f"  ESTIMATED COST:      ${est['estimated_cost']:.2f}")
    print("=" * 50)
    print()


# ── Main Processing ──────────────────────────────────────────────────────


def process_phones(
    phones: list[tuple[str, str]],
    api_key: str,
    tiers: dict | None = None,
    add_litigator: bool = False,
    batch_size: int = 10,
    delay: float = 0.1,
) -> tuple[list[dict], list[dict]]:
    """Process all phone numbers through Trestle API.

    Args:
        phones: List of (raw, cleaned) phone tuples.
        api_key: Trestle API key.
        tiers: Tier definitions (default: DEFAULT_TIERS).
        add_litigator: Include litigator risk check.
        batch_size: Concurrent API requests per batch.
        delay: Seconds between batches.

    Returns:
        (results_list, errors_list)
    """
    if tiers is None:
        tiers = DEFAULT_TIERS

    # Deduplicate
    unique_phones = list(dict.fromkeys(p[1] for p in phones))
    total = len(unique_phones)
    logger.info("Processing %d unique phone numbers...", total)

    results = []
    errors = []
    processed = 0

    for batch_start in range(0, total, batch_size):
        batch = unique_phones[batch_start : batch_start + batch_size]

        with ThreadPoolExecutor(max_workers=min(batch_size, len(batch))) as executor:
            future_to_phone = {
                executor.submit(call_trestle, phone, api_key, add_litigator): phone
                for phone in batch
            }

            for future in as_completed(future_to_phone):
                phone = future_to_phone[future]
                processed += 1

                try:
                    data = future.result()
                except Exception as e:
                    errors.append({"phone_number": phone, "error": str(e)})
                    continue

                if "error" in data and not data.get("is_valid"):
                    if data.get("error") == "Invalid API key":
                        logger.error("Invalid Trestle API key — aborting")
                        raise ValueError("Invalid Trestle API key")
                    errors.append(data)
                    continue

                score = data.get("activity_score")
                line_type = data.get("line_type")
                tier = assign_tier(score, tiers)

                litigator_risk = None
                if add_litigator and data.get("add_ons", {}).get("litigator_checks"):
                    litigator_risk = data["add_ons"]["litigator_checks"].get(
                        "phone.is_litigator_risk", None
                    )

                results.append({
                    "phone_number": phone,
                    "activity_score": score,
                    "line_type": line_type,
                    "carrier": data.get("carrier"),
                    "is_valid": data.get("is_valid"),
                    "is_prepaid": data.get("is_prepaid"),
                    "assigned_tag": tier,
                    "is_litigator_risk": litigator_risk,
                })

                # Progress every 25 records
                if processed % 25 == 0 or processed == total:
                    pct = (processed / total) * 100
                    logger.info("  Progress: %d/%d (%.0f%%)", processed, total, pct)

        # Delay between batches
        if batch_start + batch_size < total and delay > 0:
            time.sleep(delay)

    return results, errors


# ── Per-Notice Phone Scoring (DM + heirs) ────────────────────────────────


DM_PHONE_FIELDS = [
    "primary_phone", "mobile_1", "mobile_2", "mobile_3", "mobile_4",
    "mobile_5", "landline_1", "landline_2", "landline_3",
]


def _collect_phones_from_notice(notice) -> list[str]:
    """Return all cleaned phones on a notice — DM #1 flat fields + heir_map_json."""
    out: list[str] = []
    for field in DM_PHONE_FIELDS:
        val = getattr(notice, field, "") or ""
        cleaned = clean_phone(val)
        if cleaned:
            out.append(cleaned)

    heir_json = getattr(notice, "heir_map_json", "") or ""
    if heir_json:
        try:
            heirs = json.loads(heir_json)
        except (ValueError, TypeError):
            heirs = []
        if isinstance(heirs, list):
            for h in heirs:
                if not isinstance(h, dict):
                    continue
                for ph in h.get("phones", []) or []:
                    cleaned = clean_phone(ph)
                    if cleaned:
                        out.append(cleaned)
    return out


def score_record_phones(
    notices: list,
    api_key: str | None = None,
    tiers: dict | None = None,
    add_litigator: bool = False,
    batch_size: int = 10,
    delay: float = 0.1,
) -> dict[str, dict]:
    """Trestle-score every phone attached to these notices (DM #1 + all heirs).

    Closes the coverage gap where only DM #1 phones got scored via the CSV-export
    workflow. Writes a `phone_scores` dict onto each heir in heir_map_json so
    downstream consumers (PDF, DataSift export) can surface tier badges.

    Returns a flat `{cleaned_phone: {"score": int, "tier": str, "line_type": str}}`
    dict, directly usable as the `phone_tiers` parameter of
    `report_generator.generate_record_pdf`.
    """
    key = api_key or getattr(config, "TRESTLE_API_KEY", "")
    if not key:
        logger.info("Trestle API key not set — skipping per-record phone scoring")
        return {}
    if tiers is None:
        tiers = DEFAULT_TIERS

    # Collect unique cleaned phones across all notices
    unique: dict[str, None] = {}
    for n in notices:
        for p in _collect_phones_from_notice(n):
            unique.setdefault(p, None)
    phones = list(unique.keys())
    if not phones:
        return {}

    logger.info("Trestle scoring %d unique phones across %d records (~$%.2f)",
                len(phones), len(notices), len(phones) * COST_PER_PHONE)

    results: dict[str, dict] = {}
    for batch_start in range(0, len(phones), batch_size):
        batch = phones[batch_start : batch_start + batch_size]
        with ThreadPoolExecutor(max_workers=min(batch_size, len(batch))) as executor:
            futures = {
                executor.submit(call_trestle, ph, key, add_litigator): ph
                for ph in batch
            }
            for future in as_completed(futures):
                ph = futures[future]
                try:
                    data = future.result()
                except Exception as e:
                    logger.debug("Trestle exception on %s: %s", ph, e)
                    continue
                if "error" in data and not data.get("is_valid"):
                    if data.get("error") == "Invalid API key":
                        logger.error("Invalid Trestle API key — aborting heir scoring")
                        return results
                    continue
                score = data.get("activity_score")
                line_type = data.get("line_type")
                results[ph] = {
                    "score": score,
                    "tier": assign_tier(score, tiers),
                    "line_type": line_type,
                }
        if batch_start + batch_size < len(phones) and delay > 0:
            time.sleep(delay)

    # Persist per-heir scores back into heir_map_json so downstream consumers
    # don't need the global dict to surface tier info.
    for n in notices:
        heir_json = getattr(n, "heir_map_json", "") or ""
        if not heir_json:
            continue
        try:
            heirs = json.loads(heir_json)
        except (ValueError, TypeError):
            continue
        if not isinstance(heirs, list):
            continue
        mutated = False
        for h in heirs:
            if not isinstance(h, dict):
                continue
            scores: dict[str, dict] = {}
            for ph in h.get("phones", []) or []:
                cleaned = clean_phone(ph)
                if cleaned and cleaned in results:
                    scores[ph] = results[cleaned]
            if scores:
                h["phone_scores"] = scores
                mutated = True
        if mutated:
            n.heir_map_json = json.dumps(heirs, ensure_ascii=False)

    # Set best dial tier on each notice for tag generation
    apply_trestle_tiers_to_notices(notices, results)

    # Save to cache so phone-validate skips these later
    _save_to_trestle_cache(results)

    return results


def _save_to_trestle_cache(results: dict[str, dict]) -> None:
    """Persist scored phones to the shared cache so phone-validate skips them."""
    cache_path = config.OUTPUT_DIR / "phone_validation" / "trestle_scored_cache.json"
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if cache_path.exists():
            existing = json.loads(cache_path.read_text(encoding="utf-8"))
        for phone, info in results.items():
            existing[phone] = {
                "phone_number": phone,
                "activity_score": info.get("score"),
                "assigned_tag": info.get("tier", ""),
                "line_type": info.get("line_type", ""),
            }
        cache_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        logger.info("Trestle cache updated: %d total phones", len(existing))
    except Exception as e:
        logger.warning("Could not save Trestle cache: %s", e)


def apply_trestle_tiers_to_notices(notices: list, tiers_map: dict) -> None:
    """Set _dial_tier on each notice based on its best phone's Trestle score."""
    tier_priority = {"Dial First": 1, "Dial Second": 2, "Dial Third": 3, "Dial Fourth": 4, "Drop": 5}
    for n in notices:
        phones = _collect_phones_from_notice(n)
        best_tier = None
        best_rank = 99
        for p in phones:
            info = tiers_map.get(p)
            if info and info.get("tier") in tier_priority:
                rank = tier_priority[info["tier"]]
                if rank < best_rank:
                    best_rank = rank
                    best_tier = info["tier"]
        n._dial_tier = best_tier or ""


# ── Output Writers ────────────────────────────────────────────────────────


def write_datasift_tags_csv(results: list[dict], output_dir: str | Path) -> Path:
    """Write the DataSift-ready phone tags CSV (Phone Number + Phone Tag).

    This is the file uploaded to DataSift via "Update Data → Tag phones by phone number".
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    filepath = output_dir / f"phone_tags_for_datasift_{today}.csv"

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Phone Number", "Phone Tag"])
        for r in results:
            if r.get("is_valid") is not False:
                writer.writerow([r["phone_number"], r["assigned_tag"]])

    logger.info("DataSift phone tags CSV: %s (%d phones)", filepath, len(results))
    return filepath


def write_detailed_csv(results: list[dict], output_dir: str | Path) -> Path:
    """Write detailed validation results CSV with all API data."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / "validation_results.csv"

    fieldnames = [
        "phone_number", "activity_score", "line_type", "carrier",
        "is_valid", "is_prepaid", "assigned_tag", "is_litigator_risk",
    ]
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow({k: r.get(k, "") for k in fieldnames})

    logger.info("Detailed results CSV: %s", filepath)
    return filepath


def write_errors_csv(errors: list[dict], output_dir: str | Path) -> Path | None:
    """Write errors to CSV for review. Returns None if no errors."""
    if not errors:
        return None
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / "errors.csv"

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["phone_number", "error", "detail"])
        for e in errors:
            writer.writerow([
                e.get("phone_number", ""),
                e.get("error", ""),
                e.get("detail", ""),
            ])

    logger.warning("Errors CSV: %s (%d failed)", filepath, len(errors))
    return filepath


def write_summary(
    results: list[dict],
    errors: list[dict],
    tiers: dict,
    output_dir: str | Path,
) -> Path:
    """Write a human-readable summary of the validation run."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / "summary.txt"

    total = len(results)
    scores = [int(r["activity_score"]) for r in results if r["activity_score"] is not None]
    tag_counts = Counter(r["assigned_tag"] for r in results)
    line_type_counts = Counter(r["line_type"] for r in results if r["line_type"])
    avg_score = sum(scores) / len(scores) if scores else 0

    # Score distribution buckets
    buckets = defaultdict(int)
    for s in scores:
        bucket = (s // 10) * 10
        buckets[bucket] += 1

    with open(filepath, "w") as f:
        f.write("=" * 60 + "\n")
        f.write("PHONE VALIDATION SUMMARY\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 60 + "\n\n")

        f.write(f"Total phones processed: {total}\n")
        f.write(f"Errors/failures:        {len(errors)}\n")
        f.write(f"Average activity score: {avg_score:.1f}\n\n")

        f.write("--- TIER BREAKDOWN ---\n\n")
        for tag_name in tiers.keys():
            count = tag_counts.get(tag_name, 0)
            pct = (count / total * 100) if total else 0
            f.write(f"  {tag_name:20s}  {count:5d}  ({pct:5.1f}%)\n")
        for tag_name in sorted(tag_counts.keys()):
            if tag_name not in tiers:
                count = tag_counts[tag_name]
                pct = (count / total * 100) if total else 0
                f.write(f"  {tag_name:20s}  {count:5d}  ({pct:5.1f}%)\n")

        f.write(f"\n--- LINE TYPE BREAKDOWN ---\n\n")
        for lt, count in line_type_counts.most_common():
            pct = (count / total * 100) if total else 0
            f.write(f"  {lt:20s}  {count:5d}  ({pct:5.1f}%)\n")

        f.write(f"\n--- SCORE DISTRIBUTION ---\n\n")
        for bucket in sorted(buckets.keys()):
            count = buckets[bucket]
            bar = "#" * max(1, count // max(1, total // 40))
            f.write(f"  {bucket:3d}-{min(bucket+9, 100):3d}  {count:5d}  {bar}\n")

        f.write(f"\n--- DATASIFT UPLOAD INSTRUCTIONS ---\n\n")
        f.write("1. Open your DataSift/REISift account\n")
        f.write("2. Go to Upload -> Update Data\n")
        f.write("3. Select 'Tag phones by phone number'\n")
        f.write("4. Upload phone_tags_for_datasift.csv\n")
        f.write("5. Map 'Phone Number' -> Phone Number\n")
        f.write("6. Map 'Phone Tag' -> Phone Tag\n")
        f.write("7. Complete the upload\n\n")
        f.write("Tags will apply to ALL records sharing each phone number.\n")
        f.write("When sending to a dialer, send ONE tier at a time.\n")

    logger.info("Summary: %s", filepath)
    return filepath


# ── High-Level Entry Points ──────────────────────────────────────────────


def run_phone_validation(
    csv_path: str | Path,
    api_key: str | None = None,
    output_dir: str | Path | None = None,
    tiers: dict | None = None,
    add_litigator: bool = False,
    batch_size: int = 10,
) -> dict:
    """Run full phone validation pipeline on a CSV file.

    Args:
        csv_path: Path to Phone Enrichment CSV.
        api_key: Trestle API key (defaults to config.TRESTLE_API_KEY).
        output_dir: Output directory (defaults to output/phone_validation/).
        tiers: Custom tier definitions (defaults to DEFAULT_TIERS).
        add_litigator: Include litigator risk check.
        batch_size: Concurrent API requests per batch.

    Returns:
        Dict with keys: success, results_count, errors_count, tag_csv_path,
        detail_csv_path, summary_path, tier_counts.
    """
    if api_key is None:
        api_key = config.TRESTLE_API_KEY
    if not api_key:
        logger.error("No Trestle API key provided. Set TRESTLE_API_KEY in .env or pass --api-key.")
        return {"success": False, "message": "No Trestle API key"}

    if output_dir is None:
        output_dir = config.OUTPUT_DIR / "phone_validation"
    output_dir = Path(output_dir)

    if tiers is None:
        tiers = DEFAULT_TIERS

    csv_path = Path(csv_path)
    if not csv_path.exists():
        logger.error("Input file not found: %s", csv_path)
        return {"success": False, "message": f"File not found: {csv_path}"}

    # Read phones
    phones, unique_count, total_entries = read_phones_from_csv(csv_path)
    if not phones:
        logger.error("No valid phone numbers found in %s", csv_path)
        return {"success": False, "message": "No valid phone numbers found"}

    # Load cache of already-scored phones to avoid re-scoring
    cache_path = output_dir / "trestle_scored_cache.json"
    scored_cache: dict = {}
    if cache_path.exists():
        try:
            scored_cache = json.loads(cache_path.read_text(encoding="utf-8"))
            logger.info("Loaded %d previously scored phones from cache", len(scored_cache))
        except Exception:
            pass

    # Filter out already-scored phones
    new_phones = [(raw, cleaned) for raw, cleaned in phones if cleaned not in scored_cache]
    cached_count = unique_count - len(dict.fromkeys(p[1] for p in new_phones))
    new_unique = len(dict.fromkeys(p[1] for p in new_phones))

    if cached_count > 0:
        logger.info("Skipping %d already-scored phones (cache hit), %d new to score — estimated cost: $%.2f",
                    cached_count, new_unique, new_unique * COST_PER_PHONE)
    else:
        logger.info("Found %d phone entries (%d unique) — estimated cost: $%.2f",
                    total_entries, unique_count, unique_count * COST_PER_PHONE)

    # Build cached results for phones we already know
    cached_results = []
    for raw, cleaned in phones:
        if cleaned in scored_cache:
            cached_results.append(scored_cache[cleaned])

    # Process only NEW phones through Trestle API
    if new_phones:
        new_results, errors = process_phones(
            phones=new_phones,
            api_key=api_key,
            tiers=tiers,
            add_litigator=add_litigator,
            batch_size=batch_size,
        )
        # Update cache with new results
        for r in new_results:
            cleaned = clean_phone(r.get("phone_number", ""))
            if cleaned:
                scored_cache[cleaned] = r
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(scored_cache, indent=2), encoding="utf-8")
            logger.info("Saved %d scored phones to cache", len(scored_cache))
        except Exception as e:
            logger.warning("Could not save phone cache: %s", e)
    else:
        new_results = []
        errors = []
        logger.info("All phones already scored — using cache only")

    results = cached_results + new_results

    # Write outputs
    tag_csv = write_datasift_tags_csv(results, output_dir)
    detail_csv = write_detailed_csv(results, output_dir)
    write_errors_csv(errors, output_dir)
    summary = write_summary(results, errors, tiers, output_dir)

    # Tier breakdown for logging
    tag_counts = Counter(r["assigned_tag"] for r in results)
    for tag_name in tiers.keys():
        count = tag_counts.get(tag_name, 0)
        logger.info("  %s: %d", tag_name, count)

    logger.info("Phone validation complete: %d scored, %d errors", len(results), len(errors))

    return {
        "success": True,
        "results_count": len(results),
        "errors_count": len(errors),
        "tag_csv_path": tag_csv,
        "detail_csv_path": detail_csv,
        "summary_path": summary,
        "tier_counts": dict(tag_counts),
    }


def create_run_tags(
    run_csv_path: str | Path,
    datasift_export_path: str | Path,
    full_tag_csv_path: str | Path,
    api_key: str | None = None,
    batch_size: int = 3,
) -> Path | None:
    """Create a tag CSV with only phones from the current run's records.

    Matches run records against the DataSift export by name to collect
    DataSift skip trace phones, also includes Tracerfy phones from the
    run CSV, scores any unscored phones via Trestle, and writes a
    date-stamped tag file.
    """
    run_csv_path = Path(run_csv_path)
    datasift_export_path = Path(datasift_export_path)
    full_tag_csv_path = Path(full_tag_csv_path)

    if not run_csv_path.exists():
        logger.warning("Run CSV not found: %s", run_csv_path)
        return None

    # 1. Read run CSV — get record names + any Tracerfy phones
    run_names: set[tuple[str, str]] = set()
    tracerfy_phones: set[str] = set()
    tracerfy_fields = [
        "primary_phone", "mobile_1", "mobile_2", "mobile_3", "mobile_4",
        "mobile_5", "landline_1", "landline_2", "landline_3",
    ]
    with open(run_csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            first = (row.get("first_name") or "").strip().upper()
            last = (row.get("last_name") or "").strip().upper()
            if first and last:
                run_names.add((first, last))
            for field in tracerfy_fields:
                val = (row.get(field) or "").strip()
                cleaned = clean_phone(val)
                if cleaned:
                    tracerfy_phones.add(cleaned)

    logger.info("Run tags: %d records, %d Tracerfy phones from run CSV",
                len(run_names), len(tracerfy_phones))

    # 2. Match run names against DataSift export to get skip trace phones
    datasift_phones: set[str] = set()
    matched_names: set[tuple[str, str]] = set()
    if datasift_export_path.exists():
        with open(datasift_export_path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            phone_cols = [h for h in (reader.fieldnames or [])
                          if h.startswith("Phone ")]
            for row in reader:
                first = (row.get("First Name") or "").strip().upper()
                last = (row.get("Last Name") or "").strip().upper()
                if (first, last) in run_names:
                    matched_names.add((first, last))
                    for pc in phone_cols:
                        val = (row.get(pc) or "").strip()
                        cleaned = clean_phone(val)
                        if cleaned:
                            datasift_phones.add(cleaned)

    logger.info("Run tags: matched %d/%d names in DataSift export, %d skip trace phones",
                len(matched_names), len(run_names), len(datasift_phones))

    # 3. Combine all phones for this run
    all_run_phones = datasift_phones | tracerfy_phones

    if not all_run_phones:
        logger.warning("Run tags: no phones found for this run's records")
        return None

    # 4. Load existing scored tags
    scored: dict[str, str] = {}
    if full_tag_csv_path.exists():
        with open(full_tag_csv_path) as f:
            for row in csv.DictReader(f):
                scored[row["Phone Number"]] = row["Phone Tag"]

    # 5. Score any phones not already in the tag file (e.g. Tracerfy-only phones)
    unscored = [p for p in all_run_phones if p not in scored]
    if unscored:
        if not api_key:
            api_key = config.TRESTLE_API_KEY
        if api_key:
            logger.info("Run tags: scoring %d unscored phones via Trestle ($%.2f)",
                        len(unscored), len(unscored) * COST_PER_PHONE)
            for i, phone in enumerate(unscored):
                result = call_trestle(phone, api_key)
                if result.get("activity_score") is not None:
                    score = int(result["activity_score"])
                    tag = assign_tier(score, DEFAULT_TIERS)
                    scored[phone] = tag
                if (i + 1) % batch_size == 0 and i + 1 < len(unscored):
                    time.sleep(1)
        else:
            logger.warning("Run tags: %d phones unscored — no Trestle API key", len(unscored))

    # 6. Write run-specific tag file
    output_dir = full_tag_csv_path.parent
    today = datetime.now().strftime("%Y-%m-%d")
    run_tag_path = output_dir / f"phone_tags_{today}.csv"

    rows = [(p, scored[p]) for p in sorted(all_run_phones) if p in scored]
    with open(run_tag_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Phone Number", "Phone Tag"])
        w.writerows(rows)

    tier_counts = Counter(t for _, t in rows)
    logger.info("Run tags: wrote %d phones to %s", len(rows), run_tag_path)
    for tier_name in DEFAULT_TIERS:
        if tier_counts[tier_name]:
            logger.info("  %s: %d", tier_name, tier_counts[tier_name])

    return run_tag_path
