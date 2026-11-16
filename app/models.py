"""Request / response schemas for the public API."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------
class CompanyRef(BaseModel):
    """Identifies a company. Supply at least one of cvr / domain / name."""

    name: str | None = None
    cvr: str | None = Field(None, description="8-digit Danish CVR number")
    domain: str | None = Field(None, description="e.g. 'example.dk' or a full URL")
    address: str | None = Field(None, description="Free-text address; improves geocoding")
    # Optional explicit source URLs. If omitted the agent tries to find them.
    trustpilot_url: HttpUrl | None = None
    social_urls: dict[str, HttpUrl] = Field(
        default_factory=dict,
        description="Platform -> profile URL, e.g. {'linkedin': 'https://linkedin.com/company/x'}",
    )

    @field_validator("cvr")
    @classmethod
    def _cvr(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.replace(" ", "").removeprefix("DK")
        if not (v.isdigit() and len(v) == 8):
            raise ValueError("cvr must be 8 digits")
        return v

    @field_validator("domain")
    @classmethod
    def _domain(cls, v: str | None) -> str | None:
        if not v:
            return None
        from app.util import normalize_domain

        return normalize_domain(v)

    @model_validator(mode="after")
    def _at_least_one(self) -> "CompanyRef":
        if not (self.cvr or self.domain or self.name):
            raise ValueError("provide at least one of cvr, domain or name")
        return self


class CompetitorMode(str, Enum):
    auto = "auto"      # discover by industry + location
    manual = "manual"  # only the companies the caller lists
    both = "both"      # manual list + auto-discovered top-up


class DiscoveryStrategy(str, Enum):
    google_maps = "google_maps"  # Apify Google Maps scraper, true radius search
    cvr = "cvr"                  # CVR industry code + geocoded addresses
    combined = "combined"        # both, merged & de-duplicated


class CompetitorSelection(BaseModel):
    mode: CompetitorMode = CompetitorMode.auto
    manual: list[CompanyRef] = Field(default_factory=list)
    radius_km: float | None = Field(None, gt=0, le=500)
    max_competitors: int | None = Field(None, ge=1, le=25)
    strategy: DiscoveryStrategy = DiscoveryStrategy.combined
    industry_codes: list[str] = Field(
        default_factory=list,
        description="Override industry codes (DB25/DB07). Default: target's main code from CVR.",
    )
    industry_keywords: list[str] = Field(
        default_factory=list,
        description="Search terms for Google Maps discovery, e.g. ['tandlæge']. Default: CVR industry text.",
    )
    exclude_domains: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _manual_needs_list(self) -> "CompetitorSelection":
        if self.mode in (CompetitorMode.manual,) and not self.manual:
            raise ValueError("mode=manual requires at least one entry in 'manual'")
        return self


class SocialPlatform(str, Enum):
    linkedin = "linkedin"
    facebook = "facebook"
    instagram = "instagram"


class Components(BaseModel):
    """Toggle individual analysis components on/off."""

    financials: bool = True
    social: bool = True
    website: bool = True
    trustpilot: bool = True


class AnalysisOptions(BaseModel):
    social_platforms: list[SocialPlatform] = Field(
        default_factory=lambda: list(SocialPlatform)
    )
    max_social_posts: int = Field(30, ge=1, le=200)
    max_reviews: int = Field(100, ge=1, le=1000)
    max_website_pages: int = Field(10, ge=1, le=50)
    financial_years: int = Field(3, ge=1, le=10)
    language: str | None = Field(None, description="Report language: 'da' or 'en'")
    include_raw: bool = Field(False, description="Include raw scraped items in the result")


class AnalysisRequest(BaseModel):
    target: CompanyRef
    competitors: CompetitorSelection = Field(default_factory=CompetitorSelection)
    components: Components = Field(default_factory=Components)
    options: AnalysisOptions = Field(default_factory=AnalysisOptions)
    webhook_url: HttpUrl | None = Field(None, description="POSTed the finished job when done")


class DiscoveryRequest(BaseModel):
    target: CompanyRef
    competitors: CompetitorSelection = Field(default_factory=CompetitorSelection)


# ---------------------------------------------------------------------------
# Internal / response
# ---------------------------------------------------------------------------
class Company(BaseModel):
    """A resolved company (target or competitor)."""

    name: str
    cvr: str | None = None
    domain: str | None = None
    address: str | None = None
    postcode: str | None = None
    lat: float | None = None
    lng: float | None = None
    distance_km: float | None = None
    industry_code: str | None = None
    industry_text: str | None = None
    employees: int | None = None
    trustpilot_url: str | None = None
    social_urls: dict[str, str] = Field(default_factory=dict)
    source: str = "input"  # input | cvr | google_maps | manual
    is_target: bool = False

    @property
    def key(self) -> str:
        return self.cvr or self.domain or self.name.lower()


class ComponentResult(BaseModel):
    status: str = "ok"  # ok | skipped | not_found | error
    error: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class CompanyAnalysis(BaseModel):
    company: Company
    financials: ComponentResult | None = None
    website: ComponentResult | None = None
    trustpilot: ComponentResult | None = None
    social: ComponentResult | None = None


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class AnalysisResult(BaseModel):
    target: CompanyAnalysis
    competitors: list[CompanyAnalysis]
    comparison: dict[str, Any]
    synthesis: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


class Job(BaseModel):
    id: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    progress: list[str] = Field(default_factory=list)
    request: AnalysisRequest
    result: AnalysisResult | None = None
    error: str | None = None
