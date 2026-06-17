"""Reply intent — turn a lead's last reply into a buying-intent signal.

last_inbound_text is the strongest *reliable* signal we have (present for ~76% of
resellers / 70% of shops) and Sally previously only used it for drafting, not
scoring. This classifies a reply into an intent bucket and a score adjustment that
feeds the convergence score.

Rule-based and ordered (first match wins), tuned on the real phrasings in the data
but with general keywords so it degrades sensibly on unseen replies. For messy
real-world replies an LLM classifier would generalise this further (the cheap-model
pattern); the rules are exact and explainable for the demo data.

Balance rule: only positive intent lifts a lead; a clear objection lowers it; no
reply is neutral (never a penalty).
"""

from __future__ import annotations

import re

# (bucket, pattern) in priority order — most decisive first.
_RULES: list[tuple[str, re.Pattern]] = [
    ("objection", re.compile(
        r"already on another|already sell|we already|not taking on new|"
        r"not interested|no thanks|don'?t need", re.I)),
    ("deferral", re.compile(
        r"maybe (next|later)|think about it|too busy|busy this|next month|"
        r"try later|not right now|circle back later", re.I)),
    ("buying", re.compile(
        r"send (me )?(pricing|the bundle|details|over)|bundle list|how much|"
        r"one[ -]?pager|drop details|\bkeen\b|interested|sounds good|let'?s do", re.I)),
    ("scheduling", re.compile(
        r"\bcall\b|pop in|happy to chat|when can we (talk|chat)|mornings|"
        r"\b(mon|tues|wednes|thurs|fri)day\b|book a", re.I)),
    ("qualifying", re.compile(
        r"commission|fee|catch|\bship\b|payout|what brands|menswear|how does|"
        r"how do|\?", re.I)),
]

# score adjustment applied to a lead's rank (additive, then clamped 0..1)
INTENT_ADJ = {
    "buying": 0.25, "scheduling": 0.20, "qualifying": 0.10,
    "none": 0.0, "deferral": -0.10, "objection": -0.35,
}

# short human label for the UI / reason
INTENT_LABEL = {
    "buying": "buying intent", "scheduling": "wants to talk", "qualifying": "asking questions",
    "deferral": "soft no / later", "objection": "objection", "none": "no reply yet",
}


def classify_intent(text) -> tuple[str, float]:
    """Return (bucket, score_adjustment) for a reply."""
    if text is None:
        return "none", 0.0
    s = str(text).strip()
    if s == "" or s.lower() in {"nan", "nat", "none"}:
        return "none", 0.0
    for bucket, pattern in _RULES:
        if pattern.search(s):
            return bucket, INTENT_ADJ[bucket]
    return "none", 0.0
