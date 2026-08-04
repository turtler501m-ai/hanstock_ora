# VM/Linux Scripts

VM 또는 Linux 서버에서 사용하는 스크립트입니다. Windows 로컬에서는 이 폴더의 스크립트를 직접 실행하지 않습니다.

## 대시보드 서버

```bash
./scripts/vm/server.sh restart
./scripts/vm/server.sh status
./scripts/vm/server.sh logs
./scripts/vm/server.sh tail
```

## 최신 코드 반영

```bash
./scripts/vm/update.sh main
```

`update.sh`는 아래 순서로 동작합니다.

1. `.env` 존재 여부 확인
2. `git fetch`
3. 지정 브랜치 checkout/pull
4. `.venv` 없으면 생성
5. `constraints/vm-python.lock`을 `--require-hashes`로 설치
6. 대시보드 재시작
7. 서버 상태 출력

## Telegram 시그널 수집

```bash
./scripts/vm/poll.sh
./scripts/vm/polld.sh
./scripts/vm/signals.sh
```

강제 1회 실행이 필요하면:

```bash
POLLING_FORCE_RUN=1 ./scripts/vm/poll.sh
```

## Daily AI Auto Review/Rebalance

Run one cycle manually:

```bash
./scripts/vm/daily-auto.sh
```

Install the weekday cron schedule. The default is hourly during Korea stock processing hours, 09:00 through 15:20 KST, Monday-Friday:

```bash
./scripts/vm/install-daily-auto-cron.sh
```

Use a custom cron time by passing the first five cron fields:

```bash
./scripts/vm/install-daily-auto-cron.sh "35 15 * * 1-5"
```

The installer writes `CRON_TZ=Asia/Seoul`. Override it with `HANSTOCK_CRON_TZ` if the VM cron does not support that timezone.

Logs are written to `logs/daily-auto.log`.
