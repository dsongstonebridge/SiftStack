#!/usr/bin/env python3
"""Step 1 — Skip trace a CSV of records through the DirectSkip v2 API (resumable).

Sends owner name + mailing address + property address per record (DirectSkip's
"best results" recommendation). Writes results incrementally to <out>l (jsonl) so an
interrupted run can be resumed by re-invoking; consolidates to <out> when complete.

API key resolution order: --api-key > env DIRECTSKIP_API_KEY > config.json next to SKILL.md.
"""
import argparse, csv, json, os, sys, time, ssl
import urllib.request, urllib.error

API_URL = "https://api0.directskip.com/v2/search_contact.php"
NL = chr(10)


def resolve_key(cli):
    if cli:
        return cli
    if os.environ.get("DIRECTSKIP_API_KEY"):
        return os.environ["DIRECTSKIP_API_KEY"]
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "..", "config.json"), os.path.join(here, "config.json")):
        if os.path.exists(cand):
            try:
                return json.load(open(cand)).get("directskip_api_key", "")
            except Exception:
                pass
    return ""


def load_records(csv_path):
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    out = []
    for i, r in enumerate(rows):
        out.append({
            "idx": i,
            "first_name": (r.get("First Name") or "").strip(),
            "last_name": (r.get("Last Name") or "").strip(),
            "mailing_address": (r.get("Mailing address") or "").strip(),
            "mailing_city": (r.get("Mailing city") or "").strip(),
            "mailing_state": (r.get("Mailing state") or "").strip(),
            "mailing_zip": (r.get("Mailing zip5") or r.get("Mailing zip") or "").strip(),
            "property_address": (r.get("Property address") or "").strip(),
            "property_city": (r.get("Property city") or "").strip(),
            "property_state": (r.get("Property state") or "").strip(),
            "property_zip": (r.get("Property zip5") or r.get("Property zip") or "").strip(),
        })
    return out


def call_directskip(api_key, rec, timeout=120):
    payload = {"api_key": api_key,
               "first_name": rec["first_name"], "last_name": rec["last_name"],
               "mailing_address": rec["mailing_address"], "mailing_city": rec["mailing_city"],
               "mailing_state": rec["mailing_state"], "mailing_zip": rec["mailing_zip"],
               "property_address": rec["property_address"], "property_city": rec["property_city"],
               "property_state": rec["property_state"], "property_zip": rec["property_zip"],
               "custom_field_1": str(rec["idx"]), "custom_field_2": "", "custom_field_3": ""}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(API_URL, data=data,
        headers={"Accept": "application/json", "Content-Type": "application/json"}, method="POST")
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


def done_idxs(jsonl_path):
    done = set()
    if os.path.exists(jsonl_path):
        for line in open(jsonl_path, encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    done.add(json.loads(line)["input"]["idx"])
                except Exception:
                    pass
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", default="directskip_responses.json")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--max", type=int, default=0, help="max records this invocation (0=all)")
    ap.add_argument("--sleep", type=float, default=0.4)
    a = ap.parse_args()
    key = resolve_key(a.api_key)
    if not key:
        sys.exit("ERROR: no DirectSkip API key (--api-key / DIRECTSKIP_API_KEY / config.json).")

    records = load_records(a.csv)
    jsonl = a.out + "l"
    done = done_idxs(jsonl)
    todo = [r for r in records if r["idx"] not in done]
    if a.max:
        todo = todo[:a.max]
    print("total=%d done=%d running_now=%d" % (len(records), len(done), len(todo)), flush=True)

    jf = open(jsonl, "a", encoding="utf-8")
    for rec in todo:
        label = "[%d] %s %s - %s" % (rec["idx"] + 1, rec["first_name"], rec["last_name"], rec["mailing_address"])
        try:
            raw = call_directskip(key, rec)
            try:
                parsed = json.loads(raw); err = parsed.get("status", {}).get("error", "")
            except Exception:
                parsed, err = None, "UNPARSEABLE"
            out = {"input": rec, "raw": raw, "parsed": parsed}
            print("OK  %s err=%r bytes=%d" % (label, err, len(raw)), flush=True)
        except Exception as e:
            out = {"input": rec, "raw": None, "error": str(e)}
            print("ERR %s %s" % (label, e), flush=True)
        jf.write(json.dumps(out)); jf.write(NL); jf.flush()
        time.sleep(a.sleep)
    jf.close()

    all_done = done_idxs(jsonl)
    if len(all_done) >= len(records):
        res = [json.loads(l) for l in open(jsonl, encoding="utf-8") if l.strip()]
        res.sort(key=lambda x: x["input"]["idx"])
        json.dump(res, open(a.out, "w"), indent=2)
        print("COMPLETE -> %s (%d records)" % (a.out, len(res)), flush=True)
    else:
        print("PARTIAL %d/%d — re-run to continue" % (len(all_done), len(records)), flush=True)


if __name__ == "__main__":
    main()
