"""transcribe.py - transcribe recordings with tonality notes, then sort each call.

PAID STEP. Gemini 2.5 Flash via OpenRouter (OPENROUTER_API_KEY in
call-coaching/.env), about $0.003 per audio minute. Without --commit this only
prints what it would do and the estimated cost. Nothing is sent.

Two passes per call, adapted from src/call_coaching/transcribe.py:
  1. AUDIO -> transcript with AGENT / SELLER labels and bracketed delivery
     notes ([long pause 4s], [rushed], [warm tone]...). The model hears the
     call, so tonality notes are real, not guessed from text.
  2. TEXT -> sort: cold_call / lead_management / closing / not_gradeable,
     full vs short report, outcome. Decided by WHAT IS SAID, never by who
     dialed: Diego makes both cold calls and follow-ups.

Calls under --min-seconds (default 10) are not sent: they go straight to the
Connection Log as "too short to be a conversation".

USAGE (from SiftStack root):
  py -3 call-coaching/transcribe.py                 # estimate only, free
  py -3 call-coaching/transcribe.py --commit        # spend and transcribe
  py -3 call-coaching/transcribe.py --commit --call-id CA...   # one call
  py -3 call-coaching/transcribe.py --commit --force            # redo already-transcribed
"""
from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path

from cc_common import (CALLS_JSON, COST_PER_AUDIO_MIN, COST_PER_TRIAGE, TR_DIR, display_name,
                       is_excluded, load_json, log, mmss, openrouter, save_json)

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
  pipeline: one of cold_call | lead_management | closing | not_gradeable
    cold_call = first touch with the owner: the agent is introducing the reason for the call ("calling about the property on...", "any plans for it?"). No sign of an earlier conversation with this seller.
    lead_management = follow-up or qualification of an existing lead: refers to an earlier conversation, text, or callback, or digs into motivation, timeline, condition, price, roadblocks, and books a next call. No offer number from us.
    closing = the agent presents or negotiates an offer, discusses our price or terms (cash or creative/seller-finance terms), renegotiates, or walks through or signs a contract.
    not_gradeable = no live two-way conversation with the owner or a decision-maker.
  not_gradeable_reason: null, or one of voicemail | wrong_number | no_answer | dead_air | dropped | non_decision_maker | not_a_seller_call
    (not_a_seller_call = vendor, personal, buyer, or internal call)
  report_type: "full" or "short" or null
    short = the seller DECLINED ("no", "not interested", "not for sale", opt-out, or hung up) within roughly the first 30 seconds with no real property conversation beyond the decline. Only cold_call and lead_management can be short. Anything with a callback set, a deferral ("call me next week"), or engagement beyond the decline is full. When unsure, full. null when not_gradeable.
  outcome: short phrase, e.g. "booked follow-up", "callback", "not now", "no", "opt-out", "offer made", "contract signed", "voicemail"
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
    out = json.loads(m.group(0))
    if out.get("pipeline") == "closing" and out.get("report_type") == "short":
        out["report_type"] = "full"      # the closer rubric has no short report
    if out.get("pipeline") == "not_gradeable":
        out["report_type"] = None
    return out


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
        f"- labels: {'were swapped by the transcriber and have been corrected' if tri.get('labels_swapped') else 'ok'}",
        "", "## Transcript", "",
    ])
    md.write_text(header + text.strip() + "\n", encoding="utf-8")
    return md


def main() -> int:
    ap = argparse.ArgumentParser(description="Transcribe + sort call recordings (PAID with --commit)")
    ap.add_argument("--commit", action="store_true", help="actually call OpenRouter and spend")
    ap.add_argument("--call-id")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--min-seconds", type=int, default=10)
    args = ap.parse_args()

    calls = load_json(CALLS_JSON, [])
    todo, too_short = [], []
    for c in calls:
        if args.call_id and c["call_id"] != args.call_id:
            continue
        if is_excluded(c.get("caller")) or not c.get("recording_file"):
            continue
        if c.get("transcript_file") and not args.force:
            continue
        if (c.get("duration_seconds") or 0) < args.min_seconds:
            too_short.append(c)
        else:
            todo.append(c)

    for c in too_short:
        c.update({"pipeline": "not_gradeable", "not_gradeable_reason": "too_short",
                  "report_type": None, "outcome": f"under {args.min_seconds}s, not transcribed"})
    minutes = sum((c.get("duration_seconds") or 0) for c in todo) / 60
    est = minutes * COST_PER_AUDIO_MIN + len(todo) * COST_PER_TRIAGE
    log(f"{len(todo)} call(s) to transcribe, {minutes:.1f} audio minutes. Estimated cost ${est:.3f} "
        f"(~${COST_PER_AUDIO_MIN}/audio min + ~${COST_PER_TRIAGE}/call sort).")
    if too_short:
        log(f"{len(too_short)} call(s) under {args.min_seconds}s go to the Connection Log without transcription.")
    if not args.commit:
        save_json(CALLS_JSON, calls)
        log("DRY RUN: nothing sent. Re-run with --commit to transcribe.")
        return 0

    TR_DIR.mkdir(parents=True, exist_ok=True)
    errors = 0
    for i, c in enumerate(todo, 1):
        name = display_name(c.get("caller"))
        try:
            text = transcribe_audio(Path(c["recording_file"]), name)
            tri = triage(text)
            if tri.get("labels_swapped"):
                text = swap_labels(text)
            md = write_transcript(c, text, tri)
            save_json(TR_DIR / f"{c['call_id']}.json", tri)
            c.update({"transcript_file": str(md), "pipeline": tri.get("pipeline"),
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
