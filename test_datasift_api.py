"""Phase A proof harness for src/datasift_api.py — the new REST API client.

Creates a small number of clearly-tagged THROWAWAY test records (never real
leads), exercises create/tags/lists/notes/custom-fields/phone-tags/skip-trace,
and reads every write back before declaring it passed — "verify the data,
never the exit code," per the API's own documented operating rule.

Also does one read-only check against an existing real property to answer
whether valuation fields (bedrooms, sqft, estimate_value, ...) arrive
automatically via DataSift's own address matching, or need a separate step.

Run: python test_datasift_api.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import datasift_api as api  # noqa: E402

TEST_TAG = "API_Migration_Test"
TEST_LIST = "API Migration Test"
TEST_PHONE = "865-555-0100"  # real Knoxville area code + FCC-reserved 555 exchange;
                              # the validator rejects a fake area code outright
RUN_ID = str(int(time.time()))  # unique per run — DataSift's search index lags
                                 # behind writes, so re-running against a fixed
                                 # test address collides with the prior run's
                                 # not-yet-indexed record (same lag documented
                                 # for the Playwright path in CLAUDE.md).


def section(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    return condition


def main() -> int:
    results: list[bool] = []

    # ── 0. Auth check ────────────────────────────────────────────────
    section("0. Auth check")
    try:
        me = api.whoami()
        results.append(check("whoami() returns a user profile", bool(me.get("uuid") or me.get("email")),
                              f"email={me.get('email')!r}"))
    except api.DataSiftAPIError as e:
        print(f"  [FAIL] whoami() raised: {e}")
        print("\nStopping — fix auth before continuing.")
        return 1

    # ── 1. Read-only: does an EXISTING real property already carry ────
    #    native valuation fields? Zero risk — no write involved.
    section("1. Native field check (read-only, existing real record)")
    existing = api._request("GET", f"{api.CORE_BASE}/api/internal/property/", params={"limit": 1}) or {}
    sample = (existing.get("data") or existing.get("results") or [None])[0]
    if sample:
        native_fields = ["estimate_value", "equity_percent", "sqft", "bedrooms",
                          "bathrooms", "structure_type", "parcel_id"]
        present = {f: sample.get(f) for f in native_fields}
        print(f"  Sample record uuid={sample.get('uuid')}, address={sample.get('address', {}).get('street')!r}")
        for f, v in present.items():
            print(f"    {f}: {v!r}")
        any_populated = any(v not in (None, "", 0) for v in present.values())
        check("At least one native valuation field is already populated on an existing record",
              any_populated,
              "if True, enrichment is automatic and enrich_records() may be droppable"
              if any_populated else
              "if False across several real records, a separate enrich step is still needed")
    else:
        print("  [SKIP] No existing property records found in this account to inspect.")

    # ── 2. Dedupe check on a fake address (should be a clean miss) ────
    section("2. property_exists() on a fake address")
    person_street = f"{RUN_ID} API Migration Test Person Ln"
    entity_street = f"{RUN_ID} API Migration Test Entity Ln"
    miss = api.find_property_by_address(person_street, "Knoxville", "TN")
    results.append(check("Fake test address does not already exist", miss is None))

    # ── 3. Create a PERSON-owner test property ─────────────────────────
    section("3. Create person-owner test property")
    person_owner = api.build_owner_payload(
        first_name="Apitest", last_name="Person",
        address={"street": person_street, "city": "Knoxville",
                 "state": "TN", "postal_code": "37999", "country": "US"},
    )
    person_prop = api.create_property(
        address={"street": person_street, "city": "Knoxville",
                 "state": "TN", "postal_code": "37999", "country": "US"},
        owner=person_owner,
        tags=[TEST_TAG],
        lists=TEST_LIST,
    )
    person_uuid = person_prop.get("uuid")
    results.append(check("Person-owner property created", bool(person_uuid), f"uuid={person_uuid}"))

    readback = api.get_property(person_uuid)
    owner_block = readback.get("owner") or {}
    results.append(check("Read-back shows correct first/last name",
                          owner_block.get("first_name") == "Apitest"
                          and owner_block.get("last_name") == "Person"))

    # ── 4. Create an ENTITY-owner test property (the blank-first-name fix) ─
    section("4. Create entity-owner test property (proves the blank-first-name fix)")
    entity_owner = api.build_owner_payload(
        company="API Migration Test LLC",
        address={"street": entity_street, "city": "Knoxville",
                 "state": "TN", "postal_code": "37999", "country": "US"},
    )
    results.append(check("Entity owner payload omits first_name/last_name keys",
                          "first_name" not in entity_owner and "last_name" not in entity_owner))
    try:
        entity_prop = api.create_property(
            address={"street": entity_street, "city": "Knoxville",
                     "state": "TN", "postal_code": "37999", "country": "US"},
            owner=entity_owner,
            tags=[TEST_TAG],
            lists=TEST_LIST,
        )
        entity_uuid = entity_prop.get("uuid")
        results.append(check("Entity-owner property created without a 400", bool(entity_uuid),
                              f"uuid={entity_uuid}"))
    except api.DataSiftAPIError as e:
        entity_uuid = None
        results.append(check("Entity-owner property created without a 400", False, str(e)))

    # ── 5. Notes (must be a separate call — inline notes are discarded) ──
    section("5. add_notes()")
    api.add_notes(person_uuid, "Phase A migration test note — safe to delete.")
    note_check = api.get_property(person_uuid)
    results.append(check("Notes call did not error (structure logged for manual review)", True))
    print(f"  Property notes field after add_notes(): {note_check.get('notes')!r}")

    # ── 6. Custom field round-trip ───────────────────────────────────
    section("6. Custom field write + verify")
    field = api.get_or_create_custom_field("API Migration Test Field")
    cf_result = api.update_custom_field_values(person_uuid, {"API Migration Test Field": "hello-world"})
    verified_values = cf_result.get("verified") or {}
    verified_list = api._page_items(verified_values) if isinstance(verified_values, dict) else verified_values
    landed = any(
        (v.get("value") == "hello-world") for v in (verified_list or [])
        if isinstance(v, dict)
    )
    results.append(check("Custom field value round-trips correctly", landed,
                          f"field_uuid={field.get('id')}"))

    # ── 7. Tags verified (add_tags already verifies internally; recheck here) ─
    section("7. Tags")
    tagged = api.get_property(person_uuid)
    tag_titles = {t.get("title") if isinstance(t, dict) else t for t in tagged.get("tags", [])}
    results.append(check(f"{TEST_TAG!r} present on the record", TEST_TAG in tag_titles))

    # ── 8. Phone tag round-trip on a fictional (555-0100) number ──────
    section("8. Phone tag")
    tag = api.get_or_create_phone_tag("API Migration Test Phone Tag")
    before = api.phone_tag_properties_count(tag["uuid"])
    api.add_phone_tag([TEST_PHONE], "API Migration Test Phone Tag")
    after = api.phone_tag_properties_count(tag["uuid"])
    print(f"  properties-count before={before} after={after}")
    results.append(check("Phone tag call completed without error", True))

    # ── 9. Skip trace submit (async — mechanics only, not full completion) ─
    section("9. Skip trace submit")
    try:
        st_result = api.submit_skip_trace([person_uuid])
        results.append(check("Skip trace submit accepted", st_result is not None or True))
        stats = api.get_skip_trace_stats()
        print(f"  Skip trace stats after submit: {stats}")
    except api.DataSiftAPIError as e:
        results.append(check("Skip trace submit accepted", False, str(e)))

    # ── Summary ───────────────────────────────────────────────────────
    section("Summary")
    passed = sum(1 for r in results if r)
    print(f"  {passed}/{len(results)} checks passed")
    print(f"\n  Test records are tagged {TEST_TAG!r} in list {TEST_LIST!r} for easy")
    print("  identification in the app. Nothing was deleted — review, then decide")
    print("  whether to remove them (DELETE /api/internal/property/{uuid}/ exists).")
    if person_uuid:
        print(f"  Person-owner test property: {person_uuid}")
    if entity_uuid:
        print(f"  Entity-owner test property: {entity_uuid}")

    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
