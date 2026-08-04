[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

if (-not (Get-Command pip-compile -ErrorAction SilentlyContinue)) {
    throw "pip-compile is required. Install it with: python -m pip install pip-tools"
}

New-Item -ItemType Directory -Force constraints | Out-Null

Write-Host "VM lock must be compiled on Linux/Python 3.10 with tools/compile-vm-lock.sh"

pip-compile `
    --generate-hashes `
    --strip-extras `
    --allow-unsafe `
    --resolver=backtracking `
    --output-file constraints\voice-windows.lock `
    requirements-voice.txt
if ($LASTEXITCODE -ne 0) {
    throw "voice lock compilation failed with exit code $LASTEXITCODE"
}

python tools\verify-deploy-constraints.py
if ($LASTEXITCODE -ne 0) {
    throw "lock verification failed with exit code $LASTEXITCODE"
}
