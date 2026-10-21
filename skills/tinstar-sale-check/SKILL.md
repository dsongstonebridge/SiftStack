---
name: tinstar-sale-check
description: Jeff's weekly Tulsa County sheriff-sale check. Every Monday at 9 AM (or as soon as the PC is in use after that) a Windows task reads the Sheriff's foreclosure sales list (TinStar, the next six weeks of auctions), checks each address against the DataSift CRM, posts the sale date and appraisal to the PROPERTY Message Board of every exact match (never the owner record), flags records that look like the same house written differently, and pops a notification that stays until clicked. Use when Jeff asks about "TinStar findings", "the sheriff sale check", "which of my records have a sale date", "auction dates", "sheriff sale list", wants the summary of the latest check, wants to confirm/post a flagged record, or wants to run, re-run, test or fix the weekly check.
---

# TinStar sheriff-sale check

The **auction-date-agent** (`.claude/agents/auction-date-agent.md`) does this
work and nothing else; hand TinStar requests to it when it is available.

Code lives in the FORECLOSURE worktree: `C:\Users\dsong\SiftStack-foreclosure`
(this is foreclosure work; never run it from the probate folder or the OneDrive
SMS folder). Python: `C:\Users\dsong\OneDrive\Desktop\SiftStack\.venv\Scripts\python.exe`.
Run every command below with `cd C:\Users\dsong\SiftStack-foreclosure` first.

| Command | What it does |
|---|---|
| `python src/tinstar_sale_check.py summary` | Latest findings (reads `output/tinstar/latest.json`, no network) |
| `python src/tinstar_sale_check.py run --dry` | Fetch + match now, writes nothing |
| `python src/tinstar_sale_check.py run` | Fetch + match + post to exact matches |
| `python src/tinstar_sale_check.py post --confirm 54,67` | Post to flagged findings Jeff confirmed |
| `python src/tinstar_sale_check.py post --confirm 54:<uuid>` | Same, when a flag has 2+ records |
| `python src/tinstar_sale_check.py dismiss 54` | Jeff said skip a flag: never posted, never flagged again for that sale |
| `python src/tinstar_sale_check.py notify-test` | Show the notification once |
| `python tests/test_tinstar_sale_check.py` | Offline tests, nothing real touched |

## 1. Jeff asks about the findings (the usual case)

1. Run `summary`. If `checked_at` is not from this week's Monday-or-later, say so
   and offer `run`.
2. Answer briefly, in plain words:
   - how many sales are listed and the date range, how many recalled
   - EXACT matches: address, sale date, appraisal, and that the post went up
     (or "already posted" / "POST FAILED")
   - FLAGGED: one line each with the reason (unit number, street type,
     spelling, city, two records at one address) and whether the owner's last
     name is on the case. Ask which to post. Never post a flag on your own.
   - lookup failures, if any (those addresses were NOT checked)
3. On his yes, run `post --confirm` with the numbers he picked, then read the
   result line back to him.

## 2. Rules

- **Posts go on the PROPERTY record's Message Board, NEVER the owner's.**
  Nothing is ever written to an OWNER record unless Jeff says so explicitly
  (Jeff, 2026-10-21, after the first four posts landed on owner boards where
  nobody works: "an enormous mistake"). The script uses
  `post_property_message_board`; a test fails on any owner-board call.
- **THE CRM IS READ-ONLY, property Message Board posts are the ONLY write** (Jeff,
  2026-10-21). No tags, lists, statuses, custom fields, notes, owner edits,
  creates, deletes, skip traces or enrichment, from the script or by hand in a
  session, even when a finding seems to call for it. Suggest it to Jeff in
  words instead. `tests/test_tinstar_sale_check.py` fails if the script calls
  any other datasift_api write.
- A flag Jeff says "don't bother" with gets `dismiss N`; one he says yes to
  gets `post --confirm N`.
- **Match by address only**, never by case number (not every record has one).
- **Exact match** = same normalized street (datasift_api `_norm_street`:
  "E. 66TH ST." = "E 66th St", "S 112th E Ave" = "S 112th East Ave") AND same
  city, and exactly ONE CRM record. Anything else that looks like the same
  house is a FLAG. In Tulsa "E 66th St" and "E 66th Pl", or "St N" and "St S",
  are different streets, so a format difference is never assumed harmless.
- **The post** carries the sale date (Wednesdays 10:00 AM, Tulsa County HQ,
  218 W 6th St), the TinStar appraisal (the figure on the public report), the
  case number and the check date. Plain ASCII, no dashes.
- One post per record per sale date. A moved date gets a "SHERIFF SALE DATE
  CHANGED" post; a sale we posted that later shows RECALLED gets one recall
  post. A sale already recalled when first seen is not posted.
- Every post is read back off the Message Board; a post that does not land is
  reported as POST FAILED.
- Lookups are targeted (one house-number search per sale). Never page through
  the account.
- Zero upcoming sales is a failure, not a quiet week.
- The TinStar feed (`tinstar.io/api/cases`) also carries bidder names, phones
  and emails and every case back to 2020. Keep only the public-report columns
  (`KEEP` in the script). Never store or show bidder details.

## 3. The schedule

Windows task **"TinStar sale check Mondays 9 AM"** (registered by
`tools/register_tinstar_task.ps1`). Triggers: Monday 9 AM, logon, unlock, wake,
every 15 min. No WakeToRun. It runs `tools/tinstar_check_task.ps1` through
`tools/run_hidden.vbs` (no window flash). The script is silent unless this
week's check is due (`last_cycle_done` in `output/tinstar/state.json`). A
failure retries every tick; the failure notification fires on the 3rd failed
try that week. Log: `output/tinstar/check.log`.

The notification (`tools/toast_notify.ps1`) is a "reminder" toast: it stays on
screen until Jeff clicks Open VS Code or Dismiss.

To check the task: `Get-ScheduledTask -TaskName "TinStar sale check Mondays 9 AM" | Get-ScheduledTaskInfo`.
To force a re-run this week: delete `last_cycle_done` from `state.json` (the
next 15-minute tick runs it), or just run `run` by hand.
