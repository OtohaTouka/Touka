# Developer dependencies only. Product installation: ../README_QUICKSTART.md.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
try {
    $repoRoot = Split-Path -Parent $PSScriptRoot
    foreach ($name in @('uv.lock', 'pyproject.toml', '.python-version')) {
        if (!(Test-Path -LiteralPath (Join-Path $repoRoot $name) -PathType Leaf)) {
            throw "Incomplete workspace: $name is required."
        }
    }
    $expectedVersion = (Get-Content -LiteralPath (Join-Path $repoRoot '.python-version') -Raw).Trim()
    if ($expectedVersion -notmatch '^\d+\.\d+$') {
        throw 'Invalid workspace Python version.'
    }
    $null = Get-Command uv -ErrorAction Stop
    Push-Location -LiteralPath $repoRoot
    try {
        & uv sync --locked --all-packages
        $syncExit = $LASTEXITCODE
    }
    finally { Pop-Location }
    if ($syncExit -ne 0) { exit $syncExit }

    $python = Join-Path $repoRoot '.venv/Scripts/python.exe'
    if (!(Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'Workspace Python is missing after uv sync.'
    }
    $actualVersion = & $python -I -S -B -c "import sys; print('%s.%s' % sys.version_info[:2])"
    if ($LASTEXITCODE -ne 0 -or $actualVersion -ne $expectedVersion) {
        throw "Workspace Python must match .python-version ($expectedVersion)."
    }
    Write-Host 'Developer dependencies are ready. No data, configuration or services were initialized.'
    Write-Host 'For product installation, follow README_QUICKSTART.md at the repository root.'
}
catch {
    Write-Error -ErrorAction Continue $_
    exit 1
}
exit 0
