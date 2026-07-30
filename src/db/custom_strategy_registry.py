from __future__ import annotations

import functools
import importlib.util
import inspect
import json
import sqlite3
import sys
from pathlib import Path

from src.utils.logger import logger


CUSTOM_STRATEGY_PREFIXES = ("사용자전략", "🔌", "🧠", "⚙", "📈", "📊", "🛡")
CUSTOM_RULES_DIR = Path(__file__).resolve().parents[1] / "strategy" / "custom_rules"


def _load_strategy_module(py_file: Path):
    module_name = f"src.strategy.custom_rules.{py_file.stem}"
    spec = importlib.util.spec_from_file_location(module_name, py_file)
    if not spec or not spec.loader:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _strategy_classes(module):
    if module is None:
        return []
    return [
        obj
        for name, obj in inspect.getmembers(module, inspect.isclass)
        if obj.__module__ == module.__name__ and "Strategy" in name
    ]


def sync_custom_rules_to_db(conn) -> dict[str, int]:
    """Synchronize code-defined strategies and return a compact change summary."""
    from src.db.strategy_repository import (
        _default_strategy_profile,
        strategy_profile_hash,
    )

    summary = {"discovered": 0, "inserted": 0, "updated": 0, "failed": 0}
    if not CUSTOM_RULES_DIR.exists():
        return summary

    project_root = str(CUSTOM_RULES_DIR.parents[2])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    for py_file in sorted(CUSTOM_RULES_DIR.glob("*.py")):
        if py_file.name == "__init__.py":
            continue
        try:
            module = _load_strategy_module(py_file)
            for strategy_class in _strategy_classes(module):
                summary["discovered"] += 1
                strategy_id = py_file.stem
                doc_lines = [
                    line.strip()
                    for line in (strategy_class.__doc__ or "").splitlines()
                    if line.strip()
                ]
                strategy_name = doc_lines[0] if doc_lines else strategy_class.__name__
                if not strategy_name.startswith(CUSTOM_STRATEGY_PREFIXES):
                    strategy_name = f"사용자전략 {strategy_name}"
                description = " ".join(doc_lines[1:])
                if not description:
                    description = f"{py_file.name}에서 불러온 사용자 정의 전략입니다."

                profile = _default_strategy_profile(
                    {
                        "id": strategy_id,
                        "provider": "none",
                        "model": strategy_id,
                        "weight": 0.0,
                    }
                )
                profile_json = json.dumps(profile, ensure_ascii=False, sort_keys=True)
                profile_hash = strategy_profile_hash(profile)
                exists = conn.execute(
                    "SELECT 1 FROM ai_strategies WHERE id = ?", (strategy_id,)
                ).fetchone()
                if exists:
                    conn.execute(
                        """
                        UPDATE ai_strategies
                        SET name = ?, provider = 'none', model = ?, description = ?,
                            profile_json = ?, profile_hash = ?
                        WHERE id = ?
                        """,
                        (
                            strategy_name,
                            strategy_id,
                            description,
                            profile_json,
                            profile_hash,
                            strategy_id,
                        ),
                    )
                    summary["updated"] += 1
                else:
                    conn.execute(
                        """
                        INSERT INTO ai_strategies (
                            id, name, provider, model, weight, description, selected,
                            status, profile_json, strategy_version, profile_hash
                        )
                        VALUES (?, ?, 'none', ?, 0.0, ?, 0, 'verified', ?, 1, ?)
                        """,
                        (
                            strategy_id,
                            strategy_name,
                            strategy_id,
                            description,
                            profile_json,
                            profile_hash,
                        ),
                    )
                    summary["inserted"] += 1
        except (sqlite3.Error, OSError, ValueError, TypeError, ImportError) as exc:
            summary["failed"] += 1
            logger.warning(f"Failed to load custom strategy {py_file.name}: {exc}")

    if summary["failed"]:
        logger.warning(f"Custom strategy synchronization completed: {summary}")
    elif summary["inserted"]:
        logger.debug(f"Custom strategy synchronization completed: {summary}")
    return summary


@functools.lru_cache(maxsize=128)
def get_custom_strategy_instance(strategy_id: str):
    py_file = CUSTOM_RULES_DIR / f"{strategy_id}.py"
    if not py_file.exists():
        return None
    try:
        classes = _strategy_classes(_load_strategy_module(py_file))
        return classes[0]() if classes else None
    except (OSError, ValueError, TypeError, ImportError) as exc:
        logger.warning(f"Failed to load custom strategy {strategy_id}: {exc}")
        return None
