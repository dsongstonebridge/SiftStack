# Message Board Post Format

One combined post per record. This is where the human context lives — **names go here, not on phone tags.** The last-4 method lets a caller see which number belongs to whom.

## Rules
- **One post**, header `SmartSkip + DirectSkip — MM/DD/YYYY`.
- Group relatives by relationship **relative to the person skip-traced**, in this order: Sons, Daughters, Children, Parents, Siblings, Spouse, In-Laws, Other Relatives. Header reads "Sons of [owner]", "In-Laws of [owner]", etc. "Other Relatives" has no "of" suffix.
- **Other Relatives** = SmartSkip "Unknown"/"Other Relative" types **plus** anyone DirectSkip found that SmartSkip didn't.
- Each person: `NAME — <last-4s>` then their mailing address on the next line.
  - **No phone types** in the post (type is on the phone record).
  - **No tag labels** in the post.
  - DirectSkip-only people have **no mailing address** (DirectSkip doesn't return one) — just omit the address line, no annotation.
  - Relatives with **no phone** are still listed, shown as `— no phones`.
- **Number display:** last-4 by default. If two of a person's numbers share the same last-4, show last-5; if still colliding (numbers differ only by area code), keep them as last-4 side by side. **Never** show the full number or the area code. Exact-duplicate numbers are collapsed.
- **Zero-result record:** single line — `SmartSkip + DirectSkip attempted MM/DD/YYYY — no numbers returned.`

## Example
```
SmartSkip + DirectSkip — 07/15/2026

STEVE JAY: 7616, 0568, 9799, 0735

Parents of STEVE JAY:
  EMILY JAY — 8558, 6561, 8092, 9799
  806 E LAKE DR, SHALIMAR, FL
  JAMES JAY — 6561, 8092, 8545, 4319, 0735
  806 E LAKE DR, SHALIMAR, FL

In-Laws of STEVE JAY:
  WILLIAM CARSON — 6847, 9217, 8239
  80 BOBWHITE TRL, CANTON, GA

Other Relatives:
  BRYANT JAY — 7616, 0735, 0568, 9799
```

## Probate note
If the record owner is the **Personal Representative** (not the deceased), the grouping reads "Sons of [PR]" — which is what SmartSkip actually knows. Do **not** relabel as heirs of the deceased. The board should also carry the deceased's name (from the data-pull note) so a human can work out the co-heirs (the PR's siblings/cousins are often the deceased's other heirs). The agent flags, it does not assert.
