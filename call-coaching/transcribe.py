"""transcribe.py - transcribe recordings with tonality notes, then sort each call.

PAID STEP. Gemini 2.5 Flash via OpenRouter (OPENROUTER_API_KEY in
call-coaching/.env), about $0.003 per audio minute. Without --commit this only
prints what it would do and the estimated cost. Nothing is sent.

Two passes per call, adapted from src/call_coaching/transcribe.py:
  1. AUDIO -> transcript with AGENT / SELLER labels and bracketed delivery
     notes ([long pause 4s], [rushed], [warm tone]...). The model hears the
     call, so tonality notes are real, not guessed from text.
  2. TEXT -> sort: cold_call / lead_management / combined / closing /
     not_gradeable, full vs short report, outcome, and for a combined call the
     line where it switches. Decided by WHAT IS SAID, never by who dialed:
     Diego makes both cold calls and follow-ups, and often rolls a cold call
     straight into lead management (combined).

Calls under --min-seconds (default 10) are not sent: they go straight to the
Connection Log as "too short to be a conversation".

NOTHING IS TRANSCRIBED UNLESS THE USER NAMES THE CALLS (rule set 2026-10-26).
--commit refuses to run without --call-id, and there is no "transcribe
everything" option. Only the ids listed are ever sent; an id that is not in
calls.json, or an unknown call type, stops the whole run before anything is
spent.

THE USER CAN SET THE CALL TYPE: --call-id CA1:combined,CA2:cold. Their type
always wins over the sorting step's; the transcript header records both.
Types: cold (cold_call), lm (lead_management), combined, closing.

Every transcribed call is scored (scoring is free); a call the user never
named is never transcribed, so it is never scored.

USAGE (from SiftStack root):
  py -3 call-coaching/transcribe.py                                     # estimate only, free
  py -3 call-coaching/transcribe.py --call-id CA1,CA2                   # estimate for named calls, free
  py -3 call-coaching/transcribe.py --commit --call-id CA1,CA2          # PAID: only these calls
  py -3 call-coaching/transcribe.py --commit --call-id CA1:combined     # PAID, type set by the user
  py -3 call-coaching/transcribe.py --commit --call-id CA1 --force      # redo one already transcribed
"""
from __future__ import annotations

import argparse
import base64
import json
import re
from datetime import datetime
from pathlib import Path

from cc_common import (CALLS_JSON, COST_PER_AUDIO_MIN, COST_PER_TRIAGE, TR_DIR, display_name,
                       is_excluded, load_json, log, mmss, openrouter, parse_ids, save_json)

TRANSCRIBE_PROMPT = """Transcribe this real estate phone call. The company is Tulsa Homebuyers, a local home buyer in Tulsa, Oklahoma.
Rules:
- Label speakers AGENT and SELLER. AGENT is ALWAYS the Tulsa Homebuyers representative{agent_hint}: the person asking about buying the property, following up on it, or making an offer. SELLER is ALWAYS the property owner or their contact, even if the seller placed or returned the call and speaks first. Decide by what each person SAYS, not by who spoke first. Use VOICEMAIL for an automated greeting and AGENT_VM for a voicemail message our agent leaves. Use OTHER for anyone else (a relative who is not the owner, a gatekeeper).
- One line per speaker turn: LABEL: text
- Write exactly what was said, word for word, including filler words (um, uh, like, you know) and false starts. Do not clean up grammar. These transcripts are quoted in coaching reports, so accuracy matters more than readability.
- Add bracketed delivery notes inline only where you actually hear them: [long pause 4s], [interrupts], [talking over], [rushed], [monotone], [warm tone], [upswing on statement], [laughs], [sighs], [mumbled], [inaudible].
- After the transcript add a section exactly like:
DELIVERY SUMMARY:
- pace: (slow / conversational / rushed) plus one sentence about the agent
- energy and tone: one or two sentences on the agent specifically
- talk balance: rough percent agent vs seller
- filler words: rough count of the agent's um/uh/like/you know
- notable audio moments: up to 3 bullets with timestamps (mm:ss)
Output plain text only, no markdown fences."""

TRIAGE_PROMPT = """You are sorting a transcribed call from Tulsa Homebuyers (a local home buyer) for coaching.
The same person often makes both cold calls and follow-up calls, so decide ONLY from what is said.

Return STRICT JSON only, no fences, with these keys:
  pipeline: one of cold_call | lead_management | combined | closing | not_gradeable
    cold_call = first touch with the owner: the agent is introducing the reason for the call ("calling about the property on...", "any plans for it?"). No sign of an earlier conversation with this seller. Captures early motivation at most and books a callback or ends.
    lead_management = follow-up or qualification of an existing lead: refers to an earlier conversation, text, or callback, then digs into motivation, timeline, condition, price, roadblocks, and books a next call. No offer number from us.
    combined = a FIRST touch (no sign of an earlier conversation) where the agent, on this same call, goes on into full qualification: digs into the pillars (motivation, timeline, condition, price), roadblocks, or a next step toward an offer, instead of only booking a callback. No offer number from us.
    closing = the agent presents or negotiates an offer, discusses our price or terms (cash or creative/seller-finance terms), renegotiates, or walks through or signs a contract.
    not_gradeable = no live two-way conversation with the owner or a decision-maker.
  not_gradeable_reason: null, or one of voicemail | wrong_number | no_answer | dead_air | dropped | non_decision_maker | not_a_seller_call
    (not_a_seller_call = vendor, personal, buyer, or internal call)
  report_type: "full" or "short" or null
    short = the seller DECLINED ("no", "not interested", "not for sale", opt-out, or hung up) within roughly the first 30 seconds with no real property conversation beyond the decline. Only cold_call and lead_management can be short (a combined or closing call is always full). Anything with a callback set, a deferral ("call me next week"), or engagement beyond the decline is full. When unsure, full. null when not_gradeable.
  outcome: short phrase, e.g. "booked follow-up", "callback", "not now", "no", "opt-out", "offer made", "contract signed", "voicemail"
  switch_at: for combined only, the first AGENT line (word for word) where the agent moves from the cold-call job into deeper qualification, with its mm:ss if you can tell, e.g. "02:14 Do you have about 10 minutes right now?"; null otherwise
  labels_swapped: true if the AGENT-labeled speaker is actually the owner and SELLER is our agent (decide by content)
  seller_name: string or null
  property_mentioned: street mentioned on the call, or null
  summary: one plain sentence
TRANSCRIPT:
"""


def transcribe_audio(mp3: Path, agent_name: str) -> str:
    b64 = base64.b64encode(mp3.read_bytes()).decode()
    hint = f" (our agent's name is {agent_name})" if agent_name and agent_name != "Unknown" else ""
    return openrouter([{"role": "user", "content": [
        {"type": "text", "text": TRANSCRIBE_PROMPT.replace("{agent_hint}", hint)},
        {"type": "input_audio", "input_audio": {"data": b64, "format": "mp3"}},
    ]}])


def triage(transcript: str) -> dict:
    raw = openrouter([{"role": "user", "content": TRIAGE_PROMPT + transcript[:24000]}], timeout=90)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"pipeline": "not_gradeable", "not_gradeable_reason": "triage_failed", "summary": raw[:200]}
    return _normalize(json.loads(m.group(0)))


def _normalize(out: dict) -> dict:
    if out.get("pipeline") in ("closing", "combined") and out.get("report_type") != "full":
        out["report_type"] = "full"      # these rubrics have no short report
    if out.get("pipeline") == "not_gradeable":
        out["report_type"] = None
    if out.get("pipeline") != "combined":
        out["switch_at"] = None
    return out


TYPE_ALIASES = {
    "cold": "cold_call", "cold_call": "cold_call", "coldcall": "cold_call",
    "lm": "lead_management", "lead": "lead_management", "lead_management": "lead_management",
    "combined": "combined", "combo": "combined", "both": "combined",
    "closing": "closing", "close": "closing", "closer": "closing",
}


def parse_specs(specs: list[str]) -> tuple[list[str], dict, list[str]]:
    """['CA1:combined', 'CA2'] -> (['CA1', 'CA2'], {'CA1': 'combined'}, bad_types)."""
    ids, types, bad = [], {}, []
    for s in specs:
        cid, _, t = s.partition(":")
        cid, t = cid.strip(), t.strip().lower()
        ids.append(cid)
        if t:
            if t in TYPE_ALIASES:
                types[cid] = TYPE_ALIASES[t]
            else:
                bad.append(s)
    return ids, types, bad


def apply_user_type(tri: dict, user_type: str | None) -> dict:
    """The user's call type always wins. Keeps what the sorting step said."""
    tri["ai_pipeline"] = tri.get("pipeline")
    if not user_type:
        tri["type_source"] = "detected"
        return tri
    tri["type_source"] = "user"
    if tri.get("pipeline") == "not_gradeable":
        log(f"  WARNING: set as {user_type} by the user, but the sorting step saw no live "
            f"conversation ({tri.get('not_gradeable_reason')}). Using the user's type; check the transcript.")
        tri["not_gradeable_reason"] = None
    tri["pipeline"] = user_type
    if tri.get("report_type") is None:
        tri["report_type"] = "full"
    return _normalize(tri)


def swap_labels(text: str) -> str:
    """Fix a transcript whose AGENT/SELLER labels are reversed."""
    return re.sub(r"(?m)^(AGENT|SELLER):",
                  lambda m: "SELLER:" if m.group(1) == "AGENT" else "AGENT:", text)


def write_transcript(c: dict, text: str, tri: dict) -> Path:
    md = TR_DIR / f"{c['call_id']}.md"
    header = "\n".join([
        f"# Call {c['call_id']}",
        f"- caller: {display_name(c.get('caller'))}",
        f"- when: {c.get('created_local')} (Central)",
        f"- direction: {c.get('direction')}",
        f"- duration: {mmss(c.get('duration_seconds'))}",
        f"- property: {c.get('address')}",
        f"- DataSift record: {c.get('record_url')}",
        f"- recording: {c.get('recording_url')}",
        f"- sorted as: {tri.get('pipeline')} / {tri.get('report_type')} / outcome: {tri.get('outcome')}",
        f"- type set by: " + ("the user" + (f" (sorting step said {tri.get('ai_pipeline')})"
                                             if tri.get("ai_pipeline") != tri.get("pipeline") else "")
                              if tri.get("type_source") == "user" else "detected by the sorting step"),
        *([f"- switch at (suggested): {tri.get('switch_at')}"] if tri.get("pipeline") == "combined" else []),
        f"- labels: {'were swapped by the transcriber and have been corrected' if tri.get('labels_swapped') else 'ok'}",
        "", "## Transcript", "",
    ])
    md.write_text(header + text.strip() + "\n", encoding="utf-8")
    return md


def _send(c: dict) -> tuple[str, dict]:
    """The PAID part: two OpenRouter calls for one recording."""
    text = transcribe_audio(Path(c["recording_file"]), display_name(c.get("caller")))
    tri = triage(text)
    if tri.get("labels_swapped"):
        text = swap_labels(text)
    return text, tri


def main() -> int:
    ap = argparse.ArgumentParser(description="Transcribe + sort call recordings (PAID with --commit)")
    ap.add_argument("--commit", action="store_true", help="actually call OpenRouter and spend")
    ap.add_argument("--call-id", help="comma-separated call ids the user named, each optionally "
                    "with :type (cold, lm, combined, closing); REQUIRED with --commit")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--min-seconds", type=int, default=10)
    args = ap.parse_args()
    return run(args.commit, parse_ids(args.call_id), args.force, args.min_seconds)


def run(commit: bool, ids: list[str], force: bool = False, min_seconds: int = 10,
        send=None) -> int:
    """ids: call ids, each optionally 'id:type'. send(call) -> (text, triage)
    does the paid work; tests pass a stub."""
    ids, types, bad = parse_specs(ids)
    if bad:
        log(f"REFUSED: unknown call type in {', '.join(bad)} (use cold, lm, combined or closing). "
            "Nothing was sent.")
        return 3
    if commit and not ids:
        log("REFUSED: --commit needs --call-id with the calls the user named. "
            "Nothing is transcribed without their say-so. Nothing was sent.")
        return 3

    calls = load_json(CALLS_JSON, [])
    if ids:
        known = {c["call_id"] for c in calls}
        unknown = [i for i in ids if i not in known]
        if unknown:
            log(f"REFUSED: not in calls.json: {', '.join(unknown)}. Nothing was sent.")
            return 3
    wanted = set(ids)
    todo, too_short = [], []
    for c in calls:
        if wanted and c["call_id"] not in wanted:
            continue
        if is_excluded(c.get("caller")) or not c.get("recording_file"):
            continue
        if c.get("transcript_file") and not force:
            continue
        if (c.get("duration_seconds") or 0) < min_seconds:
            too_short.append(c)
        else:
            todo.append(c)

    for c in too_short:
        c.update({"pipeline": "not_gradeable", "not_gradeable_reason": "too_short",
                  "report_type": None, "outcome": f"under {min_seconds}s, not transcribed"})
    minutes = sum((c.get("duration_seconds") or 0) for c in todo) / 60
    est = minutes * COST_PER_AUDIO_MIN + len(todo) * COST_PER_TRIAGE
    log(f"{len(todo)} call(s) to transcribe, {minutes:.1f} audio minutes. Estimated cost ${est:.3f} "
        f"(~${COST_PER_AUDIO_MIN}/audio min + ~${COST_PER_TRIAGE}/call sort).")
    if too_short:
        log(f"{len(too_short)} call(s) under {min_seconds}s go to the Connection Log without transcription.")
    if not commit:
        save_json(CALLS_JSON, calls)
        log("DRY RUN: nothing sent. Transcribing needs the user to name the calls: "
            "--commit --call-id <id>,<id>.")
        return 0

    send = send or _send
    TR_DIR.mkdir(parents=True, exist_ok=True)
    errors = 0
    for i, c in enumerate(todo, 1):
        try:
            text, tri = send(c)
            tri = apply_user_type(tri, types.get(c["call_id"]))
            md = write_transcript(c, text, tri)
            save_json(TR_DIR / f"{c['call_id']}.json", tri)
            c.update({"transcribed_at": datetime.now().isoformat(timespec="seconds"),
                      "transcript_file": str(md), "pipeline": tri.get("pipeline"),
                      "type_source": tri.get("type_source"), "switch_at": tri.get("switch_at"),
                      "report_type": tri.get("report_type"),
                      "not_gradeable_reason": tri.get("not_gradeable_reason"),
                      "outcome": tri.get("outcome"), "summary": tri.get("summary")})
            log(f"  [{i}/{len(todo)}] {c['call_id']} {mmss(c.get('duration_seconds'))} -> "
                f"{tri.get('pipeline')} {tri.get('report_type') or ''} ({tri.get('outcome')})")
        except Exception as e:  # noqa: BLE001
            errors += 1
            log(f"  ERROR {c['call_id']}: {e}")
        save_json(CALLS_JSON, calls)     # checkpoint after every call
    log(f"Transcribed {len(todo) - errors} of {len(todo)} ({errors} errors).")
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
