import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StrategyLookupTabContractTests(unittest.TestCase):
    def test_strategy_tab_is_named_for_lookup(self):
        html = (ROOT / "web/templates/index.html").read_text(encoding="utf-8")
        self.assertIn('data-dashboard-tab="strategy">전략조회</button>', html)

    def test_lookup_moves_signals_and_removes_obsolete_cards(self):
        script = (ROOT / "web/static/js/app.js").read_text(encoding="utf-8")
        self.assertIn("if (aiTab && signals) aiTab.insertBefore(signals, aiTab.firstChild);", script)
        self.assertIn("strategyTab?.querySelector('.panel-candidates-history')?.remove();", script)
        self.assertIn("strategyTab?.querySelector('.panel-execution-plan')?.remove();", script)

    def test_selected_strategies_run_analysis_only_before_candidate_render(self):
        script = (ROOT / "web/static/js/app.js").read_text(encoding="utf-8")
        self.assertIn("async function previewSelectedStrategies()", script)
        self.assertIn("mode: 'analysis_only'", script)
        self.assertIn("auto_approve: false", script)
        self.assertIn("allowed_categories: ['candidate']", script)
        self.assertIn("await renderCachedStrategyPreviews(strategyIds, selected);", script)
        self.assertIn("await renderCandidates({ strategyIds, strategies: selected });", script)

    def test_cached_results_are_shown_while_selected_strategies_update(self):
        script = (ROOT / "web/static/js/app.js").read_text(encoding="utf-8")
        self.assertIn(
            "async function renderCachedStrategyPreviews(strategyIds, strategies = [], options = {})",
            script,
        )
        self.assertIn("cache_only: 'true'", script)
        self.assertIn("업데이트 중", script)
        self.assertIn("이전 결과", script)

    def test_refresh_is_always_available_and_runs_in_background(self):
        html = (ROOT / "web/templates/index.html").read_text(encoding="utf-8")
        script = (ROOT / "web/static/js/app.js").read_text(encoding="utf-8")

        self.assertIn('id="btn-refresh-strategy-lookup" class="button-ghost">새로고침</button>', html)
        self.assertIn("async function refreshStrategyLookup()", script)
        self.assertIn("await renderCachedStrategyPreviews(strategyIds, selected);", script)
        self.assertIn("await waitForStrategyPreviewCompletion(started.run_id);", script)
        self.assertIn("requested_run_matches", script)
        self.assertIn("finishStrategyPreviewUpdatingState();", script)
        self.assertIn("await renderCandidates({ strategyIds, strategies: selected });", script)
        self.assertNotIn(
            "await renderCandidates({ strategyIds, strategies: selected, refresh: true });",
            script,
        )
        self.assertIn("refresh: String(Boolean(options.refresh))", script)
        self.assertNotIn("refreshButton.hidden = false", script)
        self.assertIn("btnRefreshStrategyLookup.addEventListener('click', refreshStrategyLookup)", script)
        self.assertIn("DB 최신본 저장", script)
        self.assertIn("최대 10분까지 기다립니다", script)

    def test_lookup_completion_does_not_open_no_candidates_popup(self):
        script = (ROOT / "web/static/js/app.js").read_text(encoding="utf-8")

        self.assertIn(
            "if (!previewStrategyIds.length) setNoCandidatesModalOpen(true);",
            script,
        )

    def test_each_selected_strategy_has_its_own_result_card(self):
        script = (ROOT / "web/static/js/app.js").read_text(encoding="utf-8")
        stylesheet = (ROOT / "web/static/css/style.css").read_text(encoding="utf-8")
        self.assertIn("function renderStrategyPreviewCards(results, strategies = [])", script)
        self.assertIn("results.id = 'strategy-preview-results';", script)
        self.assertIn('class="strategy-preview-card"', script)
        self.assertIn(".strategy-preview-card", stylesheet)

    def test_approved_independent_strategy_can_be_selected(self):
        script = (ROOT / "web/static/js/app.js").read_text(encoding="utf-8")
        function_body = script.split(
            "function isSharedScheduleSelectable(strategy) {", 1
        )[1].split("}", 1)[0]
        self.assertIn("strategy.status", function_body)
        self.assertNotIn("independent_schedule", function_body)


if __name__ == "__main__":
    unittest.main()
