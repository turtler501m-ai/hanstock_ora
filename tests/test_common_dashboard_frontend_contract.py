import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")
COMMON_ANALYSIS_JS = (
    ROOT / "web" / "static" / "js" / "common-analysis.js"
).read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "web" / "templates" / "index.html").read_text(encoding="utf-8")


class CommonDashboardFrontendContractTests(unittest.TestCase):
    def test_strategy_switch_generation_guards_slow_analysis_responses(self):
        self.assertIn("strategyRequestGeneration += 1", COMMON_ANALYSIS_JS)
        self.assertIn("isCurrentStrategyRequest(request)", COMMON_ANALYSIS_JS)
        self.assertIn("analysisCyclePromises = new Map()", COMMON_ANALYSIS_JS)

    def test_analysis_refresh_runs_candidates_before_signals_and_plan(self):
        start = COMMON_ANALYSIS_JS.index("async function startCommonAnalysisRefresh()")
        end = COMMON_ANALYSIS_JS.index("function invalidateCommonTabRefreshes()", start)
        body = COMMON_ANALYSIS_JS[start:end]
        self.assertLess(
            body.index("await renderCandidates({ refresh: true })"),
            body.index("Promise.all([renderSignals(), renderExecutionPlan()])"),
        )

    def test_isolated_strategy_buttons_are_not_disabled_by_common_scheduler(self):
        start = APP_JS.index("function disableTriggerButtons(disabled)")
        end = APP_JS.index("function toKorDecision", start)
        body = APP_JS[start:end]
        self.assertNotIn("btn-pb-run", body)
        self.assertNotIn("btn-ha-run", body)

    def test_orders_show_strategy_and_performance_has_explicit_scope(self):
        self.assertIn('<th>전략</th>', INDEX_HTML)
        self.assertIn('id="select-performance-scope"', INDEX_HTML)
        self.assertIn("row.strategy_name || row.strategy_id || '미분류'", APP_JS)


    def test_performance_tab_exposes_local_trade_cleanup(self):
        self.assertIn('id="table-trade-cleanup"', INDEX_HTML)
        self.assertIn("async function renderTradeCleanup()", APP_JS)
        self.assertIn("'/api/trades/local-cleanup?limit=200'", APP_JS)
        self.assertIn("`/api/trades/local/${tradeId}?confirm=true`", APP_JS)

    def test_performance_tab_exposes_market_context_strategy_validation_and_sorting(self):
        self.assertIn("코스피 변동성", INDEX_HTML)
        self.assertIn("코스닥 변동성", INDEX_HTML)
        self.assertIn('id="table-strategy-validation"', INDEX_HTML)
        self.assertIn("strategy_name", APP_JS)
        self.assertIn("sortable-header", APP_JS)
        self.assertIn("data-sort-key", APP_JS)

    def test_trade_sync_result_lists_every_processed_item(self):
        self.assertIn('id="table-trade-sync-items"', INDEX_HTML)
        self.assertIn("result.sync_items", APP_JS)
        self.assertIn("동기화 전체 항목 보기", INDEX_HTML)

    def test_scheduler_checklist_uses_persisted_schedule_registrations(self):
        scheduler_renderer = APP_JS.split(
            "async function renderSchedulerStrategyChecklist", 1
        )[1].split("function getScheduledStrategyIds", 1)[0]
        self.assertNotIn("fetchJson('/api/ai-strategies')", scheduler_renderer)
        self.assertIn("row.strategy_id", scheduler_renderer)
        self.assertIn("narrative_momentum_strategy", scheduler_renderer)

    def test_scheduler_summary_tracks_approval_success_and_failure(self):
        self.assertIn('id="sched-result-success-cnt"', INDEX_HTML)
        self.assertIn("summaryCounts.success_count", APP_JS)
        self.assertIn("성공 <strong", APP_JS)


if __name__ == "__main__":
    unittest.main()
