"""The daily campaign run: build a batch each morning and let it send.

Until this existed, a day's texts went out because a person built the cohort,
re-laid the schedule and released it by hand. The box received replies and
dispositioned them perfectly overnight, and sent nothing. That is the gap
between a thing you operate and a thing that operates.

Three decisions worth stating, because each one is a guard:

  * **The window is ours, not theirs.** 9am to 6pm Eastern is when the team
    works, so a reply lands while somebody can act on it. The recipient's own
    8am-9pm quiet hours still apply underneath, and a send needs BOTH. A
    California owner is not texted at 6am because Knoxville is open.

  * **One build per day, spread across the whole window.** The batch is laid
    out end to end at 60-180s spacing rather than released in blocks, because
    a burst is what a carrier notices.

  * **It cannot run away.** A cap bounds the day, the touch-dedup means nobody
    is re-sent a message they already got, and every send still passes the
    per-number cap, the sticky sender, suppression and the dial-tier filter.
    If the cohort ever came back wrong, the blast radius is one day's cap.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import campaign, config, escalate, seed, sender_pool, store

log = logging.getLogger(__name__)

try:  # stdlib on 3.9+, and the image has tzdata
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

STATE_KIND = "campaign_run"


def _now_local() -> datetime:
    if ZoneInfo is None:
        return datetime.now(timezone.utc)
    return datetime.now(ZoneInfo(config.CAMPAIGN_TZ))


def within_window(now: datetime = None) -> tuple[bool, str]:
    """Is it a working moment for the team? (ok, why not)"""
    now = now or _now_local()
    days = {int(d) for d in config.CAMPAIGN_DAYS.split(",") if d.strip().isdigit()}
    if days and now.weekday() not in days:
        return False, f"{now:%A} is not a campaign day"
    if now.hour < config.CAMPAIGN_START_HOUR:
        return False, f"before {config.CAMPAIGN_START_HOUR}:00 {config.CAMPAIGN_TZ}"
    if now.hour >= config.CAMPAIGN_END_HOUR:
        return False, f"after {config.CAMPAIGN_END_HOUR}:00 {config.CAMPAIGN_TZ}"
    return True, ""


def already_ran_today() -> bool:
    today = _now_local().strftime("%Y-%m-%d")
    return not store.mark_notified(f"campaign-{today}", STATE_KIND)


def number_overloads() -> dict:
    """{from_number: (planned today incl. already sent, cap)} for every number
    whose queued texts for today would go past its daily cap."""
    today = _now_local().strftime("%Y-%m-%d")
    out = {}
    rows = store._conn().execute(
        "SELECT from_number, count(*) n FROM outbox WHERE status IN ('queued','held') "
        "AND from_number != '' AND substr(not_before,1,10) <= ? GROUP BY from_number", (today,))
    for r in rows:
        n = r["from_number"]
        cap = sender_pool.cap_for(n)
        planned = r["n"] + store.sends_today(n)
        if cap and planned > cap:
            out[n] = (planned, cap)
    return out


def _warn_number_caps() -> None:
    """Jeff, 2026-10-26: if we already KNOW a number will hit its cap today,
    say so at queue time, not after the texts quietly fail to go."""
    over = number_overloads()
    if not over:
        return
    lines = "\n".join(f"  ...{n[-4:]}: {p} planned today, cap {c} ({p - c} would be held)"
                      for n, (p, c) in sorted(over.items()))
    escalate.alert(
        "SMS campaign: a sending number will hit its cap today",
        f"{lines}\n\nThose texts wait in the outbox until tomorrow unless the cap is "
        "raised. Your call: raise the cap, or let them roll.",
        kind="campaign",
    )


def _post_round(rplan, per_touch: dict, held_for_review: bool) -> None:
    """The daily Tier 2 review: what was staged, who has the status but was
    held (and why), who looks eligible without the status, who finished."""
    from . import round2

    if rplan.skipped or rplan.error:
        return
    round2.claim_finished(rplan)
    total = sum(per_touch.values())
    escalate.alert(
        f"SMS Tier 2 round: {total} text(s) staged"
        + (" and HELD for review" if held_for_review and total else ""),
        round2.review_text(rplan, per_touch, held_for_review),
        kind="campaign",
    )


def run(force: bool = False, dry_run: bool = False) -> dict:
    """Build and release one day's batch. Safe to call every worker pass."""
    if not config.CAMPAIGN_ENABLED and not force:
        return {"skipped": "campaign scheduler disabled"}

    ok, why = within_window()
    if not ok and not force:
        return {"skipped": why}

    if not dry_run and already_ran_today():
        return {"skipped": "already ran today"}

    cap = config.CAMPAIGN_DAILY_CAP or sender_pool.capacity_today()["remaining"]
    # Build only as far as the cap. The cohort is larger than a day's pool,
    # so vetting all of it every morning would spend minutes of CRM reads on
    # candidates the cap then throws away.
    plan = campaign.build(limit=cap)
    ready = plan.candidates[:cap]

    # Tier 2 SMS Prospecting (Jeff, 2026-11-13): touches 5-10 for records the
    # team gave the round status. OFF until SMS_AGENT_ROUND_ON=1. A failure
    # here never takes the touches 1-4 run down with it.
    rplan = None
    if config.ROUND_ON:
        from . import round2
        try:
            rplan = round2.build(today=_now_local().date(),
                                 already={c.phone for c in ready})
        except Exception as exc:  # noqa: BLE001
            log.exception("round2 build failed")
            escalate.alert("SMS Tier 2 round: build failed, nothing staged",
                           str(exc)[:300], kind="campaign")
        if rplan and rplan.error:
            escalate.alert("SMS Tier 2 round: not run", rplan.error, kind="campaign")
    round_ready = rplan.candidates if rplan else []

    # NEVER silently short a day (Jeff, 2026-10-26). When the cap stops the
    # build, say how much was left so a person can decide (raise the cap,
    # split the day), instead of finding out from the KPI post.
    if plan.hit_cap or len(plan.candidates) > cap:
        left = "\n  ".join(f"{t}: {n} record(s) not looked at"
                           for t, n in plan.left_unvetted.items()) or "none"
        escalate.alert(
            "SMS campaign: today's cap was hit, some texts will NOT go out",
            f"Capacity today: {cap} texts (pool remaining). {len(ready)} queued.\n"
            f"Cut after the cap: {max(0, len(plan.candidates) - cap)} ready text(s).\n"
            f"Left unvetted:\n  {left}\n\n"
            "Your call: raise SMS_AGENT_DAILY_CAP / SMS_AGENT_DAILY_NUMBER_CAP, "
            "or let the rest go tomorrow.",
            kind="campaign",
        )

    # A source referenced BY NAME that does not resolve is a silent zero, and
    # renaming a preset in DataSift is a normal thing to do. This is the alarm
    # that turns "FTM sent nothing again" into a message on the day it breaks.
    if plan.missing_presets:
        escalate.alert(
            "SMS campaign: a source preset is missing",
            "These are referenced by name and returned nothing:\n  "
            + "\n  ".join(plan.missing_presets)
            + "\n\nCheck the titles in DataSift; a rename breaks the day silently.",
            kind="campaign",
        )

    if plan.board_notes and not dry_run:
        log.info("skip board notes: %s", seed.post_skip_notes(plan.board_notes))

    if rplan and not dry_run and not ready and not round_ready:
        _post_round(rplan, {}, False)
    if not ready and not round_ready:
        # Say so out loud. A quiet morning and a broken cohort query look
        # identical from the outside, and only one of them is fine.
        escalate.alert(
            "SMS campaign: nothing to send today",
            f"Cohort produced no eligible records.\n"
            f"Waiting out the {config.TOUCH_GAP_DAYS}-day gap: {plan.skipped_waiting}. "
            f"Finished all four touches: {plan.skipped_completed}.",
            kind="campaign",
        )
        return {"queued": 0, "reason": "no eligible candidates"}

    if dry_run:
        return {
            "would_queue": len(ready),
            "per_stage": plan.per_stage,
            "holds": plan.holds,
            "round_would_queue": len(round_ready),
        }

    # Queued per touch (that is the seed API), then re-laid as ONE timeline.
    # Queuing four batches independently gave four schedules all starting at
    # "now", which collapsed to a 2-second gap: a burst wearing a schedule.
    queued = 0
    for touch in (1, 2, 3, 4):
        batch = [c for c in ready if getattr(c, "touch", None) == touch]
        if batch:
            queued += seed.queue(batch, touch).get("queued", 0)
    round_per_touch: dict = {}
    for touch in range(5, 11):
        batch = [c for c in round_ready if getattr(c, "touch", None) == touch]
        if batch:
            n = seed.queue(batch, touch).get("queued", 0)
            queued += n
            if n:
                round_per_touch[touch] = n
    laid = seed.reschedule_held()
    released = 0
    for touch in (1, 2, 3, 4):
        released += seed.release(touch)
    if rplan:
        # The first round days stay HELD until Jeff has read the review.
        held_for_review = False
        if round_per_touch:
            from . import round2
            days = round2.note_staged_day(_now_local().date())
            if round2.release_allowed(days):
                for touch in round_per_touch:
                    released += seed.release(touch)
            else:
                held_for_review = True
        _post_round(rplan, round_per_touch, held_for_review)
    _warn_number_caps()
    result = {"queued": queued}

    summary = {
        "queued": result.get("queued", 0),
        "released": released,
        "waiting_gap": plan.skipped_waiting,
        "completed": plan.skipped_completed,
        "duplicate_person": plan.skipped_duplicate_person,
        "unreached_sources": plan.unreached,
        "window": f"{laid.get('first')} - {laid.get('last')} UTC",
        "avg_gap_seconds": laid.get("avg_gap_seconds"),
    }
    log.info("campaign run: %s", summary)

    # Was "touch {info.get('touch')}", and per_stage has never carried a
    # "touch" key, so every one of these lines has read "touch None" since it
    # was written. Cohort -> rows -> sending is what actually answers "why so
    # few", and the top hold reason says where they went.
    stages = "\n".join(
        f"  {title}: {info.get('cohort', 0)} in the preset, {info.get('rows', 0)} usable, "
        f"{info.get('ready', 0)} sending"
        + ("" if info.get("reached") else "  (never reached)")
        + (f"\n      mostly held: {max(info['holds'], key=info['holds'].get)}"
           if info.get("holds") else "")
        for title, info in plan.per_stage.items() if "error" not in info
    )
    unknown_type = sum(1 for c in ready
                       if getattr(c, "line_type_unknown", False))
    escalate.alert(
        f"SMS campaign started: {released} texts today",
        f"{stages}\n\n"
        f"Spread over {laid.get('span_minutes')} minutes, "
        f"{laid.get('avg_gap_seconds')}s average gap, "
        f"{sender_pool.capacity_today()['numbers']} numbers.\n"
        f"{plan.skipped_waiting} waiting out the {config.TOUCH_GAP_DAYS}-day gap, "
        f"{plan.skipped_completed} have finished all four.\n"
        + (f"Held: {', '.join(f'{k} {n}' for k, n in sorted(plan.holds.items(), key=lambda kv: -kv[1])[:5])}.\n"
           if plan.holds else "")
        + (f"Stopped at the cap with {len(plan.unreached)} source(s) still holding rows.\n"
           if plan.hit_cap and plan.unreached else ""),
        kind="campaign",
    )

    # The alarm that would have caught 180/day falling to 19 on the day it
    # happened, instead of a week later when Ty noticed.
    floor = config.CAMPAIGN_MIN_EXPECTED
    if floor and released < floor:
        escalate.alert(
            f"SMS campaign volume is low: {released} texts, expected at least {floor}",
            f"{stages}\n\nHeld: "
            + ", ".join(f"{k} {n}" for k, n in sorted(plan.holds.items(), key=lambda kv: -kv[1])[:6]),
            kind="campaign",
        )
    return summary


# ----------------------------------------------------------- heartbeat

def heartbeat() -> dict:
    """Record that the worker is alive. Read by an external checker.

    Deliberately just a timestamp. Anything clever that runs INSIDE the worker
    cannot report that the worker has stopped, which is the failure this is for:
    the box went down when the Fly trial ended and nothing said a word.
    """
    store.set_meta("last_worker_pass", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return {"ok": True}


def liveness() -> dict:
    """How long since the worker last did a pass."""
    raw = store.get_meta("last_worker_pass")
    if not raw:
        return {"alive": False, "reason": "no worker pass ever recorded"}
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return {"alive": False, "reason": f"unparseable timestamp {raw!r}"}
    age = (datetime.now(timezone.utc) - last).total_seconds() / 60
    return {
        "alive": age <= config.HEARTBEAT_STALE_MINUTES,
        "last_pass": raw,
        "minutes_ago": round(age, 1),
        "stale_after_minutes": config.HEARTBEAT_STALE_MINUTES,
    }
