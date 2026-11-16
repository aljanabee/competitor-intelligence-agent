# Connects the deployed function app to Azure OpenAI and publishes the latest code.
# Run from the project folder:
#   powershell -ExecutionPolicy Bypass -File .\setup-azure-openai.ps1

$RG    = "rg-competitor-intel"
$APP   = "func-competitor-intel-99256"
$AOAI  = "aoai-competitor-intel-99256"
$DEPLOYMENT = "competitor-llm"
$MODEL = "gpt-5-mini"
$MODEL_VERSION = "2025-08-07"
$SKU   = "GlobalStandard"
$CAPACITY = 100
$ENDPOINT = "https://$AOAI.openai.azure.com/"

function Step($text) { Write-Host "`n=== $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "`nFAILED: $text" -ForegroundColor Red; exit 1 }

Step "1/5 Model deployment '$DEPLOYMENT' ($MODEL, $SKU)"
$existing = az cognitiveservices account deployment show --name $AOAI --resource-group $RG --deployment-name $DEPLOYMENT --query "properties.provisioningState" -o tsv 2>$null
if ($existing -eq "Succeeded") {
    Write-Host "Already deployed - skipping."
} else {
    az cognitiveservices account deployment create --name $AOAI --resource-group $RG `
        --deployment-name $DEPLOYMENT --model-name $MODEL --model-version $MODEL_VERSION `
        --model-format OpenAI --sku-name $SKU --sku-capacity $CAPACITY --query "properties.provisioningState" -o tsv
    if ($LASTEXITCODE -ne 0) { Fail "model deployment (see the error above)" }
}

Step "2/5 Reading the Azure OpenAI key"
$AOAI_KEY = az cognitiveservices account keys list --name $AOAI --resource-group $RG --query key1 -o tsv
if ($LASTEXITCODE -ne 0 -or -not $AOAI_KEY) { Fail "could not read the key" }
Write-Host "Key read (not shown)."

Step "3/5 Configuring the function app"
az functionapp config appsettings set --resource-group $RG --name $APP --settings `
    LLM_PROVIDER=azure_openai AZURE_OPENAI_ENDPOINT=$ENDPOINT AZURE_OPENAI_DEPLOYMENT=$DEPLOYMENT `
    AZURE_OPENAI_API_KEY=$AOAI_KEY LLM_MAX_TOKENS=16000 --query "[].name" -o tsv | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "setting app settings" }
Write-Host "Settings saved."

Step "4/5 Publishing the code (2-5 minutes)"
func azure functionapp publish $APP --python
if ($LASTEXITCODE -ne 0) { Fail "publish (see the error above)" }

Step "5/5 Health check"
Start-Sleep -Seconds 20
try {
    $h = Invoke-RestMethod -Uri "https://$APP.azurewebsites.net/health" -TimeoutSec 60
    $h | ConvertTo-Json -Depth 5
    if ($h.llm_provider -eq "azure_openai" -and -not $h.llm_problem) {
        Write-Host "`nDONE - Azure OpenAI is connected." -ForegroundColor Green
    } else {
        Write-Host "`nPublished, but the LLM reports a problem: $($h.llm_problem)" -ForegroundColor Yellow
    }
} catch {
    Write-Host "Health check did not answer yet - open https://$APP.azurewebsites.net/health in a minute." -ForegroundColor Yellow
}
