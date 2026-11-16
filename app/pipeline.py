"""Orchestrates one analysis run."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from app.analyzers import financials, social, trustpilot, website
from app.analyzers.base import Ctx
from app.analyzers.website import fetch_homepage_links
from app.config import Settings
from app.discovery import resolve, select_competitors
from app.models import AnalysisRequest, AnalysisResult, Company, CompanyAnalysis, ComponentResult
from app.providers import Providers
from app.report import comparison_matrix, synthesize

log = logging.getLogger(__name__)
Progress = Callable[[str], None]
COMPONENTS = {"financials": financials.analyze, "website": website.analyze,
              "trustpilot": trustpilot.analyze, "social": social.analyze}


async def _safe(name: str, fn: Callable[[], Awaitable[ComponentResult]], company: Company,
                ctx: Ctx) -> ComponentResult:
    try:
        return await fn()
    except Exception as e:  # noqa: BLE001 - one failing source must not kill the report
        log.exception("%s failed for %s", name, company.name)
        ctx.warnings.append(f"{company.name}: {name} failed ({type(e).__name__}: {str(e)[:200]})")
        return ComponentResult(status="error", error=str(e)[:500])


async def analyze_company(c: Company, req: AnalysisRequest, ctx: Ctx, sem: asyncio.Semaphore,
                          progress: Progress) -> CompanyAnalysis:
    comps = req.components
    async with sem:
        progress(f"Analysing {c.name}")
        result = CompanyAnalysis(company=c)

        # 1) website first - it is also where social/Trustpilot profiles are found
        if comps.website:
            result.website = await _safe("website", lambda: website.analyze(c, ctx), c, ctx)
        links: dict[str, str] = {}
        if result.website and result.website.status == "ok":
            links = dict(result.website.data.get("profile_links", {}))
        # The crawler strips header/footer (where social icons live), so also scan the raw homepage HTML.
        if c.domain and (comps.social or comps.trustpilot) and not getattr(ctx.p.apify, "is_mock", False):
            for k, v in (await fetch_homepage_links(c.domain)).items():
                links.setdefault(k, v)
            if result.website and result.website.status == "ok":
                result.website.data["profile_links"] = links
        for k, v in links.items():
            if k == "trustpilot":
                c.trustpilot_url = c.trustpilot_url or v
            else:
                c.social_urls.setdefault(k, v)

        # 2) remaining components in parallel
        tasks = {name: _safe(name, (lambda n=name: COMPONENTS[n](c, ctx)), c, ctx)
                 for name in ("financials", "trustpilot", "social") if getattr(comps, name)}
        for name, res in zip(tasks, await asyncio.gather(*tasks.values())):
            setattr(result, name, res)
        return result


async def run_analysis(req: AnalysisRequest, p: Providers, s: Settings,
                       progress: Progress = lambda m: None) -> AnalysisResult:
    ctx = Ctx(p=p, opts=req.options, language=req.options.language or s.report_language)
    c = req.components
    if not p.apify.enabled and (c.website or c.trustpilot or c.social):
        ctx.warnings.append("APIFY_TOKEN not configured: website, Trustpilot and social components are skipped")
    if not p.cvr.enabled and c.financials:
        ctx.warnings.append("CVR credentials not configured: financials and CVR discovery are skipped")
    if not p.llm.enabled:
        ctx.warnings.append(f"LLM not configured ({p.llm.unavailable_reason}): no qualitative analysis or synthesis")
    progress("Resolving target company")
    target = await resolve(req.target, p, ctx.warnings, is_target=True)
    competitors = await select_competitors(target, req.competitors, p, s, ctx.warnings, progress)
    if not competitors:
        ctx.warnings.append("No competitors to compare against - result contains target analysis only")
    progress(f"Selected {len(competitors)} competitors: " + ", ".join(c.name for c in competitors))

    sem = asyncio.Semaphore(4)  # companies analysed concurrently (Apify runs are the bottleneck)
    analyses = await asyncio.gather(*[analyze_company(c, req, ctx, sem, progress)
                                      for c in [target, *competitors]])
    target_a, comp_a = analyses[0], list(analyses[1:])

    progress("Building comparison matrix")
    matrix = comparison_matrix(target_a, comp_a)
    progress("Writing synthesis")
    try:
        synthesis = await synthesize(target_a, comp_a, matrix, ctx)
    except Exception as e:  # noqa: BLE001
        ctx.warnings.append(f"Synthesis failed: {e}")
        synthesis = {"status": "error", "error": str(e)[:500]}
    return AnalysisResult(target=target_a, competitors=comp_a, comparison=matrix,
                          synthesis=synthesis, warnings=ctx.warnings)
