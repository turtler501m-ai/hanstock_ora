param(
    # 기본 대상은 현재 OCI 운영 VM입니다. 필요하면 HANSTOCK_VM_HOST로 덮어씁니다.
    # gcloud 계정에 신규 프로젝트 compute.instances.get 권한이 없어 IP 직접 지정으로 동작한다.
    [string]$HostName = $(if ($env:HANSTOCK_VM_HOST) { $env:HANSTOCK_VM_HOST } else { "168.110.102.249" }),
    [string]$User = $(if ($env:HANSTOCK_VM_USER) { $env:HANSTOCK_VM_USER } else { "ubuntu" }),
    [string]$RepoPath = $(if ($env:HANSTOCK_VM_PATH) { $env:HANSTOCK_VM_PATH } else { "~/hanstock" }),
    [string]$Branch = "main",
    [string]$Instance = $(if ($env:HANSTOCK_GCP_INSTANCE) { $env:HANSTOCK_GCP_INSTANCE } else { "" }),
    [string]$Zone = $(if ($env:HANSTOCK_GCP_ZONE) { $env:HANSTOCK_GCP_ZONE } else { "us-central1-c" }),
    [string]$Project = $(if ($env:HANSTOCK_GCP_PROJECT) { $env:HANSTOCK_GCP_PROJECT } else { "" }),
    [string]$KeyPath = $(if ($env:HANSTOCK_SSH_KEY) { $env:HANSTOCK_SSH_KEY } else { (Join-Path $env:USERPROFILE ".ssh\id_ed25519") }),
    [string]$BackupRoot = $(if ($env:HANSTOCK_VM_BACKUP_ROOT) { $env:HANSTOCK_VM_BACKUP_ROOT } else { "~/hanstock_backups" }),
    [string]$RepoUrl = $(if ($env:HANSTOCK_REPO_URL) { $env:HANSTOCK_REPO_URL } else { "https://github.com/turtler501m-ai/hanstock_ora.git" }),
    [switch]$FreshClone,
    [switch]$SkipPush
)

$ErrorActionPreference = "Stop"
$OutputEncoding = New-Object System.Text.UTF8Encoding -ArgumentList $false
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding -ArgumentList $false

function Get-GcloudPath {
    $default = Join-Path $env:LOCALAPPDATA "Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
    if (Test-Path -LiteralPath $default) {
        return $default
    }

    $command = Get-Command gcloud -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    throw "gcloud command was not found. Set HANSTOCK_VM_HOST manually or install Google Cloud SDK."
}

function Get-SshPath {
    $default = Join-Path $env:WINDIR "System32\OpenSSH\ssh.exe"
    if (Test-Path -LiteralPath $default) {
        return $default
    }

    $command = Get-Command ssh -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    throw "OpenSSH client was not found."
}

function Get-ScpPath {
    $default = Join-Path $env:WINDIR "System32\OpenSSH\scp.exe"
    if (Test-Path -LiteralPath $default) {
        return $default
    }

    $command = Get-Command scp -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    throw "OpenSSH scp client was not found."
}

function Resolve-GcpHost {
    param(
        [string]$InstanceName,
        [string]$InstanceZone,
        [string]$ProjectId
    )

    $gcloud = Get-GcloudPath
    $ip = & $gcloud compute instances describe $InstanceName `
        --zone $InstanceZone `
        --project $ProjectId `
        --format "value(networkInterfaces[0].accessConfigs[0].natIP)"

    if (-not $ip) {
        throw "Could not find an external IP for $InstanceName."
    }

    return $ip.Trim()
}

$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $Root

if (-not $HostName) {
    $HostName = Resolve-GcpHost -InstanceName $Instance -InstanceZone $Zone -ProjectId $Project
}

$ssh = Get-SshPath
$target = if ($User) { "$User@$HostName" } else { $HostName }

if (-not $SkipPush) {
    $status = git status --porcelain | Where-Object {
        $_ -notmatch '^\?\? doc/.*\.md$' -and
        $_ -notmatch '^\?\? doc\\.*\.md$'
    }
    if ($status) {
        throw "Working tree is not clean. Commit or stash changes before deploy, or use -SkipPush to deploy the current remote state."
    }

    git push origin $Branch
}

$remoteCommand = @'
set -e
BRANCH="__BRANCH__"
REPO_PATH="__REPO_PATH__"
REPO_URL="__REPO_URL__"
BACKUP_ROOT="__BACKUP_ROOT__"
FRESH_CLONE="__FRESH_CLONE__"

REPO_PATH="${REPO_PATH/#\~/$HOME}"
BACKUP_ROOT="${BACKUP_ROOT/#\~/$HOME}"

if [ "$FRESH_CLONE" = "1" ] && [ -e "$REPO_PATH" ]; then
  stamp="$(date +%Y%m%d-%H%M%S)"
  backup_path="$BACKUP_ROOT/hanstock-$stamp"
  mkdir -p "$BACKUP_ROOT"
  echo "[deploy] moving existing repo to $backup_path"
  mv "$REPO_PATH" "$backup_path"
fi

if [ ! -d "$REPO_PATH/.git" ]; then
  mkdir -p "$(dirname "$REPO_PATH")"
  git clone "$REPO_URL" "$REPO_PATH"
  if [ -n "${backup_path:-}" ] && [ -f "$backup_path/.env" ] && [ ! -f "$REPO_PATH/.env" ]; then
    echo "[deploy] copying .env from backup"
    cp "$backup_path/.env" "$REPO_PATH/.env"
  fi
fi
cd "$REPO_PATH"
git fetch origin "$BRANCH"

conflicting_shell_files="$(
  git ls-tree -r --name-only "origin/$BRANCH" -- '*.sh' |
  while IFS= read -r path; do
    if [ -e "$path" ] && ! git ls-files --error-unmatch "$path" >/dev/null 2>&1; then
      printf '%s\n' "$path"
    fi
  done
)"

if [ -n "$conflicting_shell_files" ]; then
  stamp="$(date +%Y%m%d-%H%M%S)"
  conflict_backup="$REPO_PATH/.runtime/deploy-untracked-shell-$stamp"
  mkdir -p "$conflict_backup"
  echo "$conflicting_shell_files" |
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    mkdir -p "$conflict_backup/$(dirname "$path")"
    mv "$path" "$conflict_backup/$path"
    echo "[deploy] moved untracked shell file $path to $conflict_backup/$path"
  done
fi

./scripts/vm/update.sh "$BRANCH"
'@

$remoteCommand = $remoteCommand.
    Replace("__BRANCH__", $Branch).
    Replace("__REPO_PATH__", $RepoPath).
    Replace("__REPO_URL__", $RepoUrl).
    Replace("__BACKUP_ROOT__", $BackupRoot).
    Replace("__FRESH_CLONE__", $(if ($FreshClone) { "1" } else { "0" }))
$remoteCommand = $remoteCommand -replace "`r`n", "`n" -replace "`r", "`n"

Write-Host "[deploy] target: $target"
Write-Host "[deploy] repo: $RepoPath"
Write-Host "[deploy] remote: $RepoUrl"
Write-Host "[deploy] branch: $Branch"
if ($FreshClone) {
    Write-Host "[deploy] fresh clone: enabled"
    Write-Host "[deploy] backup root: $BackupRoot"
}

$scp = Get-ScpPath
$tempScript = Join-Path ([System.IO.Path]::GetTempPath()) ("hanstock-deploy-{0}.sh" -f ([guid]::NewGuid().ToString("N")))
$remoteScript = "/tmp/hanstock-deploy-$(Get-Random).sh"
$utf8NoBom = New-Object System.Text.UTF8Encoding -ArgumentList $false
[System.IO.File]::WriteAllText($tempScript, $remoteCommand, $utf8NoBom)

try {
    if (Test-Path -LiteralPath $KeyPath) {
        & $scp -i $KeyPath -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 $tempScript "${target}:$remoteScript"
        if ($LASTEXITCODE -ne 0) {
            throw "VM deploy upload failed with exit code $LASTEXITCODE"
        }
        & $ssh -i $KeyPath -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 $target "bash '$remoteScript'; status=`$?; rm -f '$remoteScript'; exit `$status"
    } else {
        & $scp -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 $tempScript "${target}:$remoteScript"
        if ($LASTEXITCODE -ne 0) {
            throw "VM deploy upload failed with exit code $LASTEXITCODE"
        }
        & $ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 $target "bash '$remoteScript'; status=`$?; rm -f '$remoteScript'; exit `$status"
    }
}
finally {
    Remove-Item -LiteralPath $tempScript -Force -ErrorAction SilentlyContinue
}

if ($LASTEXITCODE -ne 0) {
    throw "VM deploy failed with exit code $LASTEXITCODE"
}
