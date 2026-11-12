# Registers Windows Task Scheduler jobs for the current user:
#   TRINITY-Paper  : starts the PAPER engine at logon, restarts it if it stops
#   TRINITY-Backup : daily database backup at 03:17
# Live trading is NEVER started automatically.
$ErrorActionPreference = "Stop"
$Root = Resolve-Path "$PSScriptRoot\..\..\.."
$Py = Join-Path $Root "trinity\.venv\Scripts\python.exe"

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -StartWhenAvailable -MultipleInstances IgnoreNew

$action = New-ScheduledTaskAction -Execute $Py -Argument "trinity\scripts\run_paper.py" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
Register-ScheduledTask -TaskName "TRINITY-Paper" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

$bAction = New-ScheduledTaskAction -Execute $Py -Argument "trinity\scripts\backup.py" -WorkingDirectory $Root
$bTrigger = New-ScheduledTaskTrigger -Daily -At 3:17am
Register-ScheduledTask -TaskName "TRINITY-Backup" -Action $bAction -Trigger $bTrigger -Settings $settings -Force | Out-Null

Write-Host "Registered TRINITY-Paper (at logon) and TRINITY-Backup (daily 03:17)."
Write-Host "Remove with: Unregister-ScheduledTask -TaskName TRINITY-Paper; Unregister-ScheduledTask -TaskName TRINITY-Backup"
