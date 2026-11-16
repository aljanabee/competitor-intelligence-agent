"""Financial statements & annual reports from CVR (XBRL key figures)."""
from __future__ import annotations

from app.analyzers.base import Ctx
from app.models import Company, ComponentResult


def _ratio(a: float | None, b: float | None) -> float | None:
    if a is None or not b:
        return None
    return round(a / b, 4)


def derive_kpis(reports: list[dict]) -> dict:
    if not reports:
        return {}
    cur = reports[0]["figures"]
    prev = reports[1]["figures"] if len(reports) > 1 else {}
    kpis = {
        "fiscal_year_end": reports[0]["period_end"],
        "revenue": cur.get("revenue"),
        "gross_profit": cur.get("gross_profit"),
        "net_profit": cur.get("net_profit"),
        "equity": cur.get("equity"),
        "employees": cur.get("employees"),
        "net_margin": _ratio(cur.get("net_profit"), cur.get("revenue")),
        "equity_ratio": _ratio(cur.get("equity"), cur.get("total_assets")),
        "revenue_per_employee": _ratio(cur.get("revenue"), cur.get("employees")),
        "gross_profit_per_employee": _ratio(cur.get("gross_profit"), cur.get("employees")),
    }
    # Danish SMEs (class B) often omit revenue -> gross profit is the main top line.
    for key in ("revenue", "gross_profit", "net_profit"):
        if cur.get(key) is not None and prev.get(key):
            kpis[f"{key}_growth"] = round((cur[key] - prev[key]) / abs(prev[key]), 4)
    kpis["history"] = [{"period_end": r["period_end"], **r["figures"]} for r in reports]
    return {k: v for k, v in kpis.items() if v is not None}


async def analyze(c: Company, ctx: Ctx) -> ComponentResult:
    if not c.cvr:
        return ComponentResult(status="not_found", error="No CVR number - cannot fetch annual reports")
    if not ctx.p.cvr.enabled:
        return ComponentResult(status="skipped", error="CVR credentials not configured")
    reports = await ctx.p.cvr.financials(c.cvr, ctx.opts.financial_years)
    if not reports:
        return ComponentResult(status="not_found", error="No published annual reports found")
    kpis = derive_kpis(reports)
    if len(kpis) <= 2:
        ctx.warnings.append(f"{c.name}: annual reports found but key figures could not be parsed")
    return ComponentResult(data={"kpis": kpis, "reports": [
        {k: r.get(k) for k in ("period_start", "period_end", "published", "pdf_url", "xbrl_url", "parse_error")}
        for r in reports]})
