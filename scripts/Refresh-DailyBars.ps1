<#
.SYNOPSIS
    Refresh DailySnapshot from the vendor's whole-market feed, via the service.

.DESCRIPTION
    Posts to POST /refresh. The service does the work and reports what it cost; this script's job is to
    be runnable from Task Scheduler and to leave a readable trace in a log file.

    Measured: about 30 seconds and 100 vendor units for a full session plus the ATR rebuild (~44,000
    rows written, 2,866 symbols re-indicated). Without the indicator rebuild it is about 10 seconds.

    WHY THIS EXISTS AS WELL AS THE ENDPOINT

    An endpoint is only a refresh if something calls it. The scheduled task runs this daily after the
    US close has settled; running it by hand is refresh.bat, which does the same thing.

    Exit codes: 0 refreshed or already fresh, 1 anything else -- so Task Scheduler's last-run result
    distinguishes "nothing to do" from "it went wrong".

.NOTES
    The service must already be running. It is the service that holds the vendor key and the spend
    ledger, and a refresh recorded under this caller is what the ledger will show.
#>
param(
    [string]$BaseUrl = 'http://127.0.0.1:8003',
    [string]$Market = 'US',
    [switch]$Force,
    [int]$TimeoutSeconds = 300
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Logs = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
$History = Join-Path $Logs 'refresh-history.txt'

function Note([string]$Message) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -Path $History -Value $line
    Write-Output $line
}

Note "refresh: market=$Market force=$([bool]$Force)"

$body = @{ market = $Market; force = [bool]$Force; include_indicators = $true } | ConvertTo-Json

try {
    $response = Invoke-WebRequest -Method Post -Uri "$BaseUrl/refresh" `
        -ContentType 'application/json' -Body $body `
        -Headers @{ 'X-MarketData-Caller' = 'task.daily_refresh' } `
        -TimeoutSec $TimeoutSeconds -UseBasicParsing
    $result = $response.Content | ConvertFrom-Json
} catch {
    # A 409 (busy) and a 503 (no vendor key) are answers, not crashes: read the body if there is one.
    $response = $_.Exception.Response
    if ($response) {
        try {
            $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
            $result = $reader.ReadToEnd() | ConvertFrom-Json
        } catch {
            $result = $null
        }
    }
    if (-not $result) {
        Note "FAILED: $($_.Exception.Message)"
        exit 1
    }
}

switch ($result.status) {
    'refreshed' {
        Note ("refreshed: session {0}, {1} rows, {2} units, {3}ms{4}" -f `
            $result.newest_session, $result.rows_written, $result.units, $result.duration_ms,
            $(if ($result.behind_expected) { "  BEHIND EXPECTED $($result.expected_session)" } else { "" }))
        exit 0
    }
    'already-fresh' {
        Note "already-fresh: $($result.detail)"
        exit 0
    }
    default {
        Note ("NOT REFRESHED ({0}): {1}" -f $result.status, $result.detail)
        exit 1
    }
}
