import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class VmServiceSafetyTest(unittest.TestCase):
    def test_dashboard_systemd_listens_on_public_interface(self):
        server_script = (ROOT / "scripts/vm/server.sh").read_text(encoding="utf-8")
        systemd_unit = (ROOT / "scripts/vm/hanstock.service").read_text(
            encoding="utf-8"
        )

        self.assertIn('HOST="${HOST:-127.0.0.1}"', server_script)
        self.assertIn("--host 0.0.0.0", systemd_unit)

    def test_deploy_syncs_systemd_unit_before_restart(self):
        update_script = (ROOT / "scripts/vm/update.sh").read_text(encoding="utf-8")

        install_position = update_script.index(
            "/etc/systemd/system/hanstock.service"
        )
        reload_position = update_script.index("systemctl daemon-reload")
        restart_position = update_script.index(
            '"$ROOT_DIR/scripts/vm/server.sh" restart'
        )
        self.assertLess(install_position, reload_position)
        self.assertLess(reload_position, restart_position)

    def test_deploy_renders_units_for_actual_repository_path(self):
        update_script = (ROOT / "scripts/vm/update.sh").read_text(encoding="utf-8")

        self.assertIn("install_systemd_unit", update_script)
        self.assertIn('sed "s#/home/ubuntu/hanstock#$ROOT_DIR#g"', update_script)
        self.assertIn("hanstock-autonomy.service", update_script)

    def test_local_vm_dashboard_uses_loopback_tunnel(self):
        tunnel_script = (ROOT / "scripts/local/vm-dashboard.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            '"-L", "127.0.0.1:${LocalPort}:127.0.0.1:${RemotePort}"',
            tunnel_script,
        )


if __name__ == "__main__":
    unittest.main()
