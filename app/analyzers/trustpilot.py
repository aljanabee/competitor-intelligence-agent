"""Trustpilot reviews: rating statistics + voice-of-customer themes."""
from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from statistics import mean

from app.analyzers.base import VOC_SCHEMA, Ctx, parse_date, to_num
from app.models import Company, ComponentResult


def review_stats(reviews: list[dict], today: date | None = None) -> dict:
    today = today or date.today()
    ratings = [int(to_num(r["rating"])) for r in reviews if r.get("rating") is not None]
    if not ratings:
        return {"review_count_scraped": len(reviews)}
    dist = Counter(ratings)
    dated = [(parse_date(r.get("date")), int(to_num(r["rating"]))) for r in reviews if r.get("rating") is not None]
    recent = [s for d, s in dated if d and d >= today - timedelta(days=90)]
    older = [s for d, s in dated if d and d < today - timedelta(days=90)]
    first = next((r for r in reviews if r.get("trust_score") is not None), {})
    return {
        "review_count_scraped": len(reviews),
        "total_reviews_on_platform": first.get("total_reviews"),
        "trust_score": first.get("trust_score"),
        "avg_rating": round(mean(ratings), 2),
        "distribution": {str(k): dist.get(k, 0) for k in range(1, 6)},
        "share_negative": round(sum(1 for s in ratings if s <= 2) / len(ratings), 3),
        "share_positive": round(sum(1 for s in ratings if s >= 4) / len(ratings), 3),
        "reply_rate": round(sum(1 for r in reviews if r.get("reply")) / len(reviews), 3),
        "avg_rating_last_90d": round(mean(recent), 2) if recent else None,
        "avg_rating_before": round(mean(older), 2) if older else None,
        "reviews_last_90d": len(recent),
    }


async def analyze(c: Company, ctx: Ctx) -> ComponentResult:
    if not c.domain and not c.trustpilot_url:
        return ComponentResult(status="not_found", error="No domain or Trustpilot URL")
    if not ctx.p.apify.enabled:
        return ComponentResult(status="skipped", error="APIFY_TOKEN not configured")
    domain = c.domain or c.trustpilot_url.rstrip("/").rsplit("/", 1)[-1]
    url = c.trustpilot_url or f"https://dk.trustpilot.com/review/{domain}"
    reviews = await ctx.p.apify.run("trustpilot", url=url, domain=domain, limit=ctx.opts.max_reviews)
    raw_count = len(reviews)
    sample_keys = reviews[0].get("_keys") if reviews else None
    reviews = [r for r in reviews if r.get("text") or r.get("rating") is not None]
    if not reviews:
        detail = (f"actor returned {raw_count} items but none had rating/text; fields seen: {sample_keys}"
                  if raw_count else "actor returned no items")
        return ComponentResult(status="not_found", error=f"No Trustpilot reviews found ({url}): {detail}")
    for r in reviews:
        r.pop("_keys", None)

    data: dict = {"url": url, "stats": review_stats(reviews)}
    if ctx.p.llm.enabled:
        sample = [{"stars": r.get("rating"), "date": r.get("date"), "title": r.get("title"),
                   "text": (r.get("text") or "")[:800], "company_replied": bool(r.get("reply"))}
                  for r in reviews]
        data["voice_of_customer"] = await ctx.p.llm.structured(
            name="review_analysis",
            instructions=(f"Analyse these Trustpilot reviews of {c.name}. Identify overall perception, "
                          "customer satisfaction, complaints/recurring issues and positive feedback/success stories."),
            content=sample, schema=VOC_SCHEMA, language=ctx.language)
    if ctx.opts.include_raw:
        data["raw"] = reviews
    return ComponentResult(data=data)
