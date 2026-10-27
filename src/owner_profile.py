"""Owner profile: free, read-only research on the OWNER before any SmartSkip charge.

Built 2026-10-27 after a record where the only research before paying was
an obituary search: a name-only obituary plus SmartSkip's deceased flag sent
calls to an unrelated same-name person in another state. Jeff asked for whatever can
be found on the owner, not just death searches. Sources (all free):

  1. Tulsa County Assessor: who holds title, deed history, where the tax bill
     goes, and the values table (a 3%/yr taxable rise is the Oklahoma
     homestead cap; a $0 net assessed value is a full exemption, usually the
     100% disabled veteran one). Both are things a living owner filed.
  2. Tulsa County Treasurer: who has been paying the taxes. The site began
     answering "captcha_required" on 2026-10-27; solving it costs money, so
     this section reports "not checked" instead of paying.
  3. County Clerk land records (LOCCAT): recorded documents naming the owner,
     split into this parcel and other parcels (namesakes).
  4. Our CRM: the record's Message Boards, call and text history, phone
     statuses, and other records whose OWNER has the same name.
  5. The web beyond obituaries: search results that mention the owner with
     the street, the city or known family. People-search pages are listed,
     never opened.
  6. Identity check: out-of-state numbers and mailing addresses, and after
     SmartSkip, numbers the owner shares with an out-of-state same-name
     relative (the 2026-10-27 same-name pattern).

Court records (OSCN) are deliberately left out (Jeff, 2026-10-27).

Nothing here writes anywhere. Every section degrades to a stated reason
("not checked: ...") rather than failing the run.
"""
from __future__ import annotations

import html as _html
import logging
import re
from collections import Counter, defaultdict
from typing import Callable

import requests

logger = logging.getLogger(__name__)

OK_AREA_CODES = {"405", "539", "572", "580", "918"}
_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")}
_STREET_SUFFIX = {"ST", "STREET", "AVE", "AVENUE", "RD", "ROAD", "DR", "DRIVE", "PL", "PLACE",
                  "CT", "COURT", "LN", "LANE", "BLVD", "WAY", "CIR", "CIRCLE", "TER", "PKWY"}
_PEOPLE_SEARCH = re.compile(r"truepeoplesearch|fastpeoplesearch|spokeo|whitepages|radaris|"
                            r"beenverified|peoplefinders|mylife|intelius|thatsthem|"
                            r"cyberbackgroundchecks|clustrmaps|usphonebook|nuwber|"
                            r"peoplesearchnow|411\.com|addresses\.com", re.I)
_OBIT_SITE = re.compile(r"obituar|findagrave|legacy\.com|echovita|tributearchive|funeral", re.I)


def _section(source: str) -> dict:
    return {"source": source, "status": "ok", "findings": [], "data": {}}


def _not_checked(sec: dict, why: str) -> dict:
    sec["status"] = "not checked"
    sec["findings"].append(f"Not checked: {why}")
    return sec


def name_tokens(name: str) -> set[str]:
    return {t for t in re.sub(r"[^A-Z ]", " ", (name or "").upper()).split()
            if len(t) > 1 and t not in {"JR", "SR", "II", "III", "IV", "AND", "THE"}}


def same_person_name(a: str, b: str) -> bool:
    """First and last name both present (any order, county 'LAST, FIRST' too)."""
    ta, tb = name_tokens(a), name_tokens(b)
    return len(ta) >= 2 and len(ta & tb) >= 2 or (len(ta) == 1 and ta <= tb)


def street_query(street: str) -> str:
    """'4410 E 99th St' -> '4410 E 99' (the Assessor stores '4410 E 99 ST S')."""
    toks = [t for t in re.sub(r"[^A-Za-z0-9 ]", " ", street).upper().split()]
    while toks and toks[-1] in _STREET_SUFFIX | {"N", "S", "E", "W"} and len(toks) > 2:
        toks.pop()
    return " ".join(re.sub(r"^(\d+)(ST|ND|RD|TH)$", r"\1", t) for t in toks)


def area_code(num: str) -> str:
    d = re.sub(r"\D", "", num or "")[-10:]
    return d[:3] if len(d) == 10 else ""


# ── 1. Assessor ───────────────────────────────────────────────────────

def _cells(page_html: str) -> list[str]:
    text = _html.unescape(re.sub(r"<[^>]+>", " | ", page_html))
    return [c.strip() for c in re.sub(r"\s+", " ", text).split("|") if c.strip()]


def _money(s: str) -> int | None:
    m = re.fullmatch(r"\$?([\d,]+)", s.strip())
    return int(m.group(1).replace(",", "")) if m else None


def parse_values(page_html: str) -> dict:
    """Year columns plus the taxable, gross, exemption and net rows of the
    Assessor Info page's values table."""
    cells = _cells(page_html)
    out: dict = {}
    for i, c in enumerate(cells):
        if c.endswith("Information") and i + 1 < len(cells) and re.fullmatch(r"20\d\d", cells[i + 1]):
            years = []
            for x in cells[i + 1:i + 6]:
                if re.fullmatch(r"20\d\d", x):
                    years.append(int(x))
            out["years"] = years
            break
    n = len(out.get("years") or [])
    for label, key in (("Total Taxable Value (Capped)", "taxable"),
                       ("Gross Assessed Value", "gross"), ("Exemptions", "exemptions"),
                       ("Net Assessed Value", "net"), ("Fair Cash(Market) Value", "market")):
        for i, c in enumerate(cells):
            vals = [_money(x) for x in cells[i + 1:i + 1 + n]] if c == label and n else []
            if vals and all(v is not None for v in vals):
                out[key] = vals
                break
    out["exemptions_claimed"] = parse_exemptions_claimed(page_html)
    m = re.search(r"Owner Mailing Address\s*</th>\s*<td[^>]*>(.*?)</td>", page_html, re.S | re.I)
    if m:
        parts = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", p))).strip()
                 for p in re.split(r"<br\s*/?>", m.group(1), flags=re.I)]
        out["mailing"] = ", ".join(p for p in parts if p)
    return out


def parse_exemptions_claimed(page_html: str) -> dict[str, list[int]]:
    """The Info page's 'Exemptions Claimed' table: {exemption: [years checked]}."""
    m = re.search(r'<table[^>]*table-exemption[^>]*>(.*?)</table>', page_html, re.S | re.I)
    if not m:
        return {}
    years = [int(y) for y in re.findall(r"<th[^>]*>\s*(20\d\d)\s*</th>", m.group(1))]
    out: dict[str, list[int]] = {}
    for row in re.findall(r"<tr>(.*?)</tr>", m.group(1), re.S | re.I)[1:]:
        th = re.search(r"<th[^>]*>(.*?)</th>", row, re.S | re.I)
        name = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", th.group(1)))).strip() if th else ""
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)
        got = [y for y, td in zip(years, tds) if "fa-check" in td]
        if name and got:
            out[name] = got
    return out


_EXEMPTION_MEANING = {
    "Homestead": "The owner must own AND live in the house to claim it",
    "100% Disabled Veteran": "A living 100% disabled veteran (or the surviving spouse) has to file it",
    "Senior Valuation Limitation": "An owner 65 or older living there files it, and re-qualifies on income",
    "Additional Homestead": "An owner living there with low household income files it",
}


def value_findings(v: dict) -> list[str]:
    f: list[str] = []
    for name, yrs in (v.get("exemptions_claimed") or {}).items():
        f.append(f"Exemption claimed: {name} ({', '.join(map(str, yrs))}). "
                 + (_EXEMPTION_MEANING.get(name, "") + ". " if name in _EXEMPTION_MEANING else "")
                 + "Exemptions carry over year to year, so this shows someone filed it, not that "
                   "the owner is alive today.")
    if v.get("exemptions_claimed"):
        return f
    years, tax, gross, net = v.get("years") or [], v.get("taxable"), v.get("gross"), v.get("net")
    if tax and len(tax) >= 2 and all(a > 0 for a in tax[:-1]):
        steps = [round(b / a - 1, 3) for a, b in zip(tax, tax[1:])]
        if all(abs(s - 0.03) <= 0.002 for s in steps):
            f.append("Taxable value rises exactly 3% a year (" + ", ".join(
                f"{y}: ${t:,}" for y, t in zip(years, tax)) + "). That is Oklahoma's homestead cap, "
                "so a homestead exemption appears to be on file: someone claimed this as their home.")
    if gross and net and any(g > 0 and n == 0 for g, n in zip(gross, net)):
        zero = [str(y) for y, g, n in zip(years, gross, net) if g > 0 and n == 0]
        f.append(f"Net assessed value is $0 in {', '.join(zero)} although the gross is not: the "
                 "property is FULLY exempt, usually the 100% disabled veteran exemption. A living "
                 "veteran (or a surviving spouse) has to file for it; it does carry over year to year, "
                 "so it is not proof of life on its own.")
    elif v.get("exemptions") and any(v["exemptions"]):
        f.append("Exemptions on file: " + ", ".join(
            f"{y}: ${e:,}" for y, e in zip(years, v["exemptions"])))
    return f


def assessor_section(street: str, city: str, person: str, *,
                     search: Callable | None = None, situs: Callable | None = None,
                     sales: Callable | None = None, info_html: Callable | None = None) -> dict:
    import tulsa_assessor as ta
    search = search or ta.search_assessor
    situs = situs or ta.get_parcel_situs
    sales = sales or ta.get_parcel_sales_history
    info_html = info_html or (lambda acct: requests.get(
        ta.ASSESSOR_INFO_URL_HTTP.format(acct), headers=_UA, timeout=60).text)
    sec = _section("Tulsa County Assessor")
    try:
        house = street.split()[0]
        hits = search(street_query(street)) or []
        acct = None
        for h in hits[:5]:
            s = situs(h.get("AccountNo"))
            if (s or {}).get("street", "").split()[:1] == [house]:
                acct, sit = h.get("AccountNo"), s
                break
        if not acct:
            return _not_checked(sec, f"no Assessor account found for {street}")
        sec["data"].update(account=acct, situs=sit)
        owner = sit.get("owner") or ""
        sec["findings"].append(f"Title holder(s) of record: {owner} (account {acct}).")
        if not same_person_name(person, owner):
            sec["findings"].append(f"CAUTION: {person} is NOT the name on title.")
        hist = sales(acct) or []
        sec["data"]["sales"] = hist
        for s in hist[:4]:
            sec["findings"].append(f"Deed {s.get('sale_date')}: {s.get('grantor') or '?'} -> "
                                   f"{s.get('grantee') or '?'} (${s.get('sale_price') or 0:,}, "
                                   f"{s.get('deed_type') or '?'}, doc {s.get('document_number')})")
        if hist and not same_person_name(person, hist[0].get("grantee") or ""):
            sec["findings"].append("CAUTION: the most recent deed does not go to the owner.")
        vals = parse_values(info_html(acct))
        sec["data"]["values"] = vals
        mail = vals.get("mailing") or ""
        if mail:
            same = street_query(street).split()[:2] == street_query(mail.split(",")[0]).split()[:2]
            sec["findings"].append(f"Tax bill mails to: {mail}"
                                   + (" (the property itself)." if same else
                                      " (NOT the property: absentee, or someone handling it)."))
        sec["findings"] += value_findings(vals)
    except Exception as e:                          # noqa: BLE001 - free check, never fatal
        logger.warning("assessor section failed: %s", e)
        return _not_checked(sec, f"Assessor error: {e}")
    return sec


# ── 2. Treasurer ──────────────────────────────────────────────────────

def treasurer_blocked() -> str:
    """Why the Treasurer search cannot be read right now, or "". Needed because
    tulsa_treasurer.search_owner_name() logs a 403 and returns [], which reads
    exactly like 'this person owns nothing' (caught live 2026-10-27)."""
    import tulsa_treasurer as tt
    try:
        r = requests.post(tt.SEARCH_URL, headers=tt._HEADERS, timeout=30,
                          params={"from_years": "2025", "to_year": "2026", "first_name": "",
                                  "last_name": "SMITH", "business_owner_name": "",
                                  "show_unpaid_only": "0", "total_record": "1", "show_records": "1"},
                          data={"draw": "1", "start": "0", "length": "1"})
    except Exception as e:                          # noqa: BLE001
        return f"Treasurer unreachable: {e}"
    if r.status_code == 403 or "captcha" in r.text[:300].lower():
        return ("the Treasurer site now requires a captcha (since 2026-10-27). Solving it costs "
                "about $0.0015 a search; ask Jeff before adding it.")
    return "" if r.ok else f"Treasurer answered HTTP {r.status_code}"


def treasurer_section(person: str, parcel_id: str, *, search: Callable | None = None,
                      history: Callable | None = None, blocked: Callable | None = None) -> dict:
    import tulsa_treasurer as tt
    search = search or tt.search_owner_name
    history = history or tt.get_owner_history
    blocked = blocked or treasurer_blocked
    sec = _section("Tulsa County Treasurer")
    toks = person.split()
    if len(toks) < 2:
        return _not_checked(sec, "no first and last name")
    why = blocked()
    if why:
        return _not_checked(sec, why)
    try:
        rows = search(last_name=toks[-1], first_name=toks[0]) or []
    except Exception as e:                          # noqa: BLE001
        if "403" in str(e) or "captcha" in str(e).lower():
            return _not_checked(sec, "the Treasurer site now requires a captcha (since 2026-10-27). "
                                     "Solving it costs about $0.0015 a search; ask Jeff before adding it.")
        return _not_checked(sec, f"Treasurer error: {e}")
    want = re.sub(r"\D", "", parcel_id or "")
    mine = [r for r in rows if want and re.sub(r"\D", "", r.get("parcel_id") or "") == want]
    sec["data"]["rows"] = len(rows)
    if not mine:
        sec["findings"].append(f"{len(rows)} tax-roll row(s) under {person}, none on this parcel.")
        return sec
    years = sorted({r.get("tax_year") for r in mine if r.get("tax_year")})
    sec["findings"].append(f"Tax roll lists {person} on this parcel for {', '.join(map(str, years))}.")
    try:
        payers = history(mine[0]["tax_data_id"]) or []
        for p in payers[:6]:
            sec["findings"].append("Paid " + " | ".join(str(v) for v in p.values() if v))
    except Exception as e:                          # noqa: BLE001
        sec["findings"].append(f"Payer history not read: {e}")
    return sec


# ── 3. County Clerk land records (LOCCAT) ─────────────────────────────

def loccat_section(person: str, parcel_id: str, *, search: Callable | None = None) -> dict:
    import tulsa_loccat as tl
    search = search or tl.search_advanced
    sec = _section("County Clerk land records (LOCCAT)")
    toks = person.upper().split()
    if len(toks) < 2:
        return _not_checked(sec, "no first and last name")
    try:
        hits = tl.verified_hits(search(doc_grantor_grantee=f"{toks[-1]} {toks[0]}"))
    except Exception as e:                          # noqa: BLE001
        return _not_checked(sec, f"LOCCAT error: {e}")
    want = re.sub(r"\D", "", parcel_id or "")
    here, other = [], defaultdict(set)
    for h in hits:
        p = h.get("properties") or {}
        if want and re.sub(r"\D", "", p.get("PARCELNB") or "") == want:
            here.append(p)
        else:
            for nm in re.split(r",", (p.get("GRANTOR") or "") + "," + (p.get("GRANTEE") or "")):
                if same_person_name(person, nm):
                    other[nm.strip()].add(p.get("PARCELNB"))
    here.sort(key=lambda p: p.get("RECORDINGDATE") or "", reverse=True)
    for p in here[:8]:
        sec["findings"].append(f"This property, recorded {(p.get('RECORDINGDATE') or '')[:10]}: "
                               f"{p.get('GRANTOR') or '?'} -> {p.get('GRANTEE') or '?'} "
                               f"(doc {p.get('DOCUMENTNB')})")
    if not here:
        sec["findings"].append("No recorded document on this parcel names the owner.")
    if other:
        sec["findings"].append(
            f"{len(other)} other name(s) like '{person}' appear on {len({x for v in other.values() for x in v})} "
            "other parcel(s): " + "; ".join(sorted(other)[:6])
            + ". Likely namesakes: never assume a same-name person is this owner.")
    sec["data"].update(this_parcel=len(here), namesakes=sorted(other))
    return sec


# ── 4. Our CRM ────────────────────────────────────────────────────────

def summarize_activity(events: list[dict], *, board_limit: int = 12) -> list[str]:
    calls: dict[str, dict] = defaultdict(lambda: {"answered": 0, "seconds": 0, "dispo": "",
                                                   "inbound": 0, "last": ""})
    statuses: dict[str, str] = {}
    texts_in, board = Counter(), []
    for e in sorted(events, key=lambda e: e.get("timestamp") or ""):
        t, p = e.get("event_type") or "", e.get("payload") or {}
        if t in ("owner.call.answered", "owner.call.received"):
            c = p.get("call") or {}
            num = c.get("origin_number") if c.get("direction") == "inbound" else c.get("destination_number")
            row = calls[num or "?"]
            row["answered"] += 1
            row["seconds"] += int(c.get("duration") or 0)
            row["inbound"] += c.get("direction") == "inbound"
            row["dispo"] = ((c.get("extra") or {}).get("disposition") or row["dispo"])
            row["last"] = (c.get("created") or "")[:10]
        elif t == "owner.phone.status.updated":
            ph = (p.get("owner") or {})
            st = ph.get("status")
            statuses[ph.get("phone") or "?"] = st[-1] if isinstance(st, list) and st else str(st)
        elif t == "owner.sms.received":
            texts_in[(p.get("sms") or p).get("origin_number") or "?"] += 1
        elif t in ("property.message.added", "owner.message.added"):
            msg = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", p.get("message") or "")).strip()
            who = (e.get("author_extra_info") or {}).get("first_name") or e.get("author") or "?"
            if msg:
                board.append(f"{(e.get('timestamp') or '')[:10]} {who}: {msg[:220]}")
    out = []
    for num, c in calls.items():
        out.append(f"Answered calls on {num}: {c['answered']} ({c['seconds']}s total, "
                   f"{c['inbound']} inbound), last {c['last']}, last disposition {c['dispo'] or 'none'}.")
    for num, n in texts_in.items():
        out.append(f"Text replies received from {num}: {n}.")
    for num, st in statuses.items():
        out.append(f"Phone status {num}: {st}.")
    if board:
        out.append(f"Board history ({len(board)} post(s), newest last):")
        out += ["  " + b for b in board[-board_limit:]]
    return out or ["No calls, texts or board posts in the activity log."]


def crm_section(property_uuid: str, owner_uuid: str, person: str, *, api=None) -> dict:
    if api is None:
        import datasift_api as api
    sec = _section("Our CRM (DataSift)")
    try:
        owner = api.get_owner(owner_uuid) or {}
        phones = owner.get("phones") or []
        sec["data"]["phones"] = [p.get("number") for p in phones]
        sec["findings"].append(f"{len(phones)} number(s) on the owner record.")
        events = api.get_all(f"{api.CORE_BASE}/api/internal/property/{property_uuid}/logs/",
                             max_pages=5)
        sec["findings"] += summarize_activity(events)
    except Exception as e:                          # noqa: BLE001
        return _not_checked(sec, f"CRM read failed: {e}")
    try:
        others = [r for r in search_owner_name_crm(person, api=api)
                  if r.get("uuid") != property_uuid]
        if others:
            sec["findings"].append(
                f"{len(others)} other record(s) whose owner is named {person}: " + "; ".join(
                    (r.get("address") or {}).get("street") or "?" for r in others[:6])
                + ". Could be the same person's other property, or a namesake.")
        else:
            sec["findings"].append(f"No other record has an owner named {person} "
                                   "(this search matches owner names only, not notes or co-defendants).")
    except Exception as e:                          # noqa: BLE001
        sec["findings"].append(f"Same-name search not run: {e}")
    return sec


def search_owner_name_crm(person: str, *, api, limit: int = 20) -> list[dict]:
    """Records whose OWNER name matches. A targeted query, never an account
    sweep (verified 2026-10-27: a real owner's name -> their record, gibberish -> 0)."""
    h = api._headers(has_body=True, auth="api_key")
    h["x-http-method-override"] = "GET"
    r = requests.post(f"{api.CORE_BASE}/api/internal/property/", headers=h, timeout=45,
                      json={"limit": limit, "offset": 0, "ordering": "-list_count",
                            "query": {"must": {"search": person}}})
    r.raise_for_status()
    return [x for x in api._page_items(r.json() or {})
            if same_person_name(person, " ".join(filter(None, (
                (x.get("owner") or {}).get("first_name"), (x.get("owner") or {}).get("last_name")))))]


# ── 5. The web ────────────────────────────────────────────────────────

def web_section(person: str, street: str, city: str, state: str, known: list[str], *,
                search: Callable | None = None) -> dict:
    if search is None:
        import obituary_check
        search = obituary_check.web_search
    sec = _section("Web search (beyond obituaries)")
    core = street_query(street)
    queries = [f'"{person}" "{core}"', f'"{person}" {city} {state}',
               f'"{person}" {city} Oklahoma business OR LLC OR company']
    queries += [f'"{person}" "{k}"' for k in known[:2]]
    seen, hits = set(), []
    for q in queries:
        try:
            for h in search(q) or []:
                url = h.get("href") or h.get("url") or ""
                if not url or url in seen or _OBIT_SITE.search(url):
                    continue
                seen.add(url)
                text = f"{h.get('title') or ''} {h.get('body') or ''}"
                if not same_person_name(person, text):
                    continue
                tie = []
                if core.split()[0] in text and core.split()[-1] in text.upper():
                    tie.append("names the street")
                if city and city.lower() in text.lower():
                    tie.append(f"names {city}")
                tie += [f"names {k}" for k in known if same_person_name(k, text)]
                kind = "people-search listing, not opened" if _PEOPLE_SEARCH.search(url) else ""
                hits.append({"title": h.get("title") or "", "url": url, "tie": tie, "kind": kind,
                             "snippet": re.sub(r"\s+", " ", h.get("body") or "")[:200]})
        except Exception as e:                      # noqa: BLE001
            sec["findings"].append(f"Search {q!r} failed: {e}")
    # A result that only shares the name (a famous namesake's book or
    # play) is noise; keep it only when it ties to the street, the city or
    # known family. The count of dropped ones is still reported.
    name_only = [h for h in hits if not h["tie"]]
    hits = sorted((h for h in hits if h["tie"]), key=lambda h: -len(h["tie"]))
    if name_only:
        sec["findings"].append(f"{len(name_only)} result(s) share only the name and are left out.")
    for h in hits[:12]:
        sec["findings"].append(f"{h['title']} ({h['url']})"
                               + (f" [{', '.join(h['tie'])}]" if h["tie"] else " [name only]")
                               + (f" [{h['kind']}]" if h["kind"] else "")
                               + (f" - {h['snippet']}" if h["snippet"] else ""))
    if not hits:
        sec["findings"].append("No web result ties the owner to the street, the city or known family "
                               "(obituary sites are covered by the obituary check).")
    sec["data"]["hits"] = hits
    return sec


# ── 6. Identity check ─────────────────────────────────────────────────

def identity_before(person: str, crm_phones: list[str], crm_mailing_state: str,
                    home_state: str = "OK") -> dict:
    sec = _section("Identity check (before SmartSkip)")
    out = [p for p in crm_phones if area_code(p) and area_code(p) not in OK_AREA_CODES]
    if out:
        sec["findings"].append(
            f"{len(out)} of {len(crm_phones)} number(s) on the record are out of state "
            f"({', '.join(sorted({area_code(p) for p in out}))}): an old move, or a different "
            f"person named {person}. Confirm before calling them as the owner.")
    if crm_mailing_state and crm_mailing_state.upper() != home_state:
        sec["findings"].append(f"Owner mailing address is in {crm_mailing_state}, not {home_state}.")
    if not sec["findings"]:
        sec["findings"].append("Numbers and mailing address on the record are all in-state.")
    return sec


def identity_after_smartskip(rec: dict, known: list[str], home_state: str = "OK") -> list[str]:
    """Flags on a SmartSkip result: the owner's own numbers out of state, and
    numbers the owner SHARES with an out-of-state relative of the same name,
    which is two people merged into one (2026-10-27 same-name incident)."""
    f: list[str] = []
    subj = {p["number"] for p in rec.get("subject_phones") or []}
    owner = f"{rec.get('first', '')} {rec.get('last', '')}".strip()
    out = sorted(n for n in subj if area_code(n) and area_code(n) not in OK_AREA_CODES)
    if out:
        f.append(f"SmartSkip gave the owner {len(out)} out-of-state number(s) ({', '.join(out)}). "
                 "These may belong to a different person with the same name.")
    for r in rec.get("relatives") or []:
        shared = subj & {p["number"] for p in r.get("phones") or []}
        st = (r.get("mailing_state") or "").upper()
        tied = any(same_person_name(k, r.get("name") or "") for k in known)
        if shared and same_person_name(owner, r.get("name") or "") and st and st != home_state:
            f.append(f"MERGED IDENTITY: the owner shares {', '.join(sorted(shared))} with "
                     f"'{r['name']}' ({r.get('canon_rel')}, {r.get('mailing_city')}, {st}). That is "
                     "very likely a DIFFERENT person with the same name. Do not call them as family.")
        elif st and st != home_state and not tied and r.get("canon_rel") not in ("Associate",):
            f.append(f"'{r['name']}' ({r.get('canon_rel')}, {r.get('mailing_city')}, {st}) is not "
                     "tied to any known family. SmartSkip's label alone is not proof of family.")
    return f or ["No identity problems seen in the SmartSkip result."]


# ── the whole profile ─────────────────────────────────────────────────

def build(*, person: str, street: str, city: str, state: str, property_uuid: str,
          owner_uuid: str, owner_rec: dict, known: list[str]) -> dict:
    sections = []
    asr = assessor_section(street, city, person)
    sections.append(asr)
    # Tulsa account "R12345678901234" is parcel 12345-67-89-01234 in every other county system.
    parcel = (asr["data"].get("account") or "")[1:]
    sections.append(treasurer_section(person, parcel))
    sections.append(loccat_section(person, parcel))
    crm = crm_section(property_uuid, owner_uuid, person)
    sections.append(crm)
    sections.append(web_section(person, street, city, state, known))
    phones = crm["data"].get("phones") or [p.get("number") for p in (owner_rec.get("phones") or [])]
    mail_state = ((owner_rec.get("address") or {}).get("state") or "")
    sections.append(identity_before(person, phones, mail_state, home_state=state))
    return {"person": person, "street": street, "city": city, "sections": sections}


def report(profile: dict) -> str:
    lines = [f"OWNER PROFILE: {profile['person']} ({profile['street']}, {profile['city']})",
             "Free, read-only. Court records (OSCN) are not searched.", ""]
    for s in profile["sections"]:
        lines.append(f"== {s['source']}" + ("" if s["status"] == "ok" else f" ({s['status']})"))
        lines += [f"- {x}" if not x.startswith("  ") else x for x in s["findings"]]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
