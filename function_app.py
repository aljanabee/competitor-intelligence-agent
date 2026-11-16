"""Azure Functions entry point (Python v2 programming model).

* HTTP: every FastAPI route (/v1/..., /health, /docs) is served through AsgiFunctionApp.
* Queue: `run_analysis_job` picks up jobs enqueued by POST /v1/analyses and runs
  the full analysis, outside the 230-second HTTP limit.

Requires JOB_BACKEND=azure in the app settings.
"""
import json
import logging

import azure.functions as func

from app.config import get_settings
from app.jobs import execute_job
from app.main import app as fastapi_app
from app.main import get_state

# X-API-Key auth is enforced inside FastAPI, so the Functions-level auth is anonymous.
app = func.AsgiFunctionApp(app=fastapi_app, http_auth_level=func.AuthLevel.ANONYMOUS)

QUEUE_NAME = get_settings().jobs_queue


@app.function_name("run_analysis_job")
@app.queue_trigger(arg_name="msg", queue_name=QUEUE_NAME, connection="AzureWebJobsStorage")
async def run_analysis_job(msg: func.QueueMessage) -> None:
    job_id = json.loads(msg.get_body().decode("utf-8"))["job_id"]
    logging.info("Running analysis job %s (dequeue count %s)", job_id, msg.dequeue_count)
    state = get_state()
    await execute_job(job_id, state.store, state.providers, state.settings)
