# Repository Guidelines

## Project Structure & Module Organization

Hanstock is a Python trading platform with FastAPI dashboards and an Android client.

- `src/`: application code and entry points (`dashboard.py`, `trader.py`)
- `src/api/`: KIS, futures, Bybit, LS, and QuantConnect clients
- `src/dashboard/`: dashboard routes, services, and presenters
- `src/db/`: bounded persistence repositories and migrations
- `src/strategy/`, `src/ai_stock/`, `src/futures_signals/`: trading and analysis domains
- `web/templates/`, `web/static/`: server-rendered pages, CSS, and JavaScript
- `android/`: Gradle-based Android client
- `tests/`: Python `unittest` suite
- `config/`: checked-in, non-secret configuration
- `scripts/local/`, `scripts/vm/`: Windows development and Linux VM operations
- `tools/`: verification and maintenance utilities
- `doc/`: project manuals and technical notes

Put generated state only in `.runtime/`, `logs/`, or `data/`. Keep application code, tests, and web assets free of runtime output.

## Build, Test, and Development Commands

Create a local environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Run or inspect the dashboard with `scripts\local\server.cmd restart|status|logs|tail`; open `http://127.0.0.1:8000`. Run the engine directly with `python src\trader.py`.

Before committing significant changes, run:

```powershell
powershell -ExecutionPolicy Bypass -File tools\verify-local.ps1
python -m unittest discover -s tests
powershell -ExecutionPolicy Bypass -File tools\check-encoding.ps1
```

Deploy from a clean local branch with `scripts\local\deploy-vm.ps1`. Do not edit VM source directly.

## Coding Style & Naming Conventions

Follow `.editorconfig`: UTF-8, LF endings, final newline, and no trailing whitespace. Use four-space Python indentation. Name modules and functions `snake_case`, classes `PascalCase`, and constants/environment variables `UPPER_SNAKE_CASE`. Keep API, database, dashboard, and strategy logic in their existing bounded packages. Do not hardcode credentials or account identifiers.

## Testing Guidelines

Use deterministic `unittest` tests named `test_*.py`. Mock KIS, Telegram, Slack, OpenAI, QuantConnect, Bybit, and other network calls. Add regression coverage for trading logic, routes, persistence, configuration, and deployment behavior. There is no fixed coverage threshold; changed behavior must be exercised.

## Commit & Pull Request Guidelines

Recent history uses short, behavior-focused Korean subjects, for example `전략조회 완료 팝업 제거`; concise English is also acceptable. Keep commits scoped and exclude unrelated local changes. Pull requests should explain behavior and risk, list verification commands, link relevant issues, and include screenshots for dashboard or Android UI changes.

## Security & Trading Safety

Default to `DRY_RUN=true`, `TRADING_ENV=demo`, `ENABLE_LIVE_TRADING=false`, and `REQUIRE_APPROVAL=true`. Never commit `.env`, keys, tokens, account numbers, Telegram sessions, databases, logs, or `.runtime/`. Local and VM `.env` files are separate operational secrets.
