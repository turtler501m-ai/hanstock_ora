# -*- coding: utf-8 -*-
"""AI스톡 DB 흐름 테스트: 스캔·발굴·관찰·실행 (§17). 외부 데이터는 주입(mock)."""
import unittest
from datetime import timedelta
from unittest.mock import patch

from src.db.repository import init_db, connect_db
from src.db import ai_stock_repository as repo
from src.ai_stock import (
    market_data,
    discovery_service,
    watchlist_service,
    execution_plan_service,
    performance_service,
    portfolio_service,
)
from src.ai_stock.constants import (
    DATA_GOOD, DECISION_INSUFFICIENT, WATCH_DISCOVERED, WATCH_WATCHING, WATCH_CONFIRMED,
)
from src.ai_stock.freshness import now

_AI_TABLES = [
    "ai_stock_timing_signals", "ai_stock_execution_runs", "ai_stock_execution_plans",
    "ai_stock_performance", "ai_stock_watch_events", "ai_stock_watchlist",
    "ai_stock_candidates", "ai_stock_automation_policies", "ai_stock_scans",
]


def _uptrend(n=40, start=100.0, step=1.0):
    return [start + i * step for i in range(n)]


class FakeProvider:
    """결정적 데이터 주입 제공자."""
    def __init__(self, items, series, index):
        self._items = items
        self._series = series
        self._index = index

    def universe_items(self, market):
        return list(self._items.get(market, []))

    def quote(self, market, symbol):
        s = self._series.get(symbol)
        return {"price": s[-1]} if s else None

    def daily_series(self, market, symbol):
        return self._series.get(symbol)

    def index_series(self, market):
        return self._index.get(market, {})


class FlowTestBase(unittest.TestCase):
    def setUp(self):
        init_db()
        with connect_db() as conn:
            for t in _AI_TABLES:
                conn.execute(f"DELETE FROM {t}")
            conn.commit()
        self._orig_provider = market_data.get_provider()

    def tearDown(self):
        market_data.set_provider(self._orig_provider)


class ScanTests(FlowTestBase):
    def test_duplicate_active_scan_blocked(self):
        sid = repo.create_scan(market="KR", strategy_id="s1")
        with self.assertRaises(repo.ScanConflict):
            repo.create_scan(market="KR", strategy_id="s1")
        repo.finish_scan(sid, status="completed")
        # 완료 후에는 새 스캔 허용
        repo.create_scan(market="KR", strategy_id="s1")

    def test_stale_active_scan_cleaned_inside_create(self):
        old = (now() - timedelta(minutes=120)).isoformat()
        with connect_db() as conn:
            conn.execute(
                "INSERT INTO ai_stock_scans (market, strategy_id, status, started_at) VALUES (?, ?, ?, ?)",
                ("KR", "stale_s1", "running", old),
            )
            conn.commit()
        sid = repo.create_scan(market="KR", strategy_id="stale_s1")
        self.assertGreater(sid, 0)
        scans = repo.list_scans(market="KR", limit=10)
        statuses = [s["status"] for s in scans if s["strategy_id"] == "stale_s1"]
        self.assertIn("failed", statuses)
        self.assertIn("running", statuses)

    def test_all_market_rejected_in_storage(self):
        with self.assertRaises(Exception):
            repo.create_scan(market="ALL", strategy_id="s1")


class DiscoveryTests(FlowTestBase):
    def test_run_scan_creates_scored_candidates(self):
        market_data.set_provider(FakeProvider(
            items={"KR": [{"symbol": "005930", "name": "삼성", "instrument_type": "stock"}]},
            series={"005930": _uptrend()},
            index={"KR": {"KOSPI": _uptrend()}},
        ))
        result = discovery_service.run_scan(market="KR", strategy_id="ai_stock_default_v1")
        self.assertGreaterEqual(result["summary"]["candidate_count"], 1)
        cands = repo.list_candidates(market="KR")
        self.assertTrue(cands)
        c = cands[0]
        self.assertEqual(c["currency"], "KRW")
        for k in ("rule_score", "technical_score", "final_score"):
            self.assertGreaterEqual(c[k], 0.0)
            self.assertLessEqual(c[k], 100.0)

    def test_no_data_yields_insufficient(self):
        market_data.set_provider(FakeProvider(
            items={"KR": [{"symbol": "999999", "name": "noinfo"}]},
            series={},  # 가격 시계열 없음
            index={"KR": {"KOSPI": _uptrend()}},
        ))
        with patch(
            "src.db.repository.load_watchlist_data",
            return_value={"symbols": ["999999"]},
        ):
            discovery_service.run_scan(market="KR", strategy_id="ai_stock_default_v1")
        c = repo.list_candidates(market="KR")[0]
        self.assertEqual(c["decision"], DECISION_INSUFFICIENT)

    def test_us_currency_separated(self):
        market_data.set_provider(FakeProvider(
            items={"US": [{"symbol": "AAPL", "name": "Apple", "instrument_type": "stock"}]},
            series={"AAPL": _uptrend()},
            index={"US": {"S&P500": _uptrend()}},
        ))
        discovery_service.run_scan(market="US", strategy_id="ai_stock_default_v1")
        c = repo.list_candidates(market="US")[0]
        self.assertEqual(c["currency"], "USD")


class WatchlistTests(FlowTestBase):
    def _make_candidate(self, **overrides):
        from src.ai_stock.freshness import now
        cand = {
            "scan_id": 1, "market": "KR", "symbol": "005930", "name": "삼성",
            "strategy_id": "ai_stock_default_v1", "current_price": 70000,
            "rule_score": 70, "technical_score": 75, "momentum_score": 70,
            "narrative_score": 60, "ai_score": 0, "risk_score": 20,
            "final_score": 80, "decision": "strong_watch", "data_quality": DATA_GOOD,
            "data_as_of": now().isoformat(),
        }
        cand.update(overrides)
        return repo.save_candidate(cand)

    def test_ai_alone_cannot_confirm_insufficient(self):
        cid = self._make_candidate(decision=DECISION_INSUFFICIENT, final_score=90)
        watchlist_service.register(repo.get_candidate(cid))
        watchlist_service.transition(cid, WATCH_WATCHING)
        with self.assertRaises(ValueError):
            watchlist_service.transition(cid, WATCH_CONFIRMED)

    def test_stale_cannot_confirm(self):
        cid = self._make_candidate(data_as_of="2000-01-01T00:00:00+09:00")
        watchlist_service.register(repo.get_candidate(cid))
        watchlist_service.transition(cid, WATCH_WATCHING)
        with self.assertRaises(ValueError):
            watchlist_service.transition(cid, WATCH_CONFIRMED)

    def test_valid_candidate_confirms(self):
        cid = self._make_candidate()
        watchlist_service.register(repo.get_candidate(cid))
        watchlist_service.transition(cid, WATCH_WATCHING)
        result = watchlist_service.transition(cid, WATCH_CONFIRMED)
        self.assertEqual(result["status"], WATCH_CONFIRMED)

    def test_invalid_transition_blocked(self):
        cid = self._make_candidate()
        watchlist_service.register(repo.get_candidate(cid))
        # discovered -> confirmed 직접 전이 금지
        with self.assertRaises(ValueError):
            watchlist_service.transition(cid, WATCH_CONFIRMED)


class ExecutionTests(FlowTestBase):
    def _confirmed(self, **overrides):
        from src.ai_stock.freshness import now
        cand = {
            "scan_id": 1, "market": "KR", "symbol": "005930", "name": "삼성",
            "strategy_id": "ai_stock_default_v1", "current_price": 70000,
            "rule_score": 70, "technical_score": 75, "momentum_score": 70,
            "narrative_score": 60, "ai_score": 0, "risk_score": 20,
            "final_score": 80, "decision": "strong_watch", "data_quality": DATA_GOOD,
            "data_as_of": now().isoformat(),
        }
        cand.update(overrides)
        cid = repo.save_candidate(cand)
        watchlist_service.register(repo.get_candidate(cid))
        watchlist_service.transition(cid, WATCH_WATCHING)
        watchlist_service.transition(cid, WATCH_CONFIRMED)
        return cid

    def test_no_stop_blocks_plan(self):
        cid = self._confirmed()
        with self.assertRaises(ValueError):
            execution_plan_service.create_plan(cid, options={"entry_price": 70000})

    def test_valid_plan_created(self):
        cid = self._confirmed()
        plan = execution_plan_service.create_plan(cid, options={"entry_price": 70000, "stop_price": 66000})
        self.assertGreater(plan["quantity"], 0)
        self.assertEqual(plan["status"], "planned")
        self.assertGreater(plan["risk_budget"], 0)

    def test_duplicate_active_plan_blocked(self):
        cid = self._confirmed()
        execution_plan_service.create_plan(cid, options={"entry_price": 70000, "stop_price": 66000})
        with self.assertRaises(ValueError) as ctx:
            execution_plan_service.create_plan(cid, options={"entry_price": 70000, "stop_price": 66000})
        self.assertIn("no_active_duplicate_plan", str(ctx.exception))

    def test_stale_candidate_blocks_plan_by_default(self):
        cid = self._confirmed()
        old = (now() - timedelta(days=1)).isoformat()
        with connect_db() as conn:
            conn.execute("UPDATE ai_stock_candidates SET data_as_of=? WHERE id=?", (old, cid))
            conn.commit()
        with self.assertRaises(ValueError) as ctx:
            execution_plan_service.create_plan(cid, options={"entry_price": 70000, "stop_price": 66000})
        self.assertIn("fresh_data", str(ctx.exception))

    def test_stale_candidate_plan_requires_explicit_policy(self):
        from src.ai_stock.automation_service import set_policy

        set_policy("ai_stock_default_v1", "KR", {"allow_stale_data_trade": 1})
        cid = self._confirmed()
        old = (now() - timedelta(days=1)).isoformat()
        with connect_db() as conn:
            conn.execute("UPDATE ai_stock_candidates SET data_as_of=? WHERE id=?", (old, cid))
            conn.commit()
        plan = execution_plan_service.create_plan(cid, options={"entry_price": 70000, "stop_price": 66000})
        self.assertEqual(plan["status"], "planned")
        self.assertTrue(any(c["check"] == "fresh_data" and c["ok"] for c in plan["safety_checks"]))

    def test_account_limits_block_oversized_plan(self):
        cid = self._confirmed()
        account = {
            "available": True,
            "cash": 10000,
            "total_eval": 100000,
            "stock_eval": 0,
            "holdings": [],
        }
        with patch.object(execution_plan_service, "_account_snapshot", return_value=account):
            with self.assertRaises(ValueError) as ctx:
                execution_plan_service.create_plan(cid, options={"entry_price": 70000, "stop_price": 66000})
        self.assertIn("cash_available", str(ctx.exception))


class PerformanceTests(FlowTestBase):
    def test_run_update_records_20d_metrics_and_benchmark(self):
        market_data.set_provider(FakeProvider(
            items={"KR": [{"symbol": "005930", "name": "Samsung", "instrument_type": "stock"}]},
            series={"005930": _uptrend(25, start=100.0, step=2.0)},
            index={"KR": {"KOSPI": _uptrend(25, start=100.0, step=1.0)}},
        ))
        cid = ExecutionTests._confirmed(self, initial_price=100.0, current_price=100.0)
        result = performance_service.run_update("KR")
        self.assertEqual(result["updated"], 1)
        row = repo.list_performance(market="KR")[0]
        self.assertEqual(row["candidate_id"], cid)
        self.assertEqual(row["evaluation_complete"], 1)
        self.assertIsNotNone(row["return_5d"])
        self.assertIsNotNone(row["return_20d"])
        self.assertIsNotNone(row["mfe"])
        self.assertIsNotNone(row["mae"])
        self.assertIsNotNone(row["benchmark_return"])


class PortfolioTests(FlowTestBase):
    def test_summary_includes_account_holdings_and_concentration(self):
        account = {
            "available": True,
            "source": "test",
            "data_as_of": "2026-06-25T09:00:00+09:00",
            "cash": 50000,
            "stock_eval": 50000,
            "total_eval": 100000,
            "pnl": 1000,
            "holdings": [{"symbol": "005930", "qty": 1, "price": 50000, "value": 50000}],
        }
        with patch.object(portfolio_service, "_account_snapshot", return_value=account):
            body = portfolio_service.summary("KR")
        kr = body["by_market"][0]
        self.assertTrue(kr["account_available"])
        self.assertEqual(kr["holding_count"], 1)
        self.assertEqual(kr["concentration"]["max_symbol"], "005930")
        self.assertEqual(kr["concentration"]["max_weight"], 0.5)


if __name__ == "__main__":
    unittest.main()
