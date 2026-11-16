"""Danish Business Register (CVR) via Erhvervsstyrelsen's system-to-system
Elasticsearch access (distribution.virk.dk). Credentials are free but must be
requested from cvrselvbetjening@erst.dk.

Covers: company lookup, industry/location candidate search, and annual report
key figures parsed from the XBRL filings in the `offentliggoerelser` index.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from app.config import Settings
from app.models import Company
from app.util import FileCache, normalize_domain, normalize_name

log = logging.getLogger(__name__)
META = "Vrvirksomhed.virksomhedMetadata"

# XBRL local element names (Danish FSA taxonomy + IFRS) -> our key figure name.
XBRL_FIGURES = {
    "Revenue": "revenue",
    "GrossProfitLoss": "gross_profit",
    "GrossResult": "gross_profit",
    "ProfitLossFromOrdinaryOperatingActivities": "operating_profit",
    "ProfitLoss": "net_profit",
    "Equity": "equity",
    "Assets": "total_assets",
    "LiabilitiesAndEquity": "total_assets",
    "CashAndCashEquivalents": "cash",
    "EmployeeBenefitsExpense": "staff_costs",
    "AverageNumberOfEmployees": "employees",
}


class CVRError(RuntimeError):
    pass


class CVRClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self.cache = FileCache(settings.cache_dir, settings.cache_ttl_hours)

    @property
    def enabled(self) -> bool:
        return bool(self.s.cvr_username and self.s.cvr_password)

    async def _search(self, index: str, body: dict) -> list[dict]:
        if not self.enabled:
            raise CVRError("CVR credentials not configured")
        cached = self.cache.get("cvr", {"i": index, "b": body})
        if cached is not None:
            return cached
        url = f"{self.s.cvr_base_url}/{index}/_search"
        async with httpx.AsyncClient(timeout=30, auth=(self.s.cvr_username, self.s.cvr_password)) as c:
            r = await c.post(url, json=body)
        if r.status_code >= 400:
            raise CVRError(f"CVR search failed {r.status_code}: {r.text[:300]}")
        hits = [h["_source"] for h in r.json().get("hits", {}).get("hits", [])]
        self.cache.set("cvr", {"i": index, "b": body}, hits)
        return hits

    # -- company -----------------------------------------------------------
    @staticmethod
    def to_company(src: dict) -> Company:
        v = src.get("Vrvirksomhed", src)
        m = v.get("virksomhedMetadata", {}) or {}
        addr = m.get("nyesteBeliggenhedsadresse") or {}
        branch = m.get("nyesteHovedbranche") or {}
        street = " ".join(
            str(x) for x in (addr.get("vejnavn"), addr.get("husnummerFra"), addr.get("bogstavFra")) if x
        )
        postcode = str(addr.get("postnummer") or "") or None
        address = ", ".join(x for x in (street, f"{postcode or ''} {addr.get('postdistrikt') or ''}".strip()) if x)
        homepages = [h.get("kontaktoplysning") for h in v.get("hjemmeside", []) or [] if h.get("kontaktoplysning")]
        emp = None
        for key in ("nyesteAarsbeskaeftigelse", "nyesteKvartalsbeskaeftigelse", "nyesteMaanedsbeskaeftigelse"):
            e = m.get(key) or {}
            emp = e.get("antalAnsatte") or e.get("antalAarsvaerk")
            if emp:
                break
        return Company(
            name=(m.get("nyesteNavn") or {}).get("navn") or str(v.get("cvrNummer")),
            cvr=str(v.get("cvrNummer")) if v.get("cvrNummer") else None,
            domain=normalize_domain(homepages[-1]) if homepages else None,
            address=address or None,
            postcode=postcode,
            industry_code=str(branch.get("branchekode") or "") or None,
            industry_text=branch.get("branchetekst"),
            employees=int(emp) if emp else None,
            source="cvr",
        )

    async def get_by_cvr(self, cvr: str) -> Company | None:
        hits = await self._search(
            "cvr-permanent/virksomhed",
            {"size": 1, "query": {"term": {"Vrvirksomhed.cvrNummer": int(cvr)}}},
        )
        return self.to_company(hits[0]) if hits else None

    async def find(self, name: str | None = None, domain: str | None = None,
                   postcode: str | None = None) -> Company | None:
        """Best-effort match of a name/domain to a CVR entity."""
        should: list[dict] = []
        if name:
            should.append({"match": {f"{META}.nyesteNavn.navn": {"query": name, "operator": "and"}}})
        if domain:
            should.append({"match_phrase": {"Vrvirksomhed.hjemmeside.kontaktoplysning": domain}})
        if not should:
            return None
        must: list[dict] = [{"bool": {"should": should, "minimum_should_match": 1}}]
        if postcode:
            must.append({"term": {f"{META}.nyesteBeliggenhedsadresse.postnummer": int(postcode)}})
        hits = await self._search("cvr-permanent/virksomhed", {"size": 10, "query": {"bool": {"must": must}}})
        if not hits and postcode:  # relax location
            return await self.find(name, domain, None)
        companies = [self.to_company(h) for h in hits]
        if domain:
            for c in companies:
                if c.domain == domain:
                    return c
        if name:
            target = normalize_name(name)
            for c in companies:
                if normalize_name(c.name) == target:
                    return c
        return companies[0] if companies else None

    async def candidates(self, industry_codes: list[str], postcode: int, postcode_window: int,
                         size: int = 100) -> list[Company]:
        """Active companies with matching industry code prefix near a postcode.

        Danish postcodes are regionally clustered, so a numeric window is a cheap
        pre-filter; exact distance filtering happens after geocoding.
        """
        industry = [{"prefix": {f"{META}.nyesteHovedbranche.branchekode": code}} for code in industry_codes]
        body = {
            "size": size,
            "query": {"bool": {
                "must": [
                    {"bool": {"should": industry, "minimum_should_match": 1}},
                    {"range": {f"{META}.nyesteBeliggenhedsadresse.postnummer": {
                        "gte": postcode - postcode_window, "lte": postcode + postcode_window}}},
                ],
                "must_not": [{"terms": {f"{META}.sammensatStatus": ["OPLØST", "OPLØSTEFTERKONKURS",
                                                                     "UNDERKONKURS", "OPHØRT", "TVANGSOPLØST",
                                                                     "OPLØSTEFTERFUSION", "UNDERFRIVILLIGLIKVIDATION"]}}],
            }},
        }
        return [self.to_company(h) for h in await self._search("cvr-permanent/virksomhed", body)]

    # -- financials -------------------------------------------------------
    async def financials(self, cvr: str, years: int = 3) -> list[dict[str, Any]]:
        hits = await self._search("offentliggoerelser", {
            "size": years * 3,
            "query": {"bool": {"must": [
                {"term": {"cvrNummer": int(cvr)}},
                {"term": {"offentliggoerelsestype": "regnskab"}},
            ]}},
            "sort": [{"offentliggoerelsesTidspunkt": {"order": "desc"}}],
        })
        reports: list[dict[str, Any]] = []
        seen_periods: set[str] = set()
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
            for h in hits:
                period = (h.get("regnskab") or {}).get("regnskabsperiode") or {}
                end = period.get("slutDato")
                if not end or end in seen_periods:
                    continue
                docs = h.get("dokumenter") or []
                xbrl = next((d for d in docs if "xml" in (d.get("dokumentMimeType") or "").lower()), None)
                pdf = next((d for d in docs if "pdf" in (d.get("dokumentMimeType") or "").lower()), None)
                report: dict[str, Any] = {
                    "period_start": period.get("startDato"), "period_end": end,
                    "published": h.get("offentliggoerelsesTidspunkt"),
                    "pdf_url": pdf.get("dokumentUrl") if pdf else None,
                    "xbrl_url": xbrl.get("dokumentUrl") if xbrl else None,
                    "figures": {},
                }
                if xbrl:
                    try:
                        r = await c.get(xbrl["dokumentUrl"])
                        r.raise_for_status()
                        report["figures"] = parse_xbrl(r.content, end)
                    except Exception as e:  # noqa: BLE001 - keep going on bad filings
                        report["parse_error"] = str(e)[:200]
                reports.append(report)
                seen_periods.add(end)
                if len(reports) >= years:
                    break
        return reports


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_xbrl(content: bytes, period_end: str) -> dict[str, float]:
    """Extract headline figures for the reporting period (no dimensional contexts)."""
    root = ET.fromstring(content)
    valid_ctx: set[str] = set()
    for ctx in root.iter():
        if _local(ctx.tag) != "context":
            continue
        if any(_local(e.tag) in ("segment", "scenario") for e in ctx.iter()):
            continue
        dates = {_local(e.tag): (e.text or "").strip() for e in ctx.iter()
                 if _local(e.tag) in ("endDate", "instant")}
        if period_end in dates.values():
            valid_ctx.add(ctx.get("id"))
    out: dict[str, float] = {}
    for el in root.iter():
        name = XBRL_FIGURES.get(_local(el.tag))
        if not name or el.get("contextRef") not in valid_ctx or name in out:
            continue
        try:
            out[name] = float((el.text or "").strip())
        except ValueError:
            continue
    return out
