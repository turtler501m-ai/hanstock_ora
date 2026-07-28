import asyncio
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, call, patch

import src.dashboard as dashboard
from src.dashboard.services.stock_service import DashboardStockService


COMMON_STRATEGY_ID = "news_ai_strategy_v1"
ISOLATED_STRATEGY_IDS = {
    "plunge_bounce_strategy",
    "heikin_ashi_scalping_strategy",
}


class CommonTabStrategyFlowTests(unittest.TestCase):
    @staticmethod
    def _get_route(path: str):
        for route in dashboard.app.routes:
            methods = getattr(route, "methods", set()) or set()
            if "GET" in methods and getattr(route, "path", "") == path:
                return route
        raise AssertionError(f"Missing GET route for {path}")

    def test_strategy_id_is_preserved_by_signals_candidates_and_execution_plan(self):
        signals_route = self._get_route("/api/signals")
        candidates_route = self._get_route("/api/candidates")
        execution_plan_route = self._get_route("/api/execution-plan")
        strategy = {
            "id": COMMON_STRATEGY_ID,
            "model": "none",
            "provider": "none",
            "weight": 0,
            "profile": {},
            "strategy_version": 3,
            "profile_hash": "profile-hash",
        }
        candidate_payload = {
            "candidates": [
                {
                    "ticker": "005930",
                    "name": "Samsung Electronics",
                    "current_price": 70000,
                    "score": 3,
                    "reasons": ["news"],
                }
            ],
            "scan_summary": [],
            "scanned": 1,
            "min_score": 2,
            "scan_error": None,
        }
        cycle = {"id": "cycle-common-001", "strategy_id": COMMON_STRATEGY_ID}

        with ExitStack() as stack:
            stack.enter_context(patch.object(dashboard.core, "_required_env_missing", return_value=[]))
            stack.enter_context(patch.object(dashboard.core, "_get_api", return_value=MagicMock()))
            stack.enter_context(patch.object(dashboard.core, "_get_balance_data", return_value={}))
            stack.enter_context(
                patch.object(
                    dashboard.core,
                    "_parse_balance",
                    return_value={
                        "cash": 1000000,
                        "total_eval": 1000000,
                        "pnl": 0,
                        "holdings": [],
                    },
                )
            )
            stack.enter_context(
                patch.object(
                    dashboard.core,
                    "snapshot_read_through",
                    side_effect=lambda _key, builder, **_kwargs: builder(),
                )
            )
            resolve_cycle = stack.enter_context(
                patch.object(
                    dashboard.core,
                    "_dashboard_analysis_cycle",
                    return_value=(COMMON_STRATEGY_ID, cycle),
                )
            )
            stack.enter_context(
                patch.object(dashboard.core, "_resolve_dashboard_strategy", return_value=strategy)
            )
            stack.enter_context(patch.object(dashboard.core, "mark_common_analysis_stage"))
            build_signals = stack.enter_context(
                patch.object(
                    dashboard.core.stock_service,
                    "build_dashboard_signals",
                    return_value=[{"strategy_id": COMMON_STRATEGY_ID, "symbol": "005930"}],
                )
            )
            stack.enter_context(patch.object(dashboard.core, "_load_candidate_cache", return_value=None))
            stack.enter_context(
                patch("src.db.repository.load_ai_strategies", return_value=[strategy])
            )
            stack.enter_context(
                patch("src.db.repository.load_strategy_universe_symbols", return_value=["005930"])
            )
            stack.enter_context(patch("src.db.repository.save_scanned_candidate", return_value=41))
            stack.enter_context(patch("src.db.repository.save_ai_strategies"))
            stack.enter_context(patch("src.db.repository.record_ai_strategy_event"))
            stack.enter_context(patch.object(dashboard.core, "_save_candidate_cache"))
            build_candidates = stack.enter_context(
                patch.object(dashboard.core, "build_dashboard_candidates", return_value=candidate_payload)
            )
            build_plan = stack.enter_context(
                patch.object(
                    dashboard.core.stock_service,
                    "build_dashboard_execution_plan",
                    return_value={"strategy_id": COMMON_STRATEGY_ID, "plan": []},
                )
            )

            signals = asyncio.run(
                signals_route.endpoint(
                    strategy_id=COMMON_STRATEGY_ID,
                    cycle_id=cycle["id"],
                )
            )
            candidates = asyncio.run(
                candidates_route.endpoint(
                    min_score=2,
                    ranker="gpt_5_mini",
                    optimizer="score_tilted_inverse_vol",
                    strategy_id=COMMON_STRATEGY_ID,
                    cycle_id=cycle["id"],
                )
            )
            plan = asyncio.run(
                execution_plan_route.endpoint(
                    strategy_id=COMMON_STRATEGY_ID,
                    cycle_id=cycle["id"],
                )
            )

        self.assertEqual(
            resolve_cycle.call_args_list,
            [
                call(COMMON_STRATEGY_ID, cycle["id"]),
                call(COMMON_STRATEGY_ID, cycle["id"]),
                call(COMMON_STRATEGY_ID, cycle["id"]),
            ],
        )
        self.assertEqual(build_signals.call_args.args[2]["id"], COMMON_STRATEGY_ID)
        self.assertEqual(signals["signals"][0]["strategy_id"], COMMON_STRATEGY_ID)
        self.assertEqual(signals["_analysis_cycle"], cycle)
        self.assertEqual(candidates["candidates"][0]["strategy_id"], COMMON_STRATEGY_ID)
        self.assertEqual(candidates["candidates"][0]["strategy_version"], 3)
        self.assertEqual(candidates["_analysis_cycle"], cycle)
        self.assertEqual(build_candidates.call_args.kwargs["strategy_model"], "none")
        self.assertEqual(build_plan.call_args.kwargs["strategy_id"], COMMON_STRATEGY_ID)
        self.assertEqual(
            build_plan.call_args.kwargs["parsed_balance"]["cash"],
            1000000,
        )
        self.assertEqual(plan["strategy_id"], COMMON_STRATEGY_ID)
        self.assertEqual(plan["_analysis_cycle"], cycle)

    def test_scheduler_preserves_common_strategy_id_for_each_common_cycle(self):
        common_ids = [COMMON_STRATEGY_ID, "seven_split"]
        with patch(
            "src.scheduler.run_scheduled_cycle",
            side_effect=lambda *_args, **kwargs: {
                "strategy_id": kwargs["force_strategy_id"],
                "plan": [],
            },
        ) as run_cycle, patch(
            "src.db.repository.load_ai_strategies",
            return_value=[],
        ):
            result = dashboard._run_scheduled_cycles_for_strategies(
                mode="analysis_only",
                include_ai_rebalance=False,
                auto_approve=False,
                strategy_ids=common_ids,
            )

        self.assertEqual(result["strategy_ids"], common_ids)
        self.assertEqual(
            [row["strategy_id"] for row in result["runs"]],
            common_ids,
        )
        self.assertEqual(
            run_cycle.call_args_list,
            [
                call(
                    "analysis_only",
                    include_ai_rebalance=False,
                    auto_approve=False,
                    force_strategy_id=COMMON_STRATEGY_ID,
                    allowed_categories=None,
                ),
                call(
                    "analysis_only",
                    include_ai_rebalance=False,
                    auto_approve=False,
                    force_strategy_id="seven_split",
                    allowed_categories=None,
                ),
            ],
        )

    def test_isolated_strategies_are_excluded_from_common_multi_strategy_cycle(self):
        requested_ids = [
            COMMON_STRATEGY_ID,
            "plunge_bounce_strategy",
            "seven_split",
            "heikin_ashi_scalping_strategy",
        ]
        with patch(
            "src.scheduler.run_scheduled_cycle",
            side_effect=lambda *_args, **kwargs: {
                "strategy_id": kwargs["force_strategy_id"],
                "plan": [],
            },
        ) as run_cycle, patch(
            "src.db.repository.load_ai_strategies",
            return_value=[],
        ):
            result = dashboard._run_scheduled_cycles_for_strategies(
                mode="analysis_only",
                include_ai_rebalance=False,
                auto_approve=False,
                strategy_ids=requested_ids,
            )

        common_ids = [COMMON_STRATEGY_ID, "seven_split"]
        self.assertEqual(result["strategy_ids"], common_ids)
        self.assertEqual(
            [row["strategy_id"] for row in result["runs"]],
            common_ids,
        )
        dispatched_ids = {
            invocation.kwargs["force_strategy_id"]
            for invocation in run_cycle.call_args_list
        }
        self.assertTrue(dispatched_ids.isdisjoint(ISOLATED_STRATEGY_IDS))

    def test_execution_plan_consumes_the_same_cycle_candidate_snapshot(self):
        candidate_scan = {
            "candidates": [{"ticker": "005930", "score": 3}],
            "scanned": 1,
        }
        runtime = {
            "plan": [],
            "remaining_cash": 900000,
            "daily_loss_halt": False,
            "candidate_scan": {"scanned": 1, "scan_error": None},
        }
        with patch.object(dashboard.core.trader, "build_market_data_api", return_value=MagicMock()), patch.object(
            dashboard.core.trader,
            "build_runtime_plan",
            return_value=runtime,
        ) as build_runtime:
            result = DashboardStockService().build_dashboard_execution_plan(
                api=MagicMock(),
                balance_data={},
                parsed_balance={"cash": 1000000, "total_eval": 1000000, "pnl": 0},
                strategy_id=COMMON_STRATEGY_ID,
                candidate_scan=candidate_scan,
            )

        self.assertEqual(
            build_runtime.call_args.kwargs["candidate_scan_override"],
            candidate_scan,
        )
        self.assertEqual(result["strategy_id"], COMMON_STRATEGY_ID)


if __name__ == "__main__":
    unittest.main()
