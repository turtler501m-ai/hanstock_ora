[CmdletBinding()]
param(
    [ValidateSet("quick", "dashboard", "trading", "ai", "autonomy", "futures", "mistock", "all")]
    [string]$Profile = "quick"
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONDONTWRITEBYTECODE = "1"

function Get-PythonPath {
    $venvPython = Join-Path (Resolve-Path ".") ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        return $venvPython
    }

    if ($env:PYTHON -and (Test-Path -LiteralPath $env:PYTHON)) {
        return $env:PYTHON
    }

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        return $pythonCommand.Source
    }

    throw "python executable not found"
}

$python = Get-PythonPath

Write-Host "verify-local profile: $Profile"

if ($Profile -eq "all") {
    powershell -ExecutionPolicy Bypass -File tools\check-encoding.ps1
}

& $python -c "import pathlib; [compile(p.read_text(encoding='utf-8'), str(p), 'exec') for root in ('src','tests') for p in pathlib.Path(root).rglob('*.py')]"

if ($Profile -eq "all") {
    & $python -m py_compile tools\demo-trading-rehearsal.py
    & $python tools\run-tests.py --profile all
    & $python tools\demo-trading-rehearsal.py --no-db --allow-not-ready
} else {
    & $python tools\run-tests.py --profile $Profile
}

node --check web\static\js\app.js
node --check web\static\js\futures_signals.js
node --check web\static\js\env_settings.js
node --check web\static\js\finrl.js
node --check web\static\js\vendors.js
