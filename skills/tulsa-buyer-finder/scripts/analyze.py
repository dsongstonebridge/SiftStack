"""OFFLINE. Turns a saved sweep (rows.json + details.json) into a ranked business-buyer list.
No network calls. Safe to re-run any number of times.

Market list:
  python analyze.py --out DIR
Per deal (filters the saved list, no new SiftMap calls):
  python analyze.py --out DIR --deal-zips 74112[,74135] --deal-price 140000

Writes into DIR: buyers.json, buyers_ranked.csv (or deal_<zip>_shortlist.csv in deal mode).
Ranking is an ORDER, not a score (Jeff, 2026-10-16: "I don't care about a score, just a ranking").
"""
import argparse
import csv
import json
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ENTITY = re.compile(r"\b(LLC|L L C|INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LP|LLP|LTD|PLLC|"
                    r"PROPERTIES|PROPERTY|HOLDINGS|INVESTMENTS?|INVESTORS|ENTERPRISES?|GROUP|PARTNERS|"
                    r"PARTNERSHIP|VENTURES?|CAPITAL|REALTY|HOMES|HOUSING|DEVELOPMENT|ASSETS|RENTALS?|"
                    r"BANK|ASSOCIATION|ASSN|CHURCH|MINISTRIES|AUTHORITY|DEPARTMENT|DEPT|"
                    r"COUNTY|CITY|STATE|TOWN|NATION|TRIBE|UNITED STATES|USA|SECRETARY|"
                    r"FEDERAL|MORTGAGE|FUND|TRUST|TRUSTEE|TR|ESTATE|SCHOOL|DISTRICT)\b")

# Bucket order matters: the first match wins. Every exclusion is LISTED in the output, never dropped.
EXCLUDE = [
    ("government/tribal", r"\b(COUNTY|CITY OF|STATE OF|TOWN OF|NATION|TRIBE|UNITED STATES|USA|"
                          r"SECRETARY|AUTHORITY|DEPARTMENT|DEPT|COMMISSIONERS|SCHOOL|DISTRICT)\b"),
    ("bank/lender/servicer", r"\b(BANK|MORTGAGE|FEDERAL NATIONAL|FEDERAL HOME|FANNIE|FREDDIE|"
                             r"CREDIT UNION|SAVINGS|LENDING|FINANCIAL|SERVICING)\b"),
    ("church/nonprofit/HOA", r"\b(CHURCH|MINISTRIES|ASSOCIATION|ASSN|FOUNDATION|HABITAT)\b"),
    ("trust/estate (usually family)", r"\b(TRUST|TRUSTEE|TR|ESTATE|REVOCABLE|LIVING)\b"),
]
# Copied (not imported) from SiftStack src/sms_agent/dispo_campaign.py NAMED_EXCLUSIONS:
# firms verifiable as production builders, iBuyers or SFR funds. Substring match on the upper name.
# A keyword sweep for HOMES/BUILDERS would delete small local builders, who are the BEST buyers.
NAMED_INSTITUTIONAL = (
    "BALL HOMES", "CLAYTON PROPERTIES", "CLAYTON HOMES", "CMH HOMES", "VANDERBILT MORTGAGE",
    "D R HORTON", "DR HORTON", "DHI ", "LENNAR", "PULTE", "NVR ", "RYAN HOMES", "MERITAGE",
    "SMITH DOUGLAS", "DREAM FINDERS", "DFH ",
    "OPENDOOR", "OP SPE", "OFFERPAD", "ZILLOW HOMES", "HOMEGO", "SUNDAE", "HOMEVESTORS",
    "PROGRESS RESIDENTIAL", "FREO", "INVITATION HOMES", "TRICON", "SFR JV", "AMHERST", "ARMM ",
    "MAIN STREET RENEWAL", "MSR ", "FIRSTKEY", "FKH SFR", "TRUEHOLD", "CPT-ASL",
)


def norm(n: str) -> str:
    n = re.sub(r"[.,']", "", (n or "").upper())
    n = re.sub(r"\bL L C\b", "LLC", n)
    return re.sub(r"\s+", " ", n).strip()


def is_entity(n: str) -> bool:
    return bool(ENTITY.search(norm(n)))


def bucket(n: str) -> str:
    up = norm(n)
    for firm in NAMED_INSTITUTIONAL:
        if firm in up + " ":
            return "national builder/iBuyer/SFR fund (" + firm.strip() + ")"
    for label, rx in EXCLUDE:
        if re.search(rx, up):
            return label
    return ""


def zip_of(address: str) -> str:
    tail = str(address or "").strip()[-5:]
    return tail if tail.isdigit() else ""


def mail_key(m: str) -> str:
    m = re.sub(r"[^A-Z0-9 ]", "", (m or "").upper())
    return m if len(m) > 8 and any(ch.isdigit() for ch in m) else ""


def money(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def build(out: Path) -> dict:
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    rows = {str(r["id"]): r for r in json.loads((out / "rows.json").read_text(encoding="utf-8"))}
    details = json.loads((out / "details.json").read_text(encoding="utf-8"))
    window_start = meta["since"]
    six_mo = (date.today() - timedelta(days=182)).isoformat()

    owner_info = {}
    for d in details.values():
        oi = d.get("owner_info") or {}
        if oi.get("owner_name"):
            owner_info.setdefault(norm(oi["owner_name"]), oi)

    buys, sells = defaultdict(list), defaultdict(list)
    for pid, d in details.items():
        r = rows.get(pid, {})
        for s in d.get("sale_history") or []:
            sd = str(s.get("sale_date") or "")
            if sd < window_start:
                continue
            rec = {"pid": pid, "address": r.get("address"), "zip": zip_of(r.get("address")),
                   "county": r.get("_county"), "date": sd, "price": s.get("sale_price"),
                   "cash": bool(s.get("is_cash_sale")), "buyer": s.get("buyer_name"),
                   "seller": s.get("seller_name"), "in_crm": bool(r.get("saved_uuid"))}
            if is_entity(rec["buyer"] or ""):
                buys[norm(rec["buyer"])].append(rec)
            if is_entity(rec["seller"] or ""):
                sells[norm(rec["seller"])].append(rec)

    def portfolio(name):
        oi = owner_info.get(name)
        if not oi:
            core = re.sub(r"\b(LLC|INC|CO|CORP)\b", "", name).strip()
            for k, v in owner_info.items():
                if core and re.sub(r"\b(LLC|INC|CO|CORP)\b", "", k).strip() == core:
                    oi = v
                    break
        if not oi:
            return None, {}, ""
        by_zip = defaultdict(int)
        for p in oi.get("owner_properties") or []:
            z = zip_of(p.get("address"))
            if z:
                by_zip[z] += 1
        return oi.get("total_properties"), dict(by_zip), oi.get("owner_mail_address") or ""

    # one record per company, then group companies that share a real mailing address
    companies, excluded = {}, []
    for name in set(buys) | set(sells):
        b = buys.get(name, [])
        s = sells.get(name, [])
        total, by_zip, mail = portfolio(name)
        why_out = bucket(name)
        rec = {"name": name, "buys": b, "sells": s, "portfolio": total, "by_zip": by_zip, "mail": mail}
        if why_out:
            excluded.append({"name": name, "excluded_as": why_out, "buys": len(b), "sells": len(s)})
        else:
            companies[name] = rec

    groups = defaultdict(list)
    for name, c in companies.items():
        groups[mail_key(c["mail"]) or ("NAME:" + name)].append(c)

    ranked = []
    for members in groups.values():
        b = [x for m in members for x in m["buys"]]
        s = [x for m in members for x in m["sells"]]
        deals = len({(x["date"], norm(x["seller"] or "")) for x in b})     # closings, not parcels
        exits = len({(x["date"], norm(x["buyer"] or "")) for x in s})
        prices = sorted(money(x["price"]) for x in b if money(x["price"]) >= 10000)
        by_zip = defaultdict(int)
        for m in members:
            for z, n in m["by_zip"].items():
                by_zip[z] += n
        port = sum(m["portfolio"] or 0 for m in members) or None
        sheriff = any("SHERIFF" in (x["seller"] or "").upper() for x in b)
        cash = sum(1 for x in b if x["cash"])
        recent = any(x["date"] >= six_mo for x in b + s)
        why = []
        if len(members) > 1:
            why.append(f"{len(members)} companies at one mailing address (one operator)")
        if deals:
            why.append(f"{deals} purchase{'s' if deals != 1 else ''} in {meta['days']} days")
        if exits:
            why.append(f"sold {exits} house{'s' if exits != 1 else ''} (flipper)")
        if not deals:
            why.append("no purchase recorded in the window (seller only)")
        if sheriff:
            why.append("bought at a sheriff's sale")
        if cash:
            why.append(f"{cash} cash")
        if port and port >= 5:
            why.append(f"{port} properties overall")
        if recent:
            why.append("active in the last 6 months")
        ranked.append({
            "names": [m["name"] for m in members], "mail": members[0]["mail"],
            "deals": deals, "exits": exits, "portfolio": port, "holds_by_zip": dict(by_zip),
            "zips_bought": sorted({x["zip"] for x in b if x["zip"]}),
            "price_low": prices[0] if prices else None, "price_high": prices[-1] if prices else None,
            "price_median": prices[len(prices) // 2] if prices else None,
            "sheriff": sheriff, "cash": cash, "recent": recent, "seller_only": deals == 0,
            "last_active": max([x["date"] for x in b + s] or [""]),
            "why": why, "purchases": sorted(b, key=lambda x: x["date"], reverse=True),
            "sales": sorted(s, key=lambda x: x["date"], reverse=True)})

    # The ORDER (no score): buyers before seller-only flippers; then purchases, flips sold,
    # recent activity, sheriff buys, cash buys, homes held, most recent date.
    # newest activity breaks ties: sort on it first, then the stable main sort keeps that order
    ranked.sort(key=lambda r: r["last_active"], reverse=True)
    ranked.sort(key=lambda r: (r["seller_only"], -r["deals"], -r["exits"], not r["recent"],
                               not r["sheriff"], -r["cash"], -(r["portfolio"] or 0)))
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    result = {"meta": meta, "ranked": ranked, "excluded": sorted(excluded, key=lambda e: -e["buys"])}
    (out / "buyers.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    return result


def deal_filter(result: dict, zips: set, price: float) -> list:
    """Buyers who bought in (or hold in) the deal's ZIPs and whose price band fits the ask.

    The band is ASYMMETRIC on purpose: above a buyer's high is affordability (35% tolerance);
    below their low is only taste, so the floor is divided by 4 (lesson from the 158 Old State blast).
    """
    out = []
    for r in result["ranked"]:
        if r["seller_only"]:
            continue
        here = set(r["zips_bought"]) & zips or set(r["holds_by_zip"]) & zips
        if not here:
            continue
        fits = True
        if price and r["price_high"]:
            fits = (r["price_low"] / 4) <= price <= r["price_high"] * 1.35
        if fits:
            out.append({**r, "deal_zips_matched": sorted(here)})
    return out


def write_csv(path: Path, ranked: list):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "buyer(s)", "why", "purchases", "houses_sold", "price_low", "price_median",
                    "price_high", "zips_bought", "properties_overall", "mailing_address", "last_active",
                    "purchases (date | price | address | from)"])
        for i, r in enumerate(ranked, 1):
            w.writerow([i, " + ".join(r["names"]), "; ".join(r["why"]), r["deals"], r["exits"],
                        r["price_low"], r["price_median"], r["price_high"], " ".join(r["zips_bought"]),
                        r["portfolio"], r["mail"], r["last_active"],
                        " || ".join(f"{x['date']} | {x['price']} | {x['address']} | {x['seller']}"
                                    for x in r["purchases"])])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="the sweep folder (has rows.json, details.json, meta.json)")
    ap.add_argument("--deal-zips", default="")
    ap.add_argument("--deal-price", type=float, default=0)
    a = ap.parse_args()
    out = Path(a.out)
    res = build(out)
    if a.deal_zips:
        zs = {z.strip() for z in a.deal_zips.split(",") if z.strip()}
        short = deal_filter(res, zs, a.deal_price)
        path = out / f"deal_{'_'.join(sorted(zs))}_shortlist.csv"
        write_csv(path, short)
        json.dump(short, open(out / f"deal_{'_'.join(sorted(zs))}_shortlist.json", "w", encoding="utf-8"),
                  indent=1, default=str)
        print(f"{len(short)} buyers fit ZIP {', '.join(sorted(zs))}"
              + (f" at ${a.deal_price:,.0f}" if a.deal_price else "") + f" -> {path}")
        rows = short
    else:
        write_csv(out / "buyers_ranked.csv", res["ranked"])
        print(f"{len(res['ranked'])} ranked buyers, {len(res['excluded'])} excluded -> {out / 'buyers_ranked.csv'}")
        rows = res["ranked"]
    for i, r in enumerate(rows[:15], 1):
        band = (f"${r['price_low']:,.0f}-${r['price_high']:,.0f}" if r["price_low"] else "no prices")
        print(f"{i:>2}. {' + '.join(r['names'])} | {band} | {'; '.join(r['why'])}")
