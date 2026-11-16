"""Competitor Intelligence Agent - HTTP API.

Run:  uvicorn app.main:app --reload
Docs: http://localhost:8000/docs
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import APIKeyHeader

from app.config import Settings, get_settings
from app.discovery import resolve, select_competitors
from app.jobs import build_job_backend
from app.models import AnalysisRequest, AnalysisResult, Company, DiscoveryRequest, Job
from app.pipeline import run_analysis
from app.providers import build_providers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class _State:
    """Process-wide singletons, created on first use.

    Lazy rather than in the lifespan hook, because Azure Functions' ASGI wrapper
    does not reliably run lifespan startup before requests.
    """

    _inst: "_State | None" = None

    def __init__(self) -> None:
        s = get_settings()
        self.settings = s
        self.providers = build_providers(s)
        self.store, self.runner = build_job_backend(self.providers, s)


def get_state() -> _State:
    if _State._inst is None:
        _State._inst = _State()
    return _State._inst


def reset_state() -> None:  # tests
    _State._inst = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_state()  # warm up when the server supports lifespan (uvicorn)
    yield


app = FastAPI(
    title="Competitor Intelligence Agent",
    version="1.0.0",
    description="Analyses a company against competitors in the same industry within a configurable "
                "radius: financials (CVR), website, Trustpilot and social media.",
    lifespan=lifespan,
)
_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def auth(key: str | None = Security(_key_header), s: Settings = Depends(get_settings)) -> None:
    if s.api_key_set and key not in s.api_key_set:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing X-API-Key")


_DEMO = Path(__file__).parent / "static" / "demo.html"


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/demo")


@app.get("/demo", include_in_schema=False)
def demo_page() -> HTMLResponse:
    """Browser UI for running and presenting analyses. API calls from it still need X-API-Key."""
    return HTMLResponse(_DEMO.read_text(encoding="utf-8"))


@app.get("/health")
def health(s: Settings = Depends(get_settings)) -> dict:
    p = get_state().providers
    return {"status": "ok", "mock_mode": s.mock_providers, "job_backend": s.job_backend, "sources": {
        "apify": p.apify.enabled, "cvr": p.cvr.enabled, "llm": p.llm.enabled},
        "llm_provider": s.llm_provider, "llm_problem": p.llm.unavailable_reason}


@app.get("/v1/config/defaults", dependencies=[Depends(auth)])
def defaults(s: Settings = Depends(get_settings)) -> dict:
    """Default request values - useful for building a UI around the API."""
    return {"radius_km": s.default_radius_km, "max_competitors": s.default_max_competitors,
            "industry_code_digits": s.default_industry_code_digits, "language": s.report_language,
            "components": ["financials", "social", "website", "trustpilot"],
            "social_platforms": ["linkedin", "facebook", "instagram"],
            "competitor_modes": ["auto", "manual", "both"],
            "discovery_strategies": ["google_maps", "cvr", "combined"]}


@app.post("/v1/analyses", status_code=202, response_model=Job, dependencies=[Depends(auth)])
async def create_analysis(req: AnalysisRequest) -> Job:
    """Start an analysis job. Poll GET /v1/analyses/{id} or pass webhook_url."""
    return get_state().runner.submit(req)


@app.get("/v1/analyses/{job_id}", response_model=Job, dependencies=[Depends(auth)])
def get_analysis(job_id: str) -> Job:
    job = get_state().store.get(job_id.strip())
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/v1/analyses", dependencies=[Depends(auth)])
def list_analyses(limit: int = 20) -> list[dict]:
    return [{"id": j.id, "status": j.status, "created_at": j.created_at,
             "target": j.request.target.name or j.request.target.domain or j.request.target.cvr}
            for j in get_state().store.list(limit)]


@app.post("/v1/analyses/sync", response_model=AnalysisResult, dependencies=[Depends(auth)])
async def analyze_sync(req: AnalysisRequest, s: Settings = Depends(get_settings)) -> AnalysisResult:
    """Run inline and return the result. Real scrapes take minutes - prefer the async endpoint.
    On Azure Functions an HTTP request is cut off after 230 seconds."""
    return await run_analysis(req, get_state().providers, s)


@app.post("/v1/competitors/discover", dependencies=[Depends(auth)])
async def discover(req: DiscoveryRequest, s: Settings = Depends(get_settings)) -> dict:
    """Preview which competitors would be selected - cheap, no analysis is run."""
    warnings: list[str] = []
    p = get_state().providers
    target = await resolve(req.target, p, warnings, is_target=True)
    comps: list[Company] = await select_competitors(target, req.competitors, p, s, warnings)
    return {"target": target, "competitors": comps, "warnings": warnings}
