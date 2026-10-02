"""Re-link CRM records that have no SiftMap parcel (no "Open in SiftMap" link).

For every record with an empty `dataflik_id`:
  1. ask SiftMap's address search for its wording of the street
     (siftmap_address.siftmap_street; refuses anything ambiguous),
  2. if the CRM wording differs, PATCH the property street to SiftMap's,
  3. run a PROPERTY-ONLY enrichment (owner untouched),
  4. read the record back and report whether it linked.

Proven on 2026-10-02 on 10007 S 87th Ave E (was "87Th East Ave"): linked on
the first poll; owner name, mailing address and all 10 phones unchanged. The
PATCH clears latitude/longitude/county; enrichment restores county.

Enrichment is not billed. Dry run by default; --commit writes.

    python src/repair_siftmap_links.py            # report only
    python src/repair_siftmap_links.py --commit
    python src/repair_siftmap_links.py --undo output/siftmap_repair_<date>_before.json

UNDO: git revert does not touch the CRM. Every --commit run first writes the
streets it is about to change to output/siftmap_repair_<date>_before.json;
--undo puts those streets back (it does not remove the SiftMap link data
enrichment added, which is harmless).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

import datasift_api as d
from siftmap_address import siftmap_street
from siftmap_standalone import SiftMapClient

logger = logging.getLogger(__name__)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--commit", action="store_true", help="actually patch and enrich")
    ap.add_argument("--undo", metavar="BEFORE_JSON", help="restore streets from a backup file")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.undo:
        return undo(Path(args.undo))

    items = d.get_all(f"{d.CORE_BASE}/api/internal/property/", params={"ordering": "-created"})
    unlinked = [p for p in items if not p.get("dataflik_id")]
    logger.info("%d records, %d without a SiftMap link", len(items), len(unlinked))

    client = SiftMapClient()
    plan = []          # (uuid, current street, siftmap street or None)
    for p in unlinked:
        a = p.get("address") or {}
        new = siftmap_street(a.get("street", ""), a.get("city", ""), a.get("zip5", ""), client=client)
        plan.append((p["uuid"], a, new))
        logger.info("  %-32s %-14s -> %s", a.get("street"), a.get("city"),
                    new if new else "NO CONFIDENT MATCH (left alone)")

    todo = [x for x in plan if x[2]]
    if not args.commit:
        logger.info("DRY RUN: %d would be repaired. Re-run with --commit.", len(todo))
        return 0

    backup = Path("output") / f"siftmap_repair_{date.today().isoformat()}_before.json"
    changing = [{"uuid": u, "street": a.get("street"), "city": a.get("city"),
                 "state": a.get("state"), "postal_code": a.get("postal_code")}
                for u, a, new in todo if new != a.get("street")]
    if changing:
        backup.parent.mkdir(exist_ok=True)
        old = json.loads(backup.read_text()) if backup.exists() else []
        seen = {r["uuid"] for r in old}       # keep the EARLIEST street per record
        backup.write_text(json.dumps(old + [r for r in changing if r["uuid"] not in seen], indent=1))
        logger.info("Backup of original streets: %s (undo with --undo %s)", backup, backup)

    for uuid, a, new in todo:
        if new != a.get("street"):
            d._request("PATCH", f"{d.CORE_BASE}/api/internal/property/{uuid}/",
                       json_body={"address": {"street": new, "city": a.get("city"),
                                              "state": a.get("state"),
                                              "postal_code": a.get("postal_code")}})
    uuids = [u for u, _, _ in todo]
    for i in range(0, len(uuids), 25):
        d.enrich_properties(uuids[i:i + 25], enrich_property=True,
                            enrich_owner=False, replace_owner=False)

    pending = set(uuids)
    for _ in range(12):
        time.sleep(15)
        for u in list(pending):
            if d.get_property(u).get("dataflik_id"):
                pending.discard(u)
        if not pending:
            break
    logger.info("Linked %d of %d.", len(uuids) - len(pending), len(uuids))
    for u, a, new in todo:
        if u in pending:
            logger.warning("  still unlinked: %s (%s)", new, u)
    return 0 if not pending else 1


def undo(backup: Path) -> int:
    """PATCH each record's street back to the value saved before the repair."""
    rows = json.loads(backup.read_text())
    bad = 0
    for r in rows:
        d._request("PATCH", f"{d.CORE_BASE}/api/internal/property/{r['uuid']}/",
                   json_body={"address": {k: r[k] for k in ("street", "city", "state", "postal_code")}})
        got = d.get_property(r["uuid"])["address"]["street"]
        ok = got.lower() == r["street"].lower()
        bad += not ok
        logger.info("  %s %s", "restored" if ok else "MISMATCH, reads", got)
    logger.info("Restored %d of %d.", len(rows) - bad, len(rows))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
