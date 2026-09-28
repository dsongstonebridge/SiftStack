"""Shared helpers for the Tulsa Homebuyers call-coaching pipeline.

Paths, secrets, the DataSift / OpenRouter HTTP clients, time formatting and the
caller roster. Every other script imports from here, so the rules live in one
place.

Secrets: call-coaching/.env first, then SiftStack/.env (for DATASIFT_API_KEY).
Both are gitignored. The repo is PUBLIC: nothing personal is ever written
outside output/, which is gitignored too.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "output"
RUBRICS = HERE / "rubrics"
CALLS_JSON = OUT / "calls.json"            # every call found in DataSift
REC_DIR = OUT / "recordings"
TR_DIR = OUT / "transcripts"
REPORT_DIR = OUT / "reports"
SCORECARD_DIR = OUT / "scorecards"
QUEUE_JSON = OUT / "grading_queue.json"

BUSINESS_TZ = ZoneInfo("America/Chicago")
CALLING_START = "2026-09-28"               # consistent calling began; nothing useful before

PIPELINES = ("cold_call", "lead_management", "closing")

# Windows consoles default to cp1252; force utf-8 so names and quotes never crash a run.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- secrets

def _read_env_file(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def env(key: str, required: bool = True) -> str:
    val = os.environ.get(key, "").strip()
    for f in (HERE / ".env", ROOT / ".env"):
        if not val:
            val = _read_env_file(f).get(key, "")
    if required and not val:
        sys.exit(f"Missing {key}. Put it in call-coaching\\.env (see .env.example).")
    return val


# ---------------------------------------------------------------- DataSift

DS_API = "https://apiv2.reisift.io"


def ds_request(path: str, *, method="GET", body=None, override=None, params=None):
    """DataSift request with the Open API key. Retries 429s with backoff."""
    url = DS_API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"authorization": "Api-Key " + env("DATASIFT_API_KEY"),
               "origin": "https://app.reisift.io", "referer": "https://app.reisift.io/",
               "accept": "application/json"}
    if body is not None:
        headers["content-type"] = "application/json"
    if override:
        headers["x-http-method-override"] = override
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(8):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 7:
                time.sleep(min(60, 3 * (attempt + 1)))
                continue
            raise RuntimeError(f"DataSift {e.code} on {path}: {e.read()[:200]!r}") from None
    raise RuntimeError(f"DataSift kept rate-limiting {path}")


# ---------------------------------------------------------------- OpenRouter

OPENROUTER_MODEL = "google/gemini-2.5-flash"
# Measured from OpenRouter's listed pricing 2026-09-28: audio $1/M tokens at 32 tokens/sec
# (~$0.0019/min) plus the transcript + delivery notes as output. Budget ~$0.003/min, plus
# the small text triage pass.
COST_PER_AUDIO_MIN = 0.003
COST_PER_TRIAGE = 0.0005


def openrouter(messages: list, timeout: int = 240) -> str:
    body = {"model": OPENROUTER_MODEL, "messages": messages}
    last = None
    for attempt in range(3):
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions", data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + env("OPENROUTER_API_KEY"),
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read())
            content = (resp.get("choices") or [{}])[0].get("message", {}).get("content")
            if content:
                return content
            last = "empty response: " + json.dumps(resp)[:200]
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}"
            if e.code in (401, 402, 403):
                break
        except Exception as e:  # noqa: BLE001
            last = str(e)[:200]
        time.sleep(2 * (attempt + 1))
    raise RuntimeError("OpenRouter failed: " + str(last))


# ---------------------------------------------------------------- time / io

def utc_to_local(ts: str | None) -> datetime | None:
    """'2026-09-16T21:25:58+0000' or '2026-09-16 21:26:05' (UTC) -> Chicago time."""
    if not ts:
        return None
    s = ts.replace("T", " ")[:19]
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(BUSINESS_TZ)
    except ValueError:
        return None


def fmt_local(dt: datetime | None) -> str:
    # No %-d / %-I: those crash on Windows.
    if not dt:
        return ""
    hour = dt.hour % 12 or 12
    return f"{dt:%Y-%m-%d} {hour}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


def mmss(sec) -> str:
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


def load_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- roster

def roster() -> dict:
    """callers.json: display names and exclusions. Names only (public repo)."""
    return load_json(HERE / "callers.json", {"callers": [], "exclude": []})


def caller_key(name: str | None) -> str:
    return " ".join((name or "unknown").lower().split())


def display_name(name: str | None) -> str:
    key = caller_key(name)
    for c in roster().get("callers", []):
        if key == caller_key(c.get("datasift_name")) or key == caller_key(c.get("name")):
            return c["name"]
    return (name or "Unknown").strip() or "Unknown"


def is_excluded(name: str | None) -> bool:
    return caller_key(name) in {caller_key(n) for n in roster().get("exclude", [])}


def slug(s: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in s.lower()).strip("_") or "unknown"
