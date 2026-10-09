"""What the assistant handles: analysis, reports and preference administration.

The assistant only handles analysis-related requests. A request is
classified before any model work so off-topic requests are declined without
spending model calls, while report and preference administration and topic
resets stay available. Classification is deterministic and deliberately
coarse: it declines only clear non-analytical tasks; a question about what
data or help is available (data discovery) proceeds, a short follow-up inside
an ongoing investigation counts as steering it, and an ambiguous first
request gets a focused clarification instead of a guess.

Misspelled analytical words ("revienew of september") are matched tolerantly
against a bounded core of the analytical vocabulary, with length-aware edit
costs so short words and real neighbours ("revenge", "stack", "tends") do
not match; there is no list of accepted misspellings.

Every admission carries a reason code and the classifier version so the
decision can be diagnosed from telemetry without the request text.

Nothing here authorizes anything: an admitted request still goes through
permission, product-scope and privacy checks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from retail_analytics.domain.disclosure import normalize

# Bump when the patterns change, so diagnostics show which rules decided.
CLASSIFIER_VERSION = "request-scope/3"


class RequestTopic(StrEnum):
    ANALYSIS = "analysis"
    # "What data do you have?": answered from the permitted schema.
    DATA_DISCOVERY = "data_discovery"
    REPORT_ADMINISTRATION = "report_administration"
    PREFERENCE_ADMINISTRATION = "preference_administration"
    # Clears the working objective; saved reports and evidence are kept.
    TOPIC_RESET = "topic_reset"
    OFF_TOPIC = "off_topic"
    UNCLEAR = "unclear"


class AdmissionReason(StrEnum):
    """Which rule decided (a code, never the request text)."""

    EMPTY = "empty"
    RESET_PATTERN = "reset_pattern"
    OFF_TOPIC_PATTERN = "off_topic_pattern"
    REPORT_PATTERN = "report_pattern"
    PREFERENCE_PATTERN = "preference_pattern"
    DISCOVERY_PATTERN = "discovery_pattern"
    ANALYSIS_TERMS = "analysis_terms"
    # A misspelled analytical word matched tolerantly ("revienew").
    ANALYSIS_TERMS_TOLERANT = "analysis_terms_tolerant"
    FOLLOW_UP = "follow_up"
    NO_ANALYSIS_TERMS = "no_analysis_terms"


class AdmissionDecision(StrEnum):
    PROCEED = "proceed"
    RESET_TOPIC = "reset_topic"
    DECLINE = "decline"
    CLARIFY = "clarify"


@dataclass(frozen=True, slots=True)
class Admission:
    topic: RequestTopic
    decision: AdmissionDecision
    # User-facing text for DECLINE / CLARIFY / RESET_TOPIC; None to proceed.
    message: str | None = None
    reason: AdmissionReason | None = None
    classifier_version: str = CLASSIFIER_VERSION


DECLINE_MESSAGE = (
    "I can only help with analysis of the sales data you have access to, "
    "your saved reports and your analysis preferences."
)
CLARIFY_MESSAGE = (
    "What would you like to analyze? For example revenue, orders, products, "
    "categories or customer demographics for a period."
)
RESET_MESSAGE = (
    "Starting a new topic. Earlier findings are no longer used as context; "
    "your saved reports are unchanged."
)

_I = re.IGNORECASE
_RESET = re.compile(
    r"^\s*/?(?:reset|new topic|clear context)\s*[.!]?\s*$"
    r"|\b(?:new|different|another|change(?: the)?)\s+(?:topic|subject)\b"
    r"|\b(?:reset|clear)\s+(?:the\s+)?(?:topic|context|conversation)\b"
    r"|\bstart\s+(?:over|fresh|afresh)\b"
    r"|\bforget\s+(?:what|everything)\s+we\s+(?:discussed|talked about)\b",
    _I,
)
_REPORT_ADMIN = re.compile(
    r"\b(?:list|show|open|reopen|find|search|delete|remove|restore|rename|export|"
    r"download|save|read)\b[\w\s']{0,30}\breports?\b"
    r"|\breports?\b[\w\s']{0,20}\b(?:list|delete|deleted|restore|export)\b"
    r"|\bmy\s+reports?\b",
    _I,
)
_PREFERENCE_ADMIN = re.compile(
    r"\b(?:my\s+)?preferences?\b"
    r"|\b(?:remember|forget)\s+(?:that|my|to|this|the)\b"
    r"|\bfrom now on\b|\bby default\b|\bdefault\s+(?:currency|time ?zone|format)\b"
    r"|\bwhat do you (?:remember|know) about me\b",
    _I,
)
_ANALYTICS = re.compile(
    r"\b(?:revenue|sales?|sold|orders?|items?|products?|categor(?:y|ies)|brands?|"
    r"departments?|customers?|buyers?|shoppers?|demographics?|age\s*bands?|"
    r"countr(?:y|ies)|states?|regions?|returns?|returned|cancell?ed|cancellations?|"
    r"margins?|profit|costs?|prices?|discounts?|basket|aov|average order|"
    r"conversion|retention|churn|cohorts?|trends?|growth|declines?|dropped?|"
    r"increase|decrease|compare|comparison|versus|vs\.?|month(?:ly)?|week(?:ly)?|"
    r"quarter(?:ly)?|year(?:ly)?|ytd|mtd|period|kpis?|metrics?|top|bottom|"
    r"best[- ]selling|performance|breakdown|break down|drill|segment|"
    r"inventory|stock|units?|volume|spend|spent|currency|eur|usd|forecast|"
    r"analy[sz]e|analysis|insights?|why|explain|evidence|definition)\b",
    _I,
)
# Questions about what the assistant can see or do. Each alternative needs a
# question frame ("what ... do you have", "what can you ..."), so a bare
# mention of "data" ("export all customer data") does not match.
_DISCOVERY = re.compile(
    r"\bwhat\s+(?:kinds?\s+of\s+|sorts?\s+of\s+|types?\s+of\s+)?"
    r"(?:data|information|datasets?|data\s*sets?|tables?|fields|columns|"
    r"metrics|dimensions|sources?)\b[\w\s']{0,40}?"
    r"\b(?:have|has|got|available|access|see|use|cover|covers|contain|contains|"
    r"there|exist|offer|provide|work\s+with|query|know)\b"
    r"|\bwhat\s+(?:can|could|do|should)\s+(?:you|i)\s+"
    r"(?:do|help|ask|answer|analy[sz]e|tell|look\s+at|explore|query)\b"
    r"|\bwhat\s+(?:questions?|kinds?\s+of\s+questions?|things|analys[ie]s)\s+"
    r"(?:can|could|do|should|would)\s+(?:you|i)\b"
    r"|\bhow\s+(?:can|could|do|would)\s+you\s+help\b"
    r"|\bwhat\s+are\s+you\s+(?:able|capable)\b"
    r"|\bwhat(?:'s|\s+is)\s+(?:in|available\s+in)\s+(?:the|your|this)\s+"
    r"(?:data(?:\s*set)?|database|warehouse|catalog(?:ue)?)\b"
    r"|\b(?:describe|explain|show(?:\s+me)?|list|tell\s+me\s+about|overview\s+of|"
    r"summari[sz]e|walk\s+me\s+through)\s+(?:the\s+|your\s+|my\s+|available\s+|"
    r"all\s+(?:the\s+)?)?(?:data\s*sets?|data\s+(?:model|sources?|available)|"
    r"schema|tables|fields|columns|available\s+data)\b"
    r"|\bwhich\s+(?:data|datasets?|tables|fields|columns)\b[\w\s']{0,30}?"
    r"\b(?:have|available|access|see|use|exist)\b"
    r"|^\W*(?:help|capabilities)\W*$",
    _I,
)
# Clearly non-analytical tasks; they win over incidental data words
# ("write a poem about sales" is still a poem).
_OFF_TOPIC = re.compile(
    r"\b(?:write|compose|tell)\s+(?:me\s+)?(?:a\s+|an\s+)?(?:poem|song|story|joke|"
    r"haiku|limerick|essay|cover letter|tweet)\b"
    r"|\b(?:poem|haiku|limerick|joke|lyrics)\b"
    r"|\bweather\b|\brecipe\b|\bhoroscope\b|\bsports? scores?\b"
    r"|\b(?:who won|capital of|translate|meaning of life)\b"
    r"|\b(?:write|debug|fix)\s+(?:some\s+|this\s+|my\s+|a\s+)?(?:python|java(?:script)?|"
    r"code|program|script|regex)\b"
    r"|\bhack\b|\bpassword\b|\bsystem prompt\b|\byour instructions\b"
    r"|\bignore (?:all |your |the )?(?:previous |prior )?instructions\b",
    _I,
)
_FOLLOW_UP_WORDS = 25

# Core words of ``_ANALYTICS`` that are matched tolerantly. Words shorter than
# five letters (aov, top, why, cost, sale) are only ever matched exactly.
_TOLERANT_VOCABULARY = frozenset(
    {
        "revenue", "sales", "order", "orders", "product", "products",
        "customer", "customers", "category", "categories", "brands",
        "department", "departments", "demographics", "countries", "regions",
        "returns", "returned", "cancelled", "cancellations", "margins", "profit",
        "discounts", "conversion", "retention", "cohorts", "trends",
        "growth", "month", "monthly", "weekly", "quarter", "quarterly",
        "yearly", "period", "metrics", "performance", "breakdown", "inventory",
        "volume", "currency", "forecast", "analyze", "analyse", "analysis",
        "insights", "compare", "comparison", "increase", "decrease", "evidence",
        "definition", "items", "prices", "buyers", "shoppers",
    }
)  # fmt: skip
# "w" and "y" count as vowels: "revienew" sounds out "revenue".
_VOWELS = frozenset("aeiouwy")
_WORD = re.compile(r"[a-z]+")
_MIN_TOLERANT_LENGTH = 5
_MAX_TOLERANT_WORDS = 200
_MAX_LENGTH_GAP = 1


def _budget(target: str) -> float:
    """Total edit cost allowed for ``target``; longer words tolerate more."""
    if len(target) <= 6:
        return 1.0
    return 1.5 if len(target) <= 8 else 2.0


def _edit_cost(typed: str, target: str) -> float:
    """Weighted edit distance (with adjacent transpositions) from typed to target.

    Vowel slips are cheap and consonant changes expensive, because a vowel
    slip keeps the word recognisable while a changed consonant usually makes
    another real word ("revenge"). Short targets only tolerate one dropped
    vowel or one swap of adjacent letters.
    """
    short = len(target) <= 6
    swap = 1.0
    hard = 2.0

    def gap(ch: str, *, dropped: bool) -> float:
        if short:
            return 1.0 if dropped and ch in _VOWELS else hard
        if ch in _VOWELS:
            return 0.5
        return 1.5 if dropped else hard

    def change(a: str, b: str) -> float:
        if a == b:
            return 0.0
        if a in _VOWELS and b in _VOWELS and not short:
            return 0.5
        return hard

    rows, cols = len(typed) + 1, len(target) + 1
    dist = [[0.0] * cols for _ in range(rows)]
    for i in range(1, rows):
        dist[i][0] = dist[i - 1][0] + gap(typed[i - 1], dropped=False)
    for j in range(1, cols):
        dist[0][j] = dist[0][j - 1] + gap(target[j - 1], dropped=True)
    for i in range(1, rows):
        for j in range(1, cols):
            best = min(
                dist[i - 1][j] + gap(typed[i - 1], dropped=False),
                dist[i][j - 1] + gap(target[j - 1], dropped=True),
                dist[i - 1][j - 1] + change(typed[i - 1], target[j - 1]),
            )
            if (
                i > 1
                and j > 1
                and typed[i - 1] == target[j - 2]
                and typed[i - 2] == target[j - 1]
            ):
                best = min(best, dist[i - 2][j - 2] + swap)
            dist[i][j] = best
    return dist[-1][-1]


def _collapse(word: str) -> str:
    """Fold doubled letters ("cancelled", "ordders") before comparing."""
    return re.sub(r"(.)\1+", r"\1", word)


def _near_analytical_word(word: str) -> bool:
    if len(word) < _MIN_TOLERANT_LENGTH - 1:
        return False
    typed = _collapse(word)
    for term in _TOLERANT_VOCABULARY:
        target = _collapse(term)
        if (
            typed[0] == target[0]
            and abs(len(typed) - len(target)) <= _MAX_LENGTH_GAP
            and _edit_cost(typed, target) <= _budget(target)
        ):
            return True
    return False


def _has_misspelled_analytical_word(text: str) -> bool:
    words = _WORD.findall(text.lower())[:_MAX_TOLERANT_WORDS]
    return any(_near_analytical_word(word) for word in words)


def _classify(
    text: str, *, ongoing_investigation: bool
) -> tuple[RequestTopic, AdmissionReason]:
    normalized = normalize(text).strip()
    if not normalized:
        return RequestTopic.UNCLEAR, AdmissionReason.EMPTY
    if _RESET.search(normalized):
        return RequestTopic.TOPIC_RESET, AdmissionReason.RESET_PATTERN
    if _OFF_TOPIC.search(normalized):
        return RequestTopic.OFF_TOPIC, AdmissionReason.OFF_TOPIC_PATTERN
    if _REPORT_ADMIN.search(normalized):
        return RequestTopic.REPORT_ADMINISTRATION, AdmissionReason.REPORT_PATTERN
    if _PREFERENCE_ADMIN.search(normalized):
        return (
            RequestTopic.PREFERENCE_ADMINISTRATION,
            AdmissionReason.PREFERENCE_PATTERN,
        )
    if _DISCOVERY.search(normalized):
        return RequestTopic.DATA_DISCOVERY, AdmissionReason.DISCOVERY_PATTERN
    if _ANALYTICS.search(normalized):
        return RequestTopic.ANALYSIS, AdmissionReason.ANALYSIS_TERMS
    if _has_misspelled_analytical_word(normalized):
        return RequestTopic.ANALYSIS, AdmissionReason.ANALYSIS_TERMS_TOLERANT
    if ongoing_investigation and len(normalized.split()) <= _FOLLOW_UP_WORDS:
        # "And for women?" / "Only last week" steer the current investigation.
        return RequestTopic.ANALYSIS, AdmissionReason.FOLLOW_UP
    return RequestTopic.UNCLEAR, AdmissionReason.NO_ANALYSIS_TERMS


def classify_request(text: str, *, ongoing_investigation: bool) -> RequestTopic:
    """Coarse topic of one user message (see module docstring)."""
    return _classify(text, ongoing_investigation=ongoing_investigation)[0]


def admit(topic: RequestTopic, reason: AdmissionReason | None = None) -> Admission:
    if topic is RequestTopic.OFF_TOPIC:
        return Admission(topic, AdmissionDecision.DECLINE, DECLINE_MESSAGE, reason)
    if topic is RequestTopic.UNCLEAR:
        return Admission(topic, AdmissionDecision.CLARIFY, CLARIFY_MESSAGE, reason)
    if topic is RequestTopic.TOPIC_RESET:
        return Admission(topic, AdmissionDecision.RESET_TOPIC, RESET_MESSAGE, reason)
    return Admission(topic, AdmissionDecision.PROCEED, None, reason)


def assess_request(text: str, *, ongoing_investigation: bool) -> Admission:
    return admit(*_classify(text, ongoing_investigation=ongoing_investigation))
