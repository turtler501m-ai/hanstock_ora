param(
    [string]$HostName = $(if ($env:HANSTOCK_VM_HOST) { $env:HANSTOCK_VM_HOST } else { "168.110.102.249" }),
    [string]$User = $(if ($env:HANSTOCK_VM_USER) { $env:HANSTOCK_VM_USER } else { "ubuntu" }),
    [string]$Instance = $(if ($env:HANSTOCK_GCP_INSTANCE) { $env:HANSTOCK_GCP_INSTANCE } else { "" }),
    [string]$Zone = $(if ($env:HANSTOCK_GCP_ZONE) { $env:HANSTOCK_GCP_ZONE } else { "us-central1-c" }),
    [string]$Project = $(if ($env:HANSTOCK_GCP_PROJECT) { $env:HANSTOCK_GCP_PROJECT } else { "" }),
    [string]$KeyPath = $(if ($env:HANSTOCK_SSH_KEY) { $env:HANSTOCK_SSH_KEY } else { (Join-Path $env:USERPROFILE ".ssh\id_ed25519") }),
    [int]$LocalPort = 18000,
    [int]$RemotePort = 8000,
    [switch]$NoBrowser,
    [switch]$Status,
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$RuntimeDir = Join-Path $Root ".runtime"
$StatePath = Join-Path $RuntimeDir "vm-dashboard-tunnel.json"

function Get-ToolPath {
    param(
        [string]$Name,
        [string]$DefaultPath
    )

    if ($DefaultPath -and (Test-Path -LiteralPath $DefaultPath)) {
        return $DefaultPath
    }

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    throw "$Name command was not found."
}

function Resolve-GcpHost {
    param(
        [string]$InstanceName,
        [string]$InstanceZone,
        [string]$ProjectId
    )

    $gcloud = Get-ToolPath `
        -Name "gcloud" `
        -DefaultPath (Join-Path $env:LOCALAPPDATA "Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd")

    $ip = & $gcloud compute instances describe $InstanceName `
        --zone $InstanceZone `
        --project $ProjectId `
        --format "value(networkInterfaces[0].accessConfigs[0].natIP)"

    if (-not $ip) {
        throw "Could not find an external IP for $InstanceName."
    }

    return $ip.Trim()
}

function Get-TunnelState {
    if (-not (Test-Path -LiteralPath $StatePath)) {
        return $null
    }

    try {
        return Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Get-TunnelProcess {
    $state = Get-TunnelState
    if (-not $state -or -not $state.pid) {
        return $null
    }

    return Get-Process -Id ([int]$state.pid) -ErrorAction SilentlyContinue
}

function Test-LocalDashboard {
    param([int]$Port)

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 4
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    } catch {
        return $false
    }
}

function Test-PortAvailable {
    param([int]$Port)

    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse("127.0.0.1"), $Port)
    try {
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        $listener.Stop()
    }
}

if ($Stop) {
    $process = Get-TunnelProcess
    if ($process) {
        Stop-Process -Id $process.Id -Force
        Write-Host "[vm-dashboard] stopped tunnel pid=$($process.Id)"
    } else {
        Write-Host "[vm-dashboard] no running tunnel"
    }
    Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
    exit 0
}

$state = Get-TunnelState
$process = Get-TunnelProcess
$url = "http://127.0.0.1:$LocalPort"

if ($Status) {
    if ($process) {
        Write-Host "[vm-dashboard] running pid=$($process.Id)"
        Write-Host "[vm-dashboard] url=$url"
    } else {
        Write-Host "[vm-dashboard] stopped"
    }
    if (Test-LocalDashboard -Port $LocalPort) {
        Write-Host "[vm-dashboard] health=ok"
    } else {
        Write-Host "[vm-dashboard] health=unreachable"
    }
    exit 0
}

if ($process -and (Test-LocalDashboard -Port $LocalPort)) {
    Write-Host "[vm-dashboard] tunnel already running pid=$($process.Id)"
    Write-Host "[vm-dashboard] open: $url"
    if (-not $NoBrowser) {
        Start-Process $url
    }
    exit 0
}

if (-not (Test-PortAvailable -Port $LocalPort)) {
    throw "Local port $LocalPort is already in use. Try -LocalPort 18001 or run .\vm-dashboard.cmd -Stop."
}

if (-not $HostName) {
    $HostName = Resolve-GcpHost -InstanceName $Instance -InstanceZone $Zone -ProjectId $Project
}

$ssh = Get-ToolPath -Name "ssh" -DefaultPath (Join-Path $env:WINDIR "System32\OpenSSH\ssh.exe")
$target = "$User@$HostName"
$sshArgs = @(
    "-N",
    "-L", "127.0.0.1:${LocalPort}:127.0.0.1:${RemotePort}",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=15"
)
if (Test-Path -LiteralPath $KeyPath) {
    $sshArgs = @("-i", $KeyPath) + $sshArgs
}
$sshArgs += $target

New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
$process = Start-Process -FilePath $ssh -ArgumentList $sshArgs -WindowStyle Hidden -PassThru

$ready = $false
for ($i = 0; $i -lt 12; $i++) {
    Start-Sleep -Milliseconds 500
    if ($process.HasExited) {
        throw "SSH tunnel exited early with code $($process.ExitCode)."
    }
    if (Test-LocalDashboard -Port $LocalPort) {
        $ready = $true
        break
    }
}

$statePayload = [ordered]@{
    pid = $process.Id
    target = $target
    local_port = $LocalPort
    remote_port = $RemotePort
    url = $url
    started_at = (Get-Date).ToString("o")
}
$statePayload | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8

if (-not $ready) {
    Write-Host "[vm-dashboard] tunnel started pid=$($process.Id), dashboard health check is still pending"
} else {
    Write-Host "[vm-dashboard] tunnel started pid=$($process.Id)"
}
Write-Host "[vm-dashboard] open: $url"

if (-not $NoBrowser) {
    Start-Process $url
}
