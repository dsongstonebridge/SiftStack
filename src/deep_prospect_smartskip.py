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
#: First line of the family post since 2026-10-23 (scoped scoring: only some
#: numbers reach the phone list). find_family_post matches both markers.
FAMILY_MARKER_V2 = "SMARTSKIP FAMILY - WHO IS WHO"
BOARD_ONLY = "board only - not on phone list"
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


#: Trestle scope (Jeff, 2026-10-23, standing): a living owner's record scores only
#: the subject and the spouse; a deceased owner's record never scores the dead
#: owner and scores close family only. Catch-all "Relative" and in-laws: never.
SPOUSE_RELS = {"Spouse", "Husband", "Wife"}
CLOSE_RELS = SPOUSE_RELS | {"Child", "Son", "Daughter", "Parent", "Mother", "Father",
                            "Sibling", "Brother", "Sister"}


def _match(want: str, people: list[dict]) -> list[dict]:
    """People matching `want`. "Name@Relationship" narrows two people with the
    same name (3903 S Union: the traced son and his father are both Lenord Coats)."""
    want, _, rel = want.partition("@")
    pool = [p for p in people if not rel or
            (p["relationship"] or "").strip().lower() == rel.strip().lower()]
    hits = [p for p in pool if sta._same_person(want, p["name"])]
    if len(hits) > 1:
        # An initial-only relative ("M COATS") fuzzy-matches anyone with that
        # surname; an exact full-name match wins (2026-10-23).
        exact = [p for p in hits if p["name"].strip().lower() == want.strip().lower()]
        hits = exact or hits
    return hits


def build_plan(rec: dict, primary: list[str], rel_overrides: dict[str, str] | None = None,
               *, subject_deceased: bool = False, score_add: list[str] | None = None,
               score_drop: list[str] | None = None, owner_deceased: bool | None = None) -> dict:
    """Split one parsed SmartSkip record into phone-list people and board people.

    `primary` holds "subject" and/or names of relatives (the top heir / PR).
    `rel_overrides` corrects SmartSkip's coarse labels from the obituary
    research: {"Jane Doe": "Daughter"}.
    `subject_deceased`: the research (obituary / probate filing) CONFIRMS the
    traced subject died. The subject is then labelled "Decedent" instead of
    "Owner", with no "flagged deceased, unverified" note (Jeff, 2026-10-20).
    SmartSkip's own flag never decides this; it is wrong too often.

    A number that a primary person holds goes in the phone list ONCE and is
    dropped from every board line (the owner rule on a shared household line).
    A shared number carries no relationship tag: it is not provably one
    person's.

    Who is SCORED (on the phone list, Trestle-scored) follows SPOUSE_RELS /
    CLOSE_RELS above; everyone else is board-only (named on the post, never
    added to the phone list, never scored). `score_add` / `score_drop` (names,
    or "subject") correct what SmartSkip's labels cannot know: a co-owner on
    title, or a PR SmartSkip calls a catch-all "Relative".
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
               "deceased": False if subject_deceased else rec.get("deceased", False)}
    subject_label = "Decedent (traced subject)" if subject_deceased else "Owner (traced subject)"
    people = [subject] + [
        {"name": r["name"], "relationship": override(r["name"], r["canon_rel"]),
         "phones": r["phones"], "is_subject": False, "deceased": r.get("deceased", False),
         "age": r.get("age", ""), "city": r.get("mailing_city", ""),
         "state": r.get("mailing_state", "")}
        for r in rec["relatives"]]

    def find(want: str, flag: str) -> dict:
        hits = [subject] if want.strip().lower() == "subject" else _match(want, people)
        if len(hits) != 1:
            raise SystemExit(f"{flag} {want!r} matched {len(hits)} people in this record: "
                             f"{[p['name'] for p in people]}")
        return hits[0]

    chosen: list[dict] = []
    for want in primary:
        hit = find(want, "--primary")
        if hit not in chosen:
            chosen.append(hit)

    # owner_deceased: the OWNER died even when the traced subject did not (the
    # traced person is the owner's living son). Scoring then follows the
    # deceased-owner rule while the subject keeps the "Owner" label.
    dead_owner = subject_deceased if owner_deceased is None else owner_deceased
    for p in people:
        if p["is_subject"]:
            p["scored"] = not subject_deceased
        else:
            p["scored"] = (p["relationship"] or "").strip().title() in (
                CLOSE_RELS if dead_owner else SPOUSE_RELS)
    for want in score_add or []:
        find(want, "--score")["scored"] = True
    for want in score_drop or []:
        find(want, "--no-score")["scored"] = False

    holders: dict[str, set[str]] = {}
    for p in people:
        for ph in p["phones"]:
            holders.setdefault(ph["number"], set()).add(p["name"])

    primary_numbers = {ph["number"] for p in chosen for ph in p["phones"]}
    primary_out = []
    for p in chosen:
        rel = "" if p["is_subject"] else account_rel_tag(p["relationship"])
        primary_out.append({
            "name": p["name"], "is_subject": p["is_subject"], "relationship": p["relationship"],
            "scored": p["scored"],
            "phones": [{"number": ph["number"], "type": _phone_type(ph.get("type")),
                        # Only an unshared number can carry a relationship tag.
                        "relationship_tag": rel if rel and len(holders[ph["number"]]) == 1 else ""}
                       for ph in p["phones"]]})

    board_out = []
    # Only numbers that reach the phone list count as placed, so a number an
    # unscored person shares with a scored one still reaches the phone list.
    placed = {ph["number"] for p in chosen if p["scored"] for ph in p["phones"]}
    for p in people:
        if p in chosen:
            continue
        rel = "" if p["is_subject"] else account_rel_tag(p["relationship"])
        tagged = []
        for ph in p["phones"]:
            if not p["scored"] or ph["number"] in placed:   # each number written once
                continue
            placed.add(ph["number"])
            tagged.append({"number": ph["number"], "type": _phone_type(ph.get("type")),
                           "relationship_tag": rel if rel and len(holders[ph["number"]]) == 1 else ""})
        label = subject_label if p["is_subject"] else (p["relationship"] or "Relative")
        board_out.append({
            "name": p["name"], "relationship": label, "deceased": p["deceased"],
            "extra": ", ".join(x for x in (f"age {p['age']}" if p.get("age") else "",
                                            " ".join(x for x in (p.get("city"), p.get("state")) if x))
                               if x),
            "phones": [ph["number"] for ph in p["phones"] if ph["number"] not in primary_numbers],
            "shared": [ph["number"] for ph in p["phones"] if ph["number"] in primary_numbers],
            "phone_list": tagged, "scored": p["scored"]})
    return {"subject": subject["name"], "primary": primary_out, "board": board_out,
            "phone_list_numbers": placed,
            "scored_names": [p["name"] for p in people if p["scored"]]}


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


OWNER_STATUSES = ("dead", "alive", "unverified")
CONFIDENCE_LEVELS = ("high", "medium")
# Sources that can prove a death without an obituary. SmartSkip is never one
# (v5 gotcha 1; same-name incident, 2026-10-27).
_FILING_PROOF = re.compile(r"probate|PB-\d|court|death certificate|letters|order appointing|"
                           r"filing|petition", re.I)


def owner_status_line(status: str, proof: str, owner: str, *, obits: dict | None = None,
                      confidence: str = "") -> str:
    """The first line of every deep prospecting board post and PDF (Jeff,
    2026-10-27): what OUR research says about whether the owner is alive or
    dead, never SmartSkip's deceased flag. Refuses a claim the evidence does
    not carry: on 2026-10-27 a record was called as an heir case on a name-only
    obituary plus SmartSkip's flag, and the 'son' was a same-name stranger
    in another state."""
    status = (status or "").strip().lower()
    proof = (proof or "").strip()
    confidence = (confidence or "").strip().lower()
    if status not in OWNER_STATUSES:
        raise SystemExit(f"--owner-status must be one of {', '.join(OWNER_STATUSES)}")
    if "smartskip" in proof.lower():
        raise SystemExit("--status-proof cites SmartSkip. SmartSkip's deceased flag is not proof "
                         "either way; cite the obituary, the filing, or a conversation.")
    confirmed = [c for c in (obits or {}).get("candidates", [])
                 if str(c.get("verdict", "")).startswith("CONFIRMED")]
    if status in ("dead", "alive") and not proof:
        raise SystemExit(f"--owner-status {status} needs --status-proof (the source that verifies it)")
    if status == "dead" and not confirmed and not _FILING_PROOF.search(proof):
        raise SystemExit("--owner-status dead needs an obituary CONFIRMED against known family "
                         "(obituary_check.json) or a filing/court/death-certificate source in "
                         "--status-proof. A name match is not enough: use --owner-status unverified.")
    if status == "alive" and confirmed:
        raise SystemExit("--owner-status alive contradicts a CONFIRMED obituary in obituary_check.json")
    # How sure we are, on the same line (Jeff, 2026-10-28). Unverified is low
    # by definition; a low-confidence "dead" or "alive" is really unverified.
    if status in ("dead", "alive") and confidence not in CONFIDENCE_LEVELS:
        raise SystemExit(f"--owner-status {status} needs --confidence high or medium "
                         "(if it is lower than that, use --owner-status unverified)")
    if status == "dead":
        return f"OWNER STATUS: DECEASED ({confidence} confidence) - {owner}. Proof: {proof}"
    if status == "alive":
        return f"OWNER STATUS: LIVING ({confidence} confidence) - {owner}. Proof: {proof}"
    return (f"OWNER STATUS: NOT VERIFIED (low confidence) - our research has not confirmed whether "
            f"{owner} is living or deceased. Do not tell anyone the owner died."
            + (f" Notes: {proof}" if proof else ""))


def _fmt(num: str) -> str:
    return f"{num[:3]}-{num[3:6]}-{num[6:]}" if len(num) == 10 and num.isdigit() else num


def build_family_post(plan: dict, *, today: str | None = None,
                      tiers: dict[str, str] | None = None, status_line: str = "") -> str:
    """Who is who, for a caller looking at a list of scored numbers: a dial
    sheet on its own (Jeff, 2026-10-20), each number with its dial tier, or
    "board only" for a number kept off the phone list and never scored
    (2026-10-23 scope). Relationship tags cannot carry a name, so this is
    where the names live."""
    today = today or date.today().isoformat()
    tiers = tiers or {}
    on_list = plan.get("phone_list_numbers")

    def tier(num):
        if num == "none":
            return ""
        if on_list is not None and num not in on_list:
            return BOARD_ONLY
        return tiers.get(num) or "not scored yet"

    lines = ([status_line] if status_line else []) + [
             f"{FAMILY_MARKER_V2} ({today})",
             f"Traced: {plan['subject']}. Call first: "
             + ", ".join(p["name"] for p in plan["primary"]) + ".",
             "Name | relationship | number | dial tier. Numbers marked '" + BOARD_ONLY
             + "' were not added or scored (living owner: owner + spouse only; "
               "deceased owner: close family only).", ""]
    for p in plan["primary"]:
        who = "Owner (traced subject)" if p["is_subject"] else (p.get("relationship") or "Call first")
        for ph in p["phones"] or [{"number": "none"}]:
            lines.append(" | ".join(["CALL FIRST", p["name"], who, _fmt(ph["number"]),
                                     tier(ph["number"])]).rstrip(" |"))
    for b in plan["board"]:
        rel = b["relationship"] + (" (flagged deceased, unverified)" if b["deceased"] else "")
        extra = b["extra"]
        if b["shared"]:
            extra = "; ".join(x for x in (extra, "also holds " + ", ".join(
                f"...{s[-4:]}" for s in b["shared"])) if x)
        for num in (b["phones"] or ["none"]):
            lines.append(" | ".join(["FAMILY", b["name"], rel, _fmt(num), tier(num), extra])
                         .rstrip(" |"))
    if not plan["board"]:
        lines.append("(SmartSkip returned no relatives)")
    return "\n".join(lines)


def find_family_post(posts: list[dict]) -> dict | None:
    """Newest post carrying the family marker."""
    for p in posts:
        text = _plain(p.get("message") or "")
        if FAMILY_MARKER in text or FAMILY_MARKER_V2 in text:
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
    gets the SmartSkip source tag (true: SmartSkip returned it) and is never
    Trestle-scored again, tiered or not: only numbers NOT already in the CRM
    are scored (Jeff, 2026-10-23). No relationship tag: whose number it is was
    settled before SmartSkip, and phone tags cannot be removed (2026-10-20)."""
    on_record = {sta.norm_phone(p.get("number"))
                 for p in ((rec.get("owner") or {}).get("phones") or [])} - {None}
    skip = _already_tiered(rec) | on_record
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
            if n not in on_record and n not in {e["number"] for e in phones}:
                # A number two relatives share is upserted once (2026-10-23).
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

    # Each number's dial tier for the family post: this run's score, else the
    # tier it already carried on the record. Litigator replaces the tier.
    existing = {}
    for p in ((rec.get("owner") or {}).get("phones") or []):
        n = sta.norm_phone(p.get("number"))
        t = [x for x in (p.get("tags") or [])
             if x.startswith("Dial ") or x == "Drop" or x == sta.PHONE_TAG_LITIGATOR_RISK]
        if n and t:
            existing[n] = sta.PHONE_TAG_LITIGATOR_RISK if sta.PHONE_TAG_LITIGATOR_RISK in t else t[0]
    tiers = {}
    for n in number_to_tags:
        s = by_number.get(n, {})
        tiers[n] = (sta.PHONE_TAG_LITIGATOR_RISK if s.get("litigator_risk")
                    else s.get("tier") or existing.get(n, ""))

    out = {"numbers": len(number_to_tags), "new_numbers": len(phones),
           "already_on_record": len(on_record & set(number_to_tags)),
           "scored": len(scored), "already_scored": len(skip & set(number_to_tags)),
           "tiers": tiers, "ok": True, "missing": {}}
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


def write_record(rec: dict, plan: dict, *, dry_run: bool = True, repost: bool = False,
                 status_line: str = "") -> dict:
    """`write`: every number to the phone list, scored; who-is-who on the board."""
    _api.set_dry_run(dry_run)
    try:
        uuid, owner_uuid, crm = _resolve(rec["property_address"], rec["property_city"],
                                         rec["property_state"])
        # Only SCORED people reach the phone list (2026-10-23 scope); a call-first
        # person outside the scope is named on the board post only.
        people = [p for p in plan["primary"] if p.get("scored", True)] + [
            {"name": b["name"], "phones": b["phone_list"]}
            for b in plan["board"] if b["phone_list"]]
        res = _write_phones(uuid, owner_uuid, crm, people,
                            label=plan["subject"], dry_run=dry_run)
        tags = [TAG_SMARTSKIP_SKIPPED] + ([sta.TAG_TRESTLE_SCORED] if res["scored"] else [])
        _api.add_tags(uuid, tags)
        res["property_tags"] = tags

        # Posts go on the PROPERTY board, where the team works; nothing is posted
        # to an owner record (Jeff, 2026-10-21/23). Both boards are checked so an
        # older owner-board family post still counts as already posted.
        existing = find_family_post(_api.get_property_message_board(uuid)
                                    + _api.get_message_board(owner_uuid))
        if existing and not repost:
            logger.warning("record already has a family post (%s) - not posting a second; "
                           "pass --repost to post anyway", existing.get("created"))
            res["posted"] = False
        else:
            _api.post_property_message_board(uuid, build_family_post(
                plan, tiers=res.get("tiers"), status_line=status_line))
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
        post = find_relatives_post(_api.get_property_message_board(uuid)
                                   + _api.get_message_board(owner_uuid))
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
            if r["relationship"].startswith(("Owner", "Decedent")):
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
        _api.post_property_message_board(uuid,
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
    w.add_argument("--decedent", action="store_true",
                   help="research CONFIRMS the traced subject died: label them Decedent, not Owner")
    w.add_argument("--owner-status", required=True, choices=OWNER_STATUSES,
                   help="what OUR research verifies about the owner (never SmartSkip's flag)")
    w.add_argument("--status-proof", default="", help="the source that verifies it")
    w.add_argument("--confidence", choices=CONFIDENCE_LEVELS,
                   help="how sure the research is (needed for dead/alive)")
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
        if a.decedent and a.owner_status != "dead":
            raise SystemExit("--decedent needs --owner-status dead (research-verified)")
        plan = build_plan(rec, a.primary, _overrides(a.relationship), subject_deceased=a.decedent)
        line = owner_status_line(a.owner_status, a.status_proof, plan["subject"],
                                 confidence=a.confidence or "")
        n = (sum(len(p["phones"]) for p in plan["primary"])
             + sum(len(b["phone_list"]) for b in plan["board"]))
        print(f"PHONE LIST: {n} numbers, all Trestle scored (about ${n * 0.015:.2f}). Call first:",
              "; ".join(f"{p['name']} ({len(p['phones'])})" for p in plan["primary"]))
        print("-" * 60 + "\n" + build_family_post(plan, status_line=line) + "\n" + "-" * 60)
        res = write_record(rec, plan, dry_run=dry, repost=a.repost, status_line=line)
    else:
        only = {s.strip() for s in a.only.split(",")} if a.only else None
        res = promote_record(a.street, a.city, a.state, only=only, dry_run=dry)
    print(("DRY RUN - nothing written, nothing billed. " if dry else "") + str(res))
    return 0 if res.get("ok", True) else 2


if __name__ == "__main__":
    sys.exit(main())
