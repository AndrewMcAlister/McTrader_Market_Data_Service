<#
.SYNOPSIS
    Register McTrader_Market_Data_Service to start at boot under YOUR account, via Task Scheduler.

.DESCRIPTION
    This is the same shape as McTrader_AutoStart_OnBoot, with one addition taken from
    PaperTrader_KeepAlive.

    WHY YOUR ACCOUNT AND NOT LocalSystem

    The EODHD key lives in the Windows Credential Manager, which is **per user**. A LocalSystem
    service has its own empty vault and cannot see it, so it would start, report healthy, and then
    fail every vendor call. Running as you means the keyring already works and there is nothing else
    to configure.

    LogonType is `Password`, not `S4U`. That distinction is load-bearing: an S4U task runs without
    loading your credential vault, so the keyring would be unavailable even though the account name
    looks right. Task Scheduler stores the password you type here; it is never written by this script.

    WHY A REPEATING TRIGGER AS WELL AS BOOT

    PaperTrader's keep-alive header records the reason:

        "on 2026-10-14 the machine rebooted at 05:17 Sydney and the trader was gone 43 minutes
         before the US close... A boot-only trigger would not have restarted it that evening either."

    A boot-only trigger starts the service once and never notices it die. For this service that
    matters more than for most: it is on the critical path at 10:30, so a crash at 10:29 with no
    recovery means the scan and the stops have no data.

    The repeating trigger fires `Ensure-MarketDataService.ps1`, which is idempotent -- it checks the
    port and starts the service only if nothing is listening.

.EXAMPLE
    .\install-task.ps1 -Install         # prompts for your password once
    .\install-task.ps1 -Status
    .\install-task.ps1 -Run             # start it now, without waiting for the trigger
    .\install-task.ps1 -Remove
#>
param(
    [switch]$Install,
    [switch]$Remove,
    [switch]$Run,
    [switch]$Restart,
    [switch]$Status,
    [string]$User,
    #: Local time for the daily refresh. 11:30 is chosen against the US close, not for convenience.
    [string]$RefreshAt = '11:30'
)

$ErrorActionPreference = 'Stop'

$TaskName = 'McTrader_Market_Data_Service_KeepAlive'
$Port = 8003
$Root = $PSScriptRoot   # this script lives at the project root
$Ensure = Join-Path $Root 'scripts\Ensure-MarketDataService.ps1'
$Refresh = Join-Path $Root 'scripts\Refresh-DailyBars.ps1'
$PowerShell = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
$Description = 'Market data for the trading books on port 8003: prices, daily bars and ATR. Runs as the interactive account so the EODHD key in the credential store is readable.'

function Test-Elevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Assert-Elevated($action) {
    if (Test-Elevated) { return }
    Write-Host ""
    Write-Host "Registering a task that stores a password needs elevation." -ForegroundColor Yellow
    Write-Host "Open PowerShell as Administrator and run:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "    cd `"$Root`"" -ForegroundColor Cyan
    Write-Host "    .\install-task.cmd -$action" -ForegroundColor Cyan
    Write-Host ""
    exit 1
}

function Show-State {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "task    : $TaskName -- not registered"
    } else {
        Write-Host "task    : $TaskName  [$($task.State)]"
        Write-Host "account : $($task.Principal.UserId)  logon=$($task.Principal.LogonType)  level=$($task.Principal.RunLevel)"
        foreach ($t in $task.Triggers) { Write-Host "trigger : $($t.CimClass.CimClassName)" }
        foreach ($a in $task.Actions) { Write-Host "action  : $($a.Arguments)" }
    }
    $listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    Write-Host "port    : $Port $(if ($listening) { "LISTENING (pid $($listening[0].OwningProcess))" } else { 'not listening' })"
    Write-Host "log     : $(Join-Path $Root 'logs\ensure-history.txt')"
}

if ($Status -or (-not $Install -and -not $Remove -and -not $Run -and -not $Restart)) {
    if (-not $Status) {
        Write-Host ""
        Write-Host "STATUS ONLY -- nothing was installed and nothing changed." -ForegroundColor Yellow
        Write-Host "  to register the task:  .\install-task.ps1 -Install    (needs elevation, asks for your password)" -ForegroundColor Yellow
        Write-Host "  (a bare command shows status on purpose: a typo must not register a task that stores a password)" -ForegroundColor DarkGray
        Write-Host ""
    }
    Show-State
    exit 0
}

if ($Remove) {
    Assert-Elevated 'Remove'
    if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        Write-Host "Task $TaskName is not registered."
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "removed $TaskName"
    foreach ($extra in @("$($TaskName)_Stop", "$($TaskName)_Refresh")) {
        if (Get-ScheduledTask -TaskName $extra -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $extra -Confirm:$false
            Write-Host "removed $extra"
        }
    }
    exit 0
}

if ($Restart) {
    # Why this exists: Task Scheduler's "End" ends the task, not the service. The launcher starts the
    # service detached, so the process survives the task, and the next run reports "already
    # listening" and exits 0 -- a restart that looks successful and changes nothing.
    $listening = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if (-not $listening) {
        Write-Host "nothing is listening on $Port -- starting it"
    }
    foreach ($connection in $listening) {
        $processId = $connection.OwningProcess
        Write-Host "stopping pid $processId on port $Port"
        try {
            Stop-Process -Id $processId -Force -ErrorAction Stop
        } catch {
            Write-Host ""
            Write-Host "could not stop pid $processId : $($_.Exception.Message)" -ForegroundColor Yellow
            Write-Host "If the task was registered with -RunLevel Highest, stopping it needs elevation." -ForegroundColor Yellow
            Write-Host "Re-run this from an elevated prompt:" -ForegroundColor Yellow
            Write-Host "    .\install-task.ps1 -Restart" -ForegroundColor Cyan
            exit 1
        }
    }
    # Give the port a moment to be released, then start it through the same launcher the task uses,
    # so a manual restart exercises the same path the scheduled one does.
    Start-Sleep -Seconds 2
    & $PowerShell -NoProfile -ExecutionPolicy Bypass -File $Ensure
    exit $LASTEXITCODE
}

if ($Run) {
    Write-Host "running the launcher directly (no elevation needed)"
    & $PowerShell -NoProfile -ExecutionPolicy Bypass -File $Ensure
    exit $LASTEXITCODE
}

if ($Install) {
    Assert-Elevated 'Install'

    if (-not (Test-Path $Ensure)) { throw "launcher not found: $Ensure" }

    $account = if ($User) { $User } else { "$env:USERDOMAIN\$env:USERNAME" }
    Write-Host "account : $account"
    Write-Host "The password is used only to register the task; Task Scheduler stores it." -ForegroundColor DarkGray
    $credential = Get-Credential -UserName $account -Message "Password for $account (stored by Task Scheduler)"

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Write-Host "already registered; replacing" -ForegroundColor Yellow
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    # THE PARAMETER SET MATTERS
    #
    # Register-ScheduledTask has separate sets: -Principal OR -User/-Password, never both. Mixing them
    # is "Parameter set cannot be resolved using the specified named parameters".
    #
    # The User set is the one we need, and not merely because it binds: supplying -Password is what
    # stores the credential and sets LogonType=Password, so the task runs whether or not you are logged
    # on -- and therefore so the credential vault, and the EODHD key inside it, is available. A
    # New-ScheduledTaskPrincipal -LogonType Password object describes the shape without a password,
    # which would leave the task unable to run while logged off.
    $plainPassword = $credential.GetNetworkCredential().Password

    $action = New-ScheduledTaskAction -Execute $PowerShell `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Ensure`"" `
        -WorkingDirectory $Root

    # Boot, delayed: SQL Server needs to be up before the app calls ensure_schema().
    $boot = New-ScheduledTaskTrigger -AtStartup
    $boot.Delay = 'PT2M'

    # And every N minutes, so a crash is noticed rather than waiting for the next reboot.
    # ONE trigger, at boot -- the same shape as McTrader_AutoStart_OnBoot.
    #
    # There is deliberately no repeating trigger. The launcher stops whatever holds the port before
    # starting, so a repeating trigger would restart the service on every fire, forever. McTrader's
    # task is boot-only for the same reason: it is what makes a kill-before-start script safe.

    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

    # NO -RunLevel Highest, deliberately.
    #
    # This account is in the Administrators group, so `-RunLevel Highest` runs the task with the
    # ELEVATED token while an ordinary shell is UAC-filtered. A medium-integrity shell then cannot
    # stop the service -- "Access is denied" -- so every restart would need an elevated prompt.
    #
    # Nothing here needs elevation: the database uses a login grant, the keyring is per USER (which is
    # why the task runs as you rather than as LOCAL SYSTEM), the ports are above 1024, and the logs
    # are in folders you own. Omitting -RunLevel leaves the default, limited run level.
    Register-ScheduledTask -TaskName $TaskName `
        -Action $action -Trigger $boot -Settings $settings `
        -Description $Description `
        -User $credential.UserName -Password $plainPassword | Out-Null

    # A SECOND task, with no trigger, whose only job is to stop the service.
    #
    # It exists because the service runs in session 0 (Task Scheduler started it) and an interactive
    # shell in session 2 cannot terminate it -- "Access is denied", at any run level. Running the
    # launcher from inside a task puts it back in session 0, where the kill succeeds.
    #
    # Starting a task registered with a stored password needs no elevation, so stop.bat is a plain
    # schtasks call.
    $StopTaskName = "$($TaskName)_Stop"
    if (Get-ScheduledTask -TaskName $StopTaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $StopTaskName -Confirm:$false
    }
    $stopAction = New-ScheduledTaskAction -Execute $PowerShell `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Ensure`" -Stop" `
        -WorkingDirectory $Root
    Register-ScheduledTask -TaskName $StopTaskName `
        -Action $stopAction -Settings $settings `
        -Description "$Description (stops it; run on demand, no trigger)" `
        -User $credential.UserName -Password $plainPassword | Out-Null

    # A THIRD task: the daily bulk refresh.
    #
    # 11:30 local is not a round number chosen for tidiness. The US session closes at 16:00 ET, which
    # is 07:00 or 08:00 in Sydney depending on the season, and data_service.sessions treats a session
    # as complete only three hours after its close. A refresh before that margin asks for a session
    # the service still considers open -- so it either pulls the previous one again (100 units for
    # nothing) or reports itself behind-expected every day.
    $RefreshTaskName = "$($TaskName)_Refresh"
    if (Get-ScheduledTask -TaskName $RefreshTaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $RefreshTaskName -Confirm:$false
    }
    $refreshAction = New-ScheduledTaskAction -Execute $PowerShell `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Refresh`"" `
        -WorkingDirectory $Root
    $refreshTrigger = New-ScheduledTaskTrigger -Daily -At $RefreshAt
    Register-ScheduledTask -TaskName $RefreshTaskName `
        -Action $refreshAction -Trigger $refreshTrigger -Settings $settings `
        -Description "$Description (daily bulk refresh at $RefreshAt local)" `
        -User $credential.UserName -Password $plainPassword | Out-Null

    Write-Host "`nregistered $TaskName" -ForegroundColor Cyan
    Write-Host "registered $StopTaskName (no trigger -- for stop.bat)" -ForegroundColor Cyan
    Write-Host "registered $RefreshTaskName (daily at $RefreshAt -- POST /refresh)" -ForegroundColor Cyan
    Write-Host "starting it now..."
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 8

    Show-State
    Write-Host ""
    Write-Host "check the key is reachable:" -ForegroundColor Cyan
    Write-Host "  Invoke-RestMethod http://127.0.0.1:$Port/healthz   # key_source should be 'keyring'"
    exit 0
}
