"""Auditable implementation targets for the deterministic technical strategy."""

from __future__ import annotations

import importlib
import json
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
TARGETS_PATH = BASE_DIR / "config" / "technical_strategy_targets.json"


def _resolve_object(module, dotted_name: str):
    value = module
    for part in dotted_name.split("."):
        value = getattr(value, part)
    return value


def build_technical_strategy_readiness() -> dict:
    definition = json.loads(TARGETS_PATH.read_text(encoding="utf-8"))
    rows = []
    for item in definition.get("items", []):
        missing = []
        module_name = str(item.get("module") or "")
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            module = None
            missing.append(f"{module_name}: {type(exc).__name__}")
        if module is not None:
            for object_name in item.get("objects", []):
                try:
                    _resolve_object(module, str(object_name))
                except AttributeError:
                    missing.append(f"{module_name}.{object_name}")
        for relative_path in item.get("paths", []):
            if not (BASE_DIR / relative_path).is_file():
                missing.append(str(relative_path))
        target_pct = int(item.get("target_pct") or 100)
        current_pct = target_pct if not missing else 0
        rows.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "target_pct": target_pct,
            "current_pct": current_pct,
            "complete": current_pct >= target_pct,
            "missing": missing,
        })

    overall = round(sum(row["current_pct"] for row in rows) / len(rows), 1) if rows else 0.0
    try:
        from src.strategy.condition_monitor import condition_monitor_status

        monitor = condition_monitor_status()
    except Exception as exc:
        monitor = {"running_data_available": False, "error": f"{type(exc).__name__}: {exc}"}
    target_pct = int(definition.get("target_pct") or 100)
    return {
        "name": definition.get("name"),
        "target_pct": target_pct,
        "current_pct": overall,
        "complete": bool(rows) and overall >= target_pct and all(row["complete"] for row in rows),
        "items": rows,
        "condition_monitor": monitor,
    }
