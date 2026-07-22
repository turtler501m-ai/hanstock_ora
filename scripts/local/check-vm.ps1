param(
    [string]$HostName = $(if ($env:HANSTOCK_VM_HOST) { $env:HANSTOCK_VM_HOST } else { "168.110.102.249" }),
    [string]$User = $(if ($env:HANSTOCK_VM_USER) { $env:HANSTOCK_VM_USER } else { "ubuntu" }),
    [string]$RepoPath = $(if ($env:HANSTOCK_VM_PATH) { $env:HANSTOCK_VM_PATH } else { "~/hanstock" }),
    [string]$Instance = $(if ($env:HANSTOCK_GCP_INSTANCE) { $env:HANSTOCK_GCP_INSTANCE } else { "" }),
    [string]$Zone = $(if ($env:HANSTOCK_GCP_ZONE) { $env:HANSTOCK_GCP_ZONE } else { "us-central1-c" }),
    [string]$Project = $(if ($env:HANSTOCK_GCP_PROJECT) { $env:HANSTOCK_GCP_PROJECT } else { "" }),
    [string]$KeyPath = $(if ($env:HANSTOCK_SSH_KEY) { $env:HANSTOCK_SSH_KEY } else { (Join-Path $env:USERPROFILE ".ssh\id_ed25519") }),
    [int]$LogLines = 40,
    [switch]$SkipMistock
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

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

if (-not $HostName) {
    $HostName = Resolve-GcpHost -InstanceName $Instance -InstanceZone $Zone -ProjectId $Project
}

$ssh = Get-ToolPath -Name "ssh" -DefaultPath (Join-Path $env:WINDIR "System32\OpenSSH\ssh.exe")
$target = "$User@$HostName"
$safeRepoPath = $RepoPath.Replace("'", "'\''")
$safeLogLines = [Math]::Max(1, [Math]::Min($LogLines, 300))

$remoteCommand = @'
set -e
REPO_PATH='__REPO_PATH__'
REPO_PATH="${REPO_PATH/#\~/$HOME}"

echo "== VM =="
hostname
date
echo "target=__TARGET__"
echo "repo=$REPO_PATH"

echo
echo "== Server =="
if [ -x "$REPO_PATH/scripts/vm/server.sh" ]; then
  cd "$REPO_PATH"
  ./scripts/vm/server.sh status || true
else
  echo "server script not found: $REPO_PATH/scripts/vm/server.sh"
fi

echo
echo "== Daily Auto Cron =="
crontab -l 2>/dev/null | sed -n '/# hanstock-daily-auto begin/,/# hanstock-daily-auto end/p' || true

echo
echo "== Strategy Dispatch Cron =="
crontab -l 2>/dev/null | sed -n '/# hanstock-strategy-dispatch begin/,/# hanstock-strategy-dispatch end/p' || true

echo
echo "== Strategy Dispatch State =="
if [ -x "$REPO_PATH/.venv/bin/python3" ]; then
  PYTHON_BIN="$REPO_PATH/.venv/bin/python3"
else
  PYTHON_BIN="$(command -v python3 || command -v python)"
fi
PYTHONPATH="$REPO_PATH" "$PYTHON_BIN" - <<'PY'
from src.db.repository import list_strategy_schedules, load_strategy_universe

schedules = list_strategy_schedules(enabled_only=False)
if not schedules:
    print("no strategy schedules configured")
else:
    for item in schedules:
        strategy_id = item.get("strategy_id")
        universe_count = len(load_strategy_universe(strategy_id))
        enabled = "enabled" if item.get("enabled") else "disabled"
        print(
            "{strategy_id}: {enabled}, interval={interval}m, window={start}-{end}, universe={universe}, last_run={last}".format(
                strategy_id=strategy_id,
                enabled=enabled,
                interval=item.get("interval_minutes"),
                start=item.get("start_hm"),
                end=item.get("end_hm"),
                universe=universe_count,
                last=item.get("last_run_at"),
            )
        )
PY

echo
echo "== Latest Strategy Dispatch Log =="
if [ -f "$REPO_PATH/logs/strategy-dispatch.log" ]; then
  tail -n __LOG_LINES__ "$REPO_PATH/logs/strategy-dispatch.log"
else
  echo "strategy-dispatch log not found"
fi

echo
echo "== Latest Daily Auto Log =="
if [ -f "$REPO_PATH/logs/daily-auto.log" ]; then
  tail -n __LOG_LINES__ "$REPO_PATH/logs/daily-auto.log"
else
  echo "daily-auto log not found"
fi

if [ "__SKIP_MISTOCK__" != "1" ]; then
  echo
  echo "== Mistock Cron =="
  crontab -l 2>/dev/null | sed -n '/# hanstock-mistock-auto begin/,/# hanstock-mistock-auto end/p' || true

  echo
  echo "== Latest Mistock Auto Log =="
  if [ -f "$REPO_PATH/logs/mistock-auto.log" ]; then
    tail -n __LOG_LINES__ "$REPO_PATH/logs/mistock-auto.log"
  else
    echo "mistock-auto log not found"
  fi

  echo
  echo "== Latest Mistock Monitor Log =="
  if [ -f "$REPO_PATH/logs/mistock_monitor.log" ]; then
    tail -n __LOG_LINES__ "$REPO_PATH/logs/mistock_monitor.log"
  else
    echo "mistock monitor log not found"
  fi

  echo
  echo "== Mistock Latest Scheduler Result =="
  if [ -f "$REPO_PATH/.runtime/mistock/daily_auto_last_result.json" ]; then
    python3 - "$REPO_PATH/.runtime/mistock/daily_auto_last_result.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
result = data.get("result") or {}
print("recorded_at=" + str(data.get("recorded_at")))
print("status=" + str(result.get("status")) + " ok=" + str(result.get("ok")))
print("scanned=" + str(result.get("scanned")) + " candidates=" + str(result.get("candidates")))
print("sold=" + str(len(result.get("sold") or [])) + " bought=" + str(len(result.get("bought") or [])) + " plan=" + str(len(result.get("plan") or [])))
errors = result.get("errors") or []
if errors:
    print("errors:")
    for item in errors[:20]:
        print("- {symbol} {action}: {message}".format(
            symbol=item.get("symbol", "UNKNOWN"),
            action=item.get("action", ""),
            message=item.get("message", ""),
        ))
else:
    print("errors=[]")
PY
  else
    echo "mistock latest result not found"
  fi

  echo
  echo "== Mistock Failed Trades =="
  if command -v sqlite3 >/dev/null 2>&1 && [ -f "$REPO_PATH/.runtime/mistock/trades.sqlite" ]; then
    sqlite3 -header -column "$REPO_PATH/.runtime/mistock/trades.sqlite" \
      "SELECT id, ts, symbol, action, qty, price, ok, response_msg FROM trades WHERE ok = 0 ORDER BY id DESC LIMIT 20;"
  elif [ -f "$REPO_PATH/.runtime/mistock/trades.sqlite" ]; then
    python3 - "$REPO_PATH/.runtime/mistock/trades.sqlite" <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
conn.row_factory = sqlite3.Row
rows = conn.execute(
    """
    SELECT id, ts, symbol, action, qty, price, ok, response_msg
    FROM trades
    WHERE ok = 0
    ORDER BY id DESC
    LIMIT 20
    """
).fetchall()
if not rows:
    print("no failed trades")
for row in rows:
    print(
        "{id} | {ts} | {symbol} | {action} | qty={qty} | price={price} | ok={ok} | {response_msg}".format(
            **dict(row)
        )
    )
PY
  else
    echo "mistock trades db not found"
  fi
fi
'@

$remoteCommand = $remoteCommand.
    Replace("__REPO_PATH__", $safeRepoPath).
    Replace("__TARGET__", $target).
    Replace("__LOG_LINES__", [string]$safeLogLines).
    Replace("__SKIP_MISTOCK__", $(if ($SkipMistock) { "1" } else { "0" }))

$remoteCommand = $remoteCommand -replace "`r`n", "`n" -replace "`r", "`n"
$encodedCommand = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remoteCommand))

Write-Host "[vm-check] target: $target"
Write-Host "[vm-check] instance: $Instance"
Write-Host "[vm-check] repo: $RepoPath"

if (Test-Path -LiteralPath $KeyPath) {
    & $ssh -i $KeyPath -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 $target "echo $encodedCommand | base64 -d | bash"
} else {
    & $ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 $target "echo $encodedCommand | base64 -d | bash"
}

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[vm-check] SSH connection failed."
    Write-Host "[vm-check] target: $target"
    Write-Host "[vm-check] key: $KeyPath"
    Write-Host "[vm-check] If this is a publickey error, update HANSTOCK_SSH_KEY or add this public key to the VM user's authorized_keys / GCP OS Login metadata."
    throw "VM check failed with exit code $LASTEXITCODE"
}
