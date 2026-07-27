import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import autonomy_service


class AutonomyServiceEntrypointTest(unittest.TestCase):
    def test_process_lease_rejects_live_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "service.lock"
            first = autonomy_service.ProcessLease(path)
            second = autonomy_service.ProcessLease(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_enabled_without_factory_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {"AUTONOMY_RUNTIME_DIR": folder}
            with patch.object(autonomy_service.config, "autonomy_enabled", True):
                result = autonomy_service.run(environ=env)
            self.assertEqual(result, 2)
            heartbeat = json.loads(
                (Path(folder) / "heartbeat.json").read_text(encoding="utf-8")
            )
            self.assertEqual(heartbeat["state"], "configuration_error")
            self.assertFalse((Path(folder) / "service.lock").exists())

    def test_stale_pid_lease_is_recovered(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "service.lock"
            path.write_text("99999999", encoding="utf-8")
            lease = autonomy_service.ProcessLease(path)
            self.assertTrue(lease.acquire())
            self.assertEqual(path.read_text(encoding="utf-8"), str(os.getpid()))
            lease.release()


if __name__ == "__main__":
    unittest.main()
