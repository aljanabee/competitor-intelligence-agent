"""Azure Functions path: POST -> blob + queue message -> queue function -> completed job.

Blob and queue clients are replaced by in-memory fakes, so no Azure account is needed.
"""
import asyncio
import base64
import json
import os
import time
from types import SimpleNamespace

import pytest

os.environ["MOCK_PROVIDERS"] = "true"
os.environ["API_KEYS"] = "test-key"


class FakeContainer:
    def __init__(self):
        self.blobs: dict[str, tuple[str, dict]] = {}

    def create_container(self):
        pass

    def upload_blob(self, name, data, overwrite=False, metadata=None):
        self.blobs[name] = (data, metadata or {})

    def download_blob(self, name):
        from azure.core.exceptions import ResourceNotFoundError

        if name not in self.blobs:
            raise ResourceNotFoundError("missing")
        return SimpleNamespace(readall=lambda: self.blobs[name][0])

    def list_blobs(self, include=None):
        return [SimpleNamespace(name=n, metadata=m) for n, (_, m) in self.blobs.items()]


class FakeQueue:
    def __init__(self):
        self.messages: list[str] = []

    def create_queue(self):
        pass

    def send_message(self, content):
        self.messages.append(content)


class FakeQueueMessage:
    def __init__(self, body: str):
        self._body, self.dequeue_count = body.encode(), 1

    def get_body(self):
        return self._body


@pytest.fixture()
def azure_env(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_BACKEND", "azure")
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    container, queue = FakeContainer(), FakeQueue()
    import app.jobs as jobs

    monkeypatch.setattr(jobs, "_azure_clients", lambda s: (container, queue))
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import reset_state

    reset_state()
    yield container, queue
    reset_state()
    get_settings.cache_clear()


def test_settings_use_tmp_on_azure(azure_env):
    from app.config import get_settings

    s = get_settings()
    assert s.job_backend == "azure" and "/data/" not in s.db_path


def test_post_enqueues_and_queue_function_completes_job(azure_env):
    container, queue = azure_env
    from fastapi.testclient import TestClient

    import function_app
    from app.main import app

    with TestClient(app, headers={"X-API-Key": "test-key"}) as c:
        r = c.post("/v1/analyses", json={"target": {"cvr": "30000001"},
                                          "competitors": {"max_competitors": 2, "industry_keywords": ["cafe"]}})
        assert r.status_code == 202
        job_id = r.json()["id"]
        assert json.loads(queue.messages[0]) == {"job_id": job_id}
        assert c.get(f"/v1/analyses/{job_id}").json()["status"] == "queued"

        # what the Functions host does when the message arrives
        asyncio.run(function_app.run_analysis_job(FakeQueueMessage(queue.messages[0])))

        job = c.get(f"/v1/analyses/{job_id}").json()
        assert job["status"] == "completed", job.get("error")
        assert len(job["result"]["competitors"]) == 2
        assert c.get("/v1/analyses").json()[0]["id"] == job_id
        assert c.get("/health").json()["job_backend"] == "azure"

    # duplicate delivery is a no-op
    before = container.blobs[f"{job_id}.json"][0]
    asyncio.run(function_app.run_analysis_job(FakeQueueMessage(queue.messages[0])))
    assert container.blobs[f"{job_id}.json"][0] == before


def test_function_app_registers_http_and_queue_functions(azure_env):
    import function_app

    names = {f.get_function_name() for f in function_app.app.get_functions()}
    assert "run_analysis_job" in names
    assert any("http" in n.lower() or "asgi" in n.lower() for n in names - {"run_analysis_job"}), names


def test_queue_client_uses_base64(monkeypatch):
    from app.config import Settings
    from app.jobs import _azure_clients

    s = Settings(job_backend="azure", jobs_storage_connection=
                 "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey="
                 + base64.b64encode(b"k" * 64).decode()
                 + ";BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
                   "QueueEndpoint=http://127.0.0.1:10001/devstoreaccount1;")
    _, q = _azure_clients(s)
    assert type(q._message_encode_policy).__name__ == "TextBase64EncodePolicy"
