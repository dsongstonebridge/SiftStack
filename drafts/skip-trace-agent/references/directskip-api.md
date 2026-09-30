# DirectSkip API (api0 v2) — quick reference

**Endpoint:** `POST https://api0.directskip.com/v2/search_contact.php`
**Headers:** `Accept: application/json`, `Content-Type: application/json`

## Request body
```json
{
  "api_key": "...",
  "first_name": "", "last_name": "",
  "mailing_address": "", "mailing_city": "", "mailing_state": "", "mailing_zip": "",
  "property_address": "", "property_city": "", "property_state": "", "property_zip": "",
  "custom_field_1": "", "custom_field_2": "", "custom_field_3": ""
}
```
Best results: send owner name + mailing AND property address. Optional flags:
`auto_match_boost` (default 1, leave alone), `dnc_scrub` (0/1), `owner_fix` (0/1).

## Response shape (relevant parts)
```
status.error            "" on success; e.g. "You have no authorized credit card" on billing failure
result_code.result_code  CI = confirmed single match; AB2 = multiple candidate matches (looser)
contacts[]              one per matched person
  names[]              {firstname, lastname, age, deceased: "Y"/"N"}
  phones[]             {phonenumber (10-digit), phonetype: "Mobile"/"Residential"}
  emails[]             {email}
  confirmed_address[]  {street, city, state, zip}
  relatives[]          {name, age, phones[{phonenumber, phonetype}]}   <-- NO relationship label, NO address
```

## Key gotchas
- **No relationship field.** `relatives[]` gives name + age + phones only. Infer immediate
  kin from surname match to the owner/deceased family name (+ age). Relatives array excludes
  neighbors by design.
- **No relative addresses.** Only the primary `contacts[].confirmed_address` has an address.
- **`result_code` AB2 = multiple people returned** (e.g., condos/shared mailing addresses).
  Treat AB2 relatives with more suspicion; the name/phone match gate in `process_records.py`
  filters wrong-household matches automatically.
- **Billing:** orders bill the card on file nightly. Requesting IP must be whitelisted
  (email support@directskip.com with the account email + public IP). No card on file →
  `status.error = "You have no authorized credit card"`.

## API key
Set one of (checked in this order by `directskip_trace.py`):
1. `--api-key` flag
2. env `DIRECTSKIP_API_KEY`
3. `config.json` in the skill root: `{"directskip_api_key": "..."}`  (see config.json.example)
Keep the key out of the committed skill; store it in the personal/org skills folder only.
