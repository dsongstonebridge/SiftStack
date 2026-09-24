# CLAUDE.md — SiftStack

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**SiftStack** — Full-stack real estate investing operations platform built around DataSift.ai CRM. Covers the entire REI business lifecycle:

1. **Data Acquisition:** Web scraping tnpublicnotice.com (foreclosures, tax sales, probates), scanned PDF import, courthouse terminal photo import (probate, eviction, code violations, divorce), Dropbox auto-polling
2. **Enrichment Pipeline:** 10+ steps — Smarty address standardization, Zillow property data, Knox County Tax API, obituary/heir research, Ancestry.com SSDI, Tracerfy skip trace, Trestle phone scoring, entity research
3. **Deal Analysis:** Comparable sales (Two-Bucket ARV), rehab estimation (4-tier room-by-room), deal analyzer (MAO/ROI/financing scenarios)
4. **Market Intelligence:** Zip code scoring, Market Finder reports, cash buyer list building, investor portfolio analysis
5. **CRM Automation:** DataSift upload, 26 TCA sequence templates, 12 niche sequential marketing presets, filter preset management, SiftMap sold property tagging
6. **Lead Management:** 4 Pillars of Motivation auto-qualification, STABM daily routine, pipeline reporting, deep prospecting (4-level framework)
7. **Operations:** Acquisition playbook generator (SOPs, scripts, checklists), Slack/Discord notifications, Google Drive upload, Apify Actor deployment

Currently focused on Knox and Blount counties, Tennessee.

8. **REI Skill Library:** 13 Claude Co-Work skill files (`.skill`/`.plugin` ZIPs) for distribution to DataSift community via [learn.datasift.ai/claude-skills-rei](https://learn.datasift.ai/claude-skills-rei). Skills teach Claude specific REI workflows when uploaded to Co-Work sessions or Projects.

## Commands

```bash
# Setup
pip install -r requirements.txt
playwright install chromium
cp .env.example .env  # then fill in credentials

# Run
python src/main.py daily                          # new notices since last run
python src/main.py historical                     # last 12 months of data
python src/main.py daily --split                  # separate CSV per county+type
python src/main.py daily --counties Knox          # only Knox county
python src/main.py daily --types foreclosure,probate  # only specific types
python src/main.py daily -v                       # verbose/debug logging

# DataSift preset/sequence management
python src/main.py manage-presets --discover                      # list all presets and sequences
python src/main.py manage-presets --add-sold-exclusion            # add Sold exclusion to all presets
python src/main.py manage-presets --create-sold-sequence          # create Sold cleanup sequence
python src/main.py manage-presets --all                           # discovery + update + sequence

# SiftMap sold property tagging
python src/main.py manage-sold --months-back 12                   # tag sold properties (last 12 months)
python src/main.py manage-sold --counties Knox --min-sale-price 5000

# Courthouse photo import (build 1.0.28+)
python src/main.py photo-import --folder ./photos --photo-county Knox --photo-type probate
python src/main.py photo-import --folder ./photos --photo-county Knox --photo-type eviction --skip-obituary
python src/main.py dropbox-watch                                  # auto-poll Dropbox for new photos
python src/main.py dropbox-watch --poll-interval 300 --max-polls 5  # 5-min interval, 5 cycles
python src/main.py dropbox-watch --no-delete                      # keep photos in Dropbox after processing
```

All source files are in `src/` and imports assume `src/` is the working directory. Run from project root with `python src/main.py` or set `PYTHONPATH=src`.

## Architecture

**Data flows:**
- **Web scrape:** `main.py` → `scraper.py` → `captcha_solver.py` → `notice_parser.py` + `foreclosure_filter.py` → enrichment → CSV
- **PDF import:** `main.py` → `pdf_importer.py` (pypdfium2 → `image_utils.py` OCR) → enrichment → CSV
- **Photo import:** `main.py` → `photo_importer.py` (OpenCV → `image_utils.py` OCR → `llm_parser.py`) → enrichment → CSV
- **Dropbox watch:** `dropbox_watcher.py` → `photo_importer.py` → enrichment → CSV (auto-polling loop)
- **Market Finder:** `extract_market_finder.py` → DataSift Market Finder (Playwright) → paginate all ZIP + neighborhood data → JSON → `generate_knox_report.py` → 7-sheet Excel

- **main.py** — CLI entry point. Parses args (`daily`/`historical`, `--split`, `--counties`, `--types`, `-v`). Filters saved searches by county/type, orchestrates scrape → dedup → export, logs run summary stats.
- **scraper.py** — Playwright browser automation. Reuses saved session cookies when possible, falls back to fresh login. Selects each saved search from the Smart Search dropdown (triggers ASP.NET postback), paginates results (50/page max), clicks each View button to open notice detail pages. Uses `last_run.json` for daily mode state, `cookies.json` for session persistence.
- **captcha_solver.py** — Solves reCAPTCHA v2 via **2Captcha API** on every notice detail page. Sends websiteURL + sitekey, gets back a `g-recaptcha-response` token, injects it, clicks "View Notice". Retries up to 3 times. This is the primary bottleneck (~10-30s per notice).
- **notice_parser.py** — Extracts structured fields from raw notice text using regex. There are NO structured HTML fields on the site — address, owner, dates are all embedded in free-text notice bodies. Defines the `NoticeData` dataclass used throughout.
- **foreclosure_filter.py** — Filters foreclosure search results to only keep real first-to-market trustee sales. Matches against observed title variations (substitute/successor trustee sales). Non-foreclosure notice types pass through unfiltered.
- **data_formatter.py** — Deduplicates by address (keeps most recent), then converts `NoticeData` list to Sift upload CSV. Split mode produces `{county}_{type}_{timestamp}.csv` files.
- **config.py** — Credentials (from `.env`), ASP.NET element selectors, saved search definitions, rate limiting constants, paths, image processing thresholds.
- **image_utils.py** — Shared OCR utilities used by both `pdf_importer.py` and `photo_importer.py`. Exports `fix_rotation()` (Tesseract OSD) and `ocr_page(image, psm)` with configurable page segmentation mode. Handles Tesseract binary detection.
- **photo_importer.py** — Courthouse phone photo import. OpenCV preprocessing chain (EXIF transpose → blur check → bilateral filter → perspective correction → Otsu threshold) → Tesseract OCR (PSM 4) → LLM parsing → NoticeData. Supports all 7 notice types.
- **dropbox_watcher.py** — Cursor-based Dropbox folder polling. Downloads new photos, resolves county + notice_type from folder path (`/Knox/eviction/photo.jpg`), processes through photo_importer, deletes from Dropbox after success. State persisted to `dropbox_state.json` + `photo_state.json`.
- **report_generator.py** — Generates per-record PDF deep prospecting reports using reportlab. Includes property summary, signing chain with phone tiers, valuation, deceased owner detection. Output to `output/reports/`.
- **extract_market_finder.py** — Playwright automation to extract ALL ZIP code + neighborhood data from DataSift Market Finder. Handles styled-component dropdowns, pagination (20 rows/page), Beamer popup dismissal. Outputs JSON. See "Market Finder Extraction Patterns" below.
- **market_analyzer.py** — ZIP code scoring engine. 6-factor weighted composite (Distress 30%, Value 20%, Equity 15%, Tax Delinquency 15%, Competition 10%, DOM 10%). Grades A/B/C/D, budget allocation across top ZIPs. Reads from scraped notice CSVs in `output/`.
- **drive_uploader.py** — Google Drive upload via service account. `upload_file()` (generic, returns webViewLink) and `upload_csv()` (CSV-specific, returns file ID).

## Site-Specific Details

The site is **ASP.NET WebForms** — all navigation uses `__doPostBack()` with ViewState. Session IDs are embedded in URL paths (`/(S({guid}))/`). Playwright is required because direct HTTP requests would need to manage ViewState/EventValidation manually.

**reCAPTCHA v2 is required on every single notice detail page**, even when logged in. There is no CAPTCHA on login, search, or results pages. The sitekey is hardcoded in `config.py`.

## Saved Searches

8 searches defined in `config.py` as `SAVED_SEARCHES`. Each maps to an exact dropdown option name on the Smart Search dashboard:
- Knox & Blount × (Foreclosure V2, Tax Sale V2, Tax Delinquent V2, Probate V2)

Filterable via `--counties` and `--types` CLI args (comma-separated, or omit for all).

## Key Domain Rules

- **Foreclosure filtering is critical.** Not all notices from "Foreclosure" saved searches are actual foreclosures. The scraper parses each notice's full text and only includes ones with trustee sale language. See `INCLUDE_PHRASES` / `EXCLUDE_PHRASES` in `foreclosure_filter.py`.
- **Probate owner_name** should be the Personal Representative/Executor/Administrator — not the deceased.
- **Owner names** in foreclosure notices typically appear after "executed by" in the deed of trust language.
- **Rate limiting:** 2-3 second random delays between requests, 3 retries per page.
- **Address dedup:** Same property can appear in multiple notices; `data_formatter.deduplicate()` keeps the most recent.

## Output

CSV files land in `output/` (gitignored). Logs go to `logs/` with timestamped filenames. Sift columns: `date_added, address, city, state, zip, owner_name, notice_type, county, source_url`.

**Date Semantics (build 1.0.30+):** `date_added` = the date WE added the record (the pipeline run date, stamped in `run_enrichment_pipeline`), so a daily run shows today. The legal notice's publication date lives in its own field/column, `date_published` / "Notice Publish Date" (parsed by `notice_parser` / the scraper results grid). PDF/photo imports set `date_added` explicitly (preserved, not re-stamped); CSV re-import preserves both columns. Downstream that needs the filing date (DOD sanity check, DataSift Probate Open Date, the month tag, dedup tie-break) uses `date_published` (fallback `date_added`).

## Notice Screenshots (proof-of-source)

Each scraped notice gets a full-page screenshot of its detail page on tnpublicnotice.com, captured the moment the reCAPTCHA is solved and the legal notice is visible (`notice_screenshot.py::capture_notice_screenshot`, called from `scraper.py` in the kept-notice branch). The image is the actual published notice, used to add legitimacy to outreach.

- **Scope:** foreclosures only by default (`config.NOTICE_SCREENSHOT_TYPES`, comma-separated env override). Toggle the whole feature with `CAPTURE_NOTICE_SCREENSHOTS` (default on). Capture is best-effort: a screenshot failure never drops the record. PNGs land in `output/notices/` (gitignored), named `notice_{ID}.png` by the numeric notice ID.
- **Carried on `NoticeData`:** `notice_screenshot_path` (local PNG, set at scrape) → `notice_screenshot_url` (hosted link, set at output time).
- **Hosting:** Apify run pushes each PNG to the key-value store and sets a shareable URL (mirrors the deep-prospecting PDF pattern). CLI run uploads to Google Drive when `GOOGLE_DRIVE_FOLDER_ID` + `GOOGLE_SERVICE_ACCOUNT_KEY` are set, else falls back to the local path. Helpers: `host_screenshots_via_drive()`, `set_local_screenshot_urls()`.
- **Delivery to DataSift:** the URL rides along as the `Notice Screenshot` custom field plus a "Notice Screenshot:" line in record Notes (`datasift_formatter`). DataSift's CSV upload cannot push an image into the REISift Gallery panel, so the link is the supported route.

## Scraping Backend: Scrapfly (build 1.0.31+)

The gated notice detail fetch (the "caps structure": residential proxy, anti-bot, reCAPTCHA, and the proof-of-source screenshot) can run through the **Scrapfly API** instead of the in-house Playwright + 2Captcha path. Selected by `SCRAPE_BACKEND` (defaults to `scrapfly` when `SCRAPFLY_KEY` is set, otherwise `playwright`).

- **`scrapfly_client.py`** provides `ScrapflyNoticeClient`. `login(session)` logs into Smart Search inside a Scrapfly session (forms-auth cookie + sticky residential IP), then `fetch_notice(id, session)` opens the detail page with `asp=True` + `render_js=True`, a JS scenario clicks "View Notice" (ASP solves the reCAPTCHA), and it returns rendered HTML + a full-page screenshot in one call. `fetch_notices(ids)` logs in once and yields a result per ID. Best-effort with retries; every call returns a `NoticeFetchResult`.
- **Scraper integration** (`scraper.py`): when `SCRAPE_BACKEND == "scrapfly"`, Playwright still drives login + saved-search navigation and supplies each notice ID, but the per-notice content + screenshot come from Scrapfly via `_scrapfly_notice()`. Any Scrapfly failure falls back to the 2Captcha path, so the swap is safe. Returned HTML is parsed by `notice_parser.parse_notice_html()` (shares field extraction with `parse_notice_page`).
- **Screenshots** come natively from Scrapfly (`screenshots={'notice': 'fullpage'}`), saved to `output/notices/` and hosted/linked exactly like the Playwright path.
- **Tooling:** `scrapfly_spike.py --id <id>` validates one notice (gate clears + screenshot) before relying on it. `backfill_screenshots.py [--csv ...]` logs in once and backfills screenshots for a master list (e.g. the output of `consolidate_foreclosures.py`), writing `notice_screenshot_path` / `notice_screenshot_url` back to the CSV.
- **Env:** `SCRAPFLY_KEY` (required), `SCRAPE_BACKEND`, `SCRAPFLY_COUNTRY` (default `us`), `SCRAPFLY_RENDER_WAIT_MS`, `SCRAPFLY_TIMEOUT_MS`, `SCRAPFLY_MAX_RETRIES`. Needs `scrapfly-sdk` (in requirements.txt).
- **Open validation:** whether Scrapfly's ASP clears this site's in-page reCAPTCHA "View Notice" gate is confirmed per-notice by the spike. A `gate_not_cleared` result means the JS scenario action schema or an explicit CAPTCHA step needs a tweak.

## Foreclosure Master List Consolidation (build 1.0.31+)

`consolidate_foreclosures.py` builds a master list of still-active foreclosures from the last N months of runs. It pulls each Apify run's `output.csv` from the run's key-value store (the default dataset is unused), merges local `output/` CSVs, dedupes by **property** (address + city, keeping the latest sale date so republished/postponed notices collapse to one), and removes any whose `auction_date` ("option date") has already passed. Needs `APIFY_TOKEN`. Output: `output/foreclosure_master_active_<date>.csv`.

```bash
python src/consolidate_foreclosures.py --months 3                  # Apify + local
python src/consolidate_foreclosures.py --months 3 --require-sale-date  # drop no-date junk
python src/consolidate_foreclosures.py --county Knox --no-apify     # local only, one county
```

## Apify Deployment

The project runs as an **Apify Actor** in the cloud. When `APIFY_IS_AT_HOME` or `APIFY_TOKEN` is set, `main.py` uses the Actor SDK instead of CLI args.

```bash
# Install Apify CLI
npm install -g apify-cli

# Local test (reads input.json, simulates Actor environment)
apify run --purge

# Deploy to Apify platform
apify login
apify push

# On Apify Console: set up daily schedule and configure secrets in Actor input
```

### Actor Input (configured in Apify Console or `input.json`)
- `mode`: "daily" or "historical"
- `counties` / `types`: arrays to filter saved searches (empty = all)
- `tn_username`, `tn_password`, `captcha_api_key`: secrets (required)
- `google_drive_folder_id`, `google_service_account_key`: optional Google Drive upload

### Actor Output
- **Dataset**: structured records pushed via `Actor.push_data()`
- **Key-value store**: `output.csv` backup
- **Google Drive** (optional): CSV + summary text file uploaded via service account

### Key Files
- `.actor/actor.json` — Actor manifest (name, version, Dockerfile path)
- `.actor/input_schema.json` — Input fields + validation for Apify Console UI
- `Dockerfile` — Based on `apify/actor-python-playwright:3.12`
- `src/drive_uploader.py` — Google Drive upload via base64-encoded service account key
- `input.json` — Local test input (gitignored, contains credentials)

## Courthouse Photo Pipeline (build 1.0.28+)

Courthouse terminal photos → OCR → LLM parse → enrichment → DataSift. Runner takes phone photos at Knox/Blount county terminals, uploads to Dropbox organized as `{county}/{notice_type}/`, system auto-processes.

### Notice Types (7 total)
- `foreclosure`, `tax_sale`, `tax_delinquent`, `probate` — existing from web scraper
- `eviction` — plaintiff = landlord (target contact), defendant = tenant
- `code_violation` — owner of record, violation type, compliance deadline
- `divorce` — petitioner + respondent, property from schedule page

### Critical OCR Patterns (hard-won from live testing)

**Moire pattern from terminal screens is the #1 OCR killer.** Standard Tesseract preprocessing (adaptive threshold, CLAHE) produces garbage on courthouse terminal photos. The fix:
- **Bilateral filter** (`cv2.bilateralFilter(gray, 15, 75, 75)`) removes moire while preserving text edges
- **Otsu threshold** (`cv2.THRESH_BINARY + cv2.THRESH_OTSU`) after bilateral — auto-determines optimal binary threshold
- **PSM 4** (single column variable text) for terminal screens — NOT PSM 6 (single uniform block) which was the research recommendation but fails in practice
- **Do NOT use `fix_rotation()` (Tesseract OSD) on phone photos** — EXIF transpose handles rotation. OSD on raw phone images often fails and the 270° fallback rotates correct images sideways

### Probate Deep Prospecting (from courthouse terminals)

Courthouse probate records have decedent name + PR/executor name but NO property address. Multi-tier lookup fills the gap:

**Property Address Lookup** (Step 3c in enrichment pipeline):
1. **Tier 1: Knox Tax API name search** — search `/parcels/{decedent_name}`, score by token overlap (FIRST MIDDLE LAST → LAST FIRST MIDDLE), accept >= 0.4 match. Tries multiple name variations (with/without suffix, LAST FIRST format, first+last only).
2. **Tier 2: Executor family search** — search Knox Tax API by executor name, look for properties where decedent's last name appears in owner field (family property transferred to executor).
3. **Tier 3: People search** — search TruePeopleSearch/FastPeopleSearch for decedent's last known Knox County address.

**Probate Preset** (obituary enricher):
- Triggers when court record has PR name + decedent name (no address required) — prevents wrong obituary from overriding court-named executor
- Sets DM = the named PR/executor directly, skips obituary search entirely
- Then runs DM address lookup (Knox Tax API → People Search → Tracerfy)

**DOD Sanity Check** (obituary enricher):
- Rejects obituary matches where DOD is > 3 years before the notice **publication** date (`MAX_DOD_GAP_YEARS = 3`)
- Prevents matching a 2014 obituary to a 2025 court filing (wrong person with same name)
- Applied to both full-page and snippet matches
- Anchors on `date_published` (the legal publication date), falling back to `date_added` — NOT `date_added` alone, which is now the run date (see "Date Semantics" under Output)

### Deceased-Owner Heir Resolution — Enformion (opt-in, build 1.0.30+)

The default obituary path extracts survivors/heirs from obituary text with an LLM, which can hallucinate an entire heir map (see `project_obituary_heir_hallucination` memory). The **Primary Path** of the `deep-prospecting` skill replaces this with the Enformion/Endato relatives graph — grounded, nothing inferred.

- **Module:** `src/enformion_heir.py` — reusable client: `person_search()`, `relatives_to_survivors()`, `required_signers()` (cost gate: living closest-kin `relativeLevel == "ab"` + decedent surname + DOB), `dedupe_phones()`, and `resolve_heirs_enformion(notice, parsed)` which returns `(ranked_dms, error_info)` shaped exactly like `build_heir_map()` so the rest of the pipeline is unchanged. Heir signing authority reuses `obituary_enricher.rank_decision_makers` (TN intestacy).
- **Pipeline (Step A only, 1 call/record):** `python src/main.py daily --deep-heirs`. In `obituary_enricher` Phase B, a new **Path E** runs Enformion FIRST for confirmed-deceased owners that no cheaper high-confidence path resolved (surviving co-owner on title, court-named executor). Falls through to the obituary-survivor waterfall on a miss or when creds are absent. Default (no flag, and the Apify daily Actor) keeps the old behavior — Enformion is never auto-billed.
- **Full waterfall (one record):** `python src/run_deep_prospect.py --first X --last Y --street "..." --city Knoxville --state TN --zip 37917` runs Steps A-E (decedent → required signers → per-signer search → phone dedupe → Trestle scoring) and prints a master dial sheet. Consolidates the one-off `run_brice_*` scripts.
- **Creds:** `ENFORMION_AP_NAME` / `ENFORMION_AP_PASSWORD` in `.env` + `config.py`. Billed per match (~$0.35); misses are free. Detect API failure by HTTP status, NOT the always-present `error` object.
- **DOD conflict:** Enformion's death-index DOD can disagree with the obituary DOD (often a second household death). Surfaced via a `dod_conflict` flag in `missing_data_flags`; never silently resolved.

### Dropbox Folder Structure
```
{DROPBOX_ROOT_FOLDER}/
├── Knox/
│   ├── eviction/
│   ├── code_violation/
│   ├── divorce/
│   ├── foreclosure/
│   ├── tax_sale/
│   └── probate/
└── Blount/
    └── (same subfolders)
```

### Environment Variables
- `DROPBOX_APP_KEY` — Dropbox OAuth2 app key
- `DROPBOX_APP_SECRET` — Dropbox OAuth2 app secret
- `DROPBOX_REFRESH_TOKEN` — Dropbox offline refresh token (auto-rotates access tokens)
- `DROPBOX_POLL_INTERVAL` — seconds between polls (default 900 = 15 min)
- `DROPBOX_ROOT_FOLDER` — root folder path in Dropbox (e.g., "TN Public Notice")

### Dependencies (added to requirements.txt)
- `opencv-python-headless>=4.13.0` — image preprocessing (headless = no GUI, saves 26MB in Docker)
- `numpy>=1.26.0` — required by OpenCV
- `dropbox>=12.0.2` — Dropbox SDK (minimum for post-Jan-2026 API compatibility)

## Tulsa Probate Pipeline (OSCN) — BUILT, FIRST LIVE RUN 2026-09-11

**Status: first live run 2026-09-11 — 2 records (PB-2026-0760 Ross,
PB-2026-0761 Johnson), $0.46 all in, every number verified tagged.** It exposed
four pipeline bugs, all fixed the same day — see "Lessons from the first live
run" below.

### What exists

| Piece | Where | State |
|---|---|---|
| `probate-info-extraction` skill | `~/.claude/skills/probate-info-extraction/` | 41 columns, one row per PROPERTY |
| Buy box | `src/buy_box.py` | single-family at minimum; gates BEFORE creation |
| Human check step | `src/batch_review.py` | BLOCK / WARN / EXCLUDE |
| Assessor over plain HTTP | `tulsa_assessor.search_assessor()`, `get_parcel_situs()`, `get_parcel_improvements()` | no Playwright |
| Assessor **owner mailing address** | Info page, regex `Owner Mailing Address</th>\s*<td...>` | spot-check tool, not a pipeline step |
| Probate Notes / Message Board | `_PROBATE_SECTIONS`, `_NOTES_SECTION_SETS`, `_signing_chain_block()` | signing chain + mailing address |
| Trace at the person's address | `resolve_subjects()` -> `trace_*` | probate never falls back to the property |
| Repeat-PR dedupe | `tracerfy_source()` | billed once, credited to every record |
| **The chain** | `_create_records_for_batch()` | enrich -> buy box -> review, all before creation; STOP-AND-ASK gates per ROW, not the whole batch |
| Probate columns into the trace | `main._trace_row()` | SIGNING CHAIN + relationship survive both paths |
| Relationship phone tag | `relationship_tag()`, `_primary_relationship_tag()` | heir's numbers: Daughter/Son/Wife/Husband/Grandchild, else Relative |
| PR + title holder on the board | `Title Holder of Record` column, `_signing_chain_block()` | PR as the filing names them; assessor owner of record, flagged when it is not the decedent |
| Trust-name search + LOCCAT | `main._trust_name_search()`, `src/tulsa_loccat.py` | trust searched BEFORE the decedent's own name |
| Treasurer true-negative fallback | `main._treasurer_true_negative_check()`, `src/tulsa_treasurer.py` | runs only when the Assessor found nothing; never trusts a name-only hit |

**The skill's column list and `_PROBATE_SECTIONS` must stay in sync.** A field
in one and not the other is extracted and then silently dropped — same trap as
the petition version.

### Two addresses, never confused

- **Property address** on the CRM record = the **decedent's house**. That is the
  thing being bought, and it is the whole point of the pipeline.
- **Mailing address** = the **PR's or heir's own address**. Used for skip
  tracing and mail only.

For probate the mailing address must **never** fall back to the property — the
PR rarely lives in the decedent's house (on one real case a grandchild did).
Foreclosure keeps the property fallback, since the owner usually lives there.
Both appear in the Message Board, labelled, with a "do NOT mail there" line when
they differ.

### Buy box (2026-09-04)

**Single-family homes at minimum**: must have a structure, must be residential.
Runs on the extracted sheet BEFORE creation, because
**`skip-trace --create` writes to the CRM whether or not `--commit` is passed** —
the dry-run gate protects spend, not the CRM. Fails OPEN on missing data.
Rejections are reported, never silently dropped.

**Not enforced, and do not imply otherwise: single-family vs duplex.**
`AcctType` reads "Residential" for both and the assessor's structure detail is
client-side from an unexposed endpoint. A duplex passes.

### Empty lots

Bare land never reaches the CRM but is **reported every run** under its own
banner. Detection is the assessor's verbatim `"This property has no
improvements"` — verified 8/8. A parcel can have a street address and still be
vacant, so check improvements directly rather than inferring from the address.
Parcels with no county-assigned address ride on the estate's addressed record
via `additional_parcels`; a parcel number is not an address.

### Cost — do not quote the dry run's total

Tracerfy (~$0.02/record) and DataSift (~$0.12/owner) are per-RECORD and
predictable. **Trestle is per unique NUMBER** and numbers-per-record swings
(4.8 then 6.4 on consecutive real batches). The dry run's Trestle line reads
~$0.00 because no trace has run. Budget **$0.21-0.26/record**, quote Trestle as
a range, and speak up when the `BILLED: TrestleIQ scoring N unique number(s)`
line implies more than was approved.

Everything below was established live against real Tulsa cases, with controls —
not inferred from docs.

### The data source: OSCN carries the whole thing

The old "OSCN probate is too thin" note was true only of the **results page**.
Docket **detail** pages link full imaged PDFs, free and public.

- **8 of 8** sampled Tulsa PB cases have imaged documents (6-14 docs each).
- PDFs are **scanned, no text layer** — same as the foreclosure petitions, so the
  existing `image_utils.ocr_page` 300dpi / psm 4 path applies unchanged.
- `Results.aspx` (discovery) has **no Turnstile**. Detail + document pages **do**,
  firing after ~12 requests. A challenge is **HTTP 201, ~2,357 bytes** — NOT an
  error and NOT an empty docket. Misreading those as empty once produced a
  confident "only 1 of 8 cases has documents," which was wrong by 8x.
- 2Captcha solves it over **plain HTTP** (no Playwright): `method=turnstile`,
  post back `cf-turnstile-response` **plus the page's hidden `source_uri`**.
  **~$0.00145/solve, and one solve covers a whole batch** — a later 4-PDF run
  used 0 solves on the warm session. The charge posts LATE, so compare balances
  across runs, not within one.

### What each document gives, and when

| Document | Lag | Present | Carries |
|---|---|---|---|
| **Petition** | **day 0** | 8/8 | heirs + relationships + **mailing addresses**, marital status, children, and the sworn *"owned an interest in real property located in Tulsa County"* |
| Order Appointing PR | +20-27d | 5/8 | the **court-adjudicated** heir list (stronger than the petition's claim); often where the **PR's own address** appears |
| Letters issued | +20-41d | 6/8 | PR authority confirmed |
| General Inventory | +41d | 3/8 | legal descriptions — **waivable by court order, never gate on it** |

Nothing you need is late: the Petition is day 0 and carries the first-to-market
payload. Treat the Inventory as optional enrichment.

### Finding the property (the part that used to be the blocker)

**Resolved.** `assessor.tulsacounty.org/Property/Search?terms={terms}&filterTag=null`
returns results as an embedded **DevExpress dxDataGrid JSON `dataSource`**, not an
HTML table — a `<table>` parse finds zero rows on a page holding thousands of
records. Parse it over plain `requests`; `tulsa_assessor.py` drives this with
Playwright and does not need to.

- Several `"data":[` arrays exist; take the one whose next few KB contain `AccountNo`.
- It is **JavaScript, not JSON** — `new Date(1, 0, 1)` literals must be replaced
  before `json.loads`.
- Subdivision names **are** searchable, so the Inventory's legal description works
  both as a query and as free corroboration.
- **Always run a gibberish control** (`ZZZQQQ NOTAREAL` -> 0) before trusting a
  negative. Unlike DataSift's ignored `search=` param, this endpoint really filters.

**THE TRAP — search the ROOT owner, not the decedent.** On a chain-of-deaths
probate the decedent held only an *undivided interest* in an undistributed parent
estate, so title never moved to them and the assessor returns a **true zero**.
`FULTON, ALFRED` -> 0 records; his father `FULTON, JOHNNIE SR` -> the property.
**A zero on the decedent is not evidence of no real property.** Search the
surviving spouse, the PR, and any earlier-deceased relative in the heir list.
This is the `probate-property-finder` skill's Tier 2, and on these cases it is the
primary path, not a fallback. Trust-held property appears as
`LAST, FIRST C/O LAST, FIRST REV LIVING TRUST`.

Tells that an upstream estate exists: `"an UNDIVIDED interest in"` in the
Inventory, heirs marked `Now Deceased/Child` in the Order, and **multiple probates
filed the same day by the same PR with the same surname** — that is one family
clearing a title chain, i.e. **one property investigation, not N leads**.

**Confidence:** the skill's token-overlap formula scores a trust-held match ~0.33
(LOW) because the trust name inflates the owner string. **Weight cross-source
agreement above token overlap** — the Inventory's `Lot 36, Block 3, SUBURBAN ACRES`
matching the assessor's `SubdivisionName` `SUBURBAN ACRES AMD` is the real signal.

### Who becomes the Owner / trace subject

**PR first, but only if we have their mailing address; otherwise the first listed
heir** (user's rule, 2026-09-02). Tracing a name with no address is useless, and
the filing usually hands us an heir whose address IS listed. The PR's address is
often absent from the petition (only the law firm's appears) but shows up in the
Order's heir table when the PR is also an heir.

Still true: **the decedent is NEVER the Owner**, and Enrich Owners / Swap Owners
stay **OFF** or DataSift will "correct" the PR back to the deceased owner of
record. Trace **one** subject per record; heirs are **captured free** from the
filing, never traced up front. A repeat PR across cases means **dedupe the trace
subject** or you bill the same person N times.

### Reference case

**Fulton — PB-2026-587/588/589** is the worked end-to-end example and the
regression case for any probate work. Johnnie Fulton Sr. (d. 2019) is the root
owner; two of his children died after him holding shares; his daughter **Jennifer
G. Faulk (2240 W. Newton Apt #A, Tulsa OK 74127)** is PR on all three. Property:
**4503 N Iroquois Ave, Tulsa OK 74106** (`R40800021305520`), plus an UNPLATTED
metes-and-bounds parcel (`R90328032815610`) with no street address. A
grandchild-heir lives in the estate property. Artifacts in
`output/probate_discovery/`.

### Lessons from the first live run (2026-09-11)

Fixed the same day. Offline tests: `tests/test_probate_pipeline_fixes.py` (25,
all mocked — nothing billed), plus a read-only check against the live CRM.

1. **DataSift's skip trace took ~11 minutes; the pipeline waited 5.** It then
   scored, tagged and posted without DataSift's numbers, and told Rhonda
   Thomas's Message Board "no numbers returned" minutes before DataSift filled
   it with two. The job IS observable: `GET /api/internal/activity/?type=skip_trace`
   (`status` processing -> complete; `meta.final_cost` is the real charge; the
   endpoint ignores `ordering`, so `list_skip_trace_jobs()` reads every page and
   sorts). `datasift_source()` now waits on the job for up to 30 minutes, and a
   job still running at the deadline marks its records `datasift_pending`, so
   the board says "still processing" instead of "no numbers returned".
2. **`set_phone_tags()` silently applied nothing to 7 of 7 numbers** (4 of 77 on
   2026-09-04). `writeback()` now goes through `apply_phone_tags_verified()`:
   send, read back, re-send ONCE to numbers whose tag list is genuinely empty,
   never to a partly-tagged one.
3. **Probate phone numbers carry the heir's relationship** (user rule). The
   traced heir/PR's numbers get the relationship the filing states, mapped onto
   the EXISTING phone tags — Daughter, Son, Wife, Husband, Grandchild — and
   anything else becomes `Relative`. Do not create Niece/Nephew tags; the user
   will. Only numbers a trace found (never Pre-existing), and only when the
   owner is the filing's Decision Maker. Foreclosure owners still get source +
   tier only.
4. **The 300s `wait_for_properties` stall was a dropped trailing directional.**
   Sent `4529 E Xyler St N`, stored `4529 E Xyler St`: the newest-50 scan never
   matched the brand-new record, and the fallback then logged it
   "pre-existing" — a guess, on the single newest record in the CRM. The scan
   now matches through a dropped directional when it is unique on both sides.
   Re-checked live: 1.3s.

Also fixed: **both trace paths passed only street/city/first/last into
`run_pipeline()`, so the Message Board's SIGNING CHAIN block had never been
posted on a live run.** `main._trace_row()` now carries the probate columns on
both paths.

**Running the billed step.** The `--create` dry run already creates the records
and posts their notes and Message Board. Re-running the same command with
`--commit` would post both again on every record — `upload_to_datasift()`
writes them for every record it resolves, existing or not (read from the code,
not tested live). So after a `--create` dry run, run the billed half
trace-only: `skip-trace --csv-path output/datasift_ready_probate_<date>.csv
--commit`. That path reads CSV only, never the `.xlsx`.

**Common names defeat the decedent search.** "Tina Johnson" returned a 26-way
tie at 0.67. The Johnson house was found by searching the creditor LLC named in
the petition and matching it to the heir's listed address. A lease-to-own
leaves title with the seller, and the buy box does not check who holds title.

### STOP AND ASK — calibrated 2026-09-14 (video walkthrough of real cases)

The 2026-09-11 rule ("stop on anything not straightforward") was correctly
motivated but wrongly coded: `batch_review.py` originally BLOCKed unless the
**decedent's own name** was the assessor's title holder — which would have
false-positive-blocked Bitson (below), a genuinely clean case. The user
recorded a Loom walkthrough of three real Tulsa cases
(`Downloads/Finding Probate Property Addresses in Tulsa.srt`) that recalibrated
the rule against real ownership patterns, not guessed ones:

- **Chu — messy.** Probate says nothing about real property. Living spouse
  Christopher lives at 7508 S Granite, but the assessor shows **Lydia Chiu**
  (the adult daughter) as owner — and the sales history shows Christopher
  quit-claimed it to her for $0 in 2022. *(Note: the family's real surname is
  **Chiu**, not "Chu" — Loom's auto-caption misheard it; verified live against
  the actual parcel, account `R27805831030640`.)* Flagged for review: an
  ownership transfer connected to the estate, not merely "hard to find."
- **Malick — clean, despite heavy digging.** No living spouse, no address in
  the probate. Decedent-name search with "Sr." fails; dropping the suffix
  finds it; cross-checked against the Tulsa County Clerk's LOCAT land-records
  tool as a second, independent source. Lands on a single, unambiguous owner
  (the decedent) with nothing surprising in the history — clean, just several
  search attempts.
- **Bitson — clean, despite title never being in the decedent's name.**
  Probate states *personal property only*. Living spouse D'Angelo is an
  heir/PR; checking HIS address (independent of what the probate claims about
  real/personal property) finds he is the assessor's owner, inherited ~2 years
  prior. No surprising history — clean.

**The corrected rule, built and tested (`tests/test_probate_review_stop.py`,
`tests/test_probate_message_board.py`):**

1. **Always check a living spouse's own address first** (`main._living_spouse_address()`),
   regardless of what "Real Property Stated" says — Bitson's probate said
   personal property only; the check is what surfaced the house anyway.
2. **Clean title holder = decedent, decedent's own trust, OR any named
   PR/heir** — not just the decedent's own name. This is what makes Bitson
   pass.
3. **New capability: sales-history lookup.**
   **Owner mailing address (2026-09-24).** The county's mailing address — where
   the tax bill physically goes, i.e. the authoritative absentee signal — is on
   the **Info** page (`ASSESSOR_INFO_URL_HTTP`, already used for improvements
   and sales history), matched by
   `r'Owner Mailing Address\s*</th>\s*<td[^>]*>(.*?)</td>'` and split on `<br>`.
   It is NOT on the search endpoint, whose `AccountMapDetails.OwnerAddress1`
   etc. come back null and whose `Owners` array is empty — that dead end is
   what made it look unavailable. **Match by PROPERTY ADDRESS, not owner name**:
   that took coverage from 11/28 to 27/28 on a real batch. Fall back to
   `"<Last>, <First>"` then `"<Last>"`, since the two strategies match
   different subsets. **Comparison must drop directionals and ordinal
   suffixes** or format variants read as false "different" (county
   `9921 E 114 PL` vs our `9921 East 114th Place South` is the same parcel).
   The endpoint 503s under load — retry with backoff, ~3s between records.
   **Kept as a SPOT-CHECK tool, not a pipeline step** (too slow, rate-limited);
   DataSift's owner enrichment supplies the mailing address in the pipeline.
   Result on the 2026-09-22 batch: 26 of 27 genuinely owner-occupied, one real
   absentee (Tipton -> `PO BOX 195`).

   `tulsa_assessor.get_parcel_sales_history()` parses the Assessor's real
   server-rendered "Sales/Documents" `<table>` (Grantor/Grantee/Sale
   Price/Deed Type/Document Number) — did not exist before this date. Scope to
   the "desktop" table only; the page repeats the same rows in a "mobile" div
   with different markup, which double-counts if both are parsed.
4. **Insider-transfer detection overrides an otherwise-clean title match.**
   `main._check_insider_transfer()` flags when the sales history's Grantor on
   the most recent sale matches the decedent/PR/an heir — **even when the
   current title holder is itself a named party** (this is the piece that
   closes the Chu/Chiu gap: Lydia could easily be a named heir on some other
   estate and still not make the transfer itself ordinary). Rendered on the
   SIGNING CHAIN Message Board block as `CAUTION - transfer connected to this
   estate: ...`. Free, read-only; only ever sets a field, `batch_review.py`
   decides what to do with it.
5. Downgrades to a `WARN` (not a `BLOCK`) once a human sets the sheet's
   `Property Confirmed` column to `Yes` — never inferred, set only by the user
   after reviewing the evidence shown.

### Trust-name search + LOCCAT (2026-09-15, second video walkthrough)

A follow-up video ("Tulsa Probate Home Ownership True Negatives") walked three cases where the
decedent turns out to have owned no real property at all (Scott, Coleman, Pruitt) — worked through
to distinguish a genuine "true negative" from a search that just gave up too early. Two real
capabilities came out of it:

1. **Search a named trust FIRST, before the decedent's own name.** A will stating "everything I
   own is in the X Revocable Trust" means the decedent's own name will never match a trust-titled
   parcel — searching it anyway (as the old order did) just wastes a step before falling through.
   New `Trust Name` extraction field; `main._trust_name_search()` tries the Assessor (full name,
   then the DTD/DATED-stripped variant), and only if that misses, falls back to `tulsa_loccat`
   (below). Runs before even the living-spouse-address check, since trust presence is the
   strongest signal when the will states one.
2. **New module: `src/tulsa_loccat.py`**, wrapping the Tulsa County Clerk's LOCCAT tool
   (`ais-usc-tulsacounty-web.azurewebsites.net`) — plain HTTP, no browser, no auth. Found by
   reading its page's own unminified JS (`/Scripts/map/map.js`) rather than guessing at its Mapbox
   map UI, the same discipline that cracked the Assessor's sales-history table. Two endpoints:
   - `search_parcel()` -> `POST /api/Parcels/Search` (owner/parcel/address/section-township-range)
     — TIGHT matching, verified: `KAISER,LARRY` returns exactly the one real hit. A second,
     complementary source to the Assessor's own search.
   - `search_advanced()` -> `POST /api/Parcels/SearchAdvanced` (document search by
     grantor/grantee, or subdivision/lot/block) — **the only source in this codebase that can find
     a trust by name without already knowing a parcel.** Its matching is **LOOSE, confirmed live**:
     `L & S GROUP LLC` returned **1,315** raw results (OR-matching on filler words like
     "GROUP"/"LLC"). This is the exact false-hit gotcha the video called out by hand ("Patricia
     Pruitt" surfacing "Patricia... Party" as a "hit"). Fixed and tested: `verified_hits()` filters
     to records whose GRANTOR/GRANTEE is a genuine normalized-substring match — token-overlap
     ("≥ 2 shared words") was tried first and is WRONG for entity names, since it let 1,153 of
     those 1,315 false hits through. **Never call `search_advanced()` and trust the raw result —
     always go through `verified_hits()`.**

   Live validation, not just synthetic tests: the corrected matching finds the exact real Johnson
   transaction (document `2014002906`, Larry Kaiser → L&S Group) that the sales-history lookup
   also found independently, and confirms L&S Group LLC holds **12** Tulsa County parcels —
   matching a fact already on record in this file from the original Johnson incident.

### Tulsa County Treasurer — true-negative fallback + per-row STOP-AND-ASK (2026-09-15, third video)

A third video ("Using Tulsa County Treasurer for Probate Properties") pointed at adapting the
downloaded `probate-property-finder` skill's Tier 1 (name search) / Tier 2 (executor family
search) / Tier 3 (people search) for Tulsa specifically — its Knox County TN tiers don't apply
directly, but Tier 1 led to checking whether Tulsa County has an equivalent, and it does: the
County **Treasurer**, not just the Assessor.

**New module: `src/tulsa_treasurer.py`**, wrapping `oktaxrolls.com` (Tulsa County Treasurer) —
plain HTTP, no login, no Playwright. Found the real name-search route by reading the site's own
`custom_data_table.js` rather than guessing: `POST /searchResult/Tulsa/owner_name` with
`first_name`/`last_name`/`business_owner_name` query params genuinely filters (gibberish control →
0 rows) — the DataTables global `search[value]` parameter on this site's OTHER endpoint
(`/searchResult/Tulsa/amount`, already used by `tulsa_tax_delinquent.py`) is silently ignored, the
same trap as DataSift's `search=` param elsewhere in this codebase; caught by testing a real name
against gibberish before trusting either.

- `search_owner_name()` — searches all years by default (the site's own "All Years" default).
  Live-tested against the Fulton reference case (Johnnie Fulton Sr., PB-2026-587/588/589):
  found **FIVE** real-estate parcels, not the two already on record from the Assessor-only
  investigation. Two were previously known (`40800-02-13-05520` = the reference property at 4503 N
  Iroquois Ave; `90328-03-28-15610` = the known unplatted parcel). **Three more
  (`02575-02-24-00480`, `06100-02-26-00100`, `11225-02-24-03090`) were never found by the
  Assessor-only search at all** — concrete proof this is a genuinely independent second source, not
  a slower path to the same answer.
- `get_owner_history()` — the site's own "History" button (real link is `owner_history/Tulsa`, not
  `history/Tulsa` — caught a self-inflicted false 404 from chopping the `owner_` prefix off via a
  sloppy regex before re-verifying against the raw HTML). Parses a real server-rendered
  `<table class="table-tax-data">`: who paid the tax, per year, for a parcel's lineage. This is a
  second, independent chain-of-title signal from the Assessor's deed-based sales history — tax
  *payer* history, not deed *grantor/grantee* history.
- **COMMON-NAME COLLISION IS REAL, confirmed live (user-flagged 2026-09-15):** searching a name
  recalled from an earlier true-negative case ("COLEMAN, ELIZABETH") returned a real, direct hit on
  an actual parcel, with nothing to say whether it was the same Elizabeth Coleman or a different
  person sharing a common name — the same trap already on record for the Assessor ("Tina Johnson",
  26-way tie at 0.67). **A name-only hit here is never trusted.** `address_corroborates()` requires
  an exact house-number match plus a shared street-name word against an address already known from
  the filing before a hit counts as anything.

**Role: fallback corroboration, not a replacement for the Assessor** — the video's own framing:
*"Do we need it if the assessor has already verified? No, of course not. But if the assessor
hasn't, then it could be very helpful."* `main._treasurer_true_negative_check()` runs ONLY when the
Assessor found nothing for anyone named in the filing (decedent, PR, every heir) — it searches the
same candidates on the Treasurer, requires `address_corroborates()` before accepting any hit, and
for a hit under an heir's/PR's own name additionally requires the decedent to appear in that
parcel's own tax-payer history (`history_contains_name()`) — otherwise it's presumed to be the
heir's own unrelated property, not an inheritance (the Coleman/Lewis case the video walked through:
Lewis Coleman had a real hit, but it was his own pre-existing property; the decedent never once
appears in that parcel's payer history).

**A corroborated finding writes the SAME fields an Assessor hit would** (Property Street/City,
Parcel ID, Title Holder of Record) — not just an informational note. This was a real bug caught by
writing the test before trusting the feature: the first version only set a side-field
(`Treasurer Check`), which meant the discovery was invisible to the rest of the pipeline —
`check_buy_box()`'s "no property address could be resolved" rule excludes any row with no Property
Street *before* `batch_review.py`'s STOP-AND-ASK section ever runs on it, so the note would sit on
a row that had already been thrown out. Fixed to populate the real fields, so a Treasurer discovery
flows into the *same*, already-tested title-holder check as an Assessor hit, rather than needing
(or getting) a separate code path.

**STOP-AND-ASK IS PER-ROW, NOT PER-BATCH (user, 2026-09-15):** *"I don't want the stop and ask
properties to slow down the work on the easy ones... the tougher ones can be saved for my manual
review last while the easy ones are running."* Before this, ANY row with a BLOCK finding halted
`_create_records_for_batch()` for the ENTIRE batch — one messy case held up every clean one behind
it. `_create_records_for_batch()` now partitions rows by whether they actually earned a BLOCK: the
clean rows proceed to creation in the same run, and only the flagged rows are held back (reported
in the review sheet) for a later manual pass. Mirrors how `apply_buy_box()` already splits
kept/rejected rows rather than failing the whole batch on one exclusion.

### Known gaps

1. **`--create` is not safe to re-run** — it re-posts notes and the Message
   Board on records that already exist (see "Running the billed step" above).
   Making it skip records already created would let both pipelines work as
   "same command, just add `--commit`".
2. Multi-parcel estates: parcels with no situs address ride on the addressed
   record as `additional_parcels`; `output/probate_template_SAMPLE.csv` still
   wrongly assumes one row per case.
3. Single-family vs duplex is not detectable from free data (see above).
4. `Signing Chain Count` / `Heirs Living` are still not computed — the Message
   Board's SIGNING CHAIN block carries the heir count and who can sign instead.
5. ~~No buy-box check for who holds title (lease-to-own, contract for
   deed).~~ **Addressed 2026-09-14** — not in the buy box, but in
   `batch_review.py`'s stop-and-ask rule (see above), which blocks on both an
   unconfirmed title holder and a transfer connected to the estate.
6. The native property fields `personal_representative` / `probate_open_date`
   stay empty — the pipeline sends them as custom fields this account does not
   have ("unknown custom field").
7. The insider-transfer name match (`main._names_overlap()`, >= 2 shared name
   tokens) and the "living spouse" signal (`PR Relationship`/`Marital Status`
   containing spouse/wife/husband/widow) are both first-pass heuristics from
   two real cases — not yet stress-tested against a wider batch.

### `--create` WRITES TO THE CRM WITHOUT `--commit`

`_create_records_for_batch()` runs in `_run_skip_trace()` **before** `dry` is
computed, and calls `upload_to_datasift()` unconditionally. **The dry-run
gate protects spend, not the CRM.** Any trial of `skip-trace --create` puts real
records in DataSift — which is exactly why the buy box and `batch_review.py` run
on the extracted sheet, before creation, rather than as a post-upload cleanup.

## DataSift.ai (REISift) Integration

DataSift.ai (formerly REISift) is the CRM where scraped records land for niche sequential marketing campaigns.

**As of build 1.0.34 (2026-08-19), a real REST API is live** (early access — see "REST API" section below) and handles upload, tags, lists, notes, custom fields, skip trace, and phone tags. **As of 2026-08-21 the upload path is API-only — no browser in record creation.** **As of 2026-08-26 enrichment is API-scoped too** — the record pipeline (create → enrich → trace → read → score → tag) is now **entirely API, no browser**. Playwright remains only for surfaces with no usable API: the **Sequence builder** (no API in the 323-path spec), **SiftMap sold-tagging** (request body undocumented), and **preset-exclusion** workflows.

**Domain:** `app.reisift.io` (NOT `app.datasift.ai`). Core API at `apiv2.reisift.io`, SiftMap API at `map.reisift.io`.

### The daily foreclosure run

"Do the daily foreclosure run" / "process the new petitions" is a standing,
self-contained request — don't ask which folder or which command:

1. New scanned petition PDFs land in `Desktop\Foreclosure pdfs`. **No text
   layer** — OCR at 300dpi (`image_utils.ocr_page`, psm 4) for the body, and a
   higher-res page-1 crop (400-600dpi, psm 6/11) for the stamped case number
   and file date. The body is the first ~5 pages; the property address, monthly
   payment and mortgage doc number often appear only in the Note/Mortgage
   exhibits after it.
2. `petition-info-extraction` skill → `output/petition_batch.xlsx`
   (28 columns, fresh per batch, never appended).
3. `python src/main.py skip-trace --csv-path "output/petition_batch.xlsx"
   --create --notice-type foreclosure --county Tulsa` — **a dry run**. Report
   the estimate, ask before `--commit`. **Do not re-run that command with
   `--commit` added**: the dry run already created the records and posted their
   notes, and a second `--create` posts them again. Run the billed half
   trace-only from the `datasift_ready_*.csv` it wrote (and mind the Owner
   Alive gap below).
4. Verify by reading records back: tags by TITLE, tiers against Trestle's own
   `assigned_tag`.

**The `Owner Alive` column is a real spend gate — confirmed live 2026-09-04**
(it had been built 2026-08-18 and never exercised until then). A petition whose
owner is deceased and which names **no living person at all** — only "Unknown
Heirs", "Unknown Spouse", "Unknown Occupants" — gets `Owner Alive = No`. The
record is still created, enriched, tagged `deceased` and given full petition
notes; it is simply never skip traced, because there is no one to trace. On
2026-09-04 `main.py` logged `1 record(s) flagged Owner Alive=No - created and
enriched but never traced: David Haggard` and the billable count dropped from
13 to 12. **The bar is narrow**: a named Co-Personal Representative, a
transfer-on-death beneficiary, a surviving joint tenant or a named heir all
count as living contacts (use the first-named and mark `Yes`). Only "Unknown
<anything>" means No.

**Note the gap:** `skip-trace` WITHOUT `--create` reads the CSV for
street/city/first/last only and does **not** honour `Owner Alive`. When
running the trace as a separate second step, filter the deceased-no-contact
rows out of that CSV yourself, or they get traced anyway.

Each record ends up in the CRM, API-enriched, with grouped petition detail in
Notes *and* Message Board, double skip traced, every number scored with a dial
tier and an honest source tag. ~$0.21/record.

### THE RULE (settled 2026-08-26 — do not re-litigate)

**One pipeline. API only. `python src/main.py skip-trace`** (add `--create`
when the records don't exist yet). Dry run by default; `--commit` is the spend
gate.

- **Never use `skip-and-score-upload`.** It no longer exists — mode, handler,
  dispatch and args were all removed. It was the user's own earlier code,
  written before they received Tyler Austin's, and it applied **no phone
  source tags**. Do not resurrect it or hand-assemble its steps.
- **Never reach for Playwright for upload, create, enrich, skip trace, phone
  read, scoring, or tagging.** All of it is API. `enrich_records()` is API as
  of 2026-08-26 (`enrich_records_playwright()` is a rollback, not a fallback
  to prefer). If a browser opens during a batch run, something is wrong —
  investigate rather than accept it.
- **The only remaining Playwright surfaces** are the **Sequence builder** (no
  API exists anywhere in the 323-path spec) and **SiftMap sold-tagging**
  (request body undocumented). Neither is part of the record pipeline. Saying
  "API only" refers to the pipeline; these two are genuinely unavailable over
  the API, not a shortcut being taken.
- **"Inherited" means Tyler Austin AND Ty**, and both are in active use:
  the trace/score/tag pipeline (`skip_trace_agent`) is adapted from Tyler
  Austin's FCRE skip-trace-agent; `find_property_by_address()` and
  dry-run-by-default come from his `crm_api.py`; the enrich toggle policy is
  Ty's `run_enrich_lists.py`; `lists`-as-a-bare-string is Ty's uploader.
- **Never delete anything sourced from Ty** — not when unimported, not when
  its endpoint 403s here, not when a cleanup pass flags it as dead weight.
  `src/datasift_api_upload.py` was deleted once on 2026-08-26 and had to be
  restored. Reference copies stay byte-identical; adaptations live elsewhere.


### Key Files
- `src/datasift_formatter.py` — Transforms `NoticeData` → DataSift CSV (76 columns) and, via `build_api_payload()`, the REST API's create-property payload shape
- `src/datasift_api.py` — REST API client (Open API key auth): properties, owners/phones, tags, lists, notes, custom fields, skip trace, phone tags, filter presets, SiftMap
- `src/datasift_uploader.py` — Both the API-based functions (default) and their original Playwright implementations (kept as `*_playwright()` rollback functions, not dead code) — upload, **enrich**, skip trace, phone read, phone tags, preset discovery, plus the still-Playwright-only sequence/preset-exclusion/SiftMap-sold workflows
- `test_datasift_api.py` — Phase A proof harness: create/tag/notes/custom-field/skip-trace/phone-tag round trip against throwaway test records
- `test_datasift_cutover.py` — Phase B proof: the shared `upload_to_datasift()`/`skip_trace_records()`/`read_record_phone_numbers()`/`upload_phone_tags()` functions end to end
- `test_datasift_upload.py` — Headed browser test (legacy Playwright upload + enrich + skip trace)
- `test_manage_presets.py` — Headed browser test (preset discovery + sold exclusion + sequence creation)
- `test_manage_sold.py` — Headed browser test (SiftMap sold property tagging)

### CSV Column Structure (76 columns — CLAUDE.md said 42 through 2026-08-19; verify against `DATASIFT_COLUMNS` in `datasift_formatter.py` if this drifts again)
- **Core auto-mapped (12):** Property Street/City/State/ZIP, Owner First/Last Name, Owner Type, Company Name, Mailing Street/City/State/ZIP
- **Phone/Email (14):** Phone 1-9, Email 1-5 (Tracerfy skip trace output)
- **Tags/Lists/Notes (3)**
- **Built-in fields (18):** Estimated Value, MSL Status, Last Sale Date/Price, Equity Percentage, Tax Deliquent Value, Tax Delinquent Year, Tax Auction Date, Foreclosure Date, Probate Open Date, Personal Representative, Parcel ID, Structure Type, Year Built, Living SqFt, Bedrooms, Bathrooms, Lot (Acres)
- **Custom fields (16):** Notice Type, County, Date Added, Owner Deceased, Date of Death, Decedent Name, Decision Maker, DM Relationship, DM Confidence, DM 2/3 Name/Relationship, Obituary URL, Source URL, Notice Screenshot
- **Deep prospecting (10):** DM 1/2/3 Status, DM 1 Source, Heir Count, Heirs Living, Signing Chain Count/Names, DM Confidence Reason, Data Flags
- **Entity research (3):** Entity Type, Entity Contact, Entity Contact Role

### THE WORKING PIPELINE (verified end-to-end 2026-08-21) — and the traps

Read this before touching anything DataSift. Every claim below was established
by a live test with a control, not inferred from docs. Where something is broken
it says so plainly: the expensive mistakes in this project's history all came
from a capability being assumed rather than proven.

**Proven on a real lead** (7405 S Chestnut Ave, Broken Arrow — Andrew Nordquist),
total cost **$0.185 for one record**, with an account-wide diff afterwards
confirming **zero** other records were touched.

```
petition PDF (scanned)
  -> pypdfium2 render @300dpi -> image_utils.ocr_page(psm=4)
  -> bulk_create_properties()        create - the ONLY path visible in the CRM
  -> wait_for_properties()           poll until indexed; retry ONLY the missing
  -> add_notes + post_message_board  full petition detail, both surfaces
  -> add_tags                        Courthouse Data, foreclosure, FTM
  -> tracerfy_skip_tracer            source 1   ~$0.02/record
  -> datasift submit_skip_trace      source 2   ~$0.12/owner, estimate-gated
  -> phone_validator.call_trestle    score ALL numbers  $0.015 each
  -> upsert_phones + set_phone_tags  source tag + dial tier, per number
  -> verify_phone_tags               compare TITLES, read off the record
```

One call runs it: `skip_trace_agent.run_pipeline(rows, dry_run=False)`.
`dry_run=True` does every free step and bills nothing.

```bash
# DRY RUN by default - free, prints what it would do and what it would cost
python src/main.py skip-trace --street "7405 S Chestnut Ave" --city "Broken Arrow"
python src/main.py skip-trace --csv-path leads.csv

# Raw property template -> create records + petition notes + API enrich,
# THEN trace/score/tag them. Creation and enrich are unmetered and happen
# even without --commit; every billed step still waits for it.
python src/main.py skip-trace --csv-path batch.xlsx --create --estimate
python src/main.py skip-trace --csv-path batch.xlsx --create --commit     --notice-type foreclosure --county Tulsa

# Actually run it. SPENDS MONEY - ask the user first.
python src/main.py skip-trace --csv-path leads.csv --commit
```

**THE DOUBLE SKIP TRACE IS REAL — run both sources.** They return different
numbers. On the proving run Tracerfy found two live Tulsa mobiles (both scored
100 / `Dial First`) and DataSift found an Oklahoma City number Tracerfy missed
(scored 20 / `Drop`). Running one source loses genuine coverage. Every number
carries the tag of the source that found it.

**Order is load-bearing:** Tracerfy -> DataSift -> score everything. Scoring
between the two sources leaves the second source's numbers untiered, or pays
Trestle twice.

**TRACE THE CO-BORROWER TOO (build 2026-09-24).** A foreclosure petition
routinely names a co-borrower spouse, and title sometimes carries a co-owner
the filing does not. Only the first-named individual used to be traced, so the
other decision maker — whose signature any voluntary sale needs — was never
contacted. `skip_trace_agent.co_borrower_source()` traces a second named person
at the SAME address through the same Tracerfy adapter and merges them in as a
NON-primary Person, so `writeback()`'s existing relationship-tag path labels
their numbers. Carried on rows as **`Co-Borrower First Name` / `Co-Borrower Last
Name` / `Co-Borrower Relationship`**, read by `main._trace_row()`, so both
`skip-trace` paths pick it up; `run_pipeline(co_borrowers=...)` also takes an
explicit map. DataSift's own skip trace CANNOT reach them — it is scoped to the
property's single registered owner — so a co-borrower's numbers only ever come
from Tracerfy.

Who qualifies (user's rule): **only where the filing clearly says spouse or
co-borrower AND gives a name.** Gendered wording ("husband and wife") maps to
the existing `Wife`/`Husband` phone tag; "married" or a bare co-obligor with no
stated relationship gets **no relationship tag** rather than a guessed one —
never invent a tag, they are append-only. Homestead-only defendants, ex-spouses
and unrelated title-curative parties do NOT qualify. Ran live 2026-09-23: 11
submitted, 5 returned, tags verified on the record. The county corroborated
every co-owner identified (`ALEXANDER, RICHARD ALLAN & VIRGINIA`,
`FRY, TYLER & BRITTANY`, `PEARSON, STEVEN WAYNE & GERALDINE`, ...).

**TRACERFY DOES NOT RETURN A MAILING ADDRESS.** `mail_address` / `mail_city` /
`mail_state` are INPUT columns on the upload CSV that we send blank and Tracerfy
echoes back empty — confirmed by re-downloading five real jobs (Aug 13 – Sep 23,
~78 rows), empty in every row. `tracerfy_source()` used to read `address` (the
echo of what we SUBMITTED) into `mailing_street`; it now prefers `mail_address`,
which is currently inert but correct if a provider ever supplies it. A local
`output/*_tracerfy_result.json` showing a populated, genuinely different
mail_address is NOT raw vendor output — the same job re-downloaded from Tracerfy
is blank. **A file in `output/` is not evidence of vendor behaviour; check who
wrote it before treating it as a measurement.**

**Every past Tracerfy job is retrievable FREE, forever:**
`GET https://tracerfy.com/v1/api/queues/` lists all jobs with `id`,
`created_at`, `rows_uploaded`, `credits_deducted`, `trace_type` and a
`download_url` to the full result CSV. `trace_contacts()` keeps no cache and
throws its results away, but nothing is actually lost — never re-trace to
recover a past run. Note `credits_deducted` < `rows_uploaded` is the real hit
count (28 rows -> 24 credits).

#### Capability status — do not assume beyond this table

| Capability | State | Notes |
|---|---|---|
| Upload / create | **WORKS** | `bulk_create_properties()` only |
| Address search | **WORKS** | POST-as-GET; solves cross-run dedupe |
| Tracerfy skip trace | **WORKS** | per-record, ~$0.02, billed on misses too |
| **DataSift skip trace** | **WORKS, SCOPED** | `properties` nested in `query.must` |
| Skip-trace cost preview | **WORKS, FREE** | `estimate_skip_trace()` |
| Trestle scoring + tiers | **WORKS** | 81/61/41/21; cached |
| Litigator / TCPA risk | **WORKS, default ON** | `DO NOT CALL - Litigator Risk` **replaces** the dial tier |
| Phone tags | **WORKS** | TITLES only; append-only, no removal endpoint |
| Notes / Message Board | **WORKS** | notes must be a separate call from create |
| Custom field values | **WORKS** | select/multiselect need OPTION uuids |
| Enrichment | **WORKS, SCOPED** | `properties` nested in `query.must`, same as skip trace |
| Sequences | **Playwright only** | no API in the 323-path spec |
| SiftMap sold-tagging | **blocked** | request body shape undocumented |

#### The negative findings — each cost real time or money

1. **Individual `POST` creates are invisible in the CRM, forever.** They return
   201 with a working uuid, are retrievable by uuid, appear in the API list, and
   never reach the Elasticsearch index the web UI reads. Only bulk-create does.
   Established by eliminating auth, mount and record type one variable at a
   time, including two REAL geocodable addresses both returning `type: clean`.
   **A 201 + a uuid + a clean read-back are together NOT evidence a record is
   usable.** Only presence in a list/search surface is.

2. **Skip trace goes account-wide if `properties` is at the top level.** It must
   be nested inside `query.must`. Sending it at the root is not an error the API
   reports — the key is unrecognized and scope silently becomes the whole
   account: a 2-uuid submission reported `number_of_records: 161` and billed ~29
   owners for $1.08. Nested correctly it reports 1 and bills $0.12.
   **`submit_skip_trace()` now runs the free estimate first and REFUSES if the
   count exceeds `max_records`**, so this is structurally impossible rather than
   something a human must remember.

3. **`cost` and `balance` in that response are a PROJECTION for the whole job**,
   not the charge and not your balance. Actual spend is the delta in
   `get_skip_trace_stats()["value_spent"]`, or the balance change between the
   estimate and the commit. Misreading it caused a false alarm that the account
   was nearly drained when it was fine.

4. **Phone tags need TITLES, not uuids.** Sending uuids does not error — it
   CREATES a new tag whose NAME is the uuid string, leaving junk while the real
   tag goes unapplied. The older `{"number": n, "tag_uuid": u}` payload returns
   an empty 200 and applies nothing at all. **There is no phone-tag removal
   endpoint anywhere in the spec** — tags are append-only, so this class of
   mistake can only be cleaned up manually in the UI.

5. **Emails take a BARE STRING LIST**, `{"emails": ["a@b.com"]}` — not objects.
   Sending `[{"email": ...}]` returns "Enter a valid email address", which reads
   like the address is malformed when the shape is wrong. Note the asymmetry:
   `upsert_phones` DOES take objects.

6. **Verify by the thing you asked for, not by what you sent.** A check that
   resolved titles to uuids and compared uuids "confirmed" the very junk tags
   that were wrong. Circular verification is worse than none — it manufactures
   confidence.

7. **`GET /api/internal/property/`'s `search=` query parameter is silently
   ignored** — the same unfiltered page for any value, which for days looked
   like a lagging index. The POST-as-GET body form genuinely filters.

8. **The server rewrites addresses on write.** `4920 South Troost Avenue` is
   stored `4920 S Troost Ave` (abbreviation); `17642 S Tacoma Ave` became
   `17642 S Tacoma St` (a geocoding correction). Exact matching misses both.
   `find_property_by_address()` matches house number, then normalized street,
   then falls back to ignoring the street-type suffix.

9. **Notes sent inline on create return 200 and are discarded.** Separate call.
   Tags inline on `upsert-phones` are likewise ignored.

10. **Both auth credentials must resolve to the same user.** They had drifted to
    different team seats, so the pipeline wrote as two different people
    depending on the call. Compare the `whoami()` **uuid**, not the email.

11. **Bare `POST /property/` 403s on this account** for every auth scheme
    including super-admin, on GET/OPTIONS/POST alike. Ty's
    `datasift_api_upload.py` uses it and therefore cannot run as-is.
    Re-verified live 2026-08-26 (403 on `/property/` under both auth schemes;
    200 on `/api/internal/property/` with the same credentials). **That file
    is a verbatim upstream copy and is never to be deleted** — nothing from
    Ty gets removed from this repo, whether or not it currently runs. It is
    reference, not a production path.

12. **Skip trace is asynchronous and slow.** Observed 12s on one record, 150s
    on another, and **~11 minutes** for a 2-record job on 2026-09-11. The job is
    visible at `GET /api/internal/activity/?type=skip_trace` (`status`,
    `processed`/`total`, `meta.final_cost`) via `list_skip_trace_jobs()`; the
    owner's `skiptrace_attempts` stays 0 until it finishes. Never conclude
    failure early and never re-submit to "make sure" — that is a second charge.

13. **Emails as objects make bulk-create SILENTLY DROP the record.** Owner
    emails take a bare string list. The object form `[{"email": ...}]` returns
    a loud 400 on `upsert_emails()`, but through bulk-create the job reports a
    perfectly healthy `202 accepted=1 status=enqueued` and then discards the
    record during async processing. Nothing surfaces it — the job's activity
    uuid 404s, bulk jobs never appear in the activity list, and the record is
    absent from the index under **every** `property_type`. It is
    indistinguishable from indexing lag, i.e. the exact trap behind the
    superseded platform-bug theory below. `build_api_payload()` had this wrong
    while `upsert_emails()` had it right, so it only fired on records carrying
    skip-traced emails. Fixed 2026-08-24. **When a bulk-created record never
    indexes, check the payload shape before waiting it out or re-submitting** —
    and run a positive control (an address known to exist) to prove the search
    works before blaming the index. Note the asymmetry: phones DO take objects.

14. **A source tag must never be written unless it was established.**
    `_existing_phones()` stamped `DataSift` on every number already sitting on
    a record, so numbers Tracerfy found were reported as DataSift wins. The
    user compares Tracerfy vs DataSift hit rate from these tags to decide which
    provider to keep paying, so this corrupted the one number the tags exist to
    produce. Pre-existing numbers now get `Pre-existing` — deliberately not a
    provider name, since their true origin is unknown. `datasift_source()` was
    already correct (before/after diff around the trace) and is now the ONLY
    assigner of `DataSift`. Fixed 2026-08-24. Because phone tags are
    append-only, mis-attribution is permanent without manual UI cleanup.

15. **The server rewrites addresses THREE different ways, and a real record
    then reads as "never created."** Finding 8 recorded contraction only. The
    full set, all verified live:
    - **contracts**: `4920 South Troost Avenue` -> `4920 S Troost Ave`
    - **expands**: `11300 N 118th E Ave` -> `11300 N 118Th East Ave` (2026-08-31)
    - **drops a trailing directional**: `1752 E 56th St S` -> `1752 E 56Th St`
      (2026-09-04)

    Two separate bugs fell out of this on 2026-08-31, and both made a created,
    indexed, CRM-visible record report as missing for a full 300s timeout plus
    a pointless retry — which reads exactly like a create failure:
    - `wait_for_properties.norm()` was a bare `.strip().lower()` even though
      its own docstring claimed it normalized abbreviations, and
      `datasift_uploader._addr_key()` was a **second copy** that had drifted
      identically. Both now call one shared `datasift_api.address_key()`.
      **If a match depends on both sides normalizing the same way, there must
      be exactly one function** — two copies will drift.
    - That scan reads the newest ~50 by `-created`, so a **pre-existing**
      record (bulk-create leaves an existing address alone) is structurally
      invisible to it. It now falls back to `find_property_by_address()`, which
      searches by house number. Read-only — **not** the duplicate-400 trick,
      which would create an invisible orphan.

    `find_property_by_address()` now has three tiers: exact, ignore-street-type,
    and ignore-trailing-directional. The third is **only accepted on a unique
    match** — in Tulsa `E 56th St N` and `E 56th St S` are different streets, so
    collapsing the directional could otherwise put notes, tags and skip-trace
    spend on someone else's property. Order matters inside it: strip the
    directional BEFORE the street type, or the two sides never line up.
    As of 2026-09-11 `wait_for_properties()`'s own scan applies the same rule
    (`_scan_page_matches()`, sharing `_bare_street()` with the lookup) — before
    that, every Tulsa N/S/E/W address sat out the full 300s timeout.

    **The lesson: a "not indexed" report is a claim about a LOOKUP, not about
    existence.** Before re-submitting or waiting longer, run the lookup with a
    positive control (an address known to exist) and a negative control
    (gibberish, which must return None). That separated cause from symptom in
    minutes both times.

16. **`set_phone_tags()` can return success and apply nothing.** On 2026-09-04
    it silently skipped 4 of 77 numbers on one record — same call, same payload
    shape, same batch as 73 that worked. `verify_phone_tags()` caught it by
    reading the record back; nothing in the response indicated a problem. This
    is the append-only endpoint's second silent-failure mode (finding 4 was the
    wrong payload shape). **Never treat the tagging step as done without the
    verify pass**, and when it reports gaps, re-apply only to numbers whose tag
    list is genuinely EMPTY — re-applying beside a correct tag is the
    permanent mis-attribution of finding 14. It hit 7 of 7 numbers on
    2026-09-11; `writeback()` now does the verify-and-re-send-once itself via
    `apply_phone_tags_verified()`.

#### Spend discipline

**Ask the user before EVERY metered call, including Trestle** (standing
instruction 2026-08-21, which explicitly retracted an earlier "stop asking").
Do not infer authorization from a general "go ahead" earlier in a session.

| Source | Rate | Billed on |
|---|---|---|
| Tracerfy | ~$0.02/record | every submission, **hits and misses** |
| DataSift skip trace | ~$0.12/owner | prepaid credits — **NOT** an unlimited plan |
| TrestleIQ | ~$0.015/number | per unique number — **dedupe globally first** |

A full record through the whole pipeline costs roughly **$0.15–$0.25**.

**Do NOT quote a Trestle estimate by extrapolating a prior batch's phone
count.** Tracerfy and DataSift are per-record and predictable; Trestle is per
unique NUMBER, and numbers-per-record swings widely between batches — 4.8 on
2026-08-31 (43 numbers / 9 records) versus 6.4 on 2026-09-04 (77 / 12). A
projection built on the earlier batch came in 60% under on the later one and
overshot a figure the user had already approved. The dry run's own Trestle line
is **also** useless as a forecast: it reports ~$0 because no trace has run yet,
so there are no numbers to score. Either quote a range wide enough to cover
~4–8 numbers/record, or say the Trestle line cannot be known until the traces
return — and re-confirm if it lands materially above what was approved.

#### Verification discipline

- Read the record back and compare the value you intended. Never trust a 2xx.
- Use a **control**: a query that must return nothing. The `search=` parameter
  looked functional until a gibberish term returned the same results as a real one.
- Prefer an authoritative signal (`GET` by uuid, the local Trestle cache) over an
  inferred one. "Line-typed but untiered" looked like 168 failed tags; the cache
  showed the real number was 4.
- **Snapshot before, diff after.** For any run that touches the CRM, capture every
  record's phones/tags/skiptrace state first, then diff — that is the only way to
  prove "nothing else was touched", and it is free.
- `set_dry_run(True)` intercepts every mutating call in `datasift_api` at one
  point in `_request()`, so new write functions are covered automatically.
- **When a payload's shape is unknown, capture what the web app sends** —
  Playwright with `route.abort()` reads the real contract without submitting
  anything. That is how the scoped skip-trace payload was found, after the spec,
  the OPTIONS schema and the reference implementation all failed to reveal it.
- **A check over zero items is not a pass.** Assert a non-zero denominator
  before reporting success. On 2026-08-26 a verifier read `owners` (a list)
  when the real shape is `owner` (a dict), found 0 phones, and printed "no tier
  mismatches" and "every number carries a source tag" — both vacuously true and
  both read as green. Dump one real object and confirm the keys before trusting
  any reader you just wrote.
- **Never assert an absence you did not look for.** "No Message Board posts"
  and "no source tags" were inferred from a code path, not read off a record.
  The Message Board posts existed and were rich. Read the object, or say you
  have not checked.
- **A settled-sounding note here may be an unfinished investigation.** "Enrich
  must not be called blind" read like a wall for five days; it was an open
  question, and one `route.abort()` capture answered it. Check the evidence
  class before inheriting a conclusion: "captured live", "verified 3/3", "with
  a control" is a finding. "risks", "unknown", "do not" is a to-do.
- **Fix the class, then re-grep.** After renaming or deleting a command,
  search the CLI, dispatch, argparse, docstrings, CLAUDE.md, memory **and
  `.claude/skills/*/SKILL.md`** — skills chain into commands and fail silently.
  Deleting `skip-and-score-upload` left four live invocations in CLAUDE.md and
  a skill that would have died on argparse after doing all its OCR work.
- **Before saying "done" or "confirmed", run the search that would prove you
  wrong.** Each time the user asked "are you sure?" on 2026-08-26, there was
  more. Go look first.

### REST API (build 1.0.34+, live 2026-08-19)

Early access — the user's own key, not the public "coming soon" API. Two surfaces, same `Authorization: Api-Key` header:

| API | Base URL | Covers |
|---|---|---|
| Core | `apiv2.reisift.io` | properties, owners/phones, tags, lists, notes, custom fields, skip trace, filter presets, activity |
| SiftMap | `map.reisift.io` | nationwide property search, map filters with auto-add |

`DATASIFT_API_KEY` in `.env` is this key (format: `prefix.suffix`, 8+32 chars) — no longer dormant.

**Gotchas confirmed live, none of them documented in the official reference:**
- `owner.address` is **required** on property create — 400s with `{"owner":{"address":["This field is required."]}}` if omitted.
- Entity (business) owners must **omit** `first_name`/`last_name` entirely, not send `""` — 400s on a blank first_name. `build_api_payload()` in `datasift_formatter.py` is where this gets fixed; the CSV/Playwright path still leaves these blank, which the CSV importer tolerates but the API does not.
- Custom fields use `label`/`field_type`/`uuid` in real payloads, **not** `title`/`type`/`id` as the generated reference implies. Creating a field also requires `entity_type` and `group_id` — not shown in the reference's create example.
- Phone tag apply (`POST /api/internal/phone/add-phone-tag/`) takes a **bare list** of `{"number": ..., "tag_uuid": ...}` objects (key is `number`, not `phone`), not a `{"phones": [...], "tag_uuid": ...}` wrapper. The number validator wants a real area code — `865-555-0100` works, `555-555-0100` doesn't.
- Notes sent inline on property create return 200 and are silently discarded — always a separate `POST .../add-notes/` call.
- Tags must be an array; a comma-joined string creates one literal tag.
- **`GET /api/internal/property/` has no `search` parameter at all** — corrected 2026-08-19. The real, spec-declared query parameters are only `limit`/`offset`; the general "list endpoints accept `search=`" claim in the conventions doc does not hold for this endpoint, and it was never verified against this endpoint's actual schema before `find_property_by_address()` was built on it. The endpoint silently ignores an unrecognized `search=` value rather than erroring, so it always returned the same fixed, unfiltered page regardless of query — confirmed by sending a guaranteed-zero-match gibberish term and getting identical results to a real one. This was mistaken for search-index lag at first (the symptom — "not found" regardless of how long you wait — looks identical), but re-running Enrich/phone-read many minutes later never changes because there was never a real search happening. **Fix:** don't rely on server-side address search at all. `upload_to_datasift()` persists each created record's uuid to a local map (`output/.datasift_uuid_map.json`, keyed by `lower(owner_last)|lower(street)` — see `_uuid_map_key()`/`_load_uuid_map()`/`_save_uuid_map_entries()`), and every later, separate call (`skip_trace_records()`, `read_record_phone_numbers()`, `upload_datasift_split()`'s extra-note step) reads that map instead of searching. This also means **cross-run dedupe and `mode="update"` don't currently work** — there's no reliable way to find a record created in an *earlier* process invocation (only `property_exists()` via `reapi_id`/`sift_id` is real, and courthouse-sourced records don't have those). `upload_to_datasift()` always creates fresh now rather than pretending to check; a real fix needs either a working address-lookup endpoint (still undocumented — `autocomplete/` and `exists/` also have no usable declared schema) or accepting some duplicate-address risk on true re-uploads.
- **SETTLED 2026-08-21 — create ONLY via `bulk_create_properties()`, never `create_property()`.** DataSift keeps a primary DB and a separate Elasticsearch index; the CRM web UI reads ES. Individual `POST` creates write the DB alone: they return 201 with a real, correct, uuid-retrievable record that **never becomes visible in the CRM**. bulk-create runs through the activity/job queue, which indexes into ES, and appears normally. Established by single-variable tests after an earlier A/B confounded endpoint with auth:
  - **Auth is not the variable.** An individual create using the account owner's own JWT returned 201, reached the API list index, and still could not be found in that same owner's CRM UI.
  - **The mount is not the variable.** `/api/internal/property/` and `/api/internal/properties/property/` behave identically. (Bare `POST /property/`, which Ty's `datasift_api_upload.py` uses, returns **403 on this account** for GET/OPTIONS/POST under every auth scheme including a super-admin key — not a role ceiling, that mount is simply unavailable here.)
  - **Address quality is not the variable.** Two *real, geocodable* Tulsa foreclosure leads, both `type: clean`, created minutes apart: the individual-endpoint one invisible, the bulk one visible. This also disposes of the theory that `type: incomplete` (from fake test addresses) explained the earlier results.
  - **Corollary worth internalizing:** a 201, a uuid, and a clean read-back *by uuid* are together still NOT evidence a record is usable. Only presence in a list/search surface is. Every wrong conclusion in this file's history came from treating one of those as sufficient.
- **`upload_to_datasift()` is API-only as of 2026-08-21.** bulk-create in chunks → `wait_for_properties()` polls until each address indexes (bulk-create's own job activity is NOT retrievable — the returned activity uuid 404s and bulk jobs never appear in `GET /api/internal/activity/`, whose real enum value is `create_properties`, so records appearing is the only completion signal) → one retry of ONLY the missing addresses, never the whole batch → per-uuid notes + custom fields → skip trace. **No browser at all** — `enrich=True` was the last exception and became API-scoped 2026-08-26. Verified live 3/3, confirmed visible in the CRM.
- **The duplicate-400 trick is a real address lookup, with one sharp edge.** Re-POSTing an existing address returns `400 {"non_field_errors": ["Property address already exists!"], "property": ["<uuid>"]}`, which genuinely resolves an address to its uuid across process runs. **But on an address that does NOT exist it CREATES one** — an invisible DB-only orphan that also squats the address so a later bulk-create is rejected as a duplicate. Safe only on addresses known to exist; never as a speculative lookup.
- **`wait_for_properties()` stale-delete hazard.** The list index keeps returning a record for a while after deletion, so on delete-then-recreate of the same address it can hand back the DEAD uuid (hit live 2026-08-21; the follow-up GET 404'd). Pass `verify_live=True` to GET each candidate first — it also returns full detail records rather than the thinner list objects, which omit `type` and `tags`.
- **Both auth credentials must resolve to the same user.** They had silently drifted: `DATASIFT_API_KEY` belonged to a different team seat (super-admin) than the JWT minted from `DATASIFT_EMAIL`/`DATASIFT_PASSWORD` (the account owner), so the pipeline wrote as two different people depending on the call. Fixed by re-issuing the key under the owner's login. If either credential is swapped, call `whoami()` on both and compare the returned **uuid**, not the email.

<details><summary>Superseded 2026-08-19/20 investigation, kept only as a record of how the wrong conclusion was reached</summary>

Everything in this block is **wrong** and is retained solely because the shape of the mistake is instructive. It concluded that neither JSON creation endpoint ever indexes, that the Playwright wizard was the only reliable creation path, and that this was a confirmed platform-side bug worth escalating to DataSift support.

The root error: judging indexing before the index had caught up. The diagnostic did create → `sleep(2)` → re-POST → GET → list-check, a sequence completing in ~10 seconds against indexing that takes longer. A negative list-check inside the first minute means nothing, and that single measurement error produced a platform-bug theory, a support escalation, and an architecture rewritten around Playwright. A record declared "permanently invisible" that evening later showed a CRM creation timestamp exactly matching the original API call.

A second error compounded it: the follow-up A/B varied **two** things at once (bulk+JWT vs individual+Api-Key), so "bulk vs individual" and "JWT vs Api-Key" both fit the data, and the first reading was adopted without separating them. It happened to be right, but it was not established until the single-variable tests above.

</details>

- **An enrich endpoint DOES exist — and must not be called blind.** `POST /api/internal/property/enrich/` is real (corrects the earlier "no enrich endpoint exists" claim in this file). But its trigger contract is undocumented, and an empty POST reports a count of *every property in the account*. Guessing at it risks running **owner** enrichment account-wide, which would replace the personal representative on every probate record with the deceased owner of record and undo the entire point of the PR contact mapping. **SOLVED 2026-08-26 — enrichment is now API-scoped.** The contract was captured off DataSift's own web app with a Playwright route handler that ABORTED the request, the same technique that cracked the skip-trace payload. It is the identical shape: `{"query": {"must": {"property_type": "clean", "search": "<term>", "properties": ["<uuid>"]}, "ordering": ["-list_count"]}, "enrich_property": true, "enrich_owner": false, "replace_owner": false}` — ***`properties` nests inside `query.must`***, and an un-nested or empty list is what silently went account-wide. `datasift_api.enrich_properties()` refuses an empty list outright and pre-flights the same scoped query through the FREE skip-trace estimate, refusing if it matches more records than asked for. The endpoint echoes `{"count": N}`; `enrich_records()` treats a count that disagrees with the request as a failure. Verified live on 9 records: pre-flight said 9 (not the account's 962) and the response said `count: 9`. The three toggles are explicit booleans. **Corrected 2026-09-24: `enrich_owner=True` on its own is a NO-OP; `replace_owner=True` is what actually writes** (the owner name AND the real mailing address). Both are now ON by default for non-probate, behind a per-record gate plus a name-restore pass — see "Enrich toggles" in the UI-automation section. They must still never run on probate, which the gate enforces. (Also corrected: DataSift DOES populate native valuation fields at create time — a freshly bulk-created throwaway carried `equity_percent` 100.00, `estimate_value` $293,000 and `mls` "Off Market" before any enrich call. The earlier "null on create" note was wrong. And the server drops a trailing directional on write: `2213 S Gary Ave E` → `2213 S Gary Ave`.)
- **Select/multiselect custom fields need the OPTION's uuid, not its label** — 43 of this account's 80 custom fields. Sending a label 400s with `"... is not a valid UUID."`, and because the PATCH is a **batch**, one unresolvable value fails the whole request and costs that record *every other custom field with it*. `datasift_api.resolve_custom_field_value()` resolves label → option uuid and **skips + reports** unknown fields/options rather than guessing. Verification compares read-back values: the field uuid is nested at `item["custom_field"]["uuid"]` — **not** `field_uuid`, and not the row's own top-level `uuid`, either of which finds nothing and "verifies" a write that never landed.
- **Custom fields are never auto-created.** `get_or_create_custom_field()` was removed from the upload path; it used to create ~46 fields defaulted to `field_type="text"` in a "SiftStack" group. The account's custom fields are deliberately curated, so nothing is created implicitly as a side effect of an upload. (The CRM itself is in scope as of 2026-08-21 — deliberate, explicit schema changes are fine; silent ones are not.) Unmatched labels now skip with a warning — the tradeoff being that columns without a matching field no longer land at all. Note that creating a `select` field requires its options **in the same POST**.
- **No Sequence API** anywhere in the 323-path official spec or the endpoint index. `create_sold_sequence()` stays Playwright-only, permanently.
- The 18 "built-in" CSV fields (Estimated Value, Bedrooms, etc.) are routed through the custom-fields mechanism by `build_api_payload()` rather than guessed at as native property keys — a wrong native-key guess would silently drop the value, whereas a custom field write is visible and correctable later.

**Open items, not yet migrated:**
- `update_all_presets_sold_exclusion()` / `create_sold_sequence()` — the account's real preset structure (64 presets across 14 folders: "01. HOTTEST - CALL", "05. TIER 1 - FTM - CALL", "11. DEEP PROSPECTING (ALL TIERS)", etc., created 2026-07-29) no longer matches the 21-preset/2-folder "00 Niche Sequential Marketing" / "01. Bulk Sequential Marketing" layout these functions target — that structure was apparently rebuilt outside this codebase. Porting the Sold-exclusion logic needs a decision on what the *current* funnel's exclusion policy should be, not a mechanical rewrite. `discover_presets()` itself **is** migrated (API-based, reads whatever folders/presets actually exist — see `datasift_api.list_filter_presets()`/`list_filter_preset_folders()`).
- `manage_sold_properties()` / `run_manage_sold_workflow()` (SiftMap sold-property tagging) — `POST /properties/search/` on the SiftMap API rejects every address/filter/polygon payload shape tried with the same generic "must provide filter, address, or polygon" error; the official reference doesn't document the working request body. Needs either real SiftMap API docs or a DevTools capture of the app's own SiftMap search call to unblock.
- Similarly, custom field **groups** in the real account ("Qualifying Questions", "Property Condition - General", "CapEx Assessment", ...) don't match the "SiftStack" group CLAUDE.md previously described — `build_api_payload()`'s custom fields land in a new "SiftStack" group created on first write, alongside whatever pre-existing groups the account has from its own evolution.

### Niche Sequential Marketing
DataSift's niche sequential system uses filter presets to guide records through SMS → Call → Mail → Deep Prospecting phases. Two preset folders: "00 Niche Sequential Marketing" (12 presets, courthouse data) and "01. Bulk Sequential Marketing" (9 presets, bulk data). All 21 presets exclude Sold status (build 1.0.23). A "Sold Property Cleanup" sequence in the Transactions folder auto-fires on "Sold" tag to change status, remove from lists, clear tasks, and clear assignee.

- **"Courthouse Data" tag:** Every record gets this tag — signals first-to-market county data (prioritized over bulk data in filter presets)
- **Lists column:** Maps `notice_type` → DataSift list name (`foreclosure` → "Foreclosure", `probate` → "Probate", `tax_sale` → "Tax Sale", `tax_delinquent` → "Tax Delinquent", `eviction` → "Eviction", `code_violation` → "Code Violation", `divorce` → "Divorce"). DataSift auto-creates lists from CSV.
- **Tags:** Courthouse Data, notice_type, county, YYYY-MM date, deceased/living, DM confidence level, has_auction, tax_delinquent, photo_import (for photo-sourced records)

### Upload Wizard (5 Steps)
1. **Setup:** Click "Upload File" sidebar → "Add Data" → dropdown "Uploading a new list not in DataSift yet" → enter list name → organization questions
2. **Tags:** Skip through (tags are in CSV column)
3. **Upload File:** Set file on `input[type="file"]`
4. **Map Columns:** Core address fields auto-map; Tags, Lists, and enrichment columns may need manual mapping
5. **Review + Finish Upload:** Click "Finish Upload" — processing happens in background

### Column Mapping Notes
- Only core address fields (Property Street, City, State, ZIP) reliably auto-map
- Tags, Lists, Estimated Value, and enrichment columns often stay unmapped in step 4
- Notes and MSL Status sometimes auto-map
- Custom fields (TN Public Notice group) require drag-and-drop mapping

### Contact Logic
- **Deceased owners:** Contact = decision maker (first/last name + mailing address from DM)
- **Living owners:** Contact = property owner (owner mailing address, falls back to property address)

### Post-Upload: Enrich + Skip Trace

After upload, the pipeline runs two DataSift actions, both ON by default when `--upload-datasift` is set:

1. **Enrich Property Information** (REST API as of 2026-08-26, scoped — see the REST API section; `enrich_records_playwright()` kept as rollback): Adds SiftMap property data (beds, baths, Zestimate, sqft, sale history) to uploaded records. **Owner enrichment is ON by default as of 2026-09-24, gated per record** — it is the only way to get a real mailing address, and the petition's person name is restored afterwards so nothing is lost. See "Enrich toggles" below.
2. **Skip Trace** — see "THE WORKING PIPELINE" above. Scoped over the REST API; `properties` must be nested inside `query.must` or it goes account-wide. Always `estimate_skip_trace()` first. Runs asynchronously — verify via `has_phones`/`skiptraced` on a re-read or `datasift_api.get_skip_trace_stats()`, not the submit call's response alone.

   **THIS SPENDS REAL MONEY. Corrected 2026-08-21 — this file previously claimed an "unlimited plan ($97/mo)", which is wrong.** The account runs on **prepaid credits**, so every submitted record draws down a finite balance and an over-large or repeated submission is unrecoverable spend. Treat it as a billed action under the no-unapproved-spend rule: never submit speculatively, never submit test/throwaway records, and never re-submit a batch to "make sure" — check `skiptraced`/`has_phones` first. Note that `skip_trace` defaults to **True** in `upload_to_datasift()` and its variants, so an upload spends credits unless `--no-skip-trace` is passed. `submit_skip_trace()` logs the record count as billable before sending; there is no server-side balance check available, so the count in that log line is the only pre-flight signal.

Both run in background — tracked in Activity tab.

### CLI Flags
```bash
python src/main.py daily --upload-datasift        # upload + enrich + skip trace
python src/main.py daily --upload-datasift --no-enrich       # upload only, skip enrichment
python src/main.py daily --upload-datasift --no-skip-trace   # upload + enrich, skip skip trace
python src/main.py daily --notify-slack            # send run summary to Slack/Discord
python src/main.py daily --deep-heirs               # resolve deceased-owner heirs via Enformion (~$0.35/match)
```

### Single-Command Pipeline: `skip-trace --create` (build 1.0.35+)

**THIS IS THE ONLY DataSift pipeline. There is no other one to reach for.** Runs raw-CSV-to-scored-and-tagged in one command: create (bulk-create + petition notes + custom fields + lists) → API enrich → Tracerfy → DataSift skip trace → Trestle scoring → phone tags (source + tier) → Message Board. **Dry run by default; `--commit` is the spend gate.** Drop `--create` when the records already exist in the CRM. Built from the manual trial-run pipeline proven live in the 2026-08-13/14 session; every step reuses the same functions (`upload_to_datasift()`, `read_record_phone_numbers()`, `run_phone_validation()`, `upload_phone_tags()`), not a reimplementation. As of build 1.0.34, `upload_to_datasift()`, `skip_trace_records()`, `read_record_phone_numbers()`, and `upload_phone_tags()` are the REST API versions — same functions, same signatures, same per-record safety discipline (no bulk selection, exact-match lookup before any write), now backed by direct API calls instead of Playwright clicks. `enrich_records()` is also the REST API version as of 2026-08-26, so this pipeline no longer launches a browser at all.

```bash
# Estimate only — free, no CRM contact
python src/main.py skip-trace --csv-path "Property Records.xlsx" --create --estimate

# DRY RUN (the default) — creates + enriches (both unmetered), then prints exactly
# what it WOULD trace/score/tag and the total spend. Bills nothing.
python src/main.py skip-trace --csv-path "Property Records.xlsx" --create     --notice-type foreclosure --county Tulsa

# The real run. SPENDS MONEY — ask the user first.
python src/main.py skip-trace --csv-path "Property Records.xlsx" --create --commit     --notice-type foreclosure --county Tulsa

# Mark a batch as a test run (tagged/noted so it's easy to distinguish from real leads)
python src/main.py skip-trace --csv-path "Property Records.xlsx" --create --commit     --trial-tag "Pipeline_Trial_2026-08-14"
```

Input file: a raw property-upload-template `.xlsx` or `.csv` with columns `Property Street, Property City, Property State, Property Zip, First Name, Last Name[, Record Link]` — no phone numbers, no DataSift formatting. Blank template rows (these ship as fixed-size sheets, e.g. 300 rows with only a couple filled in) are skipped automatically.

**Petition detail in Notes / Message Board (expanded 2026-08-21).** When the input came from the `petition-info-extraction` skill, `datasift_formatter._format_petition_notes()` renders its columns into grouped **CASE / PROPERTY / LOAN / MODIFICATIONS / OWNER / LIENS** sections on both the property Notes and the owner Message Board. Beyond the original six loan figures it now carries legal description, plat number, plaintiff, co-defendants, original lender, initial vs current rate, recording document numbers, modification count + history, junior lienholders, and owner status — and appends an automatic **CAUTION** when the unpaid balance exceeds the original loan (arrears capitalized through repeated modification; equity may be thin or negative). `_PETITION_SECTIONS` there and the field table in the skill's `SKILL.md` **must stay in sync** — a field added to one and not the other is extracted and then silently dropped. Signals worth reading: a high modification count means a modification-exhausted borrower, and institutional co-defendants (HUD, IRS, banks, judgment creditors) are junior liens that bear directly on equity. Note also that a petition body may give only a legal description with the street address appearing solely in the Mortgage exhibit — and the two can name different towns.

**Consolidated 2026-08-26.** `skip-and-score-upload` is GONE — it carried a
second, weaker implementation of the trace/score/tag half that applied **no
phone source tags** (it DID post the petition Message Board entry — an earlier
claim that it did not was wrong; it skipped only `writeback()`'s one-line phone
summary). Only its
create/format/enrich half was worth keeping; that is now
`_create_records_for_batch()` behind `skip-trace --create`, and everything
after creation is `skip_trace_agent.run_pipeline()` — one implementation.
Records are created **without phones on purpose**: Tracerfy runs later inside
run_pipeline, so every number reaches the CRM through the same
`upsert_phones` + `set_phone_tags` path and therefore carries an honest source
tag. The old >12-phones-per-record confirmation prompt is now a logged
`OUTLIER` warning in `score_phones()`, since dry-run-by-default plus `--commit`
is the stronger gate and a prompt breaks unattended runs.

**Do not re-run a batch whose source tags are already correct.** `_existing_phones()`
stamps every number already on a record as `Pre-existing` (correct — their true
origin is unknown), so a second pass over an already-traced record appends
`Pre-existing` beside the real `Tracerfy`/`DataSift` tag. Phone tags are
append-only, so that permanently muddies the provider comparison the tags exist
to produce.

### Environment Variables
- `DATASIFT_API_KEY` — REST API Open API key (build 1.0.34+, early access) — required for upload/tags/notes/custom-fields/skip-trace/phone-tags/`discover_presets()`
- `DATASIFT_EMAIL` / `DATASIFT_PASSWORD` — **required for the API path too**: `bulk_create_properties()` needs a Bearer JWT minted from these via `POST /api/token/`, so an API-only run still depends on them. Also used by the remaining Playwright paths (`create_sold_sequence()`, `update_all_presets_sold_exclusion()`, `manage_sold_properties()` (SiftMap)) and as a rollback for any `*_playwright()` function. Must resolve to the same user as `DATASIFT_API_KEY` — compare `whoami()` **uuid**, not email.
- `SLACK_WEBHOOK_URL` — Slack/Discord webhook for run summaries

### Login Selectors (SPA quirks, Playwright-only paths)
- Hidden checkboxes (Remember me, Terms) — click `<label>` elements, not `<input>`
- Use `wait_until="domcontentloaded"` (not `networkidle` — SPA keeps WebSocket connections open)
- Cookie validation: check for `/dashboard` or `/records` in URL (5s wait for SPA redirect)

### DataSift UI Automation Patterns

Hard-won patterns from build 1.0.22-1.0.23 (SiftMap, preset management, sequence builder). Follow these to avoid repeating past mistakes.

**Styled-Components (no native HTML controls)**
- No native `<select>` elements — all dropdowns are `[class*="Selectstyles__Select"]` containers
- `[class*="SelectValue"]` = current value display; `[class*="SelectOptionContainer"]` = dropdown options
- Multiple Select dropdowns exist per panel (Lists, Tags, Property Status) — always target the **LAST visible one**
- Use `x > 450` bounds check in all JS queries to avoid matching sidebar elements (sidebar is 0-400px)
- React state updates require native setter + event dispatch, not just `.value = ...`:
  ```js
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(input, 'new value');
  input.dispatchEvent(new Event('input', {bubbles: true}));
  input.dispatchEvent(new Event('change', {bubbles: true}));
  ```

**Panel Scrolling (Playwright scroll fails)**
- Filter panel is a scrollable `<div>`, NOT the viewport — `scroll_into_view_if_needed()` does nothing
- Use JS: `el.scrollIntoView({behavior: 'instant', block: 'center'})` instead
- Filter Presets section is at the BOTTOM of the filter panel — must scroll container down to reveal
- After scrollIntoView, element y-positions may be negative — don't filter by `y > 0` for the target element

**React DnD (Sequence Builder)**
- Cards have `draggable="false"` — Playwright's native drag won't work
- Must use slow mouse drag: `mouse.move()` → `mouse.down()` → 20 incremental steps (50ms each) → `mouse.up()`
- Add 500ms pauses between down/move/up phases
- "Add new Action +" button required for 2nd+ actions; first action uses initial drop zone
- Sidebar cards can scroll out of view when main area scrolls — scroll BOTH source and target into view before drag

**Pointer Interception (common blockers)**
- Beamer NPS survey iframe (`#npsIframeContainer`) blocks ALL pointer events globally — remove from DOM via `_dismiss_popups()`
- `RecordsFiltersstyles__RecordsFiltersSection` elements intercept clicks — use `page.evaluate()` JS click or `force=True`
- When Playwright click fails with "outside of viewport" or "intercept": switch to `page.evaluate(el => el.click())`
- SiftMap PropertyDetails panel blocks sidebar checkboxes — remove from DOM before interactions

**Preset Management Workflow**
- Flow: open filter panel → scroll to bottom → expand "Filter Presets" → expand folder → click preset → modify → Save (not Save New) → confirm overwrite
- Folder names have case variations ("00 Niche" vs "00 NICHE") — use `.toUpperCase()` comparison
- Preset names follow pattern `^\d{2}\.` (e.g., "00. Needs Skipped")
- 2 folders: "00 Niche Sequential Marketing" (12 presets), "01. Bulk Sequential Marketing" (9 presets)
- All 21 presets have Property Status "Do not include" → "Sold" (build 1.0.23)

**Sequence Builder Workflow**
- Flow: `/sequences` → Create → title + folder → drag trigger → condition → actions tab → drag actions → configure → save
- Duplicate name handling: detect error toast "different sequence title", retry with " V2" suffix
- Actions tab: navigate via "Set the Following Actions" button or URL (`/sequences/new/actions`)
- Autocomplete inputs: after each selection, `fill("")` + Escape to dismiss dropdown before next entry
- "Sold Property Cleanup" sequence exists in Transactions folder (build 1.0.23): Trigger (Property Tags Added) → Condition (Sold) → Actions (Status→Sold, Remove Lists, Clear Tasks, Clear Assignee)

**SiftMap Automation**
- Search by city (NOT county): Knox → "Knoxville, TN", Blount → "Maryville, TN"
- PropertyDetails panel auto-opens on search — remove from DOM before other interactions
- "Add Records to Account" modal: toggle OFF "Do not replace owners", add tags, dismiss dropdown by clicking heading (NOT Escape — clears tags)
- Known limitation: SiftMap filters (price, date) set values visually but don't trigger React re-query. Only sidebar-visible properties (~3-5) get added per run

**Market Finder Extraction Patterns (build 1.0.29+)**

Hard-won patterns from building `extract_market_finder.py`. The Market Finder UI differs significantly from the rest of DataSift.

- **NO HTML `<table>` element** — data table is entirely div-based: `Tablestyles__TableContainer` → `TableRow` → `TableCell` (styled-components). Searching for `<table>` or `<tr>/<td>` finds nothing.
- **PAGINATION, not infinite scroll** — table shows 20 rows per page with "1-20 of N" text and `PaginationInnerContainer` with prev/next `<button>` elements. Must click through ALL pages to get complete data. Knox County has 48 ZIPs (3 pages) and 120+ neighborhoods (7 pages).
- **State/County selection uses `InputMultiSearch`** — NOT styled-component Select dropdowns. Inputs have placeholders: `"Select States"`, `"Select Counties"`, `"Select ZIP Codes"`. Click input → type name → click dropdown result item (`[class*="Item"]:has-text("...")`).
- **ZIP/Neighborhood toggle is a styled Select dropdown** — at the top bar with `Selectstyles__SelectValue` showing current view. Check the displayed text BEFORE clicking — if already on the correct view, clicking toggles AWAY from it. Only click to switch if the displayed text doesn't match the desired view.
- **Beamer push modal (`#beamerPushModal`)** — appears on fresh login, blocks ALL pointer events. Different from the NPS survey (`#npsIframeContainer`). Both must be removed from DOM before any click interactions. Always call dismiss with `force=True` as fallback.
- **Page body scrolling required** — pagination controls are at `y=1867`, below the viewport (`clientH=824`). Must scroll `AdminPage__AdminPageBody` container down before pagination buttons are accessible.
- **Summary panel on right side** — shows county-level aggregates: Median Home Value, Homes on Market, Mo. Investor Transactions, Homes Sold Last Month, Market Rent, Gross Rental Yield, Homeownership Rate. Extract via regex on page text.

```bash
# Extract all Market Finder data for a county
python src/extract_market_finder.py --state "Tennessee" --county "Knox" -v
python src/extract_market_finder.py --state "Tennessee" --county "Knox,Blount" --headless

# Output: JSON file in output/market_finder_{state}_{county}_{timestamp}.json
```

**Per-Record Selection Pattern — no bulk "select all", ever (2026-08-13/14 incident)**

`_select_all_records()` was permanently deleted from `datasift_uploader.py`. Two live incidents on the user's real DataSift account — a silently-failed list filter causing a bulk action to select 78 unrelated existing records instead of the run's own 2, caught only because the user manually intervened — mean this codebase must never reintroduce any header/bulk/"select all" checkbox primitive. Every DataSift action that touches records now:

1. Reads the exact target records from the CSV actually being processed for this run (never a list name or filter alone — those have proven unreliable).
2. For each record: searches, requires an **exact 1-row match** (owner last name + property street number), selects only that row's own checkbox, then independently re-verifies via a fresh DOM query that exactly 1 checkbox is checked before proceeding to any action button. 0 or 2+ matches → skip that record, never guess.
3. Acts on that one record, then moves to the next. No batch selection step exists anywhere in this flow.

See `_select_single_verified_record()`, `enrich_records()`, `skip_trace_records()` in `datasift_uploader.py`. `manage-list` CLI mode is disabled — it had no source CSV to scope selection by, so there's no safe way to run it under this pattern.

**Checkbox clicks need `dispatch_event`, not `.click()`** — DataSift's row checkboxes are visually-hidden native `<input>` elements (width/height 0, opacity 0) with a custom-styled visual layered on top, same pattern as the login page's "Remember me"/Terms checkboxes. `Locator.click()` correctly refuses a 0×0 element ("outside of the viewport"); use `checkbox.dispatch_event("click")` instead to fire React's onChange directly.

**A table row IS the link** — clicking into a record's detail page is `row.click()` on the row itself (`<a class="...TableRowContainer">` wraps the whole row), not a nested `<a>` or `[class*="Owner"]` element inside it — there isn't one. The visible name text is a plain styled `<div>`.

**Export wizard is unreliable — read phone data off the record detail page instead.** `export_phone_enrichment()`'s Manage → Export wizard repeatedly failed in live testing (Filter Records panel silently not opening, multi-strategy download fallback maze). `read_record_phone_numbers()` sidesteps it entirely: search → verify exactly 1 match → open that one record → read the "PHONE NUMBERS ... EMAILS" block directly off the page. No selection, no wizard, no filter.

**`dismiss_popups()`'s JS fallback must never remove an active wizard.** Its overlay-removal selector (`[class*="ModalOverlay"]`, meant for stray notification popups) also matches DataSift's own step-wizard container — calling it mid-wizard silently deleted the wizard itself, and every step after that ran blind against whatever was actually on screen. Fixed by skipping any element containing "Next Step" or "Finish Upload" text, but be aware this is a generically-scoped selector shared by every automation flow in this file — if a future DataSift UI change adds another overlay containing those exact words, re-check this guard.

**DataSift's search index lags behind a just-completed upload.** A record can read as "not found" immediately after upload and then show up correctly, address and all, after nothing more than a page reload. `verify_uploaded_records()` retries with a hard reload before treating a miss as a real mismatch — don't remove that retry to "simplify" the function; it's covering a real, confirmed CRM behavior, not defensive over-engineering.

**Enrich toggles — SUPERSEDED 2026-09-24. Owner enrichment is now ON by default, gated PER RECORD.** The 2026-08-21 setting was Property ON / Owners OFF / Swap OFF, on the reasoning that owner enrichment replaces our contact with DataSift's owner of record — actively destructive on probate, where it swaps the PR for the *deceased* owner. That reasoning still holds; what changed is that the destructive half is now prevented structurally instead of by leaving the whole feature off.

What was established (throwaway records at properties whose true owner and mailing address were known in advance; both deleted afterwards, each with a 210-record control diff showing zero collateral change):

- **`enrich_owner=True` alone is a NO-OP.** It changed nothing even on a record with a fake owner name and a placeholder mailing address. Two earlier "owner enrichment does nothing" conclusions came from this plus testing on owner-occupied records where the correct result was also "no change" — a worthless test.
- **`replace_owner=True` is the toggle that actually writes**, and it wrote the county's exact owner (`Edward Slattery`) and exact mailing address (`Po Box 690240`).
- **On trust-held title it is ADDITIVE, not destructive**: first/last survived while `company` gained the trust name and `type` became `trust`. That is a gain — it names who must actually sign — and is never undone.

`upload_to_datasift(replace_owner=True)` is the default and is passed through to `enrich_records()`. Two protections make it monotonic ("only ever makes records BETTER" — the user's condition for enabling it):

1. **`datasift_uploader.owner_replace_eligible(row)`** excludes probate, `Owner Deceased=yes`, `Owner Alive=No`, a named decedent, deceased/PR/survivorship wording in `Owner Status`, and an explicit `Owner Overridden` flag. Read off the row already in hand — **no per-record lookups** — and `enrich_records()` makes two batched calls instead of one. Gated records still get property enrichment. Gates exactly 2 of 28 on the 2026-09-22 batch (Kim, Hopkins).
2. **`_restore_people()`** puts the petition's person back whenever the owner name changed at all, keeping the enriched mailing address, company and secondary owners. The petition names the *defendant being foreclosed on* — the person to reach — while the county may name a different co-owner (`BLEULER, SHERI & TIM ESTABROOK` vs our Timothy Estabrook). `update_owner_name(..., clear_company=False)` by default, so restoring the person does NOT strip the trust/LLC that enrichment added.

**Net effect: mailing address, trust/LLC, secondary owners and property data are gained; the owner name never changes.** Caveat worth keeping honest: DataSift's mailing address is proven right on ONE record and looked stale on one (SiftMap showed the property address for Tipton where the county says `PO BOX 195`). Better than the placeholder either way, but "accurate" is a one-sample claim.

`_read_csv_absentee_flags()` remains uncalled but is deliberately kept — still the only structural absentee detector in the codebase.

## REI Skill Library (13 Skills)

Distribution-ready Claude Co-Work skill files at `Skills for REI/improved/`. Each `.skill` is a ZIP containing `SKILL.md` + `references/` folder. Plugins (`.plugin`) also include `commands/` and `.claude-plugin/plugin.json`.

### Skill Inventory

| # | File | Division | Score | What It Does |
|---|------|----------|-------|-------------|
| 1 | `sift-market-research.skill` | Market Intel | 9.6 | Market Finder reports, zip code scoring (6 weights verified against `market_analyzer.py`), 7-sheet Excel output |
| 2 | `first-market-county-data.skill` | Market Intel | 9.7 | County clerk data extraction for all 7 notice types, FOIA templates, marketing windows |
| 3 | `buyer-prospector.skill` | Market Intel | 9.6 | Cash buyer list from 84K+ records, LLC/trust/corp research, 50-state SOS URLs |
| 4 | `real-estate-comping.skill` | Deal Analysis | 9.7 | Two-Bucket ARV, disclosure/non-disclosure routing (12 states), adjustments verified against `comp_analyzer.py` |
| 5 | `rehab-estimator.skill` | Deal Analysis | 9.8 | 912-line skill, complete Repair Cheat Sheet verified against real contractor SOW, 4-tier system |
| 6 | `deal-analyzer.plugin` | Deal Analysis | 9.6 | Combined comp+rehab pipeline, MAO (75%/70% rules), multi-loan financing, exit strategy comparison |
| 7 | `deep-prospecting.skill` | Deal Analysis | 9.6 | 4-level research depth (L1-L4), heir verification loop, DOD sanity check (3yr), 3-site skip trace waterfall |
| 8 | `probate-property-finder.skill` | Deal Analysis | 9.7 | Property lookup for probate decedents, 3-tier search (Tax API→Executor→People search), confidence scoring |
| 9 | `phone-validator.skill` | Operations | 9.8 | Trestle API scoring, 5-tier dial priority, 3 tier strategies, litigator risk check, 4.75x connect rate |
| 10 | `sequential-presets.skill` | Operations | 9.5 | 12 niche + 9 bulk filter presets, Pendulum Theory (SMS→Call→Mail→DP), DataSift UI implementation steps |
| 11 | `sift-sequences.skill` | CRM | 9.5 | 26 TCA sequence templates (verified against `sequence_templates.py`), UI walkthrough, HOT A01-A16 chains |
| 12 | `sift-operations.plugin` | CRM | 9.3 | CRM operations encyclopedia, STABM routine, lead pipeline (9 statuses), task presets, team roles |
| 13 | `playbook-creator.skill` | Operations | 9.5 | Playbook/SOP generator from transcripts, 7-node chart limit, 5th grade reading level, Word doc output |

### Cross-Skill Verified Consistency

These values are identical across all skills that reference them:
- **Phone tiers:** 81-100 (Dial First), 61-80 (Dial Second), 41-60 (Dial Third), 21-40 (Dial Fourth), 0-20 (Drop)
- **Litigator risk:** Trestle's `litigator_checks` add-on is requested on every scoring call (default ON, `--no-litigator` disables). A flagged number gets `DO NOT CALL - Litigator Risk` **INSTEAD OF** its dial-tier tag, never beside it — a "Dial First" tag next to a litigator flag would still let a tier-filtered call list pull the number, which defeats the point. Applied in both `skip_trace_agent.writeback()` and `phone_validator.write_datasift_tags_csv()`.
- **Preset folders:** "00 Niche Sequential Marketing" (12 presets), "01. Bulk Sequential Marketing" (9 presets)
- **Sequence count:** 26 TCA templates across 5 folders (Lead Management 6, Acquisitions 6, Transactions 6, Deep Prospecting 4, Default 4)
- **Comp adjustments:** Bedroom $5,000, Bathroom $7,500, $/sqft $85, Age $500/yr (from `comp_analyzer.py`)
- **Financing defaults:** HML 12%, conventional 7%, 2 points, 2.5% closing (from `deal_analyzer.py`)
- **DOD sanity:** MAX_DOD_GAP_YEARS = 3 (from `obituary_enricher.py`)
- **Notice types:** 7 total (foreclosure, tax_sale, tax_delinquent, probate, eviction, code_violation, divorce)

### Key Corrections Made During Optimization (April 2026)
- **Hardcoded credentials removed** from sift-market-research (had email/password in SKILL.md)
- **Bedroom adjustment corrected** from $10K to $5K in real-estate-comping (matched to `comp_analyzer.py`)
- **HML points corrected** from 0% to 2% in deal-analyzer (matched to `deal_analyzer.py DEFAULT_HARD_MONEY_POINTS`)
- **Linux paths fixed** in sequential-presets (was `/home/ubuntu/skills/...`, now relative)
- **Preset names aligned** across 3 skills to match `niche_sequential.py` source code
- **Transfer tax labeled** as Tennessee-specific in deal-analyzer with state reference table for top 10 states
- **"Substantial renovation" defined** in real-estate-comping: kitchen + 1 bath minimum (~$15K spend)

### Skill File Structure
```
skill-name.skill (ZIP containing):
├── SKILL.md              # Main skill instructions
├── references/            # Domain knowledge files
│   ├── *.md              # Reference documents
│   └── *.pdf             # SOPs, guides
└── scripts/              # Optional automation scripts
    └── *.py / *.js

plugin-name.plugin (ZIP containing):
├── .claude-plugin/
│   └── plugin.json       # Plugin manifest
├── commands/             # Slash commands
│   └── *.md
├── skills/
│   └── skill-name/
│       ├── SKILL.md
│       └── references/
└── README.md
```
