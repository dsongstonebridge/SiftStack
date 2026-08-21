# Keys & Tokens

Three credentials are needed — **each team member uses their own.** Keep real keys out of anything shared.

## 1. DirectSkip API key
- File: `config/directskip.config.json` (copy `directskip.config.json.example` → fill in).
- Format: `{"directskip_api_key": "..."}`
- `directskip_trace.py` reads it automatically (or pass `--api-key`, or set `DIRECTSKIP_API_KEY`).
- Requirements: the requesting IP must be **whitelisted** with DirectSkip, and a card on file. No card → `status.error = "You have no authorized credit card"`. Bills ~$0.10 per result.

## 2. TrestleIQ (phone validator) key
- Lives in the **phone-validator skill's** `config.json` (`{"trestle_api_key": "..."}`) — the agent calls that skill's `validate_phones.py`.
- Bills ~$0.015 per call. **Watch the balance** — a mid-batch credit-out returns HTTP 403 (surfaces as "Invalid API key") and halts validation. Top up (auto-refill is on) and resume the remaining chunks.

## 3. DataSift / REISift API key (CRM)  ← replaces the old 48h JWT
- **This is now a static Open API key, not a Bearer token — no more 48-hour refresh.**
- Auth header the scripts send: `Authorization: Api-Key <your key>`.
- Provide it either way (env wins):
  - env `REISIFT_API_KEY="<your key>"`, or
  - file `reisift_apikey.json` → `{"api_key": "<your key>"}` (scripts search from the working dir up, or set `REISIFT_APIKEY_JSON=/path/to/reisift_apikey.json`).
- **Each teammate generates their OWN key** in REISift → account → integration settings (plans above Professional). The key acts as that user and inherits their permissions — so authorship on posts/tags is correct per person.
- Treat it like a password; rotate/revoke from the same screen if exposed.
- If a CRM call returns 401/403, the key is missing, wrong, or lacks Open-API access — not an expiry.

## Working directory
Set `SKIPTRACE_RUN_DIR=<a writable folder>` for all intermediate files (parse, responses, resolve, tiers, merged plan, write log). If unset, defaults to `./.skiptrace_run` in the current working directory.

## Quick start for a teammate (only has the plugin)
```
export REISIFT_API_KEY="<your DataSift Open API key>"      # from REISift → integration settings
export DIRECTSKIP_API_KEY="<the FCRE DirectSkip key>"       # from Tyler
export TRESTLE_API_KEY="<the FCRE Trestle key>"             # from Tyler
export SKIPTRACE_RUN_DIR="$HOME/skiptrace_run"
```
Then just tell the agent what to run ("process this SmartSkip export", "run the Ready to Skip (TrestleIQ) filter"). No FCRE folder, no 48h token.
