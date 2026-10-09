"""What the assistant handles: analysis, reports and preference administration.

The assistant only handles analysis-related requests. A request is
classified before any model work so off-topic requests are declined without
spending model calls, while report and preference administration and topic
resets stay available. Classification is deterministic and deliberately
coarse: it declines only clear non-analytical tasks; a question about what
data or help is available (data discovery) proceeds, a short follow-up inside
an ongoing investigation counts as steering it, and an ambiguous first
request gets a focused clarification instead of a guess.

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
CLASSIFIER_VERSION = "request-scope/2"


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
