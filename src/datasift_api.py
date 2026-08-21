"""DataSift/REISift REST API client (Open API key auth, early access).

Distinct from datasift_uploader.py's Playwright automation — this talks
directly to the documented REST API instead of clicking through app.reisift.io.

Two base URLs share the same Api-Key header:
    Core:    https://apiv2.reisift.io   (properties, owners, tags, lists,
             filter presets, custom fields, skip trace, activity)
    SiftMap: https://map.reisift.io     (nationwide search, map filters)

Operating rules baked in throughout, per the documented API contract:
    - Verify the data, never the exit code — every write function here reads
      back what it wrote rather than trusting a 200/201.
    - Single-threaded, ~2 req/s, exponential backoff honoring Retry-After.
      Measured: 6 threads/~7rps 429'd 529 of 740 requests; 1 thread/~2rps
      with backoff completed cleanly. Do not raise concurrency casually.
    - Tags are always an array. A comma-joined string creates one literal tag.
    - Entity (business) owners omit first_name/last_name entirely rather than
      sending "" — the API hard-rejects a blank first_name with a 400.
    - Dedupe with POST .../exists/ before every create; batch work uses
      bulk-create once, never create-in-a-loop.

Two auth schemes:
    - `Authorization: Api-Key <key>` — every read route, plus tags, notes,
      custom fields, skip trace, phone tags.
    - `Authorization: Bearer <JWT>` — required by `bulk-create`. Minted via
      POST /api/token/ with DATASIFT_EMAIL/PASSWORD.

  Both MUST resolve to the same DataSift user. On 2026-08-21 they had
  silently drifted apart: the Api-Key belonged to a different team seat than
  the JWT, so the pipeline wrote as two different people depending on which
  call it made. Re-issuing the key under the account owner's login fixed it.
  If either credential is ever swapped, call whoami() on both and compare the
  returned user *uuid*, not just the email.

THE CREATION RULE, settled by single-variable tests 2026-08-21:

    *** Create ONLY via bulk_create_properties(), even for a single record
        (total: 1 is valid). Never via create_property(). ***

  DataSift keeps a primary DB and a separate Elasticsearch index. The CRM web
  UI — the only surface the team actually works from — reads ES. Individual
  `POST` creates write the DB alone: they return 201 with a real, correct,
  uuid-retrievable record that never appears in the CRM. bulk-create runs
  through the activity/job queue, which indexes into ES, and shows up
  normally.

  This began as an A/B that confounded endpoint with auth (bulk+JWT visible,
  individual+Api-Key invisible). Both alternatives have since been eliminated
  one variable at a time:
    - auth is not it: an individual create made with the account owner's own
      JWT returned 201, reached the API list index, and still could not be
      found in that same owner's CRM UI.
    - the mount is not it: /api/internal/property/ and
      /api/internal/properties/property/ both behave that way.
    - record `type` is not it: bulk-created records on equally fake addresses
      index as `incomplete` too, and are visible.

  Corollary worth internalizing: a 201, a uuid, and a clean read-back by uuid
  are TOGETHER still not evidence a record is usable. Only presence in a
  list/search surface is. This module's history is a chain of confident
  conclusions drawn from exactly that insufficient evidence.

  Third mount warning: bare `POST /property/` — what Ty's datasift_api_upload.py
  uses — returns 403 on this account for GET, OPTIONS and POST alike, under
  both auth schemes including a super-admin key. Not a role ceiling and not
  fixable by switching seats; that mount is simply not available here.
"""

import json as _json
import logging
import re
import time
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

CORE_BASE = config.DATASIFT_API_CORE_BASE
SIFTMAP_BASE = config.DATASIFT_API_SIFTMAP_BASE

_MIN_INTERVAL_SECONDS = 0.5  # ~2 req/s
_last_request_at = 0.0

_JWT_REFRESH_SECONDS = 30 * 60  # re-mint well inside the ~48h expiry, per docs' own guidance
_jwt_access_token: str | None = None
_jwt_minted_at = 0.0


class DataSiftAPIError(Exception):
    """Raised when a DataSift API call fails after retries or credentials are missing."""


# ── Core request plumbing ────────────────────────────────────────────

def _mint_jwt() -> str:
    if not config.DATASIFT_EMAIL or not config.DATASIFT_PASSWORD:
        raise DataSiftAPIError("DATASIFT_EMAIL/DATASIFT_PASSWORD not set — required to mint a JWT")
    resp = requests.post(f"{CORE_BASE}/api/token/", json={
        "email": config.DATASIFT_EMAIL, "password": config.DATASIFT_PASSWORD,
    }, timeout=30)
    if not resp.ok:
        raise DataSiftAPIError(f"JWT mint failed: {resp.status_code}: {resp.text[:300]}")
    return resp.json()["access"]


def _get_jwt() -> str:
    global _jwt_access_token, _jwt_minted_at
    if not _jwt_access_token or (time.monotonic() - _jwt_minted_at) > _JWT_REFRESH_SECONDS:
        _jwt_access_token = _mint_jwt()
        _jwt_minted_at = time.monotonic()
        logger.info("Minted a fresh DataSift JWT")
    return _jwt_access_token


def _headers(has_body: bool, auth: str = "api_key") -> dict:
    if auth == "jwt":
        headers = {"Authorization": f"Bearer {_get_jwt()}", "Accept": "application/json"}
    else:
        if not config.DATASIFT_API_KEY:
            raise DataSiftAPIError("DATASIFT_API_KEY not set in .env")
        headers = {
            "Authorization": f"Api-Key {config.DATASIFT_API_KEY}",
            "Accept": "application/json",
        }
    if has_body:
        headers["Content-Type"] = "application/json"
    return headers


def _pace() -> None:
    """Enforce the single-threaded ~2 req/s ceiling before every call."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < _MIN_INTERVAL_SECONDS:
        time.sleep(_MIN_INTERVAL_SECONDS - elapsed)
    _last_request_at = time.monotonic()


_DRY_RUN = False


def set_dry_run(enabled: bool) -> None:
    """Global kill-switch for every mutating call in this module.

    When on, writes are logged and return a `{"_dry_run": True, ...}` descriptor
    instead of touching the CRM; reads pass through normally. Adopted from
    Tyler Austin's crm_api.py, which defaults every write to dry_run=True.

    Implemented as one interception point in _request() rather than a kwarg on
    thirty functions, so a new write function is covered automatically instead
    of being safe only if its author remembered.
    """
    global _DRY_RUN
    _DRY_RUN = enabled
    logger.warning("DataSift API dry-run mode %s", "ENABLED" if enabled else "DISABLED")


def is_dry_run() -> bool:
    return _DRY_RUN


_MUTATING_METHODS = {"POST", "PATCH", "PUT", "DELETE"}


def _request(method: str, url: str, *, json_body: Any = None,
             params: dict | None = None, max_retries: int = 5,
             timeout: int = 30, auth: str = "api_key",
             mutating: bool | None = None) -> dict | list | None:
    """One paced, retried HTTP call. Returns parsed JSON, or None for empty/204 bodies.
    auth="jwt" for the handful of endpoints (bulk-create) that reject the Api-Key scheme.

    `mutating` overrides the method-based guess — pass False for the read-only
    endpoints that happen to use POST (exists/, compile-filter/), so dry-run
    mode doesn't stub out a lookup the caller needs a real answer from.
    """
    if mutating is None:
        mutating = method.upper() in _MUTATING_METHODS
    if _DRY_RUN and mutating:
        logger.info("DRY RUN - would %s %s%s", method, url,
                     f" body={_json.dumps(json_body)[:300]}" if json_body is not None else "")
        return {"_dry_run": True, "method": method, "url": url, "body": json_body}

    delay = 2.0
    for attempt in range(1, max_retries + 1):
        _pace()
        resp = requests.request(
            method, url, headers=_headers(has_body=json_body is not None, auth=auth),
            json=json_body, params=params, timeout=timeout,
        )
        if resp.status_code == 429:
            wait = delay
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = max(wait, float(retry_after))
                except ValueError:
                    pass
            logger.warning("DataSift API rate limited (429), waiting %.0fs [%d/%d]: %s",
                            wait, attempt, max_retries, url)
            time.sleep(wait)
            delay = min(delay * 2, 30)
            continue
        if resp.status_code >= 500 and attempt < max_retries:
            logger.warning("DataSift API server error (%d), retrying in %.0fs: %s",
                            resp.status_code, delay, url)
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        if not resp.ok:
            raise DataSiftAPIError(
                f"{method} {url} -> {resp.status_code}: {resp.text[:500]}"
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()
    raise DataSiftAPIError(f"Gave up on {method} {url} after {max_retries} attempts (rate limited)")


def _page_items(body: dict) -> list:
    return body.get("data", body.get("results", []))


def get_all(url: str, params: dict | None = None, *, siftmap_style: bool = False,
            page_size: int = 100, max_pages: int = 500) -> list[dict]:
    """Walk a paginated list endpoint to completion.

    Core API uses limit/offset with a `count` total; SiftMap uses
    offset/page_size with a `next` cursor. Pass siftmap_style=True for the
    latter.
    """
    params = dict(params or {})
    items: list[dict] = []
    if not siftmap_style:
        params["limit"] = page_size
        offset = 0
        for _ in range(max_pages):
            params["offset"] = offset
            body = _request("GET", url, params=params) or {}
            page = _page_items(body)
            items.extend(page)
            count = body.get("count", len(items))
            offset += page_size
            if offset >= count or not page:
                break
    else:
        params["page_size"] = page_size
        params["offset"] = 0
        for _ in range(max_pages):
            body = _request("GET", url, params=params) or {}
            page = _page_items(body)
            items.extend(page)
            if not body.get("next"):
                break
            params["offset"] += page_size
    return items


def whoami() -> dict:
    """Sanity-check the API key. Returns the user profile (uuid, name, email, role)."""
    return _request("GET", f"{CORE_BASE}/api/internal/user/")


# ── Properties ────────────────────────────────────────────────────────

def property_exists(*, reapi_id: str | None = None, sift_id: str | None = None) -> dict | None:
    """Dedupe gate: check whether a property already exists before creating one."""
    if not reapi_id and not sift_id:
        raise ValueError("property_exists requires reapi_id or sift_id")
    body: dict = {}
    if reapi_id:
        body["reapi_id"] = reapi_id
    if sift_id:
        body["sift_id"] = sift_id
    return _request("POST", f"{CORE_BASE}/api/internal/property/exists/", json_body=body, mutating=False) or None


def search_by_address(address_prefix: str, *, limit: int = 10, offset: int = 0,
                       ordering: str = "-list_count",
                       property_type: str = "clean") -> list[dict]:
    """Search properties by address prefix. **This actually works** — verified
    live 2026-08-21 with a control: a real prefix returns exactly its record,
    a gibberish prefix returns 0.

    Uses the undocumented POST-as-GET pattern: POST to the list endpoint with
    header `x-http-method-override: GET` and an Elasticsearch-style body. Found
    in Tyler Austin's FCRE crm_api.py, where it runs in production.

    This supersedes the long-standing claim that no address search exists. That
    claim was true of the `search=` *query parameter*, which the endpoint
    silently ignores (returning the same unfiltered page for any value — which
    is exactly why it looked like a lagging index for so long). The POST body
    form is a different mechanism and genuinely filters.

    Consequences worth acting on:
      - Cross-run dedupe is solved. A record created in an earlier process can
        now be found by address.
      - Prefer this over the duplicate-400 trick in create_property(), which
        CREATES an invisible orphan when the address does not already exist.
      - Prefer this over the local uuid map in datasift_uploader.

    `property_type` filters server-side: "clean" will NOT match records that
    failed address validation — pass "incomplete" for those, or None to skip
    the filter. Match is a PREFIX, so pass the leading portion of the street
    ("17642 S Tacoma"), not a full normalized address.
    """
    must: dict = {"search": f"address_prefix:{address_prefix}"}
    if property_type:
        must["property_type"] = property_type
    body = {"limit": limit, "offset": offset, "ordering": ordering,
            "query": {"must": must}}
    headers = _headers(has_body=True, auth="api_key")
    headers["x-http-method-override"] = "GET"
    _pace()
    resp = requests.post(f"{CORE_BASE}/api/internal/property/",
                         headers=headers, json=body, timeout=45)
    if not resp.ok:
        raise DataSiftAPIError(
            f"search_by_address({address_prefix!r}) -> {resp.status_code}: {resp.text[:300]}")
    return _page_items(resp.json() or {})


def _pick_best(results: list[dict]) -> dict:
    """Duplicate resolution: prefer a record with a non-null `status` — those
    are the actively managed leads. Tyler's rule; adopted as-is."""
    with_status = [r for r in results if r.get("status")]
    return (with_status or results)[0]


#: The server rewrites streets on write — "4920 South Troost Avenue" is stored
#: as "4920 S Troost Ave", "17642 S Tacoma Ave" as "17642 S Tacoma St". So a
#: prefix built from the caller's own wording will miss. Normalize both sides
#: and match on the house number, which is the one stable token.
_STREET_ABBR = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
    "STREET": "ST", "AVENUE": "AVE", "DRIVE": "DR", "ROAD": "RD",
    "PLACE": "PL", "LANE": "LN", "COURT": "CT", "BOULEVARD": "BLVD",
    "CIRCLE": "CIR", "TERRACE": "TER", "PARKWAY": "PKWY", "TRAIL": "TRL",
    "HIGHWAY": "HWY", "SUITE": "STE", "APARTMENT": "APT",
}


_STREET_SUFFIXES = {"ST", "AVE", "DR", "RD", "PL", "LN", "CT", "BLVD", "CIR",
                    "TER", "PKWY", "TRL", "HWY", "WAY", "LOOP", "PT", "RUN"}


def _norm_street(street: str) -> str:
    toks = re.sub(r"[^\w\s]", " ", (street or "").upper()).split()
    return " ".join(_STREET_ABBR.get(t, t) for t in toks)


def _strip_suffix(normalized: str) -> str:
    """Drop a trailing street-type token ("17642 S TACOMA ST" -> "17642 S TACOMA")."""
    toks = normalized.split()
    if len(toks) > 2 and toks[-1] in _STREET_SUFFIXES:
        toks = toks[:-1]
    return " ".join(toks)


def find_property_by_address(street: str, city: str = "", state: str = "") -> dict | None:
    """Resolve one property by address, or None. Read-only — unlike the
    duplicate-400 trick, it never creates anything on a miss.

    Searches on the house number (the only token the server leaves alone) and
    then matches the full street client-side after normalizing both sides
    through _STREET_ABBR. Matching the caller's raw wording against the stored
    form fails on exactly the cases you would most want it to catch — verified
    live: "4920 South Troost Avenue" is stored as "4920 S Troost Ave".

    Checks both `property_type` buckets: "clean" first, then "incomplete",
    since a record whose address failed validation is invisible to the former.
    """
    if not street or not street.split():
        return None
    house = street.split()[0]
    want = _norm_street(street)

    for ptype in ("clean", "incomplete"):
        try:
            results = search_by_address(house, property_type=ptype, limit=50)
        except DataSiftAPIError as e:
            logger.warning("search_by_address failed for %r: %s", house, e)
            return None

        def city_ok(addr):
            return (not city
                    or (addr.get("city") or "").strip().lower() == city.strip().lower())

        # Tier 1 — exact match on the normalized street.
        exact = [r for r in results
                 if _norm_street((r.get("address") or {}).get("street")) == want
                 and city_ok(r.get("address") or {})]
        if exact:
            return _pick_best(exact)

        # Tier 2 — ignore the street-type suffix. The server GEOCODES on write
        # and will correct a wrong one: "17642 S Tacoma Ave" was stored as
        # "17642 S Tacoma St" (verified live). Requiring the suffix to match
        # would miss exactly the records whose address we got slightly wrong,
        # which is the case worth catching. House number + street name + city
        # is specific enough to be safe; the suffix is the only thing dropped.
        loose = [r for r in results
                 if _strip_suffix(_norm_street((r.get("address") or {}).get("street"))) ==
                 _strip_suffix(want)
                 and city_ok(r.get("address") or {})]
        if loose:
            stored = (loose[0].get("address") or {}).get("street")
            logger.info("find_property_by_address: matched %r to stored %r "
                         "by ignoring the street suffix (server corrected it)",
                         street, stored)
            return _pick_best(loose)
    return None


def build_owner_payload(*, address: dict, first_name: str = "", last_name: str = "",
                         company: str = "", phones: list[dict] | None = None,
                         emails: list[dict] | None = None) -> dict:
    """Build an owner payload.

    `address` (the owner's mailing address) is required — confirmed live:
    omitting it 400s with `{"owner": {"address": ["This field is required."]}}`,
    which isn't documented in the API reference's create example.

    Entity (business) owners omit first_name/last_name entirely instead of
    sending "" — also confirmed live: the API 400s on a blank first_name, and
    the rejected keys aren't merely forbidden, they're absent from the entity
    shape. This is the fix for a real gap in the current CSV pipeline, where
    entity owners get blank name strings that the CSV importer tolerates but
    this API will not.
    """
    owner: dict = {"address": address, "phones": phones or [], "emails": emails or []}
    if company:
        owner["company"] = company
    else:
        owner["first_name"] = first_name
        owner["last_name"] = last_name
    return owner


def create_property(*, address: dict, owner: dict | None = None,
                     tags: list[str] | None = None, lists: str | list[str] | None = None,
                     status: dict | None = None, assigned_to: str | None = None) -> dict:
    """Create ONE property via the individual, synchronous endpoint.

    *** NOT A CREATION PATH. Use bulk_create_properties() instead. ***

    A record made here is written to the primary DB but never indexed into
    Elasticsearch, so it is invisible in the CRM web UI permanently — see the
    module docstring for the tests that established this. It returns a
    perfectly convincing 201 with a working uuid, which is exactly what makes
    it dangerous.

    Two legitimate uses remain, both of them deliberate:

    1. The duplicate-400 trick. Calling this for an address that ALREADY
       exists returns 400 naming the real uuid, which
       extract_existing_property_uuid() parses out. That is a genuine
       address-based existence check across process runs — the thing CLAUDE.md
       long claimed was impossible.

       CAUTION, and this is the whole reason it needs a warning: it is only
       safe on an address you are confident already exists. On an address
       that does NOT exist, this does not "fail to find" it — it CREATES it,
       as an invisible DB-only orphan, and squats the address so a later
       bulk-create of the same row is rejected as a duplicate. Never use it
       as a speculative lookup.

    2. Diagnostics, where DB-only creation is the thing being tested.

    Deliberately does NOT accept a `notes` kwarg: notes sent inline on create
    return 200 and are silently discarded. Use add_notes() after create.
    """
    payload: dict = {"address": address}
    if owner:
        payload["owner"] = owner
    if tags:
        payload["tags"] = tags
    if lists:
        payload["lists"] = lists
    if status:
        payload["status"] = status
    if assigned_to:
        payload["assigned_to"] = assigned_to
    created = _request("POST", f"{CORE_BASE}/api/internal/property/", json_body=payload)
    logger.info("Created property %s: %s", created.get("uuid"), address.get("street"))
    return created


_EXISTING_PROPERTY_UUID_RE = re.compile(r'"property":\s*\["([0-9a-fA-F-]{36})"\]')


def extract_existing_property_uuid(error: "DataSiftAPIError") -> str | None:
    """Parse the real uuid out of a `create_property()` 400 whose body is
    `{"non_field_errors": ["Property address already exists!"], "property": ["<uuid>"]}`.

    This dedupe check is the most reliable existence signal available —
    confirmed live 2026-08-19 to reflect a genuinely up-to-date store, unlike
    the list endpoint's search index, which can lag 10-15+ minutes or never
    catch up at all for some records. Returns None if the error isn't this
    specific shape (a real validation problem, not a duplicate)."""
    m = _EXISTING_PROPERTY_UUID_RE.search(str(error))
    return m.group(1) if m else None


#: Records per bulk-create call. The server's real ceiling is unknown — 6 is
#: the largest batch verified end to end. 100 is a deliberate guess well under
#: any plausible limit; lower it if large batches start returning short
#: `accepted` counts.
BULK_CHUNK_SIZE = 100


def bulk_create_properties(records: list[dict], *,
                            chunk_size: int = BULK_CHUNK_SIZE) -> list[dict]:
    """Create properties via the async JWT-only bulk-create job queue. THE
    creation path — see the module docstring.

    Splits into chunks and returns one job descriptor per chunk, so a caller
    can compare `accepted` against what it submitted.

    Schema notes, none documented, all confirmed live:
      - Requires `Authorization: Bearer <JWT>`; the Api-Key scheme 401s here
        specifically, even though it works on every other property endpoint.
      - Payload is `{"properties": [...], "total": N}`, not a bare list.
      - Returns 202 with `{"activity", "total", "accepted", "processed",
        "status": "enqueued"}` — a job receipt, NOT the created records. No
        uuids come back; resolve them with wait_for_properties().
      - `processed` is 0 at submit time and the job's own activity record is
        NOT retrievable: the returned activity uuid 404s on
        GET /api/internal/activity/{uuid}/, and bulk jobs never appear in
        that list (verified — `type=create_properties` returns 0 for an
        account with a completed bulk job). So there is no completion signal
        to poll. Confirm by looking for the records themselves.
    """
    if not records:
        return []
    jobs = []
    for i in range(0, len(records), chunk_size):
        chunk = records[i:i + chunk_size]
        job = _request(
            "POST", f"{CORE_BASE}/api/internal/property/bulk-create/",
            json_body={"properties": chunk, "total": len(chunk)}, auth="jwt",
        ) or {}
        accepted = job.get("accepted")
        if accepted is not None and accepted != len(chunk):
            logger.warning("bulk-create chunk %d: submitted %d but accepted %d",
                            i // chunk_size + 1, len(chunk), accepted)
        logger.info("bulk-create chunk %d/%d: %d records, activity=%s status=%s",
                     i // chunk_size + 1, (len(records) + chunk_size - 1) // chunk_size,
                     len(chunk), job.get("activity"), job.get("status"))
        jobs.append(job)
    return jobs


def wait_for_properties(addresses: list[tuple[str, str]], *,
                         timeout_seconds: float = 300.0,
                         poll_seconds: float = 10.0,
                         verify_live: bool = False) -> dict[str, dict]:
    """Poll the property list until every (street, city) pair shows up, or
    the timeout expires. Returns {"street|city": record} for those found;
    missing addresses are simply absent, and the caller decides what to do.

    Why polling the list rather than an activity/job status: bulk-create's
    activity record is not retrievable (see bulk_create_properties). The
    records appearing IS the completion signal, and it is also the only
    signal that means what we actually care about — that they reached the
    index the CRM reads.

    Why not the duplicate-400 trick to resolve uuids: on an address the job
    has not written yet, that call CREATES an invisible DB-only orphan and
    squats the address. It is only safe on addresses known to exist.

    Matching is on normalized street+city because the server rewrites case and
    abbreviations ("4920 South Troost Avenue" comes back "4920 S Troost Ave",
    "17642 S Tacoma Ave" -> "17642 S Tacoma St"). A caller that needs to match
    its own input string must normalize the same way, or match loosely.

    **STALE-DELETE HAZARD.** The list index keeps returning a record for a
    while after it is deleted, so on a delete-then-recreate of the same
    address this can hand back the DEAD uuid — hit live 2026-08-21, where the
    follow-up GET 404'd. Pass verify_live=True to GET each candidate and keep
    only ones that actually resolve. It costs one extra request per address,
    and it also returns full detail records rather than the thinner list
    objects (the list omits `type` and `tags`), so prefer it whenever the
    result feeds subsequent per-uuid writes on an address that may have
    existed before.
    """
    def norm(s: str, c: str) -> str:
        return f"{(s or '').strip().lower()}|{(c or '').strip().lower()}"

    remaining = {norm(s, c) for s, c in addresses}
    found: dict[str, dict] = {}
    if not remaining:
        return found

    # Read a page comfortably larger than the batch; newest first.
    page = min(max(len(addresses) * 2, 50), 500)
    deadline = time.monotonic() + timeout_seconds
    while True:
        body = _request("GET", f"{CORE_BASE}/api/internal/property/",
                         params={"ordering": "-created", "limit": page}) or {}
        for rec in _page_items(body):
            addr = rec.get("address") or {}
            key = norm(addr.get("street"), addr.get("city"))
            if key not in remaining:
                continue
            if verify_live:
                # The index lists deleted records for a while; confirm this
                # uuid actually resolves before treating it as found.
                try:
                    rec = get_property(rec.get("uuid"))
                except DataSiftAPIError:
                    continue
            found[key] = rec
            remaining.discard(key)
        if not remaining:
            logger.info("wait_for_properties: all %d record(s) indexed", len(found))
            return found
        if time.monotonic() >= deadline:
            logger.warning("wait_for_properties: %d of %d not indexed after %.0fs: %s",
                            len(remaining), len(addresses), timeout_seconds,
                            sorted(remaining)[:10])
            return found
        time.sleep(poll_seconds)


def resolve_created_properties(addresses: list[tuple[str, str]], *,
                                max_attempts: int = 20, poll_seconds: float = 15.0) -> dict[str, dict]:
    """DEPRECATED shim — use wait_for_properties().

    Kept only so nothing breaks on the old name. The original carried a long
    docstring built on two beliefs that later testing overturned: that
    indexing routinely takes 10-15+ minutes (it is seconds-to-a-minute for
    bulk-create), and that the duplicate-400 trick is a safe fallback for
    resolving anything it fails to find (it is not — on an address that does
    not exist it creates an invisible orphan; see create_property()).
    """
    return wait_for_properties(addresses,
                               timeout_seconds=max_attempts * poll_seconds,
                               poll_seconds=poll_seconds)


def get_property(property_uuid: str) -> dict:
    return _request("GET", f"{CORE_BASE}/api/internal/property/{property_uuid}/")


def delete_property(property_uuid: str) -> None:
    """Destructive — verify the exact uuid you intend to delete before calling this.
    No bulk/filter-based variant exists in this module by design; every call here
    takes one exact uuid, never a list name or filter."""
    _request("DELETE", f"{CORE_BASE}/api/internal/property/{property_uuid}/")


def add_tags(property_uuid: str, tags: list[str]) -> dict:
    """Add tags and verify they landed by reading the record back."""
    result = _request("POST", f"{CORE_BASE}/api/internal/property/{property_uuid}/add-tags/",
                       json_body={"tags": tags})
    if _DRY_RUN:
        # Nothing was written, so reading back would always "fail" — don't emit
        # a warning that reads like a real problem during a rehearsal.
        return result
    record = get_property(property_uuid)
    landed = {t.get("title") if isinstance(t, dict) else t for t in record.get("tags", [])}
    missing = [t for t in tags if t not in landed]
    if missing:
        logger.warning("add_tags: %s did not land on %s", missing, property_uuid)
    return result


def add_lists(property_uuid: str, lists: list[str]) -> dict:
    return _request("POST", f"{CORE_BASE}/api/internal/property/{property_uuid}/add-lists/",
                     json_body={"lists": lists})


def add_notes(property_uuid: str, notes: str) -> dict:
    """The only way property notes actually land — see create_property()'s docstring."""
    return _request("POST", f"{CORE_BASE}/api/internal/property/{property_uuid}/add-notes/",
                     json_body={"notes": notes})


def post_message_board(owner_uuid: str, message: str) -> dict:
    """Owner Message Board — internal team notes, distinct from property Notes."""
    return _request("POST", f"{CORE_BASE}/api/internal/owner/{owner_uuid}/message/",
                     json_body={"message": message})


# ── Custom fields ─────────────────────────────────────────────────────

_custom_field_cache: dict[str, dict] = {}


def list_custom_fields(force_refresh: bool = False) -> dict[str, dict]:
    """{field_label: field_dict} for every custom field definition. Cached — field/option
    ids are stable, so resolve labels to uuids once, not per record.

    Schema note: the generated reference calls this "title"; the live payload
    actually uses "label" (and "field_type", not "type"). Confirmed live 2026-08-19.
    """
    global _custom_field_cache
    if _custom_field_cache and not force_refresh:
        return _custom_field_cache
    body = _request("GET", f"{CORE_BASE}/api/internal/custom-fields/") or {}
    _custom_field_cache = {f["label"]: f for f in _page_items(body)}
    return _custom_field_cache


_custom_field_group_cache: dict[str, dict] = {}


def list_custom_field_groups(force_refresh: bool = False) -> dict[str, dict]:
    global _custom_field_group_cache
    if _custom_field_group_cache and not force_refresh:
        return _custom_field_group_cache
    body = _request("GET", f"{CORE_BASE}/api/internal/custom-fields/group/") or {}
    _custom_field_group_cache = {g["label"]: g for g in _page_items(body)}
    return _custom_field_group_cache


def get_or_create_custom_field_group(label: str, entity_type: str = "property") -> dict:
    groups = list_custom_field_groups()
    if label in groups:
        return groups[label]
    created = _request("POST", f"{CORE_BASE}/api/internal/custom-fields/group/",
                        json_body={"label": label, "entity_type": entity_type})
    _custom_field_group_cache[label] = created
    return created


def get_or_create_custom_field(label: str, field_type: str = "text",
                                group_label: str = "API Migration Test",
                                entity_type: str = "property") -> dict:
    """Create requires entity_type + group_id — not shown in the create example in
    the docs, confirmed live 2026-08-19: `{"entity_type": [...], "group_id": [...]}`."""
    fields = list_custom_fields()
    if label in fields:
        return fields[label]
    group = get_or_create_custom_field_group(group_label, entity_type=entity_type)
    created = _request("POST", f"{CORE_BASE}/api/internal/custom-fields/",
                        json_body={"label": label, "field_type": field_type,
                                   "entity_type": entity_type, "group_id": group["id"]})
    _custom_field_cache[label] = created
    return created


def resolve_custom_field_value(label: str, value, *,
                                fields: dict[str, dict] | None = None) -> tuple[object, str]:
    """Turn one {field_label: value} pair into the value the API will accept.
    Returns (resolved_value, reason_if_unusable) — a non-empty reason means skip.

    **select / multiselect fields need the OPTION'S uuid, not its label.**
    Sending the label is rejected with
    `{"non_field_errors": ["'LEN' is not a valid UUID."]}`. 43 of this
    account's 80 custom fields are select/multiselect, so this is the common
    case, not an edge case.

    On an option that doesn't exist we report and skip — never guess a nearby
    option, never fall back to sending the raw label.
    """
    fields = fields if fields is not None else list_custom_fields()
    field = fields.get(label)
    if not field:
        return None, f"unknown custom field {label!r}"

    ftype = (field.get("field_type") or "").lower()
    if ftype not in ("select", "multiselect"):
        return value, ""

    options = {str(o.get("label")): o.get("uuid")
               for o in (field.get("options") or []) if isinstance(o, dict)}

    if ftype == "multiselect":
        wanted = value if isinstance(value, (list, tuple)) else [
            v.strip() for v in str(value).split(",") if v.strip()]
        resolved, missing = [], []
        for v in wanted:
            uuid = options.get(str(v).strip())
            (resolved if uuid else missing).append(uuid or str(v).strip())
        if missing:
            return None, (f"{label!r}: no such option(s) {missing}; "
                          f"valid: {sorted(options)[:6]}")
        return resolved, ""

    uuid = options.get(str(value).strip())
    if not uuid:
        return None, (f"{label!r}: no such option {str(value).strip()!r}; "
                      f"valid: {sorted(options)[:6]}")
    return uuid, ""


def update_custom_field_values(property_uuid: str, values: dict[str, str]) -> dict:
    """values: {field_label: value}. Resolves labels to field uuids — and
    select/multiselect values to OPTION uuids — then writes and verifies.

    Never creates a missing field: unknown labels are skipped and reported.
    Creating fields on the fly is schema mutation, and this account's custom
    fields are deliberately curated.

    Returns {"written", "skipped", "verified_ok", "mismatched"}. `skipped`
    carries a reason per dropped value, so a caller can surface it instead of
    silently losing data.
    """
    fields = list_custom_fields()
    payload, skipped = [], []
    intended: dict[str, object] = {}

    for label, value in values.items():
        resolved, reason = resolve_custom_field_value(label, value, fields=fields)
        if reason:
            logger.warning("update_custom_field_values: %s", reason)
            skipped.append({"label": label, "value": value, "reason": reason})
            continue
        field_uuid = fields[label]["uuid"]
        payload.append({"field_uuid": field_uuid, "value": resolved})
        intended[field_uuid] = resolved

    if not payload:
        return {"written": 0, "skipped": skipped, "verified_ok": None, "mismatched": []}

    _request(
        "PATCH", f"{CORE_BASE}/api/internal/property/{property_uuid}/custom-field/update-values/",
        json_body=payload,
    )

    # Verify by COMPARISON, not by "it returned 200" — this function exists
    # because a rejected value looks exactly like a successful one.
    verified = _request(
        "GET", f"{CORE_BASE}/api/internal/property/{property_uuid}/custom-field/")
    rows = _page_items(verified) if isinstance(verified, dict) else (verified or [])
    landed: dict[str, object] = {}
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        # Read-back rows nest the field definition under "custom_field" and
        # carry their OWN uuid at the top level — so item["uuid"] is the value
        # row, NOT the field. Confirmed against a live response 2026-08-21;
        # reading the wrong key here silently verifies nothing.
        fid = ((item.get("custom_field") or {}).get("uuid")
               or item.get("field_uuid")
               or (item.get("field") or {}).get("uuid"))
        if fid:
            landed[fid] = item.get("value")

    mismatched = []
    if landed:
        for fid, want in intended.items():
            got = landed.get(fid)
            if got == want:
                continue
            if got is not None and str(got) == str(want):
                continue
            mismatched.append({"field_uuid": fid, "sent": want, "read_back": got})
        if mismatched:
            logger.warning("update_custom_field_values: %d value(s) did not land on %s: %s",
                            len(mismatched), property_uuid, mismatched[:5])

    return {"written": len(payload), "skipped": skipped,
            "verified_ok": (not mismatched) if landed else None,
            "mismatched": mismatched}


# ── Skip trace ────────────────────────────────────────────────────────

#: Set True only after the scope bug below is proven fixed. See the docstring.
ALLOW_ACCOUNT_WIDE_SKIP_TRACE = False


def submit_skip_trace(property_uuids: list[str], *, i_understand_this_is_account_wide: bool = False) -> dict:
    """*** DISABLED BY DEFAULT — THIS ENDPOINT IGNORES `property_uuids`. ***

    **It skip-traces the ENTIRE ACCOUNT, not the uuids you pass**, and it
    spends prepaid credits doing so. Proven live 2026-08-21: a submission of
    exactly 2 property uuids returned
    `{"cost": 19.32, "number_of_records": 161, "cost_per_owner": 0.12}` —
    161 being the whole account — and the stats deltas confirmed ~29 owners
    were actually processed (total_owners 210 -> 239, no_result +20, both +8,
    emails_only +1) for $1.08. It appears to have stopped early only because
    the credit balance ran out. Had the balance been larger, the whole account
    would have been traced and billed.

    This is the same failure mode as `/api/internal/property/enrich/`, which
    also reports a count of every property in the account regardless of input.
    Treat any DataSift endpoint that accepts a record list as account-wide
    until proven otherwise, and prove it on a cheap/free endpoint first.

    Raises unless BOTH `ALLOW_ACCOUNT_WIDE_SKIP_TRACE` is set and the caller
    passes `i_understand_this_is_account_wide=True` — deliberately two gates,
    so this cannot be re-enabled by a single careless edit.

    To scope a skip trace to specific records today: use the DataSift UI, or
    Tracerfy (`tracerfy_skip_tracer.trace_contacts`), which is genuinely
    per-record at ~$0.02.
    """
    if not property_uuids:
        logger.info("submit_skip_trace: nothing to submit")
        return {}

    if not (ALLOW_ACCOUNT_WIDE_SKIP_TRACE and i_understand_this_is_account_wide):
        raise DataSiftAPIError(
            f"submit_skip_trace() is disabled: this endpoint ignores its "
            f"properties list and skip-traces the ENTIRE ACCOUNT ({len(property_uuids)} "
            f"uuid(s) requested), spending prepaid credits. See its docstring. "
            f"Use Tracerfy for per-record tracing, or the DataSift UI."
        )

    logger.warning("BILLED + ACCOUNT-WIDE: submitting DataSift skip trace. The "
                    "%d uuid(s) passed will NOT limit scope.", len(property_uuids))
    return _request("POST", f"{CORE_BASE}/api/internal/property/skip-trace/",
                     json_body={"properties": property_uuids})


def get_skip_trace_stats() -> dict:
    return _request("GET", f"{CORE_BASE}/api/internal/activity/skiptrace/stats/")


# ── Owners / phones / emails ─────────────────────────────────────────

def get_owner(owner_uuid: str) -> dict:
    return _request("GET", f"{CORE_BASE}/api/internal/owner/{owner_uuid}/")


def upsert_phones(owner_uuid: str, phones: list[dict]) -> dict:
    return _request("POST", f"{CORE_BASE}/api/internal/owner/{owner_uuid}/upsert-phones/",
                     json_body={"phones": phones})


def upsert_emails(owner_uuid: str, emails: list[dict]) -> dict:
    return _request("POST", f"{CORE_BASE}/api/internal/owner/{owner_uuid}/upsert-emails/",
                     json_body={"emails": emails})


# ── Phone tags (dial-priority system) ────────────────────────────────

_phone_tag_cache: dict[str, dict] = {}


def list_phone_tags(force_refresh: bool = False) -> dict[str, dict]:
    global _phone_tag_cache
    if _phone_tag_cache and not force_refresh:
        return _phone_tag_cache
    body = _request("GET", f"{CORE_BASE}/api/internal/phone/tag/") or {}
    _phone_tag_cache = {t["title"]: t for t in _page_items(body)}
    return _phone_tag_cache


def get_or_create_phone_tag(title: str) -> dict:
    tags = list_phone_tags()
    if title in tags:
        return tags[title]
    created = _request("POST", f"{CORE_BASE}/api/internal/phone/tag/", json_body={"title": title})
    _phone_tag_cache[title] = created
    return created


def phone_tag_properties_count(tag_uuid: str) -> int:
    body = _request("GET", f"{CORE_BASE}/api/internal/phone/tag/{tag_uuid}/properties-count/") or {}
    return body.get("count", 0)


def set_phone_tags(number_to_tags: dict[str, list[str]]) -> dict:
    """Apply tags to numbers — `{number: [tag_title, ...]}` — in one call.

    CORRECTED 2026-08-21. The previous payload (`{"number": n, "tag_uuid": u}`)
    was accepted with an empty 200 and applied NOTHING; it had been recorded as
    "confirmed live", but that only ever confirmed it did not error. The real
    schema, from `OPTIONS /api/internal/phone/add-phone-tag/`, is a LIST whose
    items carry a **`tags` array of tag UUIDs** (not `tag_uuid`):

        [{"number": "9183107469", "tags": ["<uuid>", "<uuid>"]}]

    Verified semantics, each tested live:
      - `type` is OPTIONAL, and omitting it PRESERVES the phone's existing type.
        Do not send a guessed type — this endpoint upserts the phone object, so
        a wrong `type` would overwrite the real one.
      - Tags APPEND. Existing tags on the number survive, so this is safe to
        call repeatedly and safe on numbers that already carry source tags.
      - Multiple tags per number in ONE item work, which is why this function
        takes a mapping rather than one tag at a time.
    """
    items, wanted = [], {}
    for number, titles in number_to_tags.items():
        titles = [t for t in titles if t]
        if not titles:
            continue
        # TITLES, not uuids. Sending uuids does not fail — it CREATES a new
        # phone tag whose NAME is the uuid string, leaving four pieces of
        # garbage in the namespace and the real tag unapplied. Confirmed the
        # hard way on 2026-08-21. A correctly-tagged phone reads back as
        # ["Dial Third"], which is what the shape should have been inferred
        # from in the first place.
        items.append({"number": number, "tags": titles})
        wanted[number] = set(titles)
    if not items:
        return {"tagged": 0}

    result = _request("POST", f"{CORE_BASE}/api/internal/phone/add-phone-tag/",
                       json_body=items)
    if _DRY_RUN:
        return {"tagged": len(items), "dry_run": True}
    logger.info("set_phone_tags: applied tags to %d number(s)", len(items))
    return {"tagged": len(items), "result": result, "wanted": wanted}


def add_phone_tag(phone_numbers: list[str], tag_title: str) -> dict:
    """Apply ONE tag to several numbers. Thin wrapper over set_phone_tags();
    prefer that when a number needs more than one tag, so they go in one call."""
    return set_phone_tags({n: [tag_title] for n in phone_numbers})


def verify_phone_tags(owner_uuid: str, number_to_tags: dict[str, list[str]]) -> dict:
    """Read the owner back and confirm each number carries the tags requested.

    Necessary because the tag endpoint returns an empty body whether it worked
    or silently did nothing — the record is the only trustworthy signal.

    Compares TITLES against what the record reports, deliberately. An earlier
    version resolved each title to its uuid and compared uuids, which made the
    check circular: it "confirmed" tags that had in fact been created as junk
    tags named after a uuid. Compare the human-readable thing you asked for.
    """
    owner = get_owner(owner_uuid) or {}
    on_record = {(p.get("number") or ""): set(p.get("tags") or [])
                 for p in (owner.get("phones") or [])}
    missing = {}
    for number, titles in number_to_tags.items():
        have = on_record.get(number, set())
        gap = {t for t in titles if t not in have}
        if gap:
            missing[number] = sorted(gap)
    if missing:
        logger.warning("verify_phone_tags: %d number(s) missing tags: %s",
                        len(missing), missing)
    return {"ok": not missing, "missing": missing}


# ── Filter presets ────────────────────────────────────────────────────
# Schema note: list/detail responses use "title" (not "name") and "filters"
# (not "filter_data") — confirmed live 2026-08-19 against 64 real presets.

def list_filter_presets() -> list[dict]:
    return get_all(f"{CORE_BASE}/api/internal/filter-preset/")


def list_filter_preset_folders() -> list[dict]:
    return get_all(f"{CORE_BASE}/api/internal/filter-preset-folder/")


def get_filter_preset(preset_uuid: str) -> dict:
    return _request("GET", f"{CORE_BASE}/api/internal/filter-preset/{preset_uuid}/")


def compile_filter(filter_data: dict) -> dict:
    """Pre-flight validation: resolve a filter definition server-side before creating it."""
    return _request("POST", f"{CORE_BASE}/api/internal/filter-preset/compile/",
                     json_body={"filter_data": filter_data}, mutating=False)


def create_filter_preset(*, name: str, filter_data: dict, folder_uuid: str | None = None) -> dict:
    payload: dict = {"name": name, "filter_data": filter_data}
    if folder_uuid:
        payload["folder"] = folder_uuid
    return _request("POST", f"{CORE_BASE}/api/internal/filter-preset/", json_body=payload)


def update_filter_preset(preset_uuid: str, **fields) -> dict:
    return _request("PATCH", f"{CORE_BASE}/api/internal/filter-preset/{preset_uuid}/",
                     json_body=fields)


# ── SiftMap (Phase C — untested against a live account so far) ────────

def siftmap_search(*, polygon: list[list[float]] | None = None,
                    address: str | None = None) -> dict:
    body: dict = {}
    if polygon:
        body["polygon"] = polygon
    if address:
        body["address"] = address
    return _request("POST", f"{SIFTMAP_BASE}/properties/search/", json_body=body)


def siftmap_add_properties(ids: list[str], *, lists: list[str] | None = None,
                            tags: list[str] | None = None) -> dict:
    body: dict = {"ids": ids}
    if lists:
        body["lists"] = lists
    if tags:
        body["tags"] = tags
    return _request("POST", f"{SIFTMAP_BASE}/properties/add-properties/", json_body=body)


def siftmap_list_filters() -> list[dict]:
    return get_all(f"{SIFTMAP_BASE}/filters/", siftmap_style=True)
