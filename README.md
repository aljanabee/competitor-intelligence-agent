# Competitor Intelligence Agent

An API service that analyses a company against competitors in the same industry within a
configurable radius. It covers four sources, and each one can be turned on or off per request:

| Component | Source | What you get |
|---|---|---|
| `financials` | CVR (Erhvervsstyrelsen), XBRL annual reports | Revenue, gross profit, net profit, equity, employees, growth, margins, equity ratio, multi-year history, links to report PDFs |
| `website` | Apify `website-content-crawler` + LLM | Value proposition, offerings, target customers, USPs, pricing signals, trust signals, weaknesses, page structure. It also finds the company's social and Trustpilot profiles |
| `trustpilot` | Apify Trustpilot actor + LLM | Rating stats, distribution, % negative, reply rate, 90-day trend, and voice of customer |
| `social` | Apify LinkedIn / Facebook / Instagram actors + LLM | Posting frequency, engagement, and voice of customer, with audience comments weighted above the company's own posts |

**Voice of customer** (for both Trustpilot and social) answers the four questions in the brief:
overall perception and business performance, the customer satisfaction level, complaints and
recurring issues, and positive feedback and success stories. It also adds a sentiment score and
business signals.

The result has three parts. Each company gets its own analysis. A **comparison matrix** ranks
the target on every metric (leader / above median / below median / laggard). A **synthesis**
gives an executive summary, a SWOT, the threat level of each competitor, and prioritised
recommendations tied to evidence.

## Competitor selection

| `competitors.mode` | Behaviour |
|---|---|
| `auto` | Discovers competitors by industry and location |
| `manual` | Uses only the domains / CVR numbers / names you list |
| `both` | Uses your list first, then fills up to `max_competitors` with discovered ones |

Auto-discovery `strategy`:
- **`google_maps`**: searches Apify Google Maps for `industry_keywords` within a true radius around the target. Good for local businesses and returns websites.
- **`cvr`**: finds companies with the same industry code (first `DEFAULT_INDUSTRY_CODE_DIGITS` digits), pre-filters them by postcode, geocodes them, and filters by exact distance. Good for B2B companies without a storefront.
- **`combined`** (default): runs both, then de-duplicates and ranks by whether the company has a website, then distance, then size.

Google Maps hits are matched back to CVR so their financials are available too.
`POST /v1/competitors/discover` previews the selection cheaply before you pay for a full analysis.

## API

| Method | Path | |
|---|---|---|
| `POST` | `/v1/analyses` | Starts a job and returns `202` with `id`. Optional `webhook_url` is POSTed the finished job |
| `GET` | `/v1/analyses/{id}` | Status, progress log, and result |
| `GET` | `/v1/analyses` | Recent jobs |
| `POST` | `/v1/analyses/sync` | Runs inline. Only for small runs or mock mode |
| `POST` | `/v1/competitors/discover` | Preview the competitor selection only |
| `GET` | `/v1/config/defaults` | Default parameters and allowed values |
| `GET` | `/health` | Which sources are configured |

Auth uses the `X-API-Key` header, with keys set in `API_KEYS`. Interactive OpenAPI docs are at `/docs`.

Minimal request:
```json
{ "target": { "cvr": "12345678" },
  "competitors": { "radius_km": 15, "industry_keywords": ["frisør"] } }
```
For every option, see [`examples/request_full.json`](examples/request_full.json).

## Run

```bash
pip install -r requirements-dev.txt
cp .env.example .env          # fill in keys, or set MOCK_PROVIDERS=true
uvicorn app.main:app --reload # http://localhost:8000/docs
pytest -q                     # 17 tests, all offline
```
Or with Docker: `docker build -t competitor-intel . && docker run -p 8000:8000 --env-file .env -v $PWD/data:/data competitor-intel`

**Azure Functions:** see [`DEPLOY_AZURE.md`](DEPLOY_AZURE.md). The same code runs there as an
HTTP function (FastAPI via `AsgiFunctionApp`) plus a queue-triggered function that executes
analyses; set `JOB_BACKEND=azure`.

**Mock mode** (`MOCK_PROVIDERS=true`) runs the whole pipeline on synthetic data with no keys and
no cost. Use it to build integrations against the real response shape.

## Credentials

- **Apify**: `APIFY_TOKEN`. Each source is an actor in [`actors.yaml`](actors.yaml). To switch
  actors, edit the `actor` and `input` template and map the output with `fields`. No code changes
  are needed. The Trustpilot and LinkedIn actors are community-maintained, so check their input
  schema and price in the Apify Console before going to production.
- **CVR**: system-to-system access is free but needs an application to `cvrselvbetjening@erst.dk`.
  Without it, financials and CVR discovery are skipped and Google Maps discovery still works.
- **LLM: Azure OpenAI** (default, `LLM_PROVIDER=azure_openai`): `AZURE_OPENAI_ENDPOINT`,
  `AZURE_OPENAI_DEPLOYMENT` and either `AZURE_OPENAI_API_KEY` or managed identity (role
  *Cognitive Services OpenAI User*). `LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY` uses Claude instead.
  Without an LLM you still get every quantitative metric and the comparison matrix, but no
  qualitative insights or synthesis.

Any missing source degrades gracefully. The response says what was skipped and why in each
component's `status`/`error` and in `warnings`.

## Design notes

- **Fault isolation**: every component for every company runs independently. A failing scrape
  shows up as `status: error` and does not fail the job.
- **Cost control**: Apify and CVR responses are cached on disk (`CACHE_TTL_HOURS`). The website
  crawl runs first and supplies social and Trustpilot URLs. If the website component is off, the
  homepage is fetched directly to find them, with no Apify cost.
- **Structured LLM output**: the model is called with forced function/tool calling and a JSON schema, so the
  response shape is stable for downstream systems.
- **Jobs**: with `JOB_BACKEND=local` they persist in SQLite and run as in-process tasks (jobs
  running at a restart are marked failed). With `JOB_BACKEND=azure` they are stored as blobs and
  executed from an Azure Storage queue, which scales across instances.
- **Geocoding** uses OSM Nominatim at 1 request/second. DAWA shut down in 2026. For volume, plug
  a commercial Danish geocoder into `app/providers/geo.py`.
- **Financials**: Danish class-B companies often report gross profit, not revenue, so gross-profit
  KPIs are computed alongside revenue. Only XML XBRL is parsed. iXBRL-only filings (large/listed
  companies) return the report links without figures.

## Layout
```
app/
  main.py          API routes + auth
  models.py        request/response schemas (the API contract)
  pipeline.py      orchestration: resolve → discover → analyse (parallel) → compare → synthesise
  discovery.py     target resolution + auto/manual competitor selection
  report.py        comparison matrix + synthesis
  jobs.py          async jobs, SQLite store, webhooks
  analyzers/       financials · website · trustpilot · social
  providers/       apify · cvr · geo · llm · mock
actors.yaml        Apify actor configuration
function_app.py    Azure Functions entry point (HTTP + queue trigger)
host.json          Azure Functions host config
```
