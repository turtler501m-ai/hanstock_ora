param(
    [string]$HostName = $(if ($env:HANSTOCK_OCI_HOST) { $env:HANSTOCK_OCI_HOST } else { "168.110.102.249" }),
    [string]$User = $(if ($env:HANSTOCK_OCI_USER) { $env:HANSTOCK_OCI_USER } else { "ubuntu" }),
    [string]$RepoPath = $(if ($env:HANSTOCK_OCI_PATH) { $env:HANSTOCK_OCI_PATH } else { "~/hanstock" }),
    [string]$KeyPath = $(if ($env:HANSTOCK_OCI_SSH_KEY) { $env:HANSTOCK_OCI_SSH_KEY } else { (Join-Path $env:USERPROFILE ".ssh\id_ed25519") }),
    [int]$LogLines = 40,
    [switch]$SkipMistock
)

$script = Join-Path $PSScriptRoot "check-vm.ps1"
& $script `
    -HostName $HostName `
    -User $User `
    -RepoPath $RepoPath `
    -KeyPath $KeyPath `
    -LogLines $LogLines `
    -SkipMistock:$SkipMistock

exit $LASTEXITCODE
