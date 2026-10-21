# Explicit native development launcher; never stops an existing service.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$previousPythonPath = $env:PYTHONPATH
try {
    $repoRoot = Split-Path -Parent $PSScriptRoot
    $python = Join-Path $repoRoot '.venv/Scripts/python.exe'
    if (!(Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'Workspace Python is missing. Run setup.ps1 for developer dependencies.'
    }
    $expectedVersion = (Get-Content -LiteralPath (Join-Path $repoRoot '.python-version') -Raw).Trim()
    $actualVersion = & $python -I -S -B -c "import sys; print('%s.%s' % sys.version_info[:2])"
    if ($LASTEXITCODE -ne 0 -or $actualVersion -ne $expectedVersion) {
        throw 'Workspace Python does not match .python-version.'
    }
    $portText = if ($env:PORT) { $env:PORT } else { '8000' }
    $port = 0
    if (![int]::TryParse($portText, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
        throw 'PORT must be an integer from 1 to 65535.'
    }
    $env:PYTHONPATH = @($PSScriptRoot, (Join-Path $repoRoot 'ToukaMemory/src')) -join [IO.Path]::PathSeparator
    Push-Location -LiteralPath $PSScriptRoot
    try {
        & $python -B -m uvicorn src.main:app --host 127.0.0.1 --port $port --reload --no-access-log
        $serverExit = $LASTEXITCODE
    }
    finally { Pop-Location }
}
catch {
    Write-Error -ErrorAction Continue $_
    exit 1
}
finally { $env:PYTHONPATH = $previousPythonPath }
exit $serverExit
