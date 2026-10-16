---
name: tulsa-buyer-finder
description: Find and rank the business (LLC/Inc/Corp) cash buyers of single-family homes in Jeff's Tulsa market from SiftMap deed data, READ-ONLY, without touching the DataSift account. Two modes - a market-wide ranked buyer list (Tulsa County by default, any Tulsa-metro county on request, refreshed about monthly) and a per-deal shortlist ("who buys near 1234 E 56th St at $140k") filtered from the saved list in seconds. Delivers a ranked list in chat, a CSV and a PDF report. Trigger for "find buyers for my deal at ...", "who's buying in 74112", "who would buy this house", "refresh my buyer list", "build my cash buyer list", "wholesale buyers in Tulsa", "LLC buyers near ...". Next steps (finding the person behind each LLC, skip tracing) are listed but every paid step is asked first.
---

# Tulsa Buyer Finder (read-only)

Built 2026-10-16 from the Park Hill (74451) run, which found 24 ranked business buyers from 241 read-only
lookups with zero account changes. Jeff's market is single-family homes in the Tulsa metro; he rarely
does mobile homes and almost never works outside the metro.

## Hard rules (these are why Jeff trusts this)

1. **SiftMap only.** Never use DataSift "Sold Properties", never page through CRM records, never read the
   account. Jeff's standing rule: no account-wide reads without a yes.
2. **`scripts/buyer_sweep.py` is the only file that touches the network**, and it has exactly two calls:
   POST `map.reisift.io/properties/search/` and GET `/properties/detail/<id>/`, Api-Key only. Before
   EVERY run, search the file (a text search, not a run) for
   `datasift_api|add-properties|/filters/|PATCH|PUT|DELETE|apiv2|/api/internal` and confirm zero hits.
   If anyone edited it and a hit appears, stop and tell Jeff.
3. Never "Add Records to Account", never save a SiftMap preset, never tag, never create anything.
4. **Controls before trusting any number:** `probe` runs, per county, an unfiltered count, the window
   count (must be smaller) and a made-up filter key (must EQUAL unfiltered, proving SiftMap silently
   ignores unknown keys and the real filter applied). Any FAIL = stop.
5. **Stop and report before the detail lookups.** `sweep` prints how many lookups are needed and the
   time. Tell Jeff the numbers and wait for his go.
6. Count closings, not parcels (a 3-parcel closing is one purchase). Read every sale in the window,
   not just the latest.
7. Ranking is an ORDER. Never show Jeff a score (he said so).
8. Never write a regex through a bash heredoc: `\b` lands as a backspace byte and silently matches
   nothing. Use the Write/Edit tool.
9. Say what was not checked: SiftMap lags the county by weeks, $0 = unrecorded, nothing hand-checked
   against the Tulsa County Assessor unless it was.

Python: `C:\Users\dsong\OneDrive\Desktop\SiftStack\.venv\Scripts\python.exe -I`. The API key comes
from that repo's `.env` (`DATASIFT_API_KEY`). Output goes to
`C:\Users\dsong\OneDrive\Desktop\SiftStack\output\buyer_finder\<date>\` (gitignored; contains names).

## Mode 1: market list

Ask which counties (default `Tulsa`; known: Tulsa, Wagoner, Rogers, Creek, Osage, Okmulgee, Pawnee).

```
S=C:/Users/dsong/.claude/skills/tulsa-buyer-finder/scripts
PY="C:/Users/dsong/OneDrive/Desktop/SiftStack/.venv/Scripts/python.exe -I"
$PY $S/buyer_sweep.py probe --counties Tulsa              # controls; must print CONTROLS PASSED
$PY $S/buyer_sweep.py sweep --counties Tulsa              # STOP: report counts, wait for go
$PY $S/buyer_sweep.py hydrate --out <dir from sweep>      # read-only lookups, resumable
$PY $S/analyze.py --out <dir>                             # offline ranking + CSV
$PY $S/report.py  --out <dir>                             # offline PDF
```

What it pulls: single-family homes whose last sale was in the last 365 days (`--days`), gift/family
deeds excluded, plus a second pass for flip exits (a company selling a house it flipped; the company
is the SELLER there, so the current-owner flag misses it). `hydrate --scope investor` (default) only
looks up company-owned properties and flip exits; `--scope all` does every sale.

## Mode 2: per deal

Use the newest folder under `output/buyer_finder/` that covers the deal's county. If none exists or it
is over ~45 days old, say so and offer either a fresh market sweep or a quick sweep of just the deal's
ZIPs (`sweep --counties Tulsa --zips 74112,74135`, then hydrate).

```
$PY $S/analyze.py --out <dir> --deal-zips 74112,74135 --deal-price 140000
$PY $S/report.py  --out <dir> --deal-zips 74112,74135 --deal-price 140000 --deal-label "1234 E 56th St"
```

Price fit is asymmetric: a buyer whose high is $120k cannot close $300k (35% tolerance above), but a
buyer whose low is $140k can still afford $75k (floor divided by 4). Ask Jeff whether to add
neighbouring ZIPs.

## What to tell Jeff

Lead with the top 5 to 10 in order, each with why (purchases, flips sold, cash, sheriff-sale buys,
price range, ZIPs, mailing address). Then:
- companies grouped as one operator (shared mailing address),
- seller-only flippers, kept separate and labelled,
- the set-aside list (national builders/iBuyers/rental funds, government, lenders, family trusts),
- what was not checked.
Give the PDF and CSV paths in full. The PDF uses only standard fonts, so it opens anywhere, but it names
private sellers; offer a trimmed version if it is going to an outside buyer.

## Next steps (only when Jeff asks; see references/next-steps.md)

Finding the human behind each LLC, then contact info. Free steps first, every paid step asked first.
