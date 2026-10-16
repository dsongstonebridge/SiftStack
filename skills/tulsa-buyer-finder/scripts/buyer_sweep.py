"""Read-only SiftMap sweep: who bought single-family homes in Jeff's market in the last N days.

THE ONLY TWO NETWORK CALLS IN THIS FILE:
  POST https://map.reisift.io/properties/search/        a search; nothing is saved anywhere
  GET  https://map.reisift.io/properties/detail/<id>/   one property's deed / owner detail
Api-Key auth only, no fallback login. Nothing here can write to DataSift. Before every run, grep
this file for: datasift_api|add-properties|/filters/|PATCH|PUT|DELETE|apiv2|/api/internal
and expect zero hits.

Phases (run in order, each one stops so Claude can report to Jeff):
  python buyer_sweep.py probe   [--counties Tulsa] [--days 365]
  python buyer_sweep.py sweep   [--counties Tulsa,Wagoner] [--zips 74105,74112] [--days 365] [--out DIR]
  python buyer_sweep.py hydrate [--out DIR] [--scope investor|all]

Proven on Cherokee County OK (Park Hill 74451) on 2026-10-16: 3 controls, 6 searches, 241 lookups.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

SIFTSTACK = Path(r"C:\Users\dsong\OneDrive\Desktop\SiftStack")
ENV = SIFTSTACK / ".env"
BASE = "https://map.reisift.io"
PAGE = 250

# Tulsa metro (MSA) counties. Name -> FIPS.
COUNTIES = {
    "Tulsa": "40143", "Wagoner": "40145", "Rogers": "40131", "Creek": "40037",
    "Osage": "40113", "Okmulgee": "40111", "Pawnee": "40117",
    # Proven reference county from the first run. Not part of the market.
    "Cherokee": "40021",
}
ST = "OK"
# SiftMap's "last sale was an investor transaction" key. Takes a LIST (a comma string 400s).
# "flip" = the investor's EXIT was the last sale: the current owner is a retail buyer and the
# investor is the SELLER. That is why flips get their own pass: corporateOwned misses them.
K_TXN = "extra_last_sale_investor_transaction_type"

WORLD = [{"lon": -17.89461189115002, "lat": 72.08452694723852},
         {"lon": -17.89461189115002, "lat": -13.881763595427103},
         {"lon": -163.30518783395442, "lat": -13.881763595427103},
         {"lon": -163.30518783395442, "lat": 72.08452694723852}]


def api_key() -> str:
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATASIFT_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("DATASIFT_API_KEY not found in " + str(ENV))


KEY = None
_last = [0.0]
CALLS = {"search": 0, "detail": 0}


def _call(method: str, path: str, body=None, min_gap: float = 0.3):
    global KEY
    if KEY is None:
        KEY = api_key()
    hdr = {"accept": "application/json", "content-type": "application/json",
           "authorization": "Api-Key " + KEY,
           "origin": "https://beta.reisift.io", "referer": "https://beta.reisift.io/"}
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(7):
        gap = time.time() - _last[0]
        if gap < min_gap:
            time.sleep(min_gap - gap)
        _last[0] = time.time()
        req = urllib.request.Request(BASE + path, data=data, headers=hdr, method=method)
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 500, 502, 503, 504) and attempt < 6:
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    wait = float(ra) if ra else 4.0 * (attempt + 1)
                except ValueError:
                    wait = 4.0 * (attempt + 1)
                print(f"  HTTP {e.code}, waiting {wait:.0f}s", flush=True)
                time.sleep(wait)
                continue
            raise SystemExit(f"HTTP {e.code} on {method} {path}: {txt}")
        except urllib.error.URLError as e:
            if attempt < 6:
                time.sleep(3.0 * (attempt + 1))
                continue
            raise SystemExit(f"network error on {path}: {e}")
    raise SystemExit("retries exhausted on " + path)


def search(county: str, filters: dict, page: int = 1) -> dict:
    CALLS["search"] += 1
    fips = COUNTIES[county]
    title = f"{county} County, {ST}"
    body = {"result_index": 1 + (page - 1) * PAGE,   # a ROW OFFSET, not a page number
            "with_boundaries": False,
            "filters": filters,
            "addresses": [{"state": ST, "title": title, "value": title, "county": county,
                           "searchType": "county",
                           "counties": [{"fips": fips, "county_name": county}],
                           # the bare county name; "Tulsa County, OK" here returns ZERO
                           "search": county, "type": "county"}],
            "polygon": WORLD}
    return _call("POST", "/properties/search/", body)


def detail(pid) -> dict:
    CALLS["detail"] += 1
    return _call("GET", f"/properties/detail/{pid}/")


def base_filters() -> dict:
    return {"type_single_family": True,
            "extra_last_sale_value_min": 1000,          # gift deeds are not purchases
            "extra_is_last_sale_interfamily": False}    # family paper is not a purchase


def since(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def parse_counties(s: str) -> list:
    out = []
    for c in [x.strip().title() for x in s.split(",") if x.strip()]:
        if c not in COUNTIES:
            raise SystemExit(f"unknown county {c!r}; known: {', '.join(COUNTIES)}")
        out.append(c)
    return out


def zip_of(address: str) -> str:
    tail = str(address or "").strip()[-5:]
    return tail if tail.isdigit() else ""


# --------------------------------------------------------------- phases ----

def probe(a):
    """Three control searches per county. ALL must behave or the run stops."""
    ok = True
    for c in parse_counties(a.counties):
        f = base_filters()
        n_all = search(c, f).get("total_results")
        n_win = search(c, {**f, "extra_last_sale_date_min": since(a.days)}).get("total_results")
        bogus = search(c, {**f, "zz_not_a_real_filter_key": True})
        n_bog = bogus.get("total_results")
        good = bool(n_all) and n_win is not None and n_win < n_all and n_bog == n_all
        ok &= good
        print(f"{c} County: single-family {n_all:,} | sold in last {a.days} days {n_win:,} | "
              f"bogus-key control {n_bog:,}  -> {'PASS' if good else 'FAIL'}")
        rows = bogus.get("data") or []
        print("  sample:", "; ".join(str(r.get("address")) for r in rows[:4]))
    print("CONTROLS", "PASSED" if ok else "FAILED - stop and tell Jeff; do not sweep")
    print("calls:", CALLS)
    sys.exit(0 if ok else 3)


def _page_all(county, filters, cap):
    seen, page, total = {}, 1, None
    while True:
        r = search(county, filters, page)
        if total is None:
            total = r.get("total_results") or 0
        batch = r.get("data") or []
        if not batch:
            break
        for row in batch:
            seen[str(row.get("id"))] = row
        page += 1
        if len(seen) >= total or page > cap:
            break
    if total and len(seen) < total:
        print(f"  WARNING {county}: kept {len(seen)} of {total} (page cap {cap}); say so in the report")
    return list(seen.values()), total


def sweep(a):
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    zips = {z.strip() for z in (a.zips or "").split(",") if z.strip()}
    win = {**base_filters(), "extra_last_sale_date_min": since(a.days)}
    rows = {}
    summary = {}
    for c in parse_counties(a.counties):
        allrows, total = _page_all(c, win, a.max_pages)
        flips, ftotal = _page_all(c, {**win, K_TXN: ["flip"]}, a.max_pages)
        flip_ids = {str(r.get("id")) for r in flips}
        for r in allrows + flips:
            r["_county"] = c
            r["_flip_exit"] = str(r.get("id")) in flip_ids
            rows[str(r.get("id"))] = r
        summary[c] = {"sold_in_window": total, "flip_exits": ftotal}
    keep = [r for r in rows.values() if not zips or zip_of(r.get("address")) in zips]
    meta = {"built": date.today().isoformat(), "days": a.days, "since": since(a.days),
            "counties": parse_counties(a.counties), "zips": sorted(zips), "summary": summary,
            "rows": len(keep), "corporate_owned": sum(1 for r in keep if r.get("corporateOwned")),
            "flip_exits": sum(1 for r in keep if r.get("_flip_exit")),
            "already_in_crm": sum(1 for r in keep if r.get("saved_uuid"))}
    (out / "rows.json").write_text(json.dumps(keep, indent=1, default=str), encoding="utf-8")
    (out / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    to_hydrate = sum(1 for r in keep if r.get("corporateOwned") or r.get("_flip_exit"))
    print(json.dumps(meta, indent=1))
    print(f"\nSTOP HERE AND REPORT TO JEFF. Detail lookups needed: {to_hydrate} (investor scope) "
          f"or {len(keep)} (all). At ~3 per second that is about {to_hydrate // 180 + 1} min / "
          f"{len(keep) // 180 + 1} min.")
    print("calls:", CALLS)


def hydrate(a):
    out = Path(a.out)
    rows = json.loads((out / "rows.json").read_text(encoding="utf-8"))
    if a.scope == "investor":
        # Current owner is a company (bought and still holds) or the last sale was a flip exit
        # (company sold to a retail buyer). Everyone else is a homeowner-to-homeowner sale.
        rows = [r for r in rows if r.get("corporateOwned") or r.get("_flip_exit")]
    dpath = out / "details.json"
    details = json.loads(dpath.read_text(encoding="utf-8")) if dpath.exists() else {}
    todo = [r for r in rows if str(r.get("id")) not in details]
    print(f"{len(rows)} in scope, {len(details)} already saved, {len(todo)} to fetch")
    for i, r in enumerate(todo, 1):
        details[str(r.get("id"))] = detail(r.get("id"))
        if i % 25 == 0 or i == len(todo):
            dpath.write_text(json.dumps(details, indent=1, default=str), encoding="utf-8")
            print(f"  {i}/{len(todo)}", flush=True)
    dpath.write_text(json.dumps(details, indent=1, default=str), encoding="utf-8")
    print("details saved:", len(details), "| calls:", CALLS)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["probe", "sweep", "hydrate"])
    ap.add_argument("--counties", default="Tulsa")
    ap.add_argument("--zips", default="", help="comma list; per-deal fresh sweep of just these ZIPs")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--scope", choices=["investor", "all"], default="investor")
    ap.add_argument("--out", default=str(SIFTSTACK / "output" / "buyer_finder" / date.today().isoformat()))
    args = ap.parse_args()
    {"probe": probe, "sweep": sweep, "hydrate": hydrate}[args.phase](args)
