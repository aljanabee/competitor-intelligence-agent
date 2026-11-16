"""Job persistence and execution.

Two backends, chosen with JOB_BACKEND:

* local - jobs in SQLite, executed as in-process asyncio tasks (uvicorn, Docker).
* azure - jobs as JSON blobs in Azure Blob Storage; POST enqueues a message on an
          Azure Storage queue and a queue-triggered Azure Function executes it.
          Needed on Azure Functions: an HTTP invocation must answer within 230 s
          and in-process background tasks are not guaranteed to survive.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import httpx

from app.config import Settings
from app.models import AnalysisRequest, Job, JobStatus
from app.pipeline import run_analysis
from app.providers import Providers

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------
class JobStore(Protocol):
    def save(self, job: Job) -> None: ...
    def get(self, job_id: str) -> Job | None: ...
    def list(self, limit: int = 50) -> list[Job]: ...


class SQLiteJobStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, created_at TEXT, body TEXT)")
        # jobs interrupted by a restart are marked failed
        for (_jid, body) in self.db.execute("SELECT id, body FROM jobs").fetchall():
            job = Job.model_validate_json(body)
            if job.status in (JobStatus.queued, JobStatus.running):
                job.status, job.error = JobStatus.failed, "Interrupted by service restart"
                self.save(job)
        self.db.commit()

    def save(self, job: Job) -> None:
        job.updated_at = _now()
        self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?, ?)",
                        (job.id, job.created_at.isoformat(), job.model_dump_json()))
        self.db.commit()

    def get(self, job_id: str) -> Job | None:
        row = self.db.execute("SELECT body FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    def list(self, limit: int = 50) -> list[Job]:
        rows = self.db.execute("SELECT body FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [Job.model_validate_json(r[0]) for r in rows]


class MemoryJobStore:
    """For tests."""

    def __init__(self) -> None:
        self.jobs: dict[str, str] = {}

    def save(self, job: Job) -> None:
        job.updated_at = _now()
        self.jobs[job.id] = job.model_dump_json()

    def get(self, job_id: str) -> Job | None:
        body = self.jobs.get(job_id)
        return Job.model_validate_json(body) if body else None

    def list(self, limit: int = 50) -> list[Job]:
        jobs = [Job.model_validate_json(b) for b in self.jobs.values()]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)[:limit]


def _azure_clients(s: Settings):
    """Blob container + queue clients from a connection string or managed identity."""
    from azure.storage.blob import BlobServiceClient
    from azure.storage.queue import QueueClient, TextBase64EncodePolicy

    encode = TextBase64EncodePolicy()  # Functions queue trigger expects base64 by default
    if s.storage_connection:
        blob = BlobServiceClient.from_connection_string(s.storage_connection)
        queue = QueueClient.from_connection_string(s.storage_connection, s.jobs_queue,
                                                   message_encode_policy=encode)
    elif s.azurewebjobsstorage__accountname:
        from azure.identity import DefaultAzureCredential

        cred, acct = DefaultAzureCredential(), s.azurewebjobsstorage__accountname
        blob = BlobServiceClient(f"https://{acct}.blob.core.windows.net", credential=cred)
        queue = QueueClient(f"https://{acct}.queue.core.windows.net", s.jobs_queue, credential=cred,
                            message_encode_policy=encode)
    else:
        raise RuntimeError("JOB_BACKEND=azure needs AzureWebJobsStorage, JOBS_STORAGE_CONNECTION "
                           "or AzureWebJobsStorage__accountName")
    return blob.get_container_client(s.jobs_container), queue


class BlobJobStore:
    """One JSON blob per job: jobs/<id>.json."""

    def __init__(self, container) -> None:
        self.c = container
        try:
            self.c.create_container()
        except Exception:  # noqa: BLE001 - already exists / no permission to create
            pass

    def save(self, job: Job) -> None:
        job.updated_at = _now()
        self.c.upload_blob(f"{job.id}.json", job.model_dump_json(), overwrite=True,
                           metadata={"status": job.status.value, "created": job.created_at.isoformat()})

    def get(self, job_id: str) -> Job | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return Job.model_validate_json(self.c.download_blob(f"{job_id}.json").readall())
        except ResourceNotFoundError:
            return None

    def list(self, limit: int = 50) -> list[Job]:
        blobs = sorted(self.c.list_blobs(include=["metadata"]),
                       key=lambda b: (b.metadata or {}).get("created", ""), reverse=True)[:limit]
        jobs = [self.get(b.name.removesuffix(".json")) for b in blobs]
        return [j for j in jobs if j]


# ---------------------------------------------------------------------------
# Execution (shared by both backends)
# ---------------------------------------------------------------------------
async def execute_job(job_id: str, store: JobStore, providers: Providers, settings: Settings) -> Job | None:
    job = store.get(job_id)
    if job is None:
        log.error("job %s not found", job_id)
        return None
    if job.status in (JobStatus.completed, JobStatus.failed):
        log.info("job %s already %s - skipping", job_id, job.status.value)  # duplicate delivery
        return job
    job.status, job.progress = JobStatus.running, []
    store.save(job)

    def progress(msg: str) -> None:
        job.progress.append(f"{_now().strftime('%H:%M:%S')} {msg}")
        store.save(job)

    try:
        job.result = await run_analysis(job.request, providers, settings, progress)
        job.status = JobStatus.completed
        progress("Done")
    except Exception as e:  # noqa: BLE001
        log.exception("job %s failed", job.id)
        job.status, job.error = JobStatus.failed, f"{type(e).__name__}: {e}"
    store.save(job)
    if job.request.webhook_url:
        await notify(job)
    return job


async def notify(job: Job) -> None:
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            await c.post(str(job.request.webhook_url), content=job.model_dump_json(),
                         headers={"Content-Type": "application/json", "X-Job-Id": job.id})
    except Exception as e:  # noqa: BLE001
        log.warning("webhook for job %s failed: %s", job.id, e)


def new_job(req: AnalysisRequest) -> Job:
    return Job(id=uuid.uuid4().hex, status=JobStatus.queued, created_at=_now(), updated_at=_now(), request=req)


class LocalJobRunner:
    def __init__(self, store: JobStore, providers: Providers, settings: Settings):
        self.store, self.p, self.s = store, providers, settings
        self.sem = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._tasks: set[asyncio.Task] = set()

    def submit(self, req: AnalysisRequest) -> Job:
        job = new_job(req)
        self.store.save(job)
        t = asyncio.create_task(self._run(job.id))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job_id: str) -> None:
        async with self.sem:
            await execute_job(job_id, self.store, self.p, self.s)


class QueueJobRunner:
    """Persists the job, then hands it to the queue-triggered function."""

    def __init__(self, store: JobStore, queue_client, settings: Settings):
        self.store, self.queue, self.s = store, queue_client, settings
        try:
            self.queue.create_queue()
        except Exception:  # noqa: BLE001 - already exists
            pass

    def submit(self, req: AnalysisRequest) -> Job:
        job = new_job(req)
        self.store.save(job)
        self.queue.send_message(json.dumps({"job_id": job.id}))
        return job


def build_job_backend(providers: Providers, settings: Settings):
    """Returns (store, runner) for the configured backend."""
    if settings.job_backend == "azure":
        container, queue = _azure_clients(settings)
        store = BlobJobStore(container)
        return store, QueueJobRunner(store, queue, settings)
    store = SQLiteJobStore(settings.db_path)
    return store, LocalJobRunner(store, providers, settings)
