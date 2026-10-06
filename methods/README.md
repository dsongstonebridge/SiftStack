# Foreclosure run methods: 9:29 (old, proven) and 9:30 (new, on trial)

The user's rule: the new method is only ever used if it is better AND faster. The 9:29 method
worked, and one command must always get back to it.

**The command:** "go back to the 9:29 method" (or "use the old method").
When it is given, run `python methods/restore_0929.py`, then do the run exactly as the 9:29
method below describes. Do not argue for the 9:30 method or suggest keeping part of it.

**The 9:29 code is also in git**, tagged `method-0929` (commit 2a886e0): the whole pipeline as it
ran through 2026-09-29, plus the 9:29 skill in `methods/0929/`. See exactly what changed since
with `git diff method-0929`. Get one file back with `git checkout method-0929 -- <path>`. Only
roll code back if the user asks; `restore_0929.py` (the skill) is the normal rollback.

## 9:29 method (proven, as of 2026-09-29)

1. OCR the scanned PDFs in `Desktop\Foreclosure pdfs` with `image_utils.ocr_page` (300dpi, psm 4
   for the body; a 400-600dpi page-1 crop for the stamped case number and file date).
2. Claude reads each petition directly, following the `petition-info-extraction` skill, and
   writes `output/petition_batch.xlsx`.
3. `python src/main.py skip-trace --csv-path "output/petition_batch.xlsx" --create
   --notice-type foreclosure --county Tulsa` (a dry run). Report the estimate and ask.
4. The billed step re-runs the SAME command with `--commit` added, after the user says go
   (2026-10-05: `src/created_rows.py` skips rows the dry run already created, so notes and
   Message Boards are not re-posted). Batches created before 2026-10-05 are not in that
   ledger and still run trace-only from `output/datasift_ready_foreclosure_<date>.csv`.

## 9:30 method (new, on trial from 2026-09-30)

Steps 3 and 4 are IDENTICAL to 9:29. Only steps 1 and 2 change:

1. `python src/batch_ocr.py`: the same OCR settings, run on 8 cores, every page, text cached to
   `output/petition_ocr/`, plus a summary flagging cases already processed.
2. The `petition-extractor` agent (`.claude/agents/petition-extractor.md`, file-only, Sonnet) reads
   the cached text and writes one JSON per petition to `output/petition_rows/`.
   `python src/merge_petition_rows.py` checks every value's type, checks each quoted piece of
   evidence against the OCR text, and writes `output/petition_batch.xlsx`. Claude hand-reads only
   the rows it flags.

The one shared file 9:30 changed is the `petition-info-extraction` skill: co-borrower and Owner
Alive rules were added (they already existed in CLAUDE.md and the pipeline; the skill never
documented them). `restore_0929.py` puts the 9:29 copy of the skill back.

### Status (2026-09-30)

- **batch_ocr.py: ADOPTED.** The user confirmed it worked (34 PDFs, 735 pages, 14.4 min, the 3
  repeats caught). Its summary's case-number read is only a first screen; stamps OCR messily.
- **petition-extractor agent: PINNED (not in use).** Failed the 3-known-case test on 2026-09-30:
  ignored its updated rules, silently picked between conflicting figures, and would have changed
  Message Board text on 2 of 3 cases. Retest in a fresh session before any use.
- So the current run is: batch_ocr.py, then Claude reads each petition from the cached text the
  9:29 way, then steps 3 and 4 unchanged.

## What 9:30 did NOT change

`src/main.py`, `datasift_formatter.py` (the Message Board layout), skip trace, scoring and
tagging. The 9:30 tools stop before anything touches the CRM.

## One safety fix made on 2026-09-30 (applies to BOTH methods)

This is not part of 9:30: it closes a gap the 9:29 run already had.
`upload_to_datasift()` now re-reads every record's Message Board after enrichment and re-posts
the same petition text only where it is missing (`_ensure_boards_after_enrich`).
- The gap: 3 of 20 boards on the 2026-09-30 batch had lost their petition post. Those were the
  records where owner replacement swapped the owner.
- The post's text and layout are unchanged. A post that is already there is never duplicated.
- To remove the fix, check out `src/datasift_uploader.py` and `src/datasift_api.py` from
  `method-0929`.
