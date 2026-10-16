"""Offline tests for obituary_check.analyze (2026-10-16). Made-up obituary text,
no web. The spouse trap is the case that matters most: an obituary that lists
the owner as a SURVIVOR means the owner is alive.
Run: python tests/test_obituary_check.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import obituary_check as oc  # noqa: E402

failures = 0


def check(name, cond):
    global failures
    print(("PASS " if cond else "FAIL ") + name)
    failures += not cond


OWNER = "Margaret Testerly"

own = {"url": "u1", "title": "Margaret Ann Testerly Obituary (1941 - 2026) - Tulsa, OK",
       "text": "Margaret Ann Testerly, 84, of Tulsa passed away on August 2, 2026. "
               "She is survived by her daughter Susan Testerly and son Mark Testerly. "
               "She was preceded in death by her husband, Robert Testerly. Services will be held."}
spouse = {"url": "u2", "title": "Robert Testerly Obituary - Tulsa, OK",
          "text": "Robert Testerly of Tulsa passed away on March 3, 2025. He is survived by his "
                  "wife of 60 years, Margaret Testerly, and daughter Susan. Services will be held."}
child = {"url": "u3", "title": "Susan Testerly Obituary",
         "text": "Susan Testerly passed away June 1, 2024. She was preceded in death by her "
                 "mother, Margaret Testerly. Services will be held."}
other_place = {"url": "u4", "title": "Margaret Testerly Obituary - Boise, ID",
               "text": "Margaret Testerly of Boise died on May 5, 2020."}
nick = {"url": "u5", "title": "Peggy Testerly Obituary - Tulsa",
        "text": "Peggy Testerly of Tulsa passed away on August 2, 2026."}

r = oc.analyze(own, OWNER, city="Tulsa")
check("owner's own obituary: LIKELY", r["verdict"].startswith("LIKELY THE OWNER"))
check("date of death read from 'passed away on'", r["dod"] == "August 2, 2026")
check("survived-by excerpt captured", "Susan Testerly" in r["survived_by"])
check("preceded-by excerpt captured", "Robert Testerly" in r["preceded_by"])

r = oc.analyze(spouse, OWNER, city="Tulsa", spouse="Robert Testerly")
check("SPOUSE TRAP: owner listed as a survivor -> owner alive",
      r["verdict"].startswith("OWNER IS A SURVIVOR"))

r = oc.analyze(child, OWNER, city="Tulsa")
check("owner preceded a relative in death -> owner died earlier",
      r["verdict"].startswith("OWNER DIED EARLIER"))

r = oc.analyze(other_place, OWNER, city="Tulsa")
check("same name, other city -> only POSSIBLE", r["verdict"].startswith("POSSIBLE"))
check("date read from 'died on'", r["dod"] == "May 5, 2020")

r = oc.analyze(nick, OWNER, city="Tulsa")
check("nickname Peggy matches Margaret", r["verdict"].startswith("LIKELY THE OWNER"))

check("a 'Personal Representative' line is not read as a son",
      not oc.name_in("Mark Testerly", "Personal Representative: Mary Jones"))
check("street match needs house number and street name",
      oc.street_in("123 E Main St", "lived at 123 Main Street") and
      not oc.street_in("123 E Main St", "lived at 456 Main Street"))
check("legacy name-search page is dropped",
      oc._search_page("https://www.legacy.com/obituaries/name/fulton/johnnie")
      and not oc._search_page("https://www.legacy.com/us/obituaries/name/johnnie-fulton-obituary?pid=1"))

res = oc.check(OWNER, city="Tulsa", pages=[other_place, spouse, own])
check("check(): the spouse trap ranks first, ahead of a matching obituary",
      res["summary"].startswith("OWNER IS A SURVIVOR"))
check("check() with no pages: NO OBITUARY FOUND",
      oc.check(OWNER, city="Tulsa", pages=[])["summary"] == "NO OBITUARY FOUND")

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
