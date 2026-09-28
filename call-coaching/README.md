# Call Coaching (Tulsa Homebuyers)

> **Status: built, PAUSED 2026-09-28.** Revisit when a caller is hired. Nothing
> here spends money until `transcribe.py --commit`. To restart: add the
> OpenRouter key to `.env`, add the new caller to `callers.json`, then run the
> steps below.

Scores the team's recorded smrtPhone calls against three rubrics and turns them
into coaching: one report per call, one scorecard per caller, one Excel
workbook. Built per person, so a new hire is one line in `callers.json`.

The rubric a call gets (cold call, lead management, closing, or not
gradeable) is decided by **what is said on the call**, not by who made it.

## Where the calls come from (verified 2026-09-28)

smrtPhone logs every call to the property's DataSift activity. Each call
carries the call SID, the caller, the duration and a direct recording link
(`rec.smrtphone.io/...mp3`), which downloads with no login. So the pipeline
reads DataSift, then downloads the MP3. No browser login is involved. The
smrtPhone API (`getRecordingUrl`) is used only as a backup when a call has a
SID but no recording link.

## Setup

1. Copy `.env.example` to `.env` and fill in `OPENROUTER_API_KEY` (and
   `SMRTPHONE_API_TOKEN` for the backup). `DATASIFT_API_KEY` is read from
   `SiftStack\.env`.
2. Put each dialer in `callers.json` under the name DataSift shows on their
   calls. **Names only**: this repo is public.

## Run it (from the SiftStack folder)

```
set PYTHONIOENCODING=utf-8
py -3 call-coaching/pull_calls.py          # free: list calls since 2026-09-28 (--days 7 for a week)
py -3 call-coaching/download.py            # free: download recordings
py -3 call-coaching/transcribe.py          # free: prints the cost estimate only
py -3 call-coaching/transcribe.py --commit # PAID: ~$0.003 per audio minute
py -3 call-coaching/grade.py queue         # which calls need a report
    (Claude grades each call in a Claude Code session, following GRADING.md)
py -3 call-coaching/grade.py check         # rejects any report that breaks the rules
py -3 call-coaching/export.py              # workbook + per-caller scorecards
```

Calls under 10 seconds are never sent for transcription. They go to the
Connection Log.

## Files

| File | What it is |
|---|---|
| `rubrics/cold_call.md`, `lead_management.md`, `closing.md` | Tulsa Homebuyers rubrics (each lists its changes at the top) |
| `rubrics/1-..., 2-..., 3-...` | DataSift originals, unchanged |
| `GRADING.md` | Rules the grader follows: word-for-word quotes, evidence on every criterion, N/A limits, math |
| `rubric_spec.py` | Criteria, weights and N/A rules the checker enforces. Change it together with a rubric |
| `tests/test_grading.py` | Offline checks on the checker, math and tie tiers |

## Output (gitignored, contains seller info)

```
output/calls.json                 every call found, and where it is in the pipeline
output/recordings/<SID>.mp3
output/transcripts/<SID>.md       transcript with delivery notes + how it was sorted
output/reports/<rubric>/<date>_<caller>_<SID>.md   per-call report
output/scorecards/<caller>.md     per-caller scorecard
output/Call_Coaching_<from>_to_<to>.xlsx
```

Totals within 5 points are treated as equal everywhere. Short "no" calls get
their own report and are never ranked with full calls.

Nothing here posts to Slack. If it ever does, the posts are labelled
"🎧 Call Coaching" to keep them apart from the KPI bot.
