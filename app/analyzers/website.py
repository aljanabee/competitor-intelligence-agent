"""Company website: crawl key pages, extract positioning & profile links."""
from __future__ import annotations

import re

import httpx

from app.analyzers.base import Ctx, find_profile_links
from app.models import Company, ComponentResult

PAGE_SIGNALS = {
    "pricing": r"pris|price|pricing|takst",
    "about": r"om-os|about|om_os|hvem-er-vi",
    "contact": r"kontakt|contact",
    "careers": r"job|karriere|career",
    "blog": r"blog|nyheder|news|artikler",
    "cases": r"case|referencer|kunder|customers",
    "shop": r"shop|butik|webshop|kurv|cart",
    "booking": r"book|bestil-tid|reserv",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "value_proposition": {"type": "string"},
        "offerings": {"type": "array", "items": {"type": "string"}, "description": "Products / services"},
        "target_customers": {"type": "array", "items": {"type": "string"}},
        "differentiators": {"type": "array", "items": {"type": "string"}, "description": "Claimed USPs"},
        "pricing_signals": {"type": "string", "description": "Price level / model if visible, else 'not disclosed'"},
        "trust_signals": {"type": "array", "items": {"type": "string"}, "description": "Certifications, awards, reviews, client logos"},
        "calls_to_action": {"type": "array", "items": {"type": "string"}},
        "tone_and_brand": {"type": "string"},
        "content_marketing": {"type": "string", "description": "Blog/news activity and recency"},
        "weaknesses": {"type": "array", "items": {"type": "string"}, "description": "Gaps or weak points a competitor could exploit"},
    },
    "required": ["value_proposition", "offerings", "differentiators", "weaknesses"],
}


async def fetch_homepage_links(domain: str) -> dict[str, str]:
    """Cheap direct fetch (no Apify cost) used to locate social/Trustpilot profiles.

    Reads the raw HTML, so links in header/footer and behind cookie banners are included.
    """
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
               "Accept-Language": "da-DK,da;q=0.9,en;q=0.8"}
    found: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers) as c:
        for url in (f"https://{domain}", f"https://www.{domain}"):
            try:
                r = await c.get(url)
            except Exception:  # noqa: BLE001
                continue
            for k, v in find_profile_links(r.text).items():
                found.setdefault(k, v)
            if len(found) >= 3:
                break
    return found


async def analyze(c: Company, ctx: Ctx) -> ComponentResult:
    if not c.domain:
        return ComponentResult(status="not_found", error="No website/domain known")
    if not ctx.p.apify.enabled:
        return ComponentResult(status="skipped", error="APIFY_TOKEN not configured")
    pages = await ctx.p.apify.run("website", url=f"https://{c.domain}", domain=c.domain,
                                  limit=ctx.opts.max_website_pages)
    pages = [pg for pg in pages if (pg.get("text") or "").strip()]
    if not pages:  # JS-rendered or bot-protected site: retry with a real browser
        pages = await ctx.p.apify.run("website_fallback", url=f"https://{c.domain}", domain=c.domain,
                                      limit=ctx.opts.max_website_pages)
        pages = [pg for pg in pages if (pg.get("text") or "").strip()]
    if not pages:
        return ComponentResult(status="not_found", error="Website could not be crawled")

    all_text = "\n".join((pg.get("text") or "") for pg in pages)
    urls = [pg.get("url") or "" for pg in pages]
    signals = {k: any(re.search(pat, u, re.I) for u in urls) for k, pat in PAGE_SIGNALS.items()}
    data: dict = {
        "pages_crawled": len(pages),
        "urls": urls,
        "homepage_title": pages[0].get("title"),
        "meta_description": pages[0].get("description"),
        "page_signals": signals,
        "profile_links": find_profile_links(all_text + " " + " ".join(urls)),
        "word_count": len(all_text.split()),
    }
    if ctx.p.llm.enabled:
        digest = "\n\n".join(f"## {pg.get('url')}\n{(pg.get('text') or '')[:6000]}" for pg in pages)
        data["insights"] = await ctx.p.llm.structured(
            name="website_analysis",
            instructions=f"Analyse the website of {c.name} ({c.domain}) for competitive positioning.",
            content=digest, schema=SCHEMA, language=ctx.language)
    else:
        ctx.warnings.append("LLM not configured - website insights limited to structural signals")
    if ctx.opts.include_raw:
        data["raw"] = pages
    return ComponentResult(data=data)
