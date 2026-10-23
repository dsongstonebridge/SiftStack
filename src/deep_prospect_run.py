"""Deep prospect one DataSift record, start to finish, in three commands.

    1. start   FREE. Reads the record, runs the obituary check, builds the
               SmartSkip upload and runs SmartSkip's free count. Stops and
               prints what the charge would be.
    2. charge  BILLS SmartSkip (~$0.15 per hit, the saved card). Waits for the
               job and downloads the results.
    3. finish  Picks nothing on its own: Claude reads the obituary check and the
               SmartSkip family, then passes --primary (owner, or top heir/PR)
               and --basis (why). Writes EVERY SmartSkip number to the phone
               list, all Trestle scored (Jeff, 2026-10-20), a who-is-who
               Message Board post naming who to call first, the research PDF
               to the record's Documents tab, and a board line pointing to it.
               DRY RUN until --commit.

    python src/deep_prospect_run.py start --street "123 Main St" --city Tulsa
    python src/deep_prospect_run.py start --street "123 Main St" --city Tulsa \
        --search-person "Mary Smith"           # probate: search the person who DIED
    python src/deep_prospect_run.py charge --run output/deep_prospect/123-main-st
    python src/deep_prospect_run.py finish --run output/deep_prospect/123-main-st \
        --primary "Susan Smith" --relationship "Susan Smith=Daughter" \
        --basis "Mary died 2026-08-02 (Legacy, Ninde Funeral Home); widowed; Susan is PR" --commit

Rules (Jeff, 2026-10-16): SmartSkip only on records he names for deep
prospecting; the person searched is ALWAYS the owner who died or cannot be
reached, never a relative (2026-10-19) - their family comes back from that one
search; one search per property, ever (`start` refuses a repeat, see
prior_run_reason); regular records stay on Tracerfy. Business and trust owners are
skipped (SmartSkip is not free for them) unless a person is named with
--search-person. On a probate record the DataSift owner is the PR, so the
search must be the DECEDENT at the property address, or SmartSkip returns the
PR's family instead of the decedent's.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import config
import datasift_api as _api
import deep_prospect_smartskip as dps
import obituary_check
import skip_trace_agent as sta
import smartskip_parse

logger = logging.getLogger(__name__)

RUNS_DIR = Path(config.OUTPUT_DIR) / "deep_prospect"
PDF_MARKER = "DEEP PROSPECTING RESEARCH PDF"
PRICE_PER_HIT = 0.15
_ENTITY_WORDS = re.compile(r"(?<![A-Z])(TRUST|TTEE|TRUSTEE|LLC|L L C|INC|CORP|COMPANY|LP|LLP|"
                           r"PLLC|FOUNDATION|ESTATE|MINISTRIES|CHURCH|HOLDINGS|PROPERTIES)(?![A-Z])")


def _tag_titles(rec: dict) -> set[str]:
    out = set()
    for t in rec.get("tags") or []:
        out.add(t if isinstance(t, str) else (t.get("title") or t.get("name") or ""))
    return out


def prior_run_reason(run: Path, rec: dict) -> str | None:
    """Why `start` must not open a new SmartSkip order here, or None.
    A second order on the same property is a second charge: re-running start
    used to overwrite the saved order with a fresh unpaid one, which `charge`
    would then pay. Checked two ways, because one property can sit on more than
    one record (Fulton: three probates, one house) and the same house can be
    typed two ways."""
    sf = run / "state.json"
    if sf.exists():
        st = json.loads(sf.read_text(encoding="utf-8"))
        how = "PAID" if st.get("paid") else "created, not paid"
        return (f"this address already has a deep prospecting run ({run.as_posix()}): "
                f"SmartSkip order {st.get('bulkSkipId')} {how}, searched {st.get('searched')!r} "
                f"on {st.get('started')}. Use charge/finish on that run instead.")
    if dps.TAG_SMARTSKIP_SKIPPED in _tag_titles(rec):
        return (f"the DataSift record already carries the '{dps.TAG_SMARTSKIP_SKIPPED}' tag, "
                "so it has been deep prospected before.")
    return None


def slug(street: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (street or "").lower()).strip("-")[:60] or "record"


def person_name(owner: dict) -> str | None:
    """The owner's person name, or None for a business / trust with no person.
    A trust-held record that still carries the person's first and last name
    (owner enrichment adds the trust as `company`) is a person."""
    first = (owner.get("first_name") or "").strip()
    last = (owner.get("last_name") or "").strip()
    if not first or not last or _ENTITY_WORDS.search(f"{first} {last}".upper()):
        return None
    return f"{first} {last}"


def _smartskip():
    """smartskip_trace reads its session path at import, so set it first. The
    session cache holds live tokens and lives in the git-ignored .skiptrace_run/."""
    os.environ.setdefault("SKIPTRACE_RUN_DIR", str(Path(config.PROJECT_ROOT) / ".skiptrace_run"))
    import smartskip_trace
    if not (os.environ.get("SMARTSKIP_EMAIL") and os.environ.get("SMARTSKIP_PASSWORD")):
        raise SystemExit("SMARTSKIP_EMAIL / SMARTSKIP_PASSWORD are not in .env")
    return smartskip_trace


def _load(run: Path) -> dict:
    return json.loads((run / "state.json").read_text(encoding="utf-8"))


def _save(run: Path, state: dict) -> None:
    (run / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def split_name(person: str) -> tuple[str, str]:
    """'First Middle Last' -> (First, Last). 'Francisco|Perez Rodriguez' keeps a
    compound surname whole."""
    if "|" in person:
        first, last = (x.strip() for x in person.split("|", 1))
        first = first.split()[0] if first else ""
    else:
        parts = person.split()
        first, last = (parts[0], parts[-1]) if len(parts) >= 2 else ("", "")
    if not first or not last:
        raise SystemExit(f"need a first and last name to search, got {person!r}")
    return first, last


def build_input_row(person: str, rec: dict, *, at_property: bool) -> dict:
    """One SmartSkip upload row. A decedent is searched at the property (where
    they lived); a living owner at their mailing address."""
    first, last = split_name(person)
    a = rec.get("address") or {}
    m = ((rec.get("owner") or {}).get("address") or {}) if not at_property else {}
    mail = m if (m.get("street") or m.get("address")) else a
    return {"First Name": first, "Last Name": last,
            "Mailing Address": mail.get("street") or mail.get("address") or "",
            "Mailing City": mail.get("city") or "", "Mailing State": mail.get("state") or "",
            "Mailing Zip": (mail.get("postal_code") or mail.get("zip") or "")[:5],
            "Property Address": a.get("street") or a.get("address") or "",
            "Property City": a.get("city") or "", "Property State": a.get("state") or "",
            "Property Zip": (a.get("postal_code") or a.get("zip") or "")[:5]}


def _boards(owner_uuid: str, property_uuid: str | None = None) -> list[dict]:
    """Posts on both boards (read only): probate posts sit on the owner board,
    the team's notes and deep prospecting posts on the property board."""
    posts = list(_api.get_message_board(owner_uuid))
    if property_uuid:
        posts += _api.get_property_message_board(property_uuid)
    return posts


def decedent_from_board(owner_uuid: str, property_uuid: str | None = None) -> str:
    """'Russell Wayne Thomas Jr.' from a 'Decedent: ...' / 'Decedent Name: ...'
    line on the record's board, without any '(fka ...)' or '(d. ...)' tail."""
    try:
        posts = _boards(owner_uuid, property_uuid)
    except Exception as e:                      # noqa: BLE001 - free check, never fatal
        logger.warning("could not read the board for the decedent's name: %s", e)
        return ""
    for p in posts:
        for line in dps._plain(p.get("message") or "").splitlines():
            # "[^(|\n]": the July obituary posts read "Decedent: X | Died: ... |
            # Obituary: ..." on one line; stop at the bar (2026-10-23).
            m = re.match(r"\s*Decedent(?: Name)?:\s*([^(|\n]+)", line)
            if m and len(m.group(1).split()) >= 2:
                return m.group(1).strip().rstrip(",")
    return ""


def known_family_from_board(owner_uuid: str, property_uuid: str | None = None) -> list[str]:
    """Names the probate filing already established (heirs, the PR), read off
    the record's own Message Board posts. An obituary whose survivors name none
    of these people is someone else's (Jeff, 2026-10-20)."""
    names: list[str] = []
    try:
        posts = _boards(owner_uuid, property_uuid)
    except Exception as e:                      # noqa: BLE001 - free check, never fatal
        logger.warning("could not read the board for known family: %s", e)
        return names
    for p in posts:
        text = dps._plain(p.get("message") or "")
        for line in text.splitlines():
            line = line.strip()
            m = re.match(r"(?:Heirs|Named):\s*(.+)", line)
            if m:
                # Only "Name (Relationship, address)" entries; the summary line
                # "Heirs: 2 - MULTIPLE HEIRS..." carries no names.
                for part in m.group(1).split(";"):
                    if "(" not in part:
                        continue
                    name = part.split("(")[0].strip()
                    if re.fullmatch(r"[A-Za-z.'\- ]+", name) and 2 <= len(name.split()) <= 5:
                        names.append(name)
            m = re.match(r"Personal Rep(?:resentative)?[^:]*:\s*([^-(]+)", line)
            if m and len(m.group(1).split()) >= 2:
                names.append(m.group(1).strip())
    return list(dict.fromkeys(names))


# ── 1. start (free) ───────────────────────────────────────────────────

def cmd_start(a) -> int:
    uuid, owner_uuid, rec = dps._resolve(a.street, a.city, a.state)
    owner = rec.get("owner") or {}
    person = a.search_person or person_name(owner)
    if person and not a.search_person:
        # The CRM's own fields, so a compound surname ("Perez Rodriguez") is
        # searched whole rather than as its last word.
        person = f"{(owner.get('first_name') or '').strip()}|{(owner.get('last_name') or '').strip()}"
    if not person:
        print(f"SKIPPED: the owner is a business or trust ({owner.get('company') or 'no person name'}). "
              "SmartSkip is not free for those. Name a person with --search-person to trace them.")
        return 3
    at_property = bool(a.search_person) or a.search_at == "property"
    run = RUNS_DIR / slug(a.street)
    why = prior_run_reason(run, rec)
    if why and not a.restart:
        print(f"REFUSED, nothing created or charged: {why}\n"
              "Only if you really want a second SmartSkip search (a second charge), add --restart.")
        return 5
    if why:
        logger.warning("--restart: %s Opening a NEW SmartSkip order anyway.", why)
        old = run / "state.json"
        if old.exists():
            old.rename(run / f"state.replaced-{int(time.time())}.json")
    run.mkdir(parents=True, exist_ok=True)

    row = build_input_row(person, rec, at_property=at_property)
    if not row["Mailing Address"]:
        raise SystemExit("no address to search SmartSkip with")
    inp = run / "smartskip_input.csv"
    with open(inp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(row))
        w.writeheader()
        w.writerow(row)
    person = person.replace("|", " ").strip()   # the first|last split was only for SmartSkip

    # The board's probate post carries the decedent's FULL name (middle name,
    # suffix). Use it for the obituary check, so a middle name is never lost
    # because the search was typed without one (Jeff, 2026-10-20).
    full = decedent_from_board(owner_uuid, uuid)
    if full and obituary_check.name_in(person, full) and len(obituary_check.tokens(full)) > \
            len(obituary_check.tokens(person)):
        logger.info("obituary check uses the board's full name %r (searched as %r)", full, person)
        obit_name = full
    else:
        obit_name = person
    known = list(a.known or []) + known_family_from_board(owner_uuid, uuid)
    known = [k for k in dict.fromkeys(known) if not obituary_check.name_in(k, obit_name)]
    if not known and not a.spouse:
        logger.warning("no known family (no probate heirs on the board, no --spouse/--known): "
                       "obituary matches cannot be checked against survivors")
    obits = obituary_check.check(obit_name, city=row["Property City"] or a.city, state=a.state,
                                 street=row["Property Address"], spouse=a.spouse or "",
                                 known=known)
    (run / "obituary_check.json").write_text(json.dumps(obits, indent=2), encoding="utf-8")
    obit_txt = obituary_check.report(obits)
    (run / "obituary_check.txt").write_text(obit_txt, encoding="utf-8")

    ss = _smartskip()
    entry = ss.submit(ss.Session(), str(inp), {})
    state = {"street": a.street, "city": a.city, "state": a.state, "property_uuid": uuid,
             "owner_uuid": owner_uuid, "searched": person, "searched_at":
             "property" if at_property else "mailing", "bulkSkipId": entry["bulkSkipId"],
             "billable_rows": entry.get("entities"), "paid": False, "started": date.today().isoformat()}
    _save(run, state)
    print("\n" + obit_txt.encode("ascii", "replace").decode("ascii"))
    n = entry.get("entities") or 0
    print(f"\nSMARTSKIP: {n} billable row(s), at most ${n * PRICE_PER_HIT:.2f} (charged only on a hit). "
          f"NOTHING CHARGED YET.\nNext: python src/deep_prospect_run.py charge --run {run.as_posix()}")
    return 0


# ── 2. charge (BILLS SmartSkip) ───────────────────────────────────────

def cmd_charge(a) -> int:
    run = Path(a.run)
    state = _load(run)
    ss = _smartskip()
    sess = ss.Session()
    if not state.get("paid"):
        logger.warning("BILLED: SmartSkip %s row(s), up to $%.2f, on the saved card",
                       state.get("billable_rows"), (state.get("billable_rows") or 0) * PRICE_PER_HIT)
        if not ss.pay(sess, state["bulkSkipId"]):
            raise SystemExit("payment did not go through (see the message above)")
        state["paid"] = True
        _save(run, state)
    deadline = time.time() + a.wait
    status = ""
    while time.time() < deadline:
        items = ss.fetch_status(sess, state["bulkSkipId"])
        status = ((items[0].get("status") if items else "") or "").lower()
        if "complet" in status:
            break
        if status in ("error", "failed"):
            raise SystemExit(f"SmartSkip job {status}")
        time.sleep(15)
    else:
        print(f"still {status or 'not listed'} after {a.wait}s; re-run charge later (it will not pay twice)")
        return 4
    export = run / "smartskip_export.csv"
    ss.download(sess, state["bulkSkipId"], str(export), "vertical")
    state["export"] = export.name
    _save(run, state)
    rec = dps.pick_record(smartskip_parse.parse(str(export)), None)
    print(f"\nSmartSkip returned {len(rec['subject_phones'])} number(s) for {state['searched']} and "
          f"{len(rec['relatives'])} relative(s):")
    for r in rec["relatives"]:
        print(f"  {r['name']:<28} {r['canon_rel']:<10} age {r.get('age') or '?':<4} "
              f"{len(r['phones'])} number(s){'  [flagged deceased]' if r.get('deceased') else ''}")
    print(f"\nNext: read obituary_check.txt, decide the primary, then\n"
          f"python src/deep_prospect_run.py finish --run {run.as_posix()} --primary ... --basis \"...\"")
    return 0


# ── 3. finish (DRY RUN until --commit) ────────────────────────────────

def research_pack(state: dict, plan: dict, obits: dict, basis: str, phone_tags: dict,
                  notes: str = "", decedent: bool = True, owner_dead: bool = False) -> str:
    lines = [f"# Deep Prospecting: {state['street']}, {state['city']} {state['state']}",
             "", f"Prepared {date.today().isoformat()}. Searched: **{state['searched']}** "
             f"(at the {state['searched_at']} address).", "",
             "## Who to call first", "", basis or "(no basis given)", ""]
    on_list = plan.get("phone_list_numbers")

    def tag_text(n):
        if n == "none":
            return ""
        if on_list is not None and n not in on_list:
            return dps.BOARD_ONLY
        return ", ".join(phone_tags.get(n, [])) or "not scored yet"

    lines += ["| Person | Number | Tags |", "|---|---|---|"]
    for p in plan["primary"]:
        for ph in p["phones"]:
            lines.append(f"| {p['name']} | {ph['number']} | {tag_text(ph['number'])} |")
    shown = [c for c in obits.get("candidates", [])
             if not c["verdict"].startswith(obituary_check.RULED_OUT)]
    confirmed = any(c["verdict"].startswith("CONFIRMED") for c in shown)
    # A living traced person's raw top finding ("LIKELY THE OWNER'S OBITUARY" on a
    # namesake) must not sit above "treated as LIVING" (2026-10-23 report read).
    top = obits.get("summary") if (decedent or confirmed) else "no obituary for the traced person"
    lines += ["", "## Obituary check", "", f"Top finding: {top}",
              "Known family checked against survivors: "
              + (", ".join(obits.get("known") or []) or "none on record"), ""]
    # A confirmed obituary is the answer; the other name matches are noise to a
    # caller and were how a stranger's obituary got read as "likely" (10-20).
    if confirmed:
        shown = [c for c in shown if c["verdict"].startswith("CONFIRMED")]
    elif not decedent and owner_dead:
        lines += ["The traced person is living, but the OWNER is believed deceased: see the "
                  "research notes. Obituaries that only share the traced person's name are left out.", ""]
        shown = []
    elif not decedent:
        # A living owner's namesakes are strangers' obituaries: leave them out
        # rather than put "possible death" next to someone we are calling.
        lines += ["No obituary matching this owner and their known family was found; the "
                  "owner is treated as LIVING. Obituaries that only share the name are left out.", ""]
        shown = []
    elif shown:
        lines += ["No obituary is confirmed against the known family. The matches below "
                  "share a name only; treat each as unverified.", ""]
    for c in shown:
        lines += [f"- **{c['verdict']}**: {c['title']}", f"  - {c['url']}",
                  f"  - Died: {c['dod'] or 'not found'}"]
        if c.get("survived_by"):
            lines.append("  - Survived by: " + re.sub(r"\s+", " ", c["survived_by"])[:500])
        if c.get("preceded_by"):
            lines.append("  - Preceded by: " + re.sub(r"\s+", " ", c["preceded_by"])[:300])
    lines += ["", "## Family", "",
              "Scored and on the phone list: " + (", ".join(plan.get("scored_names") or []) or "nobody")
              + ". Everyone else is listed here only (living owner: owner + spouse; "
              "deceased owner: close family only).", "",
              "| Name | Relationship | Number | Tags |", "|---|---|---|---|"]
    for b in plan["board"]:
        rel = b["relationship"] + (" (flagged deceased, unverified)" if b["deceased"] else "")
        for n in b["phones"] or ["none"]:
            lines.append(f"| {b['name']} | {rel} | {n} | {tag_text(n)} |")
    lines += ["", "SmartSkip's deceased flags and relationship labels are not reliable; "
              "the obituary decides."]
    if notes.strip():
        lines += ["", "## Research notes", "", notes.strip()]
    return "\n".join(lines)


def cmd_finish(a) -> int:
    run = Path(a.run)
    state = _load(run)
    if not state.get("export"):
        raise SystemExit("no SmartSkip results yet - run charge first")
    rec = dps.pick_record(smartskip_parse.parse(str(run / state["export"])), None)
    # The SmartSkip row carries the property address we sent; the DataSift
    # record is resolved from the run's own address so the two cannot drift.
    rec.update(property_address=state["street"], property_city=state["city"],
               property_state=state["state"])
    plan = dps.build_plan(rec, a.primary, dps._overrides(a.relationship),
                          subject_deceased=a.decedent, score_add=getattr(a, "score", None),
                          score_drop=getattr(a, "no_score", None),
                          owner_deceased=True if getattr(a, "owner_deceased", False) else None)
    dry = not a.commit
    res = dps.write_record(rec, plan, dry_run=dry, repost=a.repost)

    tags: dict[str, list[str]] = {}
    if not dry:
        owner = _api.get_owner(state["owner_uuid"]) or {}
        tags = {sta.norm_phone(p.get("number")): sorted(p.get("tags") or [])
                for p in owner.get("phones") or []}
    obits = json.loads((run / "obituary_check.json").read_text(encoding="utf-8"))
    notes_file = getattr(a, "notes", None)
    notes = Path(notes_file).read_text(encoding="utf-8") if notes_file else ""
    md = research_pack(state, plan, obits, a.basis, tags, notes, decedent=a.decedent,
                       owner_dead=bool(getattr(a, "owner_deceased", False)))
    md_path = run / f"Deep_Prospecting_{slug(state['street'])}.md"
    md_path.write_text(md, encoding="utf-8")
    from deep_prospect_pdf import generate_pdf
    pdf = generate_pdf(str(md_path))

    already = any(PDF_MARKER in (p.get("message") or "")
                  for p in _boards(state["owner_uuid"], state["property_uuid"]))
    if already and not a.repost:
        print(json.dumps(res))
        print(f"PDF built ({pdf}) but NOT uploaded: this record already has a research PDF "
              "post. Pass --repost to upload another.")
        return 0 if res.get("ok", True) else 2
    _api.set_dry_run(dry)
    try:
        doc = _api.upload_property_document(state["property_uuid"], pdf)
        if not dry:
            names = [d.get("filename") for d in _api.list_property_documents(state["property_uuid"])]
            if os.path.basename(pdf) not in names:
                raise SystemExit(f"PDF upload did not land: Documents tab holds {names}")
        # PROPERTY board, where the team works; never the owner's (Jeff, 2026-10-21/23).
        _api.post_property_message_board(state["property_uuid"],
            f"{PDF_MARKER} ({date.today().isoformat()})\n"
            f"Full research (obituary, family, who to call) is in this record's Documents tab: "
            f"{os.path.basename(pdf)}")
    finally:
        _api.set_dry_run(False)
    state.update(finished=not dry, primary=a.primary, basis=a.basis)
    _save(run, state)
    print(("DRY RUN - nothing written, nothing billed. " if dry else "") + json.dumps(res))
    print(f"PDF: {pdf}" + ("" if dry else f" (uploaded: {doc.get('filename') or doc})"))
    return 0 if res.get("ok", True) else 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="FREE: obituary check + SmartSkip count")
    s.add_argument("--street", required=True)
    s.add_argument("--city", default="")
    s.add_argument("--state", default="OK")
    s.add_argument("--search-person", help="who to search: the owner who died or can't be reached "
                   "(probate: the decedent, since DataSift lists the PR as owner). Never a relative.")
    s.add_argument("--search-at", choices=["mailing", "property"], default="mailing")
    s.add_argument("--spouse", help="spouse's name, sharpens the obituary check")
    s.add_argument("--known", action="append",
                   help="a relative already known (heir, PR, child); repeat. Probate heirs and "
                        "the PR are read off the record's board automatically.")
    s.add_argument("--restart", action="store_true",
                   help="open a NEW SmartSkip order even though this property was run before (second charge)")
    c = sub.add_parser("charge", help="BILLS SmartSkip, waits, downloads")
    c.add_argument("--run", required=True)
    c.add_argument("--wait", type=int, default=1200)
    f = sub.add_parser("finish", help="write phones/board/PDF (dry run until --commit)")
    f.add_argument("--run", required=True)
    f.add_argument("--primary", action="append", required=True)
    f.add_argument("--relationship", action="append")
    f.add_argument("--basis", default="", help="why these are the primary people")
    f.add_argument("--decedent", action="store_true",
                   help="the obituary or probate filing CONFIRMS the searched person died: "
                        "label them Decedent, not Owner (always true on a probate search)")
    f.add_argument("--score", action="append",
                   help="also Trestle-score this person (a co-owner on title, or a PR SmartSkip "
                        "calls a catch-all Relative); repeat")
    f.add_argument("--no-score", action="append",
                   help="never score this person even if the default rule would; repeat")
    f.add_argument("--owner-deceased", action="store_true",
                   help="the OWNER died but the traced person is a living relative: score close "
                        "family (deceased-owner rule) without labelling the traced person a decedent")
    f.add_argument("--notes", help="text file of extra research, added to the PDF as 'Research notes'")
    f.add_argument("--repost", action="store_true")
    f.add_argument("--commit", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("ddgs").setLevel(logging.ERROR)
    return {"start": cmd_start, "charge": cmd_charge, "finish": cmd_finish}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
