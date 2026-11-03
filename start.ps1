param([ValidateRange(1,65535)][int]$Port = 8319)

$ErrorActionPreference = 'Stop'
$venvPython = Join-Path $PSScriptRoot 'venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) { throw 'Run .\install.ps1 first.' }
Push-Location $PSScriptRoot
$previousPort = $env:PORT
$previousHost = $env:HOST
try {
    $env:PORT = [string]$Port
    $env:HOST = '127.0.0.1'
    Write-Host "Hermes dashboard: http://localhost:$Port (Ctrl+C to stop)"
    & $venvPython (Join-Path $PSScriptRoot 'router.py')
    if ($LASTEXITCODE -ne 0) { throw "Router exited with code $LASTEXITCODE." }
} finally {
    $env:PORT = $previousPort
    $env:HOST = $previousHost
    Pop-Location
}
