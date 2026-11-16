"""Resolve the target and select competitors (auto, manual or both)."""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from app.config import Settings
from app.models import Company, CompanyRef, CompetitorMode, CompetitorSelection, DiscoveryStrategy
from app.providers import Providers
from app.util import haversine_km, normalize_domain, normalize_name

log = logging.getLogger(__name__)
Progress = Callable[[str], None]


async def resolve(ref: CompanyRef, p: Providers, warnings: list[str], is_target: bool = False) -> Company:
    """Turn a CompanyRef into a Company enriched from CVR + geocoding where possible."""
    company: Company | None = None
    if p.cvr.enabled:
        try:
            if ref.cvr:
                company = await p.cvr.get_by_cvr(ref.cvr)
            elif ref.domain or ref.name:
                company = await p.cvr.find(name=ref.name, domain=ref.domain)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"CVR lookup failed for {ref.name or ref.domain or ref.cvr}: {e}")
    if company is None:
        company = Company(name=ref.name or ref.domain or ref.cvr or "unknown", cvr=ref.cvr, source="input")
        if p.cvr.enabled:
            warnings.append(f"No CVR match for '{company.name}' - financials unavailable")

    # caller-supplied values win over CVR
    upd: dict = {"is_target": is_target}
    if ref.domain:
        upd["domain"] = ref.domain
    if ref.name and not company.name:
        upd["name"] = ref.name
    if ref.address:
        upd["address"] = ref.address
    if ref.trustpilot_url:
        upd["trustpilot_url"] = str(ref.trustpilot_url)
    if ref.social_urls:
        upd["social_urls"] = {k: str(v) for k, v in ref.social_urls.items()}
    if not is_target and company.source == "cvr":
        upd["source"] = "manual"
    company = company.model_copy(update=upd)

    if company.address and company.lat is None:
        try:
            loc = await p.geo.geocode(company.address)
            if loc:
                company.lat, company.lng = loc
        except Exception as e:  # noqa: BLE001
            warnings.append(f"Geocoding failed for {company.name}: {e}")
    return company


def _postcode_window(radius_km: float) -> int:
    # Heuristic pre-filter only: ~1 postcode number per km near cities, wider in rural areas.
    return int(max(30, min(400, radius_km * 6)))


async def _discover_cvr(target: Company, sel: CompetitorSelection, radius: float, limit: int,
                        p: Providers, s: Settings, warnings: list[str]) -> list[Company]:
    if not p.cvr.enabled:
        warnings.append("CVR discovery skipped: CVR credentials not configured")
        return []
    codes = sel.industry_codes or (
        [target.industry_code[: s.default_industry_code_digits]] if target.industry_code else [])
    if not codes or not target.postcode:
        warnings.append("CVR discovery skipped: target industry code or postcode unknown")
        return []
    cands = await p.cvr.candidates(codes, int(target.postcode), _postcode_window(radius), size=limit * 8)
    cands = [c for c in cands if c.cvr != target.cvr]
    out: list[Company] = []
    for c in cands:  # sequential: geocoder is rate-limited anyway
        if not c.address:
            continue
        loc = await p.geo.geocode(c.address)
        if not loc:
            continue
        c.lat, c.lng = loc
        if target.lat is not None:
            c.distance_km = round(haversine_km(target.lat, target.lng, c.lat, c.lng), 2)
            if c.distance_km > radius:
                continue
        out.append(c)
        if len(out) >= limit * 3:
            break
    return out


async def _discover_maps(target: Company, sel: CompetitorSelection, radius: float, limit: int,
                         p: Providers, warnings: list[str]) -> list[Company]:
    if not p.apify.enabled:
        warnings.append("Google Maps discovery skipped: APIFY_TOKEN not configured")
        return []
    if target.lat is None:
        warnings.append("Google Maps discovery skipped: target location unknown (add 'address')")
        return []
    keywords = sel.industry_keywords or ([target.industry_text] if target.industry_text else [])
    if not keywords:
        warnings.append("Google Maps discovery skipped: no industry keyword (set competitors.industry_keywords)")
        return []
    results = await asyncio.gather(*[
        p.apify.run("google_maps", query=k, lat=target.lat, lng=target.lng, radius_km=radius, limit=limit * 4)
        for k in keywords], return_exceptions=True)
    out: list[Company] = []
    for kw, res in zip(keywords, results):
        if isinstance(res, Exception):
            warnings.append(f"Google Maps discovery failed for '{kw}': {res}")
            continue
        for it in res:
            if not it.get("name") or it.get("lat") is None:
                continue
            d = haversine_km(target.lat, target.lng, float(it["lat"]), float(it["lng"]))
            if d > radius:
                continue
            out.append(Company(
                name=it["name"], domain=normalize_domain(it["website"]) if it.get("website") else None,
                address=it.get("address"), postcode=str(it["postcode"]) if it.get("postcode") else None,
                lat=float(it["lat"]), lng=float(it["lng"]), distance_km=round(d, 2),
                industry_text=it.get("category"), source="google_maps"))
    return out


def _dedupe(companies: list[Company], target: Company, exclude: set[str]) -> list[Company]:
    seen: set[str] = {target.cvr or "", target.domain or "", normalize_name(target.name)} | exclude
    out: list[Company] = []
    for c in companies:
        keys = {k for k in (c.cvr, c.domain, normalize_name(c.name)) if k}
        if keys & seen:
            continue
        seen |= keys
        out.append(c)
    return out


def _rank(c: Company) -> tuple:
    # prefer: has website (analysable), closer, larger
    return (c.domain is None, c.distance_km if c.distance_km is not None else 9e9, -(c.employees or 0))


async def select_competitors(target: Company, sel: CompetitorSelection, p: Providers, s: Settings,
                             warnings: list[str], progress: Progress = lambda m: None) -> list[Company]:
    radius = sel.radius_km or s.default_radius_km
    limit = sel.max_competitors or s.default_max_competitors
    exclude = {normalize_domain(d) for d in sel.exclude_domains}

    manual: list[Company] = []
    if sel.mode in (CompetitorMode.manual, CompetitorMode.both):
        progress(f"Resolving {len(sel.manual)} manually supplied competitors")
        manual = list(await asyncio.gather(*[resolve(r, p, warnings) for r in sel.manual]))
        for c in manual:
            c.source = "manual"
            if target.lat is not None and c.lat is not None:
                c.distance_km = round(haversine_km(target.lat, target.lng, c.lat, c.lng), 2)

    auto: list[Company] = []
    remaining = limit - len(manual) if sel.mode == CompetitorMode.both else limit
    if sel.mode in (CompetitorMode.auto, CompetitorMode.both) and remaining > 0:
        progress(f"Discovering competitors within {radius} km (strategy={sel.strategy.value})")
        found: list[Company] = []
        if sel.strategy in (DiscoveryStrategy.google_maps, DiscoveryStrategy.combined):
            found += await _discover_maps(target, sel, radius, remaining, p, warnings)
        if sel.strategy in (DiscoveryStrategy.cvr, DiscoveryStrategy.combined):
            try:
                found += await _discover_cvr(target, sel, radius, remaining, p, s, warnings)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"CVR discovery failed: {e}")
        found = _dedupe(found, target, exclude | {k for m in manual for k in (m.cvr, m.domain) if k})
        auto = sorted(found, key=_rank)[:remaining]
        # enrich map hits with CVR (needed for financials)
        if p.cvr.enabled:
            async def enrich(c: Company) -> Company:
                if c.cvr:
                    return c
                try:
                    m = await p.cvr.find(name=c.name, domain=c.domain, postcode=c.postcode)
                except Exception:  # noqa: BLE001
                    m = None
                if m:
                    c = c.model_copy(update={"cvr": m.cvr, "industry_code": m.industry_code,
                                             "employees": m.employees, "domain": c.domain or m.domain})
                return c
            auto = list(await asyncio.gather(*[enrich(c) for c in auto]))
        if not auto:
            warnings.append("Automatic discovery found no competitors - widen radius or set industry_keywords")

    return _dedupe(manual, target, exclude) + auto
