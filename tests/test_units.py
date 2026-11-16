from datetime import date

from app.analyzers.base import find_profile_links
from app.analyzers.financials import derive_kpis
from app.analyzers.trustpilot import review_stats
from app.providers.apify import normalize, render
from app.providers.cvr import CVRClient, parse_xbrl
from app.util import haversine_km, normalize_domain

XBRL = b"""<?xml version="1.0"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:fsa="http://xbrl.dcca.dk/fsa"
            xmlns:xbrldi="http://xbrl.org/2006/xbrldi">
  <xbrli:context id="cy"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate><xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:context id="cyi"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:instant>2024-12-31</xbrli:instant></xbrli:period></xbrli:context>
  <xbrli:context id="py"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2023-01-01</xbrli:startDate><xbrli:endDate>2023-12-31</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:context id="dim"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier>
    <xbrli:segment><xbrldi:explicitMember dimension="a">b</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
    <xbrli:period><xbrli:instant>2024-12-31</xbrli:instant></xbrli:period></xbrli:context>
  <fsa:GrossProfitLoss contextRef="py" unitRef="DKK">900</fsa:GrossProfitLoss>
  <fsa:GrossProfitLoss contextRef="cy" unitRef="DKK">1000</fsa:GrossProfitLoss>
  <fsa:ProfitLoss contextRef="cy" unitRef="DKK">150</fsa:ProfitLoss>
  <fsa:Equity contextRef="dim" unitRef="DKK">1</fsa:Equity>
  <fsa:Equity contextRef="cyi" unitRef="DKK">500</fsa:Equity>
  <fsa:Assets contextRef="cyi" unitRef="DKK">2000</fsa:Assets>
</xbrli:xbrl>"""


def test_parse_xbrl_picks_current_period_without_dimensions():
    f = parse_xbrl(XBRL, "2024-12-31")
    assert f == {"gross_profit": 1000, "net_profit": 150, "equity": 500, "total_assets": 2000}


def test_derive_kpis_growth_and_ratios():
    k = derive_kpis([
        {"period_end": "2024-12-31", "figures": {"gross_profit": 1000, "net_profit": 150, "equity": 500,
                                                 "total_assets": 2000, "employees": 10}},
        {"period_end": "2023-12-31", "figures": {"gross_profit": 800}},
    ])
    assert k["gross_profit_growth"] == 0.25
    assert k["equity_ratio"] == 0.25
    assert k["gross_profit_per_employee"] == 100


def test_render_keeps_types_and_splices_lists():
    tpl = {"a": "{{n}}", "b": ["{{urls}}"], "c": "https://x/{{d}}", "d": {"coords": ["{{lng}}", "{{lat}}"]}}
    out = render(tpl, {"n": 5, "urls": [{"url": "u1"}, {"url": "u2"}], "d": "y.dk", "lat": 56.1, "lng": 10.2})
    assert out == {"a": 5, "b": [{"url": "u1"}, {"url": "u2"}], "c": "https://x/y.dk", "d": {"coords": [10.2, 56.1]}}


def test_normalize_fallback_fields():
    items = normalize([{"stars": 4, "dates": {"publishedDate": "2025-01-01"}}],
                      {"rating": ["rating", "stars"], "date": ["date", "dates.publishedDate"]})
    assert items == [{"rating": 4, "date": "2025-01-01"}]


def test_review_stats():
    reviews = [{"rating": 5, "date": "2026-10-01", "reply": "tak"}, {"rating": 1, "date": "2025-01-01"},
               {"rating": 4, "date": "2026-09-20"}]
    s = review_stats(reviews, today=date(2026, 11, 1))
    assert s["avg_rating"] == 3.33 and s["share_negative"] == 0.333
    assert s["reviews_last_90d"] == 2 and s["reply_rate"] == 0.333


def test_profile_links():
    text = ("x https://www.linkedin.com/company/acme-aps/ y https://www.facebook.com/sharer/sharer.php "
            "https://facebook.com/acmedk https://www.instagram.com/p/abc https://instagram.com/acme.dk "
            "https://dk.trustpilot.com/review/acme.dk")
    assert find_profile_links(text) == {
        "linkedin": "https://www.linkedin.com/company/acme-aps", "facebook": "https://facebook.com/acmedk",
        "instagram": "https://instagram.com/acme.dk", "trustpilot": "https://dk.trustpilot.com/review/acme.dk"}


def test_cvr_to_company():
    src = {"Vrvirksomhed": {"cvrNummer": 12345678, "hjemmeside": [{"kontaktoplysning": "www.Acme.dk"}],
           "virksomhedMetadata": {"nyesteNavn": {"navn": "Acme ApS"},
                                  "nyesteHovedbranche": {"branchekode": "561010", "branchetekst": "Restauranter"},
                                  "nyesteBeliggenhedsadresse": {"vejnavn": "Søndergade", "husnummerFra": 1,
                                                                "postnummer": 8000, "postdistrikt": "Aarhus C"},
                                  "nyesteAarsbeskaeftigelse": {"antalAnsatte": 12}}}}
    c = CVRClient.to_company(src)
    assert (c.name, c.cvr, c.domain, c.postcode, c.industry_code, c.employees) == \
        ("Acme ApS", "12345678", "acme.dk", "8000", "561010", 12)
    assert c.address == "Søndergade 1, 8000 Aarhus C"


def test_helpers():
    assert normalize_domain("https://www.Example.dk/path") == "example.dk"
    assert 140 < haversine_km(56.1572, 10.2107, 55.6761, 12.5683) < 160  # Aarhus-Copenhagen


def test_normalize_keeps_raw_keys_for_diagnostics():
    items = normalize([{"stars": 5, "body": "x"}], {"rating": ["rating", "stars"], "_keys": ["__keys__"]})
    assert items == [{"rating": 5, "_keys": ["body", "stars"]}]


def test_trustpilot_reports_unmatched_fields():
    import asyncio
    from types import SimpleNamespace
    from app.analyzers import trustpilot
    from app.analyzers.base import Ctx
    from app.models import AnalysisOptions, Company

    class FakeApify:
        enabled = True

        async def run(self, source, **kw):
            return [{"rating": None, "text": None, "_keys": ["foo", "bar"]}]

    ctx = Ctx(p=SimpleNamespace(apify=FakeApify(), llm=SimpleNamespace(enabled=False)),
              opts=AnalysisOptions(), language="da")
    res = asyncio.run(trustpilot.analyze(Company(name="x", domain="x.dk"), ctx))
    assert res.status == "not_found" and "fields seen: ['foo', 'bar']" in res.error


def test_apify_retries_on_memory_cap_and_sends_memory(monkeypatch, tmp_path):
    import asyncio
    import httpx
    from app.config import Settings
    from app.providers import apify as apify_mod

    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, dict(req.url.params)))
        if req.method == "POST" and req.url.path.endswith("/runs"):
            if sum(1 for c in calls if c[0] == "POST") == 1:
                return httpx.Response(402, text='{"error":{"type":"actor-memory-limit-exceeded"}}')
            return httpx.Response(201, json={"data": {"id": "r1", "defaultDatasetId": "d1", "status": "SUCCEEDED"}})
        if "/datasets/" in req.url.path:
            return httpx.Response(200, json=[{"url": "https://x.dk", "markdown": "hello"}])
        return httpx.Response(200, json={"data": {"status": "SUCCEEDED"}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(apify_mod.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))

    async def no_sleep(_):
        return None

    monkeypatch.setattr(apify_mod.asyncio, "sleep", no_sleep)
    client = apify_mod.ApifyClient(Settings(apify_token="t", cache_dir=str(tmp_path)))
    items = asyncio.run(client.run("website", url="https://x.dk", domain="x.dk", limit=2))
    posts = [c for c in calls if c[0] == "POST"]
    assert len(posts) == 2 and posts[1][2].get("memory") == "1024"
    assert items[0]["text"] == "hello"


def test_profile_links_forced_to_https():
    assert find_profile_links("x http://www.instagram.com/komplett.dk y")["instagram"] == "https://www.instagram.com/komplett.dk"
