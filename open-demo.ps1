# Opens the demo page in your browser, already logged in with the service's API key.
#   powershell -ExecutionPolicy Bypass -File .\open-demo.ps1
$RG  = "rg-competitor-intel"
$APP = "func-competitor-intel-99256"
$keys = az functionapp config appsettings list --resource-group $RG --name $APP --query "[?name=='API_KEYS'].value | [0]" -o tsv
$KEY = ($keys -split ",")[0].Trim()
if (-not $KEY) { Write-Host "No API_KEYS setting found." -ForegroundColor Red; exit 1 }
# The key travels in the #fragment, which browsers never send to the server; the page stores it locally.
$url = "https://$APP.azurewebsites.net/demo#key=" + [uri]::EscapeDataString($KEY)
Start-Process $url
Write-Host "Demo opened: https://$APP.azurewebsites.net/demo" -ForegroundColor Green
