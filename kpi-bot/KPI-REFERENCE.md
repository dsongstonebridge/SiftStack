# FCRE KPI Bot — KPI Reference

Complete column mappings, formulas, and edge case notes for every detailed tab in the Slack report. This is the source of truth for what the bot calculates and how.

---

## Time Periods

All detailed sections report across five time periods:
- **Today** — rows where Date == current date
- **This Week** — rows where Date >= Monday of current week
- **MTD (Month to Date)** — rows where Date >= 1st of current month
- **QTD (Quarter to Date)** — rows where Date >= 1st of current quarter (Jan 1, Apr 1, Jul 1, Oct 1)
- **YTD (Year to Date)** — rows where Date >= Jan 1 of current year

Date parsing supports formats: `MM/DD/YYYY`, `YYYY-MM-DD`, `MM/DD/YY`. Rows with unparseable or blank dates are skipped.

---

## Executive Summary

Pulls from multiple tabs to create a rolled-up funnel view. Reported as MTD / QTD / YTD only (no today/week).

### Source Tabs & Metrics

| Metric | Source Tab | Column(s) | Formula |
|---|---|---|---|
| Leads Generated | Leads Summary | `Date New Lead` | Count of rows where date falls in period |
| Leads Qualified | Leads Summary | `Date Qualified` | Count of rows where date falls in period |
| Dead Leads | Leads Summary | `Date Dead Lead` | Count of rows where date falls in period |
| Appointments | Acquisition | `Appointments` | Sum of column values where `Date` falls in period |
| Offers Made | Acquisition | `Initial Offers` | Sum of column values where `Date` falls in period |
| Offers Accepted | Acquisition | `Offer Accepted` | Sum of column values where `Date` falls in period |
| Contracts Sent | Acquisition | `Contract Sent` | Sum of column values where `Date` falls in period |
| Under Contract | Offers Summary | `Under Contract` | Count of rows where value is not blank/no/n/a (all-time, not period-filtered) |
| Projected Profit | Offers Summary | `Projected Profit`, `Offer Made` | Sum of Projected Profit where Offer Made date falls in period |
| Closed Profit | Offers Summary | `Profit`, `Date Closed` | Sum of Profit where Date Closed falls in period |
| Total Spend | Costs + Expenses | `Cost`/`EXPENSE`, `Date`/`DATE` | Sum of both tabs where date falls in period |
| Qualification Rate | Derived | | Leads Qualified / Leads Generated (shown as %) |

### Edge Cases
- Qualification rate shows "—" if zero leads generated in the period
- Dollar values are cleaned of `$` and `,` before parsing
- Under Contract is a snapshot count (not time-bucketed) — it always shows the current active count

---

## First to Market

### Actual Column Headers (from sheet)
| Col | Header | Script Key |
|---|---|---|
| A | Ninja Name | (not tracked — per-person breakdown not yet implemented) |
| B | Date | date field |
| C | Hours | `hours` |
| D | New Ps # | `new_ps` |
| E | Follow Up  Ps # | `fu_ps` (note: double space in header) |
| F | Initial Dial | `init_dial` |
| G | FU 1 Dial | `fu1_dial` |
| H | FU2 Dial | `fu2_dial` |
| I | FU 3 Dial | `fu3_dial` |
| J | SMS | `sms` |
| K | SMS Reply | `sms_reply` |
| L | Dead | `dead` |
| M | DNC | `dnc` |
| N | No Answer | `no_answer` |
| O | Voicemail | `voicemail` |
| P | Wrong | `wrong` |
| Q | Correct Initial | `correct_init` |
| R | Correct F/U 1 | `correct_fu1` |
| S | Correct F/U 2 | `correct_fu2` |
| T | Correct F/U 3 | `correct_fu3` |
| U | New Lead | `new_lead` |
| V | Not interested | `not_interested` |
| W | Full Exhausted | `full_exhausted` |

### Derived Metrics

**Computed Totals:**
- Total Prospects = New Ps # + Follow Up Ps #
- Total Dials = Initial Dial + FU 1 Dial + FU2 Dial + FU 3 Dial
- Total FU Dials = FU 1 Dial + FU2 Dial + FU 3 Dial
- Total Correct = Correct Initial + Correct F/U 1 + Correct F/U 2 + Correct F/U 3

**Dial-to-Prospect Ratios:**
- Dials per New Prospect = Initial Dial / New Ps #
- Dials per FU Prospect = Total FU Dials / Follow Up Ps #

**Correct Number Rates:**
- Contact Rate = Total Correct / Total Prospects (%)
- Initial Correct Rate = Correct Initial / Initial Dial (%)
- Dials per Correct (Initial) = Initial Dial / Correct Initial
- FU1 Correct Rate = Correct F/U 1 / FU 1 Dial (%)
- Dials per Correct (FU1) = FU 1 Dial / Correct F/U 1
- FU2 Correct Rate = Correct F/U 2 / FU2 Dial (%)
- Dials per Correct (FU2) = FU2 Dial / Correct F/U 2
- FU3 Correct Rate = Correct F/U 3 / FU 3 Dial (%)
- Dials per Correct (FU3) = FU 3 Dial / Correct F/U 3

**Disposition Rates (% of Total Dials):**
- Dead % = Dead / Total Dials
- Wrong % = Wrong / Total Dials
- No Answer % = No Answer / Total Dials
- DNC % = DNC / Total Dials

**Lead Generation Efficiency:**
- Prospects per Lead = Total Prospects / New Lead
- Correct Numbers per Lead = Total Correct / New Lead
- Dials per Lead = Total Dials / New Lead
- Not Interested % = Not interested / Total Prospects

**Exhaustion Rate:**
- Fully Exhausted % = Full Exhausted / Total Prospects
- "Fully exhausted" means every phone number for the prospect came back dead or wrong — the prospect is completely uncontactable.

### Edge Cases
- All ratios show "—" when denominator is 0
- FU2 and FU3 columns are frequently blank/0 in early data — ratios will show "—"
- The `Follow Up  Ps #` header has a double space — the script matches this exactly
- Original column mapping had `Score Ps #` and `Initial Ps #` — actual sheet uses `New Ps #` and `Initial Dial`
- `Voicemail` column exists but is not currently used in any ratio (tracked in volume only)
- Per-caller breakdown (by Ninja Name) is not implemented — all metrics are aggregated across all callers

### What's NOT Tracked (Known Gaps)
- No breakdown of follow-up prospects by attempt number (1st attempt FU vs 2nd attempt FU vs 3rd attempt FU). The sheet lumps all follow-up prospects into one `Follow Up Ps #` column. To track this, the sheet would need separate columns for FU1 Prospects, FU2 Prospects, FU3 Prospects.

---

## Lead Management

### Actual Column Headers (from sheet)
| Col | Header | Script Key |
|---|---|---|
| A | Name | (not tracked — aggregated across all reps) |
| B | Date | date field |
| C | Hours | `hours` |
| D | New Leads | `new_leads` |
| E | New Leads Qualified | `qualified` |
| F | Qualified Lead Followups | `qual_followups` |
| G | Inbound calls | `inbound_calls` |
| H | Dials Made | `dials` |
| I | SMS Sent | `sms_sent` |
| J | Voicemails | `voicemails` |
| K | SMS Inbound | `sms_inbound` |
| L | Dead # | `dead_num` |
| M | DNC # | `dnc` |
| N | No Answer | `no_answer` |
| O | Soliciters | `soliciters` |
| P | Conversations | `conversations` |
| Q | Not interested | `not_interested` |
| R | Ghosting Leads | `ghosting` |
| S | Lost Deal | `lost_deal` |
| T | Dead Leads | `dead_leads` |
| U | Sent to AQS | `sent_aqs` |

### Derived Metrics

**Computed Totals:**
- Total Leads Worked = New Leads + Qualified Lead Followups

**Dial Efficiency:**
- Dials per Lead Worked = Dials Made / Total Leads Worked
- Dials per Conversation = Dials Made / Conversations
- Dials per Sent to AQS = Dials Made / Sent to AQS

**Contact & Conversion:**
- Conversation Rate = Conversations / Dials Made (%)
- No Answer Rate = No Answer / Dials Made (%)
- Qualification Rate = New Leads Qualified / New Leads (%)

**Pipeline Throughput (→ Acquisitions):**
- Sent to AQS Rate = Sent to AQS / Total Leads Worked (%) — the core metric: what % of leads worked make it to acquisitions
- Leads per AQS Send = Total Leads Worked / Sent to AQS
- Conversations per AQS Send = Conversations / Sent to AQS

**Lead Dispositions (% of Total Leads Worked):**
- Not Interested % = Not interested / Total Leads Worked
- Ghosting % = Ghosting Leads / Total Leads Worked
- Lost Deal % = Lost Deal / Total Leads Worked
- Dead Leads % = Dead Leads / Total Leads Worked

**Phone Dispositions (% of Dials Made):**
- Dead # % = Dead # / Dials Made
- DNC % = DNC # / Dials Made

### Edge Cases
- All ratios show "—" when denominator is 0
- `Dead #` and `Dead Leads` are different things: `Dead #` is a phone disposition (the number is dead), `Dead Leads` is a lead disposition (the lead itself is dead/unworkable)
- `Soliciters` is tracked in volume but not used in a ratio — it's informational only
- Original column mapping referenced `Solicited Conversations` and `Sent to AOS` — actual sheet headers are `Conversations`, `Soliciters`, and `Sent to AQS`
- `Qualified Lead Followups` is leads that were previously qualified and are being followed up on — not the same as `New Leads Qualified` (which is new leads that got qualified this period)
- Per-rep breakdown (by Name) is not implemented — all metrics aggregated

---

## Acquisition

### Actual Column Headers (from sheet)
| Col | Header | Script Key |
|---|---|---|
| A | Name | (not tracked — aggregated across all reps) |
| B | Date | date field |
| C | Hours | `hours` |
| D | Initial Offer Leads | `init_leads` |
| E | Offer Follow-ups | `offer_fus` |
| F | Initial Offer Dials Made | `init_dials` |
| G | Offer Followup Dials | `fu_dials` |
| H | SMS Sent | `sms_sent` |
| I | Voicemails | `voicemails` |
| J | No Answer | `no_answer` |
| K | Conversations | `conversations` |
| L | Appointments | `appointments` |
| M | Send back to LM | `send_back_lm` |
| N | Initial Offers | `init_offers` |
| O | Offer Rejected | `offer_rejected` |
| P | Offer Accepted | `offer_accepted` |
| Q | Contract Sent | `contract_sent` |
| R | Offer Accepted on Follow Up | `offer_accepted_fu` |

### Derived Metrics

**Computed Totals:**
- Total Leads Worked = Initial Offer Leads + Offer Follow-ups
- Total Dials = Initial Offer Dials Made + Offer Followup Dials
- Total Accepted = Offer Accepted + Offer Accepted on Follow Up

**Dial Efficiency:**
- Dials per Initial Lead = Initial Offer Dials Made / Initial Offer Leads
- Dials per Offer Follow-up = Offer Followup Dials / Offer Follow-ups
- Dials per Conversation = Total Dials / Conversations

**Contact & Conversion:**
- Conversation Rate = Conversations / Total Dials (%)
- No Answer Rate = No Answer / Total Dials (%)
- Appointment Rate = Appointments / Conversations (%)

**Offer Pipeline:**
- Offers per Lead Worked = Initial Offers / Total Leads Worked
- Conversations per Offer = Conversations / Initial Offers
- Dials per Offer = Total Dials / Initial Offers

**Offer Outcomes:**
- Offer Accepted (initial) count + % of Initial Offers
- Offer Accepted on Follow Up count + FU Dials per FU Acceptance (Offer Followup Dials / Offer Accepted on Follow Up)
- Total Acceptance Rate = Total Accepted / Initial Offers (%)
- Offer Rejected % = Offer Rejected / Initial Offers
- Contract Rate = Contract Sent / Initial Offers (%)
- Leads per Contract = Total Leads Worked / Contract Sent

**Disposition:**
- Send Back to LM % = Send back to LM / Total Leads Worked

### Edge Cases
- All ratios show "—" when denominator is 0
- **No data currently in this tab** — all values will show 0 and all ratios will show "—" until data starts flowing
- `Offer Accepted` (Col P) is acceptance on the initial offer. `Offer Accepted on Follow Up` (Col R) is acceptance after follow-up dials. These are separate columns that get summed for Total Accepted.
- `Send back to LM` = leads sent back to Lead Management because they weren't ready for an offer
- `Initial Offer Leads` are leads coming in from Lead Management (Sent to AQS on the LM side)
- Per-rep breakdown (by Name) is not implemented — all metrics aggregated

---

## Leads Summary

### Actual Column Headers (from sheet)
| Col | Header | Script Usage |
|---|---|---|
| A | Date New Lead | date field — when lead was generated |
| B | Source | categorical — smrtPhone, smrtDialer, etc. |
| C | Address | informational only (not tracked in KPIs) |
| D | Zip Code | informational only |
| E | List/Problem | categorical — Tax Del, Probate, Foreclosure, Divorce, Eviction |
| F | Camapign | categorical (typo in sheet — "Campaign") — FTM, etc. |
| G | Currently Listed? | categorical — Yes/No |
| H | Lead Tempature | categorical (typo — "Temperature") — should be set when qualified |
| I | Date Added Sift | date field — when prospect was added to DataSift/REISift |
| J | Date Qualified | date field |
| K | Date Not Interested | date field |
| L | Date Dead Lead | date field |
| M | Dead Reasoning | categorical — reason lead died |
| N | Date Ghosting Lead | date field |
| O | Date Lost Deal | date field |
| P | Lost Reasoning | categorical — reason deal was lost |
| Q | Date First Offer | date field — NOT currently used (offer data comes from Offers Summary tab) |
| R | Date Accepted Offer | date field — NOT currently used |
| S | Projected Profit | NOT currently used |
| T | Date Closed | NOT currently used |
| U | Profit | NOT currently used |
| V | Date Fell Through | NOT currently used |
| W | Fell Through Reasoning | NOT currently used |

### Business Context
All prospects start in DataSift/REISift (Sift). The flow is:
1. Prospect exists in Sift (Date Added Sift)
2. Caller generates a lead from the prospect (Date New Lead)
3. Lead Manager qualifies the lead (Date Qualified) — at this point a Lead Temperature MUST be set
4. Lead moves to an outcome: Qualified → Ghosting, or New Lead → Not Interested / Dead

Columns Q-W (offer/profit fields) exist on this tab but are NOT used in KPI calculations. Offer tracking is handled by the Offers Summary tab. These could be cross-referenced programmatically in the future.

### Volume Metrics (per period — Today / Week / MTD / QTD / YTD)
- New leads generated (by Date New Lead)
- Leads qualified (by Date Qualified)
- Leads not interested (by Date Not Interested)
- Dead leads (by Date Dead Lead)
- Ghosting leads (by Date Ghosting Lead)
- Lost deals (by Date Lost Deal)
- Total leads (all time)

### Pipeline Velocity (avg days between stages, per period)
Calculated only for records where both dates exist and the delta is >= 0.

| Metric | From | To | What It Measures |
|---|---|---|---|
| Added to Sift → New Lead | Date Added Sift | Date New Lead | How long a prospect sits before becoming a lead |
| New Lead → Qualified | Date New Lead | Date Qualified | Lead-to-qualification cycle time |
| New Lead → Not Interested | Date New Lead | Date Not Interested | How fast leads go cold |
| New Lead → Dead | Date New Lead | Date Dead Lead | Time to dead |
| Qualified → Ghosting | Date Qualified | Date Ghosting Lead | Qualified leads going dark |
| Added to Sift → Lost Deal | Date Added Sift | Date Lost Deal | Full lifecycle to loss |

Velocity is bucketed by the OUTCOME date (when the transition happened), not the originating date.

### Distribution Breakdowns (emoji bar charts, per period)
Shown as top 5 values with percentage bars. Bucketed by Date New Lead.

- **List/Problem** — what distress list the lead came from (% of new leads)
- **Campaign** — what campaign generated the lead (% of new leads)
- **Lead Temperature** — temperature of qualified leads (% of qualified leads in period, or new leads if no qualified)
- **Dead Reasoning** — why leads died (% of dead leads) — only shown if dead > 0
- **Lost Reasoning** — why deals were lost (% of lost deals) — only shown if lost > 0

### Edge Cases
- All velocity metrics show "—" when no valid date pairs exist for the period
- Velocity ignores negative deltas (outcome date before origin date) — these are anomalies
- Distribution bars show "No data" when the counter is empty
- `Camapign` and `Lead Tempature` are typos in the actual sheet headers — script matches them exactly
- Columns Q-W are deliberately not used — offer tracking belongs in Offers Summary

### Known Data Quality Issues (as of 2026-04-04)
- 18 of 43 leads (42%) have Date Qualified but NO Lead Temperature — this is a process violation
- Only 1 lead has a temperature set (COLD)
- All 43 leads show Campaign = FTM (no campaign diversity yet)
- Suspicion: lead manager may be setting Date Qualified on the day they attempt to call, not the day they actually reach the lead and qualify it

### Future: Data Quality Bot (NOT YET BUILT)
A separate bot is planned that would post to a DIFFERENT Slack channel and flag:
- Days where no KPIs were entered for any tab
- Leads with Date Qualified but no Lead Temperature
- Leads where Date Qualified < Date New Lead (qualified before it was a lead — anomaly)
- Leads with missing List/Problem
- Other date sequence anomalies
This is documented here for future reference but is NOT part of the current KPI bot.

---

## Tabs Still Using Basic Format

The following tabs have not been expanded into detailed KPI sections. They report today/this week only with simple sums.

### SmrtDialer
- Columns used: `Date`, `Dials`, `New Lead`, `Talk Days (hour)`
- Metrics: dials, new leads, talk time (today/week)

### Exhausted
- Columns used: `Date`, `Hours Worked`, `New Prospects`, `Lead`
- Metrics: hours worked, new prospects, leads generated (today/week)
- Note: this tab has the same caller-level structure as First to Market with similar columns. Could be expanded to match.

### Offers Summary
- All-time snapshot (not time-bucketed for daily metrics)
- Metrics: total offers, active/pending, accepted, rejected, under contract, projected profit, closed profit
- Dollar values cleaned of `$` and `,`

### Costs
- Columns used: `Date`, `Channel`, `Cost`
- Metrics: total spend today/week, breakdown by channel

### Expenses
- Columns used: `DATE`, `CATEGORY`, `EXPENSE`
- Metrics: total expenses today/week, breakdown by category
- Note: column headers are uppercase in this tab

---

## Slack Formatting Notes

- Slack Block Kit section blocks have a 3000-character limit per `text` field
- The `_text_to_blocks()` helper splits long sections (First to Market, Lead Management, Acquisition) into multiple section blocks by splitting on period boundaries (`*— Today —*`, `*— This Week —*`, etc.)
- First to Market, Lead Management, and Acquisition use a `header` block for their section title (instead of inline bold) because their content spans multiple section blocks
- All other sections use inline `*EMOJI TITLE*` formatting within a single section block

## Helper Functions

- `_safe_int(val)` — converts to int, returns 0 on failure
- `_safe_float(val)` — converts to float, returns 0.0 on failure
- `_ratio(num, denom, fmt)` — safe division returning formatted string or "—"
- `_pct(num, denom)` — safe percentage string or "—"
- `_text_to_blocks(text, max_chars)` — splits text into multiple Slack section blocks
- `parse_date(val)` — parses date strings in MM/DD/YYYY, YYYY-MM-DD, or MM/DD/YY format
- `get_today_and_week()` — returns today's date and Monday of current week
- `get_period_starts()` — returns 1st of month, 1st of quarter, 1st of year
