"""Restore and curate Hanstock watchlists from an SQLite backup."""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

from src.config import config
from src.strategy.watchlist_policy import eligibility_reason
from src.trader import KIStockAPI


def _rows(path: Path, table: str) -> list[tuple]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        return conn.execute(
            f"SELECT strategy_id, symbol, name, created_at FROM {table}"
            if table == "strategy_universe"
            else "SELECT symbol, name, created_at FROM watchlist"
        ).fetchall()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    source = args.source.resolve()
    target = Path(config.trade_db_path).resolve()
    strategy_rows = _rows(source, "strategy_universe")
    source_watchlist = _rows(source, "watchlist")

    with sqlite3.connect(target) as conn:
        current_watchlist = conn.execute(
            "SELECT symbol, name, created_at FROM watchlist"
        ).fetchall()

    names: dict[str, str] = {}
    for symbol, name, _ in source_watchlist + current_watchlist:
        names[str(symbol)] = str(name or symbol)
    for _, symbol, name, _ in strategy_rows:
        names[str(symbol)] = str(name or symbol)

    api = KIStockAPI(notify_errors=False)
    eligible: set[str] = set()
    rejected: dict[str, str] = {}
    quotes: dict[str, dict] = {}
    for symbol in sorted(names):
        quote = api.get_quote(symbol)
        quotes[symbol] = quote
        reason = eligibility_reason(
            price=quote.get("current"),
            market_cap=quote.get("market_cap"),
            known_mid_large=True,
        )
        if reason:
            rejected[symbol] = reason
        else:
            eligible.add(symbol)

    restored_strategy_rows = [
        row for row in strategy_rows if str(row[1]) in eligible
    ]
    restored_strategy_rows.extend(
        [
            ("ai_stock_default_v1", symbol, names[symbol], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            for symbol in sorted(eligible)
        ]
    )
    now = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = target.parent / "backups" / f"trades-before-watchlist-restore-{now}.sqlite"

    print(
        {
            "source_watchlist": len(source_watchlist),
            "source_strategy_universe": len(strategy_rows),
            "eligible_symbols": len(eligible),
            "restored_strategy_rows": len(restored_strategy_rows),
            "rejected_symbols": len(rejected),
            "rejected": rejected,
            "apply": args.apply,
        }
    )
    if not args.apply:
        return 0

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(target) as source_conn, sqlite3.connect(backup_path) as backup_conn:
        source_conn.backup(backup_conn)

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(target) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM watchlist")
        conn.executemany(
            "INSERT INTO watchlist(symbol, name, created_at) VALUES (?, ?, ?)",
            [(symbol, names[symbol], created_at) for symbol in sorted(eligible)],
        )
        conn.execute("DELETE FROM strategy_universe")
        conn.executemany(
            """
            INSERT INTO strategy_universe(strategy_id, symbol, name, created_at)
            VALUES (?, ?, ?, ?)
            """,
            restored_strategy_rows,
        )
        conn.commit()
    print({"backup": str(backup_path), "target": str(target)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
