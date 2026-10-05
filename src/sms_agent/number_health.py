"""Carrier flag check for the sending numbers, read from NumberVerifier.

Tulsa fork (Jeff, 2026-10-05): never send from a number that more than
`config.NUMBER_HEALTH_MAX_FLAGS` carriers currently flag as spam.

NumberVerifier's public API (x-apikey) returns the monitored numbers but NOT
their flags. The red/green dots on Reports > Caller ID History come from the
dashboard's own AppSync query `getDates`, which needs a dashboard login, so we
sign in exactly the way the web app does: Cognito USER_SRP_AUTH (the plain
password flow is disabled on their client), stdlib only. Each row is
{phone, dt, flags: "att,tmobile,verizon"} with a count > 0 meaning flagged.

Found by reading their dashboard bundle on 2026-10-05; it is an internal API
and can change without notice. Every failure therefore HOLDS sending rather
than passing it, and says so in #SMS once a day.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

import requests

from . import config, store

log = logging.getLogger(__name__)

POOL_ID = "us-west-2_wCrPSacya"
CLIENT_ID = "7ns6dqjj80kft4bhet6hak0qoq"
IDP = "https://cognito-idp.us-west-2.amazonaws.com/"
GRAPHQL = "https://cxdy3quairfdfn3uduxir4jbv4.appsync-api.us-west-2.amazonaws.com/graphql"
CARRIERS = ("AT&T", "T-Mobile", "Verizon")
CACHE_KEY = "number_health"
CACHE_SECONDS = 3600          # the data is daily; an hour is plenty fresh
STALE_DAYS = 4                # no row this recent = we do not know = hold

# --------------------------------------------------------------- Cognito SRP
_N_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6BF12FFA06D98A0864D87602733EC86A64521F2B18177B200C"
    "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB3143DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF")
_N = int(_N_HEX, 16)
_G = 2


def _hex(n: int) -> str:
    return "%x" % n


def _pad(h) -> str:
    h = h if isinstance(h, str) else _hex(h)
    if len(h) % 2 == 1:
        return "0" + h
    if h[0] in "89ABCDEFabcdef":
        return "00" + h
    return h


def _hash(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest().rjust(64, "0")


def _hex_hash(h: str) -> str:
    return _hash(bytes.fromhex(h))


_K = int(_hex_hash("00" + _N_HEX + "02"), 16)


def _idp(target: str, body: dict) -> dict:
    r = requests.post(IDP, json=body, timeout=30, headers={
        "X-Amz-Target": f"AWSCognitoIdentityProviderService.{target}",
        "Content-Type": "application/x-amz-json-1.1"})
    j = r.json()
    if r.status_code != 200:
        raise RuntimeError(f"NumberVerifier login {target}: {j.get('__type')} {j.get('message')}")
    return j


def _login() -> str:
    email = os.getenv("NUMBERVERIFIER_EMAIL", "").strip()
    password = os.getenv("NUMBERVERIFIER_PASSWORD", "").strip()
    if not email or not password:
        raise RuntimeError("NUMBERVERIFIER_EMAIL / NUMBERVERIFIER_PASSWORD not set")
    a = int.from_bytes(os.urandom(128), "big") % _N
    A = pow(_G, a, _N)
    j = _idp("InitiateAuth", {"AuthFlow": "USER_SRP_AUTH", "ClientId": CLIENT_ID,
                              "AuthParameters": {"USERNAME": email, "SRP_A": _hex(A)}})
    if j.get("ChallengeName") != "PASSWORD_VERIFIER":
        raise RuntimeError(f"NumberVerifier login: unexpected challenge {j.get('ChallengeName')}")
    p = j["ChallengeParameters"]
    user_id, B = p["USER_ID_FOR_SRP"], int(p["SRP_B"], 16)
    u = int(_hex_hash(_pad(A) + _pad(B)), 16)
    pool = POOL_ID.split("_")[1]
    x = int(_hex_hash(_pad(p["SALT"]) + _hash(f"{pool}{user_id}:{password}".encode("utf-8"))), 16)
    S = pow(B - _K * pow(_G, x, _N), a + u * x, _N)
    prk = hmac.new(bytes.fromhex(_pad(u)), bytes.fromhex(_pad(S)), hashlib.sha256).digest()
    key = hmac.new(prk, b"Caldera Derived Key\x01", hashlib.sha256).digest()[:16]
    now = dt.datetime.now(dt.timezone.utc)
    ts = now.strftime("%a %b ") + str(now.day) + now.strftime(" %H:%M:%S UTC %Y")
    msg = pool.encode() + user_id.encode() + base64.b64decode(p["SECRET_BLOCK"]) + ts.encode()
    sig = base64.b64encode(hmac.new(key, msg, hashlib.sha256).digest()).decode()
    j = _idp("RespondToAuthChallenge", {
        "ChallengeName": "PASSWORD_VERIFIER", "ClientId": CLIENT_ID,
        "ChallengeResponses": {"USERNAME": user_id, "PASSWORD_CLAIM_SECRET_BLOCK": p["SECRET_BLOCK"],
                               "TIMESTAMP": ts, "PASSWORD_CLAIM_SIGNATURE": sig}})
    if "AuthenticationResult" not in j:
        raise RuntimeError(f"NumberVerifier login needs {j.get('ChallengeName')} (two-factor?)")
    return j["AuthenticationResult"]["AccessToken"]


# --------------------------------------------------------------- the data
def _gql(token: str, query: str, variables: dict) -> dict:
    r = requests.post(GRAPHQL, json={"query": query, "variables": variables},
                      headers={"Authorization": token}, timeout=30)
    j = r.json()
    if r.status_code != 200 or j.get("errors"):
        raise RuntimeError(f"NumberVerifier query: HTTP {r.status_code} {str(j.get('errors'))[:200]}")
    return j["data"]


def _account_id(token: str) -> str:
    cached = store.get_meta("number_health_account")
    if cached:
        return cached
    claims = token.split(".")[1]
    sub = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))["sub"]
    q = ("query L($f: ModelAccountFilterInput, $t: String) { listAccounts(filter: $f, limit: 1000,"
         " nextToken: $t) { items { id } nextToken } }")
    nxt = None
    # Scan-then-filter: an empty page is normal, the match can be several pages in.
    for _ in range(50):
        page = _gql(token, q, {"f": {"or": [{"owner": {"eq": sub}}, {"userIDs": {"contains": sub}}]},
                               "t": nxt})["listAccounts"]
        if page["items"]:
            store.set_meta("number_health_account", page["items"][0]["id"])
            return page["items"][0]["id"]
        nxt = page.get("nextToken")
        if not nxt:
            break
    raise RuntimeError("NumberVerifier: no account found for this login")


def fetch() -> dict:
    """{10-digit number: {"day": "YYYY-MM-DD", "flags": [att, tmo, vz]}}, latest day each."""
    token = _login()
    account = _account_id(token)
    q = ("query G($accountid: String, $nextToken: String, $from: String)"
         " { getDates(accountid: $accountid, nextToken: $nextToken, from: $from) }")
    rows, nxt = [], None
    for _ in range(50):
        page = json.loads(_gql(token, q, {"accountid": account, "from": str(STALE_DAYS + 3),
                                          "nextToken": nxt})["getDates"])
        rows += page.get("items") or []
        nxt = page.get("nextToken")
        if not nxt:
            break
    latest: dict = {}
    for row in rows:
        number = store.clean_phone(row.get("phone") or "")
        parts = str(row.get("flags") or "").split(",")
        if len(number) != 10 or len(parts) != 3:
            continue
        day = str(row.get("dt") or "")[:10]
        if number not in latest or day > latest[number]["day"]:
            latest[number] = {"day": day, "flags": [int(p or 0) for p in parts]}
    return latest


def _snapshot() -> tuple[Optional[dict], str]:
    """Cached fetch. (data, "") or (None, why it could not be read)."""
    raw = store.get_meta(CACHE_KEY)
    if raw:
        cached = json.loads(raw)
        if time.time() - cached.get("at", 0) < CACHE_SECONDS:
            return cached.get("data"), cached.get("error", "")
    try:
        data, err = fetch(), ""
    except Exception as exc:  # noqa: BLE001 - any failure holds sending
        data, err = None, str(exc)[:200]
        log.warning("number health unreadable: %s", err)
        _alert_once("unreadable", "SMS agent: number health check is failing",
                    f"Could not read carrier flags from NumberVerifier, so texting is HELD "
                    f"until it works again.\n{err}")
    store.set_meta(CACHE_KEY, json.dumps({"at": time.time(), "data": data, "error": err}))
    return data, err


def _alert_once(key: str, title: str, body: str) -> None:
    today = dt.date.today().isoformat()
    if store.get_meta(f"number_health_alert:{key}") == today:
        return
    store.set_meta(f"number_health_alert:{key}", today)
    try:
        from . import escalate
        escalate.alert(title, body, kind="number_health")
    except Exception as exc:  # noqa: BLE001
        log.warning("number health alert failed: %s", exc)


def check(number: str) -> tuple[Optional[bool], str]:
    """(True, "") may send; (False, why) pulled; (None, why) unknown, hold."""
    data, err = _snapshot()
    if data is None:
        return None, f"number health unreadable: {err}"
    digits = store.clean_phone(number)
    row = data.get(digits)
    if not row:
        return None, f"...{digits[-4:]} is not monitored in NumberVerifier"
    age = (dt.date.today() - dt.date.fromisoformat(row["day"])).days
    if age > STALE_DAYS:
        return None, f"...{digits[-4:]} flag data is {age} days old"
    names = [c for c, n in zip(CARRIERS, row["flags"]) if n > 0]
    if len(names) > config.NUMBER_HEALTH_MAX_FLAGS:
        why = f"...{digits[-4:]} flagged by {', '.join(names)} ({row['day']})"
        _alert_once(f"pulled:{digits}", "SMS agent: sending number pulled",
                    f"{why}. No texts go out from it until it is back to "
                    f"{config.NUMBER_HEALTH_MAX_FLAGS} flag(s) or fewer.")
        return False, why
    return True, ""
