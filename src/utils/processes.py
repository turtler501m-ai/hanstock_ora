"""Cross-platform process inspection helpers."""
from __future__ import annotations

import ctypes
import os
from typing import Any


def process_exists(pid: Any) -> bool:
    """Return whether *pid* exists without sending it a signal on Windows."""
    try:
        process_id = int(pid)
    except (TypeError, ValueError):
        return False
    if process_id <= 0:
        return False
    if process_id == os.getpid():
        return True

    if os.name == "nt":
        process_query_limited_information = 0x1000
        error_access_denied = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        open_process.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_int

        handle = open_process(
            process_query_limited_information,
            False,
            process_id,
        )
        if handle:
            close_handle(handle)
            return True
        return ctypes.get_last_error() == error_access_denied

    try:
        os.kill(process_id, 0)
    except (OSError, PermissionError):
        return False
    return True
