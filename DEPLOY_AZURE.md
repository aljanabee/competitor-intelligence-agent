# Deploying to Azure Functions

The API runs as a Python Azure Function app on the **Flex Consumption** plan:

```
Client ──HTTPS──▶ http_app_func (FastAPI via AsgiFunctionApp)
                     │  POST /v1/analyses
                     ├─▶ Blob Storage  jobs/<id>.json      (job status + result)
                     └─▶ Storage queue analysis-jobs ──▶ run_analysis_job (queue trigger)
                                                             └─ runs the analysis, writes the
                                                                result to the blob, calls webhook
```

Why the queue: an HTTP-triggered function must respond within 230 seconds, and a full
analysis takes several minutes. So `POST /v1/analyses` only stores the job and enqueues it;
the queue-triggered function does the work (timeout set to 30 min in `host.json`).
Clients poll `GET /v1/analyses/{id}` or receive the `webhook_url` callback, exactly as before.

## Prerequisites

| Tool | Check |
|---|---|
| Azure subscription with permission to create resources | `az account show` |
| Azure CLI | `az version` |
| Azure Functions Core Tools v4 | `func --version` (install: `npm i -g azure-functions-core-tools@4`) |
| Python 3.11 | `python --version` |

## 1. Choose names and a region

```bash
az login
az functionapp list-flexconsumption-locations --query "sort_by(@, &name)[].{Region:name}" -o table
```

Pick a region from the list (e.g. `swedencentral` or `northeurope`), then set variables.
Storage account names: 3–24 lowercase letters/digits, globally unique. App names: globally unique.

```bash
# bash / zsh
RG=rg-competitor-intel
REGION=swedencentral
STORAGE=stcompintel$RANDOM
APP=func-competitor-intel-$RANDOM
```
```powershell
# PowerShell
$RG="rg-competitor-intel"; $REGION="swedencentral"
$STORAGE="stcompintel$(Get-Random -Maximum 99999)"; $APP="func-competitor-intel-$(Get-Random -Maximum 99999)"
```

## 2. Create the resources

```bash
az group create --name $RG --location $REGION

az storage account create --name $STORAGE --resource-group $RG --location $REGION \
  --sku Standard_LRS --allow-blob-public-access false

az functionapp create --resource-group $RG --name $APP --storage-account $STORAGE \
  --flexconsumption-location $REGION --runtime python --runtime-version 3.11
```

The same storage account holds the Functions runtime data, the `analysis-jobs` queue and the
`jobs` blob container. The app creates the queue and container on first use.

## 3. Configure app settings

```bash
az functionapp config appsettings set --resource-group $RG --name $APP --settings \
  JOB_BACKEND=azure \
  MOCK_PROVIDERS=false \
  API_KEYS="<long-random-key-1>,<key-for-another-client>" \
  APIFY_TOKEN="<apify-token>" \
  LLM_PROVIDER=azure_openai \
  AZURE_OPENAI_ENDPOINT="https://<openai-resource>.openai.azure.com" \
  AZURE_OPENAI_DEPLOYMENT="<deployment-name>" \
  AZURE_OPENAI_API_KEY="<azure-openai-key>" \
  CVR_USERNAME="<cvr-user>" CVR_PASSWORD="<cvr-password>" \
  REPORT_LANGUAGE=da
```

Generate API keys with e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

Tip: for a first smoke test set `MOCK_PROVIDERS=true` and leave the provider keys empty.

**Secrets in Key Vault (recommended for production).** Store the tokens in Azure Key Vault and
reference them instead of pasting values:
`AZURE_OPENAI_API_KEY=@Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/azure-openai-key/)`.
Give the function app's managed identity the *Key Vault Secrets User* role on the vault.

**Storage with managed identity.** If your app uses identity-based storage
(`AzureWebJobsStorage__accountName` instead of a connection string), the code picks that up
automatically. Grant the app's identity *Storage Blob Data Contributor* and
*Storage Queue Data Contributor* on the storage account.

## 3b. Azure OpenAI (the LLM)

The qualitative analysis and synthesis use an Azure OpenAI model deployment.

```bash
AOAI=aoai-competitor-intel-$RANDOM
az provider register --namespace Microsoft.CognitiveServices
az cognitiveservices account create --name $AOAI --resource-group $RG --location $REGION \
  --kind OpenAI --sku S0 --custom-domain $AOAI

# pick a model + version available in your region (a "mini" model keeps cost low)
az cognitiveservices account list-models --name $AOAI --resource-group $RG \
  --query "[?format=='OpenAI'].{model:name, version:version}" -o table

az cognitiveservices account deployment create --name $AOAI --resource-group $RG \
  --deployment-name competitor-llm --model-name <model> --model-version <version> \
  --model-format OpenAI --sku-name DataZoneStandard --sku-capacity 50

az cognitiveservices account show --name $AOAI --resource-group $RG --query properties.endpoint -o tsv
az cognitiveservices account keys list --name $AOAI --resource-group $RG --query key1 -o tsv
```

Put the endpoint, `competitor-llm` and the key into the app settings in step 3
(`AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_KEY`).

- `DataZoneStandard` keeps processing inside the EU data zone. If your subscription has no quota
  for it, use `GlobalStandard` (cheaper and more widely available, but data may be processed
  in any region) or `Standard` (single region).
- `--sku-capacity 50` = 50,000 tokens per minute. Plenty for a few analyses at a time.
- **Without a key (managed identity):** leave `AZURE_OPENAI_API_KEY` empty, then
  `az functionapp identity assign -g $RG -n $APP` and grant that identity the
  *Cognitive Services OpenAI User* role on the OpenAI resource.

## 4. Deploy the code

From the project folder (the one containing `function_app.py` and `host.json`):

```bash
func azure functionapp publish $APP
```

This uploads the code and installs `requirements.txt` with a remote build. `.funcignore`
keeps tests, local data and secrets out of the package. When it finishes it lists two
functions: `http_app_func` (all HTTP routes) and `run_analysis_job` (queue).

## 5. Verify

```bash
URL=https://$APP.azurewebsites.net        # the publish output prints the exact host name
curl $URL/health
# {"status":"ok","mock_mode":false,"job_backend":"azure","sources":{...}}

curl -X POST $URL/v1/analyses -H "X-API-Key: <key>" -H "Content-Type: application/json" \
  -d '{"target":{"cvr":"12345678"},"competitors":{"radius_km":15,"industry_keywords":["frisør"]}}'
# -> 202 {"id":"...","status":"queued",...}

curl -H "X-API-Key: <key>" $URL/v1/analyses/<id>
```

Interactive docs: `$URL/docs`. Logs and failures: the app's **Application Insights** →
*Logs* / *Failures*, or `func azure functionapp logstream $APP`.

## Run the Functions version locally

```bash
npm i -g azurite && azurite --silent --location ./data/azurite &   # local storage emulator
cp local.settings.json.example local.settings.json                  # mock mode by default
func start
# http://localhost:7071/health  and  http://localhost:7071/docs
```

`uvicorn app.main:app --reload` still works for plain local development (`JOB_BACKEND=local`).

## Updating

Change code → `func azure functionapp publish $APP`. Change `actors.yaml` → publish again
(it is part of the package). Change settings → `az functionapp config appsettings set ...`
(the app restarts automatically).

## Operational notes

| Topic | Behaviour |
|---|---|
| HTTP limit | Requests must finish in 230 s. `POST /v1/analyses/sync` is only safe in mock mode or for very small runs |
| Job timeout | `functionTimeout` 30 min in `host.json`. Raise it if analyses with many competitors take longer |
| Retries | A job is attempted at most twice (`maxDequeueCount: 2`), then the message moves to `analysis-jobs-poison`. A job that already finished is never re-run |
| Concurrency | `batchSize: 1`, so each instance runs one analysis at a time; Flex Consumption adds instances as the queue grows |
| Cache | Apify/CVR responses are cached in the instance's `/tmp`, so cache hits only happen on a warm instance |
| Cold start | The first request after idle can take several seconds; Flex Consumption *always ready* instances remove this at a cost |
| Job retention | Job blobs are kept until deleted. Set a lifecycle management rule on the `jobs` container (e.g. delete after 90 days) to match your GDPR retention |
| Cost | Mostly Apify + Azure OpenAI usage. The function app itself costs little at low volume |
