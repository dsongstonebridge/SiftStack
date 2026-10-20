"""Deep prospecting: write SmartSkip results into DataSift.

Jeff's rule (2026-10-20, replaces the 2026-10-16 relatives-on-the-board rule):
EVERY number SmartSkip returns, owner and relatives alike, goes on the phone
list and gets Trestle scored, the way Ty set it up. Scoring a few, calling,
missing, then scoring a few more costs more in time than ~$0.015 a number
saves. The --primary person(s) are who to call first; that is said on the
board and in the research PDF, not enforced by holding numbers back.

Two commands, both DRY RUN until --commit:

  write    One SmartSkip export record -> every number on the phone list
           (SmartSkip source tag + dial tier + relationship tag where it is
           certain), one Message Board post saying who is who and who to call
           first, property tags `SmartSkip Skipped` (+ `TrestleIQ Scored`).

  promote  Only for a record written under the old rule (a board post headed
           BOARD_MARKER): read its relatives back off the board, add those
           numbers to the phone list, score them, and post a short note.

    python src/deep_prospect_smartskip.py write --export smartskip.csv --primary subject
    python src/deep_prospect_smartskip.py write --export smartskip.csv \
        --primary "Jane Doe" --relationship "Jane Doe=Daughter" --commit
    python src/deep_prospect_smartskip.py promote --street "123 Main St" --city Tulsa
    python src/deep_prospect_smartskip.py promote --street "123 Main St" --city Tulsa \
        --only Son,Daughter,Wife,Husband --commit

Who is primary is ALWAYS stated by the caller (--primary). SmartSkip's
Deceased flag is wrong often enough (see the deep-prospecting-v5 skill) that
"the subject is alive, so the subject is primary" is not a safe default; the
obituary research decides, then the caller says so.

Uses the pipeline's own datasift_api (Jeff's account, normal HTTPS checks) and
skip_trace_agent.score_phones (Trestle, litigator check on), never the FCRE
plugin's network code.
"""
from __future__ import annotations

import argparse
import html
import logging
import re
import sys
from datetime import date

import datasift_api as _api
import skip_trace_agent as sta
import smartskip_parse

logger = logging.getLogger(__name__)

SOURCE_SMARTSKIP = "SmartSkip"
TAG_SMARTSKIP_SKIPPED = "SmartSkip Skipped"

#: First line of the relatives post. `promote` finds the post by this text, so
#: changing it orphans every post already written.
BOARD_MARKER = "SMARTSKIP RELATIVES - NOT ON THE PHONE LIST"
#: First line of the 2026-10-20 family post (every number already on the phone
#: list). `promote` never reads it: there is nothing left to add.
FAMILY_MARKER = "SMARTSKIP FAMILY - ALL NUMBERS ON THE PHONE LIST"
PROMOTED_MARKER = "SMARTSKIP RELATIVES ADDED TO THE PHONE LIST"
LINE_PREFIX = "RELATIVE |"

#: Relationship phone tags that exist in the account (skip_trace_agent,
#: checked 2026-09-11). Anything else maps to Relative; a new relationship tag
#: is never created implicitly, because phone tags are append-only.
ACCOUNT_REL_TAGS = {"Son", "Daughter", "Wife", "Husband", "Grandchild", "Relative"}

_TYPE_MAP = {"mobile": "Mobile", "wireless": "Mobile", "cell": "Mobile",
             "landline": "Landline", "land line": "Landline"}


def account_rel_tag(rel: str) -> str:
    """SmartSkip's (or the caller's) relationship -> an EXISTING phone tag."""
    r = (rel or "").strip().title()
    if r in ACCOUNT_REL_TAGS:
        return r
    return sta.relationship_tag(rel) or "Relative"


def _phone_type(raw: str) -> str:
    return _TYPE_MAP.get((raw or "").strip().lower(), "")


# ── plan: who goes in the phone list, who goes on the board ───────────

def pick_record(recs: list[dict], input_name: str | None) -> dict:
    if not recs:
        raise SystemExit("export holds no records")
    if input_name:
        hits = [r for r in recs if sta._same_person(input_name, r["input_name"])]
        if len(hits) != 1:
            raise SystemExit(f"--input-name {input_name!r} matched {len(hits)} records; "
                             f"export holds: {[r['input_name'] for r in recs]}")
        return hits[0]
    if len(recs) > 1:
        raise SystemExit(f"export holds {len(recs)} records; pick one with --input-name "
                         f"(one of {[r['input_name'] for r in recs]})")
    return recs[0]


def build_plan(rec: dict, primary: list[str], rel_overrides: dict[str, str] | None = None) -> dict:
    """Split one parsed SmartSkip record into phone-list people and board people.

    `primary` holds "subject" and/or names of relatives (the top heir / PR).
    `rel_overrides` corrects SmartSkip's coarse labels from the obituary
    research: {"Jane Doe": "Daughter"}.

    A number that a primary person holds goes in the phone list ONCE and is
    dropped from every board line (the owner rule on a shared household line).
    A shared number carries no relationship tag: it is not provably one
    person's.
    """
    if not primary:
        raise SystemExit("--primary is required: 'subject' and/or the top heir / PR by name")
    rel_overrides = rel_overrides or {}

    def override(name, default):
        for k, v in rel_overrides.items():
            if sta._same_person(k, name):
                return v
        return default

    subject = {"name": f"{rec['first']} {rec['last']}".strip(), "relationship": "",
               "phones": rec["subject_phones"], "is_subject": True,
               "deceased": rec.get("deceased", False)}
    people = [subject] + [
        {"name": r["name"], "relationship": override(r["name"], r["canon_rel"]),
         "phones": r["phones"], "is_subject": False, "deceased": r.get("deceased", False),
         "age": r.get("age", ""), "city": r.get("mailing_city", ""),
         "state": r.get("mailing_state", "")}
        for r in rec["relatives"]]

    chosen: list[dict] = []
    for want in primary:
        if want.strip().lower() == "subject":
            hits = [subject]
        else:
            hits = [p for p in people if sta._same_person(want, p["name"])]
        if len(hits) != 1:
            raise SystemExit(f"--primary {want!r} matched {len(hits)} people in this record: "
                             f"{[p['name'] for p in people]}")
        if hits[0] not in chosen:
            chosen.append(hits[0])

    holders: dict[str, set[str]] = {}
    for p in people:
        for ph in p["phones"]:
            holders.setdefault(ph["number"], set()).add(p["name"])

    primary_numbers = {ph["number"] for p in chosen for ph in p["phones"]}
    primary_out = []
    for p in chosen:
        rel = "" if p["is_subject"] else account_rel_tag(p["relationship"])
        primary_out.append({
            "name": p["name"], "is_subject": p["is_subject"],
            "phones": [{"number": ph["number"], "type": _phone_type(ph.get("type")),
                        # Only an unshared number can carry a relationship tag.
                        "relationship_tag": rel if rel and len(holders[ph["number"]]) == 1 else ""}
                       for ph in p["phones"]]})

    board_out = []
    placed = set(primary_numbers)
    for p in people:
        if p in chosen:
            continue
        rel = "" if p["is_subject"] else account_rel_tag(p["relationship"])
        tagged = []
        for ph in p["phones"]:
            if ph["number"] in placed:        # each number written once
                continue
            placed.add(ph["number"])
            tagged.append({"number": ph["number"], "type": _phone_type(ph.get("type")),
                           "relationship_tag": rel if rel and len(holders[ph["number"]]) == 1 else ""})
        label = "Owner (traced subject)" if p["is_subject"] else (p["relationship"] or "Relative")
        board_out.append({
            "name": p["name"], "relationship": label, "deceased": p["deceased"],
            "extra": ", ".join(x for x in (f"age {p['age']}" if p.get("age") else "",
                                            " ".join(x for x in (p.get("city"), p.get("state")) if x))
                               if x),
            "phones": [ph["number"] for ph in p["phones"] if ph["number"] not in primary_numbers],
            "shared": [ph["number"] for ph in p["phones"] if ph["number"] in primary_numbers],
            "phone_list": tagged})
    return {"subject": subject["name"], "primary": primary_out, "board": board_out}


# ── the board post: written by `write`, read back by `promote` ────────

def build_board_post(plan: dict, *, today: str | None = None) -> str:
    today = today or date.today().isoformat()
    lines = [f"{BOARD_MARKER} ({today})",
             f"Traced: {plan['subject']}. Phone list holds only: "
             + ", ".join(p["name"] for p in plan["primary"]) + ".",
             "These numbers are NOT scored and NOT on the phone list. "
             "If the phone list numbers come up empty, ask Claude to add the relatives for this record.",
             ""]
    n = 0
    for b in plan["board"]:
        rel = b["relationship"] + (" (flagged deceased, unverified)" if b["deceased"] else "")
        extra = b["extra"]
        if b["shared"]:
            extra = "; ".join(x for x in (extra, "also holds " + ", ".join(
                f"...{s[-4:]}" for s in b["shared"]) + " (on the phone list)") if x)
        for num in (b["phones"] or ["none"]):
            lines.append(" | ".join(["RELATIVE", b["name"], rel, num, extra]).rstrip(" |"))
            n += num != "none"
    if not plan["board"]:
        lines.append("(SmartSkip returned no relatives)")
    lines += ["", f"{n} relative number(s) held here."]
    return "\n".join(lines)


def build_family_post(plan: dict, *, today: str | None = None) -> str:
    """Who is who, for a caller looking at a list of scored numbers. Every
    number here is already on the phone list; relationship tags cannot carry a
    name, so this is where the names live."""
    today = today or date.today().isoformat()
    lines = [f"{FAMILY_MARKER} ({today})",
             f"Traced: {plan['subject']}. Call first: "
             + ", ".join(p["name"] for p in plan["primary"]) + ".",
             "Every number below is on the phone list with its Trestle dial tier.", ""]
    for p in plan["primary"]:
        who = "Owner (traced subject)" if p["is_subject"] else "Call first"
        for ph in p["phones"] or [{"number": "none"}]:
            lines.append(" | ".join(["CALL FIRST", p["name"], who, ph["number"]]))
    for b in plan["board"]:
        rel = b["relationship"] + (" (flagged deceased, unverified)" if b["deceased"] else "")
        extra = b["extra"]
        if b["shared"]:
            extra = "; ".join(x for x in (extra, "also holds " + ", ".join(
                f"...{s[-4:]}" for s in b["shared"])) if x)
        for num in (b["phones"] or ["none"]):
            lines.append(" | ".join(["FAMILY", b["name"], rel, num, extra]).rstrip(" |"))
    if not plan["board"]:
        lines.append("(SmartSkip returned no relatives)")
    return "\n".join(lines)


def find_family_post(posts: list[dict]) -> dict | None:
    """Newest post carrying the family marker."""
    for p in posts:
        if FAMILY_MARKER in _plain(p.get("message") or ""):
            return p
    return None


def _plain(text: str) -> str:
    """Board text as plain lines, whatever markup the server hands back."""
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text or "")
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return t.replace("\r", "")


def parse_board_post(text: str) -> list[dict]:
    """The RELATIVE lines of a relatives post -> [{name, relationship, number}].
    Lines with no number ('none') are dropped; they have nothing to add."""
    out = []
    for line in _plain(text).split("\n"):
        line = line.strip()
        if not line.startswith(LINE_PREFIX):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            continue
        number = sta.norm_phone(parts[3])
        if not number:
            continue
        rel = re.sub(r"\s*\(flagged deceased.*\)$", "", parts[2]).strip()
        out.append({"name": parts[1], "relationship": rel, "number": number,
                    "deceased": "flagged deceased" in parts[2]})
    return out


def find_relatives_post(posts: list[dict]) -> dict | None:
    """Newest post carrying the marker (get_message_board is newest first)."""
    for p in posts:
        if BOARD_MARKER in _plain(p.get("message") or ""):
            return p
    return None


# ── CRM writes ────────────────────────────────────────────────────────

def _resolve(street: str, city: str, state: str) -> tuple[str, str, dict]:
    hit = _api.find_property_by_address(street, city, state, strict=True)
    if not hit:
        raise SystemExit(f"no DataSift record found for {street}, {city} - create it first")
    rec = _api.get_property(hit["uuid"])
    owner_uuid = (rec.get("owner") or {}).get("uuid")
    if not owner_uuid:
        raise SystemExit(f"record {hit['uuid']} has no owner")
    return hit["uuid"], owner_uuid, rec


def _already_tiered(rec: dict) -> set[str]:
    """Numbers on the record that already carry a dial tier or the litigator
    tag: scoring them again buys the same answer twice."""
    out = set()
    for p in ((rec.get("owner") or {}).get("phones") or []):
        tags = set(p.get("tags") or [])
        if any(t.startswith("Dial ") or t == "Drop" for t in tags) or sta.PHONE_TAG_LITIGATOR_RISK in tags:
            n = sta.norm_phone(p.get("number"))
            if n:
                out.add(n)
    return out


def _write_phones(property_uuid: str, owner_uuid: str, rec: dict, people: list[dict],
                  *, label: str, dry_run: bool) -> dict:
    """Score + upsert + tag a set of numbers. `people` is
    [{"name", "phones": [{"number", "type", "relationship_tag"}]}].
    Same safety as skip_trace_agent.writeback: caller statuses carried into the
    upsert, tags verified by reading the record back.

    A number ALREADY on the record (an earlier skip trace, then dialed) is
    never re-upserted: the upsert replaces the phone object, so it could only
    overwrite its line type with SmartSkip's guess or put a status at risk. It
    gets the SmartSkip source tag (true: SmartSkip returned it) and a tier only
    if it never had one. No relationship tag: whose number it is was settled
    before SmartSkip, and phone tags cannot be removed (Jeff, 2026-10-20)."""
    skip = _already_tiered(rec)
    on_record = {sta.norm_phone(p.get("number"))
                 for p in ((rec.get("owner") or {}).get("phones") or [])} - {None}
    subjects = [{"name": label, "property_address": property_uuid, "people": [
        {"phones": [{"number": ph["number"], "type_raw": ph.get("type") or "",
                     "sources": [SOURCE_SMARTSKIP]}
                    for ph in p["phones"] if ph["number"] not in skip]}
        for p in people]}]
    scored = sta.score_phones(subjects, dry_run=dry_run)
    by_number = {ph["number"]: ph for p in subjects[0]["people"] for ph in p["phones"]}

    phones, number_to_tags = [], {}
    for p in people:
        for ph in p["phones"]:
            n = ph["number"]
            s = by_number.get(n, {})
            if n not in on_record:
                entry = {"number": n}
                if s.get("type_raw") or ph.get("type"):
                    entry["type"] = s.get("type_raw") or ph.get("type")
                phones.append(entry)
            tags = [SOURCE_SMARTSKIP]
            if s.get("litigator_risk"):
                tags.append(sta.PHONE_TAG_LITIGATOR_RISK)   # REPLACES the tier
            elif s.get("tier"):
                tags.append(s["tier"])
            if ph.get("relationship_tag") and n not in on_record:
                tags.append(ph["relationship_tag"])
            number_to_tags.setdefault(n, []).extend(tags)

    out = {"numbers": len(number_to_tags), "new_numbers": len(phones),
           "already_on_record": len(on_record & set(number_to_tags)),
           "scored": len(scored), "already_scored": len(skip & set(number_to_tags)),
           "ok": True, "missing": {}}
    if not number_to_tags:
        return out
    if phones:
        current = sta._current_phone_state(property_uuid)
        for e in phones:
            e.update(current.get(e["number"], {}))
        _api.upsert_phones(owner_uuid, phones)
    check = _api.apply_phone_tags_verified(
        owner_uuid, {n: sorted(set(t)) for n, t in number_to_tags.items()})
    out.update(ok=check["ok"], missing=check.get("missing", {}))
    return out


def write_record(rec: dict, plan: dict, *, dry_run: bool = True, repost: bool = False) -> dict:
    """`write`: every number to the phone list, scored; who-is-who on the board."""
    _api.set_dry_run(dry_run)
    try:
        uuid, owner_uuid, crm = _resolve(rec["property_address"], rec["property_city"],
                                         rec["property_state"])
        people = plan["primary"] + [{"name": b["name"], "phones": b["phone_list"]}
                                    for b in plan["board"] if b["phone_list"]]
        res = _write_phones(uuid, owner_uuid, crm, people,
                            label=plan["subject"], dry_run=dry_run)
        tags = [TAG_SMARTSKIP_SKIPPED] + ([sta.TAG_TRESTLE_SCORED] if res["scored"] else [])
        _api.add_tags(uuid, tags)
        res["property_tags"] = tags

        existing = find_family_post(_api.get_message_board(owner_uuid))
        if existing and not repost:
            logger.warning("record already has a family post (%s) - not posting a second; "
                           "pass --repost to post anyway", existing.get("created"))
            res["posted"] = False
        else:
            _api.post_message_board(owner_uuid, build_family_post(plan))
            res["posted"] = True
        res.update(property_uuid=uuid)
        return res
    finally:
        _api.set_dry_run(False)


def promote_record(street: str, city: str, state: str, *, only: set[str] | None = None,
                   dry_run: bool = True) -> dict:
    """`promote`: the relatives post's numbers onto the phone list, scored."""
    _api.set_dry_run(dry_run)
    try:
        uuid, owner_uuid, crm = _resolve(street, city, state)
        post = find_relatives_post(_api.get_message_board(owner_uuid))
        if not post:
            raise SystemExit("no SmartSkip relatives post on this record's Message Board")
        rows = parse_board_post(post.get("message") or "")
        on_record = {sta.norm_phone(p.get("number"))
                     for p in ((crm.get("owner") or {}).get("phones") or [])}
        already = [r for r in rows if r["number"] in on_record]
        rows = [r for r in rows if r["number"] not in on_record]
        if only:
            wanted = {account_rel_tag(o) for o in only}
            rows = [r for r in rows if account_rel_tag(r["relationship"]) in wanted]

        holders: dict[str, set[str]] = {}
        for r in rows:
            holders.setdefault(r["number"], set()).add(r["name"])
        people: dict[str, dict] = {}
        for r in rows:
            p = people.setdefault(r["name"], {"name": r["name"], "phones": []})
            if r["number"] in {ph["number"] for ph in p["phones"]}:
                continue
            tag = account_rel_tag(r["relationship"]) if len(holders[r["number"]]) == 1 else ""
            if r["relationship"].startswith("Owner"):
                tag = ""                                  # the traced subject is nobody's relative
            p["phones"].append({"number": r["number"], "type": "", "relationship_tag": tag})

        res = {"property_uuid": uuid, "already_on_record": len(already),
               "to_add": sum(len(p["phones"]) for p in people.values())}
        if not people:
            res.update(numbers=0, scored=0, ok=True, posted=False)
            return res
        res.update(_write_phones(uuid, owner_uuid, crm, list(people.values()),
                                 label=street, dry_run=dry_run))
        if res["scored"]:
            _api.add_tags(uuid, [sta.TAG_TRESTLE_SCORED])
        names = ", ".join(f"{p['name']} ({len(p['phones'])})" for p in people.values())
        _api.post_message_board(owner_uuid,
            f"{PROMOTED_MARKER} ({date.today().isoformat()})\n"
            f"Added and scored {res['numbers']} number(s) after the primary numbers came up empty: "
            f"{names}.")
        res["posted"] = True
        return res
    finally:
        _api.set_dry_run(False)


# ── CLI ───────────────────────────────────────────────────────────────

def _overrides(items: list[str]) -> dict[str, str]:
    out = {}
    for it in items or []:
        name, _, rel = it.partition("=")
        if not rel:
            raise SystemExit(f"--relationship wants NAME=Relationship, got {it!r}")
        out[name.strip()] = rel.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write", help="SmartSkip export -> every number on the phone list, scored")
    w.add_argument("--export", required=True)
    w.add_argument("--input-name", help="which record, when the export holds several")
    w.add_argument("--primary", action="append", required=True,
                   help="'subject' or a relative's name; repeat for co-owners")
    w.add_argument("--relationship", action="append", help="NAME=Relationship correction")
    w.add_argument("--repost", action="store_true")
    w.add_argument("--commit", action="store_true", help="actually write and bill Trestle")
    p = sub.add_parser("promote", help="old-rule records only: board relatives -> phone list")
    p.add_argument("--street", required=True)
    p.add_argument("--city", default="")
    p.add_argument("--state", default="OK")
    p.add_argument("--only", help="comma list, e.g. Son,Daughter,Wife,Husband")
    p.add_argument("--commit", action="store_true", help="actually write and bill Trestle")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dry = not a.commit

    if a.cmd == "write":
        rec = pick_record(smartskip_parse.parse(a.export), a.input_name)
        plan = build_plan(rec, a.primary, _overrides(a.relationship))
        n = (sum(len(p["phones"]) for p in plan["primary"])
             + sum(len(b["phone_list"]) for b in plan["board"]))
        print(f"PHONE LIST: {n} numbers, all Trestle scored (about ${n * 0.015:.2f}). Call first:",
              "; ".join(f"{p['name']} ({len(p['phones'])})" for p in plan["primary"]))
        print("-" * 60 + "\n" + build_family_post(plan) + "\n" + "-" * 60)
        res = write_record(rec, plan, dry_run=dry, repost=a.repost)
    else:
        only = {s.strip() for s in a.only.split(",")} if a.only else None
        res = promote_record(a.street, a.city, a.state, only=only, dry_run=dry)
    print(("DRY RUN - nothing written, nothing billed. " if dry else "") + str(res))
    return 0 if res.get("ok", True) else 2


if __name__ == "__main__":
    sys.exit(main())
