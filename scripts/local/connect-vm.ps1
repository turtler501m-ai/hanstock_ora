param(
    [string]$HostName = $(if ($env:HANSTOCK_VM_HOST) { $env:HANSTOCK_VM_HOST } else { "168.110.102.249" }),
    [string]$User = $(if ($env:HANSTOCK_VM_USER) { $env:HANSTOCK_VM_USER } else { "ubuntu" }),
    [string]$Instance = $(if ($env:HANSTOCK_GCP_INSTANCE) { $env:HANSTOCK_GCP_INSTANCE } else { "" }),
    [string]$Zone = $(if ($env:HANSTOCK_GCP_ZONE) { $env:HANSTOCK_GCP_ZONE } else { "us-central1-c" }),
    [string]$Project = $(if ($env:HANSTOCK_GCP_PROJECT) { $env:HANSTOCK_GCP_PROJECT } else { "" }),
    [string]$KeyPath = $(if ($env:HANSTOCK_SSH_KEY) { $env:HANSTOCK_SSH_KEY } else { (Join-Path $env:USERPROFILE ".ssh\id_ed25519") })
)

$ErrorActionPreference = "Stop"

$ssh = Join-Path $env:WINDIR "System32\OpenSSH\ssh.exe"
if (-not (Test-Path -LiteralPath $ssh)) {
    $sshCommand = Get-Command ssh -ErrorAction SilentlyContinue
    if (-not $sshCommand) {
        throw "OpenSSH client was not found."
    }
    $ssh = $sshCommand.Source
}

if (-not $HostName) {
    $gcloud = Join-Path $env:LOCALAPPDATA "Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
    if (-not (Test-Path -LiteralPath $gcloud)) {
        $gcloudCommand = Get-Command gcloud -ErrorAction SilentlyContinue
        if (-not $gcloudCommand) {
            throw "gcloud command was not found."
        }
        $gcloud = $gcloudCommand.Source
    }

    $HostName = & $gcloud compute instances describe $Instance `
        --zone $Zone `
        --project $Project `
        --format "value(networkInterfaces[0].accessConfigs[0].natIP)"

    if (-not $HostName) {
        throw "Could not find an external IP for $Instance."
    }
}

$target = "$User@$HostName"
Write-Host "Opening SSH: $target ($Instance)"

if (Test-Path -LiteralPath $KeyPath) {
    & $ssh -t -i $KeyPath -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 $target
} else {
    & $ssh -t -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 $target
}
