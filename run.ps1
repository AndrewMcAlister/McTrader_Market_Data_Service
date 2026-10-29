# McTrader_Market_Data_Service — run and operate
#
#   .\run.ps1              start the service on 127.0.0.1:8003
#   .\run.ps1 -Daily       run the daily job once (bulk pull + indicator rebuild)
#   .\run.ps1 -Test        run the test suite (spends no vendor units)
#   .\run.ps1 -Schema      apply sql/001_schema.sql and report row counts
#
# Everything runs from the project's own virtualenv; the service has no dependency on
# McTrader's environment.

param(
    [switch]$Daily,
    [switch]$Test,
    [switch]$Schema,
    [switch]$Seed,
    [int]$SeedDays = 90,
    [string]$Since
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    Write-Host "No virtualenv at $python" -ForegroundColor Red
    Write-Host "Create it once with:" -ForegroundColor Yellow
    Write-Host "  python -m venv .venv"
    Write-Host "  .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}

Push-Location $root
try {
    if ($Test) {
        & $python -m pytest tests -q
        exit $LASTEXITCODE
    }
    if ($Schema) {
        & $python tools\apply_schema.py
        exit $LASTEXITCODE
    }
    if ($Seed) {
        & $python -m jobs.daily --seed-from-barhourly --days $SeedDays --rebuild-indicators
        exit $LASTEXITCODE
    }
    if ($Daily) {
        if ($Since) {
            & $python -m jobs.daily --since $Since
        } else {
            & $python -m jobs.daily
        }
        exit $LASTEXITCODE
    }

    Write-Host "McTrader_Market_Data_Service on http://127.0.0.1:8003  (docs at /docs, health at /healthz)" -ForegroundColor Cyan
    & $python -m data_service
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
