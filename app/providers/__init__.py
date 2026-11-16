from dataclasses import dataclass

from app.config import Settings
from app.providers.apify import ApifyClient
from app.providers.cvr import CVRClient
from app.providers.geo import Geocoder
from app.providers.llm import LLM


@dataclass
class Providers:
    apify: ApifyClient
    cvr: CVRClient
    geo: Geocoder
    llm: LLM


def build_providers(settings: Settings) -> Providers:
    if settings.mock_providers:
        from app.providers.mock import MockApify, MockCVR, MockGeocoder

        return Providers(MockApify(), MockCVR(), MockGeocoder(), LLM(settings))  # type: ignore[arg-type]
    return Providers(ApifyClient(settings), CVRClient(settings), Geocoder(settings), LLM(settings))
