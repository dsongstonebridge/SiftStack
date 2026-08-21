# Phone & Property Tag Taxonomy

The canonical, high-level vocabulary. **Write only these.** Never invent a spelling, never put a person's name on a tag. (The account's phone-tag namespace was cleaned from 245 drifted tags down to this set in July 2026 — don't reintroduce drift.)

## Phone tags (on individual numbers)

A phone's tags = **source(s) + dial tier + relationship/role**. Multiple source tags and multiple relationship tags can coexist on one number.

### Source (who found the number) — one or more
| Tag | Meaning |
|---|---|
| `SmartSkip` | Found by SmartSkip |
| `DirectSkip` | Found by DirectSkip |
| `DataSift` | Pre-existing bulk number already on the record (DataSift-native), no other source |
| `SkipGenie`, `TPS`, `CBC`, `Forewarn`, `Dataflik`, `Kind`, `fastbackgroundcheck` | Other/legacy skip providers — preserved if already present |

A number found by two sources carries **both** tags (e.g., `SmartSkip` + `DirectSkip`). There is **no** "BOTH" tag.

### Dial tier (TrestleIQ activity score) — exactly one
| Tag | Score |
|---|---|
| `Dial First` | 81–100 |
| `Dial Second` | 61–80 |
| `Dial Third` | 41–60 |
| `Dial Fourth` | 21–40 |
| `Drop` | 0–20 (dead/disconnected) |

### Relationship (to the person skip-traced) — one or more
`Son`, `Daughter`, `Mother`, `Father`, `Brother`, `Sister`, `Husband`, `Wife`, `Spouse`, `Sibling`, `Parent`, `Child`, `In-Law`, `Cousin`, `Nephew`, `Niece`, `Grandchild`, `Grandson`, `Granddaughter`, `Aunt`, `Uncle`, `Relative`, `PR`, `Deceased`

- Gender-specific tags (Son/Daughter, Brother/Sister, Mother/Father, Husband/Wife) are inferred from the first name; **ambiguous name → fall back to the neutral tag** (Child/Sibling/Parent/Spouse).
- `Relative` = relationship unknown (SmartSkip "Other Relative"/"Unknown", or a DirectSkip-only person).
- `PR` = the number matches the Personal Representative on a probate record.
- `Deceased` = the dead owner's own number.
- Owner's own numbers get **no relationship tag** (source + tier only).

### Roles (from legal/eviction docs) — optional
`Owner`, `Landlord`, `Tenant`, `Attorney`, `Trustee`

## Property tags (on the record)

| Tag | When |
|---|---|
| `REISift Skipped` (+ `REISift Skipped MM/YY`) | DataSift native skip (set by DataSift) |
| `SmartSkip Skipped` | after the SmartSkip pass |
| `DirectSkip Skipped` | after the DirectSkip pass |
| `TrestleIQ Scored` | **only** when numbers were actually validated (never on a zero-result record) |
