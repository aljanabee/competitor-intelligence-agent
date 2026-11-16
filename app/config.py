"""Service-level settings, read from environment variables / .env.

Per-request behaviour (radius, enabled components, competitor mode ...) is NOT
configured here - it is part of the API request. These settings are the
defaults and the credentials.
"""
import tempfile
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # --- API -------------------------------------------------------------
    api_keys: str = ""  # comma-separated; empty = auth disabled (dev only)
    db_path: str = str(ROOT / "data" / "jobs.sqlite3")
    max_concurrent_jobs: int = 3

    # --- Job backend ---------------------------------------------------------
    # local: in-process asyncio + SQLite (uvicorn / Docker)
    # azure: Azure Storage queue + Blob Storage (Azure Functions)
    job_backend: str = "local"
    # Connection string for jobs blob/queue. Falls back to AzureWebJobsStorage.
    # Leave both empty and set AzureWebJobsStorage__accountName to use managed identity.
    jobs_storage_connection: str = ""
    azurewebjobsstorage: str = ""  # read from the Functions-provided setting
    azurewebjobsstorage__accountname: str = ""
    jobs_container: str = "jobs"
    jobs_queue: str = "analysis-jobs"

    # --- Mock mode: run the whole pipeline on synthetic data, no keys needed
    mock_providers: bool = False

    # --- Apify -------------------------------------------------------------
    apify_token: str = ""
    apify_base_url: str = "https://api.apify.com/v2"
    apify_actors_file: str = str(ROOT / "actors.yaml")
    apify_run_timeout_s: int = 600
    apify_max_concurrent_runs: int = 2  # Apify accounts have a total memory cap (free plan: 16 GB)
    cache_dir: str = str(ROOT / "data" / "cache")
    cache_ttl_hours: int = 24

    # --- CVR (Erhvervsstyrelsen system-til-system adgang) -----------------
    cvr_username: str = ""
    cvr_password: str = ""
    cvr_base_url: str = "http://distribution.virk.dk"

    # --- Geocoding (Nominatim / OpenStreetMap by default) -----------------
    geocoder_url: str = "https://nominatim.openstreetmap.org/search"
    geocoder_user_agent: str = "competitor-intel-agent/1.0"
    geocoder_min_interval_s: float = 1.1  # Nominatim usage policy: max 1 req/s

    # --- LLM -----------------------------------------------------------------
    llm_provider: str = "azure_openai"  # azure_openai | anthropic
    # Azure OpenAI (v1 API). Leave the key empty to use managed identity / Entra ID.
    azure_openai_endpoint: str = ""     # https://<resource>.openai.azure.com
    azure_openai_api_key: str = ""
    azure_openai_deployment: str = ""   # the deployment name, e.g. gpt-5.4-mini
    # Anthropic (alternative)
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-5-5"
    llm_max_tokens: int = 4096
    report_language: str = "da"  # language of narrative output (da / en)

    # --- Request defaults -----------------------------------------------
    default_radius_km: float = 25.0
    default_max_competitors: int = 5
    default_industry_code_digits: int = 4  # DB25/DB07 prefix length used for matching

    @model_validator(mode="after")
    def _writable_paths(self) -> "Settings":
        # Function apps run from a read-only package: keep cache/db under /tmp.
        if self.job_backend == "azure":
            tmp = Path(tempfile.gettempdir())
            if self.cache_dir == str(ROOT / "data" / "cache"):
                self.cache_dir = str(tmp / "competitor-intel-cache")
            if self.db_path == str(ROOT / "data" / "jobs.sqlite3"):
                self.db_path = str(tmp / "jobs.sqlite3")
        return self

    @property
    def storage_connection(self) -> str:
        return self.jobs_storage_connection or self.azurewebjobsstorage

    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
