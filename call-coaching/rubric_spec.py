"""rubric_spec.py - the machine-checkable shape of the three Tulsa Homebuyers rubrics.

Mirrors rubrics/cold_call.md, lead_management.md and closing.md: criterion ids,
short names, category weights, where N/A is allowed, and the short-call rules.
If a rubric file changes its criteria or weights, change this file in the same
commit or the checker will reject correct reports.
"""
from __future__ import annotations

RUBRICS = {
    "cold_call": {
        "file": "cold_call.md",
        "title": "Cold Call",
        "weights": {"1": 15, "2": 30, "3": 20, "4": 15, "5": 20},
        "categories": {"1": "Opener and Decision-Maker Confirmation", "2": "Four Pillars Discovery",
                       "3": "Objection Handling and List Sensitivity", "4": "Tonality and Call Control",
                       "5": "Close and Next Step"},
        "criteria": {
            "1.1": "Decision-maker confirmed first", "1.2": "Soft plans frame with specific property",
            "1.3": "Awkwardness named", "1.4": "Escalation discipline",
            "2.1": "Motivation asked soft, then mined", "2.2": "Timeline pinned",
            "2.3": "Condition earned and non-judgmental", "2.4": "Price last, ballpark, max two asks",
            "2.5": "Order kept, flow flexible",
            "3.1": "How did you get my info", "3.2": "List-type do-not-say discipline",
            "3.3": "Converting the no", "3.4": "Delay and busy handled small",
            "3.5": "Address earned, not demanded",
            "4.1": "Silence honored", "4.2": "Human, not script-reading",
            "4.3": "Mirroring and active listening", "4.4": "Calm and scarce, never desperate",
            "4.5": "Filler-word discipline",
            "5.1": "Time and Agenda set", "5.2": "Result and Permission given",
            "5.3": "Deal killers surfaced", "5.4": "Specific either-or booking with partner intro",
            "5.5": "Role discipline: no closing on call one",
        },
        # Cold call: N/A whenever the seller made a criterion unreachable or the
        # transcript lacks the signal. Any criterion may be N/A, with a reason.
        "na_any": True,
        "na_allowed": set(),
        "never_na": set(),
        "short": {"opener": ["1.1", "1.2", "1.3", "1.4"], "conversion": "3.3"},
    },
    "lead_management": {
        "file": "lead_management.md",
        "title": "Lead Management",
        "weights": {"1": 10, "2": 30, "3": 20, "4": 20, "5": 20},
        "categories": {"1": "Opening and Continuity", "2": "Four Pillars Qualification",
                       "3": "Roadblocks and Logistics", "4": "Rapport, Tonality, Call Control",
                       "5": "Next Action and Handoff"},
        "criteria": {
            "1.1": "Continuity opener", "1.2": "Expectation setting at the open",
            "1.3": "Decision-maker confirmation", "1.4": "New-information re-contact",
            "1.5": "Revival toolset and ghost diagnosis",
            "2.1": "Motivation: the reason behind the trigger", "2.2": "Timeline to agreement, bucketed",
            "2.3": "Condition: three-bucket grade, tour, capex",
            "2.4": "Price: pocket number pulled, reflected, never negotiated",
            "2.5": "Pillar order and flow discipline",
            "3.1": "Influencers", "3.2": "Logistics and after the sale",
            "3.3": "Encumbrances, title, and agent exposure", "3.4": "Occupancy and junk",
            "4.1": "Question, then silence", "4.2": "Mirroring and reflection",
            "4.3": "Empathy beats and non-judgment", "4.4": "Let them rant, keep control",
            "4.5": "The no ladder and objection handling",
            "5.1": "Binary outcome rule", "5.2": "Specific next step with expectations set",
            "5.3": "Cadence match to pillar evidence",
            "5.4": "Handoff readiness and warm transfer", "5.5": "Human details drawn out",
        },
        # LM: N/A only where the rubric explicitly allows it. A confirmed
        # non-decision-maker call may also null out categories 2, 3 and 4.
        "na_any": False,
        "na_allowed": {"1.4", "1.5", "5.4"},
        "non_dm_categories": {"2", "3", "4"},
        "never_na": set(),
        "short": {"opener": ["1.1", "1.2", "1.3", "1.4", "1.5"], "conversion": "4.5"},
    },
    "closing": {
        "file": "closing.md",
        "title": "Closing",
        "weights": {"1": 15, "2": 25, "3": 20, "4": 25, "5": 10, "6": 5},
        "categories": {"1": "Handoff Open + Discovery Deepening", "2": "Money Conversation + Offer",
                       "3": "Objection Handling + Negotiation", "4": "Commitment Locking + Next Steps",
                       "5": "Tonality + Human Delivery", "6": "ABL Discipline + Offer Gating"},
        "criteria": {
            "1.1": "Confirmation open, never re-asks the story",
            "1.2": "Restates stakes and removes a roadblock", "1.3": "Timeline with under-promise language",
            "1.4": "Condition covered before any adjusted number", "1.5": "Deal killers covered before the offer",
            "2.1": "Seller's number and pocket number", "2.2": "Presents our offer options as a choice",
            "2.3": "Uses contrast as the anchor", "2.4": "Works inside the seller's numbers",
            "2.5": "Pre-negotiates flex, reframes to net", "2.6": "Transparency about our intentions",
            "3.1": "Diagnoses the fear", "3.2": "Deploys the correct framework",
            "3.3": "Best-Price Ask, once only", "3.4": "Third-party objections to momentum",
            "3.5": "Terms pivot as a tool", "3.6": "Renegotiation by the book",
            "4.1": "Moves to signature the instant the seller picks",
            "4.2": "Contract walkthrough with Oklahoma disclosures",
            "4.3": "Firm either-or callback", "4.4": "Narrates process and mechanics",
            "5.1": "Calm, confident, never desperate", "5.2": "Silence and pauses honored",
            "5.3": "Empathy beats", "5.4": "Human delivery, mirroring, avatar matching",
            "6.1": "Always Be Leaving posture", "6.2": "No number thrown at a shopper",
        },
        # Closer: N/A only if the situation never arose. 2.6 is ALWAYS scored.
        "na_any": True,
        "na_allowed": set(),
        "never_na": {"2.6"},
        "short": None,          # the closer rubric has no short report
    },
}

BANDS = [(90, "Elite"), (75, "Strong"), (60, "Developing"), (40, "Needs Work"), (0, "Retrain")]
TIE_POINTS = 5.0            # totals within 5 points are treated as equal


def band_for(total: float) -> str:
    for floor, name in BANDS:
        if total >= floor:
            return name
    return "Retrain"


def category_of(cid: str) -> str:
    return cid.split(".")[0]


def compute(pipeline: str, criteria: dict) -> tuple[dict, float | None]:
    """Category averages (non-N/A) and the weighted total with N/A redistribution."""
    spec = RUBRICS[pipeline]
    cats = {}
    for cat in spec["weights"]:
        vals = [v for k, v in criteria.items() if category_of(k) == cat and v is not None]
        cats[cat] = round(sum(vals) / len(vals), 2) if vals else None
    live = {c: w for c, w in spec["weights"].items() if cats[c] is not None}
    if not live:
        return cats, None
    wsum = sum(live.values())
    total = sum((cats[c] / 5) * w for c, w in live.items()) * 100 / wsum
    return cats, round(total, 1)
