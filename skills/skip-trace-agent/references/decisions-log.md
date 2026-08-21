# Decisions Log — every rule locked during the build

The "why" behind the agent's behavior. When in doubt, this file wins.

## Parsing (SmartSkip)
- **Support both export formats** (vertical row-per-person, and wide row-per-property). Auto-detect: vertical has an `Input Name` column; wide has `RELATIVE 1: First Name` columns. Both normalize to one structure — everything downstream is format-blind. Proven identical on a 100-property test (phone-number sets matched 100/100).
- **Keep Subject + Relatives; drop Associates** (neighbors/coworkers/tenants). Associates are also kept as a **negative filter** (see merge).
- **Drop people over age 100** — bad data, almost certainly deceased/wrong.
- **Relationship → canonical tag, relative to the subject**, with gender from first name: Child→Son/Daughter, Parent→Mother/Father, Sibling→Brother/Sister, Spouse→Husband/Wife; In-law→In-Law; Other/Unknown→Relative. **Ambiguous name → neutral tag** (don't guess Son vs Daughter wrong on a live record).

## Merge (SmartSkip + DirectSkip)
- **Cross-source overlap:** a number found by both sources gets both source tags. This is the high-confidence signal (two independent sources = likely live).
- **Collapse a source's own repeated echoes of one person** (DirectSkip returns the same person across multiple contact blocks) — merge their numbers. This is NOT the same as same-name Sr/Jr.
- **Do NOT dedupe same-name relatives across the record** — could be Sr/Jr, or a skip-trace returning multiple people with one name where only one is right. Keep separate; differing addresses help tell them apart. (Tyler's explicit call.)
- **Associate negative-filter:** a DirectSkip-only person whose name matches a dropped SmartSkip associate is excluded — stops a known neighbor from re-entering via DirectSkip.
- **DirectSkip-only people** (no SmartSkip match, not an associate) → "Other Relatives" on the board, tagged `Relative`.

## Numbers & tags
- **Never a name as a phone tag.** Names on the message board only.
- **No "BOTH" tag** — both real source tags side by side.
- **Shared identical number across people → tag that one phone with all applicable relationships** ("tag it as both").
- **Exact-duplicate numbers for one person → collapse** (tag once).
- **Owner's own numbers:** source + tier only; no relationship, no name.
- **Pre-existing untagged bulk number → tag `DataSift`** and re-score it (so every number on the record gets a current tier). Numbers already carrying a source tag (e.g., `Dataflik`) keep it.
- **Phone type (MOBILE/LANDLINE) comes from TrestleIQ** `line_type` (Mobile→MOBILE; Landline/FixedVOIP/NonFixedVOIP/TollFree→LANDLINE; else keep existing / UNKNOWN). Type IS set on the phone record (just not shown in the post).

## TrestleIQ
- **Validate the full union** (existing + SmartSkip + DirectSkip), deduped globally (one call per unique number).
- **`TrestleIQ Scored` property tag only when numbers were actually scored.** Zero-result records don't get it.

## Zero-result records
- Still tag `SmartSkip Skipped` + `DirectSkip Skipped` (proves it was attempted, so it drops out of the "ready to skip" filter).
- Post a one-line "attempted — no numbers returned" note. No phones, no `TrestleIQ Scored`.

## Probate / Personal Representative
- On probate deals the record owner is often the **PR** (added from the filing), not the deceased. SmartSkip's relationships are relative to the *subject skip-traced*.
- Label what's known ("Sons of [PR]"); mark the PR's own numbers `PR`. **Never assert "heir of the deceased."**
- The PR's siblings/cousins are frequently the deceased's other heirs — surface this for a human to verify (the deceased's name is on the board from the data pull). Flag, don't decide.

## Live-CRM write discipline
- **Dry-run → pilot one → review → batch.** Idempotent (safe to re-run), logged (`run/writeback_log.jsonl`), resumable by `--start/--count/--record`.
- **Resilient upsert:** REISift 400s on a number it deems invalid (bogus area code). Catch the 400, drop exactly the flagged phone index(es), retry — so one bad skip number can't silently block a whole record. (This is why a Destin record once sat untagged in the filter — now handled.)
- **Message board is append-only** (no edit endpoint). The writeback skips posting if today's combined post already exists.

## CRM write endpoints (verified live)
| Action | Endpoint |
|---|---|
| Add/tag phones | `POST /api/internal/owner/{owner_uuid}/upsert-phones/` (keyed by number; re-send full tag set — it replaces the array) |
| Append message | `POST /api/internal/property/{property_uuid}/message/` |
| Add property tags | `POST /api/internal/property/{property_uuid}/add-tags/` |
| Resolve by address | `POST /api/internal/property/` + header `x-http-method-override: GET`, `search: address_prefix:...`; require exact street match |
| Phone-tag namespace | `GET/PATCH/DELETE /api/internal/phone/tag/{uuid}/` (a tag only deletes once it has zero phones) |

## Mode B (CRM filter) query
The "02. Ready to Skip (DirectSkip)" preset (folder `1.FTM`) filters on: `all_tags` = [REISift Skipped, SmartSkip Skipped], `any_tags` = [week tags], `must_not.any_tags` = [DirectSkip Skipped], plus `any_lists`. Apply the preset's saved `filters.must` block via `POST /api/internal/property/` with `x-http-method-override: GET`, paginate, and build the DirectSkip input from each record's owner name + mailing + property address.
