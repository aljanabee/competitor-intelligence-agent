"""Address -> coordinates. Default: OpenStreetMap Nominatim (free, 1 req/s).

DAWA was shut down in 2026; to use a commercial Danish geocoder instead,
implement `Geocoder.geocode` against it and keep the same signature.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from app.config import Settings
from app.util import FileCache


class Geocoder:
    def __init__(self, settings: Settings):
        self.s = settings
        self.cache = FileCache(settings.cache_dir, 24 * 90)  # addresses rarely move
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def geocode(self, address: str) -> tuple[float, float] | None:
        cached = self.cache.get("geo", address)
        if cached is not None:
            return tuple(cached) if cached else None
        async with self._lock:  # respect rate limit
            wait = self.s.geocoder_min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            async with httpx.AsyncClient(timeout=20, headers={"User-Agent": self.s.geocoder_user_agent}) as c:
                r = await c.get(self.s.geocoder_url, params={
                    "q": address, "format": "json", "limit": 1, "countrycodes": "dk"})
            self._last = time.monotonic()
        r.raise_for_status()
        data = r.json()
        result = (float(data[0]["lat"]), float(data[0]["lon"])) if data else None
        self.cache.set("geo", address, list(result) if result else [])
        return result
