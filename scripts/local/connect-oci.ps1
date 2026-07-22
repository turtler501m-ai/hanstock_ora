param(
    [string]$HostName = $(if ($env:HANSTOCK_OCI_HOST) { $env:HANSTOCK_OCI_HOST } else { "168.110.102.249" }),
    [string]$User = $(if ($env:HANSTOCK_OCI_USER) { $env:HANSTOCK_OCI_USER } else { "ubuntu" }),
    [string]$KeyPath = $(if ($env:HANSTOCK_OCI_SSH_KEY) { $env:HANSTOCK_OCI_SSH_KEY } else { (Join-Path $env:USERPROFILE ".ssh\id_ed25519") })
)

$script = Join-Path $PSScriptRoot "connect-vm.ps1"
& $script -HostName $HostName -User $User -KeyPath $KeyPath

exit $LASTEXITCODE
