import unittest

from src.dashboard.routes.stock_route_groups import (
    analysis_router,
    order_router,
    performance_router,
    plan_router,
)


def _paths(router) -> set[str]:
    return {getattr(route, "path", "") for route in router.routes}


def _endpoint_modules(router) -> set[str]:
    return {
        getattr(getattr(route, "endpoint", None), "__module__", "")
        for route in router.routes
    }


class StockRouteOwnershipTests(unittest.TestCase):
    def test_order_routes_have_order_owner(self):
        paths = _paths(order_router)
        self.assertIn("/api/approvals", paths)
        self.assertIn("/api/trades/order-status/sync", paths)
        self.assertIn("/api/holdings/sell-all", paths)

    def test_performance_routes_have_performance_owner(self):
        paths = _paths(performance_router)
        self.assertIn("/api/performance", paths)
        self.assertIn("/api/performance/periodic", paths)
        self.assertIn("/api/decisions/history", paths)

    def test_plan_routes_have_plan_owner(self):
        paths = _paths(plan_router)
        self.assertIn("/api/risk/status", paths)
        self.assertIn("/api/scheduler/status", paths)
        self.assertIn("/api/system/kill", paths)

    def test_analysis_routes_have_analysis_owner(self):
        paths = _paths(analysis_router)
        self.assertIn("/api/ai-strategies", paths)
        self.assertIn("/api/watchlist", paths)
        self.assertIn("/api/analysis-cycles", paths)

    def test_no_route_is_owned_by_multiple_groups(self):
        groups = [
            _paths(analysis_router),
            _paths(plan_router),
            _paths(order_router),
            _paths(performance_router),
        ]
        for index, paths in enumerate(groups):
            for other in groups[index + 1 :]:
                self.assertFalse(paths & other)

    def test_handlers_are_physically_owned_by_bounded_modules(self):
        expected = [
            (analysis_router, "src.dashboard.routes.stock_analysis"),
            (plan_router, "src.dashboard.routes.stock_plan"),
            (order_router, "src.dashboard.routes.stock_order"),
            (performance_router, "src.dashboard.routes.stock_performance"),
        ]
        for router, module_name in expected:
            self.assertEqual(_endpoint_modules(router), {module_name})


if __name__ == "__main__":
    unittest.main()
