# -*- coding: utf-8 -*-
"""Compatibility facade for bounded AI stock repositories.

New code must import the domain repository directly.
"""
from functools import wraps
from threading import RLock

from src.db import (
    ai_execution_repository,
    ai_risk_repository,
    ai_scan_repository,
    ai_snapshot_repository,
    ai_watchlist_repository,
)
from src.db.ai_schema_repository import *  # noqa: F401,F403
from src.db.ai_scan_repository import *  # noqa: F401,F403
from src.db.ai_watchlist_repository import *  # noqa: F401,F403
from src.db.ai_execution_repository import *  # noqa: F401,F403
from src.db.ai_risk_repository import *  # noqa: F401,F403
from src.db.ai_snapshot_repository import *  # noqa: F401,F403

# Historical private connection hook used by health diagnostics/tests.
from src.db.ai_stock_support import connect_ai_stock as _connect

_compat_connect_lock = RLock()


def _compat_wrapper(module, implementation):
    """Forward the historical facade connection hook to a bounded repository."""
    @wraps(implementation)
    def call(*args, **kwargs):
        if module._connect is _connect:
            return implementation(*args, **kwargs)
        with _compat_connect_lock:
            original = module._connect
            module._connect = _connect
            try:
                return implementation(*args, **kwargs)
            finally:
                module._connect = original
    return call


for _module in (
    ai_scan_repository,
    ai_watchlist_repository,
    ai_execution_repository,
    ai_risk_repository,
    ai_snapshot_repository,
):
    for _name in _module.__all__:
        _implementation = getattr(_module, _name)
        if callable(_implementation) and not isinstance(_implementation, type):
            globals()[_name] = _compat_wrapper(_module, _implementation)

del _module, _name, _implementation
