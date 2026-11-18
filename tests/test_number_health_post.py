"""Offline test of the daily number-health post (no network, no Slack).

    python tests/test_number_health_post.py
"""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from sms_agent import number_health_post as p  # noqa: E402

p.STATE = Path(tempfile.mkdtemp()) / "state.json"
os.environ["NUMBER_HEALTH_TAG_SLACK_IDS"] = "UERIC,UDIEGO"
posts = []
p.post = lambda text, dry: posts.append(text) or True

FAILS = 0


def check(name, cond):
    global FAILS
    print(("PASS " if cond else "FAIL ") + name)
    FAILS += 0 if cond else 1


def at(h, m=0):
    return dt.datetime(2026, 11, 18, h, m)


T, Y = "2026-11-18", "2026-11-17"
morning = {"9189217981": {"day": T, "flags": [0, 0, 0]},
           "9184770140": {"day": T, "flags": [1, 0, 1]},
           "9183596446": {"day": Y, "flags": [1, 0, 0]}}

check("before 9 AM does nothing", p.run(at(8, 45), fetch=lambda: morning) == "outside 9 AM - 7 PM" and not posts)
r = p.run(at(9), fetch=lambda: morning)
check("9 AM posts and waits", r == "posted, waiting" and len(posts) == 1)
check("tags both people", posts[0].startswith("<@UERIC> <@UDIEGO> *Number health"))
check("marks the stale number", "from Nov 17, today's check not in yet" in posts[0])
check("two carriers = red", ":red_circle: (918) 477-0140  flagged by AT&T, Verizon" in posts[0])
check("promises an update", "post again" in posts[0])
check("15 min later: skipped, hourly", p.run(at(9, 15), fetch=lambda: morning) == "checked within the hour")
check("an hour later, nothing new = quiet", p.run(at(10), fetch=lambda: morning) == "no new checks yet" and len(posts) == 1)
check("unlock at 10:20: skipped", p.run(at(10, 20), fetch=lambda: morning) == "checked within the hour")


def boom():
    raise RuntimeError("down")


check("error after a post stays quiet", p.run(at(11, 30), fetch=boom).startswith("unreadable") and len(posts) == 1)
later = dict(morning, **{"9183596446": {"day": T, "flags": [0, 0, 0]}})
r = p.run(at(13), fetch=lambda: later)
check("update posted when today's check lands", r == "posted update, all current" and len(posts) == 2)
check("update is headed Updated and marks the new one",
      "*Updated number health" in posts[1] and "(918) 359-6446  clean  _(new today)_" in posts[1])
check("done for the day", p.run(at(14), fetch=lambda: later) == "done for today" and len(posts) == 2)
check("after 7 PM does nothing", p.run(at(19, 5), fetch=lambda: later) == "outside 9 AM - 7 PM")

# Next day, NumberVerifier down all morning: one note at 11, then the real post.
nxt = dt.datetime(2026, 11, 19, 9)
check("down at 9: no post", p.run(nxt, fetch=boom).startswith("unreadable") and len(posts) == 2)
p.run(nxt.replace(hour=11), fetch=boom)
p.run(nxt.replace(hour=11, minute=15), fetch=boom)
check("one 'could not read' note at 11", len(posts) == 3 and "Couldn't read" in posts[2])
d19 = {k: {"day": "2026-11-19", "flags": v["flags"]} for k, v in later.items()}
check("first good read posts the snapshot",
      p.run(nxt.replace(hour=12), fetch=lambda: d19) == "posted, all current" and "*Number health, Thu Nov 19*" in posts[3])

print(f"\n{FAILS} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
