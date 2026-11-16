"""Generic Apify actor runner driven by actors.yaml."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx
import yaml

from app.config import Settings
from app.util import FileCache

log = logging.getLogger(__name__)
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


class ApifyError(RuntimeError):
    pass


def render(template: Any, params: dict[str, Any]) -> Any:
    """Fill {{placeholders}} in a nested input template."""
    if isinstance(template, dict):
        return {k: render(v, params) for k, v in template.items()}
    if isinstance(template, list):
        out: list[Any] = []
        for v in template:
            r = render(v, params)
            # a list placeholder inside a list is spliced, e.g. ["{{urls}}"]
            if isinstance(v, str) and _PLACEHOLDER.fullmatch(v) and isinstance(r, list):
                out.extend(r)
            else:
                out.append(r)
        return out
    if isinstance(template, str):
        m = _PLACEHOLDER.fullmatch(template)
        if m:
            return params.get(m.group(1))
        return _PLACEHOLDER.sub(lambda mm: str(params.get(mm.group(1), "")), template)
    return template


def get_path(item: dict, path: str) -> Any:
    cur: Any = item
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def pick(item: dict, candidates: list[str]) -> Any:
    for c in candidates:
        v = get_path(item, c)
        if v not in (None, "", [], {}):
            return v
    return None


def normalize(items: list[dict], fields: dict[str, list[str]]) -> list[dict]:
    out = []
    for it in items:
        row = {k: pick(it, cands) for k, cands in fields.items() if k != "_keys"}
        if "_keys" in fields and isinstance(it, dict):
            row["_keys"] = sorted(it.keys())[:40]  # diagnostics: what the actor really returned
        out.append(row)
    return out


class ApifyClient:
    def __init__(self, settings: Settings):
        self.s = settings
        with open(settings.apify_actors_file) as f:
            self.actors: dict[str, dict] = yaml.safe_load(f)
        self.cache = FileCache(settings.cache_dir, settings.cache_ttl_hours)
        self._sem = asyncio.Semaphore(max(1, settings.apify_max_concurrent_runs))

    @property
    def enabled(self) -> bool:
        return bool(self.s.apify_token)

    async def run(self, source: str, **params: Any) -> list[dict]:
        """Run the actor configured for `source`; returns normalised items."""
        cfg = self.actors.get(source)
        if not cfg:
            raise ApifyError(f"no actor configured for source '{source}'")
        if not self.enabled:
            raise ApifyError("APIFY_TOKEN not configured")
        run_input = render(cfg["input"], params)
        cache_key = {"actor": cfg["actor"], "input": run_input}
        raw = self.cache.get("apify", cache_key)
        if raw is None:
            async with self._sem:  # stay under the account's total memory limit
                raw = await self._run_actor(cfg["actor"], run_input, cfg.get("memory_mb"))
            self.cache.set("apify", cache_key, raw)
        fields = cfg.get("fields")
        return normalize(raw, fields) if fields else raw

    async def _run_actor(self, actor: str, run_input: dict, memory_mb: int | None = None) -> list[dict]:
        actor_id = actor.replace("/", "~")
        headers = {"Authorization": f"Bearer {self.s.apify_token}"}
        base = self.s.apify_base_url
        params = {"memory": memory_mb} if memory_mb else {}
        async with httpx.AsyncClient(timeout=60, headers=headers) as c:
            for attempt in range(6):
                r = await c.post(f"{base}/acts/{actor_id}/runs", json=run_input, params=params)
                if r.status_code == 402 and "memory-limit" in r.text and attempt < 5:
                    # other runs (ours or the account's) still hold memory - wait and retry
                    log.info("apify memory cap reached for %s, retrying in 30s", actor)
                    await asyncio.sleep(30)
                    continue
                break
            if r.status_code >= 400:
                raise ApifyError(f"{actor}: start failed {r.status_code} {r.text[:300]}")
            run = r.json()["data"]
            run_id, dataset_id = run["id"], run["defaultDatasetId"]
            log.info("apify run started actor=%s run=%s", actor, run_id)

            waited = 0
            status = run["status"]
            while status in ("READY", "RUNNING"):
                # waitForFinish long-polls up to 60s server-side
                r = await c.get(f"{base}/actor-runs/{run_id}", params={"waitForFinish": 60}, timeout=90)
                r.raise_for_status()
                status = r.json()["data"]["status"]
                waited += 60
                if waited > self.s.apify_run_timeout_s:
                    await c.post(f"{base}/actor-runs/{run_id}/abort")
                    raise ApifyError(f"{actor}: timed out after {waited}s")
                await asyncio.sleep(0)
            if status != "SUCCEEDED":
                raise ApifyError(f"{actor}: run ended with status {status}")

            r = await c.get(f"{base}/datasets/{dataset_id}/items", params={"clean": "true", "format": "json"})
            r.raise_for_status()
            return r.json()
