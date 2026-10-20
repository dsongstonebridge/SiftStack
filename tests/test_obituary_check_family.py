"""Offline tests for obituary_check family matching (2026-10-20).

Jeff: an obituary whose survivors name none of the family the probate filing
already established is not the owner's, however well name and city match.
Run: python tests/test_obituary_check_family.py
"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import obituary_check as oc
fails = 0
def check(n, c):
    global fails; print(("PASS " if c else "FAIL ") + n); fails += not c
owner = "Russell Wayne Thomas"
known = ["Rhonda Lee Thomas", "Kimberly Woodie"]
right = {"url": "a", "title": "Russell Wayne Thomas Jr. Obituary - Broken Arrow, OK",
         "text": "Russell Wayne Thomas, Jr., 55, of Tulsa, Oklahoma passed from this life on July 30, 2026, in Tulsa. "
                 "He is survived by his beloved wife, Rhonda Thomas; his daughter, Kimberly; his sister, Rebecca; his brother, William. "
                 "He was preceded in death by his parents."}
wrong = {"url": "b", "title": "Russell Hand Thomas Obituary Jul 19, 2021 - Schaudt Funeral",
         "text": "Russell Hand Thomas of Tulsa died July 19, 2021. Russ is survived by siblings: James Rissler (Maurita), "
                 "Tomi Blackburn, Jack Thomas (Robin), Mark Thomas (Lisa). He was preceded in death by his mother, Jimmy Thomas."}
wrong2 = {"url": "c", "title": "Russell Thomas Obituary - Tulsa",
          "text": "Russell Thomas, 80, of Tulsa died March 2, 2019. He is survived by his wife Gloria and son Mark. "}
r = oc.check(owner, city="Tulsa", known=known, pages=[wrong, right, wrong2])
v = {c["url"]: c["verdict"] for c in r["candidates"]}
check("the real obituary is CONFIRMED by known family", v["a"].startswith("CONFIRMED"))
check("a different middle name is ruled out", v["b"].startswith("DIFFERENT PERSON (middle name Hand"))
check("survivors naming none of the known family -> ruled out", v["c"].startswith("DIFFERENT PERSON (none"))
check("top finding is the confirmed one", r["summary"].startswith("CONFIRMED"))
rep = oc.report(r)
check("ruled-out pages are not listed as matches", "1. DIFFERENT" not in rep and "RULED OUT" in rep)
r2 = oc.check(owner, city="Tulsa", known=[], pages=[wrong2])
check("no known family: falls back to LIKELY and says survivors unchecked",
      r2["summary"].startswith("LIKELY") and "not checked" in r2["summary"])
trap = {"url": "d", "title": "Rhonda Thomas Obituary",
        "text": "Rhonda Thomas of Tulsa passed away May 1, 2026. She is survived by her husband, Russell Wayne Thomas."}
check("spouse trap still caught", oc.check(owner, city="Tulsa", known=known, pages=[trap])["summary"].startswith("OWNER IS A SURVIVOR"))
print(fails, "failure(s)")
sys.exit(1 if fails else 0)
