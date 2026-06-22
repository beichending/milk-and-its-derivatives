param(
    [string]$TaskName = "SGX Whole Milk Powder Futures Daily Collector",
    [string]$RunAt = "19:30"
)

$ErrorActionPreference = "Stop"
$python = "C:\Users\dbcmi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$collector = Join-Path $PSScriptRoot "sgx_wmp_collector.py"
$config = Join-Path $PSScriptRoot "config.json"
$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "`"$collector`" --config `"$config`" collect" `
    -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $RunAt
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 10)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Collect all SGX WMP futures contracts and run historical anomaly checks." `
    -Force

Write-Host "Installed task '$TaskName' at $RunAt local time."
