"""Per-stage wall-clock timing for a pipeline run.

User, 2026-09-25: make each run faster, and measure it - "tell me where the
minutes go". Every stage of `skip-trace [--create]` records how long it took,
and the run ends with one table in the log.

Usage:
    import run_timer
    t = run_timer.start("create (bulk)")
    ...
    run_timer.stop(t)
    run_timer.report()        # logs the table; call once at the end of a run

A stage that runs more than once (e.g. an indexing wait retried) accumulates.
Timing never raises: a missing stop() just leaves that stage out.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_stages: dict[str, list[float]] = {}   # name -> [total_seconds, count], insertion-ordered
_run_start: float | None = None


def reset() -> None:
    global _run_start
    _stages.clear()
    _run_start = time.perf_counter()


def start(name: str) -> tuple[str, float]:
    global _run_start
    if _run_start is None:
        _run_start = time.perf_counter()
    return name, time.perf_counter()


def stop(token: tuple[str, float] | None) -> float:
    if not token:
        return 0.0
    name, t0 = token
    dt = time.perf_counter() - t0
    tot = _stages.setdefault(name, [0.0, 0])
    tot[0] += dt
    tot[1] += 1
    return dt


def stages() -> dict[str, tuple[float, int]]:
    return {k: (v[0], v[1]) for k, v in _stages.items()}


def report() -> str:
    """Log (and return) the per-stage table, slowest first, plus total wall time."""
    if not _stages:
        return ""
    wall = time.perf_counter() - _run_start if _run_start is not None else sum(
        v[0] for v in _stages.values())
    lines = ["RUN TIMING (wall clock)", f"  {'stage':<38}{'seconds':>9}{'runs':>6}{'share':>8}"]
    for name, (secs, n) in sorted(_stages.items(), key=lambda kv: -kv[1][0]):
        share = (secs / wall * 100) if wall else 0
        lines.append(f"  {name:<38}{secs:>9.1f}{n:>6}{share:>7.0f}%")
    lines.append(f"  {'TOTAL (whole run)':<38}{wall:>9.1f}")
    text = "\n".join(lines)
    logger.info("\n%s", text)
    return text
