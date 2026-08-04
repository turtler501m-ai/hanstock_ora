"""Compatibility exports for the physically separated stock routers."""

from fastapi import APIRouter
from src.dashboard.routes.stock_analysis import router as analysis_router
from src.dashboard.routes.stock_order import router as order_router
from src.dashboard.routes.stock_performance import router as performance_router
from src.dashboard.routes.stock_plan import router as plan_router


def build_stock_router(source: APIRouter | None = None) -> APIRouter:
    """Return the aggregate router; source remains accepted for compatibility."""
    aggregate = APIRouter()
    aggregate.include_router(analysis_router)
    aggregate.include_router(plan_router)
    aggregate.include_router(order_router)
    aggregate.include_router(performance_router)
    return aggregate
