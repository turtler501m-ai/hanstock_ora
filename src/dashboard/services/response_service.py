from __future__ import annotations

import math
from typing import Any

from fastapi.responses import JSONResponse


def json_safe_value(value: Any) -> Any:
    """Replace non-finite floats before Starlette serializes API payloads."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe_value(item) for item in value]
    return value


class SafeJSONResponse(JSONResponse):
    """JSON response that converts NaN and infinities to JSON null."""

    def render(self, content: Any) -> bytes:
        return super().render(json_safe_value(content))
