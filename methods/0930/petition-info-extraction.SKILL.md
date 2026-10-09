---
name: petition-info-extraction
description: Extract key figures from Oklahoma real estate foreclosure petition PDFs (owner, property address, mortgage date, loan amounts, payment and filing dates) and log them as rows in a tracking spreadsheet. Trigger whenever the user uploads one or more foreclosure petition / mortgage foreclosure PDFs and asks to pull info, extract data, or add them to a spreadsheet or tracker — including when they hand over a whole batch of PDFs at once and say something like "process these" or "add these to my sheet." Works for a single PDF or dozens in one request. **Also trigger on a standing run with no files attached** — "do the daily foreclosure run", "run the foreclosures", "process the new petitions", "any new foreclosures?" — in which case the PDFs are already sitting in the user's foreclosure PDFs folder on their Desktop (they are SCANNED, so OCR them; there is no text layer). The batch then auto-chains into `skip-trace --create`, which is a DRY RUN until `--commit`.
---

# Petition Info Extraction

Foreclosure petitions filed in Oklahoma district courts follow a consistent structure: a
numbered "Petition for Foreclosure of Mortgage" (page 1 onward) followed by exhibits — the
promissory Note (Exhibit A), the Mortgage (Exhibit B), and sometimes an Assignment of Mortgage
(Exhibit C). The petition body states the same core facts in prose, which is usually faster and
more reliable to pull from than digging through the exhibits.

## What to extract, per PDF

For every PDF in the batch, extract these fields:

| Field | Where to find it |
|---|---|
| First Name / Last Name | The individual defendant/borrower's name in the case caption and petition paragraph 3 ("the Defendant, [NAME], for good and valuable consideration, made, executed and delivered..."). If more than one person is named as borrower (e.g. spouses), use only the first-named individual — don't create duplicate rows or combine names. |
| Property Street | From the legal description block ("commonly known as [ADDRESS]") — usually paragraph 4. |
| Property City / State / Zip | Same address block. |
| Date of Mortgage/Note | The date in petition paragraph 3 ("On or about [DATE], the Defendant... made, executed and delivered... a certain promissory note"). This also appears as the Note's date line in Exhibit A if you need to cross-check. |
| Original Loan Amount | The principal sum in that same paragraph 3 sentence ("promising... to pay... the sum of $X"). |
| Unpaid Principal Balance | Later in the petition, usually phrased "there is now due on said Note and Mortgage the principal sum of $X with accrued interest thereon, plus interest accruing at the rate of Y% per annum from [DATE]." Take the dollar figure here, not the original loan amount. |
| Interest Rate | The annual rate stated alongside the principal sum above (also stated in paragraph 3). |
| Date of Last Payment | The petition states a missed-payment date, e.g. "said payment was due... on [DATE], which said payment has not been made." That date is the *first missed* payment, not the last paid one. The last payment actually made was for the month before — subtract one calendar month from the missed-payment date. (Example: if the petition says the payment due October 1, 2025 was not made, the last payment was for September 1, 2025.) The "interest accruing... from [DATE]" language elsewhere in the petition should match this same month-before date — use it to confirm you have the right month. |
| Date Foreclosure Filed | The court's file-stamp on page 1 ("DISTRICT COURT FILED," with a date stamped near the case number, e.g. "AUG 12 2026"). This is different from the notarization/verification date near the attorney signature later in the document — use the court clerk's filing stamp, not the verification date. |

### Also extract these (added 2026-08-21)

The fields above are the loan figures alone, which leave out most of what actually drives a
decision on a foreclosure lead. Pull these too:

| Field | Where to find it |
|---|---|
| Legal Description | The "to-wit:" block in paragraph 4 — lot, block, addition, county. Copy it verbatim; it is how the parcel is identified when the street address is missing or disputed, which happens (one petition gave the legal description in Glenpool while the Mortgage exhibit's address line read Mounds). |
| Plat Number | Same block ("according to the Recorded Plat No. X"). |
| Case Number | From the caption near the file-stamp. |
| Court County | The district court county named in the caption. |
| Plaintiff | The lender/servicer bringing the action, from the caption. |
| Co-Defendants | Every other named defendant besides the borrower and the generic "Spouse of"/"Occupants" entries. **Never include the assigned judge.** Tulsa County stamps the judge's name in the caption, usually right after "Defendants." or beside the last defendant and just above "PETITION FOR FORECLOSURE" (e.g. "TRACY L. PRIDDY", Civil Docket A). It is not a party: it appears on every case on that docket, so a name that repeats across unrelated petitions in that spot is the judge. Only names in the defendant list itself count. |
| Original Lender | The payee the note was delivered to in paragraph 3 — often NOT the plaintiff, because the loan was assigned. |
| Initial Interest Rate | The rate in paragraph 3 at origination, distinct from the current rate in the "there is now due" paragraph. Record both; a large gap between them is itself a signal. |
| Original Monthly Payment | The "payable in monthly installments of $X" figure in paragraph 3. |
| Mortgage Recorded Date / Mortgage Document Number | "recorded on [DATE] as Document No. [N]" in paragraph 4/5. |
| Date of Default | The first missed payment date as stated. Distinct from Date of Last Payment, which is the month before it. |
| Loan Modification Count / Loan Modification History | Petitions list every recorded modification with date and document number. Count them, and record as `MM/DD/YYYY Doc N; ...`. **A high count is a strong signal** — a borrower with 7 modifications is modification-exhausted and unlikely to be rescued by another workout. |
| Junior Lienholders | Named defendants that are institutions rather than people — HUD, the IRS, banks, judgment creditors. These are debts beyond the first mortgage and bear directly on whether any equity exists. |
| Owner Status | "Living" unless the petition names an estate, heirs, an administrator, or a deceased party. A borrower described as the "present record owner" who is "personally obligated" is living. Do not infer death from absence of information — state what the petition actually names. |

### Co-borrower (added 2026-09-24)

`Co-Borrower First Name` / `Co-Borrower Last Name` / `Co-Borrower Relationship`. The pipeline
skip traces this second person at the same address, because a voluntary sale needs their
signature too. Fill them **only when the petition clearly calls a second named person the
borrower's spouse or co-borrower**. Relationship: `Wife` or `Husband` when the wording is gendered
("John Smith and Jane Smith, husband and wife"); otherwise leave Relationship blank rather than
guessing (phone tags are append-only). Homestead-only defendants, ex-spouses and unrelated
title-curative parties do NOT qualify.

### Owner Alive (spend gate)

`Owner Alive` = `No` only when the owner is deceased AND the petition names **no living person at
all** (only "Unknown Heirs", "Unknown Spouse", "Unknown Occupants"). Such a record is created and
enriched but never skip traced. A named co-personal representative, transfer-on-death beneficiary,
surviving joint tenant or named heir counts as living: put that person in First/Last Name and mark
`Yes`. Otherwise `Yes`.

**If a field genuinely isn't stated in the petition, leave that cell blank rather than guessing or
skipping the whole row.** A partially-filled row is more useful than a missing one. This applies
with particular force to the loan figures: a blank is recoverable, a wrong balance is not.

Case Number and Court County are now extracted — this reverses the earlier exclusion. Still do
**not** add a record/docket link column.

These columns flow into the DataSift record's Notes and Message Board through
`_format_petition_notes()` in `src/datasift_formatter.py`, which groups them into CASE /
PROPERTY / LOAN / MODIFICATIONS / OWNER / LIENS sections and appends a payoff CAUTION when the
unpaid balance exceeds the original loan. **If you add a field here, add it to
`_PETITION_SECTIONS` there too, or it will be extracted and then silently dropped.**

## Output spreadsheet

Write one row per PDF to an Excel file with exactly these columns, in this order:

`Property Street | Property City | Property State | Property Zip | First Name | Last Name | Date of Mortgage/Note | Original Loan Amount | Unpaid Principal Balance | Interest Rate | Date of Last Payment | Date Foreclosure Filed | Case Number | Court County | Plaintiff | Co-Defendants | Legal Description | Plat Number | Original Lender | Initial Interest Rate | Original Monthly Payment | Mortgage Recorded Date | Mortgage Document Number | Date of Default | Loan Modification Count | Loan Modification History | Junior Lienholders | Owner Status | Co-Borrower First Name | Co-Borrower Last Name | Co-Borrower Relationship | Owner Alive`

The first twelve columns are the original set and their order is fixed — the downstream
`skip-trace --create` pipeline reads them positionally in places. Append new columns after
them, never in the middle.

Formatting conventions:
- Header row: bold white text on a dark blue fill, centered.
- Zip codes as text (so leading behavior/format is preserved), not numbers.
- Dollar fields formatted as currency ($#,##0.00) — this covers Original Loan Amount, Unpaid
  Principal Balance, and Original Monthly Payment.
- Interest rate stored as a fraction with a 0.000% number format (e.g. 0.07125, not 7.125).
  Applies to both Interest Rate and Initial Interest Rate.
- Dates as real date values with an mm/dd/yyyy number format, not plain text strings. Applies to
  Date of Mortgage/Note, Date of Last Payment, Date Foreclosure Filed, Mortgage Recorded Date,
  and Date of Default.
- Loan Modification Count as a number; Loan Modification History, Co-Defendants, and Junior
  Lienholders as text, semicolon-separated where there are several.
- Use a plain, professional font (e.g. Calibri 11) for data rows.

**Always create a fresh file scoped to this batch only — do not append to a growing tracker.**
(Changed 2026-08-17: this used to append to a persistent "Foreclosure Records.xlsx"-style
tracking file, but that made it unsafe to auto-chain into the skip-trace pipeline
below — every run would re-process every PDF ever extracted, not just the new ones. A
fresh-per-batch file sidesteps that entirely, and also sidesteps the write-once connected-folder
overwrite problem that used to force incremented filenames like "Foreclosure Records 2.xlsx".)

Save the batch file to the SiftStack project's `output/` folder (e.g.
`output/petition_batch.xlsx`, overwritten each run — it's a local, non-synced folder so there's
no write-once/overwrite issue). If the user explicitly asks for a separate standing record of
everything ever extracted, that's a different, explicit ask — don't default to it.

## Batch processing

Read and extract from every PDF given in the request before touching the spreadsheet — build
the full list of rows first, then open the spreadsheet once and write all rows in a single pass.
This is both faster and avoids partially-written files if something goes wrong midway.

After saving, briefly summarize what was extracted (how many records, and call out any PDFs
where a field came back blank) — not a full recitation of every value — then immediately
continue to the next section in the same response.

## Immediately continue to DataSift upload

This skill exists to feed the `skip-trace --create` pipeline (`src/main.py`) — the two are
meant to run as one action from the user's side, not two separate commands. As soon as the batch
spreadsheet is saved, without waiting for a separate instruction, run:

```
python src/main.py skip-trace --csv-path "output/petition_batch.xlsx" --create --notice-type foreclosure --county Tulsa
```

**That command is a DRY RUN.** It creates the records, writes the petition
Notes/Message Board, and enriches over the API (all unmetered), then prints
exactly what it would trace, score and tag, plus the total spend -- and bills
nothing. **Report that estimate and ask before adding `--commit`**, which is
what actually spends money (Tracerfy ~$0.02/record, DataSift ~$0.12/owner,
Trestle ~$0.015/number).

`skip-and-score-upload` was DELETED 2026-08-26 -- do not use it, and do not
hand-assemble its steps. `skip-trace` is the only pipeline.

(Oklahoma foreclosure petitions are this skill's whole scope, and Tulsa is the user's market —
these are the correct defaults, not a guess to double-check.) Report the combined result — records
extracted, then what happened in the upload/skip-trace/scoring pipeline — as one outcome, not two
separate check-ins.
