#!/usr/bin/env python3
"""Mode B merge — records already SmartSkipped in the CRM (relatives on the message
board, often no full phones on the record). Parse each board for relatives +
relationship + last-4s, match DirectSkip people to them, and emit the same
merged_plan.json shape the writeback consumes. Also emits resolve.json and
existing_phones.json from the pre-pulled records + boards (no re-resolve needed).

Usage: python3 merge_modeb.py <run_dir>
  expects <run_dir>/records.json, boards.json, directskip_responses.json
  writes  <run_dir>/merged_plan.json, resolve.json, existing_phones.json
"""
import json, re, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_smartskip as P
from merge_sources import ds_people, nkey, norm

def main():
    RUN=sys.argv[1]
    recs=json.load(open(os.path.join(RUN,"records.json")))
    boards=json.load(open(os.path.join(RUN,"boards.json")))
    ds=json.load(open(os.path.join(RUN,"directskip_responses.json")))
    ds_by_idx={r["input"]["idx"]:(r.get("parsed") or {}) for r in ds}

    # header keyword -> SmartSkip-style type (fed to parse_smartskip.canon_rel with the name)
    HDR=[("son","child"),("daughter","child"),("child","child"),("brother","sibling"),
         ("sister","sibling"),("sibling","sibling"),("mother","parent"),("father","parent"),
         ("parent","parent"),("husband","spouse"),("wife","spouse"),("spouse","spouse"),
         ("in-law","in-law"),("in law","in-law"),("inlaw","in-law")]
    OWNER_HDR=("owner","subject","owners")

    def parse_board(text):
        """-> list of {name, key, last4s:set, rtype, is_owner}."""
        people=[]; cur_type=None; cur_owner=False
        for raw in (text or "").split("\n"):
            line=re.sub(r"\s+"," ",raw).strip()
            if not line: continue
            low=line.lower().rstrip(":")
            # section header?
            if any(low==h or low.startswith(h) for h in OWNER_HDR) and len(low)<12:
                cur_owner=True; cur_type=None; continue
            matched_hdr=None
            for kw,ty in HDR:
                if low==kw or low==kw+"s" or low.startswith(kw):
                    matched_hdr=ty; break
            if matched_hdr and len(line)<20:
                cur_type=matched_hdr; cur_owner=False; continue
            # inline "Name. Relative Parent" / "Name. Subject"
            rtype=cur_type; is_owner=cur_owner
            m_inline=re.search(r"\.\s*(subject|relative\s+(\w+)|owner)", line, re.I)
            if m_inline:
                g=m_inline.group(0).lower()
                if "subject" in g or "owner" in g: is_owner=True; rtype=None
                elif m_inline.group(2):
                    t=m_inline.group(2).lower()
                    rtype={"parent":"parent","child":"child","sibling":"sibling","spouse":"spouse"}.get(t, t if t in ("in-law",) else None)
            # extract name + last4s
            nums={re.sub(r"\D","",x)[-4:] for x in re.findall(r"\d[\d\-]{2,}", line) if len(re.sub(r"\D","",x))>=4}
            namepart=re.split(r"[-\d.]", line)[0]
            namepart=re.sub(r"[^A-Za-z .]"," ",namepart).strip()
            k=nkey(*(namepart.split()[:1]+namepart.split()[-1:])) if len(namepart.split())>=2 else None
            if not k: continue
            people.append({"name":namepart.upper().strip(),"key":k,"last4s":nums,"rtype":rtype,"is_owner":is_owner})
        # merge dup keys
        merged={}
        for p in people:
            e=merged.setdefault(p["key"],p)
            e["last4s"]|=p["last4s"]; e["is_owner"]=e["is_owner"] or p["is_owner"]
            if not e.get("rtype"): e["rtype"]=p["rtype"]
        return list(merged.values())

    plan=[]; resolve={}; existing={}
    for i,rec in enumerate(recs):
        resolve[str(i)]={"uuid":rec["uuid"],"owner":rec["owner"],"addr":rec["property_address"],"subject":"%s %s"%(rec["first"],rec["last"])}
        existing[str(i)]={"phones":[]}   # these records have ~0 phones on the owner
        # Owner-number identification: DS person matching the owner's name, OR (fallback)
        # a DS number whose last-4 matches an owner last-4 on the board. Everyone else
        # from DirectSkip = generic "Relative" -> "Other Relatives" (DirectSkip has no
        # relationship; the SmartSkip relationship research already lives on the board).
        owner_key=nkey(rec["first"],rec["last"])
        bpeople=parse_board(boards.get(str(i),""))
        owner_l4=set().union(*[bp["last4s"] for bp in bpeople if bp["is_owner"]]) if any(bp["is_owner"] for bp in bpeople) else set()
        dsp=ds_people(ds_by_idx.get(i,{}))
        people=[]
        owner={"name":"%s %s"%(rec["first"],rec["last"]),"canon_rel":"Owner","mailing":"","is_owner":True,"sources":["DirectSkip"],"phones":{}}
        for d in dsp:
            is_owner = (d["key"]==owner_key) or (owner_l4 and {n[-4:] for n in d["nums"]} & owner_l4 and d.get("is_primary"))
            if is_owner:
                for n in d["nums"]: owner["phones"].setdefault(n,["DirectSkip"])
                continue
            nm=("%s %s"%(d["first"],d["last"])).strip()
            people.append({"name":nm,"canon_rel":"Relative","mailing":"","is_owner":False,
                           "sources":["DirectSkip"],"phones":{n:["DirectSkip"] for n in d["nums"]}})
        allp=[owner]+people
        plan.append({"subject":owner["name"],"property_address":rec["property_address"],
                     "property_city":rec["property_city"],"property_state":rec["property_state"],
                     "has_results":any(p["phones"] for p in allp),"people":allp,
                     "ds_only_count":sum(1 for p in people if p["canon_rel"]=="Relative")})

    json.dump(plan, open(os.path.join(RUN,"merged_plan.json"),"w"), indent=2)
    json.dump(resolve, open(os.path.join(RUN,"resolve.json"),"w"), indent=2)
    json.dump(existing, open(os.path.join(RUN,"existing_phones.json"),"w"), indent=2)
    tot=sum(len(p["phones"]) for s in plan for p in s["people"])
    print("records:",len(plan),"| with results:",sum(1 for s in plan if s["has_results"]),
          "| total DirectSkip numbers:",tot)


if __name__=='__main__':
    main()
