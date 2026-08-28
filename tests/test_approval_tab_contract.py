import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")


class ApprovalTabContractTests(unittest.TestCase):
    def test_order_submission_and_fill_have_distinct_labels(self):
        self.assertIn("if (order === 'submitted') return '주문접수';", APP_JS)
        self.assertIn("if (order === 'filled' || order === 'reconciled') return '체결완료';", APP_JS)
        self.assertIn("approvalDisplayStatus(status, orderStatus)", APP_JS)
        self.assertIn("approvalDisplayStatus(result.status, result.order_status)", APP_JS)

    def test_approval_table_messages_span_all_columns(self):
        self.assertNotIn("setTableMessage('#table-approvals tbody', 9", APP_JS)
        self.assertIn("setTableMessage('#table-approvals tbody', 10", APP_JS)


if __name__ == "__main__":
    unittest.main()
