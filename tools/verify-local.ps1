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
$env:PYTHONWARNINGS = "error::ResourceWarning,default::DeprecationWarning"

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

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Command,
        [Parameter(Mandatory = $true)][string]$Label
    )
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

Write-Host "verify-local profile: $Profile"

if ($Profile -eq "all") {
    Invoke-Checked { powershell -ExecutionPolicy Bypass -File tools\check-encoding.ps1 } "encoding check"
}

Invoke-Checked { & $python -c "import pathlib; [compile(p.read_text(encoding='utf-8'), str(p), 'exec') for root in ('src','tests') for p in pathlib.Path(root).rglob('*.py')]" } "Python source compile"
Invoke-Checked { & $python tools\verify-deploy-constraints.py } "deploy constraints verification"

if ($Profile -eq "all") {
    Invoke-Checked { & $python -m py_compile tools\demo-trading-rehearsal.py } "demo rehearsal compile"
    Invoke-Checked { & $python tools\run-tests.py --profile all } "all test profile"
    Invoke-Checked { & $python tools\demo-trading-rehearsal.py --no-db --allow-not-ready } "demo trading rehearsal"
} else {
    Invoke-Checked { & $python tools\run-tests.py --profile $Profile } "$Profile test profile"
}

Invoke-Checked { node --check web\static\js\app.js } "app.js syntax check"
Invoke-Checked { node --check web\static\js\futures_signals.js } "futures_signals.js syntax check"
Invoke-Checked { node --check web\static\js\env_settings.js } "env_settings.js syntax check"
Invoke-Checked { node --check web\static\js\finrl.js } "finrl.js syntax check"
Invoke-Checked { node --check web\static\js\vendors.js } "vendors.js syntax check"
