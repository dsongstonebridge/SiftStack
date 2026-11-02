"""Deep prospecting rules (Jeff, 2026-11-02), shared by `deep_prospect_run.py finish`
and this repair tool. Our research trumps SmartSkip. Only Dial First / Dial Second
numbers stay on a DEEP PROSPECTED record; everything else is listed on the PROPERTY
Message Board, one line per person, and in the research PDF. A record that has not
been deep prospected is never touched: the tool refuses it.

As a tool it repairs a record finish already wrote with the old rules. The old family
post is never deleted. Nothing is re-scored or re-traced, so it costs nothing. Dry run
until --commit.

    python src/dp_board_split.py --run-dir <output/deep_prospect/4913-s-troost-ave>
    python src/dp_board_split.py --run-dir <...> --commit

Each run folder needs a dp_research.json with what OUR research established, never
SmartSkip's flags (see load_research). Every post and PDF opens with who holds title
and whether they are alive or dead. Any label not corrected there is marked as
SmartSkip's guess. Never put "obituary", "funeral" or "decedent" on a line naming a
living person: the SMS death check drops every first name on that line. Write "obit".

Order is load-bearing: snapshot, post, read the post back, remove, read the
record back. A number leaves the record only after it is confirmed on the board.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import datasift_api as api

POST_MARKER = "DEEP PROSPECT NUMBERS"
OLD_MARKERS = ("SMARTSKIP FAMILY - WHO IS WHO", "SMARTSKIP FAMILY - ALL NUMBERS ON THE PHONE LIST")
KEEP = ("Dial First", "Dial Second")
LOW = ("Dial Third", "Dial Fourth", "Drop")
LITIGATOR = "DO NOT CALL - Litigator Risk"
TIER_ORDER = {t: i for i, t in enumerate(KEEP + LOW + (LITIGATOR,))}
SOURCES = ("SmartSkip", "Tracerfy", "DataSift", "Pre-existing")
# A caller's disposition means someone worked this number: leave it alone.
REMOVABLE_STATUSES = {"UNKNOWN", "", None}
DP_RECORD_TAG = "SmartSkip Skipped"
RESEARCH_FILE = "dp_research.json"
#: A call-first person's label has to say where it came from.
SOURCE_RX = re.compile(r"\b(filing|obit|county|deed|conversation)\b", re.I)
RULED_OUT_VERDICTS = ("DIFFERENT PERSON", "UNRELATED", "OWNER IS A SURVIVOR")


def load_research(run_dir: Path) -> dict | None:
    """What OUR research established, one file per run folder:
      status       one line per owner of record: alive or dead, and the source
      basis        the who-to-call paragraph
      labels       {"NAME AS POSTED": "corrected label naming its source"}
      relationships {"NAME": "Son"}  the real relationship, which decides scoring and the tag
      dead         names the filing or obit says died: never scored, never call first
      not_family   names research rules out: never scored
      named        people the filing or obit names who are not in SmartSkip's results
      call_first_outside  [{"name","label","numbers"}] a call-first person SmartSkip missed
      obits        replaces the automatic obituary list after someone read the pages
      obit_note    what reading the obituaries showed
      known        the known family, when the automatic list is wrong"""
    f = run_dir / RESEARCH_FILE
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def research_problems(research: dict, people: list[dict], primaries: list[str]) -> list[str]:
    """Why finish must refuse. Research trumps SmartSkip, so it has to exist and be real."""
    out = []
    if not research.get("basis"):
        out.append("no basis: the who-to-call paragraph")
    posted = {p["name"] for p in people}
    for name in list(research.get("labels") or {}) + list(research.get("relationships") or {}):
        if name not in posted:
            out.append(f"{name!r} is not in the SmartSkip results, so that label would be lost")
    dead = {n.lower() for n in research.get("dead") or []}
    for p in people:
        if p["section"] != "CALL FIRST":
            continue
        if p["name"].lower() in dead:
            out.append(f"{p['name']} is listed as dead and cannot be call first")
        if not SOURCE_RX.search(p["relationship"] or ""):
            out.append(f"call-first {p['name']} needs a label naming its source (filing, obit, "
                       f"county, deed or conversation), not {p['relationship']!r}")
    if not primaries and not research.get("call_first_outside"):
        out.append("nobody to call first")
    return out


def people_from_plan(plan: dict, tiers: dict, research: dict) -> list[dict]:
    """The plan as post/report rows, with research applied over SmartSkip's labels."""
    labels = research.get("labels") or {}
    people = []
    for x in research.get("call_first_outside") or []:
        people.append({"section": "CALL FIRST", "name": x["name"], "relationship": x["label"], "extra": "",
                       "numbers": [{"number": digits(n), "tier": ""} for n in x.get("numbers") or []]})
    for p in plan["primary"]:
        who = "Owner (traced subject)" if p.get("is_subject") else (p.get("relationship") or "")
        people.append({"section": "CALL FIRST", "name": p["name"], "relationship": labels.get(p["name"], who),
                       "extra": "", "numbers": [{"number": ph["number"], "tier": tiers.get(ph["number"], "")}
                                                for ph in p["phones"]]})
    for b in plan["board"]:
        rel = b["relationship"] + (" (SmartSkip flags deceased, unverified)" if b.get("deceased") else "")
        extra = b.get("extra") or ""
        if b.get("shared"):
            extra = "; ".join(x for x in (extra, "also holds " + ", ".join(
                f"...{n[-4:]}" for n in b["shared"])) if x)
        people.append({"section": "FAMILY", "name": b["name"], "relationship": labels.get(b["name"], rel),
                       "extra": extra, "numbers": [{"number": n, "tier": tiers.get(n, "")} for n in b["phones"]]})
    return people


def on_record_now(rows: list[dict]) -> set[str]:
    return {r["number"] for r in rows}


def clean(text) -> str:
    text = html.unescape(html.unescape(text or ""))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def _obit_lines(research: dict, obits: dict) -> list[str]:
    entries = research.get("obits")
    if entries is None:
        entries = [c for c in (obits or {}).get("candidates", [])
                   if not str(c.get("verdict", "")).startswith(RULED_OUT_VERDICTS)]
        confirmed = [c for c in entries if str(c.get("verdict", "")).startswith("CONFIRMED")]
        entries = confirmed or entries
    known = research.get("known") or ", ".join((obits or {}).get("known") or []) or "none on record"
    out = ["## Obituary check", "", "Known family: " + known, ""]
    if research.get("obit_note"):
        out += [research["obit_note"], ""]
    elif entries and not any(str(c.get("verdict", "")).startswith("CONFIRMED") for c in entries):
        out += ["None of these has been compared to the known family, so treat them as likely, not confirmed.", ""]
    elif not entries:
        out += ["No obit found.", ""]
    for c in entries:
        verdict = clean(c.get("verdict")).replace("LIKELY THE OWNER'S OBITUARY", "LIKELY MATCH")
        out += [f"- **{verdict}**: {clean(c.get('title'))[:110]}", f"  - {c.get('url', '')}"]
        date_on_page = c.get("dod") or c.get("date")
        if date_on_page:
            out.append(f"  - Date on page: {clean(date_on_page)}")
        if c.get("survived_by") and not c.get("filler"):
            out.append("  - Survived by: " + clean(c["survived_by"])[:600])
        if c.get("preceded_by"):
            kept = re.split(r"(?<=[.;])\s*(?:He|She|[A-Z][a-z]+) is survived", clean(c["preceded_by"]))[0]
            out.append("  - Preceded by: " + kept[:360])
    return out


def report_md(state: dict, people: list[dict], rows: list[dict], research: dict, obits: dict,
              holder: str, on_record: set[str], *, notes: str = "", profile: str = "",
              extra_status: list[str] | None = None, today: str | None = None) -> str:
    """The research PDF's text. `rows` = the phones before anything came off."""
    today = today or date.today().isoformat()
    by = {r["number"]: r for r in rows}

    def where(n):
        tier = _tier(n, by)
        place = "On the phone list" if n["number"] in on_record else "Message Board only"
        return f"{place}, {tier}" if tier else f"{place}, not scored"

    L = [f"# Deep Prospecting: {state['street']}, {state['city']} {state['state']}", "",
         f"**OWNER OF RECORD: {holder or 'not found, verify at the county'}**", ""]
    for x in list(research.get("status") or []) + list(extra_status or []):
        L += [f"**{x}**", ""]
    L += [f"Prepared {state.get('started') or today}, research final {today}. Searched: **{state['searched']}** "
          f"(at the {state.get('searched_at', 'property')} address).", "",
          "## Who to call first", "", research.get("basis") or "", "",
          "| Person | Relationship | Number | Where |", "|---|---|---|---|"]
    for p in people:
        if p["section"] != "CALL FIRST":
            continue
        nums = sorted(p["numbers"], key=lambda n: _number_rank(n, by))
        for n in nums or [None]:
            L.append(f"| {p['name']} | {p['relationship']} | {fmt(n['number']) if n else 'none'} | "
                     f"{where(n) if n else ''} |")
    L += [""] + _obit_lines(research, obits)
    L += ["", "## Family", "", "Numbers marked Dial First or Dial Second are on the record's phone list. "
          "A number with no tier was not scored. Family labels are SmartSkip's guess unless they name "
          "the filing, obit or county.", "", "| Name | Relationship | Numbers |", "|---|---|---|"]
    for p in people:
        if p["section"] != "FAMILY":
            continue
        who = ", ".join(x for x in (p["relationship"], p["extra"]) if x)
        nums = sorted(p["numbers"], key=lambda n: _number_rank(n, by))
        L.append(f"| {p['name']} | {who} | {'; '.join(_number_text(n, by) for n in nums) or 'no numbers'} |")
    if research.get("named"):
        L += ["", "## Named in the filing or obit, no numbers from SmartSkip", ""]
        L += [f"- {n}" for n in research["named"]]
    listed = {n["number"] for p in people for n in p["numbers"]}
    other = [r for r in rows if r["number"] not in listed]
    if other:
        L += ["", "## Other numbers from earlier skip traces", "", "| Number | Tags | Where |", "|---|---|---|"]
        for r in other:
            n = {"number": r["number"]}
            labels = ", ".join(t for t in r["tags"] if t != tier_of(r["tags"]))
            L.append(f"| {fmt(r['number'])} | {labels} | {where(n)} |")
    if notes.strip():
        L += ["", "## Research notes", "", notes.strip()]
    if profile.strip():
        L += ["", "## Owner profile (county, CRM, web, identity)", ""]
        for ln in profile.strip().splitlines()[2:]:
            L.append(f"### {ln[3:]}" if ln.startswith("== ") else ln)
    return "\n".join(L)


def removable_after_post(rows: list[dict]) -> list[str]:
    """Numbers to take off a deep prospected record. Never empties the phone list:
    a record whose only numbers are below Dial Second keeps them."""
    plan = plan_numbers(rows)
    return [r["number"] for r in plan["remove"]] if plan["keep"] else []


def digits(number) -> str:
    d = "".join(c for c in str(number or "") if c.isdigit())
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def fmt(number: str) -> str:
    d = digits(number)
    return f"{d[:3]}-{d[3:6]}-{d[6:]}" if len(d) == 10 else str(number)


def phone_rows(rec: dict) -> list[dict]:
    rows = []
    for p in (rec.get("owner") or {}).get("phones") or []:
        tags = [t.get("title") if isinstance(t, dict) else t for t in (p.get("tags") or [])]
        rows.append({"number": digits(p.get("number")), "tags": tags,
                     "status": p.get("status"), "type": p.get("type")})
    return rows


def tier_of(tags: list[str]) -> str:
    if LITIGATOR in tags:
        return LITIGATOR
    return next((t for t in KEEP + LOW if t in tags), "")


def plan_numbers(rows: list[dict]) -> dict:
    """Which numbers stay and which go. Anything unclear stays and is reported."""
    keep, remove, held = [], [], []
    for r in rows:
        tier = tier_of(r["tags"])
        if tier in KEEP:
            keep.append(r)
        elif not tier:
            held.append({**r, "why": "no dial tier"})
        elif r["status"] not in REMOVABLE_STATUSES:
            held.append({**r, "why": f"status {r['status']}"})
        else:
            remove.append(r)
    return {"keep": keep, "remove": remove, "held": held}


def find_old_post(posts: list[dict]) -> dict | None:
    hits = [p for p in posts if any(m in (p.get("message") or "") for m in OLD_MARKERS)]
    return hits[0] if hits else None


def parse_old_post(text: str) -> dict:
    """The family post, as written by deep_prospect_smartskip.build_family_post."""
    out = {"status_lines": [], "traced": "", "people": []}
    seen_marker = False
    by_key: dict[tuple, dict] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if any(m in line for m in OLD_MARKERS):
            seen_marker = True
            continue
        if not seen_marker:
            if line:
                out["status_lines"].append(line)
            continue
        if line.startswith("Traced:"):
            out["traced"] = line
            continue
        parts = [x.strip() for x in line.split(" | ")]
        if parts[0] not in ("CALL FIRST", "FAMILY") or len(parts) < 3:
            continue
        section, name, rel = parts[0], parts[1], parts[2]
        number = parts[3] if len(parts) > 3 else "none"
        tier = parts[4] if len(parts) > 4 else ""
        extra = parts[5] if len(parts) > 5 else ""
        if section == "CALL FIRST" and not digits(number):
            number, tier = "none", ""
        key = (section, name, rel)
        person = by_key.get(key)
        if person is None:
            person = by_key[key] = {"section": section, "name": name, "relationship": rel,
                                    "extra": extra, "numbers": []}
            out["people"].append(person)
        if digits(number):
            person["numbers"].append({"number": digits(number), "tier": tier})
    return out


def _tier(n: dict, by_number: dict) -> str:
    live = by_number.get(n["number"])
    tier = tier_of(live["tags"]) if live else ""
    return tier or (n.get("tier") if n.get("tier") in TIER_ORDER else "")


def title_holder(owner_posts: list[dict]) -> str:
    """The county's title holder, off the probate/petition post on the owner board."""
    for p in owner_posts:
        for line in (p.get("message") or "").splitlines():
            if line.strip().lower().startswith("title holder of record:"):
                return line.split(":", 1)[1].strip()
    return ""


def _number_text(n: dict, by_number: dict) -> str:
    tier = _tier(n, by_number)
    return f"{fmt(n['number'])} ({tier})" if tier else fmt(n["number"])


def _number_rank(n: dict, by_number: dict) -> int:
    return TIER_ORDER.get(_tier(n, by_number), len(TIER_ORDER))


def _person_line(p: dict, by_number: dict) -> str:
    who = ", ".join(x for x in (p["name"], p["relationship"], p["extra"]) if x)
    numbers = sorted(p["numbers"], key=lambda n: _number_rank(n, by_number))
    text = "; ".join(_number_text(n, by_number) for n in numbers) or "no numbers"
    return f"{who}: {text}"


def apply_labels(old: dict, labels: dict) -> list[str]:
    """Put the researched labels on the people. Returns names that matched nobody,
    which the caller refuses: a typo must not leave a wrong label on the board."""
    posted = {p["name"] for p in old["people"]}
    for p in old["people"]:
        if p["name"] in labels:
            p["relationship"] = labels[p["name"]]
    return sorted(set(labels) - posted)


def build_post(old: dict, rows: list[dict], plan: dict, *, owner_of_record: str,
               statuses: list[str], named: list[str] | None = None,
               today: str | None = None, on_record: set[str] | None = None) -> str:
    today = today or date.today().isoformat()
    by_number = {r["number"]: r for r in rows}
    # One line per person: the death check reads a line at a time, so a living
    # co-owner must never share a line with a death.
    lines = [f"OWNER OF RECORD: {owner_of_record or 'not found, verify at the county'}"]
    lines += [f"STATUS: {s}" for s in statuses] + list(old["status_lines"])
    lines += ["", f"{POST_MARKER} ({today})"]
    if old["traced"]:
        lines.append(old["traced"])
    tiers_kept = {tier_of(by_number[n]["tags"]) for n in (on_record or ()) if n in by_number}
    if on_record and not tiers_kept & set(KEEP):
        lines.append("This record has no Dial First or Second number, so its only numbers stay on the "
                     "phone list. No tier shown = not scored.")
    else:
        lines.append("Dial First and Dial Second numbers are on the record. Every other number "
                     "is only here. No tier shown = not scored.")
    lines.append("Family labels are SmartSkip's guess unless they name the filing, obit or county.")
    first = [p for p in old["people"] if p["section"] == "CALL FIRST"]
    family = [p for p in old["people"] if p["section"] == "FAMILY"]
    if first:
        lines += ["", "CALL FIRST"] + [_person_line(p, by_number) for p in first]
    if family:
        lines += ["", "FAMILY"] + [_person_line(p, by_number) for p in family]

    if named:
        lines += ["", "NAMED IN THE FILING OR OBIT, NO NUMBERS FROM SMARTSKIP"] + list(named)

    listed = {n["number"] for p in old["people"] for n in p["numbers"]}
    other = [r for r in plan["remove"] if r["number"] not in listed]
    if other:
        lines += ["", "OTHER NUMBERS TAKEN OFF THE RECORD"]
        for r in other:
            labels = [t for t in r["tags"] if t not in SOURCES and t != tier_of(r["tags"])]
            source = ", ".join(t for t in r["tags"] if t in SOURCES)
            who = ", ".join(labels + ([f"from {source}"] if source else []))
            lines.append(f"{fmt(r['number'])} ({tier_of(r['tags'])})" + (f": {who}" if who else ""))
    return "\n".join(lines)


def missing_from_post(post: str, numbers: list[str]) -> list[str]:
    return [n for n in numbers if fmt(n) not in (post or "")]


def changed_kept(before: list[dict], after: list[dict]) -> list[str]:
    """Kept numbers whose tags or status differ after the removal."""
    now = {r["number"]: r for r in after}
    bad = []
    for r in before:
        a = now.get(r["number"])
        if not a or sorted(a["tags"]) != sorted(r["tags"]) or a["status"] != r["status"]:
            bad.append(r["number"])
    return bad


def run(run_dir: Path, *, commit: bool) -> int:
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    uuid, owner = state["property_uuid"], state["owner_uuid"]
    rec = api.get_property(uuid)
    rows = phone_rows(rec)
    plan = plan_numbers(rows)
    posts = api.get_property_message_board(uuid)
    old_post = find_old_post(posts)
    print(f"{state['street']}: {len(rows)} on record | keep {len(plan['keep'])} | "
          f"remove {len(plan['remove'])} | held {len(plan['held'])}")
    for h in plan["held"]:
        print(f"  HELD, staying on record: {fmt(h['number'])} ({h['why']})")
    if not old_post:
        print("REFUSED: no family post on this record's property board.")
        return 2
    if DP_RECORD_TAG not in [t.get("title") if isinstance(t, dict) else t for t in rec.get("tags") or []]:
        print(f"REFUSED: no {DP_RECORD_TAG} tag. Numbers only ever come off a deep prospected record.")
        return 2
    if not plan["keep"]:
        print("REFUSED: this would leave no number on the record.")
        return 2

    research = load_research(run_dir)
    if not research:
        print(f"REFUSED: no {RESEARCH_FILE} (who holds title, alive or dead, family labels).")
        return 2
    statuses = research.get("status") or []
    if not statuses:
        print(f"REFUSED: {RESEARCH_FILE} has no status lines.")
        return 2
    old = parse_old_post(old_post["message"])
    unmatched = apply_labels(old, research.get("labels") or {})
    if unmatched:
        print("REFUSED: labels for names not on the post:", unmatched)
        return 2
    # On a re-run the removed numbers are gone from the record, so the post is
    # always built from the state before anything was removed.
    snapshot = run_dir / "pre_split_snapshot.json"
    post_rows = (json.loads(snapshot.read_text(encoding="utf-8"))["phones"]
                 if snapshot.exists() else rows)
    owner_of_record = title_holder(api.get_message_board(owner))
    text = build_post(old, post_rows, plan_numbers(post_rows), owner_of_record=owner_of_record,
                      statuses=statuses, named=research.get("named") or [])
    print("\n--- new post ---\n" + text + "\n--- end ---\n")
    remove = [r["number"] for r in plan["remove"]]
    gaps = missing_from_post(text, remove)
    if not owner_of_record:
        print("REFUSED: no title holder on the owner board.")
        return 2
    if gaps:
        print("REFUSED: not in the new post:", gaps)
        return 2
    if not commit:
        print("DRY RUN: nothing written. Would remove:", ", ".join(fmt(n) for n in remove))
        return 0

    if not snapshot.exists():
        snapshot.write_text(json.dumps({"phones": rows, "old_post": old_post}, indent=1),
                            encoding="utf-8")
    existing = next((p for p in posts if POST_MARKER in (p.get("message") or "")), None)
    if existing and existing.get("message") == text:
        print("New post already on the board, not posting a second.")
    elif existing:
        api.update_property_message_board(uuid, existing["uuid"], text)
        print("Corrected the existing new post in place.")
    else:
        api.post_property_message_board(uuid, text)
    board = next((p for p in api.get_property_message_board(uuid)
                  if POST_MARKER in (p.get("message") or "")), None)
    gaps = missing_from_post((board or {}).get("message"), remove)
    if not board or gaps or board.get("message") != text:
        print("STOPPED before any removal: post not confirmed on the board.", gaps)
        return 2
    print("Post confirmed on the property board.")

    after_rows = rows
    if remove:
        api.remove_phones(owner, remove)
        after_rows = phone_rows(api.get_property(uuid))
    state["basis"] = research.get("basis") or state.get("basis")
    (run_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    replace_pdf(run_dir, state, old, post_rows, research, owner_of_record, on_record_now(after_rows))
    if not remove:
        return 0
    after = phone_rows(api.get_property(uuid))
    left = {r["number"] for r in after}
    still = [n for n in remove if n in left]
    expected = {r["number"] for r in rows} - set(remove)
    lost = sorted(expected - left)
    changed = changed_kept([r for r in rows if r["number"] in expected], after)
    print(f"Read back: {len(after)} on record | still there {len(still)} | "
          f"lost by mistake {len(lost)} | kept numbers changed {len(changed)}")
    if still or lost or changed:
        print("PROBLEM. still:", still, "lost:", lost, "changed:", changed,
              "\nSnapshot:", snapshot)
        return 3
    print("OK: only the intended numbers left the record.")
    return 0


def replace_pdf(run_dir: Path, state: dict, old: dict, rows: list[dict], research: dict,
                holder: str, on_record: set[str]) -> None:
    """Rebuild the record's research PDF from the research and swap it. Each document
    name is unique per record, and the old PDF only goes once the new one is listed."""
    from deep_prospect_pdf import generate_pdf
    obits_file = run_dir / "obituary_check.json"
    obits = json.loads(obits_file.read_text(encoding="utf-8")) if obits_file.exists() else {}
    slug = run_dir.name
    md_path = run_dir / f"Deep_Prospecting_{slug}.md"
    md_path.write_text(report_md(state, old["people"], rows, research, obits, holder, on_record),
                       encoding="utf-8")
    built = generate_pdf(str(md_path))
    final = run_dir / f"Deep_Prospecting_{slug}_corrected.pdf"
    os.replace(built, final)
    uuid = state["property_uuid"]
    docs = [d for d in api.list_property_documents(uuid)
            if d.get("filename") == final.name]
    for d in docs:
        api.delete_property_document(uuid, d["uuid"])
    api.upload_property_document(uuid, str(final))
    names = [d.get("filename") for d in api.list_property_documents(uuid)]
    if final.name not in names:
        raise SystemExit(f"PDF upload did not land: Documents tab holds {names}")
    for d in api.list_property_documents(uuid):
        if d.get("filename") == f"Deep_Prospecting_{slug}.pdf":
            api.delete_property_document(uuid, d["uuid"])
    print("PDF on the record:", final.name)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    return run(a.run_dir, commit=a.commit)


if __name__ == "__main__":
    raise SystemExit(main())
