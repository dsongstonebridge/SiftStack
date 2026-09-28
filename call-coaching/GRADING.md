# Grading calls: rules for the grader

Grading is done by Claude inside a Claude Code session, reading each transcript
against its rubric. No extra API cost. `grade.py check` rejects any report that
breaks a mechanical rule. The judgment rules below are the grader's to keep.

## Steps

1. `py -3 call-coaching/grade.py queue`: lists each call that needs a report,
   with its rubric, its transcript, and the exact path the report must be
   written to. The rubric is picked from what was said on the call, not from
   who dialed.
2. For each call, read the WHOLE rubric file (`rubrics/cold_call.md`,
   `lead_management.md` or `closing.md`, including its "Tulsa Homebuyers
   changes" section) and the WHOLE transcript, then write the report.
3. `py -3 call-coaching/grade.py check`: fix every failure and re-run until
   all pass. Only passing reports reach the workbook and scorecards.
4. `py -3 call-coaching/export.py`: workbook and per-caller scorecards.

If the transcript shows the call was sorted wrong (for example it was a
follow-up, not a first touch), say so to the user and ask before grading it on
a different rubric. Do not silently re-route.

## Hard rules

- **Word for word.** Every quote from the call must appear in the transcript
  exactly. Copy, do not paraphrase. Delivery notes in brackets and speaker
  labels may be left out; "..." may join two exact pieces of the same passage.
  The checker verifies this.
- **Every criterion cites evidence.** Each criterion row ends in an Evidence
  cell holding a quote from the call, or `ABSENT: <what was never said>`, or
  `N/A: <why the situation never arose>`.
- **N/A only where the rubric allows it**, each with a reason in the JSON
  `na_reasons`, and the report states the weight redistribution:
  `N/A criteria and weight redistribution: <list, or "none">`.
  - Cold call: a criterion the seller made unreachable, or whose signal the
    transcript does not preserve.
  - Lead management: only 1.4, 1.5 and 5.4, unless the owner/decision-maker
    was disconfirmed early (`"non_decision_maker": true`), which also allows
    categories 2, 3 and 4 to be N/A.
  - Closing: only when the situation never arose. **2.6 is never N/A.**
- **The math must add up.** Category = average of its scored criteria
  (2 decimals). Total = sum of (category / 5 x weight), divided by the sum
  of the weights of the categories that have a score, x 100 (1 decimal).
  Band from the total (90 Elite, 75 Strong, 60 Developing, 40 Needs Work,
  below 40 Retrain); an auto-fail makes the band `FAIL`. The tables and the
  JSON must agree.
- **Grade only what is audible on the call.** Never score from DataSift
  data, notes, dispositions, tags or anything else off the call. The header
  details (caller, date, address) are only for labelling.
- **Tonality** only from the transcript and its bracketed delivery notes and
  DELIVERY SUMMARY. No guessing at what the voice "probably" sounded like.
- **Short calls are their own report** (cold call and lead management only):
  the seller declined within about 30 seconds with nothing beyond the
  decline. Use the rubric's short-call format, `total` and `band` null. Never
  ranked with full calls. When in doubt, grade it as a full call.
- **Totals within 5 points are equal.** Never call one caller or call better
  than another over a gap of 5 or less; the scorecards show such totals as
  tied.
- **Closing: Oklahoma disclosures.** On an agreement-stage call, if any of
  the assign/resell notice, the see-an-attorney recommendation, or the
  2-business-day right to cancel is not heard, 4.2 is at most 2 and the
  report says `OK disclosure missed`. Not an auto-fail. Coaching, not legal
  advice.

## Report format

Use the rubric's own Coaching Output Format (full or short), with two changes
so the checker can read it:

1. The criterion table is always three columns, one row per criterion in
   rubric order, the id first:

   ```
   | Criterion | Score | Evidence |
   |---|---|---|
   | 2.1 Motivation asked soft, then mined | 3 | "why are you thinking about selling" asked once; first answer "just getting older" accepted with no follow-up |
   | 3.1 How did you get my info | N/A | N/A: seller never asked |
   ```
   Short reports use the same three columns for their OPENER rows and add a
   `| 3.3 Converting the no | 2 | ... |` row (lead management: `4.5`).
2. Always include the line `N/A criteria and weight redistribution: ...`
   on full reports.

End every report with the SCORES JSON block from the rubric, plus these keys:

```json
{"call_id": "CA...", "pipeline": "cold_call", "caller": "Diego",
 "call_type": "full", "duration_seconds": 95, "outcome": "callback",
 "auto_fail": "PASS", "total": 62.4, "band": "Developing",
 "categories": {"1": 3.5, "2": 3.0, "3": 3.33, "4": 2.8, "5": 3.0},
 "criteria": {"1.1": 4, "...": 3, "3.1": null},
 "na_reasons": {"3.1": "seller never asked how we got the number"},
 "recording_url": "https://rec.smrtphone.io/..."}
```

`pipeline` is `cold_call`, `lead_management` or `closing`. Closing also needs
`ok_disclosure_missed` (null when no agreement stage was reached) and
`ok_disclosure_missing`. Lead management may add `"non_decision_maker": true`.
Short reports add `opener`, `conversion`, `conversion_attempted` per the rubric.

Write the report to the exact path `grade.py queue` gives. The file name ends
in the call id, which is how the checker links a report to its transcript.
