"""Social media activity (LinkedIn, Facebook, Instagram) + what people say."""
from __future__ import annotations

import asyncio
from datetime import date
from statistics import mean

from app.analyzers.base import VOC_SCHEMA, Ctx, parse_date, to_num
from app.models import Company, ComponentResult

PLATFORM_SCHEMA = {
    "type": "object",
    "properties": {
        "voice_of_customer": VOC_SCHEMA,
        "content_strategy": {"type": "string", "description": "What the company itself posts about and how"},
        "platform_notes": {"type": "object", "additionalProperties": {"type": "string"},
                           "description": "Per-platform observation"},
    },
    "required": ["voice_of_customer", "content_strategy"],
}


def activity_stats(posts: list[dict], today: date | None = None) -> dict:
    today = today or date.today()
    if not posts:
        return {"posts": 0}
    dates = sorted(d for d in (parse_date(p.get("date")) for p in posts) if d)
    likes = [to_num(p.get("likes")) for p in posts]
    comments = [to_num(p.get("comments")) for p in posts]
    shares = [to_num(p.get("shares")) for p in posts]
    span_days = max((dates[-1] - dates[0]).days, 1) if len(dates) > 1 else None
    return {
        "posts": len(posts),
        "last_post": dates[-1].isoformat() if dates else None,
        "days_since_last_post": (today - dates[-1]).days if dates else None,
        "posts_per_month": round(len(posts) / span_days * 30, 2) if span_days else None,
        "avg_likes": round(mean(likes), 1),
        "avg_comments": round(mean(comments), 1),
        "avg_shares": round(mean(shares), 1),
        "avg_engagement": round(mean(l + c + s for l, c, s in zip(likes, comments, shares)), 1),
    }


async def _platform(c: Company, platform: str, url: str, ctx: Ctx) -> dict:
    posts = await ctx.p.apify.run(platform, url=url, domain=c.domain, limit=ctx.opts.max_social_posts)
    out: dict = {"url": url, "stats": activity_stats(posts), "posts": posts, "comments": []}
    if platform == "instagram":
        for p in posts:
            for cm in p.get("latest_comments") or []:
                if isinstance(cm, dict) and cm.get("text"):
                    out["comments"].append(cm["text"])
    elif platform == "facebook" and posts:
        top = sorted(posts, key=lambda p: to_num(p.get("comments")), reverse=True)[:5]
        urls = [{"url": p["url"]} for p in top if p.get("url")]
        if urls:
            try:
                cms = await ctx.p.apify.run("facebook_comments", urls=urls, limit=ctx.opts.max_social_posts)
                out["comments"] = [cm["text"] for cm in cms if cm.get("text")]
            except Exception as e:  # noqa: BLE001
                ctx.warnings.append(f"{c.name}: Facebook comments unavailable ({e})")
    return out


async def analyze(c: Company, ctx: Ctx) -> ComponentResult:
    if not ctx.p.apify.enabled:
        return ComponentResult(status="skipped", error="APIFY_TOKEN not configured")
    wanted = [pl.value for pl in ctx.opts.social_platforms]
    targets = {pl: c.social_urls[pl] for pl in wanted if c.social_urls.get(pl)}
    if not targets:
        return ComponentResult(status="not_found",
                               error="No social profiles found on website - pass social_urls explicitly")
    results = await asyncio.gather(*[_platform(c, pl, u, ctx) for pl, u in targets.items()],
                                   return_exceptions=True)
    platforms: dict = {}
    for pl, res in zip(targets, results):
        if isinstance(res, Exception):
            platforms[pl] = {"status": "error", "error": str(res)[:300]}
        else:
            platforms[pl] = res
    ok = {pl: r for pl, r in platforms.items() if "stats" in r}
    data: dict = {
        "missing_platforms": [pl for pl in wanted if pl not in targets],
        "platforms": {pl: {k: v for k, v in r.items() if k not in ("posts", "comments")} for pl, r in platforms.items()},
        "total_posts": sum(r["stats"]["posts"] for r in ok.values()),
        "avg_engagement": round(mean([r["stats"]["avg_engagement"] for r in ok.values() if r["stats"]["posts"]] or [0]), 1),
    }
    if ok and ctx.p.llm.enabled:
        content = {pl: {"company_posts": [{"date": p.get("date"), "text": (p.get("text") or "")[:600],
                                           "likes": p.get("likes"), "comments": p.get("comments")}
                                          for p in r["posts"]],
                        "audience_comments": [t[:400] for t in r["comments"]]}
                   for pl, r in ok.items()}
        data["analysis"] = await ctx.p.llm.structured(
            name="social_analysis",
            instructions=(f"Analyse the social media presence of {c.name}. 'company_posts' is what the "
                          "company publishes; 'audience_comments' is what others say. Assess overall business "
                          "performance and perception, customer satisfaction, complaints/recurring issues and "
                          "positive feedback/success stories. Weight audience comments higher than self-promotion."),
            content=content, schema=PLATFORM_SCHEMA, language=ctx.language)
    if ctx.opts.include_raw:
        data["raw"] = {pl: {"posts": r.get("posts"), "comments": r.get("comments")} for pl, r in ok.items()}
    status = "ok" if ok else "error"
    return ComponentResult(status=status, data=data, error=None if ok else "All platforms failed")
