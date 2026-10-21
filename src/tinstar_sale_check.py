"""Weekly Tulsa County sheriff-sale check against the CRM (Jeff, 2026-10-21).

The Sheriff's "Foreclosure Sales List" (tcso.org -> tinstar.io/reports) shows
every sale for the next six weeks. A sale date is the first sign an Oklahoma
foreclosure is actually going to auction, so each Monday we check which of
those properties already live in the CRM and put the date on their record.

    python src/tinstar_sale_check.py run --dry       # fetch + match, writes nothing
    python src/tinstar_sale_check.py run             # + Message Board posts on exact matches
    python src/tinstar_sale_check.py summary         # the latest findings, short
    python src/tinstar_sale_check.py post --confirm 4,7      # post to flagged findings Jeff OKed
    python src/tinstar_sale_check.py post --confirm 4:<property uuid>   # when a flag has 2+ records
    python src/tinstar_sale_check.py scheduled       # what the Windows task runs (see tools/)

How it works:
  - THE CRM IS READ-ONLY HERE except Message Board posts (Jeff, 2026-10-21).
    The only DataSift write this module may call is post_property_message_board
    (the PROPERTY record's board). NEVER the owner record: Jeff, 2026-10-21,
    after the first posts went to owner boards: "We never, ever, ever write
    anything to the owner records without explicitly saying so." No
    tags, lists, statuses, fields, notes, creates or deletes. A test scans
    this file and fails if any other datasift_api call appears.
  - The report page loads its list from tinstar.io/api/cases (plain JSON, no
    login, no CAPTCHA, verified 2026-10-21). That feed also carries every case
    back to 2020 plus winning bidders' names, phones and emails. Only the
    columns the PUBLIC report shows are kept (KEEP below); nothing else is
    stored.
  - Matching is by ADDRESS, never case number (not every CRM record has one).
    Each sale is searched by its house number only (a targeted lookup, never
    a sweep of the account), both property buckets, then compared client-side
    through datasift_api's own normalizer.
  - Exactly one record with the same normalized street and city = MATCH: it
    gets a Message Board post with the sale date and appraisal. Anything that
    looks like the same house but differs (street type, trailing direction,
    spelling, city, or two records at one address) is FLAGGED and never
    posted until Jeff confirms it. In Tulsa "E 66th St" and "E 66th Pl" are
    different streets, so a format difference is never assumed harmless.
  - A record is posted once per sale date. A moved date gets a "date changed"
    post; a sale we posted that is later recalled gets one recall post.
  - Zero upcoming sales is a FAILURE, not a quiet week: the list always holds
    six weeks of sales.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

import requests

import config
import datasift_api as _api

logger = logging.getLogger("tinstar_sale_check")

FEED_URL = "https://www.tinstar.io/api/cases"
REPORT_URL = "https://www.tinstar.io/reports"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# The columns the public report shows (plus noSale). Bidder contact details,
# inspection photos and the rest of the feed are deliberately dropped.
KEEP = ("id", "caseNumber", "address", "city", "state", "zipCode", "plaintiff",
        "defendant", "saleDate", "appraisalTinstar", "recalled", "noSale")

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = Path(config.OUTPUT_DIR) / "tinstar"
STATE_PATH = OUT_DIR / "state.json"
LATEST_PATH = OUT_DIR / "latest.json"
LOG_PATH = OUT_DIR / "check.log"
TOAST_PS1 = REPO / "tools" / "toast_notify.ps1"

SALE_MARKER = "SHERIFF SALE SET"
MOVED_MARKER = "SHERIFF SALE DATE CHANGED"
RECALL_MARKER = "SHERIFF SALE RECALLED"
NEAR_RATIO = 0.80          # street spelling similarity that still counts as "maybe the same"
SEARCH_LIMIT = 100
RUN_HOUR = 9               # Monday 9 AM, business-local (this PC's clock)
FAIL_NOTIFY_AFTER = 3      # failed attempts in one week before the failure toast


# ── Fetch ─────────────────────────────────────────────────────────────


def fetch_sales(today: date, *, retries: int = 3) -> list[dict]:
    """Upcoming, publicly listed sales (sale date today or later), sorted."""
    last: Exception | None = None
    rows = None
    for attempt in range(retries):
        try:
            r = requests.get(FEED_URL, headers={"User-Agent": UA, "Accept": "application/json"},
                             timeout=90)
            r.raise_for_status()
            rows = r.json()
            break
        except (requests.RequestException, ValueError) as e:
            last = e
            logger.warning("TinStar fetch attempt %d failed: %s", attempt + 1, e)
            if attempt + 1 < retries:
                time.sleep(30 * (attempt + 1))
    if rows is None:
        raise RuntimeError(f"could not read the TinStar list: {last}")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("TinStar returned no cases at all - the feed changed or is down")
    return filter_upcoming(rows, today)


def filter_upcoming(rows: list[dict], today: date) -> list[dict]:
    iso = today.isoformat()
    sales = [{k: r.get(k) for k in KEEP} for r in rows
             if (r.get("saleDate") or "") >= iso and not r.get("hideFromReports")]
    if not sales:
        raise RuntimeError("0 upcoming sales on the TinStar list - it always holds six weeks, "
                           "so the feed changed or TinStar is down")
    sales.sort(key=lambda s: (s["saleDate"], s.get("address") or ""))
    return sales


def clean_street(raw: str) -> str:
    return " ".join((raw or "").replace(".", " ").split())


# ── Match ─────────────────────────────────────────────────────────────


def _search(prefix: str, ptype: str, *, tries: int = 4) -> list[dict]:
    """search_by_address with retries: it has none of its own, and one 502
    from the CRM stopped the first live run (2026-10-21)."""
    for attempt in range(tries):
        try:
            return _api.search_by_address(prefix, property_type=ptype, limit=SEARCH_LIMIT)
        except (_api.DataSiftAPIError, requests.RequestException) as e:
            if attempt + 1 == tries:
                raise
            logger.warning("CRM search %r failed (%s), retrying", prefix, str(e)[:80])
            time.sleep(10 * (attempt + 1))
    return []


def _candidates(street: str) -> list[dict]:
    """CRM records whose house number equals this sale's. Targeted: one
    house-number search per bucket, never a sweep."""
    toks = street.split()
    if not toks or not toks[0].isdigit():
        return []
    house = toks[0]
    found: dict[str, dict] = {}
    for ptype in ("clean", "incomplete"):
        rows = _search(house, ptype)
        if len(rows) >= SEARCH_LIMIT and len(toks) > 1:
            # a short house number is a prefix of many others; narrow it
            rows = _search(f"{house} {toks[1]}", ptype)
        for r in rows:
            st = ((r.get("address") or {}).get("street") or "").split()
            if st and st[0] == house and r.get("uuid"):
                found[r["uuid"]] = r
    return list(found.values())


def classify(sale: dict, cands: list[dict]) -> tuple[str, list[dict]]:
    """("match"|"duplicate"|"flag"|"none", [{"record": r, "reasons": [...]}])."""
    street = clean_street(sale.get("address"))
    city = (sale.get("city") or "").strip().lower()
    zip5 = (sale.get("zipCode") or "")[:5]
    want = _api._norm_street(street)
    want_bare = _api._bare_street(want)
    exact, near = [], []
    for r in cands:
        a = r.get("address") or {}
        got = _api._norm_street(a.get("street"))
        rc = (a.get("city") or "").strip().lower()
        same_city = not rc or not city or rc == city
        if got == want and same_city:
            exact.append({"record": r, "reasons": []})
            continue
        reasons = []
        if got == want:
            pass
        elif _api._bare_street(got) == want_bare:
            reasons.append("street type or direction differs")
        else:
            ratio = SequenceMatcher(None, _api._bare_street(got), want_bare).ratio()
            if ratio < NEAR_RATIO:
                continue
            reasons.append(f"street spelled differently ({int(ratio * 100)}% alike)")
        if not same_city:
            reasons.append(f"city differs ({a.get('city')} vs {sale.get('city')})")
        rz = (a.get("postal_code") or a.get("zip") or "")[:5]
        if rz and zip5:
            reasons.append("same zip" if rz == zip5 else f"zip differs ({rz} vs {zip5})")
        near.append({"record": r, "reasons": reasons})
    if len(exact) == 1:
        return "match", exact
    if len(exact) > 1:
        for e in exact:
            e["reasons"].append(f"{len(exact)} CRM records share this address")
        return "duplicate", exact
    if near:
        return "flag", near
    return "none", []


def _record_view(r: dict, reasons: list[str], sale: dict) -> dict:
    """What we keep about a CRM record: property uuid, address, owner NAME.
    The owner's uuid is deliberately not kept: posts go to the property."""
    detail = r
    if not (r.get("owner") or {}).get("last_name"):
        detail = _api.get_property(r["uuid"]) or r
    owner = detail.get("owner") or {}
    a = detail.get("address") or r.get("address") or {}
    name = " ".join(x for x in (owner.get("first_name"), owner.get("last_name")) if x) \
        or owner.get("company") or ""
    defendant = (sale.get("defendant") or "").upper()
    last = (owner.get("last_name") or "").upper()
    if reasons is not None and last and len(last) > 2:
        reasons.append("owner name is on the case" if last in defendant
                       else "owner name not on the case")
    return {"uuid": r["uuid"],
            "street": a.get("street"), "city": a.get("city"),
            "zip": a.get("postal_code") or a.get("zip"), "owner": name,
            "reasons": reasons}


# ── Message Board ─────────────────────────────────────────────────────


def _fmt_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%A}, {d:%B} {d.day}, {d.year}"


def board_text(sale: dict, checked: date, *, moved_from: str | None = None) -> str:
    head = MOVED_MARKER if moved_from else SALE_MARKER
    lines = [f"{head} (Tulsa County Sheriff, TinStar list)",
             f"Sale date: {_fmt_date(sale['saleDate'])}, 10:00 AM, Tulsa County HQ, 218 W 6th St"]
    if moved_from:
        lines.append(f"Was: {_fmt_date(moved_from)}")
    lines += [f"Appraisal: {sale.get('appraisalTinstar') or 'not listed'}",
              f"Case: {sale.get('caseNumber') or 'not listed'}",
              f"Checked {checked.isoformat()}"]
    return "\n".join(lines)


def recall_text(sale: dict, checked: date) -> str:
    return "\n".join([f"{RECALL_MARKER} (Tulsa County Sheriff, TinStar list)",
                      f"The {_fmt_date(sale['saleDate'])} sale is marked RECALLED.",
                      f"Case: {sale.get('caseNumber') or 'not listed'}",
                      f"Checked {checked.isoformat()}"])


def _post_verified(property_uuid: str, text: str) -> None:
    """Post to the PROPERTY's Message Board and read it back."""
    _api.post_property_message_board(property_uuid, text)
    board = _api.get_property_message_board(property_uuid)
    if not any((m.get("message") or "").strip() == text.strip() for m in board):
        raise RuntimeError(f"Message Board post did not land on property {property_uuid}")


def post_sale(rec: dict, sale: dict, state: dict, checked: date) -> str:
    """Post the sale to one record. Returns what happened (for the summary)."""
    posted = state.setdefault("posted", {})
    prefix = f"{rec['uuid']}|{sale.get('caseNumber')}|"
    key = prefix + sale["saleDate"]
    if key in posted:
        return f"already posted {posted[key]['on']}"
    if sale.get("recalled"):
        return "not posted: sale already recalled"
    earlier = sorted(k.split("|")[2] for k in posted if k.startswith(prefix))
    sale_line = f"Sale date: {_fmt_date(sale['saleDate'])}"
    board = _api.get_property_message_board(rec["uuid"])
    if any(sale_line in (m.get("message") or "") for m in board):
        posted[key] = {"on": checked.isoformat(), "note": "already on the board"}
        return "already on the board"
    text = board_text(sale, checked, moved_from=earlier[-1] if earlier else None)
    _post_verified(rec["uuid"], text)
    posted[key] = {"on": checked.isoformat(), "board": "property"}
    return "posted (date changed)" if earlier else "posted"


def post_recalls(sales: list[dict], state: dict, checked: date) -> list[str]:
    """One recall post per sale we already posted that is now recalled."""
    done = state.setdefault("recall_posted", {})
    out = []
    by_key = {f"{s.get('caseNumber')}|{s['saleDate']}": s for s in sales if s.get("recalled")}
    for key, info in list(state.get("posted", {}).items()):
        uuid, case, sdate = key.split("|")
        sale = by_key.get(f"{case}|{sdate}")
        if not sale or key in done or info.get("board") != "property":
            continue
        _post_verified(uuid, recall_text(sale, checked))
        done[key] = checked.isoformat()
        out.append(f"{clean_street(sale.get('address'))} ({case})")
    return out


# ── State + run ───────────────────────────────────────────────────────


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def run(*, dry: bool, today: date | None = None) -> dict:
    today = today or date.today()
    if dry:
        _api.set_dry_run(True)
    sales = fetch_sales(today)
    state = load_state()
    findings = []
    for sale in sales:
        street = clean_street(sale.get("address"))
        try:
            status, hits = classify(sale, _candidates(street))
            recs = [_record_view(h["record"], h["reasons"], sale) for h in hits]
            err = None
        except Exception as e:      # a failed lookup is not "not in the CRM"
            logger.exception("CRM lookup failed for %s", street)
            status, recs, err = "error", [], str(e)[:200]
        f = {"n": len(findings) + 1, "status": status, "sale": sale,
             "street": street, "records": recs}
        if err:
            f["error"] = err
        dismissed = state.get("dismissed", {})
        if status in ("flag", "duplicate") and recs and all(
                f"{r['uuid']}|{sale.get('caseNumber')}" in dismissed for r in recs):
            status = f["status"] = "dismissed"   # Jeff said skip it; don't ask again
        posted = state.get("posted", {})
        if status in ("flag", "duplicate"):
            done = [r for r in recs
                    if f"{r['uuid']}|{sale.get('caseNumber')}|{sale['saleDate']}" in posted]
            if done:                              # Jeff confirmed it on an earlier check
                status = f["status"] = "confirmed"
                for r in done:
                    r["post"] = "already posted (you confirmed)"
        if status == "match":
            if dry:
                recs[0]["post"] = "dry run: would post" if not sale.get("recalled") \
                    else "not posted: sale already recalled"
            else:
                try:
                    recs[0]["post"] = post_sale(recs[0], sale, state, today)
                except Exception as e:      # one bad record must not stop the rest
                    logger.exception("post failed for %s", street)
                    recs[0]["post"] = f"POST FAILED: {e}"
        findings.append(f)
    recalls = [] if dry else post_recalls(sales, state, today)
    if not dry:
        save_state(state)
    result = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": dry, "source": REPORT_URL,
        "sales": len(sales),
        "sale_dates": sorted({s["saleDate"] for s in sales}),
        "recalled": sum(1 for s in sales if s.get("recalled")),
        "counts": {k: sum(1 for f in findings if f["status"] == k)
                   for k in ("match", "duplicate", "flag", "none", "error", "dismissed")},
        "recall_posts": recalls,
        "findings": findings,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = today.isoformat() + ("_dry" if dry else "")
    for p in (OUT_DIR / f"run_{stamp}.json", LATEST_PATH):
        p.write_text(json.dumps(result, indent=2), encoding="utf-8")
    errors = result["counts"]["error"]
    if errors > max(3, len(sales) // 10):
        # The CRM is having a bad day: don't mark the week done on a half check.
        raise RuntimeError(f"{errors} of {len(sales)} CRM lookups failed")
    return result


# ── Summary ───────────────────────────────────────────────────────────


def summary_text(res: dict) -> str:
    # recount from the findings: confirm/dismiss change statuses after the check
    c = {k: sum(1 for f in res["findings"] if f["status"] == k)
         for k in ("match", "duplicate", "flag", "none", "error", "dismissed", "confirmed")}
    dates = res["sale_dates"]
    failed = f", {c['error']} lookups FAILED (not checked)" if c.get("error") else ""
    out = [f"TinStar check {res['checked_at'][:16].replace('T', ' ')}"
           f"{' (dry run: exact matches not posted)' if res.get('dry_run') else ''}",
           f"{res['sales']} sales listed, {dates[0]} to {dates[-1]} ({res['recalled']} recalled)",
           f"In the CRM: {c['match']} exact, {c['flag'] + c['duplicate']} need your OK, "
           f"{c['confirmed']} you confirmed, {c['dismissed']} you said skip, "
           f"{c['none']} not in the CRM" + failed, ""]
    for f in res["findings"]:
        if f["status"] in ("none", "dismissed"):
            continue
        if f["status"] == "error":
            out.append(f"#{f['n']} {f['street']}, {f['sale'].get('city')} | CRM lookup FAILED, "
                       f"not checked ({f.get('error', '')[:80]})")
            continue
        s = f["sale"]
        tag = "RECALLED " if s.get("recalled") else ""
        head = (f"#{f['n']} {f['street']}, {s.get('city')} | {tag}sale {s['saleDate']} | "
                f"appraisal {s.get('appraisalTinstar') or '?'} | {s.get('caseNumber')}")
        out.append(head)
        for r in f["records"]:
            line = f"    CRM: {r['street']}, {r['city']} | owner {r['owner'] or '?'}"
            if len(f["records"]) > 1:
                line += f" | uuid {r['uuid']}"
            if r.get("reasons"):
                line += " | " + "; ".join(r["reasons"])
            if r.get("post"):
                line += f" | {r['post']}"
            out.append(line)
        if f["status"] in ("flag", "duplicate"):
            out.append(f"    -> needs your OK (post --confirm {f['n']}"
                       f"{':<uuid>' if len(f['records']) > 1 else ''})")
    if res.get("recall_posts"):
        out += ["", "Recall posts written: " + ", ".join(res["recall_posts"])]
    return "\n".join(out)


def load_latest() -> dict:
    try:
        return json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SystemExit("No TinStar check has run yet (output/tinstar/latest.json missing).")


def dismiss_findings(spec: str) -> list[str]:
    """Jeff said skip these flags: never post, never flag again for this sale."""
    res = load_latest()
    by_n = {f["n"]: f for f in res["findings"]}
    state = load_state()
    gone = state.setdefault("dismissed", {})
    out = []
    for item in [x.strip() for x in spec.split(",") if x.strip()]:
        f = by_n.get(int(item))
        if not f or not f["records"]:
            out.append(f"#{item}: no such finding with a CRM record")
            continue
        for r in f["records"]:
            gone[f"{r['uuid']}|{f['sale'].get('caseNumber')}"] = date.today().isoformat()
        f["status"] = "dismissed"
        out.append(f"#{item} {f['street']}: dismissed, will not be posted or flagged again")
    save_state(state)
    LATEST_PATH.write_text(json.dumps(res, indent=2), encoding="utf-8")
    return out


def confirm_posts(spec: str, *, today: date | None = None) -> list[str]:
    """Post to flagged findings Jeff confirmed: "4,7" or "4:<uuid>"."""
    today = today or date.today()
    res = load_latest()
    by_n = {f["n"]: f for f in res["findings"]}
    state = load_state()
    out = []
    for item in [x.strip() for x in spec.split(",") if x.strip()]:
        n_s, _, uuid = item.partition(":")
        f = by_n.get(int(n_s))
        if not f or not f["records"]:
            out.append(f"#{n_s}: no such finding with a CRM record")
            continue
        recs = [r for r in f["records"] if not uuid or r["uuid"] == uuid]
        if len(recs) != 1:
            out.append(f"#{n_s}: {len(f['records'])} records, say which one (#{n_s}:<uuid>)")
            continue
        try:
            msg = post_sale(recs[0], f["sale"], state, today)
        except Exception as e:
            msg = f"POST FAILED: {e}"
        recs[0]["post"] = msg + " (you confirmed)"
        if not msg.startswith("POST FAILED"):
            f["status"] = "confirmed"
        out.append(f"#{n_s} {f['street']}: {msg}")
    save_state(state)
    LATEST_PATH.write_text(json.dumps(res, indent=2), encoding="utf-8")
    return out


# ── Schedule + notification ───────────────────────────────────────────


def due_cycle(now: datetime) -> str:
    """The Monday-9AM cycle `now` belongs to (ISO date of that Monday)."""
    monday = (now - timedelta(days=now.weekday())).replace(hour=RUN_HOUR, minute=0,
                                                           second=0, microsecond=0)
    if now < monday:
        monday -= timedelta(days=7)
    return monday.date().isoformat()


def notify(title: str, body: str) -> None:
    """Toast that stays on screen until clicked (see tools/toast_notify.ps1)."""
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-WindowStyle", "Hidden", "-File", str(TOAST_PS1),
                        "-Title", title, "-Body", body],
                       timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        logger.exception("notification failed")


def scheduled(now: datetime | None = None) -> int:
    """Called every 15 min / on logon / unlock / wake. Silent unless due."""
    now = now or datetime.now()
    cycle = due_cycle(now)
    state = load_state()
    if state.get("last_cycle_done") == cycle:
        return 0
    handler = _setup_file_log()
    try:
        return _scheduled_run(now, cycle, state)
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()


def _scheduled_run(now: datetime, cycle: str, state: dict) -> int:
    logger.info("cycle %s due, running", cycle)
    try:
        res = run(dry=False, today=now.date())
    except Exception as e:
        fails = state.setdefault("fails", {})
        fails[cycle] = fails.get(cycle, 0) + 1
        save_state(state)
        logger.exception("check failed (%d this week)", fails[cycle])
        if fails[cycle] == FAIL_NOTIFY_AFTER:
            notify("TinStar sale check FAILED",
                   f"{str(e)[:150]}. Open a Claude session and ask about the TinStar check.")
        return 1
    state = load_state()
    state["last_cycle_done"] = cycle
    save_state(state)
    c = res["counts"]
    bad = sum(1 for f in res["findings"] for r in f["records"]
              if str(r.get("post", "")).startswith("POST FAILED"))
    posted = sum(1 for f in res["findings"] for r in f["records"]
                 if str(r.get("post", "")).startswith("posted"))
    lookup_note = f", {c['error']} lookups failed" if c.get("error") else ""
    body = (f"{res['sales']} sales checked. {c['match']} already in your CRM "
            f"({posted} new posts), {c['flag'] + c['duplicate']} need your OK"
            f"{f', {bad} posts FAILED' if bad else ''}"
            f"{lookup_note}. "
            "Open a Claude session and ask: TinStar findings.")
    logger.info("done: %s", body)
    notify("TinStar sheriff sale check done", body)
    return 0


def _setup_file_log() -> logging.Handler:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    h = logging.FileHandler(LOG_PATH, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(h)
    logging.getLogger().setLevel(logging.INFO)
    return h


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry", action="store_true", help="fetch + match only, write nothing")
    sub.add_parser("summary")
    d = sub.add_parser("dismiss")
    d.add_argument("findings", help='finding numbers Jeff said to skip, e.g. "54"')
    c = sub.add_parser("post")
    c.add_argument("--confirm", required=True, help='finding numbers, e.g. "4,7" or "4:<uuid>"')
    n = sub.add_parser("notify-test")
    n.add_argument("--body", default="Test notification. Click to dismiss.")
    sub.add_parser("scheduled")
    a = p.parse_args(argv)

    if a.cmd == "scheduled":
        return scheduled()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if a.cmd == "run":
        print(summary_text(run(dry=a.dry)))
    elif a.cmd == "summary":
        print(summary_text(load_latest()))
    elif a.cmd == "dismiss":
        print("\n".join(dismiss_findings(a.findings)))
    elif a.cmd == "post":
        print("\n".join(confirm_posts(a.confirm)))
    elif a.cmd == "notify-test":
        notify("TinStar sheriff sale check", a.body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
