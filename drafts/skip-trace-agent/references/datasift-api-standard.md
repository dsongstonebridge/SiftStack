# DataSift / REISift API Reference — Standard (Api-Key)

**Owner:** Tyler Austin
**Audience:** Every agent and human at FCRE that needs to read from or write to DataSift programmatically using the **official public API key**
**Effective:** July 2026

> **This is the "Standard" (Api-Key) companion to `datasift-api.md`.**
> There are two ways to talk to Sift, and FCRE has documented both:
> - **`datasift-api.md` — JWT / internal-session variant.** Reverse-engineered from the beta UI's browser traffic. Uses a Bearer JWT copied from DevTools that expires every ~48h. Covers a lot of undocumented `/api/internal/` behavior and hard-won gotchas.
> - **`datasift-api-standard.md` (this doc) — Api-Key / public variant.** Uses an official REISift Open API key that does not expire on a 48h clock. Endpoints and shapes here are confirmed against the official OpenAPI spec at `https://developers.datasift.ai/` (DataSift Core + SiftMap), pulled July 2026.
>
> Prefer this Api-Key path for anything that can run on it — it's the supported surface and kills the 48h token dance. Fall back to the JWT doc only for the handful of endpoints the public API doesn't expose (see **§ Internal-only endpoints** at the bottom).

---

## Why This Doc Exists

DataSift (product name REISift) is the CRM of record for every lead FCRE touches. Call history, property data, owner info, phones, SIFTline pipeline position, custom fields — it all lives in Sift.

The official public API is documented at:
- **DataSift Core:** `https://developers.datasift.ai/datasift/` (spec: `/datasift/spec.yaml` — 573 documented operations)
- **SiftMap:** `https://developers.datasift.ai/sift-map/` (spec: `/sift-map/spec.yaml` — 12 documented operations)

The public spec is **auto-generated from their Django/DRF routes**, not a hand-curated marketing subset — so it's actually quite complete, including a ton of integration-specific routes (smrtphone, kixie, aircall, calltools, dataflik, twilio, plivo, etc.). What's missing from it is missing on purpose: routes that an Api-Key isn't permitted to call (staff/admin actions) or genuinely internal automation. Details in **§ Internal-only endpoints**.

---

## Authentication — Api-Key (the whole point of this doc)

Auth is a static **REISift Open API key**, sent on every request in the `Authorization` header with the `Api-Key` scheme:

```
Authorization: Api-Key YOUR_OPEN_API_KEY
```

Example:
```
curl https://apiv2.reisift.io/api/internal/property/ \
  -H "Authorization: Api-Key YOUR_OPEN_API_KEY"
```

### How to get a key
- Generate and manage keys in the REISift app under **account → integration settings**.
- **Plan gate:** Open API keys are available on plans **above Professional**. Confirm the FCRE account tier before relying on this.
- Each key **acts on behalf of the user it was issued for and inherits that user's permissions.** So issue the FCRE agent key from a dedicated agent user account (as we already do for authorship clarity), not from Tyler's personal login.

### Key hygiene
- Treat it like a password. Keep it out of source control; store it in `Shared/clients/config/` the same way the JWT lives today (e.g., a `reisift_apikey.json` next to `reisift_auth.json`).
- Rotate/revoke from the same integration screen if it's ever exposed.

### What changes vs. the JWT doc
| | JWT variant (`datasift-api.md`) | Api-Key variant (this doc) |
|---|---|---|
| Header | `authorization: Bearer <jwt>` | `Authorization: Api-Key <key>` |
| Lifetime | ~48h, manual DevTools refresh | Static until rotated/revoked |
| Origin/referer/ui-version headers | Required (mimics the browser) | **Not required** — it's a real API client |
| `x-http-method-override: GET` | Used for property search | **Not documented** — public API exposes a real `GET /api/internal/property/` (see §CRM 1) |
| Impersonation, sequences | Available (staff JWT) | **Not exposed** to Api-Key |

Everything else — base URLs, most paths, request/response shapes — is the same. The two docs describe the same server; only the front door differs.

---

## Two Systems, Two Base URLs (unchanged)

| System | Base URL | What lives here |
|---|---|---|
| **CRM** | `https://apiv2.reisift.io` | Your account's records — properties, SIFTline boards, message board, custom fields, activity log, tasks, phones, deals |
| **SiftMap** | `https://map.reisift.io` | Nationwide property dataset — any address, with sale/mortgage/MLS history, owner portfolios, AI scores |

Same Api-Key works on both. Don't mix endpoints between them.

---

## CRM — apiv2.reisift.io

The public spec exposes ~560 CRM operations. Below: first a **complete family index** so you know the full surface, then **detailed narrative** on the endpoints FCRE actually uses (carried over from the JWT doc, re-confirmed against the public spec), then the **newly-documented endpoints** that close open gaps from the JWT doc.

### Complete family index (public CRM surface)

| Family | Base path | Ops |
|---|---|---|
| **Properties** | `/api/internal/property/` and `/api/internal/properties/property/` | list (GET), create (POST), detail (GET/PATCH/DELETE), next/prev, logs, deal, custom-field, document, image |
| **Owners** | `/api/internal/owner/` | CRUD, logs, next/prev, message (full CRUD + pin), sms, offer (read), upsert-phones, remove-phones, upsert-emails, remove-emails, add-phone-tag, add-phone-status, contact, do-not-mail-ever, add-property |
| **Phones** | `/api/internal/phone/` and `/phone/` | phone CRUD, phone **type** list, phone **tag** CRUD (+ properties-count) |
| **Tags** | `/api/internal/tag/`, `/api/internal/tag-folder/` | CRUD + folders |
| **Lists** | `/api/internal/list/`, `/api/internal/list-folder/` | CRUD + folders + properties-count |
| **Statuses** | `/api/internal/global-status/`, `/api/internal/properties/global-status/` | list/detail/update |
| **Custom Fields** | `/api/internal/custom-fields/`, `/custom-fields/group/`, `/custom-fields/{field_id}/option/` | full CRUD for fields, groups, and select options; per-property values via `/property/{property_uuid}/custom-field/` |
| **SIFTline** | `/api/internal/siftline/board/` | board CRUD; columns via `/board/column/{column_uuid}/card/` (card CRUD, bulk, next/prev, timeline); siftline tasks |
| **Tasks** | `/api/internal/task/`, `/task-group/`, `/task-preset/` | task CRUD, create-by-preset, complete, groups, presets, recurrence |
| **Deals** | `/api/internal/deal/` | full CRUD + `/property/{uuid}/deal/` |
| **Filter Presets** | `/api/internal/filter-preset/`, `/filter-preset-folder/` | CRUD + folders + scheduled-export |
| **Contacts** | `/api/internal/contacts/contact/`, `/contacts/tag/`, `/contacts/tag-folder/` | CRUD, next/prev, status, counts |
| **Activity** | `/api/internal/property/{uuid}/logs/`, `/owner/{uuid}/logs/`, `/activity/skiptrace/*`, `/activity/dataflik/*` | event logs + skiptrace stats |
| **Account / User** | `/api/internal/user/`, `/api/internal/account/{uuid}/`, `/api/internal/addon/` | account, user list, add-ons/billing |
| **Documents / Images** | `/api/internal/property/{property_uuid}/document/`, `/image/` | upload (presigned-url), list, detail, delete |
| **Integrations** | `/smrtphone/`, `/smrtdialer/`, `/kixie/`, `/aircall/`, `/calltools/`, `/dataflik/`, `/twilio/`, `/plivo/`, `/xencall/`, `/reirail/`, `/launch-control/`, `/smarter-contact/`, `/email-integration/`, `/calendar-integration/` | dialer/SMS/skip provider hooks |
| **Dashboards** | `/api/internal/dashboard/`, `/dashboard-report/{uuid}/` | reporting |

> When you need an endpoint not detailed below, pull the exact request/response schema from `https://developers.datasift.ai/datasift/spec.yaml` (it's a standard OpenAPI 3.0.2 file) rather than re-deriving from DevTools.

---

### 1. Property Search / List

**Public endpoint:** `GET /api/internal/property/` (real GET — no method-override needed)
**Also:** `POST /api/internal/property/` (create)

The JWT doc uses `POST /api/internal/property/` + header `x-http-method-override: GET` to run the rich filter DSL (`query.must` with `all_tags` / `any_tags` / `must_not` / `search: address_prefix:...`). The public API documents a real `GET /api/internal/property/` for listing, and the method-override trick is **not** in the public spec.

> **✅ CONFIRMED LIVE (2026-07-21):** BOTH paths work with an Api-Key and return identical results:
> - `POST /api/internal/property/` + `x-http-method-override: GET` with a `{"query":{"must":{...}}}` body (the internal trick) → 200. The override is honored on the Api-Key path.
> - `GET /api/internal/property/?query=<url-encoded {"must":{...}}>` (documented method) → 200, same result set.
>
> Plain `POST /api/internal/property/` with no override is **create** (returns 400 "address/owner required"), not search. The `must_not`-nests-inside-`must` gotcha, status casing, and duplicate-resolution rules from `datasift-api.md §CRM 1` still apply.

Everything about the filter DSL grammar, the `must_not`-nests-inside-`must` gotcha, status casing, and duplicate-resolution rules is unchanged — see `datasift-api.md §CRM 1` (still law).

### 2. Property Detail

**Endpoint:** `GET /api/internal/property/{uuid}/` — full record (also `PATCH` to update, `DELETE` to remove)

Same rich payload documented in the JWT doc (owner object, `phones[]` with `type`/`status`/`is_connected`/`tags[]`, distress fields, AI scores). Reading existing phones for a record goes through here.

### 3. Message Board — **owner-scoped in the public API**

**Read:** `GET /api/internal/owner/{owner_uuid}/message/?ordering=-created`
**Write:** `POST /api/internal/owner/{owner_uuid}/message/` — body `{ "message": "<text>" }`
**Edit:** `PATCH /api/internal/owner/{owner_uuid}/message/{uuid}/`
**Delete:** `DELETE /api/internal/owner/{owner_uuid}/message/{uuid}/`
**Pin / unpin:** `POST .../message/{uuid}/pin/` and `.../unpin/`

> **✅ CONFIRMED LIVE (2026-07-21) — property-scoped message works on the Api-Key too.** Although the public OpenAPI spec only lists the **owner-scoped** message endpoints, the **property-scoped** endpoints the JWT doc/skip-trace agent use also work with an Api-Key:
> - `POST /api/internal/property/{uuid}/message/` → **201** (create)
> - `DELETE /api/internal/property/{uuid}/message/{uuid}/` → **204** (delete — the JWT doc said this wasn't possible; it is, on the Api-Key path)
> - Owner-scoped `POST`/`DELETE /owner/{owner_uuid}/message/{uuid}/` also work (201/204).
>
> **So the skip-trace agent needs NO message-endpoint change** — keep posting property-scoped.
>
> **⚠️ Two separate boards.** The property board and the owner board are distinct collections. In the test record, the pre-existing human note lived on the **owner** board and the property board was empty. Confirm in the app which board your team actually reads before assuming property-scoped posts are team-visible. `owner_uuid` is in the property detail payload (`owner.uuid`) if you ever want to post to the owner board instead.

Author is set from the key's user. Keep the "identify machine-written posts" convention (prefix + link).

### 4. Property Tags — Write

**Add:** `POST /api/internal/property/{uuid}/add-tags/` — body `{ "tags": ["DirectSkip Skipped"] }`
**Remove:** `POST /api/internal/property/{uuid}/remove-tags/` — mirror shape
**Directory:** `GET /api/internal/tag/` (list all account tags), `/tag-folder/` for folders

Server merges on add (existing tags preserved), returns the full property object. This is the confirmed-correct write (the old `PATCH /property/{uuid}/ {tags_add:[...]}` silently no-ops — see JWT doc July addendum).

### 5. Property Status — Write

**Endpoint:** `POST /api/internal/property/{uuid}/status/` — body `{"status": "<value>"}` (or `null` to clear)

**Status directory difference:** the JWT doc reads the status list from `/api/internal/properties/status/`. The public API exposes `/api/internal/global-status/` and `/api/internal/properties/global-status/` instead. Use those to resolve the canonical active status list. Casing rules (system defaults snake_case, user-created verbatim — never normalize) still apply.

### 6. Filter Presets

Full CRUD, public:
- `GET/POST /api/internal/filter-preset/`, `GET/PUT/PATCH/DELETE /filter-preset/{uuid}/`
- Folders: `/api/internal/filter-preset-folder/...` (+ `/{folder_uuid}/filter-preset/`)
- **New in public:** `/api/internal/filter-preset/{filter_preset_uuid}/scheduled-export/` (CRUD) — scheduled exports of a saved filter

Save-vs-apply field-shape asymmetry from the JWT doc (§A6) still applies. Each preset row carries `{uuid, title, folder, quick_filter, filters}`.

### 7. Phones & Phone Tags — Write

- **Upsert phones (owner-level):** `POST /api/internal/owner/{owner_uuid}/upsert-phones/` — body `{"phones":[{"number","type","status","is_connected","tags":[...]}]}`. Keyed by `number`; upserts; unsent numbers left alone. Re-send the FULL merged tag set per number or you may wipe existing tags.
- **Remove phones:** `POST /api/internal/owner/{owner_uuid}/remove-phones/`
- **Add phone tag:** `POST /api/internal/owner/{uuid}/add-phone-tag/` (also property- and phone-scoped variants exist: `addPhoneTagProperty`, `addPhoneTagPhone`)
- **Add phone status:** `POST /api/internal/owner/{uuid}/add-phone-status/`
- **Phone tag directory (CRUD):** `GET/POST /api/internal/phone/tag/`, `GET/PATCH/DELETE /phone/tag/{uuid}/`, `GET /phone/tag/{uuid}/properties-count/`
- **Phone type list:** `GET /api/internal/phone/type/`
- **Owner emails:** `upsert-emails` / `remove-emails`

> This closes the JWT doc's "phone tags write endpoint — deferred" gap. It's fully documented.

### 8. SIFTline (Boards / Columns / Cards)

- **Boards:** `GET/POST /api/internal/siftline/board/`, `GET/PUT/PATCH/DELETE /siftline/board/{uuid}/`
- **Cards (column-scoped):** `GET/POST /siftline/board/column/{column_uuid}/card/`, `GET/PUT/PATCH/DELETE .../card/{uuid}/`, plus `next/`, `prev/`, `timeline/`
- **Bulk card ops:** `POST /siftline/board/column/card-bulk/`, `PUT .../card-bulk/{uuid}/`
- **Card move:** `PATCH /siftline/board/column/{column_uuid}/card/{uuid}/` with `{"order":0,"column":"<new_column_uuid>"}` — same as JWT doc. **Card UUID ≠ property UUID** still holds.

> **Not in the public spec:** the JWT doc's convenience lookups `GET /siftline/board/{uuid}/column/` (list columns for a board) and `GET /siftline/property/{uuid}/card/` ("what boards is this property on"). See §Internal-only. To get card UUIDs on the public path, read them from the column's card list or from board-filtered property search results.

### 9. Tasks

- `GET/POST /api/internal/task/` — **includes from-scratch create** (`POST /task/`), which the JWT doc listed as an open gap. Custom title/due/assignee without a preset is supported.
- `POST /api/internal/task/create-by-preset/` — preset-based create (unchanged)
- `POST /api/internal/task/{uuid}/complete/`, `DELETE /task/{uuid}/`
- Groups/presets: `/task-group/`, `/task-group/{uuid}/task-preset/`; recurrence supported

### 10. Activity Log

`GET /api/internal/property/{uuid}/logs/` and `GET /api/internal/owner/{uuid}/logs/` — same event taxonomy, pagination, and payload shapes as the JWT doc (call-pairing, card-payload quirks, `source` = human vs. automation, etc.). All of that reference material in `datasift-api.md §CRM 17` still applies verbatim.

---

### Newly-documented endpoints (close JWT-doc "Open Gaps")

These were "not yet captured" / "implied" in `datasift-api.md`. The public spec documents them:

| Gap in JWT doc | Now documented (public) |
|---|---|
| Deal CRUD | `GET/POST /api/internal/deal/`, `GET/PUT/PATCH/DELETE /deal/{uuid}/`, `GET /property/{uuid}/deal/` |
| Offer read | `GET /api/internal/owner/{owner_uuid}/offer/` and `/offer/{uuid}/` (**read-only** in public API; owner-scoped, not property-scoped) |
| Custom field **write** | `GET/POST /api/internal/custom-fields/`, `.../{id}/` (PATCH/DELETE); groups `/custom-fields/group/...`; **select options** `/custom-fields/{field_id}/option/...` (resolves the "select returns choice UUID" join) |
| Message edit/delete | `PATCH` / `DELETE` on `/owner/{owner_uuid}/message/{uuid}/` (+ pin/unpin) |
| Task from scratch | `POST /api/internal/task/` |
| Phone tag write | `/api/internal/phone/tag/` CRUD + `add-phone-tag` actions |
| Documents / images | `/property/{property_uuid}/document/` and `/image/` (presigned-url upload, list, delete) |

---

## SiftMap — map.reisift.io (public)

Only **12 documented operations**. Same Api-Key auth (`Authorization: Api-Key ...`).

| Method | Path | Purpose |
|---|---|---|
| GET | `/filters/` | list saved filters (`offset`, `ordering`, `page_size`, `search`, `is_active`) |
| POST | `/filters/` | create saved filter (auto-add validation, snapshot cap) |
| GET | `/filters/{id}/` | get one |
| PATCH | `/filters/{id}/` | update (toggle auto-add, rename, replace spec) |
| DELETE | `/filters/{id}/` | delete |
| PATCH | `/filters/{id}/favorite/` | mark/unmark favorite |
| GET | `/filters/settings/` | account filter settings |
| PATCH | `/filters/settings/` | update settings |
| POST | `/properties/search/` | property search (address/polygon/county/zip + filters) |
| GET | `/properties/detail/{property_id}/` | full detail (sale/mortgage/MLS history, owner, distressors, AI scores) |
| POST | `/properties/add-properties/` | add properties to the CRM by list of IDs |
| POST | `/properties/add-properties-by-query/` | add by address search / polygon |

The `filter_data` create/response shape, rich-vs-thin address shapes, the world-polygon requirement, `zip_codes` stringified-JSON gotcha, comping filter fields, and the newest-first sale-history mechanics are all documented in `datasift-api.md §SiftMap` and §A8–A10 — still accurate. **New here vs. the JWT doc:** the two `add-properties*` endpoints (the "pull these into my CRM" action) and `/filters/settings/` are explicitly in the public spec.

---

## Skip-Trace Agent — Public-API Compatibility

**Question:** can `fcre-skip-trace/skip-trace-agent` complete all its skip-tracing on the public Api-Key API?

**Answer: Yes — with ZERO endpoint changes. Just swap the auth header.** Every read and write it does was smoke-tested live against an Api-Key on 2026-07-21 (record: 110 Hawkins Rd, Fort Walton Beach) and all passed. Third-party steps (DirectSkip, TrestleIQ) don't touch DataSift and are unaffected.

| Agent operation | Endpoint it uses today | Api-Key result (live) |
|---|---|---|
| Resolve record by address / run filter preset | `POST /property/` + `x-http-method-override: GET` (filter DSL) | ✅ 200 — override honored; documented `GET /property/?query=` also works |
| Read existing phones | `GET /property/{uuid}/` | ✅ 200 |
| Read filter presets | `GET /filter-preset/...` | ✅ 200 |
| Upsert phones (source/tier/relationship tags) | `POST /owner/{owner_uuid}/upsert-phones/` | ✅ 200 `{"added":[...]}` |
| Remove phones (best-30 reconcile) | `POST /owner/{owner_uuid}/remove-phones/` | ✅ 200 |
| Add property skip tags | `POST /property/{uuid}/add-tags/` | ✅ 200 |
| Remove property tags | `POST /property/{uuid}/remove-tags/` | ✅ 200 |
| Combined house-style post | `POST /property/{property_uuid}/message/` | ✅ **201 — property-scoped write works on the Api-Key** (not in the public spec, but live-confirmed) |

**Bottom line:** the entire skip-trace CRM footprint runs on the Api-Key path unchanged. The only migration step is:
1. **Swap the auth header** from `Authorization: Bearer <jwt>` to `Authorization: Api-Key <key>` (and drop the browser origin/referer/ui-version headers). No path or body changes.

The 48h token refresh in the agent's pre-flight (`Shared/clients/config/reisift_auth.json`, "refresh from DevTools if a CRM call 401s") **goes away entirely** — that's the biggest operational win. Only open item: confirm in-app that property-scoped posts land on the board your team reads (see §CRM 3, "two separate boards").

---

## Smoke Test Log — 2026-07-21 (Api-Key, live)

First live validation of an Open API key against the production FCRE account. Key user: Tyler Austin, role `sensei`, feature flags include `open_api` and `staff`. All write tests run against a self-owned test record (110 Hawkins Rd, Fort Walton Beach — owner "Tyler Austin") and **fully reversed**; record confirmed back to original state (20 tags, 5 phones, original owner note intact).

| # | Test | Endpoint | Result |
|---|---|---|---|
| 1 | Auth / whoami | `GET /api/internal/user/` | ✅ 200 |
| 2 | List properties | `GET /api/internal/property/?limit=1` | ✅ 200 (count 213,855) |
| 3 | Filter presets | `GET /api/internal/filter-preset/` | ✅ 200 (60 presets) |
| 4 | Status list | `GET /api/internal/properties/global-status/` | ✅ 200 |
| 5 | Tag list | `GET /api/internal/tag/` | ✅ 200 (1,609 tags) |
| 6 | Search — POST-as-GET override | `POST /property/` + `x-http-method-override: GET` | ✅ 200 |
| 7 | Search — documented GET | `GET /property/?query=` | ✅ 200 (identical results) |
| 8 | Plain POST (control) | `POST /property/` (no override) | 400 = create, not search (expected) |
| 9 | Message create — property | `POST /property/{uuid}/message/` | ✅ 201 |
| 10 | Message delete — property | `DELETE /property/{uuid}/message/{uuid}/` | ✅ 204 |
| 11 | Message create/delete — owner | `POST`/`DELETE /owner/{uuid}/message/` | ✅ 201 / 204 |
| 12 | Add tag | `POST /property/{uuid}/add-tags/` | ✅ 200 |
| 13 | Remove tag | `POST /property/{uuid}/remove-tags/` | ✅ 200 |
| 14 | Upsert phone | `POST /owner/{uuid}/upsert-phones/` | ✅ 200 `{"added":[...]}` |
| 15 | Remove phone | `POST /owner/{uuid}/remove-phones/` | ✅ 200 |

**Takeaways:** (1) the Api-Key reaches the same `/api/internal/` surface as the browser JWT, including endpoints not in the public spec (property-scoped messages, the method-override search). (2) Property and owner message boards are separate collections. (3) The key carries `staff` — so staff-gated routes (e.g. impersonate) may also answer to it; untested, test before relying.

---

## Internal-only endpoints (present on the JWT path, NOT in the public Api-Key spec)

Tyler's hunch — "those endpoints probably still exist, they're just hidden from the docs" — is **half right.** The routes exist on the server (the browser JWT hits them), but they're **excluded from the Api-Key OpenAPI schema**, and in at least some cases an Api-Key likely **can't** call them because the key's permissions don't grant it. The public spec is auto-generated from the DRF routes, so exclusion is deliberate (permission-gated or internal), not an oversight.

| Endpoint | Status on Api-Key |
|---|---|
| `POST /api/internal/impersonate/{email}/` | **Staff-only.** Almost certainly forbidden to an Api-Key (the key is scoped to one user). Keep on the staff-JWT path. |
| `/api/internal/sequence/*` (automation sequences) | Not exposed. No public route family. Keep as an open gap. |
| `GET /siftline/board/{uuid}/column/` (list columns) | Not in public spec — use the column-scoped card endpoints instead. |
| `GET /siftline/property/{uuid}/card/` (property→cards lookup) | Not in public spec — derive card membership from column card lists / board-filtered search. |
| `x-http-method-override: GET` on property search | Not in the spec, but **confirmed working with an Api-Key (2026-07-21)**. Documented `GET /property/?query=` also works. Use either. |
| `POST /property/{uuid}/message/` (property-scoped) | Not in the spec, but **confirmed working with an Api-Key (201/204, 2026-07-21)**. Skip-trace keeps using it. |
| `/api/internal/account/user/` (user list) | Public path is `/api/internal/user/`. |
| `/api/internal/properties/status/` (status list) | Public paths are `/global-status/` and `/properties/global-status/`. |

> **Recommendation:** don't assume an Api-Key can hit the internal-only routes just because the JWT can. Test each one you care about with an Api-Key; where it 403s, that route stays on the JWT/staff path. If any of them *do* answer to an Api-Key, capture it here with a dated note.

---

## Canonical Python Implementation

The existing clients in `Shared/clients/` (`reisift_auth.py`, `crm_api.py`, `siftmap_api.py`, `crm_filters.py`, `siftmap_filters.py`) are built around the Bearer JWT. To support this Api-Key path:

- Add an Api-Key mode to `reisift_auth.py`'s header builder: emit `Authorization: Api-Key <key>` and drop the `origin`/`referer`/`x-reisift-ui-version` browser headers. Read the key from `Shared/clients/config/reisift_apikey.json`.
- Keep both modes selectable (`auth_mode="apikey" | "jwt"`) so agents can fall back to JWT for the internal-only routes above.
- No other client changes needed for the endpoints FCRE uses — paths and bodies are identical; only the auth header differs (plus the message-board owner-scope swap noted in the skip-trace section).

---

## Open Gaps (Api-Key path)

- ~~Filter-DSL search via documented GET~~ — **RESOLVED 2026-07-21.** Both the POST-as-GET override and `GET /property/?query=` work with an Api-Key.
- **Which board the app surfaces** — property vs. owner message board (see §CRM 3). Confirm in-app that skip-trace's property-scoped posts are team-visible.
- **Staff-gated routes on an Api-Key** — the key carries `staff`, so impersonate/sequences *might* answer to it. Untested; test before relying.
- **Sequences** — still no public route family; unknown whether the staff key reaches the internal sequence routes.
- **Deal / custom-field / document request-body schemas** — endpoints confirmed; pull exact field schemas from `spec.yaml` when first used in code.

---

*Last updated: July 20, 2026 — Florida Cash Real Estate. Confirmed against `developers.datasift.ai` OpenAPI specs (DataSift Core + SiftMap), pulled July 20, 2026.*
