import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def normalize_domain(value: str) -> str:
    v = value.strip().lower()
    if "://" not in v:
        v = "http://" + v
    host = urlparse(v).hostname or ""
    return host.removeprefix("www.")


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_LEGAL_SUFFIX = re.compile(r"\b(aps|a/s|as|ivs|i/s|k/s|p/s|smba|amba|holding)\b\.?", re.I)


def normalize_name(name: str) -> str:
    n = _LEGAL_SUFFIX.sub("", name.lower())
    return re.sub(r"[^a-z0-9æøå]+", " ", n).strip()


class FileCache:
    """Tiny JSON file cache with TTL - avoids paying twice for the same scrape."""

    def __init__(self, directory: str, ttl_hours: int):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_hours * 3600

    def _path(self, namespace: str, key: Any) -> Path:
        h = hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:32]
        return self.dir / f"{namespace}-{h}.json"

    def get(self, namespace: str, key: Any) -> Any | None:
        p = self._path(namespace, key)
        if p.exists() and time.time() - p.stat().st_mtime < self.ttl:
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                return None
        return None

    def set(self, namespace: str, key: Any, value: Any) -> None:
        self._path(namespace, key).write_text(json.dumps(value, default=str))
