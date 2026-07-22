# Local Windows Scripts

로컬 Windows 개발 PC에서 사용하는 스크립트입니다.

## 대시보드 서버

권장 명령은 프로젝트 루트의 래퍼입니다.

```powershell
.\server.cmd restart
.\server.cmd status
.\server.cmd logs
.\server.cmd tail
```

직접 호출하려면 아래 명령을 사용합니다.

```powershell
.\scripts\local\server.cmd restart
```

`scripts/local/server.cmd`는 `tools/server.ps1`을 호출합니다.

## 로컬 검증

```powershell
.\verify.cmd
```

내부적으로 `tools/verify-local.ps1`을 실행합니다.

## Telegram 1회 수집

```powershell
.\scripts\local\telegram_poll.ps1
```

## VM 배포와 접속

```powershell
.\deploy-oci.ps1
.\deploy-oci.ps1 -SkipPush
.\connect-oci.ps1
.\check-oci.ps1
.\deploy-vm.ps1
.\deploy-vm.ps1 -SkipPush
.\deploy-vm.ps1 -FreshClone
.\connect-vm.ps1
.\check-vm.ps1
.\vm-dashboard.cmd
.\scripts\local\stock1.ps1   # 신규 VM SSH 접속 래퍼
```

기본 VM 대상(신규 운영 VM):

```text
host: 168.110.102.249 (http://168.110.102.249:8000)
user: ubuntu
key: ~/.ssh/id_ed25519
repo: ~/hanstock
```

Oracle VM 기본 대상:

```text
host: 168.110.102.249 (http://168.110.102.249:8000)
user: ubuntu
key: ~/.ssh/id_ed25519
repo: ~/hanstock
source: https://github.com/turtler501m-ai/hanstock_ora.git
```

> 참고: 이 gcloud 계정은 신규 프로젝트의 `compute.instances.get` 권한이 없어
> 기본값은 현재 OCI 운영 IP(`168.110.102.249`)를 직접 사용한다. 외부 IP가
> 바뀌면 `$env:HANSTOCK_VM_HOST`로 덮어쓰거나 스크립트 기본값을 갱신할 것.

환경변수로 대상을 바꿀 수 있습니다(예: 다른 VM/구 VM 접속).

```powershell
$env:HANSTOCK_VM_HOST="168.110.102.249"
$env:HANSTOCK_VM_USER="ubuntu"
$env:HANSTOCK_VM_PATH="~/hanstock"
# gcloud 해석을 쓰는 경우에만 필요(권한 있는 프로젝트):
$env:HANSTOCK_GCP_ZONE="us-central1-c"
```
