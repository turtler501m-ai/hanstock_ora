import unittest

from src.dashboard.routes.stock import _approval_classification


class ApprovalClassificationTests(unittest.TestCase):
    def test_strategy_order_uses_strategy_name_and_id(self):
        result = _approval_classification(
            strategy_id="seven_split",
            strategy_name="기본 분할매매",
            source="auto_trader",
        )

        self.assertEqual(result["order_classification"], "strategy")
        self.assertEqual(result["order_classification_label"], "기본 분할매매")
        self.assertEqual(
            result["order_classification_detail"],
            "전략 주문 · seven_split",
        )

    def test_missing_source_is_explicitly_manual(self):
        result = _approval_classification(
            strategy_id=None,
            strategy_name=None,
            source=None,
        )

        self.assertEqual(result["order_classification"], "manual")
        self.assertEqual(result["order_classification_label"], "수동 주문")
        self.assertEqual(
            result["order_classification_detail"],
            "출처 미기록 · 수동 처리",
        )

    def test_known_non_strategy_sources_have_clear_labels(self):
        cases = {
            "dashboard_sell_all": ("manual", "수동 전량매도"),
            "portfolio-optimizer": ("tool", "포트폴리오 최적화"),
            "scheduler-test": ("test", "테스트 주문"),
            "auto_trader": ("automation", "자동매매 · 전략 미기록"),
            "trader": ("automation", "자동매매 · 전략 미기록"),
        }

        for source, expected in cases.items():
            with self.subTest(source=source):
                result = _approval_classification(
                    strategy_id="",
                    strategy_name="",
                    source=source,
                )
                self.assertEqual(
                    (
                        result["order_classification"],
                        result["order_classification_label"],
                    ),
                    expected,
                )
                self.assertEqual(
                    result["order_classification_detail"],
                    f"출처: {source}",
                )

    def test_unknown_external_source_is_other_order(self):
        result = _approval_classification(
            strategy_id="",
            strategy_name="",
            source="external-batch",
        )

        self.assertEqual(result["order_classification"], "other")
        self.assertEqual(result["order_classification_label"], "기타 주문")
        self.assertEqual(
            result["order_classification_detail"],
            "출처: external-batch",
        )


if __name__ == "__main__":
    unittest.main()
