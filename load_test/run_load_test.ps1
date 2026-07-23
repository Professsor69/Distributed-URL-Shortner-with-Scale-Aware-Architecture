#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Run the Phase 5 Locust load test against the URL Shortener service.

.DESCRIPTION
    Executes two load test scenarios (baseline and high-load) in headless mode
    and saves HTML reports to load_test/results/.

.PARAMETER Host
    Base URL of the running FastAPI service. Default: http://localhost:8000

.PARAMETER Baseline
    Run only the baseline (50 users). Skip the 200-user high-load test.

.EXAMPLE
    .\load_test\run_load_test.ps1
    .\load_test\run_load_test.ps1 -Host "http://localhost:8000" -Baseline
#>

param(
    [string]$Host = "http://localhost:8000",
    [switch]$Baseline
)

$ErrorActionPreference = "Stop"
$ResultsDir = Join-Path $PSScriptRoot "results"

# ── Pre-flight checks ──────────────────────────────────────────────────────────
Write-Host "`n[1/4] Checking prerequisites..." -ForegroundColor Cyan

# Check venv Locust
$locust = Join-Path (Split-Path $PSScriptRoot -Parent) "venv\Scripts\locust.exe"
if (-not (Test-Path $locust)) {
    Write-Error "Locust not found at $locust. Run: pip install locust>=2.31.0"
    exit 1
}

# Check server is reachable
try {
    $health = Invoke-RestMethod "$Host/metrics/health" -TimeoutSec 3
    Write-Host "  Server reachable at $Host" -ForegroundColor Green
} catch {
    Write-Error "Server not reachable at $Host. Start it first:`n  uvicorn app.main:app --reload"
    exit 1
}

# Ensure results directory exists
New-Item -ItemType Directory -Force -Path $ResultsDir | Out-Null
Write-Host "  Results will be saved to: $ResultsDir" -ForegroundColor Green

# ── Baseline run (50 users) ────────────────────────────────────────────────────
Write-Host "`n[2/4] Running BASELINE test (50 users, 60s)..." -ForegroundColor Cyan

$locustArgs = @(
    "--headless"
    "--users", "50"
    "--spawn-rate", "5"
    "--run-time", "60s"
    "--host", $Host
    "--html", "$ResultsDir\baseline_report.html"
    "--csv",  "$ResultsDir\baseline"
    "-f", (Join-Path $PSScriptRoot "locustfile.py")
)

& $locust @locustArgs
Write-Host "  Baseline complete. Report: $ResultsDir\baseline_report.html" -ForegroundColor Green

if ($Baseline) {
    Write-Host "`nBaseline-only mode. Done." -ForegroundColor Yellow
    exit 0
}

# ── High-load run (200 users) ─────────────────────────────────────────────────
Write-Host "`n[3/4] Running HIGH-LOAD test (200 users, 60s)..." -ForegroundColor Cyan
Write-Host "  TIP: For best results, restart uvicorn with --workers 4 before this test" -ForegroundColor Yellow

$locustArgs200 = @(
    "--headless"
    "--users", "200"
    "--spawn-rate", "10"
    "--run-time", "60s"
    "--host", $Host
    "--html", "$ResultsDir\highload_report.html"
    "--csv",  "$ResultsDir\highload"
    "-f", (Join-Path $PSScriptRoot "locustfile.py")
)

& $locust @locustArgs200
Write-Host "  High-load complete. Report: $ResultsDir\highload_report.html" -ForegroundColor Green

# ── Summary ────────────────────────────────────────────────────────────────────
Write-Host "`n[4/4] Reports saved:" -ForegroundColor Cyan
Get-ChildItem $ResultsDir | Where-Object { $_.Extension -in ".html", ".csv" } |
    ForEach-Object { Write-Host "  $($_.FullName)" }

Write-Host "`nDone! Open the HTML reports in your browser for the full Locust UI." -ForegroundColor Green
