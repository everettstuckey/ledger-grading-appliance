[CmdletBinding()]
param(
    [switch]$InstallOnly
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT' -or -not $env:LOCALAPPDATA) {
    throw 'This launcher requires Windows and a local user profile.'
}

$venv = Join-Path $env:LOCALAPPDATA 'Ledger\venv'
$python = Join-Path $venv 'Scripts\python.exe'
$requirements = Join-Path $PSScriptRoot 'requirements.txt'
$manifest = Join-Path $venv 'ledger-requirements.sha256'
$requirementsHash = (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash
$firstRun = -not (Test-Path -LiteralPath $python -PathType Leaf)
if ($firstRun) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if (-not $launcher) {
        throw 'Install Python 3.12 (64-bit) with its Windows launcher, then try again.'
    }
    & $launcher.Source -3.12 -m venv $venv
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not create the Ledger Python environment. Check that Python 3.12 is installed.'
    }
}

$installedHash = if (Test-Path -LiteralPath $manifest -PathType Leaf) { (Get-Content -LiteralPath $manifest -Raw).Trim() }
if ($firstRun -or $InstallOnly -or $installedHash -ne $requirementsHash) {
    & $python -m pip install --requirement $requirements
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not install Ledger dependencies. Re-run with -InstallOnly after resolving the error.'
    }
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw 'The Ledger Python environment has incompatible packages.'
    }
    Set-Content -LiteralPath $manifest -Value $requirementsHash -Encoding ascii
}

if (-not $InstallOnly) {
    & $python (Join-Path $PSScriptRoot 'engine\ledger-main-minimus.py')
    exit $LASTEXITCODE
}
