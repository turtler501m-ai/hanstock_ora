[CmdletBinding()]
param(
    [ValidateSet("quick", "dashboard", "trading", "ai", "all")]
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

$testTargets = @{
    quick = @(
        "tests.test_dashboard_core",
        "tests.test_runtime_plan",
        "tests.test_scheduler_api"
    )
    dashboard = @(
        "tests.test_dashboard_core",
        "tests.test_dashboard_auth",
        "tests.test_dashboard_execution_plan",
        "tests.test_dashboard_plan_views",
        "tests.test_runtime_dashboard_alignment",
        "tests.test_scheduler_api"
    )
    trading = @(
        "tests.test_trader_core",
        "tests.test_runtime_plan",
        "tests.test_order_router",
        "tests.test_execution_policy",
        "tests.test_kis_api",
        "tests.test_kis_client"
    )
    ai = @(
        "tests.test_ai_stock_core",
        "tests.test_ai_stock_api",
        "tests.test_ai_strategy_lifecycle",
        "tests.test_ai_strategy_presets",
        "tests.test_autonomy_ai_stock_integration"
    )
}

if ($Profile -eq "all") {
    & $python -m py_compile tools\demo-trading-rehearsal.py
    & $python -m unittest discover -s tests -t .
    & $python tools\demo-trading-rehearsal.py --no-db --allow-not-ready
} else {
    & $python -m unittest @($testTargets[$Profile])
}

node --check web\static\js\app.js
node --check web\static\js\futures_signals.js
node --check web\static\js\env_settings.js
node --check web\static\js\finrl.js
node --check web\static\js\vendors.js
