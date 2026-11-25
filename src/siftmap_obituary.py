"""SiftMap obituary check, after SmartSkip (Jeff, 2026-11-24/25).

The DataSift plan upgrade added obituary data to SiftMap. Every deep prospect now
double-checks death status against it after the SmartSkip step, because
SmartSkip's deceased flag is unreliable and carries no date of death.

What SiftMap gives (GET map.reisift.io/properties/detail/{dataflik_id}/, read live
2026-11-25 on 18 past deep prospects):
    obituary_history: [{"full_name": "SHERRI COLLINS", "death_date": "2026-08-13",
                        "obit_id": "62088327"}]
    distressor_data.is_obituary_property: {"is_active": true, "date_added": <ms>}
Every date SiftMap had matched our own research (7 of 7). It only knows
deaths tied to a name on title, and it misses some (Russell Thomas, died
2026-07-30, empty; one property flagged with no name). So a SiftMap hit
CONFIRMS a death; an empty result proves nothing about anyone being alive.

When sources disagree about a death, assume dead (Jeff).
"""
from __future__ import annotations

import json
from pathlib import Path

import owner_profile

FILE = "siftmap_obituary.json"


def _client():
    import config
    from siftmap_standalone import SiftMapClient
    return SiftMapClient(api_key=config.DATASIFT_API_KEY)


def fetch(street: str, city: str, state: str = "OK", *, client=None) -> dict:
    """SiftMap's obituary data for the parcel at `street`. Never raises: a failed
    read comes back as status 'error', which is NOT the same as no obituary."""
    out = {"status": "", "error": "", "parcel": "", "dataflik_id": "", "owner": "",
           "secondary": [], "obits": [], "flag": False, "flag_since": ""}
    try:
        c = client or _client()
        hits = c.autocomplete(f"{street}, {city}, {state}", limit=5) or []
        hit = next((h for h in hits if owner_profile.same_street(
            (h.get("title") or "").split(",")[0], street)), None)
        if not hit:
            out.update(status="no_parcel",
                       error=f"no SiftMap parcel for {street} (saw: "
                             f"{[h.get('title') for h in hits[:3]]})")
            return out
        d = c.get_detail(hit["id"]) or {}
        oi = d.get("owner_info") or {}
        flag = ((d.get("distressor_data") or {}).get("is_obituary_property") or {})
        since = ""
        if flag.get("date_added"):
            from datetime import datetime, timezone
            since = datetime.fromtimestamp(flag["date_added"] / 1000, tz=timezone.utc).date().isoformat()
        out.update(status="ok", parcel=hit.get("title") or "", dataflik_id=str(hit["id"]),
                   owner=oi.get("owner_name") or "", secondary=oi.get("secondary_owner_names") or [],
                   obits=[{"name": (o.get("full_name") or "").strip(), "death_date": o.get("death_date") or "",
                           "obit_id": str(o.get("obit_id") or "")}
                          for o in d.get("obituary_history") or [] if o.get("full_name")],
                   flag=bool(flag.get("is_active")), flag_since=since)
    except Exception as e:                       # noqa: BLE001 - reported, never swallowed as "none"
        out.update(status="error", error=f"{type(e).__name__}: {e}"[:300])
    return out


def _same(a: str, b: str) -> bool:
    """SiftMap writes FIRST LAST only, so compare first and last name, any order."""
    return owner_profile.same_person_name(a, b) or owner_profile.same_person_name(b, a)


def compare(sm: dict, searched: str, ss_rec: dict | None = None) -> dict:
    """What SiftMap's obituaries mean for this record.

    {"searched_dead": {...} | None, "co_owner_dead": [...], "relative_dead": [...],
     "lines": [plain-English lines for the profile and the board]}"""
    res = {"searched_dead": None, "co_owner_dead": [], "relative_dead": [], "lines": []}
    if sm.get("status") != "ok":
        res["lines"].append(f"SiftMap obituary check could NOT run ({sm.get('status')}: "
                            f"{sm.get('error')}). That is not the same as no obituary.")
        return res
    ss_rec = ss_rec or {}
    title_names = [n for n in [sm.get("owner") or ""] + list(sm.get("secondary") or []) if n]
    relatives = ss_rec.get("relatives") or []
    for o in sm.get("obits") or []:
        when = f"died {o['death_date']}" if o.get("death_date") else "date of death not given"
        if _same(o["name"], searched):
            res["searched_dead"] = o
            line = f"SiftMap: obituary for {o['name'].title()} (the person searched), {when} (obit {o['obit_id']})."
            if ss_rec and not ss_rec.get("deceased"):
                line += " SmartSkip does not flag them deceased; SmartSkip is often wrong on this."
            res["lines"].append(line)
            continue
        if any(_same(o["name"], t) for t in title_names):
            res["co_owner_dead"].append(o)
            res["lines"].append(f"SiftMap: obituary for co-owner {o['name'].title()}, {when} "
                                f"(obit {o['obit_id']}).")
            continue
        rel = [r for r in relatives if _same(o["name"], r.get("name") or "")]
        if rel:
            res["relative_dead"].append({**o, "relatives": [r.get("name") for r in rel]})
            res["lines"].append(
                f"SiftMap: obituary for {o['name'].title()}, {when} (obit {o['obit_id']}). SmartSkip lists "
                f"{', '.join(r.get('name', '').title() for r in rel)} as a relative: if that is the same "
                "person, they are dead. Do not score or name them until research says otherwise.")
            continue
        res["lines"].append(f"SiftMap: obituary on this property for {o['name'].title()}, {when} "
                            f"(obit {o['obit_id']}); not the person searched, not on title.")
    if sm.get("flag") and not sm.get("obits"):
        res["lines"].append(f"SiftMap flags this as an obituary property (since {sm.get('flag_since') or '?'}) "
                            "but gives no name or date. Treat someone on title as possibly dead.")
    if not res["lines"]:
        res["lines"].append("SiftMap has no obituary for this property. That does not prove anyone is "
                            "alive: SiftMap misses deaths.")
    return res


def run_check(run: Path, state: dict, ss_rec: dict | None = None, *, client=None) -> dict:
    """Fetch, compare, save siftmap_obituary.json and add the block to owner_profile.txt."""
    sm = fetch(state["street"], state.get("city") or "", state.get("state") or "OK", client=client)
    cmp_ = compare(sm, state.get("searched") or "", ss_rec)
    data = {"siftmap": sm, **cmp_}
    (run / FILE).write_text(json.dumps(data, indent=2), encoding="utf-8")
    block = "\n== SiftMap obituary check (after SmartSkip)\n" + "\n".join(f"- {x}" for x in cmp_["lines"]) + "\n"
    pt = run / "owner_profile.txt"
    text = pt.read_text(encoding="utf-8") if pt.exists() else ""
    if "== SiftMap obituary check" in text:
        text = text.split("\n== SiftMap obituary check")[0]
    pt.write_text(text.rstrip() + "\n" + block, encoding="utf-8")
    return data


def load(run: Path) -> dict | None:
    f = run / FILE
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def finish_problems(data: dict | None, status: str, research: dict, call: str = "") -> list[str]:
    """Why finish must wait, or []. `call` = Jeff's decision (--siftmap-call)."""
    if call:
        return []
    if not data or (data.get("siftmap") or {}).get("status") != "ok":
        why = (data or {}).get("siftmap", {}).get("error") if data else "not run"
        return [f"the SiftMap obituary check did not run ({why}). Re-run finish once SiftMap answers, "
                "or pass --siftmap-call \"<Jeff's decision>\"."]
    out = []
    sd = data.get("searched_dead")
    if sd and status == "alive":
        out.append(f"--owner-status alive, but SiftMap has an obituary for {sd['name'].title()} "
                   f"(died {sd.get('death_date') or '?'}). When sources disagree, assume dead.")
    dead = {d.upper() for d in (research.get("dead") or []) + (research.get("not_family") or [])}
    for r in data.get("relative_dead") or []:
        names = [n for n in r.get("relatives") or [] if n.upper() not in dead]
        if names:
            out.append(f"SiftMap has an obituary for {r['name'].title()} (died {r.get('death_date') or '?'}) "
                       f"and SmartSkip lists {', '.join(n.title() for n in names)}. Put them in "
                       f"dp_research.json 'dead', or 'not_family' with why it is a different person.")
    return out


def status_note(data: dict | None) -> str:
    """The SiftMap line added under the owner status on the board and the PDF."""
    if not data or (data.get("siftmap") or {}).get("status") != "ok":
        return ""
    keep = [x for x in data.get("lines") or [] if x.startswith("SiftMap: obituary") or "obituary property" in x]
    return " ".join(keep)
