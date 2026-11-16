from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from app.models import AnalysisOptions
from app.providers import Providers

SOCIAL_PATTERNS = {
    "linkedin": re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/[A-Za-z0-9_\-%.]+", re.I),
    "facebook": re.compile(r"https?://(?:www\.|m\.)?facebook\.com/(?!sharer|share|dialog|plugins|tr\b)[A-Za-z0-9_.\-]+", re.I),
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/(?!p/|explore/)[A-Za-z0-9_.]+", re.I),
}
TRUSTPILOT_PATTERN = re.compile(r"https?://(?:[a-z]{2}\.|www\.)?trustpilot\.com/review/[A-Za-z0-9_.\-]+", re.I)


def find_profile_links(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for platform, pat in SOCIAL_PATTERNS.items():
        m = pat.search(text)
        if m:
            out[platform] = "https://" + m.group(0).rstrip("/.").split("://", 1)[1]
    m = TRUSTPILOT_PATTERN.search(text)
    if m:
        out["trustpilot"] = "https://" + m.group(0).split("://", 1)[1]
    return out


def parse_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):  # epoch s / ms
        ts = v / 1000 if v > 1e11 else v
        return datetime.fromtimestamp(ts, tz=timezone.utc).date()
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def to_num(v: Any) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, list):  # some actors return the list of comments
        return float(len(v))
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


@dataclass
class Ctx:
    p: Providers
    opts: AnalysisOptions
    language: str
    warnings: list[str] = field(default_factory=list)


# Shared "voice of customer" schema - the four things the brief asks for.
VOC_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "overall_perception": {"type": "string", "description": "Overall business performance and perception, 2-4 sentences"},
        "sentiment_score": {"type": "number", "description": "-1 (very negative) to 1 (very positive)"},
        "customer_satisfaction": {"type": "string", "enum": ["very_low", "low", "mixed", "high", "very_high", "insufficient_data"]},
        "customer_satisfaction_summary": {"type": "string", "description": "Why that satisfaction level"},
        "complaints": {"type": "array", "description": "Complaints and recurring issues, most frequent first",
                       "items": {"type": "object", "properties": {
                           "theme": {"type": "string"}, "frequency": {"type": "string", "enum": ["isolated", "occasional", "recurring", "dominant"]},
                           "example": {"type": "string", "description": "short verbatim evidence"}},
                           "required": ["theme", "frequency"]}},
        "positive_feedback": {"type": "array", "description": "Positive feedback and customer success stories",
                              "items": {"type": "object", "properties": {
                                  "theme": {"type": "string"}, "example": {"type": "string"}},
                                  "required": ["theme"]}},
        "business_signals": {"type": "array", "items": {"type": "string"},
                             "description": "Signals on growth, hiring, launches, expansion, trouble etc."},
        "data_quality_note": {"type": "string", "description": "How representative the data is"},
    },
    "required": ["overall_perception", "sentiment_score", "customer_satisfaction", "complaints", "positive_feedback"],
}
