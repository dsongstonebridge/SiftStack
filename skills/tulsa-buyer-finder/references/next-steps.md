# After the list: finding the people behind the LLCs

Do these only when Jeff asks. Free first. ASK before every paid step, every time, with the count and
the cost. Never assume an earlier yes covers a new step.

1. **Free - SiftMap reverse address ("the Harper move").** Look up the buyer's MAILING address in
   SiftMap (autocomplete -> detail -> `owner_info.owner_name`). If the mailing address is a house, its
   owner is often the person behind the LLC. Only count it as a person if the name has no company word
   in it. Pattern: `unmask` in SiftStack `src/dispo_buyers.py` (read it, do not edit Ty's files).
   Measured on Knox: about 40% of LLCs resolved this way at $0.
2. **Free, manual - Oklahoma Secretary of State business search** for officers and the registered
   agent. A registered agent that is a law firm or "registered agent service" is NOT the owner.
3. **Paid (~$0.10 per hit), ask first - Enformion BusinessV2 officers.** It searches company names
   nationally and cannot be limited by state, so keep only officers with an Oklahoma address; an
   out-of-state officer is usually a different company with the same name. An "agent" title counts
   only if their name appears in the company name.
4. **Paid, ask first - skip trace the resolved person** at THEIR address, not the company's
   (Tracerfy ~$0.02/record, DataSift ~$0.12/owner), then Trestle scoring (~$0.015/number; the count
   cannot be predicted before the trace). Checking whether the CRM already has their phones is a CRM
   read: ask first.

Reaching out: Jeff's dispo copy rules live in the `dispo-deal-blast` skill (no contract price, no
exact address over text unless he chooses to disclose it).
