"""Synthetic providers for MOCK_PROVIDERS=true.

Lets you exercise the full API (discovery, toggles, jobs, report shape)
without any API keys or cost. Data is deterministic per company.
"""
from __future__ import annotations

import hashlib
import random
from datetime import date, timedelta
from typing import Any

from app.models import Company

_BASE = (56.1572, 10.2107)  # Aarhus C
_NAMES = ["Nordlys", "Havblik", "Skovbryn", "Kystlinjen", "Engblomst", "Fjordglimt", "Bakkedal", "Søbredden"]


def _rng(*parts: Any) -> random.Random:
    return random.Random(int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:8], 16))


class MockGeocoder:
    async def geocode(self, address: str) -> tuple[float, float] | None:
        r = _rng("geo", address)
        return _BASE[0] + r.uniform(-0.3, 0.3), _BASE[1] + r.uniform(-0.5, 0.5)


class MockCVR:
    enabled = True

    def _company(self, i: int, code: str = "561010", text: str = "Restauranter") -> Company:
        r = _rng("cvr", i)
        name = f"{_NAMES[i % len(_NAMES)]} {text.split()[0]} ApS"
        return Company(
            name=name, cvr=str(30000000 + i), domain=f"{_NAMES[i % len(_NAMES)].lower()}.dk",
            address=f"Testvej {i + 1}, {8000 + r.randint(0, 300)} Aarhus", postcode=str(8000 + r.randint(0, 300)),
            industry_code=code, industry_text=text, employees=r.randint(3, 80), source="cvr",
        )

    async def get_by_cvr(self, cvr: str) -> Company | None:
        return self._company(int(cvr) % 1000)

    async def find(self, name=None, domain=None, postcode=None) -> Company | None:
        seed = int(hashlib.md5((domain or name or "").encode()).hexdigest()[:4], 16) % 997
        c = self._company(seed)
        return c.model_copy(update={"name": name or c.name, "domain": domain or c.domain})

    async def candidates(self, industry_codes, postcode, postcode_window, size=100) -> list[Company]:
        return [self._company(i, industry_codes[0]) for i in range(1, 9)]

    async def financials(self, cvr: str, years: int = 3) -> list[dict]:
        r = _rng("fin", cvr)
        revenue = r.uniform(5e6, 60e6)
        out = []
        for y in range(years):
            end = date(2025 - y, 12, 31)
            margin = r.uniform(-0.03, 0.12)
            out.append({
                "period_start": f"{end.year}-01-01", "period_end": end.isoformat(),
                "published": f"{end.year + 1}-05-15", "pdf_url": None, "xbrl_url": None,
                "figures": {
                    "revenue": round(revenue), "gross_profit": round(revenue * 0.4),
                    "net_profit": round(revenue * margin), "equity": round(revenue * r.uniform(0.1, 0.5)),
                    "total_assets": round(revenue * r.uniform(0.5, 1.2)), "employees": r.randint(5, 60),
                },
            })
            revenue /= 1 + r.uniform(-0.1, 0.2)
        return out


class MockApify:
    enabled = True
    is_mock = True

    async def run(self, source: str, **p: Any) -> list[dict]:
        r = _rng(source, p.get("url") or p.get("domain") or p.get("query"))
        today = date(2026, 11, 1)
        if source == "google_maps":
            lat, lng = p["lat"], p["lng"]
            return [{
                "name": f"{_NAMES[i]}s {p['query'].title()} & Co", "website": f"https://{_NAMES[i].lower()}-{i}.dk",
                "lat": lat + r.uniform(-0.15, 0.15), "lng": lng + r.uniform(-0.25, 0.25),
                "address": f"Mockgade {i}, 8000 Aarhus", "postcode": "8000", "category": p["query"],
                "rating": round(r.uniform(3.4, 4.9), 1), "reviews_count": r.randint(5, 400),
            } for i in range(min(int(p.get("limit", 8)), 8))]
        if source == "website":
            dom = p["url"].split("//")[-1].strip("/")
            pages = ["", "om-os", "priser", "kontakt", "blog"]
            return [{"url": f"https://{dom}/{pg}", "title": f"{dom} {pg}", "description": "Mock page",
                     "text": f"Velkommen til {dom}. Vi tilbyder kvalitet og service. "
                             f"Følg os på https://www.linkedin.com/company/{dom.split('.')[0]} "
                             f"og https://www.facebook.com/{dom.split('.')[0]}"} for pg in pages]
        if source == "trustpilot":
            good = ["Super service og hurtig levering", "Venligt personale, kommer igen", "God kvalitet til prisen"]
            bad = ["Lang ventetid på svar", "Varen kom for sent", "Kundeservice svarede ikke"]
            items = []
            for i in range(min(int(p.get("limit", 40)), 40)):
                stars = r.choices([1, 2, 3, 4, 5], weights=[1, 1, 1, 3, 5])[0]
                items.append({"rating": stars, "title": None, "text": r.choice(good if stars >= 4 else bad),
                              "date": (today - timedelta(days=r.randint(0, 365))).isoformat(),
                              "reply": "Tak for din anmeldelse" if r.random() < 0.4 else None,
                              "trust_score": round(r.uniform(3.2, 4.7), 1), "total_reviews": r.randint(40, 900)})
            return items
        if source in ("linkedin", "facebook", "instagram"):
            texts = ["Vi har ansat nye kolleger!", "Ny kampagne er i gang", "Tak for en fantastisk messe",
                     "Kunde-case: sådan hjalp vi", "Vi fejrer 10 år"]
            items = []
            for i in range(min(int(p.get("limit", 15)), 15)):
                it = {"text": r.choice(texts), "date": (today - timedelta(days=i * r.randint(3, 12))).isoformat(),
                      "likes": r.randint(2, 300), "comments": r.randint(0, 40), "shares": r.randint(0, 20),
                      "url": f"{p['url']}/posts/{i}"}
                if source == "instagram":
                    it["latest_comments"] = [{"text": "Elsker det 😍"}, {"text": "Hvornår åbner I igen?"}]
                items.append(it)
            return items
        if source == "facebook_comments":
            return [{"text": t, "date": today.isoformat(), "likes": r.randint(0, 9)}
                    for t in ["Rigtig god oplevelse", "Ingen svar på min mail", "Anbefales!"]]
        return []
