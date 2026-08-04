# 실행 스크립트 기준

이 프로젝트의 실행 스크립트는 사용 위치별로 분리합니다.

## 로컬 Windows

일반 사용자는 프로젝트 루트의 짧은 래퍼 명령을 사용합니다.

```powershell
.\scripts\local\server.cmd restart
.\scripts\local\server.cmd status
.\scripts\local\server.cmd logs
.\scripts\local\server.cmd tail
powershell -ExecutionPolicy Bypass -File .\tools\verify-local.ps1
```

Telegram poll 1회 실행:

```powershell
.\scripts\local\telegram_poll.ps1
```

VM 자동 배포와 접속:

```powershell
.\scripts\local\deploy-vm.ps1
.\scripts\local\connect-vm.ps1
.\scripts\local\check-vm.ps1
.\scripts\local\vm-dashboard.ps1
```

내부 구조:

```text
server.cmd     -> scripts/local/server.cmd -> tools/server.ps1
verify.cmd     -> tools/verify-local.ps1
deploy-vm.ps1  -> scripts/local/deploy-vm.ps1
connect-vm.ps1 -> scripts/local/connect-vm.ps1
check-vm.ps1   -> scripts/local/check-vm.ps1
vm-dashboard.cmd -> scripts/local/vm-dashboard.ps1
```

## VM/Linux

VM에서는 Windows용 래퍼를 사용하지 않고 `scripts/vm`만 사용합니다.

```bash
./scripts/vm/server.sh restart
./scripts/vm/server.sh status
./scripts/vm/server.sh logs
./scripts/vm/server.sh tail
./scripts/vm/update.sh main
```

Telegram 시그널 수집:

```bash
./scripts/vm/poll.sh
./scripts/vm/polld.sh
./scripts/vm/signals.sh
```

## 원칙

- 로컬 개발자는 루트 래퍼 또는 `scripts/local`과 `tools`만 사용합니다.
- VM에서는 `scripts/vm`만 사용합니다.
- 실제 서버 제어 구현은 `tools/server.ps1`과 `scripts/vm/server.sh`에만 둡니다.
- 루트 래퍼에는 복잡한 로직을 넣지 않고 내부 스크립트로만 위임합니다.
- `.env`, `.runtime`, `logs`, DB, 토큰, 세션 파일은 스크립트로도 Git에 올리지 않습니다.
