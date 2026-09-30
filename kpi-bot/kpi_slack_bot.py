"""
DataSift KPI Bot → Slack Daily Update
Pulls data from every tab in your KPI Google Sheet
and posts a formatted summary to Slack at end of day.

Configure via .env (copy .env.template to .env and fill in values).
"""

import os
import gspread
from google.oauth2.service_account import Credentials
import requests
import json
from datetime import datetime, timedelta
from collections import defaultdict, Counter

# ── Config ──────────────────────────────────────────────────
# All config loaded from .env or environment variables.
# Copy .env.template to .env and fill in your values.

from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Load .env file if present (no external lib dependency)
_env_path = Path(SCRIPT_DIR) / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

SPREADSHEET_ID = os.environ.get("KPI_SPREADSHEET_ID", "").strip()
SLACK_WEBHOOK_URL = os.environ.get("KPI_SLACK_WEBHOOK_URL", "").strip()
SERVICE_ACCOUNT_FILE = os.environ.get(
    "KPI_SERVICE_ACCOUNT_FILE",
    os.path.join(SCRIPT_DIR, "service_account.json"),
)
COMPANY_NAME = os.environ.get("KPI_COMPANY_NAME", "Your Company").strip()

# Fail fast if config is missing
if not SPREADSHEET_ID:
    raise RuntimeError("KPI_SPREADSHEET_ID not set. Copy .env.template to .env and fill in your values.")
if not SLACK_WEBHOOK_URL:
    raise RuntimeError("KPI_SLACK_WEBHOOK_URL not set. Copy .env.template to .env and fill in your values.")
if not os.path.exists(SERVICE_ACCOUNT_FILE):
    raise RuntimeError(f"Service account file not found at {SERVICE_ACCOUNT_FILE}. See README for Google Cloud setup.")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def get_sheet():
    """Authenticate and return the spreadsheet."""
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)


def parse_date(val):
    """Try to parse a date string, return date object or None."""
    if not val or not isinstance(val, str):
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(val.strip(), fmt).date()
        except ValueError:
            continue
    return None


def get_today_and_week():
    """Return today's date and the start of the current week (Monday)."""
    today = datetime.now().date()
    week_start = today - timedelta(days=today.weekday())  # Monday
    return today, week_start


def get_period_starts():
    """Return the first day of the current month, quarter, and year."""
    today = datetime.now().date()
    month_start = today.replace(day=1)
    quarter_month = ((today.month - 1) // 3) * 3 + 1
    quarter_start = today.replace(month=quarter_month, day=1)
    year_start = today.replace(month=1, day=1)
    return month_start, quarter_start, year_start


def executive_summary_kpis(sheet):
    """High-level MTD/QTD/YTD funnel summary across all tabs."""
    month_start, quarter_start, year_start = get_period_starts()

    # ── Leads Generated & Qualified (from Leads Summary) ──
    leads_mtd = leads_qtd = leads_ytd = 0
    qualified_mtd = qualified_qtd = qualified_ytd = 0
    dead_mtd = dead_qtd = dead_ytd = 0
    try:
        ws = sheet.worksheet("Leads Summary")
        records = ws.get_all_records()
        for row in records:
            d = parse_date(str(row.get("Date New Lead", "")))
            if d and d >= year_start:
                leads_ytd += 1
                if d >= quarter_start:
                    leads_qtd += 1
                if d >= month_start:
                    leads_mtd += 1

            dq = parse_date(str(row.get("Date Qualified", "")))
            if dq and dq >= year_start:
                qualified_ytd += 1
                if dq >= quarter_start:
                    qualified_qtd += 1
                if dq >= month_start:
                    qualified_mtd += 1

            dd = parse_date(str(row.get("Date Dead Lead", "")))
            if dd and dd >= year_start:
                dead_ytd += 1
                if dd >= quarter_start:
                    dead_qtd += 1
                if dd >= month_start:
                    dead_mtd += 1
    except Exception:
        pass

    # ── Offers, Accepted, Contracts (from Acquisition) ──
    offers_mtd = offers_qtd = offers_ytd = 0
    accepted_mtd = accepted_qtd = accepted_ytd = 0
    contracts_mtd = contracts_qtd = contracts_ytd = 0
    appts_mtd = appts_qtd = appts_ytd = 0
    try:
        ws = sheet.worksheet("Acquisition")
        records = ws.get_all_records()
        for row in records:
            d = parse_date(str(row.get("Date", "")))
            off = int(row.get("Initial Offers", 0) or 0)
            acc = int(row.get("Offer Accepted", 0) or 0)
            cont = int(row.get("Contract Sent", 0) or 0)
            appt = int(row.get("Appointments", 0) or 0)

            if d and d >= year_start:
                offers_ytd += off
                accepted_ytd += acc
                contracts_ytd += cont
                appts_ytd += appt
                if d >= quarter_start:
                    offers_qtd += off
                    accepted_qtd += acc
                    contracts_qtd += cont
                    appts_qtd += appt
                if d >= month_start:
                    offers_mtd += off
                    accepted_mtd += acc
                    contracts_mtd += cont
                    appts_mtd += appt
    except Exception:
        pass

    # ── Projected & Closed Profit (from Offers Summary) ──
    projected_mtd = projected_qtd = projected_ytd = 0.0
    closed_mtd = closed_qtd = closed_ytd = 0.0
    under_contract = 0
    try:
        ws = sheet.worksheet("Offers Summary")
        records = ws.get_all_records()
        for row in records:
            uc = str(row.get("Under Contract", "")).strip()
            if uc and uc.lower() not in ("", "no", "n/a"):
                under_contract += 1

            d = parse_date(str(row.get("Offer Made", "")))

            proj = row.get("Projected Profit", 0)
            try:
                proj = float(str(proj).replace("$", "").replace(",", "")) if proj else 0
            except ValueError:
                proj = 0

            profit = row.get("Profit", 0)
            try:
                profit = float(str(profit).replace("$", "").replace(",", "")) if profit else 0
            except ValueError:
                profit = 0

            dc = parse_date(str(row.get("Date Closed", "")))
            if dc and dc >= year_start:
                closed_ytd += profit
                if dc >= quarter_start:
                    closed_qtd += profit
                if dc >= month_start:
                    closed_mtd += profit

            if d and d >= year_start:
                projected_ytd += proj
                if d >= quarter_start:
                    projected_qtd += proj
                if d >= month_start:
                    projected_mtd += proj
    except Exception:
        pass

    # ── Total Spend (Costs + Expenses) ──
    spend_mtd = spend_qtd = spend_ytd = 0.0
    try:
        ws = sheet.worksheet("Costs")
        for row in ws.get_all_records():
            d = parse_date(str(row.get("Date", "")))
            cost = row.get("Cost", 0)
            try:
                cost = float(str(cost).replace("$", "").replace(",", "")) if cost else 0
            except ValueError:
                cost = 0
            if d and d >= year_start:
                spend_ytd += cost
                if d >= quarter_start:
                    spend_qtd += cost
                if d >= month_start:
                    spend_mtd += cost
    except Exception:
        pass

    try:
        ws = sheet.worksheet("Expenses")
        for row in ws.get_all_records():
            d = parse_date(str(row.get("DATE", "")))
            amt = row.get("EXPENSE", 0)
            try:
                amt = float(str(amt).replace("$", "").replace(",", "")) if amt else 0
            except ValueError:
                amt = 0
            if d and d >= year_start:
                spend_ytd += amt
                if d >= quarter_start:
                    spend_qtd += amt
                if d >= month_start:
                    spend_mtd += amt
    except Exception:
        pass

    # ── Conversion rates ──
    qual_rate_mtd = f"{(qualified_mtd / leads_mtd * 100):.0f}%" if leads_mtd else "—"
    qual_rate_qtd = f"{(qualified_qtd / leads_qtd * 100):.0f}%" if leads_qtd else "—"
    qual_rate_ytd = f"{(qualified_ytd / leads_ytd * 100):.0f}%" if leads_ytd else "—"

    month_name = datetime.now().strftime("%B")
    quarter_num = (datetime.now().month - 1) // 3 + 1
    year = datetime.now().year

    lines = [
        f"*{month_name} MTD  |  Q{quarter_num} QTD  |  {year} YTD*",
        "",
        f"*Leads Generated:*  {leads_mtd} MTD  /  {leads_qtd} QTD  /  {leads_ytd} YTD",
        f"*Leads Qualified:*  {qualified_mtd} MTD  /  {qualified_qtd} QTD  /  {qualified_ytd} YTD  ({qual_rate_mtd} / {qual_rate_qtd} / {qual_rate_ytd})",
        f"*Dead Leads:*  {dead_mtd} MTD  /  {dead_qtd} QTD  /  {dead_ytd} YTD",
        f"*Appointments:*  {appts_mtd} MTD  /  {appts_qtd} QTD  /  {appts_ytd} YTD",
        f"*Offers Made:*  {offers_mtd} MTD  /  {offers_qtd} QTD  /  {offers_ytd} YTD",
        f"*Offers Accepted:*  {accepted_mtd} MTD  /  {accepted_qtd} QTD  /  {accepted_ytd} YTD",
        f"*Contracts Sent:*  {contracts_mtd} MTD  /  {contracts_qtd} QTD  /  {contracts_ytd} YTD",
        f"*Under Contract:*  {under_contract} (active)",
        f"*Projected Profit:*  ${projected_mtd:,.0f} MTD  /  ${projected_qtd:,.0f} QTD  /  ${projected_ytd:,.0f} YTD",
        f"*Closed Profit:*  ${closed_mtd:,.0f} MTD  /  ${closed_qtd:,.0f} QTD  /  ${closed_ytd:,.0f} YTD",
        f"*Total Spend:*  ${spend_mtd:,.0f} MTD  /  ${spend_qtd:,.0f} QTD  /  ${spend_ytd:,.0f} YTD",
    ]
    return "\n".join(lines)


def _distribution_bar(counter, total=None, top_n=5):
    """Build an emoji bar chart for a Counter, showing top N items as % of total.

    Uses sum of counter values as denominator (not an external total),
    so only items that actually have values are represented.
    'Other' only shows when there are more distinct values beyond top_n.
    """
    if not counter:
        return "_No data_"
    counter_total = sum(counter.values())
    if not counter_total:
        return "_No data_"
    # Use counter's own total as denominator — only counts items with actual values
    denom = counter_total
    # How many items with missing data (external total vs counter total)
    missing = (total - counter_total) if total and total > counter_total else 0
    lines = []
    for label, count in counter.most_common(top_n):
        pct = count / denom * 100
        filled = round(pct / 10)
        bar = "\u2588" * filled + "\u2591" * (10 - filled)
        lines.append(f"  {bar}  {pct:.0f}% — {label} ({count})")
    # Only show "Other" if there are more distinct values beyond top_n
    shown_items = counter.most_common(top_n)
    if len(counter) > top_n:
        other_count = sum(c for _, c in counter.most_common()[top_n:])
        other_pct = other_count / denom * 100
        lines.append(f"  {'':>10}  {other_pct:.0f}% — Other ({other_count})")
    # Flag missing data if external total is higher than counter total
    if missing > 0:
        lines.append(f"  {'':>10}  \u26a0\ufe0f {missing} missing (no value set)")
    return "\n".join(lines)


def _avg_days_between(date_pairs):
    """Calculate average days between pairs of dates. Ignores None pairs and negative values."""
    diffs = []
    for d1, d2 in date_pairs:
        if d1 and d2:
            delta = (d2 - d1).days
            if delta >= 0:
                diffs.append(delta)
    if not diffs:
        return "—"
    avg = sum(diffs) / len(diffs)
    return f"{avg:.1f} days (n={len(diffs)})"


def leads_summary_kpis(sheet):
    """Leads Summary tab — detailed volume, velocity, distributions, and data quality."""
    try:
        ws = sheet.worksheet("Leads Summary")
        records = ws.get_all_records()
    except Exception as e:
        return f"_Leads Summary: Error reading tab — {e}_"

    today, week_start = get_today_and_week()
    month_start, quarter_start, year_start = get_period_starts()

    # Date column keys
    DATE_COLS = {
        "new_lead": "Date New Lead",
        "added_sift": "Date Added Sift",
        "qualified": "Date Qualified",
        "not_interested": "Date Not Interested",
        "dead": "Date Dead Lead",
        "ghosting": "Date Ghosting Lead",
        "lost_deal": "Date Lost Deal",
    }

    # Categorical columns
    CAT_COLS = {
        "source": "Source",
        "list_problem": "List/Problem",
        "campaign": "Camapign",
        "lead_temp": "Lead Tempature",
        "dead_reasoning": "Dead Reasoning",
        "lost_reasoning": "Lost Reasoning",
        "currently_listed": "Currently Listed?",
    }

    # Parse all rows
    parsed = []
    for row in records:
        r = {}
        for k, col in DATE_COLS.items():
            r[k] = parse_date(str(row.get(col, "")))
        for k, col in CAT_COLS.items():
            r[k] = str(row.get(col, "")).strip()
        parsed.append(r)

    # ── Volume by period ──
    count_keys = ["new_lead", "qualified", "not_interested", "dead", "ghosting", "lost_deal"]
    periods = {p: {k: 0 for k in count_keys} for p in ["today", "week", "mtd", "qtd", "ytd"]}

    # Categorical counters per period
    cat_keys = ["source", "list_problem", "campaign", "lead_temp", "dead_reasoning", "lost_reasoning"]
    cat_periods = {p: {k: Counter() for k in cat_keys} for p in ["today", "week", "mtd", "qtd", "ytd"]}

    # Velocity pairs per period
    velocity_defs = {
        "sift_to_lead": ("added_sift", "new_lead"),
        "lead_to_qualified": ("new_lead", "qualified"),
        "lead_to_not_interested": ("new_lead", "not_interested"),
        "lead_to_dead": ("new_lead", "dead"),
        "qualified_to_ghosting": ("qualified", "ghosting"),
        "sift_to_lost": ("added_sift", "lost_deal"),
    }
    vel_periods = {p: {k: [] for k in velocity_defs} for p in ["today", "week", "mtd", "qtd", "ytd"]}

    for r in parsed:
        # Use new_lead date for period bucketing on volume
        for ck in count_keys:
            d = r[ck]
            if not d:
                continue
            if d == today:
                periods["today"][ck] += 1
            if d >= week_start:
                periods["week"][ck] += 1
            if d >= month_start:
                periods["mtd"][ck] += 1
            if d >= quarter_start:
                periods["qtd"][ck] += 1
            if d >= year_start:
                periods["ytd"][ck] += 1

        # Categoricals — bucket by new_lead date
        nl = r["new_lead"]
        if nl:
            for period_name, start in [("today", today), ("week", week_start), ("mtd", month_start), ("qtd", quarter_start), ("ytd", year_start)]:
                if period_name == "today" and nl != today:
                    continue
                if period_name != "today" and nl < start:
                    continue
                for ck in cat_keys:
                    val = r[ck]
                    if val:
                        cat_periods[period_name][ck][val] += 1

        # Velocity — bucket by the OUTCOME date (when it happened)
        for vk, (from_key, to_key) in velocity_defs.items():
            d_from = r[from_key]
            d_to = r[to_key]
            if d_from and d_to:
                # bucket by outcome date
                if d_to == today:
                    vel_periods["today"][vk].append((d_from, d_to))
                if d_to >= week_start:
                    vel_periods["week"][vk].append((d_from, d_to))
                if d_to >= month_start:
                    vel_periods["mtd"][vk].append((d_from, d_to))
                if d_to >= quarter_start:
                    vel_periods["qtd"][vk].append((d_from, d_to))
                if d_to >= year_start:
                    vel_periods["ytd"][vk].append((d_from, d_to))

    total_all_time = len(parsed)

    def _build_period_summary(p, label):
        d = periods[p]
        cats = cat_periods[p]
        vels = vel_periods[p]
        total_new = d["new_lead"]

        lines = [
            f"*— {label} —*",
            "",
            f"*Volume:*  {d['new_lead']} new leads  |  {d['qualified']} qualified  |  {d['not_interested']} not interested  |  {d['dead']} dead  |  {d['ghosting']} ghosting  |  {d['lost_deal']} lost deals",
            "",
            f"*Pipeline Velocity (avg days):*",
            f"  Added to Sift → New Lead:  {_avg_days_between(vels['sift_to_lead'])}",
            f"  New Lead → Qualified:  {_avg_days_between(vels['lead_to_qualified'])}",
            f"  New Lead → Not Interested:  {_avg_days_between(vels['lead_to_not_interested'])}",
            f"  New Lead → Dead:  {_avg_days_between(vels['lead_to_dead'])}",
            f"  Qualified → Ghosting:  {_avg_days_between(vels['qualified_to_ghosting'])}",
            f"  Added to Sift → Lost Deal:  {_avg_days_between(vels['sift_to_lost'])}",
            "",
            f"*List/Problem Distribution:*",
            _distribution_bar(cats["list_problem"], total_new),
            "",
            f"*Campaign Distribution:*",
            _distribution_bar(cats["campaign"], total_new),
            "",
            f"*Lead Temperature Distribution:*",
            _distribution_bar(cats["lead_temp"], d["qualified"] if d["qualified"] else total_new),
        ]

        # Only show dead/lost reasoning if there are dead/lost leads in this period
        if d["dead"] > 0:
            lines += [
                "",
                f"*Dead Reasoning:*",
                _distribution_bar(cats["dead_reasoning"], d["dead"]),
            ]
        if d["lost_deal"] > 0:
            lines += [
                "",
                f"*Lost Reasoning:*",
                _distribution_bar(cats["lost_reasoning"], d["lost_deal"]),
            ]

        return "\n".join(lines)

    sections = [
        _build_period_summary("today", "Today"),
        "",
        _build_period_summary("week", "This Week"),
        "",
        _build_period_summary("mtd", "Month to Date"),
        "",
        _build_period_summary("qtd", "Quarter to Date"),
        "",
        _build_period_summary("ytd", "Year to Date"),
        "",
        f"*Total Leads (all time):*  {total_all_time}",
    ]
    return "\n".join(sections)


def smrtdialer_kpis(sheet):
    """SmrtDialer tab — dials, new leads, talk time today/this week."""
    try:
        ws = sheet.worksheet("SmrtDialer")
        records = ws.get_all_records()
    except Exception as e:
        return f"_SmrtDialer: Error reading tab — {e}_"

    today, week_start = get_today_and_week()

    dials_today = 0
    dials_week = 0
    new_leads_today = 0
    new_leads_week = 0
    talk_hours_today = 0.0
    talk_hours_week = 0.0

    for row in records:
        d = parse_date(str(row.get("Date", "")))
        dials = int(row.get("Dials", 0) or 0)
        new = int(row.get("New Lead", 0) or 0)
        talk = float(row.get("Talk Days (hour)", 0) or 0)

        if d == today:
            dials_today += dials
            new_leads_today += new
            talk_hours_today += talk
        if d and d >= week_start:
            dials_week += dials
            new_leads_week += new
            talk_hours_week += talk

    lines = [
        f"*Dials:* {dials_today} today / {dials_week} this week",
        f"*New Leads:* {new_leads_today} today / {new_leads_week} this week",
        f"*Talk Time:* {talk_hours_today:.1f}h today / {talk_hours_week:.1f}h this week",
    ]
    return "\n".join(lines)


def _safe_int(val):
    """Safely convert a value to int, defaulting to 0."""
    try:
        return int(val) if val else 0
    except (ValueError, TypeError):
        return 0


def _safe_float(val):
    """Safely convert a value to float, defaulting to 0."""
    try:
        return float(val) if val else 0.0
    except (ValueError, TypeError):
        return 0.0


def _ratio(num, denom, fmt=",.1f"):
    """Safe division returning formatted string or '—'."""
    if not denom:
        return "—"
    return f"{num / denom:{fmt}}"


def _pct(num, denom):
    """Safe percentage string."""
    if not denom:
        return "—"
    return f"{num / denom * 100:.1f}%"


def first_to_market_kpis(sheet):
    """First to Market tab — detailed volume + efficiency metrics."""
    try:
        ws = sheet.worksheet("First to Market")
        records = ws.get_all_records()
    except Exception as e:
        return f"_First to Market: Error reading tab — {e}_"

    today, week_start = get_today_and_week()
    month_start, quarter_start, year_start = get_period_starts()

    # Column key mappings (exact headers from sheet)
    COLS = {
        "new_ps": "New Ps #",
        "fu_ps": "Follow Up  Ps #",
        "init_dial": "Initial Dial",
        "fu1_dial": "FU 1 Dial",
        "fu2_dial": "FU2 Dial",
        "fu3_dial": "FU 3 Dial",
        "sms": "SMS",
        "sms_reply": "SMS Reply",
        "dead": "Dead",
        "dnc": "DNC",
        "no_answer": "No Answer",
        "voicemail": "Voicemail",
        "wrong": "Wrong",
        "correct_init": "Correct Initial",
        "correct_fu1": "Correct F/U 1",
        "correct_fu2": "Correct F/U 2",
        "correct_fu3": "Correct F/U 3",
        "new_lead": "New Lead",
        "not_interested": "Not interested",
        "full_exhausted": "Full Exhausted",
        "hours": "Hours",
    }

    # Accumulator keys — one dict per time period
    keys = [
        "new_ps", "fu_ps", "init_dial", "fu1_dial", "fu2_dial", "fu3_dial",
        "sms", "sms_reply", "dead", "dnc", "no_answer", "voicemail", "wrong",
        "correct_init", "correct_fu1", "correct_fu2", "correct_fu3",
        "new_lead", "not_interested", "full_exhausted", "hours",
    ]
    periods = {p: {k: 0 for k in keys} for p in ["today", "week", "mtd", "qtd", "ytd"]}
    # hours needs float
    for p in periods.values():
        p["hours"] = 0.0

    for row in records:
        d = parse_date(str(row.get("Date", "")))
        if not d:
            continue

        vals = {}
        for k in keys:
            if k == "hours":
                vals[k] = _safe_float(row.get(COLS[k], 0))
            else:
                vals[k] = _safe_int(row.get(COLS[k], 0))

        # Bucket into periods
        if d == today:
            for k in keys:
                periods["today"][k] += vals[k]
        if d >= week_start:
            for k in keys:
                periods["week"][k] += vals[k]
        if d >= month_start:
            for k in keys:
                periods["mtd"][k] += vals[k]
        if d >= quarter_start:
            for k in keys:
                periods["qtd"][k] += vals[k]
        if d >= year_start:
            for k in keys:
                periods["ytd"][k] += vals[k]

    def _build_period_summary(p, label):
        """Build the KPI lines for a single period."""
        d = periods[p]
        total_dials = d["init_dial"] + d["fu1_dial"] + d["fu2_dial"] + d["fu3_dial"]
        total_fu_dials = d["fu1_dial"] + d["fu2_dial"] + d["fu3_dial"]
        total_ps = d["new_ps"] + d["fu_ps"]
        total_correct = d["correct_init"] + d["correct_fu1"] + d["correct_fu2"] + d["correct_fu3"]

        lines = [
            f"*— {label} —*",
            "",
            f"*Volume:*  {d['new_ps']} new prospects  |  {d['fu_ps']} FU prospects  |  {total_ps} total  |  {d['hours']:.1f}h worked",
            f"*Dials:*  {d['init_dial']} initial  |  {d['fu1_dial']} FU1  |  {d['fu2_dial']} FU2  |  {d['fu3_dial']} FU3  |  {total_dials} total",
            f"*SMS:*  {d['sms']} sent  |  {d['sms_reply']} replies",
            "",
            f"*Dials/New Prospect:*  {_ratio(d['init_dial'], d['new_ps'])}",
            f"*Dials/FU Prospect:*  {_ratio(total_fu_dials, d['fu_ps'])}",
            "",
            f"*Correct Numbers:*  {d['correct_init']} initial  |  {d['correct_fu1']} FU1  |  {d['correct_fu2']} FU2  |  {d['correct_fu3']} FU3  |  {total_correct} total",
            f"*Contact Rate:*  {_pct(total_correct, total_ps)}  ({total_correct} correct / {total_ps} prospects)",
            f"*Initial Correct Rate:*  {_pct(d['correct_init'], d['init_dial'])}  ({_ratio(d['init_dial'], d['correct_init'])} dials/correct)",
            f"*FU1 Correct Rate:*  {_pct(d['correct_fu1'], d['fu1_dial'])}  ({_ratio(d['fu1_dial'], d['correct_fu1'])} dials/correct)",
            f"*FU2 Correct Rate:*  {_pct(d['correct_fu2'], d['fu2_dial'])}  ({_ratio(d['fu2_dial'], d['correct_fu2'])} dials/correct)",
            f"*FU3 Correct Rate:*  {_pct(d['correct_fu3'], d['fu3_dial'])}  ({_ratio(d['fu3_dial'], d['correct_fu3'])} dials/correct)",
            "",
            f"*Dispositions (% of {total_dials} dials):*  Dead {_pct(d['dead'], total_dials)}  |  Wrong {_pct(d['wrong'], total_dials)}  |  No Answer {_pct(d['no_answer'], total_dials)}  |  DNC {_pct(d['dnc'], total_dials)}",
            "",
            f"*New Leads:*  {d['new_lead']}  |  *Prospects/Lead:*  {_ratio(total_ps, d['new_lead'])}  |  *Correct/Lead:*  {_ratio(total_correct, d['new_lead'])}  |  *Dials/Lead:*  {_ratio(total_dials, d['new_lead'])}",
            f"*Not Interested:*  {d['not_interested']}  ({_pct(d['not_interested'], total_ps)} of prospects)",
            f"*Fully Exhausted:*  {d['full_exhausted']}  ({_pct(d['full_exhausted'], total_ps)} of prospects)",
        ]
        return "\n".join(lines)

    # Build today + this week as the daily section
    sections = [
        _build_period_summary("today", "Today"),
        "",
        _build_period_summary("week", "This Week"),
        "",
        _build_period_summary("mtd", "Month to Date"),
        "",
        _build_period_summary("qtd", "Quarter to Date"),
        "",
        _build_period_summary("ytd", "Year to Date"),
    ]
    return "\n".join(sections)


def exhausted_kpis(sheet):
    """Exhausted tab — hours worked, prospects, dials."""
    try:
        ws = sheet.worksheet("Exhausted")
        records = ws.get_all_records()
    except Exception as e:
        return f"_Exhausted: Error reading tab — {e}_"

    today, week_start = get_today_and_week()

    hours_today = 0.0
    hours_week = 0.0
    new_ps_today = 0
    new_ps_week = 0
    leads_today = 0
    leads_week = 0

    for row in records:
        d = parse_date(str(row.get("Date", "")))
        hrs = float(row.get("Hours Worked", 0) or 0)
        new_ps = int(row.get("New Prospects", 0) or 0)
        ld = int(row.get("Lead", 0) or 0)

        if d == today:
            hours_today += hrs
            new_ps_today += new_ps
            leads_today += ld
        if d and d >= week_start:
            hours_week += hrs
            new_ps_week += new_ps
            leads_week += ld

    lines = [
        f"*Hours Worked:* {hours_today:.1f}h today / {hours_week:.1f}h this week",
        f"*New Prospects:* {new_ps_today} today / {new_ps_week} this week",
        f"*Leads Generated:* {leads_today} today / {leads_week} this week",
    ]
    return "\n".join(lines)


def lead_management_kpis(sheet):
    """Lead Management tab — detailed volume + efficiency metrics."""
    try:
        ws = sheet.worksheet("Lead Management")
        records = ws.get_all_records()
    except Exception as e:
        return f"_Lead Management: Error reading tab — {e}_"

    today, week_start = get_today_and_week()
    month_start, quarter_start, year_start = get_period_starts()

    # Column key mappings (exact headers from sheet)
    COLS = {
        "hours": "Hours",
        "new_leads": "New Leads",
        "qualified": "New Leads Qualified",
        "qual_followups": "Qualified Lead Followups",
        "inbound_calls": "Inbound calls",
        "dials": "Dials Made",
        "sms_sent": "SMS Sent",
        "voicemails": "Voicemails",
        "sms_inbound": "SMS Inbound",
        "dead_num": "Dead #",
        "dnc": "DNC #",
        "no_answer": "No Answer",
        "soliciters": "Soliciters",
        "conversations": "Conversations",
        "not_interested": "Not interested",
        "ghosting": "Ghosting Leads",
        "lost_deal": "Lost Deal",
        "dead_leads": "Dead Leads",
        "sent_aqs": "Sent to AQS",
    }

    keys = list(COLS.keys())
    periods = {p: {k: 0 for k in keys} for p in ["today", "week", "mtd", "qtd", "ytd"]}
    for p in periods.values():
        p["hours"] = 0.0

    for row in records:
        d = parse_date(str(row.get("Date", "")))
        if not d:
            continue

        vals = {}
        for k in keys:
            if k == "hours":
                vals[k] = _safe_float(row.get(COLS[k], 0))
            else:
                vals[k] = _safe_int(row.get(COLS[k], 0))

        if d == today:
            for k in keys:
                periods["today"][k] += vals[k]
        if d >= week_start:
            for k in keys:
                periods["week"][k] += vals[k]
        if d >= month_start:
            for k in keys:
                periods["mtd"][k] += vals[k]
        if d >= quarter_start:
            for k in keys:
                periods["qtd"][k] += vals[k]
        if d >= year_start:
            for k in keys:
                periods["ytd"][k] += vals[k]

    def _build_period_summary(p, label):
        d = periods[p]
        total_leads_worked = d["new_leads"] + d["qual_followups"]
        total_outreach = d["dials"] + d["sms_sent"]

        lines = [
            f"*— {label} —*",
            "",
            f"*Volume:*  {d['new_leads']} new leads  |  {d['qualified']} qualified  |  {d['qual_followups']} qualified FUs  |  {total_leads_worked} total worked  |  {d['hours']:.1f}h",
            f"*Dials:*  {d['dials']} dials  |  {d['sms_sent']} SMS sent  |  {d['sms_inbound']} SMS inbound  |  {d['inbound_calls']} inbound calls  |  {d['voicemails']} VMs",
            "",
            f"*Dial Efficiency:*",
            f"  Dials/Lead Worked:  {_ratio(d['dials'], total_leads_worked)}",
            f"  Dials/Conversation:  {_ratio(d['dials'], d['conversations'])}",
            f"  Dials/Sent to AQS:  {_ratio(d['dials'], d['sent_aqs'])}",
            "",
            f"*Contact & Conversion:*",
            f"  Conversation Rate:  {_pct(d['conversations'], d['dials'])}  ({d['conversations']} convos / {d['dials']} dials)",
            f"  No Answer Rate:  {_pct(d['no_answer'], d['dials'])}",
            f"  Qualification Rate:  {_pct(d['qualified'], d['new_leads'])}  ({d['qualified']} qualified / {d['new_leads']} new leads)",
            "",
            f"*Pipeline Throughput (→ Acquisitions):*",
            f"  Sent to AQS:  {d['sent_aqs']}",
            f"  AQS Send Rate:  {_pct(d['sent_aqs'], total_leads_worked)}  ({d['sent_aqs']} / {total_leads_worked} leads worked)",
            f"  Leads/AQS Send:  {_ratio(total_leads_worked, d['sent_aqs'])}",
            f"  Conversations/AQS Send:  {_ratio(d['conversations'], d['sent_aqs'])}",
            "",
            f"*Lead Dispositions (% of {total_leads_worked} leads worked):*",
            f"  Not Interested:  {d['not_interested']}  ({_pct(d['not_interested'], total_leads_worked)})"
            f"  |  Ghosting:  {d['ghosting']}  ({_pct(d['ghosting'], total_leads_worked)})"
            f"  |  Lost Deal:  {d['lost_deal']}  ({_pct(d['lost_deal'], total_leads_worked)})"
            f"  |  Dead Leads:  {d['dead_leads']}  ({_pct(d['dead_leads'], total_leads_worked)})",
            "",
            f"*Phone Dispositions (% of {d['dials']} dials):*"
            f"  Dead #: {_pct(d['dead_num'], d['dials'])}"
            f"  |  DNC: {_pct(d['dnc'], d['dials'])}"
            f"  |  Soliciters: {d['soliciters']}",
        ]
        return "\n".join(lines)

    sections = [
        _build_period_summary("today", "Today"),
        "",
        _build_period_summary("week", "This Week"),
        "",
        _build_period_summary("mtd", "Month to Date"),
        "",
        _build_period_summary("qtd", "Quarter to Date"),
        "",
        _build_period_summary("ytd", "Year to Date"),
    ]
    return "\n".join(sections)


def acquisition_kpis(sheet):
    """Acquisition tab — detailed volume + efficiency metrics."""
    try:
        ws = sheet.worksheet("Acquisition")
        records = ws.get_all_records()
    except Exception as e:
        return f"_Acquisition: Error reading tab — {e}_"

    today, week_start = get_today_and_week()
    month_start, quarter_start, year_start = get_period_starts()

    COLS = {
        "hours": "Hours",
        "init_leads": "Initial Offer Leads",
        "offer_fus": "Offer Follow-ups",
        "init_dials": "Initial Offer Dials Made",
        "fu_dials": "Offer Followup Dials",
        "sms_sent": "SMS Sent",
        "voicemails": "Voicemails",
        "no_answer": "No Answer",
        "conversations": "Conversations",
        "appointments": "Appointments",
        "send_back_lm": "Send back to LM",
        "init_offers": "Initial Offers",
        "offer_rejected": "Offer Rejected",
        "offer_accepted": "Offer Accepted",
        "offer_accepted_fu": "Offer Accepted on Follow Up",
        "contract_sent": "Contract Sent",
    }

    keys = list(COLS.keys())
    periods = {p: {k: 0 for k in keys} for p in ["today", "week", "mtd", "qtd", "ytd"]}
    for p in periods.values():
        p["hours"] = 0.0

    for row in records:
        d = parse_date(str(row.get("Date", "")))
        if not d:
            continue

        vals = {}
        for k in keys:
            if k == "hours":
                vals[k] = _safe_float(row.get(COLS[k], 0))
            else:
                vals[k] = _safe_int(row.get(COLS[k], 0))

        if d == today:
            for k in keys:
                periods["today"][k] += vals[k]
        if d >= week_start:
            for k in keys:
                periods["week"][k] += vals[k]
        if d >= month_start:
            for k in keys:
                periods["mtd"][k] += vals[k]
        if d >= quarter_start:
            for k in keys:
                periods["qtd"][k] += vals[k]
        if d >= year_start:
            for k in keys:
                periods["ytd"][k] += vals[k]

    def _build_period_summary(p, label):
        d = periods[p]
        total_leads = d["init_leads"] + d["offer_fus"]
        total_dials = d["init_dials"] + d["fu_dials"]
        total_accepted = d["offer_accepted"] + d["offer_accepted_fu"]

        lines = [
            f"*— {label} —*",
            "",
            f"*Volume:*  {d['init_leads']} initial leads  |  {d['offer_fus']} offer FUs  |  {total_leads} total worked  |  {d['hours']:.1f}h",
            f"*Dials:*  {d['init_dials']} initial  |  {d['fu_dials']} FU dials  |  {total_dials} total  |  {d['sms_sent']} SMS  |  {d['voicemails']} VMs",
            "",
            f"*Dial Efficiency:*",
            f"  Dials/Initial Lead:  {_ratio(d['init_dials'], d['init_leads'])}",
            f"  Dials/Offer Follow-up:  {_ratio(d['fu_dials'], d['offer_fus'])}",
            f"  Dials/Conversation:  {_ratio(total_dials, d['conversations'])}",
            "",
            f"*Contact & Conversion:*",
            f"  Conversation Rate:  {_pct(d['conversations'], total_dials)}  ({d['conversations']} convos / {total_dials} dials)",
            f"  No Answer Rate:  {_pct(d['no_answer'], total_dials)}",
            f"  Appointment Rate:  {_pct(d['appointments'], d['conversations'])}  ({d['appointments']} appts / {d['conversations']} convos)",
            "",
            f"*Offer Pipeline:*",
            f"  Initial Offers Made:  {d['init_offers']}",
            f"  Offers/Lead Worked:  {_ratio(d['init_offers'], total_leads)}",
            f"  Conversations/Offer:  {_ratio(d['conversations'], d['init_offers'])}",
            f"  Dials/Offer:  {_ratio(total_dials, d['init_offers'])}",
            "",
            f"*Offer Outcomes:*",
            f"  Accepted (initial):  {d['offer_accepted']}  ({_pct(d['offer_accepted'], d['init_offers'])} of offers)",
            f"  Accepted (follow-up):  {d['offer_accepted_fu']}  |  FU Dials/FU Acceptance:  {_ratio(d['fu_dials'], d['offer_accepted_fu'])}",
            f"  Total Accepted:  {total_accepted}  ({_pct(total_accepted, d['init_offers'])} of offers)",
            f"  Rejected:  {d['offer_rejected']}  ({_pct(d['offer_rejected'], d['init_offers'])} of offers)",
            f"  Contracts Sent:  {d['contract_sent']}  ({_pct(d['contract_sent'], d['init_offers'])} of offers)",
            f"  Leads/Contract:  {_ratio(total_leads, d['contract_sent'])}",
            "",
            f"*Disposition:*",
            f"  Send Back to LM:  {d['send_back_lm']}  ({_pct(d['send_back_lm'], total_leads)} of leads worked)",
        ]
        return "\n".join(lines)

    sections = [
        _build_period_summary("today", "Today"),
        "",
        _build_period_summary("week", "This Week"),
        "",
        _build_period_summary("mtd", "Month to Date"),
        "",
        _build_period_summary("qtd", "Quarter to Date"),
        "",
        _build_period_summary("ytd", "Year to Date"),
    ]
    return "\n".join(sections)


def offers_summary_kpis(sheet):
    """Offers Summary tab — pipeline snapshot, projected profit."""
    try:
        ws = sheet.worksheet("Offers Summary")
        records = ws.get_all_records()
    except Exception as e:
        return f"_Offers Summary: Error reading tab — {e}_"

    total_offers = len(records)
    active = 0
    accepted = 0
    rejected = 0
    under_contract = 0
    total_projected = 0.0
    total_closed_profit = 0.0

    for row in records:
        status = str(row.get("Offer Status", "")).strip().lower()
        if status == "accepted":
            accepted += 1
        elif status == "rejected":
            rejected += 1
        elif status in ("pending", "active", ""):
            active += 1

        uc = str(row.get("Under Contract", "")).strip()
        if uc and uc.lower() not in ("", "no", "n/a"):
            under_contract += 1

        proj = row.get("Projected Profit", 0)
        try:
            proj = float(str(proj).replace("$", "").replace(",", "")) if proj else 0
            total_projected += proj
        except ValueError:
            pass

        profit = row.get("Profit", 0)
        try:
            profit = float(str(profit).replace("$", "").replace(",", "")) if profit else 0
            total_closed_profit += profit
        except ValueError:
            pass

    lines = [
        f"*Total Offers:* {total_offers}",
        f"*Active/Pending:* {active}  |  *Accepted:* {accepted}  |  *Rejected:* {rejected}",
        f"*Under Contract:* {under_contract}",
        f"*Projected Profit (pipeline):* ${total_projected:,.0f}",
        f"*Closed Profit:* ${total_closed_profit:,.0f}",
    ]
    return "\n".join(lines)


def costs_kpis(sheet):
    """Costs tab — spend today/this week by channel."""
    try:
        ws = sheet.worksheet("Costs")
        records = ws.get_all_records()
    except Exception as e:
        return f"_Costs: Error reading tab — {e}_"

    today, week_start = get_today_and_week()

    spend_today = 0.0
    spend_week = 0.0
    channel_week = defaultdict(float)

    for row in records:
        d = parse_date(str(row.get("Date", "")))
        cost = row.get("Cost", 0)
        try:
            cost = float(str(cost).replace("$", "").replace(",", "")) if cost else 0
        except ValueError:
            cost = 0

        channel = str(row.get("Channel", "Unknown")).strip()

        if d == today:
            spend_today += cost
        if d and d >= week_start:
            spend_week += cost
            channel_week[channel] += cost

    lines = [
        f"*Total Spend:* ${spend_today:,.0f} today / ${spend_week:,.0f} this week",
    ]
    if channel_week:
        breakdown = " | ".join(f"{ch}: ${amt:,.0f}" for ch, amt in sorted(channel_week.items()))
        lines.append(f"*By Channel:* {breakdown}")

    return "\n".join(lines)


def expenses_kpis(sheet):
    """Expenses tab — total expenses this week."""
    try:
        ws = sheet.worksheet("Expenses")
        records = ws.get_all_records()
    except Exception as e:
        return f"_Expenses: Error reading tab — {e}_"

    today, week_start = get_today_and_week()

    exp_today = 0.0
    exp_week = 0.0
    cat_week = defaultdict(float)

    for row in records:
        d = parse_date(str(row.get("DATE", "")))
        amt = row.get("EXPENSE", 0)
        try:
            amt = float(str(amt).replace("$", "").replace(",", "")) if amt else 0
        except ValueError:
            amt = 0

        cat = str(row.get("CATEGORY", "Other")).strip()

        if d == today:
            exp_today += amt
        if d and d >= week_start:
            exp_week += amt
            cat_week[cat] += amt

    lines = [
        f"*Total Expenses:* ${exp_today:,.0f} today / ${exp_week:,.0f} this week",
    ]
    if cat_week:
        breakdown = " | ".join(f"{c}: ${a:,.0f}" for c, a in sorted(cat_week.items()))
        lines.append(f"*By Category:* {breakdown}")

    return "\n".join(lines)


def _text_to_blocks(text, max_chars=2900):
    """Split long mrkdwn text into multiple section blocks to stay under Slack's 3000-char limit."""
    if len(text) <= max_chars:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    blocks = []
    # Split on the period separator lines (blank lines between periods)
    chunks = text.split("\n\n*— ")
    current = chunks[0]

    for chunk in chunks[1:]:
        candidate = current + "\n\n*— " + chunk
        if len(candidate) > max_chars:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": current}})
            current = "*— " + chunk
        else:
            current = candidate

    if current:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": current}})

    return blocks


def build_slack_message(sheet):
    """Build the full Slack message from all tabs."""
    _now = datetime.now()
    today = f"{_now:%A, %B} {_now.day}, {_now:%Y}"  # Windows-safe (no %-d)

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📊 {COMPANY_NAME} — Sheet KPIs (Daily Update) — {today}"}
        },
        {"type": "divider"},
    ]

    # Executive Summary
    blocks += _text_to_blocks(f"*🎯 EXECUTIVE SUMMARY*\n{executive_summary_kpis(sheet)}")
    blocks.append({"type": "divider"})

    # Leads Summary (detailed — will be split into multiple blocks)
    blocks.append({"type": "header", "text": {"type": "plain_text", "text": "🏠 LEADS SUMMARY"}})
    blocks += _text_to_blocks(leads_summary_kpis(sheet))
    blocks.append({"type": "divider"})

    # SmrtDialer
    blocks += _text_to_blocks(f"*📞 SMRTDIALER*\n{smrtdialer_kpis(sheet)}")
    blocks.append({"type": "divider"})

    # First to Market (will be split into multiple blocks)
    blocks.append({"type": "header", "text": {"type": "plain_text", "text": "🏃 FIRST TO MARKET"}})
    blocks += _text_to_blocks(first_to_market_kpis(sheet))
    blocks.append({"type": "divider"})

    # Exhausted
    blocks += _text_to_blocks(f"*♻️ EXHAUSTED*\n{exhausted_kpis(sheet)}")
    blocks.append({"type": "divider"})

    # Lead Management (detailed — will be split into multiple blocks)
    blocks.append({"type": "header", "text": {"type": "plain_text", "text": "📋 LEAD MANAGEMENT"}})
    blocks += _text_to_blocks(lead_management_kpis(sheet))
    blocks.append({"type": "divider"})

    # Acquisition (detailed — will be split into multiple blocks)
    blocks.append({"type": "header", "text": {"type": "plain_text", "text": "🤝 ACQUISITION"}})
    blocks += _text_to_blocks(acquisition_kpis(sheet))
    blocks.append({"type": "divider"})

    # Offers Summary
    blocks += _text_to_blocks(f"*💰 OFFERS SUMMARY*\n{offers_summary_kpis(sheet)}")
    blocks.append({"type": "divider"})

    # Marketing Costs
    blocks += _text_to_blocks(f"*📣 MARKETING COSTS*\n{costs_kpis(sheet)}")
    blocks.append({"type": "divider"})

    # Expenses
    blocks += _text_to_blocks(f"*💸 EXPENSES*\n{expenses_kpis(sheet)}")

    return {"blocks": blocks}


def post_to_slack(payload):
    """Send the message to Slack."""
    resp = requests.post(
        SLACK_WEBHOOK_URL,
        json=payload,
        headers={"Content-Type": "application/json"},
    )
    if resp.status_code != 200:
        raise Exception(f"Slack POST failed ({resp.status_code}): {resp.text}")
    print(f"✅ Slack message posted successfully at {datetime.now().strftime('%I:%M %p')}")


def main():
    print("Connecting to Google Sheets...")
    sheet = get_sheet()
    print("Building KPI summary...")
    payload = build_slack_message(sheet)
    print("Posting to Slack...")
    post_to_slack(payload)


if __name__ == "__main__":
    main()
