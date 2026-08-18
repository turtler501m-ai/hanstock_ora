import unittest

from src.notifier.slack import _decorate_payload


class SlackInstanceLabelTest(unittest.TestCase):
    def test_adds_instance_label_and_limits_message_lines(self):
        payload = _decorate_payload(
            {
                "text": "완료\n두 번째\n세 번째\n제외할 네 번째",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "요약\n건수 1\n오류 0\n제외할 상세",
                        },
                    }
                ],
            },
            tag="ORA",
        )

        self.assertEqual(payload["text"], "[ORA] 완료\n두 번째\n세 번째")
        self.assertEqual(
            payload["blocks"][0]["text"]["text"],
            "[ORA] 요약\n건수 1\n오류 0",
        )


if __name__ == "__main__":
    unittest.main()
