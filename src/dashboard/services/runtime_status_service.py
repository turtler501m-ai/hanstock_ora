from __future__ import annotations

import os
import socket


def dashboard_runtime_info() -> dict:
    """Describe which host serves the dashboard without domain dependencies."""
    hostname = socket.gethostname()
    explicit_label = os.environ.get("HANSTOCK_DASHBOARD_LABEL", "").strip()
    explicit_origin = os.environ.get("HANSTOCK_DASHBOARD_ORIGIN", "").strip().lower()
    is_vm = explicit_origin == "vm" or hostname.startswith("hanstock-server")
    label = explicit_label or ("VM DASHBOARD" if is_vm else "LOCAL DASHBOARD")
    return {
        "label": label,
        "origin": "vm" if is_vm else "local",
        "is_vm": is_vm,
        "hostname": hostname,
    }
