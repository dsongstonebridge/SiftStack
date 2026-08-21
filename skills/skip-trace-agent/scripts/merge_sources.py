#!/usr/bin/env python3
"""Stage 2-3 (merge half) — combine SmartSkip (parsed) + DirectSkip responses per subject.

Per subject, produce a unified people list where each PERSON carries:
  - name, relationship-to-subject (canonical tag), mailing (SmartSkip only), phones[]
  - each phone: number + which SOURCE(s) found it (SmartSkip / DirectSkip / both)
  - each person: whether SmartSkip / DirectSkip / both surfaced them

Matching (SmartSkip relative  <->  DirectSkip person):
  - by name key (first token + last token, upper)  OR  shared phone last-4
  DirectSkip people that match no SmartSkip relative AND are not the owner become
  "DirectSkip-only" -> they land in the message board's "Other Relatives" section.

DirectSkip noise controls (mirrors the existing skill):
  - drop absurd ages (>100)
  - AB2 result_code relatives are looser; the name/last-4 gate still filters wrong-household
This does NOT touch the CRM. Output: merged plan per subject (json / preview).
"""
import json, re, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_smartskip as P

def norm(p):
    d=re.sub(r"\D","",p or "")
    if len(d)==11 and d.startswith("1"): d=d[1:]
    return d if len(d)==10 else None

def nkey(first,last):
    toks=[t for t in re.split(r"[ .]+", ("%s %s"%(first or "",last or "")).upper()) if len(t)>1]
    return (toks[0],toks[-1]) if len(toks)>=2 else None

def absurd(age):
    try: return int(str(age).strip())>100
    except: return False

def ds_people(parsed):
    """DirectSkip contacts+relatives -> list of {first,last,key,age,deceased,nums:set,is_primary}.
    DirectSkip echoes the same person across multiple contact blocks, so we DEDUPE by
    name-key here (merging their numbers) - this is a true within-DirectSkip duplicate,
    distinct from the SmartSkip Sr/Jr case we deliberately keep separate."""
    merged={}
    def emit(first,last,age,deceased,phones,is_primary):
        if absurd(age): return
        k=nkey(first,last)
        if not k: return
        nums={norm(ph.get("phonenumber")) for ph in phones}; nums={n for n in nums if n}
        e=merged.get(k)
        if not e:
            e={"first":(first or "").strip(),"last":(last or "").strip(),"key":k,
               "age":str(age or "").strip(),"deceased":str(deceased or "").upper()=="Y",
               "nums":set(),"is_primary":False}
            merged[k]=e
        e["nums"]|=nums
        e["is_primary"]=e["is_primary"] or is_primary
        e["deceased"]=e["deceased"] or (str(deceased or "").upper()=="Y")
    for ci,c in enumerate(parsed.get("contacts") or []):
        nm=(c.get("names") or [{}])[0]
        emit(nm.get("firstname"),nm.get("lastname"),nm.get("age"),nm.get("deceased"),c.get("phones",[]),ci==0)
        for rel in (c.get("relatives") or []):
            parts=(rel.get("name") or "").split()
            f=parts[0] if parts else ""; l=parts[-1] if len(parts)>1 else ""
            emit(f,l,rel.get("age"),"N",rel.get("phones",[]),False)
    return list(merged.values())

def merge_subject(sm_rec, ds_parsed):
    subj_key=nkey(sm_rec["first"],sm_rec["last"])
    subj_surname=subj_key[1] if subj_key else None
    # ---- index SmartSkip relatives ----
    people=[]  # unified people
    sm_last4_owner={n["number"][-4:] for n in sm_rec["subject_phones"]}
    # owner/subject as a person
    owner={"name":("%s %s"%(sm_rec["first"],sm_rec["last"])).strip(),"canon_rel":"Owner",
           "mailing":sm_rec.get("mailing_address",""),"is_owner":True,
           "sources":set(),"phones":{}}  # phones: number -> set(sources)
    for ph in sm_rec["subject_phones"]:
        owner["phones"].setdefault(ph["number"],set()).add("SmartSkip"); owner["sources"].add("SmartSkip")
    sm_people=[owner]
    for rl in sm_rec["relatives"]:
        per={"name":rl["name"],"canon_rel":rl["canon_rel"],"type_raw":rl["type_raw"],
             "mailing":", ".join(x for x in [rl["mailing_street"],rl["mailing_city"],rl["mailing_state"]] if x),
             "key":nkey(rl["first"],rl["last"]),"last4s":{p["number"][-4:] for p in rl["phones"]},
             "is_owner":False,"sources":set(),"phones":{},"deceased":rl.get("deceased",False)}
        for p in rl["phones"]:
            per["phones"].setdefault(p["number"],set()).add("SmartSkip"); per["sources"].add("SmartSkip")
        sm_people.append(per)

    # ---- fold DirectSkip people in ----
    assoc_keys={tuple(k) for k in sm_rec.get("associate_keys",[])}   # known neighbors/coworkers = negative filter
    dsp=ds_people(ds_parsed)
    ds_only=[]
    for d in dsp:
        if d["key"] in assoc_keys:
            continue  # DirectSkip re-surfaced a SmartSkip associate (neighbor/coworker) - drop as noise
        # match to owner? (same surname + shared last-4 or same name key)
        matched=None
        for per in sm_people:
            pk=per.get("key") if not per["is_owner"] else subj_key
            share_l4=bool(d["nums"] & ({n[-4:] for n in per["phones"]}))
            if (pk and d["key"]==pk) or (share_l4 and d["nums"]):
                matched=per; break
        if matched:
            for n in d["nums"]:
                matched["phones"].setdefault(n,set()).add("DirectSkip")
            matched["sources"].add("DirectSkip")
        else:
            # DirectSkip-only person -> Other Relatives (no mailing from DirectSkip)
            if not d["nums"] and not d["is_primary"]:
                continue  # skip phoneless DS-only noise
            ds_only.append({"name":("%s %s"%(d["first"],d["last"])).strip(),"canon_rel":"Relative",
                            "mailing":"","key":d["key"],"is_owner":False,"deceased":d["deceased"],
                            "surname_mismatch": bool(subj_surname and d["key"] and d["key"][1]!=subj_surname),
                            "sources":{"DirectSkip"},
                            "phones":{n:{"DirectSkip"} for n in d["nums"]}})
    all_people=sm_people+ds_only
    return {"subject":owner["name"],"property_address":sm_rec["property_address"],
            "property_city":sm_rec["property_city"],"property_state":sm_rec["property_state"],
            "has_results": any(p["phones"] for p in all_people),
            "people":all_people,"ds_only_count":len(ds_only)}

def build(sm_path, ds_path):
    sm=P.parse(sm_path)
    ds=json.load(open(ds_path))
    ds_by_idx={r["input"]["idx"]:(r.get("parsed") or {}) for r in ds}
    return [merge_subject(s, ds_by_idx.get(i,{})) for i,s in enumerate(sm)]

if __name__=="__main__":
    plan=build(sys.argv[1], sys.argv[2])
    # serialize sets
    def ser(o):
        return sorted(o) if isinstance(o,set) else o
    for s in plan:
        for p in s["people"]:
            p["sources"]=sorted(p["sources"]); p["phones"]={k:sorted(v) for k,v in p["phones"].items()}
    json.dump(plan, open(sys.argv[3] if len(sys.argv)>3 else "merged_plan.json","w"), indent=2, default=ser)
    # overlap stats
    ov=sum(1 for s in plan for p in s["people"] for n,src in p["phones"].items() if len(src)>1)
    tot=sum(1 for s in plan for p in s["people"] for n in p["phones"])
    print("subjects:",len(plan),"| total numbers:",tot,"| found by BOTH sources:",ov)
