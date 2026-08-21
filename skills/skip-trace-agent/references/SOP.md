# SOP — Skip Trace Agent Runbook

Step-by-step to process a batch end to end. Commands assume you're running inside a Cowork session on the FCRE folder. All scripts live in `Skip Trace Agent/scripts/`. Set `SKIPTRACE_RUN_DIR` to a writable folder for all intermediate files (defaults to `./.skiptrace_run`).

> **Golden rule:** dry-run → pilot ONE record → review in DataSift → batch. Never batch blind.

---

## 0. Pre-flight — set your keys (each person uses their own DataSift key)

```
export REISIFT_API_KEY="<your DataSift Open API key>"   # CRM auth — static, no 48h refresh
export DIRECTSKIP_API_KEY="<FCRE DirectSkip key>"
export TRESTLE_API_KEY="<FCRE Trestle key>"
export SKIPTRACE_RUN_DIR="$HOME/skiptrace_run"          # writable working dir
```
1. **DataSift API key** — auth is `Authorization: Api-Key <key>`; the scripts read `REISIFT_API_KEY` (or a `reisift_apikey.json`). No token refresh. A 401/403 means the key is missing/wrong or lacks Open-API access. See `config/README.md`.
2. **DirectSkip key** — `DIRECTSKIP_API_KEY` (or `config/directskip.config.json`). Bills per result; IP must be whitelisted.
3. **TrestleIQ credits** — enough for ~$0.015 × expected unique numbers. If it runs dry mid-run you get a 403 ("Invalid API key") — top up and resume.
4. **Cost the run**: unique numbers ≈ (existing on records) + SmartSkip + DirectSkip, deduped. See `references/cost-model.md`.

---

## Mode A — from a SmartSkip export file

### 1. Parse the SmartSkip export (read-only)
Auto-detects vertical vs wide format, keeps Subject + Relatives, drops Associates, infers relationship tags.
```
python3 scripts/parse_smartskip.py "path/to/SmartSkip Export.csv" > "$SKIPTRACE_RUN_DIR/smartskip.json"
```
Sanity check: subject count, and that a few records look right (owner + grouped relatives).

### 2. Build the DirectSkip input, then run DirectSkip (bills per result)
DirectSkip wants owner name + mailing AND property address. Build the input CSV from the parse, then trace (resumable — re-run to continue):
```
# build input (name + both addresses, one row per subject)
python3 - <<'PY'
import json,csv,sys
recs=json.load(open("$SKIPTRACE_RUN_DIR/smartskip.json"))
cols=["First Name","Last Name","Mailing address","Mailing city","Mailing state","Mailing zip","Property address","Property city","Property state","Property zip"]
w=csv.DictWriter(open("$SKIPTRACE_RUN_DIR/ds_input.csv","w",newline=""),fieldnames=cols); w.writeheader()
for r in recs:
    w.writerow({"First Name":r["first"],"Last Name":r["last"],"Mailing address":r["mailing_address"],"Mailing city":r["mailing_city"],"Mailing state":r["mailing_state"],"Mailing zip":r["mailing_zip"],"Property address":r["property_address"],"Property city":r["property_city"],"Property state":r["property_state"],"Property zip":r["property_zip"]})
PY
python3 scripts/directskip_trace.py --csv "$SKIPTRACE_RUN_DIR/ds_input.csv" --out "$SKIPTRACE_RUN_DIR/directskip_responses.json"
```
Runs in the background of the tool timeout — if it says PARTIAL, just run the same line again; it skips finished records.

Now jump to **step 3 (shared)** below.

---

## Mode B — from a CRM filter (records already SmartSkipped)

Records in e.g. `1.FTM / 02. Ready to Skip (DirectSkip)`. Their SmartSkip data is already in the CRM (phones tagged `SmartSkip`, relatives on the message board). Pull the filter's records, build the DirectSkip input from the CRM record fields (owner name + mailing + property address), then run DirectSkip as in step 2.

```
# Pull records in a named preset -> ds_input.csv (owner name + addresses).
# The preset's filter spec (all_tags / any_tags / any_lists / must_not) is applied
# via POST /api/internal/property/ with x-http-method-override: GET.
# See references/decisions-log.md "Mode B" for the exact query, then run directskip_trace.py.
```
In Mode B, the SmartSkip relatives come from the CRM (message board + existing SmartSkip-tagged phones) rather than a parse file; the merge matches DirectSkip people against them. Then continue at step 3.

---

## 3. Resolve records + read existing phones (read-only)

Match each subject to its DataSift record by **property address** (exact street match; never guess), and snapshot the phones already on each record (so we can tag pre-existing bulk numbers `DataSift` and re-score them).
```
python3 scripts/resolve_records.py "$SKIPTRACE_RUN_DIR/smartskip.json"
```
Writes `resolve.json` (idx → {uuid, owner, addr}) and `existing_phones.json`. Read-only.
**Resumable** — saves every 10 records; if it prints `PARTIAL` (a big batch can exceed a
45s tool call), just run the same line again and it skips already-resolved indices.

## 4. Merge SmartSkip + DirectSkip
```
python3 scripts/merge_sources.py "$SKIPTRACE_RUN_DIR/<smartskip file or parse>" \
    "$SKIPTRACE_RUN_DIR/directskip_responses.json" "$SKIPTRACE_RUN_DIR/merged_plan.json"
```
Prints overlap stats (numbers found by BOTH sources, DirectSkip-only people → "Other Relatives"). Applies the associate negative-filter and DirectSkip self-dedupe.

## 5. TrestleIQ validation of the FULL union (bills per unique number)
Collect all unique numbers across existing + SmartSkip + DirectSkip (dedupe globally — a number's tier is the same everywhere, so validate once), split into chunks, and validate:
```
# build unique-number chunks (250 each) from merged_plan.json + existing_phones.json
# then, per chunk (resumable, skip chunks already done):
python3 scripts/validate_phones.py --input chunk.csv --output out_dir --batch-size 40 --delay 0
```
Consolidate to `$SKIPTRACE_RUN_DIR/tiers.json` (number → dial tier) and `linetypes.json` (number → MOBILE/LANDLINE).
> Run these in the FOREGROUND in small chunks. Do NOT launch a big background job — it can wedge the workspace. If credits run out you'll see 403 → top up → resume the remaining chunks.

## 6. Writeback — DRY-RUN, then PILOT, then BATCH
```
# dry-run a few (no writes) — inspect the tag arrays + post text
python3 scripts/writeback_smartskip.py --start 0 --count 3

# pilot ONE record live, then READ IT BACK in DataSift and confirm
python3 scripts/writeback_smartskip.py --record 1 --execute

# batch the rest, in chunks (idempotent + logged to run/writeback_log.jsonl)
python3 scripts/writeback_smartskip.py --start 1 --count 25 --execute
python3 scripts/writeback_smartskip.py --start 26 --count 25 --execute
# ...continue until all done
```
Per record the writeback: upserts phones (source(s)+tier+relationship, `DataSift` on pre-existing untagged, type from TrestleIQ), appends ONE combined house-style post, and adds property tags (`TrestleIQ Scored` only if scored). The upsert is **resilient**: if REISift 400s on a bogus number (bad area code), it drops exactly that number and retries so one bad number can't block a record.

Every CRM call now **self-throttles** (min interval between calls) and **retries HTTP 429/503** with exponential backoff (honoring `Retry-After`). Batches run unattended — you no longer have to pace chunks by hand or gap-fill skipped records. If you still see repeated 429s on a busy account, raise the spacing: `export REISIFT_MIN_INTERVAL=0.6`. Keep `--count` per invocation small enough to finish inside a 45s tool call (~15–20 records is comfortable); it's idempotent and logged, so overlap is harmless.

## 6b. Reconcile to best-30 (required after writeback)
REISift hard-caps an owner at 30 phones and evicts uncontrolled when you upsert more, so it can keep dead numbers and drop your Dial-First ones. This pass fixes it deterministically — keeps highest dial tiers, drops Drop/Dial-Fourth first — and backfills `TrestleIQ Scored` on any record that missed it.
```
python3 scripts/reconcile_best30.py                          # dry-run (all)
python3 scripts/reconcile_best30.py --start 0 --count 25 --execute   # chunk it
python3 scripts/reconcile_best30.py --start 25 --count 25 --execute
# ...continue; only over-cap records touch the CRM, so ranges fly
```
Idempotent (re-running a reconciled record is a no-op). `--start/--count` exist so it fits inside the 45s tool cap on big batches.

## 7. Verify + report
Sample records: phones have MOBILE/LANDLINE types (no UNKNOWN for scored numbers), correct tag arrays, exactly one combined post, property tags present. Confirm the write log covers every record (`run/writeback_log.jsonl`). Report: records processed, numbers added, both-source overlap count, zero-result count, and total cost.

---

## Performance & reliability (baked in — expect this)
- **Every long step is chunked + resumable** so it survives the ~45s per-tool-call cap in a Cowork session. Just re-run the same command to continue:
  - DirectSkip (`directskip_trace.py`) — resumable via its JSONL; re-run to finish.
  - Resolve (`resolve_records.py`) — saves every 10 records; re-run on `PARTIAL`.
  - TrestleIQ (`validate_phones.py`) — run one 250-number chunk per call; skip chunks already done.
  - Writeback / reconcile (`--start/--count`) — advance the window each call; both idempotent + logged.
- **CRM calls self-throttle and retry 429/503** with exponential backoff (honors `Retry-After`). No more hand-pacing or gap-filling. Tune with `REISIFT_MIN_INTERVAL` (default `0.35`s) and `REISIFT_MAX_RETRIES` (default `6`).
- **Background jobs don't survive** a tool call in this sandbox — run everything in the foreground, chunked. Don't `nohup &` a long job; it gets killed when the call returns.
- **Auth is a static Open API key** (`Authorization: Api-Key`), read from `reisift_auth.json` → `accounts.fcre.api_key`. No 48h JWT refresh. (The *installed* `/skip-trace-agent` plugin is an older Bearer-token build — run the scripts from this FCRE folder, not that.)

## Troubleshooting
- **CRM 401/403** → DataSift API key missing/wrong or lacks Open-API access (not an expiry). Check `REISIFT_API_KEY` / `accounts.fcre.api_key`.
- **Repeated CRM 429s** → calls already retry with backoff; if a batch still stalls, raise `REISIFT_MIN_INTERVAL` (e.g. `0.6`–`1.0`) and shrink `--count`.
- **TrestleIQ "Invalid API key" after N calls** → out of credits (it's a 403); top up, resume remaining chunks.
- **A record has no skip tags but should** → the writeback 400'd on a bogus number and skipped it in an older run; re-run that record (`--record N --execute`). The resilient upsert now prevents this.
- **Workspace unresponsive** → a runaway background job; kill python processes and run validation in small foreground chunks.
- **DirectSkip empty for everyone** → key missing/not whitelisted, or no card on file.
