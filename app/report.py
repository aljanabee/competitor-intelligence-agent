"""Cross-company comparison matrix and the final LLM synthesis."""
from __future__ import annotations

from statistics import median
from typing import Any

from app.analyzers.base import Ctx
from app.models import CompanyAnalysis

# (metric id, component, dotted path into ComponentResult.data, higher_is_better)
METRICS: list[tuple[str, str, str, bool]] = [
    ("revenue", "financials", "kpis.revenue", True),
    ("gross_profit", "financials", "kpis.gross_profit", True),
    ("net_profit", "financials", "kpis.net_profit", True),
    ("net_margin", "financials", "kpis.net_margin", True),
    ("gross_profit_growth", "financials", "kpis.gross_profit_growth", True),
    ("revenue_growth", "financials", "kpis.revenue_growth", True),
    ("equity_ratio", "financials", "kpis.equity_ratio", True),
    ("employees", "financials", "kpis.employees", True),
    ("gross_profit_per_employee", "financials", "kpis.gross_profit_per_employee", True),
    ("trustpilot_avg_rating", "trustpilot", "stats.avg_rating", True),
    ("trustpilot_trust_score", "trustpilot", "stats.trust_score", True),
    ("trustpilot_total_reviews", "trustpilot", "stats.total_reviews_on_platform", True),
    ("trustpilot_share_negative", "trustpilot", "stats.share_negative", False),
    ("trustpilot_reply_rate", "trustpilot", "stats.reply_rate", True),
    ("trustpilot_sentiment", "trustpilot", "voice_of_customer.sentiment_score", True),
    ("social_total_posts", "social", "total_posts", True),
    ("social_avg_engagement", "social", "avg_engagement", True),
    ("social_sentiment", "social", "analysis.voice_of_customer.sentiment_score", True),
    ("website_pages", "website", "pages_crawled", True),
]


def _get(d: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(part)
    return d


def comparison_matrix(target: CompanyAnalysis, competitors: list[CompanyAnalysis]) -> dict:
    everyone = [target, *competitors]
    rows: list[dict] = []
    for metric, comp, path, higher in METRICS:
        values: dict[str, float] = {}
        for a in everyone:
            res = getattr(a, comp)
            if res and res.status == "ok":
                v = _get(res.data, path)
                if isinstance(v, (int, float)):
                    values[a.company.name] = v
        if len(values) < 2:
            continue
        ordered = sorted(values.items(), key=lambda kv: kv[1], reverse=higher)
        tv = values.get(target.company.name)
        row: dict = {"metric": metric, "component": comp, "higher_is_better": higher,
                     "values": values, "leader": ordered[0][0]}
        comp_vals = [v for k, v in values.items() if k != target.company.name]
        if tv is not None and comp_vals:
            med = median(comp_vals)
            rank = [k for k, _ in ordered].index(target.company.name) + 1
            better = tv > med if higher else tv < med
            row |= {"target_value": tv, "competitor_median": med, "target_rank": rank, "of": len(values),
                    "target_position": "leader" if rank == 1 else ("above_median" if better else
                                       "laggard" if rank == len(values) else "below_median")}
        rows.append(row)
    summary = {
        "leads_on": [r["metric"] for r in rows if r.get("target_position") == "leader"],
        "lags_on": [r["metric"] for r in rows if r.get("target_position") == "laggard"],
    }
    return {"metrics": rows, "summary": summary}


SYNTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "executive_summary": {"type": "string", "description": "5-8 sentences for a decision maker"},
        "market_overview": {"type": "string"},
        "target_position": {"type": "string", "enum": ["market_leader", "strong_challenger", "mid_pack", "lagging", "unclear"]},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "weaknesses": {"type": "array", "items": {"type": "string"}},
        "opportunities": {"type": "array", "items": {"type": "string"}},
        "threats": {"type": "array", "items": {"type": "string"}},
        "competitor_profiles": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"},
            "threat_level": {"type": "string", "enum": ["low", "medium", "high"]},
            "edge_over_target": {"type": "string"},
            "weakness_to_exploit": {"type": "string"}}, "required": ["name", "threat_level"]}},
        "recommendations": {"type": "array", "items": {"type": "object", "properties": {
            "priority": {"type": "integer", "description": "1 = highest"},
            "action": {"type": "string"},
            "rationale": {"type": "string"},
            "evidence": {"type": "string", "description": "Which data point supports it"}},
            "required": ["priority", "action", "rationale"]}},
        "data_gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["executive_summary", "target_position", "strengths", "weaknesses", "recommendations"],
}


def _compact(a: CompanyAnalysis) -> dict:
    out: dict = {"company": a.company.model_dump(include={"name", "domain", "cvr", "distance_km",
                                                          "industry_text", "employees", "source"})}
    for comp in ("financials", "website", "trustpilot", "social"):
        res = getattr(a, comp)
        if res is None:
            continue
        if res.status != "ok":
            out[comp] = {"status": res.status, "error": res.error}
            continue
        d = dict(res.data)
        d.pop("raw", None)
        d.pop("reports", None)
        d.pop("urls", None)
        if comp == "financials":
            d.get("kpis", {}).pop("history", None)
        out[comp] = d
    return out


async def synthesize(target: CompanyAnalysis, competitors: list[CompanyAnalysis],
                     matrix: dict, ctx: Ctx) -> dict:
    if not ctx.p.llm.enabled:
        return {"status": "unavailable", "reason": ctx.p.llm.unavailable_reason or "LLM not configured"}
    content = {"target": _compact(target), "competitors": [_compact(c) for c in competitors],
               "comparison_matrix": matrix}
    return await ctx.p.llm.structured(
        name="competitive_synthesis",
        instructions=(f"Write a competitive analysis of {target.company.name} against the listed competitors. "
                      "Use the comparison matrix for quantitative claims and the per-company analyses for "
                      "qualitative ones. Recommendations must be concrete and tied to evidence. "
                      "competitor_profiles: exactly one entry per listed competitor, named exactly as in the data "
                      "(no extra or duplicate companies)."),
        content=content, schema=SYNTHESIS_SCHEMA, language=ctx.language)
