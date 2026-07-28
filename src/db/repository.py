import sqlite3
import os
import json
import hashlib
from pathlib import Path
from datetime import datetime, timedelta, timezone
from src.config import config
from src.db.connection import open_sqlite
from src.utils.logger import logger

KST = timezone(timedelta(hours=9))

class DBWrapper:
    def __init__(self, conn, is_pg=False, close_on_exit=False):
        self.conn = conn
        self.is_pg = is_pg
        self.close_on_exit = close_on_exit

    def __enter__(self):
        self.conn.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            return self.conn.__exit__(exc_type, exc_val, exc_tb)
        finally:
            if self.close_on_exit:
                self.conn.close()

    def execute(self, sql, params=()):
        if self.is_pg:
            from psycopg2.extras import DictCursor

            sql = sql.replace("?", "%s")
            if "AUTOINCREMENT" in sql:
                sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
            
            cursor = self.conn.cursor(cursor_factory=DictCursor)
        else:
            cursor = self.conn.cursor()
            
        cursor.execute(sql, params)
        return cursor
        
    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()
        
    @property
    def row_factory(self):
        if self.is_pg:
            return None
        return self.conn.row_factory
        
    @row_factory.setter
    def row_factory(self, factory):
        if not self.is_pg:
            self.conn.row_factory = factory

def connect_db():
    db_url = os.environ.get("DATABASE_URL")
    if db_url and db_url.startswith("postgres"):
        try:
            import psycopg2
            from psycopg2.extras import DictCursor
        except ImportError as exc:
            raise RuntimeError(
                "Postgres DATABASE_URL requires psycopg2-binary to be installed"
            ) from exc
        conn = psycopg2.connect(db_url)
        return DBWrapper(conn, is_pg=True, close_on_exit=True)
    else:
        db_path = Path(config.trade_db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_sqlite(db_path)
        return DBWrapper(conn, is_pg=False, close_on_exit=True)

def init_db() -> None:
    with connect_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                action TEXT NOT NULL,
                qty INTEGER NOT NULL,
                price INTEGER NOT NULL,
                reason TEXT,
                ok INTEGER NOT NULL,
                env TEXT,
                dry_run INTEGER
            )
            """
        )
        _ensure_column(conn, "trades", "broker_order_id", "TEXT")
        _ensure_column(conn, "trades", "order_status", "TEXT")
        _ensure_column(conn, "trades", "filled_qty", "INTEGER DEFAULT 0")
        _ensure_column(conn, "trades", "filled_price", "INTEGER DEFAULT 0")
        _ensure_column(conn, "trades", "pre_order_qty", "INTEGER DEFAULT 0")
        _ensure_column(conn, "trades", "response_msg", "TEXT")
        _ensure_column(conn, "trades", "broker_result", "TEXT")
        _ensure_column(conn, "trades", "strategy_id", "TEXT")
        _ensure_column(conn, "trades", "strategy_version", "INTEGER")
        _ensure_column(conn, "trades", "profile_hash", "TEXT")
        _ensure_column(conn, "trades", "source_approval_id", "INTEGER")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS decision_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                action TEXT NOT NULL,
                qty INTEGER NOT NULL,
                price INTEGER NOT NULL,
                reason TEXT,
                indicators TEXT,
                approved INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scanned_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scanned_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                score INTEGER NOT NULL,
                reasons TEXT,
                price INTEGER,
                env TEXT NOT NULL,
                rsi REAL,
                rsi2 REAL,
                macd_hist REAL,
                sma20 REAL,
                sma60 REAL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scanned_candidates_scanned_at ON scanned_candidates(scanned_at)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist (
                symbol TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_strategies (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                weight REAL NOT NULL,
                description TEXT,
                selected INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        _ensure_column(conn, "ai_strategies", "status", "TEXT DEFAULT 'draft'")
        _ensure_column(conn, "ai_strategies", "profile_json", "TEXT")
        _ensure_column(conn, "ai_strategies", "strategy_version", "INTEGER DEFAULT 1")
        _ensure_column(conn, "ai_strategies", "profile_hash", "TEXT")
        _ensure_column(conn, "ai_strategies", "last_verified_at", "TEXT")
        _ensure_column(conn, "ai_strategies", "last_backtested_at", "TEXT")
        _ensure_column(conn, "ai_strategies", "last_paper_started_at", "TEXT")
        _ensure_column(conn, "ai_strategies", "last_paper_completed_at", "TEXT")
        _ensure_column(conn, "ai_strategies", "last_used_at", "TEXT")
        _ensure_column(conn, "ai_strategies", "last_validation_result", "TEXT")
        _ensure_column(conn, "scanned_candidates", "strategy_id", "TEXT")
        _ensure_column(conn, "scanned_candidates", "strategy_version", "INTEGER")
        _ensure_column(conn, "scanned_candidates", "profile_hash", "TEXT")
        _ensure_column(conn, "scanned_candidates", "ranker_model", "TEXT")
        _ensure_column(conn, "scanned_candidates", "optimizer", "TEXT")
        _ensure_column(conn, "scanned_candidates", "rule_score", "REAL")
        _ensure_column(conn, "scanned_candidates", "ml_score", "REAL")
        _ensure_column(conn, "scanned_candidates", "final_score", "REAL")
        _ensure_column(conn, "scanned_candidates", "ai_model_status", "TEXT")
        _ensure_column(conn, "scanned_candidates", "ai_fallback_reason", "TEXT")
        _ensure_column(conn, "scanned_candidates", "top_features_json", "TEXT")
        _ensure_column(conn, "scanned_candidates", "forward_return_1d", "REAL")
        _ensure_column(conn, "scanned_candidates", "forward_return_5d", "REAL")
        _ensure_column(conn, "scanned_candidates", "forward_return_20d", "REAL")
        _ensure_column(conn, "scanned_candidates", "return_updated_at", "TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_strategy_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                strategy_version INTEGER,
                event_type TEXT NOT NULL,
                payload TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS token_usage (
                date TEXT PRIMARY KEY,
                prompt_tokens INTEGER NOT NULL,
                completion_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL,
                api_calls INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auto_approval (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                action TEXT NOT NULL,
                qty INTEGER NOT NULL,
                price INTEGER NOT NULL,
                reason TEXT,
                source TEXT,
                status TEXT NOT NULL,
                response_msg TEXT
            )
            """
        )
        _ensure_column(conn, "approvals", "strategy_id", "TEXT")
        _ensure_column(conn, "approvals", "strategy_version", "INTEGER")
        _ensure_column(conn, "approvals", "profile_hash", "TEXT")
        _ensure_column(conn, "approvals", "source_candidate_id", "INTEGER")
        _ensure_column(conn, "approvals", "managed_order_id", "INTEGER")
        _ensure_column(conn, "approvals", "decision_id", "INTEGER")
        _ensure_column(conn, "approvals", "position_id", "INTEGER")
        _ensure_column(conn, "approvals", "client_order_key", "TEXT")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_managed_order_unique
            ON approvals(managed_order_id)
            WHERE managed_order_id IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_client_order_key_unique
            ON approvals(client_order_key)
            WHERE client_order_key IS NOT NULL AND client_order_key <> ''
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduler_results (
                recorded_at TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                result TEXT NOT NULL
            )
            """
        )
        _ensure_column(conn, "scheduler_results", "strategy_id", "TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_charts (
                symbol TEXT,
                date TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                PRIMARY KEY (symbol, date)
            )
            """
        )
        # 대시보드 라이브 데이터(잔고/보유/후보 등)의 마지막 성공본을 DB에 보관해
        # 불러오기 실패 시 파일 캐시 대신 DB로 폴백할 수 있게 한다.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS account_snapshots (
                account_key TEXT NOT NULL,
                trading_env TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                PRIMARY KEY (account_key, trading_env, kind)
            )
            """
        )
        # 전략별 스케쥴(대시보드에서 등록/제어, VM 디스패처가 읽어 실행)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_schedules (
                strategy_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                interval_minutes INTEGER NOT NULL DEFAULT 15,
                start_hm TEXT NOT NULL DEFAULT '0900',
                end_hm TEXT NOT NULL DEFAULT '1530',
                weekdays TEXT NOT NULL DEFAULT '1-5',
                mode TEXT NOT NULL DEFAULT 'execute',
                auto_approve INTEGER NOT NULL DEFAULT 1,
                last_run_at TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        # 전략별 전용 유니버스(스캔 대상 종목)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_universe (
                strategy_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (strategy_id, symbol)
            )
            """
        )

        # 일회성 관심종목 클린업 마이그레이션 (더 많은 AI 자동 추가 자리를 확보하기 위함)
        try:
            c_mig = conn.execute("SELECT value FROM watchlist_settings WHERE key = 'migration_watchlist_cleaned_v3'")
            row_mig = c_mig.fetchone()
            if row_mig is None or row_mig[0] != '1':
                c = conn.execute("SELECT COUNT(*) FROM watchlist")
                count = c.fetchone()[0]
                if count > 20:
                    conn.execute("DELETE FROM watchlist")
                    ts = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S")
                    default_symbols = ["005930", "000660", "035420", "005380", "035720"]
                    from src.market_metadata import resolve_stock_name

                    for s in default_symbols:
                        conn.execute(
                            "INSERT OR IGNORE INTO watchlist (symbol, name, created_at) VALUES (?, ?, ?)",
                            (s, resolve_stock_name(s, "우량 종목"), ts)
                        )
                    logger.info("[MIGRATION] Watchlist cleaned up to 5 default symbols for AI slots.")
                conn.execute("INSERT OR REPLACE INTO watchlist_settings (key, value) VALUES ('migration_watchlist_cleaned_v3', '1')")
                conn.commit()
        except (sqlite3.Error, OSError, ValueError, TypeError) as m_err:
            logger.warning(f"Failed to run watchlist migration clean up: {m_err}")
        try:
            sync_custom_rules_to_db(conn)
        except (sqlite3.Error, OSError, ValueError, TypeError) as sc_err:
            logger.warning(f"Failed to sync custom rules to DB on init: {sc_err}")
        try:
            from src.db.ai_stock_repository import init_ai_stock_tables
            init_ai_stock_tables(conn)
            conn.commit()
        except (sqlite3.Error, OSError, ValueError, TypeError, ImportError) as ai_err:
            conn.rollback()
            raise RuntimeError(
                f"Failed to initialize required autonomous-strategy tables: {ai_err}"
            ) from ai_err
        try:
            from src.db.analysis_repository import init_analysis_cycle_tables

            init_analysis_cycle_tables(conn)
            conn.commit()
        except (sqlite3.Error, OSError, ValueError, TypeError, ImportError) as cycle_err:
            conn.rollback()
            raise RuntimeError(
                f"Failed to initialize analysis-cycle tables: {cycle_err}"
            ) from cycle_err


def _ensure_column(conn: DBWrapper, table: str, column: str, column_type: str) -> None:
    if conn.is_pg:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {column_type}")
        return
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {row[1] for row in rows}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")



# Compatibility facade. New code should import the bounded repository module it owns.
from src.db.trade_repository import *  # noqa: F401,F403,E402
from src.db.usage_repository import *  # noqa: F401,F403,E402
from src.db.strategy_repository import *  # noqa: F401,F403,E402
from src.db.scheduler_repository import *  # noqa: F401,F403,E402
from src.db.market_repository import *  # noqa: F401,F403,E402
from src.db.analysis_repository import *  # noqa: F401,F403,E402
