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
        self.assertIn("await renderCandidates({ strategyIds });", script)


if __name__ == "__main__":
    unittest.main()
