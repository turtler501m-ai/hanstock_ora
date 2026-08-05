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
        self.assertIn("row.order_classification_label", APP_JS)
        self.assertIn("approval-classification-badge", APP_JS)
        self.assertIn("'수동 주문'", APP_JS)


    def test_performance_tab_exposes_local_trade_cleanup(self):
        self.assertIn('id="table-trade-cleanup"', INDEX_HTML)
        self.assertIn("async function renderTradeCleanup()", APP_JS)
        self.assertIn("'/api/trades/local-cleanup?limit=200'", APP_JS)
        self.assertIn("`/api/trades/local/${tradeId}?confirm=true`", APP_JS)

    def test_performance_tab_exposes_market_context_strategy_validation_and_sorting(self):
        self.assertIn("보유주식 당일 등락", INDEX_HTML)
        self.assertIn("KOSPI 대비", APP_JS)
        self.assertIn("KOSDAQ 대비", APP_JS)
        self.assertIn("holding_change_symbol_count", APP_JS)
        self.assertIn("성과 등락", INDEX_HTML)
        self.assertIn("코스피 (등락)", INDEX_HTML)
        self.assertIn("코스닥 (등락)", INDEX_HTML)
        self.assertNotIn("코스피 변동성", INDEX_HTML)
        self.assertNotIn("코스닥 변동성", INDEX_HTML)
        self.assertIn("코스피 등락 (%)", APP_JS)
        self.assertIn("코스닥 등락 (%)", APP_JS)
        self.assertIn('id="table-strategy-validation"', INDEX_HTML)
        self.assertIn("strategy_name", APP_JS)
        self.assertIn("sortable-header", APP_JS)
        self.assertIn("data-sort-key", APP_JS)

    def test_performance_tab_exposes_forward_returns_and_manual_review(self):
        self.assertIn("전략별 모의성과 및 수동 검증", INDEX_HTML)
        self.assertIn("/api/performance/forward", APP_JS)
        self.assertIn("function renderStrategyForwardPerformance(items)", APP_JS)
        self.assertIn("item.excess_vs_kospi_pct", APP_JS)
        self.assertIn("strategy-review-decision", APP_JS)
        self.assertIn("자동매매 상태는 변경되지 않았습니다", APP_JS)
        self.assertIn("Forward performance render failed", APP_JS)
        self.assertIn("qualitySummary", APP_JS)
        self.assertIn('title="${escapeHtml(qualityDetail)}"', APP_JS)
        self.assertIn("거래비용과 계좌 입출금이 확인되지 않은 결과는 추정치", INDEX_HTML)

    def test_trade_sync_result_lists_every_processed_item(self):
        self.assertIn('id="table-trade-sync-items"', INDEX_HTML)
        self.assertIn('id="table-trade-sync-runs"', INDEX_HTML)
        self.assertIn("run.sync_items", APP_JS)
        self.assertIn("result.runs", APP_JS)
        self.assertIn("trade-sync-run-button", APP_JS)
        self.assertIn("run.status === 'running'", APP_JS)
        self.assertIn("/api/trades/sync/runs/", APP_JS)
        self.assertIn("동기화 전체 항목 보기", INDEX_HTML)
        self.assertIn("details.hidden = false", APP_JS)

    def test_trade_sync_continues_and_refreshes_across_tab_changes(self):
        self.assertIn("startTradeSyncPolling()", APP_JS)
        self.assertIn("tradeSyncPollInterval = setInterval(poll, 3000)", APP_JS)
        self.assertIn("result.status === 'running'", APP_JS)
        self.assertIn("백그라운드에서 시작했습니다", APP_JS)

    def test_trade_sync_history_status_is_scoped_inside_run_renderer(self):
        start = APP_JS.index("function renderTradeSyncResult")
        end = APP_JS.index("async function loadTradeSyncResult", start)
        body = APP_JS[start:end]
        runs_map = body.index("runs.map")
        self.assertGreater(body.index("const runStatus", runs_map), runs_map)

    def test_holdings_tab_has_refresh_control(self):
        self.assertIn(
            'data-dashboard-tab="portfolio">보유종목</button>',
            INDEX_HTML,
        )
        self.assertIn('id="btn-refresh-holdings"', INDEX_HTML)
        self.assertIn("await renderBalance()", APP_JS)

    def test_holdings_tab_exposes_strategy_value_loss_summary_and_filters(self):
        self.assertIn('id="table-holding-strategies"', INDEX_HTML)
        self.assertIn('id="holding-attribution-coverage"', INDEX_HTML)
        self.assertIn('id="holding-loss-list"', INDEX_HTML)
        self.assertIn('id="select-holding-strategy-filter"', INDEX_HTML)
        self.assertIn('id="select-holding-pnl-filter"', INDEX_HTML)
        self.assertIn("<th>손익 상태</th>", INDEX_HTML)
        self.assertIn("function renderHoldingStrategySummary(balance)", APP_JS)
        self.assertIn("balance.strategy_summary || []", APP_JS)
        self.assertIn("holdingStrategyFilter === 'all'", APP_JS)
        self.assertIn("holdingPnlFilter === 'all'", APP_JS)

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

    def test_overview_strategy_settings_are_grouped_and_readiness_is_collapsible(self):
        self.assertIn("function strategySettingGroups(config)", APP_JS)
        self.assertIn("title: '기본 매매'", APP_JS)
        self.assertIn("title: '손절·수익 보호'", APP_JS)
        self.assertIn("title: '후보 선별'", APP_JS)
        self.assertIn("title: '자금·리스크'", APP_JS)
        self.assertIn('class="strategy-settings-shell"', APP_JS)
        self.assertIn('class="strategy-readiness-details"', APP_JS)
        self.assertIn("진입 이후 최고가 기준", APP_JS)
        self.assertIn("전략 설정 저장", APP_JS)

    def test_watchlist_exposes_summary_policy_and_filters(self):
        self.assertIn('id="watchlist-total-count"', INDEX_HTML)
        self.assertIn('id="watchlist-sector-summary"', INDEX_HTML)
        self.assertIn('id="form-watchlist-policy"', INDEX_HTML)
        self.assertIn('id="num-watchlist-min-price"', INDEX_HTML)
        self.assertIn('value="5000"', INDEX_HTML)
        self.assertIn('id="select-watchlist-policy-filter"', INDEX_HTML)
        self.assertIn("function renderWatchlistSummary(data)", APP_JS)
        self.assertIn("'/api/watchlist/policy'", APP_JS)
        self.assertIn("row.policy_status === policyFilter", APP_JS)

    def test_schedule_tab_groups_overview_settings_execution_and_results(self):
        self.assertIn('class="schedule-overview-grid"', INDEX_HTML)
        self.assertIn('id="sched-overview-run-state"', INDEX_HTML)
        self.assertIn('id="sched-overview-schedule-state"', INDEX_HTML)
        self.assertIn('class="schedule-runtime-details"', INDEX_HTML)
        self.assertIn('class="schedule-mode-card is-safe"', INDEX_HTML)
        self.assertIn('class="schedule-mode-card is-auto"', INDEX_HTML)
        self.assertIn('class="schedule-mode-card is-execute"', INDEX_HTML)
        self.assertIn('class="schedule-result-metrics"', INDEX_HTML)
        self.assertEqual(INDEX_HTML.count('id="btn-run-analysis-only"'), 1)
        self.assertEqual(INDEX_HTML.count('id="btn-run-daily-auto"'), 1)
        self.assertEqual(INDEX_HTML.count('id="btn-run-execute"'), 1)
        self.assertIn("runStateEl.textContent", APP_JS)
        self.assertIn("scheduleStateEl.textContent", APP_JS)
        self.assertIn('class="scheduler-strategy-option"', APP_JS)


if __name__ == "__main__":
    unittest.main()
