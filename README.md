# Hanstock

KIS Open API 기반 국내주식 자동매매와 해외선물 시그널 대시보드를 운영하는 Python/FastAPI 프로젝트입니다.

## 빠른 실행

아래 `scripts/local/`, `scripts/vm/`, `tools/` 경로가 공식 진입점입니다.
저장소 루트의 동명 `.ps1`/`.cmd` 파일은 기존 사용자 명령을 유지하기 위한
호환 래퍼이며, 새 운영 절차에서는 사용하지 않습니다. 내부 문서와 자동화가
공식 경로로 전환된 뒤 다음 주요 릴리스에서 제거합니다.

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

## 배포 의존성

운영 배포에서는 검증된 버전을 고정하는 constraints 파일을 함께 사용합니다.

```powershell
pip install --require-hashes -r constraints/vm-python.lock
```

`requirements-*.txt`는 지원 버전 범위를, `constraints-deploy.txt`는 lock 생성용
버전 기준을, `constraints/vm-python.lock`은 간접 의존성과 패키지 해시까지 고정한
운영 설치본을 나타냅니다. 버전 갱신은 테스트 통과 후 별도 변경으로 수행합니다.
Windows 음성 기능은 별도로 고정된 `constraints/voice-windows.lock`을
`pip install --require-hashes -r constraints/voice-windows.lock`로 설치합니다.
현재 VM lock은 운영 VM과 동일한 Linux/Python 3.10 환경에서 생성·검증합니다.

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

`doc/S1.한스톡사용설명서.md`가 공식 운영 문서입니다. `doc/`와 `docPlan/`의
나머지 분석·설계 문서는 구현 배경을 보존한 참고 자료이며, 현재 운영 명령은
공식 사용설명서와 이 README를 우선합니다.
