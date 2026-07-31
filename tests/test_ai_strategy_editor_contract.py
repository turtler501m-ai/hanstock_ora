import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AiStrategyEditorContractTests(unittest.TestCase):
    def test_strategy_tab_contains_click_editor_and_only_three_easy_presets(self):
        html = (ROOT / "web/templates/index.html").read_text(encoding="utf-8")

        self.assertIn('id="ai-strategy-detail-panel"', html)
        self.assertIn('id="form-edit-ai-strategy"', html)
        self.assertIn('name="profile_json"', html)
        self.assertEqual(html.count('class="button-ghost easy-strategy-preset"'), 3)

    def test_editor_supports_full_profile_and_patch_update(self):
        script = (ROOT / "web/static/js/app.js").read_text(encoding="utf-8")

        self.assertIn("function fillStrategyDetail(strategy)", script)
        self.assertIn("async function patchStrategyJson(id, payload)", script)
        self.assertIn("profile.risk[key]", script)
        self.assertIn("method: 'PATCH'", script)


if __name__ == "__main__":
    unittest.main()
