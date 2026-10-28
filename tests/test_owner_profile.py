"""Offline tests for owner_profile (2026-10-27). Every source is stubbed;
nothing touches the county sites, DataSift or the web.
Run: python tests/test_owner_profile.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import owner_profile as op  # noqa: E402

failures = 0


def check(name, cond):
    global failures
    print(("PASS " if cond else "FAIL ") + name)
    failures += not cond


# ── Assessor Info page (shape copied from a live page, 2026-10-27; values fictional) ──
INFO = """
<th>Owner Mailing Address</th>
<td>4410 E 99TH ST<br>TULSA , OK 74136</td>
<h5>Values and Tax Information</h5><table><tr><th></th><th>2024</th><th>2025</th><th>2026</th></tr>
<tr><td>Fair Cash(Market) Value</td><td>$183,421</td><td>$216,100</td><td>$215,200</td></tr>
<tr><td>Total Taxable Value (Capped)</td><td>$131,895</td><td>$135,852</td><td>$139,928</td></tr>
<tr><td>Gross Assessed Value</td><td>$14,509</td><td>$14,943</td><td>$15,392</td></tr>
<tr><td>Exemptions</td><td>$0</td><td>$0</td><td>$0</td></tr>
<tr><td>Net Assessed Value</td><td>$0</td><td>$0</td><td>$0</td></tr></table>
<h5>Exemptions Claimed</h5>
<table class="table table-bordered table-sm table-exemption"><thead><tr><th scope="col"></th>
<th scope="col">2024</th><th scope="col">2025</th><th scope="col">2026</th></tr></thead><tbody>
<tr><th scope="row"><a href="/Exemption/Homestead">Homestead</a></th>
<td><i class="fas fa-check"></i></td><td><i class="fas fa-check"></i></td><td><i class="fas fa-check"></i></td></tr>
<tr><th scope="row"><a>Additional Homestead</a></th><td>-</td><td>-</td><td>-</td></tr>
<tr><th scope="row"><a>100% Disabled Veteran</a></th>
<td><i class="fas fa-check"></i></td><td><i class="fas fa-check"></i></td><td><i class="fas fa-check"></i></td></tr>
</tbody></table>
"""
v = op.parse_values(INFO)
check("values: years read", v.get("years") == [2024, 2025, 2026])
check("values: taxable and net rows read", v.get("taxable") == [131895, 135852, 139928]
      and v.get("net") == [0, 0, 0])
check("values: mailing address read", v.get("mailing") == "4410 E 99TH ST, TULSA , OK 74136")
check("exemptions claimed: Homestead + 100% DV, Additional Homestead (dashes) left out",
      v["exemptions_claimed"] == {"Homestead": [2024, 2025, 2026],
                                  "100% Disabled Veteran": [2024, 2025, 2026]})
vf = " ".join(op.value_findings(v))
check("findings name both exemptions and what each needs", "own AND live" in vf and "The owner must" in vf
      and "disabled veteran" in vf and "not that the owner is alive today" in vf)
v2 = op.parse_values(INFO.split("<h5>Exemptions Claimed")[0])
vf2 = " ".join(op.value_findings(v2))
check("no exemption table: falls back to the 3% homestead cap and $0 net",
      "3% a year" in vf2 and "FULLY exempt" in vf2)


def fake_search(q):
    return [{"AccountNo": "R1"}]


def fake_situs(acct):
    return {"street": "4410 E 99 ST S", "owner": "FENWICK, PAUL & LINDA ORMSBY"}


def fake_sales(acct):
    return [{"sale_date": "6/3/2010", "grantor": "PRIOR, SELLER",
             "grantee": "FENWICK, PAUL & LINDA ORMSBY", "sale_price": 115000,
             "deed_type": "General Warranty Deed", "document_number": "2010000001"}]


a = op.assessor_section("4410 E 99th St", "Tulsa", "Paul Fenwick", search=fake_search,
                        situs=fake_situs, sales=fake_sales, info_html=lambda acct: INFO)
at = "\n".join(a["findings"])
check("assessor: title holders, deed and mailing reported", "FENWICK, PAUL & LINDA ORMSBY" in at
      and "2010000001" in at and "(the property itself)" in at)
check("assessor: owner on title -> no title caution", "NOT the name on title" not in at)
b = op.assessor_section("4410 E 99th St", "Tulsa", "Dana Kessel", search=fake_search,
                        situs=fake_situs, sales=fake_sales, info_html=lambda acct: INFO)
check("assessor: a person not on title is called out", "NOT the name on title" in "\n".join(b["findings"]))
c = op.assessor_section("4410 E 99th St", "Tulsa", "Paul Fenwick", search=lambda q: [],
                        situs=fake_situs, sales=fake_sales, info_html=lambda acct: INFO)
check("assessor: no account -> 'not checked', never a silent empty", c["status"] == "not checked")
check("street query drops suffix and ordinal", op.street_query("4410 E 99th St") == "4410 E 99")

# ── Treasurer: a link to check by hand (Jeff, 2026-10-28) ────────────
t = op.treasurer_section("Paul Fenwick", "12345678901234")
check("treasurer: never searched, gives a link to check by hand",
      t["status"] == "check by hand" and op.TREASURER_SEARCH_URL in t["findings"][0]
      and "Paul Fenwick" in t["findings"][0] and "12345678901234" in t["findings"][0])

# ── LOCCAT ────────────────────────────────────────────────────────────


def fake_loccat(**k):
    def doc(parcel, grantor, grantee, date):
        return {"verified": True, "properties": {"PARCELNB": parcel, "GRANTOR": grantor,
                                                 "GRANTEE": grantee, "RECORDINGDATE": date,
                                                 "DOCUMENTNB": "D" + date[:4]}}
    return [doc("12345678901234", "PRIOR SELLER", "FENWICK PAUL,ORMSBY LINDA", "06/03/2010"),
            doc("99999999999999", "FENWICK ADA MAE", "FENWICK PAUL JAMES", "08/26/1987"),
            {"verified": False, "properties": {"PARCELNB": "9", "GRANTOR": "JOHN DOE", "GRANTEE": "X"}}]


lc = op.loccat_section("Paul Fenwick", "12345678901234", search=fake_loccat)
lt = "\n".join(lc["findings"])
check("loccat: this parcel's document listed", "This property, recorded 06/03/2010" in lt)
check("loccat: same-name people on other parcels flagged as namesakes",
      "FENWICK PAUL JAMES" in lt and "namesakes" in lt)
check("loccat: unverified loose matches never used", "JOHN DOE" not in lt)

# ── CRM activity ──────────────────────────────────────────────────────
EV = [{"event_type": "owner.call.answered", "timestamp": "2026-10-27 21:10:59",
       "payload": {"call": {"created": "2026-10-27T21:10:59+0000", "origin_number": "2085550101",
                            "destination_number": "9188771187", "direction": "inbound",
                            "duration": 70, "extra": {"disposition": "WRONG"}}}},
      {"event_type": "owner.phone.status.updated", "timestamp": "2026-10-27 21:12:00",
       "payload": {"owner": {"phone": "2085550101", "status": ["NO_ANSWER", "WRONG"]}}},
      {"event_type": "property.message.added", "timestamp": "2026-10-08 10:00:00",
       "author_extra_info": {"first_name": "Diego"}, "payload": {"message": "found out karen is dead"}}]
act = "\n".join(op.summarize_activity(EV))
check("activity: answered call with number, length, inbound and disposition",
      "2085550101: 1 (70s total, 1 inbound)" in act and "WRONG" in act)
check("activity: latest phone status and board text", "Phone status 2085550101: WRONG" in act
      and "Diego: found out karen is dead" in act)
check("activity: empty log says so", op.summarize_activity([]) ==
      ["No calls, texts or board posts in the activity log."])

# ── web ───────────────────────────────────────────────────────────────


def fake_web(q):
    return [{"title": "A Stage Play by Paul Fenwick", "href": "https://y.test/1", "body": "play"},
            {"title": "Paul Fenwick, 70 - MyLife", "href": "https://www.mylife.com/paul-fenwick/1",
             "body": "network includes Linda Ormsby"},
            {"title": "Paul Fenwick obituary", "href": "https://www.legacy.com/x", "body": "Tulsa"}]


w = op.web_section("Paul Fenwick", "4410 E 99th St", "Tulsa", "OK", ["Linda Ormsby"], search=fake_web)
wt = "\n".join(w["findings"])
check("web: a result tied to known family is kept and marked", "names Linda Ormsby" in wt)
check("web: people-search pages are listed, never opened", "people-search listing, not opened" in wt)
check("web: name-only results are dropped (and counted)", "Stage Play" not in wt and "share only the name" in wt)
check("web: obituary sites left to the obituary check", "legacy.com" not in wt)

# ── identity ──────────────────────────────────────────────────────────
ib = op.identity_before("Paul Fenwick", ["9185550103", "2085550101", "3035550104"], "OK")
check("identity before: out-of-state numbers are noted only, not called the wrong person",
      "2 of 3" in ib["findings"][0] and "noted only" in ib["findings"][0]
      and "different person" not in ib["findings"][0])
check("identity before: all in-state reads clean",
      "all in-state" in op.identity_before("A B", ["9185551212"], "OK")["findings"][0])
# Replay of the 2026-10-27 same-name incident: the owner shares an out-of-state mobile with an out-of-state
# "son" of the same name.
REC = {"first": "PAUL", "last": "FENWICK",
       "subject_phones": [{"number": "9185550103"}, {"number": "2085550101"}],
       "relatives": [{"name": "PAUL FENWICK", "canon_rel": "Child", "mailing_city": "BOISE",
                      "mailing_state": "ID", "phones": [{"number": "2085550101"}, {"number": "2085550102"}]},
                     {"name": "MARLA QUINCE", "canon_rel": "In-law", "mailing_city": "MERIDIAN",
                      "mailing_state": "ID", "phones": []},
                     {"name": "LINDA ORMSBY", "canon_rel": "Relative", "mailing_city": "TULSA",
                      "mailing_state": "OK", "phones": []}]}
# Mix-up levels (Jeff, 2026-10-28): strong = shared number with a same-name
# person in another state; maybe = same name in another state, nothing shared;
# out-of-state numbers or relatives on their own are never flagged.
f = op.identity_findings(REC)
check("strong: shared number with a same-name out-of-state relative",
      [x["level"] for x in f] == ["strong"] and "208-555-0101" in f[0]["text"] and "Boise, ID" in f[0]["text"])
check("out-of-state relative with a different name is not flagged",
      not any("Marla" in x["text"] for x in f))
REC_MAYBE = dict(REC, relatives=[dict(REC["relatives"][0], phones=[{"number": "2085550102"}])])
fm = op.identity_findings(REC_MAYBE)
check("maybe: same-name relative in another state, no shared number", [x["level"] for x in fm] == ["maybe"])
REC_PLAIN = {"first": "PAUL", "last": "FENWICK",
             "subject_phones": [{"number": "9185550103"}, {"number": "3035550104"}],
             "relatives": [{"name": "DANA FENWICK", "canon_rel": "Child", "mailing_city": "DALLAS",
                            "mailing_state": "TX", "phones": [{"number": "2145550105"}]},
                           {"name": "PAUL FENWICK", "canon_rel": "Child", "mailing_city": "TULSA",
                            "mailing_state": "OK", "phones": [{"number": "9185550103"}]}]}
check("no flag: owner's out-of-state number, an out-of-state child, an in-state same-name son",
      op.identity_findings(REC_PLAIN) == [])
check("profile text: no flags reads as no sign of a mix-up",
      "No sign" in op.identity_after_smartskip(REC_PLAIN)[0])
check("profile text: strong finding labelled STRONG",
      op.identity_after_smartskip(REC)[0].startswith("STRONG: "))
check("note: nothing to say -> empty", op.mixup_note([]) == "")
check("note: strong carries Jeff's call",
      op.mixup_note(f, "Jeff: skip the 208 numbers").startswith("POSSIBLE MIX-UP (likely): ")
      and "Jeff's call: Jeff: skip the 208 numbers" in op.mixup_note(f, "Jeff: skip the 208 numbers"))
check("note: maybe says scoring went ahead as usual",
      op.mixup_note(fm).startswith("POSSIBLE MIX-UP (maybe): ") and "Scored as usual" in op.mixup_note(fm))

rep = op.report({"person": "Paul Fenwick", "street": "4410 E 99th St", "city": "Tulsa",
                 "sections": [a, t, lc]})
check("report: one heading per source and says OSCN is not searched",
      rep.count("\n== ") == 3 and "OSCN" in rep)

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
