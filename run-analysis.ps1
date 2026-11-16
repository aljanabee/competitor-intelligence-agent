# Starts an analysis on the deployed API, waits for it, and prints the result.
# Run from the project folder, for example:
#
#   powershell -ExecutionPolicy Bypass -File .\test-analysis.ps1
#       (small built-in test request)
#
#   powershell -ExecutionPolicy Bypass -File .\test-analysis.ps1 -Target firma.dk -Competitors konkurrent1.dk,konkurrent2.dk
#       (manual competitors)
#
#   powershell -ExecutionPolicy Bypass -File .\test-analysis.ps1 -Target firma.dk -Auto -RadiusKm 20 -Keywords "tandlæge"
#       (automatic competitor discovery)
#
# Options: -NoFinancials -NoSocial -NoWebsite -NoTrustpilot -MaxCompetitors 3 -Small
#          -BodyFile request.json   (send your own JSON request from a file)

param(
    [string]$Target = "",
    [string[]]$Competitors = @(),
    [switch]$Auto,
    [double]$RadiusKm = 25,
    [string[]]$Keywords = @(),
    [int]$MaxCompetitors = 5,
    [switch]$NoFinancials,
    [switch]$NoSocial,
    [switch]$NoWebsite,
    [switch]$NoTrustpilot,
    [switch]$Small,
    [string]$BodyFile = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RG  = "rg-competitor-intel"
$APP = "func-competitor-intel-99256"
$URL = "https://$APP.azurewebsites.net"

# ---- build the request ------------------------------------------------------
if ($BodyFile) {
    $Body = Get-Content $BodyFile -Raw -Encoding UTF8
} else {
    if (-not $Target) {
        $req = @{ target = @{ name = "Test ApS"; domain = "example.dk" };
                  competitors = @{ mode = "manual"; manual = @(@{ domain = "example.com" }) } }
    } else {
        $targetRef = if ($Target -match '^\d{8}$') { @{ cvr = $Target } } else { @{ domain = $Target } }
        $comp = @{ max_competitors = $MaxCompetitors; radius_km = $RadiusKm }
        # "-File" passes "a.dk,b.dk" as one string - split on commas ourselves
        $Competitors = @($Competitors | ForEach-Object { $_ -split "," } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        $Keywords = @($Keywords | ForEach-Object { $_ -split "," } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        $manual = @($Competitors | ForEach-Object { @{ domain = $_ } })
        if ($Auto -and $manual.Count) { $comp.mode = "both" }
        elseif ($Auto) { $comp.mode = "auto" }
        else {
            if (-not $manual.Count) { Write-Host "Give -Competitors a,b or use -Auto." -ForegroundColor Red; exit 1 }
            $comp.mode = "manual"
        }
        if ($manual.Count) { $comp.manual = $manual }
        if ($Keywords.Count) { $comp.industry_keywords = $Keywords }
        $req = @{
            target = $targetRef
            competitors = $comp
            components = @{ financials = -not $NoFinancials; social = -not $NoSocial;
                            website = -not $NoWebsite; trustpilot = -not $NoTrustpilot }
        }
        if ($Small) { $req.options = @{ max_reviews = 30; max_social_posts = 10; max_website_pages = 5 } }
    }
    $Body = $req | ConvertTo-Json -Depth 10
}
Write-Host "Request:" -ForegroundColor Cyan
Write-Host $Body

# ---- call the API -----------------------------------------------------------
Write-Host "`nReading the API key from the app settings..." -ForegroundColor Cyan
$keys = az functionapp config appsettings list --resource-group $RG --name $APP --query "[?name=='API_KEYS'].value | [0]" -o tsv
$KEY = ($keys -split ",")[0].Trim()
if (-not $KEY) { Write-Host "No API_KEYS setting found." -ForegroundColor Red; exit 1 }
$headers = @{ "X-API-Key" = $KEY }

# Windows PowerShell decodes JSON responses as Latin-1; read the bytes as UTF-8 instead (æ, ø, å).
function Get-Json($uri) {
    $r = Invoke-WebRequest -Uri $uri -Headers $headers -UseBasicParsing
    return [System.Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray()) | ConvertFrom-Json
}

Write-Host "Starting analysis..." -ForegroundColor Cyan
try {
    $job = Invoke-RestMethod -Method Post -Uri "$URL/v1/analyses" -Headers $headers `
        -ContentType "application/json; charset=utf-8" -Body ([System.Text.Encoding]::UTF8.GetBytes($Body))
} catch {
    Write-Host "The API rejected the request:" -ForegroundColor Red
    Write-Host $_.ErrorDetails.Message
    exit 1
}
Write-Host "Job id: $($job.id)"

$deadline = (Get-Date).AddMinutes(25)
do {
    Start-Sleep -Seconds 10
    $job = Get-Json "$URL/v1/analyses/$($job.id)"
    $last = if ($job.progress) { $job.progress[-1] } else { "" }
    Write-Host ("[{0}] {1}  {2}" -f (Get-Date -Format "HH:mm:ss"), $job.status, $last)
} while ($job.status -in @("queued", "running") -and (Get-Date) -lt $deadline)

$file = "result-$($job.id).json"
$job | ConvertTo-Json -Depth 50 | Out-File $file -Encoding utf8
Write-Host "`nFull result saved to $file" -ForegroundColor Cyan

if ($job.status -ne "completed") {
    Write-Host "Job ended as '$($job.status)': $($job.error)" -ForegroundColor Red
    exit 1
}

# ---- summary ----------------------------------------------------------------
$r = $job.result
Write-Host "`n--- Companies ---" -ForegroundColor Cyan
foreach ($a in @($r.target) + @($r.competitors)) {
    $c = $a.company
    $parts = foreach ($k in "financials", "website", "trustpilot", "social") {
        if ($a.$k) { "${k}: $($a.$k.status)" }
    }
    $role = if ($c.is_target) { "TARGET" } else { "  comp" }
    Write-Host ("{0}  {1} ({2})  {3}" -f $role, $c.name, $c.domain, ($parts -join ", "))
}

Write-Host "`n--- Warnings ---" -ForegroundColor Yellow
if ($r.warnings) { $r.warnings | ForEach-Object { Write-Host " - $_" } } else { Write-Host " (none)" }

Write-Host "`n--- Synthesis (written by the LLM) ---" -ForegroundColor Green
$s = $r.synthesis
if ($s.status) {
    Write-Host "Synthesis not produced: $($s.status) $($s.reason) $($s.error)" -ForegroundColor Red
} elseif ("$($s.executive_summary)".Contains("[mock]")) {
    Write-Host "This is mock text - MOCK_PROVIDERS is still true." -ForegroundColor Yellow
} else {
    Write-Host "Position: $($s.target_position)`n"
    Write-Host $s.executive_summary
    if ($s.recommendations) {
        Write-Host "`nRecommendations:"
        $i = 0
        $s.recommendations | Sort-Object priority | ForEach-Object { $i++; Write-Host (" {0}. {1}" -f $i, $_.action) }
    }
    Write-Host "`nOK - the LLM produced a real synthesis." -ForegroundColor Green
}
