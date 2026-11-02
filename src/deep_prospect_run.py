"""Deep prospect DataSift records, start to finish, in three commands.

    1. batch   FREE. Every record goes in ONE SmartSkip order: reads each record,
               runs its obituary check and owner profile (Assessor, Treasurer, LOCCAT, CRM, web,
               identity), builds one upload, runs SmartSkip's free
               count. Stops and prints what the charge would be. (`start
               --only-one` is the same thing when there truly is only one.)
    2. charge  BILLS the SmartSkip BALANCE, once, for the whole order (~$0.15 per
               row). NEVER the card (Jeff, 2026-11-02): a short balance stops
               here and leaves the order unpaid. Waits, downloads, and splits
               the results back into each record's run folder.
    3. finish  Picks nothing on its own: Claude reads the boards, filing and obituaries,
               writes the record's dp_research.json, and passes --primary. Our research
               trumps SmartSkip: it sets the labels and who gets scored, and finish
               refuses without it. Only Dial First/Second numbers go on the record
               (Jeff, 2026-11-02); the rest go in one Message Board post, one line per
               person, and the research PDF in the Documents tab.
               DRY RUN until --commit.

    python src/deep_prospect_run.py batch --list records.json
        # [{"street": "123 Main St", "city": "Tulsa", "search_person": "Mary Smith",
        #   "spouse": "", "known": []}, ...]   probate: search the person who DIED
    python src/deep_prospect_run.py start --only-one --street "123 Main St" --city Tulsa
    python src/deep_prospect_run.py charge --order output/deep_prospect/_orders/<id>
    python src/deep_prospect_run.py finish --run output/deep_prospect/123-main-st \
        --primary "Susan Smith" --relationship "Susan Smith=Daughter" \
        --owner-status dead --confidence high --status-proof "obituary (Ninde) names Susan as daughter" \
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
import dp_board_split as split
import owner_profile
import skip_trace_agent as sta
import smartskip_parse

logger = logging.getLogger(__name__)

# One folder, whichever checkout runs this: the SMS agent holds a record whose run
# folder it cannot find (2026-11-02: eleven records landed in the wrong checkout).
RUNS_DIR = Path(os.getenv("SMS_AGENT_DP_RUNS_DIR") or Path(config.OUTPUT_DIR) / "deep_prospect")
PDF_MARKER = "DEEP PROSPECTING RESEARCH PDF"
PRICE_PER_HIT = 0.15
PRICE_CENTS = 15
ORDERS_DIR = RUNS_DIR / "_orders"
#: `start --only-one` is refused this soon after another order: that is one batch.
ONE_ORDER_WINDOW_SECONDS = 3600
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

def _prepare(a) -> dict | None:
    """Everything before the SmartSkip order, for one record: resolve it, refuse a
    repeat, write its input row, run its obituary check. None = skipped, with why printed."""
    uuid, owner_uuid, rec = dps._resolve(a.street, a.city, a.state)
    owner = rec.get("owner") or {}
    person = a.search_person or person_name(owner)
    if person and not a.search_person:
        # The CRM's own fields, so a compound surname ("Perez Rodriguez") is
        # searched whole rather than as its last word.
        person = f"{(owner.get('first_name') or '').strip()}|{(owner.get('last_name') or '').strip()}"
    if not person:
        print(f"SKIPPED {a.street}: the owner is a business or trust "
              f"({owner.get('company') or 'no person name'}). SmartSkip is not free for those. "
              "Name a person with search_person to trace them.")
        return None
    at_property = bool(a.search_person) or a.search_at == "property"
    run = RUNS_DIR / slug(a.street)
    why = prior_run_reason(run, rec)
    if why and not a.restart:
        print(f"REFUSED {a.street}, nothing created or charged: {why}\n"
              "Only if you really want a second SmartSkip search (a second charge), set restart.")
        return None
    if why:
        logger.warning("restart: %s Opening a NEW SmartSkip order anyway.", why)
        old = run / "state.json"
        if old.exists():
            old.rename(run / f"state.replaced-{int(time.time())}.json")
    run.mkdir(parents=True, exist_ok=True)

    row = build_input_row(person, rec, at_property=at_property)
    if not row["Mailing Address"]:
        print(f"SKIPPED {a.street}: no address to search SmartSkip with")
        return None
    with open(run / "smartskip_input.csv", "w", newline="", encoding="utf-8") as fh:
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
        logger.warning("no known family (no probate heirs on the board, no spouse/known): "
                       "obituary matches cannot be checked against survivors")
    obits = obituary_check.check(obit_name, city=row["Property City"] or a.city, state=a.state,
                                 street=row["Property Address"], spouse=a.spouse or "",
                                 known=known)
    (run / "obituary_check.json").write_text(json.dumps(obits, indent=2), encoding="utf-8")
    obit_txt = obituary_check.report(obits)
    (run / "obituary_check.txt").write_text(obit_txt, encoding="utf-8")

    # Everything free on the OWNER, not just death searches (Jeff, 2026-10-27).
    profile = owner_profile.build(person=person, street=a.street, city=row["Property City"] or a.city,
                                  state=a.state, property_uuid=uuid, owner_uuid=owner_uuid,
                                  owner_rec=owner, known=known + ([a.spouse] if a.spouse else []))
    (run / "owner_profile.json").write_text(json.dumps(profile, indent=2, default=str),
                                            encoding="utf-8")
    profile_txt = owner_profile.report(profile)
    (run / "owner_profile.txt").write_text(profile_txt, encoding="utf-8")
    print("\n" + profile_txt.encode("ascii", "replace").decode("ascii"))
    print("\n" + obit_txt.encode("ascii", "replace").decode("ascii"))
    return {"run": run, "row": row,
            "state": {"street": a.street, "city": a.city, "state": a.state, "property_uuid": uuid,
                      "owner_uuid": owner_uuid, "searched": person, "searched_at":
                      "property" if at_property else "mailing", "paid": False,
                      "started": date.today().isoformat()}}


def _open_order(items: list) -> int:
    """ONE SmartSkip order for every record in `items`, unpaid (Jeff, 2026-11-02)."""
    ss = _smartskip()           # before the obituary checks: a missing login fails fast
    prepared = [p for p in (_prepare(a) for a in items) if p]
    if not prepared:
        print("Nothing to order.")
        return 3
    order_id = f"{date.today().isoformat()}-{int(time.time())}"
    order = ORDERS_DIR / order_id
    order.mkdir(parents=True, exist_ok=True)
    inp = order / "smartskip_input.csv"
    with open(inp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(prepared[0]["row"]))
        w.writeheader()
        w.writerows(p["row"] for p in prepared)
    entry = ss.submit(ss.Session(), str(inp), {})
    for p in prepared:
        _save(p["run"], {**p["state"], "bulkSkipId": entry["bulkSkipId"], "order": order_id,
                         "billable_rows": 1})
    n = entry.get("entities") or len(prepared)
    (order / "order.json").write_text(json.dumps(
        {"bulkSkipId": entry["bulkSkipId"], "rows": n, "paid": False, "created_ts": time.time(),
         "runs": [str(p["run"]) for p in prepared]}, indent=2), encoding="utf-8")
    print(f"\nSMARTSKIP: ONE order, {len(prepared)} record(s), {n} billable row(s), at most "
          f"${n * PRICE_PER_HIT:.2f} from the balance. NOTHING CHARGED YET.\n"
          f"Next: python src/deep_prospect_run.py charge --order {order.as_posix()}")
    return 0


def _recent_order(now: float | None = None) -> str | None:
    """An order opened within ONE_ORDER_WINDOW_SECONDS, or None."""
    now = time.time() if now is None else now
    if not ORDERS_DIR.exists():
        return None
    for f in ORDERS_DIR.glob("*/order.json"):
        if now - json.loads(f.read_text(encoding="utf-8")).get("created_ts", 0) < ONE_ORDER_WINDOW_SECONDS:
            return f.parent.name
    return None


def cmd_batch(a) -> int:
    records = json.loads(Path(a.list).read_text(encoding="utf-8"))
    return _open_order([argparse.Namespace(
        street=r["street"], city=r.get("city", ""), state=r.get("state", "OK"),
        search_person=r.get("search_person"), search_at=r.get("search_at", "mailing"),
        spouse=r.get("spouse"), known=r.get("known"), restart=bool(r.get("restart")))
        for r in records])


def cmd_start(a) -> int:
    """One record alone, only when there truly is only one (Jeff, 2026-11-02)."""
    if not a.only_one:
        print("REFUSED, nothing created or charged: SmartSkip records go in ONE order. Use "
              "`batch --list records.json`. If there truly is only one, add --only-one.")
        return 7
    recent = _recent_order()
    if recent:
        print(f"REFUSED, nothing created or charged: order {recent} was opened in the last hour. "
              "Several singles in a row is a batch: put them in one `batch --list`.")
        return 7
    return _open_order([a])


# ── 2. charge (BILLS the SmartSkip balance, never the card) ─────

def pay_from_balance(sess, bid: str, rows: int) -> None:
    """Pay one order from the SmartSkip balance, the same call the web app makes.
    The body names no card, and there is no fallback: anything but a clean balance
    payment stops here with the order left unpaid (Jeff, 2026-11-02)."""
    code, wallet, _ = sess.call("GET", "/wallet/balance")
    if code != 200:
        raise SystemExit(f"could not read the SmartSkip balance ({code}). Nothing was charged.")
    have, need = int(wallet.get("balance") or 0), rows * PRICE_CENTS
    if have < need:
        raise SystemExit(f"SMARTSKIP BALANCE TOO LOW: ${have / 100:.2f} on hand, up to ${need / 100:.2f} "
                         f"needed for {rows} row(s). Nothing was charged and the card was not touched. "
                         "Top up the balance, then re-run this charge: the order is still open.")
    logger.warning("BILLED: SmartSkip %d row(s), up to $%.2f, from the balance ($%.2f on hand)",
                   rows, need / 100, have / 100)
    code, data, _ = sess.call("POST", "/bulk-skip/payment-intent",
                              body={"bulkSkipId": bid, "paymentSource": "wallet"})
    if code not in (200, 201) or not isinstance(data, dict):
        raise SystemExit(f"balance payment failed ({code}): {data}. The card was not touched.")
    if data.get("clientSecret") or not (data.get("walletTransactionId") or
                                        str(data.get("status") or "").lower() in ("succeeded", "paid", "completed")):
        raise SystemExit(f"balance payment did not go through: {data}. The card was not touched. "
                         "Check the order at https://app.smartskip.io/bulk-skip before retrying.")
    _, after, _ = sess.call("GET", "/wallet/balance")
    print(f"Paid from the balance. status={data.get('status')} balance now "
          f"${int((after or {}).get('balance') or 0) / 100:.2f}")


def _addr_key(street: str, city: str) -> str:
    return f"{' '.join((street or '').lower().split())}|{(city or '').strip().lower()}"


def split_export(export: Path, runs: list) -> list:
    """Write each run's share of one order's export to its own smartskip_export.csv,
    matched on the property address and city every row carries. Returns the runs
    that got nothing; a row no run claims is an error, never silently lost."""
    with open(export, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames
        rows = list(reader)
    by_key = {}
    for r in rows:
        by_key.setdefault(_addr_key(r.get("Property Address"), r.get("Property City")), []).append(r)
    empty = []
    for run in runs:
        row = next(csv.DictReader(open(run / "smartskip_input.csv", newline="", encoding="utf-8")))
        mine = by_key.pop(_addr_key(row.get("Property Address"), row.get("Property City")), [])
        if not mine:
            empty.append(run)
            continue
        with open(run / "smartskip_export.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(mine)
    if by_key:
        raise SystemExit(f"export rows matched no record: {sorted(by_key)}. See {export}")
    return empty


def cmd_charge(a) -> int:
    if not a.order and not a.run:
        raise SystemExit("charge needs --order (or --run for a record's order)")
    if a.order:
        order_dir = Path(a.order)
    else:
        state = _load(Path(a.run))
        if not state.get("order"):
            raise SystemExit("this run predates one-order runs and has no order to charge")
        order_dir = ORDERS_DIR / state["order"]
    order = json.loads((order_dir / "order.json").read_text(encoding="utf-8"))
    runs = [Path(r) for r in order["runs"]]
    bid = order["bulkSkipId"]
    ss = _smartskip()
    sess = ss.Session()
    if not order.get("paid"):
        pay_from_balance(sess, bid, order["rows"])
        order["paid"] = True
        (order_dir / "order.json").write_text(json.dumps(order, indent=2), encoding="utf-8")
        for run in runs:
            _save(run, {**_load(run), "paid": True})
    deadline = time.time() + a.wait
    status = ""
    while time.time() < deadline:
        items = ss.fetch_status(sess, bid)
        status = ((items[0].get("status") if items else "") or "").lower()
        if "complet" in status:
            break
        if status in ("error", "failed"):
            raise SystemExit(f"SmartSkip job {status}")
        time.sleep(15)
    else:
        print(f"still {status or 'not listed'} after {a.wait}s; re-run charge later (it will not pay twice)")
        return 4
    export = order_dir / "smartskip_export.csv"
    ss.download(sess, bid, str(export), "vertical")
    empty = split_export(export, runs)
    for run in runs:
        state = _load(run)
        if run in empty:
            print(f"\n{state['street']}: SmartSkip returned nothing for {state['searched']}.")
            continue
        state = {**state, "export": "smartskip_export.csv"}
        rec = dps.pick_record(smartskip_parse.parse(str(run / "smartskip_export.csv")), None)
        print(f"\n{state['street']}: {len(rec['subject_phones'])} number(s) for {state['searched']} "
              f"and {len(rec['relatives'])} relative(s):")
        for r in rec["relatives"]:
            print(f"  {r['name']:<28} {r['canon_rel']:<10} age {r.get('age') or '?':<4} "
                  f"{len(r['phones'])} number(s){'  [flagged deceased]' if r.get('deceased') else ''}")
        known = []
        oj = run / "obituary_check.json"
        if oj.exists():
            known = json.loads(oj.read_text(encoding="utf-8")).get("known") or []
        findings = owner_profile.identity_findings(rec, home_state=state.get("state") or "OK")
        state["identity"] = findings
        _save(run, state)
        flags = owner_profile.identity_after_smartskip(rec, known, home_state=state.get("state") or "OK")
        block = "\n== Identity check (after SmartSkip)\n" + "\n".join(f"- {f}" for f in flags) + "\n"
        pt = run / "owner_profile.txt"
        if pt.exists() and "Identity check (after SmartSkip)" not in pt.read_text(encoding="utf-8"):
            pt.write_text(pt.read_text(encoding="utf-8").rstrip() + "\n" + block, encoding="utf-8")
        elif not pt.exists():
            pt.write_text(block.lstrip(), encoding="utf-8")
        print(block)
        if any(f["level"] == "strong" for f in findings):
            # Likely two people merged: Jeff decides before anything is scored (2026-10-28).
            print(f"STOP {state['street']}: SmartSkip likely merged two people. Ask Jeff before "
                  "finish, then pass his decision as --mixup-call \"...\".")
    print("\nNext, per record: read obituary_check.txt and owner_profile.txt, write dp_research.json, then\n"
          "python src/deep_prospect_run.py finish --run <record's run folder> --primary ... --commit")
    return 0


# ── 3. finish (DRY RUN until --commit) ────────────────────────────────

def cmd_finish(a) -> int:
    run = Path(a.run)
    state = _load(run)
    if not state.get("export"):
        raise SystemExit("no SmartSkip results yet - run charge first")
    # Owner alive/dead comes from OUR research, never SmartSkip's flag, and
    # tops every post (Jeff, 2026-10-27, after a same-name mix-up).
    obits = json.loads((run / "obituary_check.json").read_text(encoding="utf-8"))
    status = getattr(a, "owner_status", None)
    if not status:
        raise SystemExit("--owner-status is required: dead, alive or unverified")
    if (a.decedent or getattr(a, "owner_deceased", False)) and status != "dead":
        raise SystemExit("--decedent / --owner-deceased need --owner-status dead, verified by an "
                         "obituary confirmed against known family or a filing. Otherwise the owner "
                         "is unverified and nobody is an heir yet.")
    research = split.load_research(run)
    if not research and getattr(a, "research", None):
        research = json.loads(Path(a.research).read_text(encoding="utf-8"))
    if not research:
        raise SystemExit(f"no {split.RESEARCH_FILE} in {run}: research trumps SmartSkip, so finish "
                         "will not run without it. See dp_board_split.load_research for the fields.")
    status_line = dps.owner_status_line(status, getattr(a, "status_proof", ""),
                                        state["searched"], obits=obits,
                                        confidence=getattr(a, "confidence", None) or "")
    rec = dps.pick_record(smartskip_parse.parse(str(run / state["export"])), None)
    # A likely mix-up (owner shares a number with a same-name person in another
    # state) waits for Jeff's call; a maybe is only noted (Jeff, 2026-10-28).
    findings = owner_profile.identity_findings(rec, home_state=state.get("state") or "OK")
    call = (getattr(a, "mixup_call", None) or "").strip()
    if any(f["level"] == "strong" for f in findings) and not call:
        raise SystemExit("SmartSkip likely merged two people:\n  "
                         + "\n  ".join(f["text"] for f in findings if f["level"] == "strong")
                         + "\nAsk Jeff, then re-run with --mixup-call \"<his decision>\".")
    note = owner_profile.mixup_note(findings, call)
    if note:
        status_line = f"{status_line}\n{note}"
    # The SmartSkip row carries the property address we sent; the DataSift
    # record is resolved from the run's own address so the two cannot drift.
    rec.update(property_address=state["street"], property_city=state["city"],
               property_state=state["state"])
    overrides = {**dps._overrides(a.relationship), **(research.get("relationships") or {})}
    plan = dps.build_plan(rec, a.primary or [], overrides,
                          subject_deceased=a.decedent, score_add=getattr(a, "score", None),
                          score_drop=(getattr(a, "no_score", None) or []) + list(research.get("dead") or [])
                          + list(research.get("not_family") or []),
                          owner_deceased=True if getattr(a, "owner_deceased", False) else None,
                          outside_primary=bool(research.get("call_first_outside")))
    problems = split.research_problems(research, split.people_from_plan(plan, {}, research), a.primary or [])
    if problems:
        raise SystemExit("REFUSED, nothing written:\n  " + "\n  ".join(problems))
    dry = not a.commit
    res = dps.write_record(rec, plan, research=research, dry_run=dry, repost=a.repost,
                           status_line=status_line, known_tiers=state.get("tiers"))

    notes_file = getattr(a, "notes", None)
    notes = Path(notes_file).read_text(encoding="utf-8") if notes_file else ""
    profile_file = run / "owner_profile.txt"
    after = split.phone_rows(_api.get_property(state["property_uuid"])) if not dry else res["rows_before"]
    md = split.report_md(state, res["people"], res["rows_before"], research, obits, res["holder"],
                         split.on_record_now(after) if not dry else set(), notes=notes,
                         profile=profile_file.read_text(encoding="utf-8") if profile_file.exists() else "",
                         extra_status=[status_line])
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
            f"{status_line}\n{PDF_MARKER} ({date.today().isoformat()})\n"
            f"Full research (obituary, family, who to call) is in this record's Documents tab: "
            f"{os.path.basename(pdf)}")
    finally:
        _api.set_dry_run(False)
    state.update(finished=not dry, primary=a.primary, basis=research.get("basis"), owner_status=status_line,
                 tiers=res.get("tiers") or state.get("tiers"))
    _save(run, state)
    print(("DRY RUN - nothing written, nothing billed. " if dry else "") + json.dumps(res))
    print(f"PDF: {pdf}" + ("" if dry else f" (uploaded: {doc.get('filename') or doc})"))
    return 0 if res.get("ok", True) else 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("batch", help="FREE: ONE SmartSkip order for every record, obituary checks + count")
    b.add_argument("--list", required=True, help="JSON list of {street, city, state, search_person, "
                   "search_at, spouse, known, restart}")
    s = sub.add_parser("start", help="FREE: batch of one, only with --only-one")
    s.add_argument("--only-one", action="store_true", help="there truly is only one record to deep prospect")
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
    c = sub.add_parser("charge", help="BILLS the SmartSkip balance once for the order, never the card")
    c.add_argument("--order", help="output/deep_prospect/_orders/<id>")
    c.add_argument("--run", help="a record's run folder: charges that record's order")
    c.add_argument("--wait", type=int, default=1200)
    f = sub.add_parser("finish", help="write phones/board/PDF (dry run until --commit)")
    f.add_argument("--run", required=True)
    f.add_argument("--primary", action="append",
                   help="who to call first, from the SmartSkip results; repeat. May be left out only "
                        "when dp_research.json has call_first_outside")
    f.add_argument("--research", help=f"defaults to {split.RESEARCH_FILE} in the run folder")
    f.add_argument("--relationship", action="append")
    f.add_argument("--basis", default="", help="not used: the basis lives in dp_research.json")
    f.add_argument("--owner-status", required=True, choices=dps.OWNER_STATUSES,
                   help="what OUR research verifies about the owner, printed at the top of every "
                        "post and the PDF. dead needs an obituary confirmed against known family or "
                        "a filing; SmartSkip's deceased flag never counts (2026-10-27)")
    f.add_argument("--status-proof", default="",
                   help="the source that verifies it (required for dead/alive)")
    f.add_argument("--confidence", choices=dps.CONFIDENCE_LEVELS,
                   help="how sure the research is that the owner is dead/alive (needed for those)")
    f.add_argument("--mixup-call",
                   help="Jeff's decision when charge said SmartSkip likely merged two people")
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
    return {"batch": cmd_batch, "start": cmd_start, "charge": cmd_charge, "finish": cmd_finish}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
