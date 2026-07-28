from unittest import TestCase
from unittest.mock import patch

from src.ai_stock.decision_pipeline_service import build_pipeline


class DecisionPipelineTests(TestCase):
    def test_pipeline_exposes_ai_thesis_evidence_risk_and_workflow(self):
        scan = {"id": 9, "strategy_id": "ai_news_v1", "market": "KR"}
        candidate = {
            "candidate_id": 11,
            "scan_id": 9,
            "market": "KR",
            "strategy_id": "ai_news_v1",
            "symbol": "005930",
            "name": "삼성전자",
            "decision": "watch",
            "ai_score": 82.0,
            "confidence": 0.91,
            "fallback_used": False,
            "related_narratives": ["HBM 공급 확대"],
            "positive_factors": ["수요 증가"],
            "negative_factors": ["가격 변동성"],
            "warnings": [],
            "invalidation_conditions": ["공급 계약 취소"],
            "narrative_score": 78.0,
            "risk_score": 10.0,
            "final_score": 84.0,
            "rule_score": 70.0,
            "data_quality": "good",
            "data_as_of": "2099-01-01T00:00:00+09:00",
            "market_regime": "bullish",
        }
        policy = {
            "strategy_id": "ai_news_v1",
            "automation_level": 5,
            "enabled": 1,
            "min_final_score": 65.0,
            "min_rule_score": 40.0,
            "max_risk_score": 60.0,
        }
        with (
            patch("src.ai_stock.decision_pipeline_service.repo.list_scans", return_value=[scan]),
            patch("src.ai_stock.decision_pipeline_service.repo.list_candidates", return_value=[candidate]),
            patch("src.ai_stock.decision_pipeline_service.repo.list_policies", return_value=[policy]),
            patch(
                "src.ai_stock.decision_pipeline_service.repo.list_watchlist",
                return_value=[{"candidate_id": 11, "status": "confirmed"}],
            ),
            patch(
                "src.ai_stock.decision_pipeline_service.repo.list_execution_plans",
                return_value=[{"id": 3, "candidate_id": 11, "status": "planned"}],
            ),
            patch("src.ai_stock.decision_pipeline_service.repo.list_execution_runs", return_value=[]),
            patch("src.ai_stock.decision_pipeline_service.repo.list_managed_orders", return_value=[]),
            patch("src.ai_stock.decision_pipeline_service.repo.list_strategy_decisions", return_value=[]),
            patch(
                "src.ai_stock.decision_pipeline_service._heartbeat",
                return_value={"state": "running", "detail": ""},
            ),
            patch("src.ai_stock.decision_pipeline_service.config.ai_strategy_enabled", True),
        ):
            result = build_pipeline(market="KR", strategy_id="ai_news_v1")

        row = result["candidates"][0]
        self.assertEqual(row["thesis"]["source"], "AI 판단")
        self.assertEqual(row["evidence"]["narratives"], ["HBM 공급 확대"])
        self.assertTrue(row["risk_gate"]["proceed"])
        self.assertEqual(row["workflow"]["plan_id"], 3)
        self.assertEqual(result["summary"]["ai_ready_count"], 1)

    def test_pipeline_labels_rule_fallback_explicitly(self):
        scan = {"id": 1, "strategy_id": "rules", "market": "KR"}
        candidate = {
            "candidate_id": 1,
            "market": "KR",
            "strategy_id": "rules",
            "decision": "neutral",
            "ai_score": 0,
            "confidence": None,
            "fallback_used": False,
            "related_narratives": [],
            "final_score": 50,
            "rule_score": 60,
            "risk_score": 10,
            "data_as_of": "2099-01-01T00:00:00+09:00",
        }
        with (
            patch("src.ai_stock.decision_pipeline_service.repo.list_scans", return_value=[scan]),
            patch("src.ai_stock.decision_pipeline_service.repo.list_candidates", return_value=[candidate]),
            patch("src.ai_stock.decision_pipeline_service.repo.list_policies", return_value=[]),
            patch("src.ai_stock.decision_pipeline_service.repo.list_watchlist", return_value=[]),
            patch("src.ai_stock.decision_pipeline_service.repo.list_execution_plans", return_value=[]),
            patch("src.ai_stock.decision_pipeline_service.repo.list_execution_runs", return_value=[]),
            patch("src.ai_stock.decision_pipeline_service.repo.list_managed_orders", return_value=[]),
            patch("src.ai_stock.decision_pipeline_service.repo.list_strategy_decisions", return_value=[]),
            patch(
                "src.ai_stock.decision_pipeline_service._heartbeat",
                return_value={"state": "disabled", "detail": "off"},
            ),
            patch("src.ai_stock.decision_pipeline_service.config.ai_strategy_enabled", False),
        ):
            result = build_pipeline(market="KR")

        self.assertEqual(result["candidates"][0]["thesis"]["source"], "수치 규칙 대체")
        self.assertIn("AI_STRATEGY_ENABLED 비활성", result["blockers"])
