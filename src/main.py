"""Entry point for SiftStack — full-stack REI operations platform.

Runs as either:
  - Apify Actor (when APIFY_IS_AT_HOME is set — reads input from Actor.get_input())
  - Standalone CLI (python src/main.py daily --counties Knox --types foreclosure)
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import config
from config import (
    LOG_DIR,
    NOTICE_TYPES,
    OUTPUT_DIR,
    SAVED_SEARCHES,
    SavedSearch,
)
from data_formatter import deduplicate, write_csv, write_csv_by_type
from scraper import scrape_all

logger = logging.getLogger(__name__)


# ── Shared helpers ────────────────────────────────────────────────────


def _filter_searches(
    counties: list[str] | None,
    types: list[str] | None,
) -> list[SavedSearch]:
    """Filter SAVED_SEARCHES by county and/or notice type."""
    searches = list(SAVED_SEARCHES)

    if counties:
        county_set = {c.lower() for c in counties}
        searches = [s for s in searches if s.county.lower() in county_set]

    if types:
        type_set = {t.lower() for t in types}
        searches = [s for s in searches if s.notice_type.lower() in type_set]

    return searches


# ── Preflight health checks ─────────────────────────────────────────


def _preflight_check(mode: str, searches: list[SavedSearch] | None = None) -> list[str]:
    """Verify required API keys and service connectivity before running.

    Args:
        searches: Filtered search list for this run. When provided, credential
                  checks are scoped to the sources actually in use so that an
                  Oklahoma-only run doesn't fail on missing TN credentials.

    Returns a list of failure descriptions. Empty list = all checks passed.
    """
    failures: list[str] = []

    # ── Credential checks (mode-dependent) ──────────────────────────
    scrape_modes = {"daily", "historical"}
    enrichment_modes = scrape_modes | {"pdf-import", "photo-import", "dropbox-watch", "csv-import"}
    datasift_modes = {"manage-presets", "manage-sold", "phone-validate"}

    if mode in scrape_modes:
        # Only require TNPN creds when tnpn-source searches are in scope.
        # OK searches (OSCN, TinStar) are public and need no credentials.
        active = searches if searches is not None else list(SAVED_SEARCHES)
        has_tnpn = any(s.source == "tnpn" for s in active)
        if has_tnpn:
            if not config.TNPN_EMAIL or not config.TNPN_PASSWORD:
                failures.append("TNPN_EMAIL / TNPN_PASSWORD not set (required for TN scraping)")
            if not config.CAPTCHA_API_KEY:
                failures.append("CAPTCHA_API_KEY not set (CAPTCHA solving will fail)")

    if mode in enrichment_modes:
        # These are warnings, not blockers — pipeline degrades gracefully
        if not config.SMARTY_AUTH_ID or not config.SMARTY_AUTH_TOKEN:
            logger.warning("Preflight: SMARTY credentials missing — address standardization will be skipped")
        if not config.OPENWEBNINJA_API_KEY:
            logger.warning("Preflight: OPENWEBNINJA_API_KEY missing — Zillow enrichment will be skipped")
        if not config.ANTHROPIC_API_KEY:
            logger.warning("Preflight: ANTHROPIC_API_KEY missing — obituary search and LLM parsing will be skipped")

    if mode in datasift_modes:
        if not config.DATASIFT_EMAIL or not config.DATASIFT_PASSWORD:
            failures.append("DATASIFT_EMAIL / DATASIFT_PASSWORD not set (required for DataSift operations)")

    if mode == "dropbox-watch":
        if not config.DROPBOX_APP_KEY or not config.DROPBOX_APP_SECRET or not config.DROPBOX_REFRESH_TOKEN:
            failures.append("DROPBOX credentials incomplete (need APP_KEY, APP_SECRET, REFRESH_TOKEN)")

    if mode == "phone-validate":
        if not config.TRESTLE_API_KEY:
            failures.append("TRESTLE_API_KEY not set (required for phone validation)")

    # ── Connectivity checks (only for TN scrape modes) ─────────────
    if mode in scrape_modes and has_tnpn:
        import requests as _requests
        try:
            resp = _requests.head(config.BASE_URL, timeout=10, allow_redirects=True)
            if resp.status_code >= 500:
                failures.append(f"tnpublicnotice.com returned {resp.status_code} — site may be down")
        except Exception as e:
            failures.append(f"Cannot reach tnpublicnotice.com: {e}")

    # ── 2Captcha balance check (TN only) ───────────────────────────
    if mode in scrape_modes and has_tnpn and config.CAPTCHA_API_KEY:
        import requests as _requests
        try:
            resp = _requests.get(
                f"https://2captcha.com/res.php?key={config.CAPTCHA_API_KEY}&action=getbalance",
                timeout=10,
            )
            balance_text = resp.text.strip()
            try:
                balance = float(balance_text)
                if balance < 0.50:
                    failures.append(f"2Captcha balance too low: ${balance:.2f} (need at least $0.50)")
                else:
                    logger.info("Preflight: 2Captcha balance: $%.2f", balance)
            except ValueError:
                if "ERROR" in balance_text:
                    failures.append(f"2Captcha API key invalid: {balance_text}")
        except Exception as e:
            logger.warning("Preflight: Could not check 2Captcha balance: %s", e)

    return failures


# ── Apify Actor mode ─────────────────────────────────────────────────


async def actor_main() -> None:
    """Run as an Apify Actor — full automated pipeline.

    Scrape → Enrich → Tracerfy → DataSift Upload → Slack Notification.
    """
    from apify import Actor
    from time import time as _time

    # Set up Python logging so all modules output at INFO level
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    async with Actor:
        pipeline_start = _time()
        actor_input = await Actor.get_input() or {}

        # Override config credentials from Actor input.
        # Set both config.* AND os.environ so downstream modules that read
        # from either source (e.g., datasift_uploader uses os.environ) pick them up.
        _cred_map = {
            "TNPN_EMAIL": actor_input.get("tn_username", ""),
            "TNPN_PASSWORD": actor_input.get("tn_password", ""),
            "CAPTCHA_API_KEY": actor_input.get("captcha_api_key", ""),
            "ANTHROPIC_API_KEY": actor_input.get("anthropic_api_key", ""),
            "SMARTY_AUTH_ID": actor_input.get("smarty_auth_id", ""),
            "SMARTY_AUTH_TOKEN": actor_input.get("smarty_auth_token", ""),
            "OPENWEBNINJA_API_KEY": actor_input.get("openwebninja_api_key", ""),
            "SERPER_API_KEY": actor_input.get("serper_api_key", ""),
            "FIRECRAWL_API_KEY": actor_input.get("firecrawl_api_key", ""),
            "TRACERFY_API_KEY": actor_input.get("tracerfy_api_key", ""),
            "DATASIFT_EMAIL": actor_input.get("datasift_email", ""),
            "DATASIFT_PASSWORD": actor_input.get("datasift_password", ""),
            "SLACK_WEBHOOK_URL": actor_input.get("slack_webhook_url", ""),
            "TRESTLE_API_KEY": actor_input.get("trestle_api_key", ""),
        }
        for key, val in _cred_map.items():
            setattr(config, key, val)
            if val:
                os.environ[key] = val

        mode = actor_input.get("mode", "daily")
        counties = actor_input.get("counties") or None
        types = actor_input.get("types") or None
        since_date_override = actor_input.get("since_date", "").strip()
        start_page = int(actor_input.get("start_page", 1) or 1)
        drive_folder_id = actor_input.get("google_drive_folder_id", "")
        drive_key_b64 = actor_input.get("google_service_account_key", "")

        # Pipeline toggles
        do_tracerfy = actor_input.get("run_tracerfy", False)
        do_notify_slack = actor_input.get("notify_slack", True)
        # Deep prospecting (obituary/heir/DM resolution -> skip trace -> DP PDFs) is
        # OFF by default: the daily run uploads data + screenshots straight to
        # DataSift, and a separate pipeline handles deep prospecting. Flip
        # resolve_heirs on to deep-prospect inline. All modules stay in the tree.
        resolve_heirs = actor_input.get("resolve_heirs", False)

        # Buy box / filter toggles
        include_vacant = actor_input.get("include_vacant", False)
        include_commercial = actor_input.get("include_commercial", False)
        include_entities = actor_input.get("include_entities", False)

        # Validate
        if not config.TNPN_EMAIL or not config.TNPN_PASSWORD:
            Actor.log.error("tn_username and tn_password are required")
            try:
                from slack_notifier import notify_preflight_failure
                notify_preflight_failure(["TNPN credentials missing"])
            except Exception:
                pass
            await Actor.fail(status_message="Missing SiftStack credentials")
            return
        if not config.CAPTCHA_API_KEY:
            Actor.log.warning("captcha_api_key not set — CAPTCHA solving will fail")

        # Filter searches
        searches = _filter_searches(counties, types)
        if not searches:
            Actor.log.error("No saved searches match the given counties/types filters")
            await Actor.fail(status_message="No matching saved searches")
            return

        Actor.log.info(
            "Running %d saved searches: %s",
            len(searches),
            ", ".join(s.saved_search_name for s in searches),
        )

        # Set up residential proxy if requested
        proxy_url: str | None = None
        use_proxy = actor_input.get("use_residential_proxy", True)
        if use_proxy:
            try:
                proxy_config = await Actor.create_proxy_configuration(
                    groups=["RESIDENTIAL"]
                )
                proxy_url = await proxy_config.new_url()
                Actor.log.info("Residential proxy configured")
            except Exception:
                Actor.log.warning("Could not configure residential proxy — running without proxy")

        # Track seen notice IDs for incremental dedup
        seen_ids: set[str] = set()
        # One business-tz run date for the whole run so every dataset row shares
        # it (no midnight straddle across incremental pushes) and the run-window
        # cutoff matches the enrichment date_added stamp.
        run_today = config.run_date()

        def _notice_id(url: str) -> str:
            import re
            m = re.search(r"[?&]ID=(\d+)", url)
            return m.group(1) if m else ""

        async def push_batch(batch_notices):
            """Push new unique notices to dataset immediately after each search."""
            unique = []
            for n in batch_notices:
                nid = _notice_id(n.source_url)
                if nid and nid in seen_ids:
                    continue
                if nid:
                    seen_ids.add(nid)
                unique.append(n)
            if unique:
                _today = run_today  # one business-tz date for the whole run
                await Actor.push_data([
                    {
                        "date_added": n.date_added or _today,
                        "date_published": n.date_published,
                        "address": n.address,
                        "city": n.city,
                        "state": n.state,
                        "zip": n.zip,
                        "owner_name": n.owner_name,
                        "notice_type": n.notice_type,
                        "county": n.county,
                        "decedent_name": n.decedent_name,
                        "owner_street": n.owner_street,
                        "owner_city": n.owner_city,
                        "owner_state": n.owner_state,
                        "owner_zip": n.owner_zip,
                        "auction_date": n.auction_date,
                        "zip_plus4": n.zip_plus4,
                        "latitude": n.latitude,
                        "longitude": n.longitude,
                        "dpv_match_code": n.dpv_match_code,
                        "vacant": n.vacant,
                        "rdi": n.rdi,
                        "mls_status": n.mls_status,
                        "mls_listing_price": n.mls_listing_price,
                        "mls_last_sold_date": n.mls_last_sold_date,
                        "mls_last_sold_price": n.mls_last_sold_price,
                        "estimated_value": n.estimated_value,
                        "estimated_equity": n.estimated_equity,
                        "equity_percent": n.equity_percent,
                        "property_type": n.property_type,
                        "bedrooms": n.bedrooms,
                        "bathrooms": n.bathrooms,
                        "sqft": n.sqft,
                        "year_built": n.year_built,
                        "lot_size": n.lot_size,
                        "source_url": n.source_url,
                        "raw_text": n.raw_text[:5000] if n.raw_text else "",
                    }
                    for n in unique
                ])
                Actor.log.info("Pushed %d records to dataset (incremental)", len(unique))

        # Log LLM parser status
        if config.ANTHROPIC_API_KEY:
            Actor.log.info("LLM fallback enabled (Claude Haiku) for missing fields")
        else:
            Actor.log.info("LLM fallback disabled — set anthropic_api_key to enable")

        if start_page > 1:
            Actor.log.info("Starting from page %d (skipping earlier pages)", start_page)

        try:
            kvs = await Actor.open_key_value_store()

            # ── Load last_run_date from Apify KVS (persists between runs) ──
            if mode == "daily" and not since_date_override:
                stored = await kvs.get_value("last_run_date")
                if stored:
                    since_date_override = stored
                    Actor.log.info("Daily mode: using stored last_run_date = %s", stored)
                else:
                    Actor.log.info("Daily mode: no stored last_run_date, defaulting to 7 days")

            # ── Load cross-run seen-ID cache from KVS (makes daily re-runs idempotent) ──
            seen_ids = await kvs.get_value("seen_notice_ids") or {}
            Actor.log.info("Loaded %d previously-seen notice IDs from KVS", len(seen_ids))

            async def persist_seen_ids(ids: dict) -> None:
                """Mid-run persistence — if a later search crashes, progress is kept."""
                try:
                    await kvs.set_value("seen_notice_ids", ids)
                    await kvs.set_value("last_run_date", run_today)
                except Exception as e:
                    Actor.log.warning("Failed to persist seen_notice_ids to KVS: %s", e)

            # ── Scrape ────────────────────────────────────────────────
            notices = await scrape_all(
                mode=mode, searches=searches, proxy_url=proxy_url, on_batch=push_batch,
                since_date_override=since_date_override or None,
                llm_api_key=config.ANTHROPIC_API_KEY or None,
                start_page=start_page,
                seen_ids=seen_ids,
                on_search_complete=persist_seen_ids,
            )
            # Handle async probate lookup before pipeline (requires await)
            probate_notices = [n for n in notices if n.notice_type == "probate" and n.decedent_name and not n.address]
            if probate_notices:
                try:
                    from property_lookup import lookup_decedent_properties
                    Actor.log.info("Looking up property addresses for %d probate notices...", len(probate_notices))
                    await lookup_decedent_properties(probate_notices)
                except ImportError:
                    Actor.log.warning("property_lookup module not found -- skipping property lookup")
                except Exception as e:
                    Actor.log.warning("Property lookup failed: %s -- continuing without lookups", e)

            # ── Enrichment ────────────────────────────────────────────
            from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

            opts = PipelineOptions(
                skip_parcel_lookup=True,  # web scrape notices don't have parcel IDs
                skip_vacant_filter=include_vacant,
                skip_commercial_filter=include_commercial,
                skip_entity_filter=include_entities,
                skip_obituary=not resolve_heirs,  # deep prospecting off by default
                source_label="Apify Actor",
            )
            notices = run_enrichment_pipeline(notices, opts)

            if not notices:
                Actor.log.warning("No notices found")
                return

            total = len(notices)

            # ── Tracerfy Skip Trace (DP candidates only) ────────────
            # Only run Tracerfy on records that need deep prospecting
            # (deceased owners, heir maps, decision makers). Basic records
            # get skip traced for free inside DataSift's unlimited plan.
            tracerfy_stats = None
            if do_tracerfy and config.TRACERFY_API_KEY:
                dp_for_tracerfy = [
                    n for n in notices
                    if n.owner_deceased == "yes" or n.heir_map_json or n.decision_maker_name
                ]
                if dp_for_tracerfy:
                    Actor.log.info("Running Tracerfy on %d DP candidates (%d basic records skipped)...",
                                   len(dp_for_tracerfy), total - len(dp_for_tracerfy))
                    try:
                        from tracerfy_skip_tracer import batch_skip_trace
                        tracerfy_stats = batch_skip_trace(dp_for_tracerfy)
                        Actor.log.info(
                            "Tracerfy: %d/%d matched, %d phones, %d emails, $%.2f",
                            tracerfy_stats["matched"], tracerfy_stats["submitted"],
                            tracerfy_stats["phones_found"], tracerfy_stats["emails_found"],
                            tracerfy_stats["cost"],
                        )
                    except Exception as e:
                        Actor.log.warning("Tracerfy skip trace failed: %s — continuing", e)
                else:
                    Actor.log.info("No DP candidates — Tracerfy skipped (0 deceased/DM records)")
            elif do_tracerfy:
                Actor.log.info("Tracerfy skipped — no API key configured")

            # ── Generate Deep Prospecting PDFs ────────────────────────
            # Only generate PDFs for records that have deep prospecting data:
            # deceased owners with heir/DM info, or records with signing chains.
            # Basic records (just address + owner) don't need a PDF.
            pdf_urls = []
            dp_candidates = [
                n for n in notices
                if n.owner_deceased == "yes" or n.heir_map_json or n.decision_maker_name
            ]

            # Score every phone (DM #1 + all heirs) with Trestle before rendering,
            # so signing-chain phones get tier badges — not just DM #1's.
            phone_tiers: dict = {}
            if dp_candidates and config.TRESTLE_API_KEY:
                try:
                    from phone_validator import score_record_phones
                    phone_tiers = score_record_phones(dp_candidates, config.TRESTLE_API_KEY)
                    Actor.log.info("Trestle scored %d unique phones across DP candidates",
                                   len(phone_tiers))
                except Exception as e:
                    Actor.log.warning("Per-record Trestle scoring failed: %s — continuing", e)

            if dp_candidates:
                try:
                    from report_generator import generate_record_pdf
                    kvs = await Actor.open_key_value_store()
                    kvs_id = getattr(kvs, 'id', None) or getattr(kvs, '_id', '')
                    report_dir = Path("output/reports")

                    for n in dp_candidates:
                        pdf_path = generate_record_pdf(
                            n, output_dir=report_dir, phone_tiers=phone_tiers,
                        )
                        key = pdf_path.name
                        with open(pdf_path, "rb") as f:
                            await kvs.set_value(key, f.read(), content_type="application/pdf")
                        url = f"https://api.apify.com/v2/key-value-stores/{kvs_id}/records/{key}"
                        pdf_urls.append({"address": n.address, "url": url})

                    Actor.log.info("Generated %d deep prospecting PDFs (%d records skipped — no DP data)",
                                   len(pdf_urls), total - len(dp_candidates))
                except Exception as e:
                    Actor.log.warning("PDF generation failed: %s — continuing", e)
            else:
                Actor.log.info("No records need deep prospecting PDFs")

            # ── Write CSV ─────────────────────────────────────────────
            csv_path = write_csv(notices)
            if not kvs:
                kvs = await Actor.open_key_value_store()
            with open(csv_path, "rb") as f:
                await kvs.set_value("output.csv", f.read(), content_type="text/csv")
            Actor.log.info("CSV saved to key-value store as 'output.csv'")

            # ── Google Drive Upload ───────────────────────────────────
            if drive_folder_id and drive_key_b64:
                Actor.log.info("Uploading to Google Drive...")
                from drive_uploader import upload_csv, upload_summary

                by_type: dict[str, int] = {}
                by_county: dict[str, int] = {}
                for n in notices:
                    by_type[n.notice_type] = by_type.get(n.notice_type, 0) + 1
                    by_county[n.county] = by_county.get(n.county, 0) + 1

                file_id = upload_csv(csv_path, drive_folder_id, drive_key_b64, total)
                if file_id:
                    Actor.log.info("CSV uploaded to Drive (file ID: %s)", file_id)
                else:
                    Actor.log.error("CSV upload to Drive failed — CSV still in key-value store")

                upload_summary(by_type, by_county, total, drive_folder_id, drive_key_b64)
            elif drive_folder_id:
                Actor.log.warning("google_drive_folder_id set but google_service_account_key missing — skipping Drive upload")

            # ── Host notice screenshots (proof-of-source) ─────────────
            # Push each foreclosure's notice screenshot to the KVS and set a
            # shareable URL so the DataSift Notes link + "Notice Screenshot"
            # field travel with the record. Mirrors the PDF KVS pattern above.
            try:
                shots = [n for n in notices if getattr(n, "notice_screenshot_path", "")]
                if shots:
                    if not kvs:
                        kvs = await Actor.open_key_value_store()
                    kvs_id = getattr(kvs, 'id', None) or getattr(kvs, '_id', '')
                    hosted = 0
                    for n in shots:
                        p = Path(n.notice_screenshot_path)
                        if not p.exists():
                            continue
                        with open(p, "rb") as f:
                            await kvs.set_value(p.name, f.read(), content_type="image/png")
                        # Only publish a URL when we have a real store id; a blank
                        # id would yield a 404 image. Degrade to local path (no
                        # inline image) rather than a broken Slack image block.
                        if kvs_id:
                            n.notice_screenshot_url = (
                                f"https://api.apify.com/v2/key-value-stores/{kvs_id}/records/{p.name}"
                            )
                        hosted += 1
                    Actor.log.info("Hosted %d notice screenshots in KVS", hosted)
            except Exception as e:
                Actor.log.warning("Notice screenshot hosting failed: %s, continuing", e)

            # ── DataSift CSVs → KVS (manual upload) ─────────────────
            # Generate DataSift-formatted CSVs and save to Apify KVS
            # for manual download + upload to DataSift (more reliable than
            # automated Playwright upload in headless cloud containers).
            datasift_csv_urls = []
            try:
                from datasift_formatter import write_datasift_split_csvs

                csv_infos = write_datasift_split_csvs(notices, source_label=mode)
                kvs = await Actor.open_key_value_store()
                for info in csv_infos:
                    key = f"datasift_{info['label'].lower().replace(' ', '_')}.csv"
                    with open(info["path"], "rb") as f:
                        await kvs.set_value(key, f.read(), content_type="text/csv")
                    # Build public download URL
                    kvs_id = getattr(kvs, 'id', None) or getattr(kvs, '_id', '')
                    url = f"https://api.apify.com/v2/key-value-stores/{kvs_id}/records/{key}"
                    datasift_csv_urls.append({"label": info["label"], "url": url, "records": info.get("count", "?")})
                    Actor.log.info("DataSift CSV (%s) saved to KVS: %s", info["label"], key)
            except Exception as e:
                Actor.log.error("DataSift CSV generation failed: %s", e)

            # ── Slack Notification ────────────────────────────────────
            elapsed_min = (_time() - pipeline_start) / 60

            # Compute estimated run cost
            cost_breakdown = {}
            # 2Captcha: $0.003 per solve, ~1 solve per notice scraped
            captcha_count = total  # each notice detail page requires a CAPTCHA
            cost_breakdown["2Captcha"] = round(captcha_count * 0.003, 2)
            # Anthropic Haiku: ~$0.001 per record (LLM parsing + obituary search)
            if config.ANTHROPIC_API_KEY:
                cost_breakdown["Anthropic (Haiku)"] = round(total * 0.001, 3)
            # Tracerfy: actual cost from batch stats
            if tracerfy_stats and tracerfy_stats.get("cost", 0) > 0:
                cost_breakdown["Tracerfy"] = round(tracerfy_stats["cost"], 2)
            # Smarty: free tier 250/month, $0.01 after
            smarty_count = sum(1 for n in notices if n.dpv_match_code)
            if smarty_count > 0:
                cost_breakdown["Smarty"] = round(max(0, smarty_count - 250) * 0.01, 2) if smarty_count > 250 else 0.0
            # Zillow (OpenWeb Ninja): free tier 100/month, $0.01 after
            zillow_count = sum(1 for n in notices if n.estimated_value)
            if zillow_count > 0:
                cost_breakdown["Zillow"] = round(max(0, zillow_count - 100) * 0.01, 2) if zillow_count > 100 else 0.0
            # Remove zero-cost entries for cleaner display
            cost_breakdown = {k: v for k, v in cost_breakdown.items() if v > 0}

            if do_notify_slack and config.SLACK_WEBHOOK_URL:
                try:
                    from slack_notifier import (
                        send_slack_notification, send_record_package, _send_webhook,
                    )

                    # Send standard run summary with cost breakdown
                    send_slack_notification(
                        notices,
                        elapsed_min=elapsed_min,
                        cost_breakdown=cost_breakdown,
                    )

                    # Send the per-record Sift upload package: details + inline
                    # notice screenshots (hosted to KVS above), chunked into batches.
                    send_record_package(notices)

                    # Send DataSift CSV download links as a follow-up message
                    if datasift_csv_urls:
                        csv_lines = [
                            "*DataSift CSVs ready for manual upload:*",
                        ]
                        for csv_info in datasift_csv_urls:
                            csv_lines.append(f"  <{csv_info['url']}|{csv_info['label']}> ({csv_info['records']} records)")
                        csv_lines.append("_Upload at app.reisift.io → Upload File → Add Data_")
                        _send_webhook("\n".join(csv_lines))

                    # Send PDF download links
                    if pdf_urls:
                        pdf_lines = [
                            f"*Deep Prospecting PDFs ({len(pdf_urls)} records):*",
                        ]
                        for pdf_info in pdf_urls:
                            pdf_lines.append(f"  <{pdf_info['url']}|{pdf_info['address']}>")
                        pdf_lines.append("_Attach to DataSift record → Notes or Files_")
                        _send_webhook("\n".join(pdf_lines))

                    Actor.log.info("Slack notification sent")
                except Exception as e:
                    Actor.log.warning("Slack notification failed: %s", e)

            # ── Save last_run_date + seen_notice_ids to Apify KVS for next run ─────
            await kvs.set_value("last_run_date", run_today)
            await kvs.set_value("seen_notice_ids", seen_ids)
            Actor.log.info(
                "Saved last_run_date + %d seen_notice_ids to KVS for next daily run",
                len(seen_ids),
            )

            Actor.log.info("Done — %d notices exported (%.1f min)", total, elapsed_min)

        except Exception as e:
            Actor.log.error("Pipeline failed: %s", e, exc_info=True)
            try:
                from slack_notifier import notify_error
                notify_error("Apify Actor Pipeline", e, context=f"mode={mode}")
            except Exception:
                pass
            await Actor.fail(status_message=f"Pipeline error: {e}")


# ── CLI mode ──────────────────────────────────────────────────────────


def setup_logging(verbose: bool = False) -> None:
    """Configure logging to both console and date-stamped log file."""
    level = logging.DEBUG if verbose else logging.INFO
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_file = LOG_DIR / f"scrape_{timestamp}.log"

    # Force UTF-8 on console output to avoid cp1252 encoding errors on Windows
    console = logging.StreamHandler(
        open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
    )
    handlers: list[logging.Handler] = [
        console,
        logging.FileHandler(log_file, encoding="utf-8"),
    ]

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.info("Logging to %s", log_file)


def _upload_notices_to_datasift(notices: list, args, source_label: str = "") -> dict | None:
    """Format notices as DataSift-ready CSV(s) and upload, if --upload-datasift set.

    Shared by every ingestion path (scrape, PDF import, photo import,
    dropbox watch, csv-import, obituary pre-probate) so no scraper we build
    can silently skip DataSift's Notes/Tags/Call Prep formatting the way
    pdf-import and photo-import previously did.

    Args:
        notices: Enriched NoticeData records to upload.
        args: Parsed CLI args.
        source_label: Scraper/source name (e.g. "acclaim", "oscn",
            "preprobate_obituary", "pdf_import") embedded in the generated
            filenames so every DataSift-ready CSV is traceable to its source.

    Returns the upload result dict, or None if --upload-datasift wasn't set.
    """
    if not getattr(args, "upload_datasift", False):
        return None

    from datasift_formatter import write_datasift_split_csvs

    csv_infos = write_datasift_split_csvs(notices, source_label=source_label)
    for info in csv_infos:
        logging.info("DataSift CSV (%s): %s", info["label"], info["path"])

    if getattr(args, "csv_only", False):
        logging.info(
            "--csv-only set — DataSift CSV(s) generated, skipping automated "
            "Playwright upload. Upload manually."
        )
        return {"success": None, "message": "CSV generated; automated upload skipped (--csv-only)"}

    from datasift_uploader import upload_datasift_split, upload_to_datasift

    do_enrich = not getattr(args, "no_enrich", False)
    do_skip_trace = not getattr(args, "no_skip_trace", False)
    update_list = getattr(args, "datasift_update_list", None)
    ds_mode = "update" if update_list else "add"

    if len(csv_infos) > 1:
        upload_result = asyncio.run(
            upload_datasift_split(csv_infos, enrich=do_enrich, skip_trace=do_skip_trace)
        )
    else:
        upload_result = asyncio.run(
            upload_to_datasift(
                csv_infos[0]["path"],
                enrich=do_enrich,
                skip_trace=do_skip_trace,
                mode=ds_mode,
                list_name=update_list,
            )
        )

    if upload_result.get("success"):
        logging.info("DataSift upload: %s", upload_result.get("message", "OK"))
        if upload_result.get("enrich_result"):
            logging.info("  Enrich: %s", upload_result["enrich_result"].get("message", ""))
        if upload_result.get("skip_trace_result"):
            logging.info("  Skip trace: %s", upload_result["skip_trace_result"].get("message", ""))
    else:
        logging.error("DataSift upload failed: %s", upload_result.get("message"))

    return upload_result


def _run_pdf_import(args) -> None:
    """Run the PDF import pipeline: OCR → parse → enrich → CSV."""
    from pdf_importer import process_pdf
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

    # Validate required args
    if not args.pdf_path:
        logging.error("--pdf-path is required for pdf-import mode")
        sys.exit(1)
    if not args.pdf_county:
        logging.error("--pdf-county is required for pdf-import mode")
        sys.exit(1)

    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        logging.error("PDF file not found: %s", pdf_path)
        sys.exit(1)

    county = args.pdf_county.strip().title()  # "knox" → "Knox"

    api_key = config.ANTHROPIC_API_KEY or None

    # OCR + parse
    notices = process_pdf(
        pdf_path=pdf_path,
        county=county,
        api_key=api_key,
        date_added=args.pdf_date,
        regex_only=args.regex_only,
    )

    if not notices:
        logging.warning("No records extracted from PDF")
        sys.exit(0)

    # Run unified enrichment pipeline
    opts = PipelineOptions(
        skip_parcel_lookup=args.skip_tax,
        skip_smarty=args.skip_smarty,
        skip_zillow=args.skip_zillow,
        skip_tax=args.skip_tax,
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=args.skip_obituary,
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_vacant_filter=getattr(args, "include_vacant", False),
        skip_commercial_filter=getattr(args, "include_commercial", False),
        skip_entity_filter=getattr(args, "include_entities", False),
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_dm_address=args.skip_dm_address,
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        deep_heirs=getattr(args, "deep_heirs", False),
        source_label=f"PDF import ({pdf_path.name})",
    )
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No records remaining after pipeline")
        return

    # Write output
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{county.lower()}_tax_sale_{timestamp}.csv"
    path = write_csv(notices, filename=filename)
    logging.info("Output: %s", path)

    _upload_notices_to_datasift(notices, args, source_label=f"pdf_import_{county.lower()}")
    logging.info("Done — %d records exported", len(notices))


def _run_photo_import(args) -> None:
    """Run the photo import pipeline: preprocess → OCR → parse → enrich → CSV."""
    from photo_importer import process_photos
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

    # Validate required args
    if not args.folder:
        logging.error("--folder is required for photo-import mode")
        sys.exit(1)
    if not args.photo_county:
        logging.error("--photo-county is required for photo-import mode")
        sys.exit(1)
    if not args.photo_type:
        logging.error("--photo-type is required for photo-import mode")
        sys.exit(1)

    folder = Path(args.folder)
    if not folder.exists() or not folder.is_dir():
        logging.error("Folder not found: %s", folder)
        sys.exit(1)

    county = args.photo_county.strip().title()

    notice_type = args.photo_type.strip().lower()
    api_key = config.ANTHROPIC_API_KEY or None

    # OCR + parse
    notices = process_photos(
        folder=folder,
        county=county,
        notice_type=notice_type,
        date_added=args.photo_date,
        api_key=api_key,
        correct_perspective=not getattr(args, "no_perspective_correct", False),
    )

    if not notices:
        logging.warning("No records extracted from photos")
        sys.exit(0)

    # Run unified enrichment pipeline
    # Skip vacant land filter for notice types without property addresses
    # (probate from court terminals never has property address — would filter everything)
    no_address_types = {"probate", "divorce"}
    opts = PipelineOptions(
        skip_vacant_filter=getattr(args, "include_vacant", False) or notice_type in no_address_types,
        skip_commercial_filter=getattr(args, "include_commercial", False),
        skip_entity_filter=getattr(args, "include_entities", False),
        skip_parcel_lookup=args.skip_tax,
        skip_smarty=args.skip_smarty,
        skip_zillow=args.skip_zillow,
        skip_tax=args.skip_tax,
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=args.skip_obituary,
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_dm_address=args.skip_dm_address,
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        deep_heirs=getattr(args, "deep_heirs", False),
        source_label=f"Photo import ({folder.name})",
    )
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No records remaining after pipeline")
        return

    # Write output
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{county.lower()}_{notice_type}_{timestamp}.csv"
    path = write_csv(notices, filename=filename)
    logging.info("Output: %s", path)

    _upload_notices_to_datasift(notices, args, source_label=f"photo_{notice_type}")
    logging.info("Done — %d records exported", len(notices))


def _run_csv_import(args) -> None:
    """Run the CSV re-import pipeline: read CSV → enrich → write new CSV.

    Supports multiple CSV paths (comma-separated) for merging datasets.
    Supports --upload-datasift to format and upload to DataSift after enrichment.
    """
    from data_formatter import read_csv
    from enrichment_pipeline import (
        PipelineOptions,
        detect_existing_enrichment,
        run_enrichment_pipeline,
    )

    # Validate required args
    if not args.csv_path:
        logging.error("--csv-path is required for csv-import mode")
        sys.exit(1)

    # Support multiple CSV paths (comma-separated)
    csv_paths = [Path(p.strip()) for p in args.csv_path.split(",")]
    for cp in csv_paths:
        if not cp.exists():
            logging.error("CSV file not found: %s", cp)
            sys.exit(1)

    county = None
    if args.csv_county:
        county = args.csv_county.strip().title()

    # Read all CSVs → NoticeData, merge
    all_notices = []
    for cp in csv_paths:
        batch = read_csv(cp)
        logging.info("Loaded %d records from %s", len(batch), cp.name)
        all_notices.extend(batch)

    if not all_notices:
        logging.warning("No records found in CSV(s)")
        sys.exit(0)

    # Deduplicate by source_url (notice ID) — keeps most recent
    seen_urls = {}
    for n in all_notices:
        url = getattr(n, "source_url", "") or ""
        if url and url in seen_urls:
            # Keep the one with more enrichment data
            existing = seen_urls[url]
            if (getattr(n, "estimated_value", "") or "") and not (getattr(existing, "estimated_value", "") or ""):
                seen_urls[url] = n
        elif url:
            seen_urls[url] = n
        else:
            # No source_url — keep all (dedup by address later)
            seen_urls[id(n)] = n
    notices = list(seen_urls.values())
    if len(notices) < len(all_notices):
        logging.info("Deduped %d → %d records (by source_url)", len(all_notices), len(notices))

    # Override county if provided (for CSVs without county column)
    if county:
        for n in notices:
            if not n.county.strip():
                n.county = county

    logging.info("Total: %d records from %d CSV(s)", len(notices), len(csv_paths))

    # Build pipeline options
    primary_name = csv_paths[0].name
    opts = PipelineOptions(
        skip_filter_sold=False,
        skip_vacant_filter=getattr(args, "include_vacant", False),
        skip_commercial_filter=getattr(args, "include_commercial", False),
        skip_entity_filter=getattr(args, "include_entities", False),
        skip_smarty=args.skip_smarty,
        skip_zillow=args.skip_zillow,
        skip_tax=args.skip_tax,
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=args.skip_obituary,
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_dm_address=args.skip_dm_address,
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        deep_heirs=getattr(args, "deep_heirs", False),
        source_label=f"CSV import ({primary_name})",
    )
    detect_existing_enrichment(notices, opts)
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No records remaining after pipeline")
        return

    # Tracerfy batch skip trace (Trestle scoring deferred to phone-validate
    # post-DataSift step — see feedback-pipeline-order in memory)
    if not getattr(args, "skip_tracerfy", False) and config.TRACERFY_API_KEY:
        from tracerfy_skip_tracer import batch_skip_trace
        tracerfy_stats = batch_skip_trace(notices)
        if tracerfy_stats.get("credits_exhausted"):
            logging.error(
                "TRACERFY OUT OF CREDITS -- skip trace disabled. "
                "Add credits at https://tracerfy.com/billing"
            )
        logging.info(
            "Tracerfy: %d/%d matched, %d phones, %d emails, $%.2f",
            tracerfy_stats.get("matched", 0), tracerfy_stats.get("submitted", 0),
            tracerfy_stats.get("phones_found", 0), tracerfy_stats.get("emails_found", 0),
            tracerfy_stats.get("cost", 0.0),
        )

    # Write output
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{csv_paths[0].stem}_reimport_{timestamp}.csv"
    path = write_csv(notices, filename=filename)
    logging.info("Output: %s", path)

    # Derive a clean source label from the input filename for the DataSift
    # CSV name, e.g. "acclaim_consolidated_2026-07-09" -> "acclaim_consolidated"
    import re as _re_label
    csv_label = _re_label.sub(r"_\d{4}-\d{2}-\d{2}(_\d{6})?$", "", csv_paths[0].stem)
    csv_label = _re_label.sub(r"_(FINAL|reimport)$", "", csv_label, flags=_re_label.IGNORECASE)

    _upload_notices_to_datasift(notices, args, source_label=csv_label)
    logging.info("Done — %d records exported", len(notices))


def _run_phone_validate(args) -> None:
    """Run phone validation via Trestle API with DataSift export/upload."""
    import json as _json

    csv_path = getattr(args, "csv_path", None)
    list_name = getattr(args, "list_name", None)
    preset_folder = getattr(args, "preset_folder", None)
    all_records = getattr(args, "all_records", False)

    # Must specify at least one targeting mode
    if not csv_path and not list_name and not preset_folder and not all_records:
        logging.error(
            "phone-validate requires one of: --csv-path, --list-name, --preset-folder, or --all-records"
        )
        sys.exit(1)

    # Parse custom tiers if provided
    tiers = None
    custom_tiers_str = getattr(args, "custom_tiers", None)
    if custom_tiers_str:
        try:
            raw = _json.loads(custom_tiers_str)
            tiers = {k: tuple(v) for k, v in raw.items()}
            logging.info("Using custom tiers: %s", tiers)
        except (_json.JSONDecodeError, ValueError) as e:
            logging.error("Invalid --custom-tiers JSON: %s", e)
            sys.exit(1)

    # Estimate-only mode
    if getattr(args, "estimate", False):
        from phone_validator import estimate_cost, print_estimate

        if csv_path:
            est = estimate_cost(csv_path)
            print_estimate(est)
        else:
            logging.error("--estimate requires --csv-path (export from DataSift first, then estimate)")
            sys.exit(1)
        return

    # Full validation workflow
    from datasift_uploader import run_phone_validation_workflow

    run_csv = getattr(args, "run_csv", None)

    result = asyncio.run(run_phone_validation_workflow(
        list_name=list_name,
        preset_folder=preset_folder,
        all_records=all_records,
        csv_path=csv_path,
        upload_tags=not getattr(args, "no_upload", False),
        api_key=config.TRESTLE_API_KEY or None,
        tiers=tiers,
        add_litigator=getattr(args, "add_litigator", False),
        batch_size=getattr(args, "batch_size", 10),
        run_csv=run_csv,
    ))

    if result.get("success"):
        logging.info("Phone validation: %s", result.get("message", "OK"))
        if result.get("validation_result"):
            vr = result["validation_result"]
            logging.info("  Results: %d scored, %d errors", vr.get("results_count", 0), vr.get("errors_count", 0))
            for tag, count in vr.get("tier_counts", {}).items():
                logging.info("    %s: %d", tag, count)
        if result.get("run_tag_csv"):
            logging.info("  Run-specific tags: %s", result["run_tag_csv"])
        if result.get("upload_result"):
            logging.info("  Tag upload: %s", result["upload_result"].get("message", ""))
    else:
        logging.error("Phone validation failed: %s", result.get("message"))
        sys.exit(1)


def _run_daily_obits(args) -> None:
    """Run the pre-probate obituary pipeline.

    Scrapes Tulsa obituaries -> Assessor lookup (property owner?) ->
    LLM heir parsing -> enrichment -> CSV output.
    """
    from tulsa_assessor import lookup_addresses_tulsa
    from enrichment_pipeline import run_enrichment_pipeline, PipelineOptions

    # Step 1: Scrape direct funeral home sites (complete heir data from day 1, no delay needed)
    import asyncio as _asyncio
    from funeral_home_scraper import scrape_all_funeral_homes, FUNERAL_HOMES, _SEEN_FILE, _load_seen, _save_seen
    headless = not getattr(args, "headed", False)

    # --rescrape: remove today's entries from the seen file so this run
    # re-processes all obituaries scraped earlier today.
    if getattr(args, "rescrape", False):
        import json as _json
        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y-%m-%d")
        seen = _load_seen()
        before = len(seen)
        seen = {url: dt for url, dt in seen.items() if dt != today}
        _save_seen(seen)
        cleared = before - len(seen)
        logging.info("--rescrape: cleared %d today's seen entries (%d remaining)", cleared, len(seen))

    logging.info("Scraping direct funeral home sites (%d configured)...", len(FUNERAL_HOMES))
    obits = _asyncio.run(scrape_all_funeral_homes(headless=headless))
    logging.info("Funeral homes: %d new obituaries", len(obits))

    if not obits:
        logging.warning("No new obituaries found across funeral home sites")
        return

    # Drop obituaries with no survived-by text — without heir info we have no
    # one to contact and no DM to corroborate the Assessor property match.
    obits_with_heirs = [o for o in obits if o.get("survived_by_raw", "").strip()]
    dropped_no_heirs = len(obits) - len(obits_with_heirs)
    if dropped_no_heirs:
        logging.info(
            "Dropped %d obituaries with no survived-by text (%d remaining)",
            dropped_no_heirs, len(obits_with_heirs),
        )
    obits = obits_with_heirs
    if not obits:
        logging.warning("No obituaries with heir information — nothing to process")
        return

    property_owners = resolve_obit_leads(obits, headless=headless)
    if not property_owners:
        return

    # Step 4: Run enrichment pipeline
    logging.info("Running enrichment on %d pre-probate leads...", len(property_owners))
    skip_zillow = getattr(args, "skip_zillow", False)
    skip_tracerfy = getattr(args, "skip_tracerfy", False)

    skip_smarty = getattr(args, "skip_smarty", False)

    opts = PipelineOptions(
        skip_smarty=skip_smarty,
        skip_zillow=skip_zillow,
        skip_obituary=True,  # already have DOD + heirs from obituary
        skip_tax=True,
        skip_entity_filter=False,
        source_label="daily-obits",
    )
    enriched = run_enrichment_pipeline(property_owners, opts)

    if not enriched:
        logging.warning("No records after enrichment")
        return

    # Step 5: Tracerfy skip trace the HEIR, not the deceased
    if not skip_tracerfy and config.TRACERFY_API_KEY:
        from tracerfy_skip_tracer import batch_skip_trace
        tracerfy_stats = batch_skip_trace(enriched)
        logging.info(
            "Tracerfy: %d/%d matched, %d phones, %d emails, $%.2f",
            tracerfy_stats.get("matched", 0), tracerfy_stats.get("submitted", 0),
            tracerfy_stats.get("phones_found", 0), tracerfy_stats.get("emails_found", 0),
            tracerfy_stats.get("cost", 0.0),
        )

    # Step 6: Write output CSV
    from data_formatter import write_csv
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    csv_path = config.OUTPUT_DIR / ("ok_obits_preprobate_%s.csv" % timestamp)

    # Pre-probate/obituary tags (Pre-Probate, obituary, county, month, deceased,
    # dm_*, no_heirs, needs_deep_prospecting, etc.) are generated by
    # datasift_formatter._build_tags() -- called automatically for both this
    # raw CSV's "Tags" column and the DataSift-ready CSV below. Don't set
    # notice.tags by hand here; it's never read back out.

    write_csv(enriched, csv_path)
    logging.info("Output: %s (%d pre-probate leads)", csv_path, len(enriched))

    # DataSift upload (same logic as every other ingestion path) -- previously
    # this mode only ever wrote the raw CSV, so obituary/pre-probate leads
    # never got a DataSift-formatted Notes/Tags upload (Message Board content,
    # Call Prep, needs_deep_prospecting, etc.) unless someone manually ran
    # csv-import afterward.
    _upload_notices_to_datasift(enriched, args, source_label="preprobate_obituary")

    # Summary
    logging.info("== Pre-Probate Obituary Summary ==")
    logging.info("  Obituaries scraped: %d (direct funeral homes)", len(obits))
    logging.info("  Property owners: %d", len(property_owners))
    logging.info("  After enrichment: %d", len(enriched))
    with_dm = sum(1 for n in enriched if n.decision_maker_name)
    logging.info("  With decision maker: %d", with_dm)


def resolve_obit_leads(
    obits: list[dict], headless: bool = True, apply_buy_box: bool = True,
) -> list["NoticeData"]:
    """Resolve a list of scraped obituary dicts into property-owning leads with a DM.

    Shared by _run_daily_obits and any one-off rerun script that gathers its
    own `obits` list (e.g. re-fetching specific historical obituaries) —
    keeps the Assessor lookup + decision-maker resolution logic in one place.

    Steps: build NoticeData -> pre-seed DM from obituary -> Assessor address
    lookup -> drop non-owners -> buy box filter -> DM resolution (obituary
    text first, Assessor co-owner fallback, spouse last-name fill / SUSPECT
    swap).

    apply_buy_box: set False to keep records that fail the buy-box filter
    (e.g. an operator has manually decided a specific lead is worth pursuing
    despite failing sqft/year-built/type criteria).
    """
    from tulsa_assessor import lookup_addresses_tulsa

    # Step 2: Search Assessor for each deceased — do they own property?
    logging.info("Checking %d deceased for property ownership...", len(obits))
    notices = []
    from notice_parser import NoticeData
    for obit in obits:
        name = obit["name"]
        dod = obit.get("date_of_death", "")
        age = obit.get("age")

        # Prefix source_url so datasift_formatter._build_tags() detects
        # this as a pre-probate obituary record (not a court-filed probate)
        # and applies Pre-Probate + obituary tags instead of "probate".
        detail_url = obit.get("detail_url", "")
        source = obit.get("source", "")
        if source == "funeral_home_direct" and detail_url:
            source_url = "funeral_home_direct:" + detail_url
        else:
            source_url = detail_url

        n = NoticeData(
            date_added=datetime.now().strftime("%Y-%m-%d"),
            owner_name=name,
            decedent_name=name,
            notice_type="probate",
            county="Tulsa",
            state="OK",
            source_url=source_url,
            obituary_url=detail_url,
            owner_deceased="yes",
            date_of_death=dod,
        )
        n._obit_data = obit
        notices.append(n)

    # Step 1.5: Pre-seed DM from obituary BEFORE Assessor lookup so _dm_corroborates
    # can pick the correct property when multiple records exist for the same surname.
    # This also becomes the FINAL DM — Step 3 won't override it (obituary = authoritative
    # for who is living; Assessor deed can be years out of date with deceased co-owners).
    for n in notices:
        obit_pre = getattr(n, "_obit_data", {})
        survived_pre = obit_pre.get("survived_by_raw", "")
        if survived_pre and not n.decision_maker_name:
            heir_pre = _parse_heirs_from_obituary(survived_pre, n.owner_name or "")
            n._heir_parse_result = heir_pre  # preserve all_heirs for Step 3 SUSPECT swap
            if heir_pre.get("dm_name"):
                n.decision_maker_name = heir_pre["dm_name"]
                n.decision_maker_relationship = heir_pre.get("dm_relationship", "")

    # Run Assessor lookup to find property addresses
    import asyncio
    lookup_result = asyncio.run(lookup_addresses_tulsa(notices, headless=headless))
    found_count = lookup_result[0] if isinstance(lookup_result, tuple) else lookup_result
    logging.info("Assessor: %d/%d deceased owned property", found_count, len(notices))

    # Keep only records where an address was confirmed by Assessor.
    # Records dropped here are either renters, out-of-county owners, or
    # non-spousal DM cases where all Assessor records had unmatched co-owners
    # (can't confirm the right property — better to drop than mail wrong house).
    property_owners = [n for n in notices if n.address and n.address.strip()]
    no_property = len(notices) - len(property_owners)
    if no_property:
        logging.info(
            "Dropped %d deceased with no confirmed property "
            "(renters, out-of-county, or unconfirmed co-owner)",
            no_property,
        )

    if not property_owners:
        logging.warning("No property-owning deceased found in obituaries")
        return []

    # Buy box filter — shared logic from enrichment_pipeline
    if apply_buy_box:
        from enrichment_pipeline import filter_buy_box
        property_owners = filter_buy_box(property_owners)
        if not property_owners:
            logging.warning("No records passed buy box filter")
            return []

    # Ensure owner_name is populated (Assessor lookup may have left it blank)
    for n in property_owners:
        if not n.owner_name and n.decedent_name:
            n.owner_name = n.decedent_name

    # Step 3: Identify decision maker — obituary text first, Assessor co-owner as fallback.
    # The obituary "survived by" text is the authoritative source for who is ALIVE.
    # The Assessor deed can be years out of date (deceased spouses stay on deeds).
    # Only use Assessor co-owner when obituary gave us nothing.
    import re as _re
    for n in property_owners:
        # Priority 1: obituary text — most reliable for living heirs
        if not n.decision_maker_name:
            obit = getattr(n, "_obit_data", {})
            survived_by = obit.get("survived_by_raw", "")
            if survived_by:
                heir_info = _parse_heirs_from_obituary(survived_by, n.owner_name or n.decedent_name)
                if heir_info.get("dm_name"):
                    n.decision_maker_name = heir_info["dm_name"]
                    n.decision_maker_relationship = heir_info.get("dm_relationship", "")
                    n.dm_confidence = heir_info.get("confidence", "")
                    n.dm_confidence_reason = "obituary"
                    logging.info("  %s -> DM (obituary): %s (%s)",
                                 n.owner_name or n.decedent_name,
                                 n.decision_maker_name,
                                 n.decision_maker_relationship)

        # Priority 2: Assessor co-owner — only when obituary gave us nothing
        # e.g. obituary has no "survived by" section but deed shows joint ownership
        tax_name = n.tax_owner_name or ""
        co_owner_match = _re.search(r"\s+(?:&|AND)\s+(.+?)(?:\s+(?:TTEE|TRUST|REV\b|ET\s|CO\s)|$)",
                                    tax_name, _re.IGNORECASE)
        if co_owner_match and not n.decision_maker_name:
            co_raw = co_owner_match.group(1).strip().title()
            deceased_name_raw = n.owner_name or n.decedent_name or ""
            deceased_last = deceased_name_raw.strip().split()[-1] if deceased_name_raw else ""
            deceased_first = deceased_name_raw.strip().split()[0].upper() if deceased_name_raw else ""
            co_parts = co_raw.split()

            # If the co-owner IS the deceased (deceased listed 2nd on deed, e.g.
            # "MOORE, BILLY JAMES AND DONNA JEAN" where Donna is the decedent),
            # flip and use the primary owner (before AND) instead.
            co_first_upper = co_parts[0].upper() if co_parts else ""
            if deceased_first and co_first_upper and (
                deceased_first == co_first_upper
                or (len(deceased_first) >= 4 and len(co_first_upper) >= 4
                    and deceased_first[:4] == co_first_upper[:4])
            ):
                primary_m = _re.match(r"^(.+?)\s+(?:&|AND)\s+", tax_name, _re.IGNORECASE)
                if primary_m:
                    praw = primary_m.group(1).strip()
                    if "," in praw:
                        last_p, first_p = praw.split(",", 1)
                        praw = first_p.strip() + " " + last_p.strip()
                    co_raw = praw.title()
                    co_parts = co_raw.split()
                    logging.debug(
                        "  %s: deceased listed 2nd on deed — using primary owner '%s'",
                        deceased_name_raw, co_raw,
                    )

            # "CHELSI J" or "CHELSI" → no real last name; resolve to full name.
            # "CHELSI JOHNSON" (last name 3+ chars) → keep as-is.
            has_real_last = len(co_parts) >= 2 and len(co_parts[-1]) >= 3
            if not has_real_last:
                co_first = co_parts[0]
                # Search the obituary's survived_by text for "{co_first} LastName"
                obit_data = getattr(n, "_obit_data", {})
                obit_survived = obit_data.get("survived_by_raw", "") or ""
                stop_words = {"the", "and", "his", "her", "with", "who", "their",
                              "that", "this", "from", "was", "are", "for", "she"}
                full_match = _re.search(
                    r'\b' + _re.escape(co_first) + r'\s+([A-Z][a-z]{2,})\b',
                    obit_survived, _re.IGNORECASE,
                )
                if full_match and full_match.group(1).lower() not in stop_words:
                    co_raw = co_first + " " + full_match.group(1).title()
                elif deceased_last:
                    co_raw = co_first + " " + deceased_last.title()
            n.decision_maker_name = co_raw
            n.decision_maker_relationship = "spouse"
            n.dm_confidence = "high"
            n.dm_confidence_reason = "assessor_co_owner"
            logging.info("  %s -> DM (Assessor co-owner): %s", n.owner_name or n.decedent_name, co_raw)

        # Step 3b: spouse last-name fill + SUSPECT swap.
        # We now know the Assessor result, so we can make better decisions:
        #   corr=False  → spouse NOT on deed (likely remarried) → try child swap, no last-name fill
        #   corr=None   → sole owner (no co-owner to check)     → original spouse assumed, fill last name
        #   corr=True   → spouse confirmed on deed              → fill last name
        if n.decision_maker_relationship == "spouse":
            from tulsa_assessor import _dm_corroborates
            corr = _dm_corroborates(n.tax_owner_name or "", n.decision_maker_name)
            heir_result = getattr(n, "_heir_parse_result", {})
            all_heirs = heir_result.get("all_heirs") or []
            deceased_surname = (n.owner_name or n.decedent_name or "").strip().split()
            deceased_surname = deceased_surname[-1].upper() if deceased_surname else ""

            if corr is False:
                # Spouse not on deed — likely remarried. Prefer a same-surname child.
                swapped = False
                for heir in all_heirs:
                    heir_clean = _clean_dm_name_from_obit(heir)
                    if not heir_clean:
                        continue
                    heir_parts = heir_clean.strip().split()
                    if (len(heir_parts) >= 2
                            and deceased_surname
                            and heir_parts[-1].upper() == deceased_surname):
                        logging.info(
                            "  %s -> SUSPECT spouse '%s' swapped to child '%s' "
                            "(spouse not on Assessor deed, likely remarried)",
                            n.owner_name or n.decedent_name,
                            n.decision_maker_name, heir_clean,
                        )
                        n.decision_maker_name = heir_clean
                        n.decision_maker_relationship = "child"
                        n.dm_confidence = "medium"
                        n.dm_confidence_reason = "suspect_spouse_swap"
                        swapped = True
                        break
                if not swapped:
                    logging.info(
                        "  %s -> SUSPECT spouse '%s' — no same-surname child in all_heirs, "
                        "keeping spouse (no last-name fill — may be remarried)",
                        n.owner_name or n.decedent_name, n.decision_maker_name,
                    )
            else:
                # corr=True (on deed) or corr=None (sole owner) — original spouse, fill last name
                dm_parts = n.decision_maker_name.strip().split()
                if len(dm_parts) == 1 and deceased_surname and len(deceased_surname) > 2:
                    n.decision_maker_name = f"{dm_parts[0]} {deceased_surname.title()}"
                    logging.info(
                        "  %s -> spouse '%s' last name filled from deceased surname",
                        n.owner_name or n.decedent_name, n.decision_maker_name,
                    )

    return property_owners


_OBIT_JUNK_WORDS = frozenset({
    "of", "his", "her", "the", "a", "an", "their", "and", "or", "with",
    "by", "in", "on", "at", "to", "from", "as", "is", "was", "are", "were",
    "wife", "husband", "mother", "father", "son", "daughter", "child",
    "children", "sibling", "brother", "sister", "parent", "survivor",
    "survived", "also", "one", "two", "three", "four", "five",
})


def _clean_dm_name_from_obit(name: str) -> str:
    """Strip location/extra phrases from an obituary-parsed DM name.

    'Carisa of Collinsville'  -> 'Carisa'   (location phrase)
    'Tommy Goad and Michelle' -> 'Tommy Goad'  (joint DMs)
    'Rachel Tatro of Tulsa, OK' -> 'Rachel Tatro'
    'Debbie Denton of'        -> 'Debbie Denton'  (trailing junk word)
    'Terri Jean Hozhabri'     -> 'Terri Hozhabri'  (drop middle name, keep first+last)
    'of'  -> ''  (LLM hallucination — function word, not a name)
    """
    import re as _re
    if not name:
        return name
    # Strip " of [CityName]" and anything after
    name = _re.sub(r"\s+of\s+[A-Z][A-Za-z]+.*$", "", name).strip()
    # Strip " and ..." suffix (joint/multiple DMs — keep only first)
    name = _re.sub(r"\s+and\s+.*$", "", name, flags=_re.IGNORECASE).strip()
    # Strip trailing junk words ("Debbie Denton of" → "Debbie Denton")
    name = _re.sub(r"\s+\b(?:of|and|the|a|an|his|her)\b\s*$", "", name, flags=_re.IGNORECASE).strip()
    # Strip trailing comma/punctuation
    name = name.rstrip(",.;").strip()
    words = name.split()
    if not words:
        return ""
    # Expand CamelCase-fused tokens before further checks.
    # "JeanHozhabri" → ["Jean", "Hozhabri"]; skips Mc/Mac prefixes (McDonald, MacPherson).
    _MC_MAC = _re.compile(r"^(?:Mc|Mac)[A-Z]")
    expanded = []
    for w in words:
        parts = _re.findall(r"[A-Z][a-z]{2,}", w) if w and w[0].isupper() else []
        if len(parts) >= 2 and "".join(parts) == w and not _MC_MAC.match(w):
            expanded.extend(parts)
        else:
            expanded.append(w)
    words = expanded
    # Reject any name whose first word is a function/relation word — the LLM
    # extracted a phrase instead of a person's name ("of almost", "of 38 years")
    if words[0].lower() in _OBIT_JUNK_WORDS:
        return ""
    # Reject single tokens under 3 chars
    if len(words) == 1 and len(words[0]) <= 2:
        return ""
    # Normalize 3-word names to first + last (drop middle name).
    # "Terri Jean Hozhabri" → "Terri Hozhabri"; "Mary Louise Johnson" → "Mary Johnson".
    if len(words) == 3:
        name = f"{words[0]} {words[2]}"
    else:
        name = " ".join(words)
    return name


def _fill_spouse_lastname(result: dict, deceased_name: str) -> dict:
    """If a spouse was extracted with only a first name, append the deceased's last name.

    'Donna' + deceased 'Jay Crabb' -> 'Donna Crabb'
    Only applies when relationship == 'spouse' and dm_name is a single word.
    """
    if result.get("dm_relationship") != "spouse":
        return result
    dm = (result.get("dm_name") or "").strip()
    if not dm or len(dm.split()) >= 2:
        return result  # already has last name
    deceased_parts = deceased_name.strip().split()
    if not deceased_parts:
        return result
    deceased_last = deceased_parts[-1]
    if deceased_last.lower() in _OBIT_JUNK_WORDS or len(deceased_last) <= 2:
        return result
    result["dm_name"] = f"{dm} {deceased_last}"
    return result


def _parse_heirs_from_obituary(survived_by: str, deceased_name: str) -> dict:
    """Extract the best decision maker from 'survived by' text.

    Returns dict with dm_name, dm_relationship, confidence.
    Uses regex for now; LLM parsing available when ANTHROPIC_API_KEY is set.
    """
    import re as _re

    result = {"dm_name": "", "dm_relationship": "", "confidence": ""}

    if not survived_by:
        return result

    text = survived_by

    # Try LLM parsing first if available
    if config.ANTHROPIC_API_KEY and config.ANTHROPIC_API_KEY != "sk-ant-api03-your-key-here":
        llm_result = _llm_parse_heirs(text, deceased_name)
        if llm_result:
            llm_result["dm_name"] = _clean_dm_name_from_obit(llm_result.get("dm_name", ""))
            # If LLM chose a spouse (first-name-only) but there's a full-name child
            # with the deceased's last name in all_heirs, prefer that child.
            # Guards against in-laws: Bryan (Crabb) has a DIFFERENT last name from Jay Crabb,
            # so he won't displace Donna (spouse). But a "Moore, William" child would beat Maurice.
            if llm_result.get("dm_relationship") == "spouse":
                all_heirs = llm_result.get("all_heirs") or []
                dm_parts = (llm_result.get("dm_name") or "").strip().split()
                deceased_surname = deceased_name.strip().split()[-1].upper() if deceased_name.strip() else ""
                for heir in all_heirs:
                    heir_clean = _clean_dm_name_from_obit(heir)
                    if not heir_clean:
                        continue
                    heir_parts = heir_clean.strip().split()
                    heir_surname = heir_parts[-1].upper() if heir_parts else ""
                    # Full-name child sharing deceased's surname beats a spouse with no last name
                    if (len(heir_parts) >= 2
                            and len(dm_parts) < 2
                            and deceased_surname
                            and heir_surname == deceased_surname):
                        llm_result["dm_name"] = heir_clean
                        llm_result["dm_relationship"] = "child"
                        llm_result["confidence"] = "medium"
                        break
            return llm_result

    # Regex fallback: look for spouse first, then children
    # "his wife, Sandra" / "her beloved wife, Donna" / "her husband, John"
    # Limit to 0-2 words between pronoun and wife/husband — prevents matching
    # "his children Nicole Hooper and husband James" where James is a son-in-law.
    # Require "his/her" pronoun — the bare "husband/wife" pattern caused false
    # positives on "Nicole Hooper and husband James" (son-in-law in children list).
    spouse_patterns = [
        _re.compile(r"(?:his|her)\s+(?:\w+\s+){0,2}(?:wife|husband|spouse),?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)", _re.IGNORECASE),
    ]
    for pat in spouse_patterns:
        m = pat.search(text)
        if m:
            result["dm_name"] = _clean_dm_name_from_obit(m.group(1).strip())
            result["dm_relationship"] = "spouse"
            result["confidence"] = "high"
            return result

    # Children: "his daughter Rachel Tatro" / "her son Michael"
    child_patterns = [
        _re.compile(r"(?:his|her)\s+(?:daughter|son),?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)", _re.IGNORECASE),
        _re.compile(r"(?:children|sons|daughters),?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)", _re.IGNORECASE),
    ]
    for pat in child_patterns:
        m = pat.search(text)
        if m:
            result["dm_name"] = _clean_dm_name_from_obit(m.group(1).strip())
            result["dm_relationship"] = "child"
            result["confidence"] = "medium"
            return result

    # Sibling: "his sister Teresa" / "her brother Dean"
    sibling_patterns = [
        _re.compile(r"(?:his|her)\s+(?:sister|brother),?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)", _re.IGNORECASE),
    ]
    for pat in sibling_patterns:
        m = pat.search(text)
        if m:
            result["dm_name"] = _clean_dm_name_from_obit(m.group(1).strip())
            result["dm_relationship"] = "sibling"
            result["confidence"] = "low"
            return result

    # Parents: "his parents, Charles Leroy Martin and Ella Darlene Martin"
    parent_patterns = [
        _re.compile(r"(?:his|her)\s+parents?,?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)", _re.IGNORECASE),
    ]
    for pat in parent_patterns:
        m = pat.search(text)
        if m:
            result["dm_name"] = _clean_dm_name_from_obit(m.group(1).strip())
            result["dm_relationship"] = "parent"
            result["confidence"] = "low"
            return result

    return result


def _llm_parse_heirs(survived_by_text: str, deceased_name: str) -> Optional[dict]:
    """Use Claude Haiku to parse structured heir data from obituary text."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

        prompt = (
            "From this obituary excerpt, identify the best person to contact about "
            "selling the deceased's property. Priority: spouse > adult child > sibling > parent.\n\n"
            "Deceased: %s\n\n"
            "Obituary 'survived by' text: %s\n\n"
            "Rules:\n"
            "- dm_name MUST be an actual person's name (first name or full name) literally present in the text.\n"
            "- Do NOT return generic words like 'wife', 'husband', 'of', 'his', 'her', 'children', etc.\n"
            "- Only use relationship 'spouse' if the text explicitly names a surviving husband or wife.\n"
            "- If the deceased's spouse is also mentioned as deceased (e.g. 'preceded by his husband'), "
            "do not list them as a survivor — list the children instead.\n"
            "- If you cannot find a clear name, return empty string for dm_name.\n"
            "- PRIORITY ORDER: adult child with full name (First Last) who shares the deceased's surname > "
            "spouse > adult child with only first name > sibling > parent.\n"
            "- IMPORTANT: When the text says 'X and her husband, Y', 'X and his wife, Y', or "
            "'X and husband Y' or 'X and wife Y' where X is already identified as a child of the "
            "deceased, Y is a son-in-law or daughter-in-law — do NOT list Y as the deceased's spouse or DM.\n"
            "- Example: 'his children Nicole Hooper and husband James' → Nicole Hooper is the child; "
            "James is Nicole's husband (son-in-law). The deceased has NO surviving spouse listed here.\n"
            "- Exclude sons-in-law and daughters-in-law entirely.\n\n"
            "Respond with ONLY a JSON object (no markdown):\n"
            '{"dm_name": "First Last", "dm_relationship": "spouse|child|sibling|parent", '
            '"confidence": "high|medium|low", "all_heirs": ["Name1", "Name2"]}'
        ) % (deceased_name, survived_by_text[:1000])

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        import json
        text = resp.content[0].text.strip()
        return json.loads(text)
    except Exception as e:
        logging.debug("LLM heir parse failed: %s", e)
        return None


def _run_manage_presets(args) -> None:
    """Run the DataSift filter preset management workflow."""
    from datasift_uploader import run_manage_presets_workflow

    discover = getattr(args, "discover", False)
    add_sold = getattr(args, "add_sold_exclusion", False)
    create_seq = getattr(args, "create_sold_sequence", False)

    # Default to discover if no flags specified
    if not (discover or add_sold or create_seq):
        discover = True

    preset_folders = None
    if getattr(args, "preset_folders", None):
        preset_folders = [f.strip() for f in args.preset_folders.split(",")]

    result = asyncio.run(run_manage_presets_workflow(
        discover=discover,
        add_sold_exclusion=add_sold,
        create_sequence=create_seq,
        preset_folders=preset_folders,
    ))

    if result.get("success"):
        logging.info("Manage presets: %s", result.get("message", "OK"))
        if result.get("discovery"):
            disc = result["discovery"]
            for folder, presets in disc.get("preset_folders", {}).items():
                logging.info("  Folder '%s': %s", folder, presets)
            logging.info("  Sequences: %s", disc.get("sequences", []))
        if result.get("presets"):
            p = result["presets"]
            logging.info("  Updated: %s", p.get("updated", []))
            logging.info("  Failed: %s", p.get("failed", []))
        if result.get("sequence"):
            logging.info("  Sequence: %s", result["sequence"].get("message"))
    else:
        logging.error("Manage presets failed: %s", result.get("message"))
        sys.exit(1)


def _run_manage_list(args) -> None:
    """Trigger enrichment and/or skip trace on an existing DataSift list."""
    import asyncio as _asyncio
    from playwright.async_api import async_playwright as _apw

    list_name = getattr(args, "list_name", None)
    if not list_name:
        logging.error("--list-name is required for manage-list mode")
        sys.exit(1)

    do_enrich = not getattr(args, "no_enrich", False)
    do_skip_trace = not getattr(args, "no_skip_trace", False)

    async def _run():
        from datasift_core import login
        from datasift_uploader import enrich_records, skip_trace_records

        async with _apw() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()
            try:
                logged_in = await login(page, config.DATASIFT_EMAIL, config.DATASIFT_PASSWORD)
                if not logged_in:
                    logging.error("DataSift login failed")
                    return

                if do_enrich:
                    result = await enrich_records(page, list_name)
                    if result.get("success"):
                        logging.info("Enrichment: %s", result.get("message", "OK"))
                    else:
                        logging.error("Enrichment failed: %s", result.get("message"))

                if do_skip_trace:
                    result = await skip_trace_records(page, list_name)
                    if result.get("success"):
                        logging.info("Skip trace: %s", result.get("message", "OK"))
                    else:
                        logging.error("Skip trace failed: %s", result.get("message"))
            finally:
                await browser.close()

    _asyncio.run(_run())


def _run_manage_sold(args) -> None:
    """Run the SiftMap sold properties management workflow."""
    from datasift_uploader import run_manage_sold_workflow

    # Parse counties if provided, otherwise use default (Knox, Blount)
    counties = None
    if args.counties and args.counties.lower() != "all":
        counties = [c.strip().title() for c in args.counties.split(",")]

    result = asyncio.run(run_manage_sold_workflow(
        counties=counties,
        months_back=getattr(args, "months_back", 1),
        min_sale_price=getattr(args, "min_sale_price", 1000),
        sold_tag_date=getattr(args, "sold_tag_date", None),
    ))

    if result.get("success"):
        logging.info("Manage sold: %s", result.get("message", "OK"))
        logging.info("  Counties: %s", ", ".join(result.get("counties_processed", [])))
        logging.info("  Total records: %d", result.get("total_records", 0))
    else:
        logging.error("Manage sold failed: %s", result.get("message"))
        sys.exit(1)


def cli_main() -> None:
    """Run as standalone CLI."""
    parser = argparse.ArgumentParser(
        description="SiftStack — full-stack REI operations platform"
    )
    parser.add_argument(
        "mode",
        choices=[
            "daily", "historical", "pdf-import", "photo-import", "dropbox-watch",
            "csv-import", "phone-validate", "manage-sold", "manage-presets", "manage-list",
            "daily-obits",
            # New analysis & workflow modes
            "comp", "rehab", "analyze-deal", "market-analysis", "buyer-prospect",
            "deep-prospect", "lead-manage", "setup-sequences", "niche-sequential",
            "playbook",
        ],
        help=(
            "daily/historical = scrape notices; daily-obits = pre-probate obituary leads; "
            "pdf-import/photo-import = import from files; "
            "dropbox-watch = poll Dropbox; csv-import = re-enrich CSV; "
            "phone-validate = Trestle scoring; manage-sold/manage-presets = DataSift ops; "
            "comp = comparable sales ARV; rehab = rehab cost estimate; "
            "analyze-deal = full deal analysis; market-analysis = zip code scoring; "
            "buyer-prospect = cash buyer lists; deep-prospect = 4-level research; "
            "lead-manage = 4 Pillars qualification; setup-sequences = CRM automation; "
            "niche-sequential = marketing cycle; playbook = SOP generator"
        ),
    )
    parser.add_argument(
        "--counties",
        type=str,
        default=None,
        help='Comma-separated counties to scrape (e.g. "Knox,Blount" or "all")',
    )
    parser.add_argument(
        "--types",
        type=str,
        default=None,
        help='Comma-separated notice types (e.g. "foreclosure,probate" or "all")',
    )
    parser.add_argument(
        "--split",
        action="store_true",
        help="Output separate CSV files per notice type",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Override date cutoff (YYYY-MM-DD). Overrides daily/historical mode logic.",
    )
    parser.add_argument(
        "--force-tax-refresh",
        action="store_true",
        help="Ignore cached tax-delinquent scan and do a full re-scan from oktaxrolls.com",
    )
    parser.add_argument(
        "--force-tinstar",
        action="store_true",
        help="Run TinStar scraper regardless of day of week (normally Fridays only)",
    )
    parser.add_argument(
        "--max-notices",
        type=int,
        default=0,
        help="Stop after scraping this many notices (0 = no limit)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )

    # PDF import arguments
    parser.add_argument(
        "--pdf-path",
        type=str,
        default=None,
        help="Path to scanned tax sale PDF (required for pdf-import mode)",
    )
    parser.add_argument(
        "--pdf-county",
        type=str,
        default=None,
        help='County name for PDF import, e.g. "Knox" (required for pdf-import mode)',
    )
    parser.add_argument(
        "--pdf-date",
        type=str,
        default=None,
        help="Date for PDF records (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--regex-only",
        action="store_true",
        help="Skip LLM parsing and use regex only (pdf-import mode)",
    )
    # Photo import arguments
    parser.add_argument(
        "--folder",
        type=str,
        default=None,
        help="Path to folder of phone photos (required for photo-import mode)",
    )
    parser.add_argument(
        "--photo-county",
        type=str,
        default=None,
        dest="photo_county",
        help='County name for photo import, e.g. "Knox" (required for photo-import mode)',
    )
    parser.add_argument(
        "--photo-type",
        type=str,
        default=None,
        dest="photo_type",
        help='Notice type for photo import, e.g. "eviction" (required for photo-import mode)',
    )
    parser.add_argument(
        "--photo-date",
        type=str,
        default=None,
        help="Date for photo records (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--no-perspective-correct",
        action="store_true",
        dest="no_perspective_correct",
        help="Skip perspective correction in photo preprocessing (photo-import mode)",
    )
    # Dropbox watcher arguments
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=None,
        dest="poll_interval",
        help="Seconds between Dropbox polls (default: 900 = 15 min)",
    )
    parser.add_argument(
        "--max-polls",
        type=int,
        default=None,
        dest="max_polls",
        help="Maximum number of poll cycles (default: infinite)",
    )
    parser.add_argument(
        "--no-delete",
        action="store_true",
        dest="no_delete",
        help="Don't delete photos from Dropbox after processing",
    )
    # CSV import arguments
    parser.add_argument(
        "--csv-path",
        type=str,
        default=None,
        help="Path to existing CSV file to re-enrich (required for csv-import mode)",
    )
    parser.add_argument(
        "--csv-county",
        type=str,
        default=None,
        help='County name for CSV import, e.g. "Knox" (sets county for records missing it)',
    )

    parser.add_argument(
        "--skip-smarty",
        action="store_true",
        help="Skip Smarty address standardization",
    )
    parser.add_argument(
        "--skip-zillow",
        action="store_true",
        help="Skip Zillow property enrichment",
    )
    parser.add_argument(
        "--skip-tax",
        action="store_true",
        help="Skip tax delinquency enrichment",
    )
    parser.add_argument(
        "--skip-obituary",
        action="store_true",
        help="Skip obituary search for deceased owner detection",
    )
    parser.add_argument(
        "--skip-ancestry",
        action="store_true",
        help="Skip Ancestry.com lookup (SSDI + obituary collection)",
    )
    parser.add_argument(
        "--skip-geocode",
        action="store_true",
        help="Skip reverse geocode retry for failed Smarty lookups",
    )
    parser.add_argument(
        "--skip-dm-address",
        action="store_true",
        help="Skip decision-maker mailing address lookup",
    )
    parser.add_argument(
        "--skip-heir-verification",
        action="store_true",
        help="Skip heir alive/dead verification loop (still runs obituary search)",
    )
    parser.add_argument(
        "--max-heir-depth",
        type=int,
        default=2,
        help="Max recursion depth for heir verification (default: 2)",
    )
    parser.add_argument(
        "--tracerfy-tier1",
        action="store_true",
        help="Use Tracerfy as primary DM address lookup ($0.02/record)",
    )
    parser.add_argument(
        "--deep-heirs",
        action="store_true",
        help="Resolve deceased-owner heirs via Enformion Person Search (grounded "
             "relatives graph, ~$0.35/match) instead of obituary survivor parsing",
    )
    parser.add_argument(
        "--skip-tracerfy",
        action="store_true",
        help="Skip Tracerfy batch skip trace (phones + emails) before DataSift upload",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run browsers in headed (visible) mode — lets you watch every click",
    )
    parser.add_argument(
        "--rescrape",
        action="store_true",
        help="daily-obits: clear today's seen-file entries and re-process all obituaries from today",
    )
    parser.add_argument(
        "--skip-acclaim",
        action="store_true",
        help="Skip Acclaim (Tulsa County Clerk) scraper — use for daily OSCN-only runs",
    )
    parser.add_argument(
        "--acclaim-only",
        action="store_true",
        help="Run Acclaim scraper only — skips OSCN, TinStar, Tulsa World, and tax scraper",
    )
    parser.add_argument(
        "--llm-backend",
        choices=["anthropic", "ollama", "openrouter"],
        default=os.getenv("LLM_BACKEND", "anthropic"),
        help="LLM backend: 'anthropic' (Claude Haiku, paid) or 'ollama' (local, free)",
    )
    parser.add_argument(
        "--research-entities",
        action="store_true",
        help="Research entity-owned properties to find the person behind LLCs/Corps (web search + LLM)",
    )
    # Buy box / filter toggles — control which property types pass through
    parser.add_argument(
        "--include-vacant",
        action="store_true",
        help="Keep vacant land parcels (default: filtered out). Use if your buy box includes land deals.",
    )
    parser.add_argument(
        "--include-commercial",
        action="store_true",
        help="Keep commercial properties (default: filtered out). Use if your buy box includes commercial.",
    )
    parser.add_argument(
        "--include-entities",
        action="store_true",
        help="Keep entity-owned records (LLC, Corp, etc.) without filtering. Default: removed unless --research-entities finds a person.",
    )
    parser.add_argument(
        "--upload-datasift",
        action="store_true",
        help="Upload results to DataSift.ai via Playwright (requires DATASIFT_EMAIL/PASSWORD)",
    )
    parser.add_argument(
        "--csv-only",
        action="store_true",
        help=(
            "With --upload-datasift: generate the DataSift-ready CSV(s) but skip the "
            "automated Playwright upload step. Use while doing manual uploads."
        ),
    )
    parser.add_argument(
        "--datasift-update-list",
        metavar="LIST_NAME",
        default=None,
        help=(
            "Use DataSift 'Update Data' mode to update an existing list rather than "
            "creating a new one. Provide the exact list name, e.g. 'SiftStack 2026-06-12'. "
            "Prevents duplicate records when re-uploading with fixes."
        ),
    )
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip DataSift property enrichment after upload",
    )
    parser.add_argument(
        "--no-skip-trace",
        action="store_true",
        help="Skip DataSift skip trace after upload",
    )
    parser.add_argument(
        "--notify-slack",
        action="store_true",
        help="Send run summary to Slack/Discord webhook (requires SLACK_WEBHOOK_URL)",
    )
    parser.add_argument(
        "--audit-records",
        action="store_true",
        help="Audit DataSift for incomplete records (future: daily check via Playwright)",
    )

    # Phone validation arguments
    parser.add_argument(
        "--list-name",
        type=str,
        default=None,
        help="DataSift list name to export phones from (phone-validate mode)",
    )
    parser.add_argument(
        "--preset-folder",
        type=str,
        default=None,
        help="DataSift preset folder to export phones from (phone-validate mode)",
    )
    parser.add_argument(
        "--all-records",
        action="store_true",
        help="Export all DataSift records for phone validation (phone-validate mode)",
    )
    parser.add_argument(
        "--estimate",
        action="store_true",
        help="Show phone validation cost estimate only, no API calls (phone-validate mode)",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Skip uploading phone tags back to DataSift (phone-validate mode)",
    )
    parser.add_argument(
        "--custom-tiers",
        type=str,
        default=None,
        help='JSON custom tier boundaries, e.g. \'{"Hot": [80,100], "Cold": [0,79]}\' (phone-validate mode)',
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Concurrent Trestle API requests per batch (phone-validate mode, default: 10)",
    )
    parser.add_argument(
        "--add-litigator",
        action="store_true",
        help="Include litigator risk check in phone validation (phone-validate mode)",
    )
    parser.add_argument(
        "--run-csv",
        type=str,
        default=None,
        help="Path to current run's output CSV — filters phone tags to only this run's records (phone-validate mode)",
    )

    # Daily-obits arguments
    parser.add_argument(
        "--obit-pages",
        type=int,
        default=3,
        help="Max Echovita listing pages to scrape (20 obits/page, default: 3)",
    )
    parser.add_argument(
        "--obit-days",
        type=int,
        default=7,
        help="Only include obituaries with DOD within this many days (default: 7)",
    )
    parser.add_argument(
        "--obit-min-days",
        type=int,
        default=0,
        help="Skip obituaries newer than this many days (default: 0 = no minimum). "
             "Use 7 to skip fresh stubs and target 7-14 day window with better heir data.",
    )

    # Manage sold arguments
    parser.add_argument(
        "--months-back",
        type=int,
        default=1,
        help="Months of sales to pull from SiftMap (manage-sold mode, default: 1)",
    )
    parser.add_argument(
        "--min-sale-price",
        type=int,
        default=1000,
        help="Min sale price to exclude deed transfers (manage-sold mode, default: 1000)",
    )
    parser.add_argument(
        "--sold-tag-date",
        type=str,
        default=None,
        help="Tag date in YYYY-MM format (manage-sold mode, default: current month)",
    )

    # Manage presets arguments
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Discover and list all preset folders, presets, and sequences (manage-presets mode)",
    )
    parser.add_argument(
        "--add-sold-exclusion",
        action="store_true",
        help="Update existing presets to exclude Sold status/tag (manage-presets mode)",
    )
    parser.add_argument(
        "--create-sold-sequence",
        action="store_true",
        help="Create Sold Property Cleanup sequence (manage-presets mode)",
    )
    parser.add_argument(
        "--preset-folders",
        type=str,
        default=None,
        help='Comma-separated preset folder names to target (manage-presets mode, default: all)',
    )

    # ── New analysis & workflow mode arguments ────────────────────────
    # Comp analysis
    parser.add_argument("--address", type=str, default=None,
                        help="Property address (comp/rehab/analyze-deal modes)")
    parser.add_argument("--city", type=str, default=None,
                        help="Property city (comp/rehab/analyze-deal modes)")
    parser.add_argument("--zip-code", type=str, default=None,
                        help="Property ZIP code (comp/rehab/analyze-deal modes)")
    parser.add_argument("--radius", type=float, default=0.5,
                        help="Comp search radius in miles (comp mode, default: 0.5)")
    parser.add_argument("--months", type=int, default=6,
                        help="Comp lookback months (comp mode, default: 6)")

    # Rehab estimation
    parser.add_argument("--tier", type=int, default=2, choices=[1, 2, 3, 4],
                        help="Finish tier 1-4 (rehab mode, default: 2)")
    parser.add_argument("--scope", type=str, default="full", choices=["full", "wholetail"],
                        help="Rehab scope (rehab mode, default: full)")
    parser.add_argument("--region", type=str, default="knoxville",
                        help="Regional pricing (rehab mode, default: knoxville)")
    parser.add_argument("--sqft", type=int, default=0,
                        help="Property sqft override (rehab mode)")
    parser.add_argument("--bedrooms", type=int, default=0,
                        help="Bedrooms override (rehab mode)")
    parser.add_argument("--bathrooms", type=float, default=0,
                        help="Bathrooms override (rehab mode)")

    # Deal analysis
    parser.add_argument("--purchase-price", type=float, default=0,
                        help="Purchase price (analyze-deal mode, default: auto-calculate MAO)")
    parser.add_argument("--rehab-tier", type=int, default=2, choices=[1, 2, 3, 4],
                        help="Rehab tier for deal analysis (default: 2)")
    parser.add_argument("--exit-strategy", type=str, default="flip",
                        choices=["flip", "wholesale", "hold"],
                        help="Exit strategy (analyze-deal mode, default: flip)")

    # Market analysis
    parser.add_argument("--zip-codes", type=str, default=None,
                        help="Comma-separated ZIP codes to analyze (market-analysis mode)")
    parser.add_argument("--monthly-budget", type=float, default=5000,
                        help="Monthly marketing budget for allocation (market-analysis mode)")

    # Buyer prospecting
    parser.add_argument("--min-transactions", type=int, default=2,
                        help="Min transactions to qualify as investor (buyer-prospect mode)")

    # Deep prospecting
    parser.add_argument("--depth", type=int, default=3, choices=[1, 2, 3, 4],
                        help="Research depth level 1-4 (deep-prospect mode, default: 3)")

    # Lead management
    parser.add_argument("--lead-action", type=str, default="qualify",
                        choices=["qualify", "report"],
                        help="Lead management action (lead-manage mode)")

    # Sequence setup
    parser.add_argument("--seq-folder", type=str, default="all",
                        choices=["lead-management", "acquisitions", "transactions",
                                 "deep-prospecting", "default", "all"],
                        help="Sequence folder to create (setup-sequences mode)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without creating (setup-sequences/niche-sequential)")

    # Niche sequential
    parser.add_argument("--channel", type=str, default="sms",
                        choices=["sms", "call", "mail", "dp"],
                        help="Marketing channel (niche-sequential mode)")
    parser.add_argument("--day", type=int, default=1, choices=[1, 2, 3],
                        help="Cycle day 1-3 (niche-sequential mode)")
    parser.add_argument("--ns-action", type=str, default="execute",
                        choices=["execute", "setup-presets", "status"],
                        help="Niche sequential action (niche-sequential mode)")

    # Playbook
    parser.add_argument("--blueprint", type=str, default="wholesale",
                        choices=["wholesale", "flip", "hold", "hybrid"],
                        help="Investment blueprint (playbook mode)")
    parser.add_argument("--market", type=str, default="knoxville",
                        help="Target market (playbook mode)")
    parser.add_argument("--team-size", type=int, default=1,
                        help="Team size 1/2/5 (playbook mode)")

    args = parser.parse_args()

    # Apply LLM backend override from CLI flag
    if hasattr(args, "llm_backend") and args.llm_backend:
        import config as cfg
        cfg.LLM_BACKEND = args.llm_backend
        if args.llm_backend == "ollama":
            logging.info("LLM backend: Ollama (%s)", cfg.OLLAMA_MODEL)
        elif args.llm_backend == "openrouter":
            logging.info("LLM backend: OpenRouter (%s)", cfg.OPENROUTER_MODEL)

    setup_logging(args.verbose)

    # ── Early search filtering (needed by preflight) ─────────────────
    # For scrape modes, filter searches now so preflight can scope its
    # credential checks to the sources actually in use this run.
    _early_counties = None
    _early_types = None
    if args.mode in {"daily", "historical"}:
        if args.counties and args.counties.lower() != "all":
            _early_counties = [c.strip() for c in args.counties.split(",")]
        if args.types and args.types.lower() != "all":
            _early_types = [t.strip() for t in args.types.split(",")]
    _preflight_searches = _filter_searches(_early_counties, _early_types) or None
    if getattr(args, "acclaim_only", False) and _preflight_searches:
        _preflight_searches = [s for s in _preflight_searches if s.source == "acclaimed"] or None

    # ── Preflight health checks ──────────────────────────────────────
    preflight_failures = _preflight_check(args.mode, _preflight_searches)
    if preflight_failures:
        for f in preflight_failures:
            logging.error("Preflight FAILED: %s", f)
        # Send Slack alert so unattended runs are visible
        try:
            from slack_notifier import notify_preflight_failure
            notify_preflight_failure(preflight_failures)
        except Exception:
            pass  # Don't fail on notification failure
        sys.exit(1)
    logging.info("Preflight checks passed")

    # ── New analysis & workflow modes ─────────────────────────────────

    if args.mode == "comp":
        if not args.address:
            print("ERROR: --address is required for comp mode")
            return
        from comp_analyzer import run_comp_analysis
        result = run_comp_analysis(
            address=args.address, city=args.city or "", zip_code=args.zip_code or "",
            radius=args.radius, months=args.months,
        )
        if "error" in result:
            logger.error("Comp analysis failed: %s", result["error"])
        else:
            print(f"Comp report: {result['report_path']}")
            arv = result["arv"]
            print(f"ARV: ${arv.arv_low:,.0f} (low) / ${arv.arv_mid:,.0f} (mid) / ${arv.arv_high:,.0f} (high)")
            print(f"Confidence: {arv.confidence} — {arv.confidence_reason}")
        return

    if args.mode == "rehab":
        if not args.address:
            print("ERROR: --address is required for rehab mode")
            return
        from rehab_estimator import run_rehab_estimate
        result = run_rehab_estimate(
            address=args.address, sqft=args.sqft, bedrooms=args.bedrooms or 3,
            bathrooms=args.bathrooms or 2.0, tier=args.tier, scope=args.scope,
            region=args.region,
        )
        full = result["full_estimate"]
        wt = result["wholetail_estimate"]
        print(f"Rehab report: {result['report_path']}")
        print(f"Full rehab: ${full.grand_total:,.0f} ({full.total_weeks:.0f} weeks)")
        print(f"Wholetail:  ${wt.grand_total:,.0f} ({wt.total_weeks:.0f} weeks)")
        return

    if args.mode == "analyze-deal":
        if not args.address:
            print("ERROR: --address is required for analyze-deal mode")
            return
        from deal_analyzer import run_deal_analysis
        result = run_deal_analysis(
            address=args.address, city=args.city or "", zip_code=args.zip_code or "",
            purchase_price=args.purchase_price, rehab_tier=args.rehab_tier,
            exit_strategy=args.exit_strategy, region=args.region,
            radius=args.radius, months=args.months,
        )
        if "error" in result:
            logger.error("Deal analysis failed: %s", result["error"])
        else:
            pkg = result["package"]
            print(f"Deal report: {result['report_path']}")
            print(f"Recommendation: {pkg.recommendation}")
            print(f"ARV: ${pkg.arv.arv_mid:,.0f} | Rehab: ${pkg.rehab_full.grand_total:,.0f}")
            print(f"Flip MAO: ${pkg.mao.flip_mao:,.0f} | Profit: ${pkg.flip.net_profit:,.0f} ({pkg.flip.roi_pct:.0f}% ROI)")
        return

    if args.mode == "market-analysis":
        from market_analyzer import run_market_analysis
        counties = args.counties.split(",") if args.counties else None
        zip_codes = args.zip_codes.split(",") if args.zip_codes else None
        result = run_market_analysis(
            counties=counties, zip_codes=zip_codes,
            monthly_budget=args.monthly_budget,
        )
        if "error" in result:
            logger.error("Market analysis failed: %s", result["error"])
        else:
            report = result["report"]
            print(f"Market report: {result['report_path']}")
            print(f"Analyzed {report.total_zips} zips, {report.total_notices} total notices")
            if report.top_zips:
                top = report.top_zips[0]
                print(f"Top zip: {top.zip_code} (score {top.score:.1f}, grade {top.grade})")
        return

    if args.mode == "buyer-prospect":
        from buyer_prospector import run_buyer_prospecting
        counties = args.counties.split(",") if args.counties else None
        result = run_buyer_prospecting(
            counties=counties,
            months_back=args.months_back,
            min_transactions=args.min_transactions,
        )
        if "error" in result:
            logger.error("Buyer prospecting failed: %s", result["error"])
        else:
            report = result["report"]
            print(f"Buyer report: {result['report_path']}")
            print(f"Found {report.total_investors} investors")
            print(f"CSV: {result.get('csv_path', 'N/A')}")
        return

    if args.mode == "deep-prospect":
        csv_path = args.csv_path if hasattr(args, "csv_path") and args.csv_path else ""
        if not csv_path:
            csvs = sorted(config.OUTPUT_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
            csv_path = str(csvs[0]) if csvs else ""
        if not csv_path:
            print("ERROR: --csv-path required or place CSVs in output/")
            return
        import asyncio
        from deep_prospector import run_deep_prospecting
        result = asyncio.run(run_deep_prospecting(
            csv_path=csv_path, depth=args.depth,
            max_records=args.max_notices if hasattr(args, "max_notices") else 0,
        ))
        if "error" in result:
            logger.error("Deep prospecting failed: %s", result["error"])
        else:
            stats = result["stats"]
            print(f"Report: {result['report_path']}")
            print(f"Processed {stats['total']} records at depth {args.depth}")
            print(f"Phones: {stats['phones_found']} | Deceased: {stats['deceased_confirmed']} | DMs: {stats['dms_identified']}")
        return

    if args.mode == "lead-manage":
        from lead_manager import run_lead_management
        csv_path = args.csv_path if hasattr(args, "csv_path") and args.csv_path else ""
        result = run_lead_management(
            action=args.lead_action, csv_path=csv_path,
        )
        if "error" in result:
            logger.error("Lead management failed: %s", result["error"])
        else:
            print(f"STABM report: {result['report_path']}")
            print(f"Total: {result['total']} | Hot: {result['hot']} | Warm: {result['warm']} | Cold: {result['cold']}")
        return

    if args.mode == "setup-sequences":
        from sequence_templates import get_templates, list_templates, preview_sequence
        templates = get_templates(args.seq_folder)
        if args.dry_run:
            print(f"DRY RUN — Would create {len(templates)} sequences in DataSift:")
            for t in templates:
                preview = preview_sequence(t)
                print(f"  [{preview['folder']}] {preview['name']}")
                print(f"    Trigger: {preview['trigger']}")
                print(f"    Actions: {len(preview['actions'])}")
        else:
            print(f"Sequence creation requires Playwright — {len(templates)} templates ready")
            print("Templates defined. DataSift Playwright creation coming in next build.")
            print("\nTemplate list:")
            print(list_templates())
        return

    if args.mode == "niche-sequential":
        from niche_sequential import run_niche_sequential
        result = run_niche_sequential(
            list_name=args.list_name or "",
            channel=args.channel, day=args.day,
            csv_path=args.csv_path if hasattr(args, "csv_path") and args.csv_path else "",
            action=args.ns_action,
        )
        if "error" in result:
            logger.error("Niche sequential failed: %s", result["error"])
        elif "output" in result:
            print(f"Exported: {result['output']}")
            print(f"Channel: {result['channel']}, Day {result['day']}, {result['records']} records")
        elif "presets" in result:
            for p in result["presets"]:
                print(f"  {p['name']}: {p['description']}")
        return

    if args.mode == "playbook":
        from playbook_generator import run_playbook_generator
        result = run_playbook_generator(
            blueprint=args.blueprint, market=args.market,
            team_size=args.team_size,
        )
        print(f"Playbook: {result['playbook_path']}")
        print(f"Blueprint: {result['blueprint'].title()} | Market: {result['market'].title()} | Team: {result['team_size']}")
        return

    # Phone validation mode — separate pipeline
    if args.mode == "phone-validate":
        _run_phone_validate(args)
        return

    # Manage presets mode — filter preset + sequence management
    if args.mode == "manage-presets":
        _run_manage_presets(args)
        return

    # Manage list mode — enrich/skip-trace an existing DataSift list
    if args.mode == "manage-list":
        _run_manage_list(args)
        return

    # Manage sold properties mode — SiftMap workflow
    if args.mode == "manage-sold":
        _run_manage_sold(args)
        return

    # PDF import mode — separate pipeline
    if args.mode == "pdf-import":
        _run_pdf_import(args)
        return

    # Photo import mode — separate pipeline
    if args.mode == "photo-import":
        _run_photo_import(args)
        return

    # Dropbox watcher mode — polls for new photos
    if args.mode == "dropbox-watch":
        from dropbox_watcher import run_watcher
        run_watcher(
            poll_interval=args.poll_interval,
            delete_after=not getattr(args, "no_delete", False),
            max_polls=args.max_polls,
        )
        return

    # CSV re-import mode — separate pipeline
    if args.mode == "csv-import":
        _run_csv_import(args)
        return

    # Daily obituary pre-probate pipeline
    if args.mode == "daily-obits":
        _run_daily_obits(args)
        return

    # Filter saved searches
    counties = None
    if args.counties and args.counties.lower() != "all":
        counties = [c.strip() for c in args.counties.split(",")]

    types = None
    if args.types and args.types.lower() != "all":
        types = [t.strip() for t in args.types.split(",")]

    searches = _filter_searches(counties, types)
    if not searches:
        logging.error("No saved searches match the given --counties / --types filters")
        sys.exit(1)

    logging.info(
        "Running %d saved searches: %s",
        len(searches),
        ", ".join(s.saved_search_name for s in searches),
    )

    try:
        _run_scrape_pipeline(args, searches)
    except Exception as e:
        logging.exception("Pipeline failed with unhandled error")
        try:
            from slack_notifier import notify_error
            notify_error("Pipeline (top-level)", e, context=f"mode={args.mode}")
        except Exception:
            pass
        sys.exit(1)


def _run_scrape_pipeline(args, searches) -> None:
    """Run the daily/historical scrape → enrich → export → upload pipeline."""
    from datetime import timedelta

    # ── Resolve the since_date shared across all scrapers ────────────
    since_date: str | None = args.since
    if not since_date:
        if args.mode == "daily":
            from scraper import load_last_run_date
            since_date = load_last_run_date()
            if not since_date:
                since_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
                logging.info("No prior run state found — pulling last 7 days (%s)", since_date)
        elif args.mode == "historical":
            since_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

    # ── Partition searches by data source ────────────────────────────
    _acclaim_only = getattr(args, "acclaim_only", False)

    tnpn_searches       = [] if _acclaim_only else [s for s in searches if s.source == "tnpn"]
    oscn_searches        = [] if _acclaim_only else [s for s in searches if s.source == "oscn"]
    tinstar_searches     = [] if _acclaim_only else [s for s in searches if s.source == "tinstar"]
    oktaxrolls_searches  = [] if _acclaim_only else [s for s in searches if s.source == "oktaxrolls"]
    tulsaworld_searches  = [] if _acclaim_only else [s for s in searches if s.source == "tulsaworld"]
    acclaimed_searches   = [s for s in searches if s.source == "acclaimed"]

    all_notices: list = []
    _td_notices: list = []  # tax delinquent records — written to a separate CSV
    _acclaim_new_instrs: dict = {}  # instrument# → date; saved to cache after CSV write

    # ── TN Public Notice scraper (existing Playwright + CAPTCHA flow) ─
    if tnpn_searches:
        logging.info(
            "Scraping %d TN saved search(es) via tnpublicnotice.com",
            len(tnpn_searches),
        )
        tn_notices = asyncio.run(scrape_all(
            mode=args.mode, searches=tnpn_searches,
            llm_api_key=config.ANTHROPIC_API_KEY or None,
            since_date_override=since_date,
            max_notices=args.max_notices,
        ))
        all_notices.extend(tn_notices)

    # ── Oklahoma OSCN scraper (plain HTTP, no login/CAPTCHA) ─────────
    if oscn_searches:
        from oscn_scraper import scrape_oscn
        logging.info("Scraping %d OSCN search(es) for Tulsa County", len(oscn_searches))
        for search in oscn_searches:
            try:
                ok_notices = scrape_oscn(
                    county=search.county,
                    notice_type=search.notice_type,
                    since_date=since_date or (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),
                )
                all_notices.extend(ok_notices)
            except Exception:
                logging.exception("OSCN scrape failed for %s/%s", search.county, search.notice_type)

    # ── Tulsa County Treasurer tax-delinquent scraper ────────────────
    if oktaxrolls_searches:
        from tulsa_tax_delinquent import scrape_tulsa_tax_delinquent
        logging.info("Scraping Tulsa County tax-delinquent list (2+ years, Real Estate)")
        try:
            max_td = args.max_notices if getattr(args, "max_notices", 0) > 0 else 500
            td_notices = scrape_tulsa_tax_delinquent(
                min_years_delinquent=2,
                min_amount=500.0,
                max_records=max_td,
                force_refresh=getattr(args, "force_tax_refresh", False),
            )
            _td_notices.extend(td_notices)
            logging.info("Tax delinquent: %d records fetched (writing to separate CSV)", len(td_notices))
        except Exception:
            logging.exception("Tax delinquent scrape failed")

    # ── TinStar sheriff sale scraper (Fridays only — list updates weekly) ──
    _is_friday = datetime.now().weekday() == 4
    _force_tinstar = getattr(args, "force_tinstar", False)
    if tinstar_searches and (_is_friday or _force_tinstar):
        from tinstar_scraper import scrape_tinstar
        logging.info("Scraping %d TinStar search(es) for Tulsa County", len(tinstar_searches))
        for search in tinstar_searches:
            try:
                ts_notices = asyncio.run(scrape_tinstar(
                    county=search.county,
                    since_date=since_date,
                    email=config.TINSTAR_EMAIL,
                    password=config.TINSTAR_PASSWORD,
                ))
                all_notices.extend(ts_notices)
            except Exception:
                logging.exception("TinStar scrape failed for %s", search.county)
    elif tinstar_searches and not _is_friday:
        logging.info("TinStar skipped (runs Fridays only — use --force-tinstar to override)")

    # ── Tulsa World legal notice scraper (Column.us REST API) ────────────
    if tulsaworld_searches:
        from tulsa_world_scraper import scrape_tulsa_world
        logging.info("Scraping Tulsa World legal notices (Column.us API)")
        try:
            tw_notices = scrape_tulsa_world(
                since_date=since_date,
                days_back=30,
            )
            all_notices.extend(tw_notices)
            logging.info("Tulsa World: %d real-estate notices", len(tw_notices))
        except Exception:
            logging.exception("Tulsa World scrape failed")

    # ── Acclaim (Tulsa County Clerk) recorded document scraper ───────────
    if acclaimed_searches and not getattr(args, "skip_acclaim", False):
        if not config.ACCLAIM_EMAIL or not config.ACCLAIM_PASSWORD:
            logging.warning(
                "Acclaim: acclaim_EMAIL / acclaim_PASSWORD not set in .env -- skipping"
            )
        else:
            import json as _json
            import re as _re
            from acclaimed_scraper import scrape_acclaimed

            logging.info("Scraping Tulsa County Clerk (Acclaim) for Lis Pendens + Sheriff Deeds")

            # Acclaim database is verified ~7 days behind real-time.  Use a 14-day
            # floor so each weekly run covers a full week of verified recordings.
            _acclaim_14d = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
            _acclaim_since = since_date if since_date and since_date < _acclaim_14d else _acclaim_14d

            # Load seen-IDs cache — prevents re-exporting instruments across weekly runs
            _acc_cache_path = config.ACCLAIM_SEEN_IDS_FILE
            _acc_seen: dict = {}
            if _acc_cache_path.exists():
                try:
                    _acc_seen = _json.loads(_acc_cache_path.read_text(encoding="utf-8"))
                except Exception as _e:
                    logging.warning("Acclaim: could not load seen-IDs cache: %s", _e)
            _acc_seen_keys_before: set = set(_acc_seen.keys())

            def _acc_instr(url: str) -> str:
                m = _re.search(r"instrumentNumber=(\d+)", url)
                return m.group(1) if m else ""

            try:
                acc_notices = asyncio.run(scrape_acclaimed(
                    since_date=_acclaim_since,
                    email=config.ACCLAIM_EMAIL,
                    password=config.ACCLAIM_PASSWORD,
                    max_records=args.max_notices if getattr(args, "max_notices", 0) > 0 else 500,
                    seen_ids=_acc_seen,
                    headless=not getattr(args, "headed", False),
                ))

                # All cache-filtered instruments were already skipped inside scrape_acclaimed.
                # Register new instruments for cache persistence, tagging with doc_type.
                for _n in acc_notices:
                    _instr = _acc_instr(_n.source_url)
                    if _instr and _instr not in _acc_seen_keys_before:
                        import re as _re_dt
                        _dt_m = _re_dt.search(r'\|acclaim_doc_type:(\S+)', _n.raw_text or '')
                        _dt_label = _dt_m.group(1) if _dt_m else ""
                        _acclaim_new_instrs[_instr] = (
                            f"{datetime.now().strftime('%Y-%m-%d')}|{_dt_label}"
                            if _dt_label else datetime.now().strftime("%Y-%m-%d")
                        )
                # Also save instruments dropped in OCR phase — _enrich_notices_with_pdf
                # mutated _acc_seen directly with |DROPPED entries so they skip next run.
                for _k, _v in _acc_seen.items():
                    if _k not in _acc_seen_keys_before:
                        _acclaim_new_instrs.setdefault(_k, _v)

                all_notices.extend(acc_notices)
                logging.info("Acclaim: %d new records", len(acc_notices))
            except Exception:
                logging.exception("Acclaim scrape failed")

    notices = all_notices
    logging.info("Total raw notices across all sources: %d", len(notices))

    # Cap total records when --max-notices is set (applies to all sources)
    if getattr(args, "max_notices", 0) > 0 and len(notices) > args.max_notices:
        notices = notices[: args.max_notices]
        logging.info("Truncated to %d notices (--max-notices)", args.max_notices)

    # Handle async probate lookup before pipeline (requires asyncio.run)
    probate_notices = [n for n in notices if n.notice_type == "probate" and n.decedent_name and not n.address]
    if probate_notices:
        try:
            from property_lookup import lookup_decedent_properties
            logging.info("Looking up property addresses for %d probate notices...", len(probate_notices))
            asyncio.run(lookup_decedent_properties(probate_notices))
        except ImportError:
            logging.warning("property_lookup module not found -- skipping property lookup")
        except Exception as e:
            logging.warning("Property lookup failed: %s -- continuing without lookups", e)

    # Tulsa County Assessor address lookup for:
    # 1. OK records with no address at all
    # 2. Acclaim records flagged by OCR (kept without a verified street address from the PDF)
    tulsa_no_addr = [
        n for n in notices
        if n.county == "Tulsa" and n.state == "OK"
        and (not n.address.strip() or getattr(n, "needs_assessor_lookup", False))
    ]
    if tulsa_no_addr:
        try:
            from tulsa_assessor import lookup_addresses_tulsa
            logging.info(
                "Tulsa Assessor: looking up addresses for %d records...", len(tulsa_no_addr)
            )
            found, missed = asyncio.run(lookup_addresses_tulsa(tulsa_no_addr))
            logging.info("Tulsa Assessor: %d found, %d not found", found, missed)
        except Exception as e:
            logging.warning("Tulsa Assessor lookup failed: %s -- continuing", e)

    # Drop OK records that still have no address after all lookup tiers.
    # These are genuine misses (renter, out-of-county property, pre-death transfer)
    # and cannot be mailed or uploaded to DataSift usefully.
    before_drop = len(notices)
    notices = [n for n in notices if not (n.state == "OK" and not n.address.strip())]
    dropped = before_drop - len(notices)
    if dropped:
        logging.info("Removed %d OK records with no address after all lookup tiers", dropped)

    # Run unified enrichment pipeline
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

    opts = PipelineOptions(
        skip_parcel_lookup=True,  # web scrape notices don't have parcel IDs
        skip_vacant_filter=getattr(args, "include_vacant", False),
        skip_commercial_filter=getattr(args, "include_commercial", False),
        skip_entity_filter=getattr(args, "include_entities", False),
        skip_smarty=getattr(args, "skip_smarty", False),
        skip_zillow=getattr(args, "skip_zillow", False),
        skip_tax=getattr(args, "skip_tax", False),
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=args.skip_obituary,
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_dm_address=args.skip_dm_address,
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        deep_heirs=getattr(args, "deep_heirs", False),
        source_label=f"CLI {args.mode}",
    )
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No notices found")
        # Send Slack ping even on empty runs so operators know the job
        # ran successfully (vs silently dying). Previously sys.exit(0)
        # fired before the Slack block at the bottom of this function.
        if getattr(args, "notify_slack", False):
            try:
                from slack_notifier import send_slack_notification
                send_slack_notification([])
            except Exception:
                logging.exception("Slack notification for empty run failed")
        sys.exit(0)

    # Tracerfy batch skip trace (phones + emails for all records)
    tiers_map: dict = {}
    tracerfy_stats: dict = {}
    if not getattr(args, "skip_tracerfy", False):
        import config as cfg
        if cfg.TRACERFY_API_KEY:
            from tracerfy_skip_tracer import batch_skip_trace
            tracerfy_stats = batch_skip_trace(notices)
            if tracerfy_stats.get("credits_exhausted"):
                logging.error(
                    "TRACERFY OUT OF CREDITS — skip trace disabled for this run. "
                    "Add credits at https://tracerfy.com/billing to resume phone/email lookups."
                )
            logging.info(
                "Tracerfy: %d/%d matched, %d phones, %d emails, $%.2f",
                tracerfy_stats.get("matched", 0), tracerfy_stats.get("submitted", 0),
                tracerfy_stats.get("phones_found", 0), tracerfy_stats.get("emails_found", 0),
                tracerfy_stats.get("cost", 0.0),
            )
            # Trestle scoring deferred to phone-validate post-DataSift step
            # (see feedback-pipeline-order in memory)

    # Host notice screenshots (proof-of-source) so the link travels with the
    # record into DataSift (Notes + "Notice Screenshot" field). Uses Google
    # Drive when configured, otherwise references the local PNG path.
    try:
        from notice_screenshot import (
            host_screenshots_via_drive,
            set_local_screenshot_urls,
        )
        captured = sum(1 for n in notices if n.notice_screenshot_path)
        if captured:
            hosted = host_screenshots_via_drive(
                notices,
                config.GOOGLE_DRIVE_FOLDER_ID,
                config.GOOGLE_SERVICE_ACCOUNT_KEY,
            )
            local_only = set_local_screenshot_urls(notices)
            logging.info(
                "Notice screenshots: %d captured, %d hosted on Drive, %d local-only",
                captured, hosted, local_only,
            )
    except Exception:
        logging.exception("Notice screenshot hosting failed, continuing")

    # Write output
    if args.split:
        paths = write_csv_by_type(notices)
        for p in paths:
            logging.info("Output: %s", p)
    else:
        path = write_csv(notices)
        logging.info("Output: %s", path)

    # ── Persist Acclaim seen-IDs cache (only after successful CSV write) ──
    if _acclaim_new_instrs:
        import json as _json
        _acc_cache_path = config.ACCLAIM_SEEN_IDS_FILE
        try:
            existing: dict = {}
            if _acc_cache_path.exists():
                existing = _json.loads(_acc_cache_path.read_text(encoding="utf-8"))
            existing.update(_acclaim_new_instrs)
            # Prune entries older than 60 days to prevent unbounded growth
            _cutoff = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
            existing = {k: v for k, v in existing.items() if v >= _cutoff}
            _acc_cache_path.write_text(_json.dumps(existing, indent=2), encoding="utf-8")
            logging.info("Acclaim: seen-IDs cache saved (%d total entries)", len(existing))
        except Exception as _e:
            logging.warning("Acclaim: could not save seen-IDs cache: %s", _e)

    # ── Tax delinquent separate CSV (no Smarty/Zillow — preserve API credits) ──
    if _td_notices:
        try:
            from datetime import datetime as _dt
            _td_filename = f"ok_tax_delinquent_{_dt.now().strftime('%Y-%m-%d_%H%M%S')}.csv"
            _td_path = write_csv(_td_notices, filename=_td_filename)
            logging.info("Tax delinquent output: %s (%d records)", _td_path, len(_td_notices))
        except Exception:
            logging.exception("Tax delinquent CSV write failed")

    # Generate deep-prospecting PDFs for deceased/DM/heir records.
    # Matches the Apify branch behavior so CLI runs get the same reports —
    # includes the Case Summary section added for deceased-owner records.
    dp_candidates = [
        n for n in notices
        if n.owner_deceased == "yes" or n.heir_map_json or n.decision_maker_name
    ]
    if dp_candidates:
        try:
            from report_generator import generate_record_pdf
            report_dir = Path("output/reports")
            generated = 0
            for n in dp_candidates:
                try:
                    pdf_path = generate_record_pdf(
                        n, output_dir=report_dir, phone_tiers=tiers_map,
                    )
                    logging.info("Report generated: %s", pdf_path)
                    generated += 1
                except Exception:
                    logging.exception("PDF generation failed for %s", n.address)
            logging.info(
                "Generated %d/%d deep-prospecting PDFs in %s",
                generated, len(dp_candidates), report_dir,
            )
        except Exception:
            logging.exception("Report generator import failed")

    # Label the DataSift CSV by whichever scraper source(s) actually ran this
    # pass, e.g. "acclaim" for an --acclaim-only run, "tnpn_oscn_acclaim" for
    # a full mixed daily run.
    _active_sources = []
    if tnpn_searches:
        _active_sources.append("tnpn")
    if oscn_searches:
        _active_sources.append("oscn")
    if tinstar_searches:
        _active_sources.append("tinstar")
    if oktaxrolls_searches:
        _active_sources.append("taxrolls")
    if tulsaworld_searches:
        _active_sources.append("tulsaworld")
    if acclaimed_searches:
        _active_sources.append("acclaim")
    _scrape_source_label = "_".join(_active_sources) or args.mode

    upload_result = _upload_notices_to_datasift(notices, args, source_label=_scrape_source_label)

    # Slack/Discord notification
    if getattr(args, "notify_slack", False):
        from slack_notifier import send_slack_notification

        send_slack_notification(notices, upload_result=upload_result)

    # Audit DataSift for incomplete records (future daily check)
    if getattr(args, "audit_records", False):
        logging.info("--audit-records: Not yet implemented. "
                      "Will check DataSift Incomplete tab via Playwright in a future build.")

    # Persist last_run_date so the next daily run only pulls new records
    if args.mode == "daily":
        from scraper import save_last_run_date
        save_last_run_date()
        logging.info("Run state saved — next daily run will start from today")

    logging.info("Done — %d notices exported", len(notices))


# ── Entry point ───────────────────────────────────────────────────────


if __name__ == "__main__":
    if os.environ.get("APIFY_IS_AT_HOME") or os.environ.get("APIFY_TOKEN"):
        # Running inside Apify platform or with apify run
        asyncio.run(actor_main())
    else:
        # Standalone CLI
        cli_main()
