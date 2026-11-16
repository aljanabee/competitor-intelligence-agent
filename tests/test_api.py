"""End-to-end API tests in mock mode (no external calls)."""
import os
import time

import pytest

os.environ["MOCK_PROVIDERS"] = "true"
os.environ["API_KEYS"] = "test-key"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("JOB_BACKEND", "local")
    from app.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from app.main import app, reset_state

    reset_state()

    with TestClient(app, headers={"X-API-Key": "test-key"}) as c:
        yield c


def test_auth_required(client):
    r = client.get("/v1/config/defaults", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


def test_validation(client):
    r = client.post("/v1/analyses", json={"target": {}})
    assert r.status_code == 422
    r = client.post("/v1/analyses", json={"target": {"cvr": "123"}})
    assert r.status_code == 422
    r = client.post("/v1/analyses", json={"target": {"name": "x"}, "competitors": {"mode": "manual"}})
    assert r.status_code == 422


def test_discover_auto(client):
    r = client.post("/v1/competitors/discover", json={
        "target": {"cvr": "30000001"},
        "competitors": {"radius_km": 40, "max_competitors": 4, "industry_keywords": ["restaurant"]}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert 1 <= len(body["competitors"]) <= 4
    assert all(c["distance_km"] is None or c["distance_km"] <= 40 for c in body["competitors"])
    assert body["target"]["cvr"] not in {c["cvr"] for c in body["competitors"]}


def test_sync_manual_with_component_toggles(client):
    r = client.post("/v1/analyses/sync", json={
        "target": {"domain": "https://www.mitfirma.dk", "name": "Mit Firma ApS"},
        "competitors": {"mode": "manual", "manual": [{"domain": "konkurrent1.dk"}, {"domain": "konkurrent2.dk"}]},
        "components": {"financials": True, "website": True, "trustpilot": True, "social": False}})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["target"]["company"]["domain"] == "mitfirma.dk"
    assert [c["company"]["domain"] for c in res["competitors"]] == ["konkurrent1.dk", "konkurrent2.dk"]
    assert res["target"]["social"] is None  # disabled component is not run
    assert res["target"]["trustpilot"]["status"] == "ok"
    assert res["target"]["financials"]["data"]["kpis"]["revenue"] > 0
    metrics = {m["metric"] for m in res["comparison"]["metrics"]}
    assert {"revenue", "trustpilot_avg_rating"} <= metrics
    assert "executive_summary" in res["synthesis"]


def test_async_job_both_mode(client):
    r = client.post("/v1/analyses", json={
        "target": {"cvr": "30000001"},
        "competitors": {"mode": "both", "manual": [{"domain": "valgt-konkurrent.dk"}],
                        "max_competitors": 3, "industry_keywords": ["restaurant"]},
        "options": {"social_platforms": ["linkedin", "instagram"], "max_reviews": 20}})
    assert r.status_code == 202
    job_id = r.json()["id"]
    for _ in range(100):
        job = client.get(f"/v1/analyses/{job_id}").json()
        if job["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert job["status"] == "completed", job.get("error")
    res = job["result"]
    comps = res["competitors"]
    assert comps[0]["company"]["domain"] == "valgt-konkurrent.dk" and comps[0]["company"]["source"] == "manual"
    assert len(comps) == 3
    social = res["target"]["social"]
    assert social["status"] == "ok" and set(social["data"]["platforms"]) == {"linkedin"}  # mock site links LI+FB
    assert "instagram" in social["data"]["missing_platforms"]
    assert job["progress"][-1].endswith("Done")
    assert any(j["id"] == job_id for j in client.get("/v1/analyses").json())
    assert client.get(f"/v1/analyses/%20%20{job_id}%20").json()["id"] == job_id  # stray spaces ignored


def test_demo_page_served_without_key(client):
    r = client.get("/demo", headers={"X-API-Key": ""})
    assert r.status_code == 200 and "Konkurrentanalyse" in r.text
    assert client.get("/", follow_redirects=False).headers["location"] == "/demo"
