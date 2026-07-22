param(
    [string]$HostName = $(if ($env:HANSTOCK_OCI_HOST) { $env:HANSTOCK_OCI_HOST } else { "168.110.102.249" }),
    [string]$User = $(if ($env:HANSTOCK_OCI_USER) { $env:HANSTOCK_OCI_USER } else { "ubuntu" }),
    [string]$RepoPath = $(if ($env:HANSTOCK_OCI_PATH) { $env:HANSTOCK_OCI_PATH } else { "~/hanstock" }),
    [string]$Branch = "main",
    [string]$KeyPath = $(if ($env:HANSTOCK_OCI_SSH_KEY) { $env:HANSTOCK_OCI_SSH_KEY } else { (Join-Path $env:USERPROFILE ".ssh\id_ed25519") }),
    [string]$RepoUrl = $(if ($env:HANSTOCK_REPO_URL) { $env:HANSTOCK_REPO_URL } else { "https://github.com/turtler501m-ai/hanstock_ora.git" }),
    [switch]$FreshClone,
    [switch]$SkipPush
)

$script = Join-Path $PSScriptRoot "deploy-vm.ps1"
& $script `
    -HostName $HostName `
    -User $User `
    -RepoPath $RepoPath `
    -Branch $Branch `
    -KeyPath $KeyPath `
    -RepoUrl $RepoUrl `
    -FreshClone:$FreshClone `
    -SkipPush:$SkipPush

exit $LASTEXITCODE
