import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.utils import openai_guard


class OpenAIRateLimitGuardTests(unittest.TestCase):
    def test_rate_limit_is_shared_through_runtime_file(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            openai_guard, "_PATH", Path(temp_dir) / "cooldown"
        ), patch.object(openai_guard.time, "time", return_value=1000.0):
            self.assertEqual(openai_guard.remaining_cooldown(), 0.0)
            self.assertEqual(openai_guard.record_rate_limit(30), 30.0)
            self.assertEqual(openai_guard.remaining_cooldown(), 30.0)
            with self.assertRaisesRegex(RuntimeError, "cooldown active"):
                openai_guard.require_available()


if __name__ == "__main__":
    unittest.main()
