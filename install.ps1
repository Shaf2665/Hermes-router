param([switch]$Start)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Push-Location $PSScriptRoot
try {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw 'Python 3.10+ is required. Install Python and enable its PATH option, then run this script again.'
    }
    & $pythonCommand.Source -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 or newer is required.' }
    $venvPython = Join-Path $PSScriptRoot 'venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        & $pythonCommand.Source -m venv (Join-Path $PSScriptRoot 'venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
    }
    & $venvPython -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed; the router has not been started.' }
    & $venvPython -c 'import flask, waitress, requests, tiktoken'
    if ($LASTEXITCODE -ne 0) { throw 'Dependency verification failed.' }
    $envPath = Join-Path $PSScriptRoot '.env'
    if (-not (Test-Path -LiteralPath $envPath)) {
        [System.IO.File]::WriteAllText($envPath, "HOST=127.0.0.1`nPORT=8319`n", [System.Text.UTF8Encoding]::new($false))
    }
    Write-Host 'Installed. Existing configuration was preserved.'
    Write-Host 'Run .\start.ps1, then open http://localhost:8319. See documentation/setup.md.'
    if ($Start) { & (Join-Path $PSScriptRoot 'start.ps1') }
} finally {
    Pop-Location
}
