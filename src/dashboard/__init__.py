# -*- coding: utf-8 -*-
from contextlib import asynccontextmanager

from fastapi import HTTPException, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.dashboard.core import app
from src.ai_stock import constants as _ai_stock_constants
from src.ai_stock.schemas import envelope as _ai_stock_envelope

# Import all route modules so decorators register on app
import src.dashboard.routes.pages as pages
import src.dashboard.routes.account as account
import src.dashboard.routes.futures as futures
import src.dashboard.routes.quantconnect as quantconnect
import src.dashboard.routes.settings as settings
import src.dashboard.routes.stock as stock
import src.dashboard.routes.mistock as mistock
import src.dashboard.routes.plunge_bounce as plunge_bounce
import src.dashboard.routes.narrative_momentum as narrative_momentum
import src.dashboard.routes.ai_stock as ai_stock

for route_module in [
    pages,
    account,
    futures,
    quantconnect,
    settings,
    stock,
    mistock,
    plunge_bounce,
    narrative_momentum,
    ai_stock,
]:
    app.include_router(route_module.router)


@asynccontextmanager
async def _dashboard_lifespan(_app):
    settings.run_dashboard_startup_tasks()
    try:
        yield
    finally:
        settings._stop_kis_websocket()


app.router.lifespan_context = _dashboard_lifespan


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/ai-stock"):
        market = request.query_params.get("market") or _ai_stock_constants.MARKET_ALL
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return _ai_stock_error_response(exc.status_code, market, [detail])
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def _request_validation_exception_handler(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/api/ai-stock"):
        market = request.query_params.get("market") or _ai_stock_constants.MARKET_ALL
        errors = []
        for item in exc.errors():
            loc = ".".join(str(part) for part in item.get("loc", []))
            msg = item.get("msg", "validation error")
            errors.append(f"{loc}: {msg}" if loc else str(msg))
        return _ai_stock_error_response(422, market, errors or ["validation error"])
    return await request_validation_exception_handler(request, exc)


def _ai_stock_error_response(status_code: int, market: str, errors: list[str]) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=_ai_stock_envelope(
            None,
            market=market,
            errors=errors,
            ok=False,
            meta={"data_quality": "error"},
        ),
    )

# Dynamically expose all names from core and all route files for backward compatibility
import src.dashboard.core as _core

for mod in [_core, pages, account, futures, quantconnect, settings, stock, mistock, plunge_bounce, narrative_momentum]:
    globals().update({k: v for k, v in mod.__dict__.items() if not k.startswith("__")})
