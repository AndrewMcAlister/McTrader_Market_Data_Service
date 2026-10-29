# Register ONLY the daily bulk-refresh task, without the interactive Get-Credential that
# install-task.ps1 -Install uses for the whole estate.
#
# The principal is copied from the keep-alive task that already works, but with LogonType S4U:
# the refresh posts to http://127.0.0.1:8003/refresh and needs no network resource beyond
# localhost, so it needs no stored password. S4U also lets it run whether or not the account is
# logged on, which is the point of a scheduled job on this box.
#
# If this fails on permissions, the task needs an elevated shell -- the script says so rather
# than failing silently.

$ErrorActionPreference = 'Stop'

$TaskName = 'McTrader_Market_Data_Service_KeepAlive_Refresh'
$Script   = 'D:\McTrader_Market_Data_Service\scripts\Refresh-DailyBars.ps1'
$At       = '11:30'

if (-not (Test-Path $Script)) { throw "missing $Script" }

$source = Get-ScheduledTask -TaskName 'McTrader_Market_Data_Service_KeepAlive' -ErrorAction Stop
$userId = $source.Principal.UserId
Write-Host "  principal copied from the keep-alive task: $userId"

$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType S4U -RunLevel Limited
$action = New-ScheduledTaskAction `
    -Execute 'C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Script`"" `
    -WorkingDirectory 'D:\McTrader_Market_Data_Service'
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "  removed the previous registration"
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description 'Bulk refresh of the market data service (POST /refresh), daily after the US close has settled' | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ""
Write-Host "  registered : $($task.TaskName)"
Write-Host "  state      : $($task.State)"
Write-Host "  principal  : $($task.Principal.UserId)  logon=$($task.Principal.LogonType)  runlevel=$($task.Principal.RunLevel)"
Write-Host "  trigger    : daily at $At"
Write-Host "  action     : $($task.Actions[0].Arguments)"
Write-Host "  next run   : $($info.NextRunTime)"
