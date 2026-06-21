param(
    [string]$Config = "$PSScriptRoot\config.json"
)

$ErrorActionPreference = "Stop"
$python = "C:\Users\dbcmi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if (-not (Test-Path $Config)) {
    Copy-Item (Join-Path $PSScriptRoot "config.example.json") $Config
}

& $python (Join-Path $PSScriptRoot "sgx_butter_collector.py") --config $Config collect
exit $LASTEXITCODE
