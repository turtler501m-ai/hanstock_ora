# Hanstock

KIS Open API 기반 국내주식 자동매매와 해외선물 시그널 대시보드를 운영하는 Python/FastAPI 프로젝트입니다.

## 빠른 실행

로컬 Windows:

```powershell
.\scripts\local\server.cmd restart
```

VM/Linux:

```bash
./scripts/vm/server.sh restart
```

## 자동 배포

기본 배포 대상은 OCI 운영 VM(`168.110.102.249`, user `ubuntu`)입니다. 대시보드는 VM의 `127.0.0.1:8000`에 바인딩되므로 `scripts/local/connect-vm.ps1` 또는 SSH 터널을 통해 접속합니다. 자세한 대상/환경변수는 `scripts/local/README.md` 참조.

```powershell
.\scripts\local\deploy-vm.ps1
```

VM 폴더를 백업하고 새로 clone해서 현행화:

```powershell
.\scripts\local\deploy-vm.ps1 -FreshClone
```

## 검증

```powershell
powershell -ExecutionPolicy Bypass -File tools\verify-local.ps1
python -m unittest discover -s tests -t .
```

## 문서

전체 사용설명서는 아래 단일 문서에 정리되어 있습니다.

```text
doc/S1.한스톡사용설명서.md
```
