"""Concrete restart recovery hooks for managed orders and hard-stop protection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from src.db import ai_stock_repository as repository

from .broker_adapters import ManagedOrderReconciler, ReconciliationResult
from .protection import (
    HardStopProtectionService,
    ProtectionBroker,
    ProtectionGateSignal,
)


@dataclass(frozen=True)
class ProtectionRecoveryResult:
    position_id: int
    status: str
    reason: str = ""


class AutonomousRecoveryService:
    """RecoveryHooks implementation used by ContinuousStrategyService."""

    def __init__(
        self,
        *,
        order_reconcilers: Mapping[str, ManagedOrderReconciler],
        protection_brokers: Mapping[str, ProtectionBroker],
        protection: HardStopProtectionService | None = None,
        repo: Any = repository,
    ):
        self.order_reconcilers = {
            str(key).upper(): value for key, value in order_reconcilers.items()
        }
        self.protection_brokers = {
            str(key).upper(): value for key, value in protection_brokers.items()
        }
        self.repo = repo
        self.protection = protection or HardStopProtectionService(repo=repo)
        self.last_order_results: dict[str, tuple[ReconciliationResult, ...]] = {}
        self.last_protection_results: dict[
            str, tuple[ProtectionRecoveryResult, ...]
        ] = {}

    def reconcile_open_orders(self, market: str) -> tuple[ReconciliationResult, ...]:
        market = str(market).upper()
        reconciler = self.order_reconcilers.get(market)
        if reconciler is None:
            raise RuntimeError(f"managed order reconciler unavailable for {market}")
        results = reconciler.recover_unsettled()
        if any(item.status in {"error", "inconsistent"} for item in results):
            raise RuntimeError(f"managed order reconciliation incomplete for {market}")
        self.last_order_results[market] = results
        return results

    def reconcile_protections(
        self, market: str
    ) -> tuple[ProtectionRecoveryResult, ...]:
        market = str(market).upper()
        broker = self.protection_brokers.get(market)
        if broker is None:
            raise RuntimeError(f"protection broker unavailable for {market}")
        results: list[ProtectionRecoveryResult] = []
        positions = self.repo.list_strategy_positions(
            market=market, active_only=True
        )
        virtual_totals: dict[tuple[str, str], int] = {}
        for position in positions:
            if str(position.get("side") or "long") != "long":
                continue
            key = (str(position.get("account_id") or ""), str(position["symbol"]))
            virtual_totals[key] = virtual_totals.get(key, 0) + int(
                position.get("remaining_qty") or 0
            )
        mismatched_owners: set[tuple[str, str]] = set()
        for (account_id, symbol), virtual_qty in virtual_totals.items():
            actual_qty = int(
                broker.fetch_position_qty(account_id=account_id, symbol=symbol)
            )
            if actual_qty != virtual_qty:
                mismatched_owners.add((account_id, symbol))

        for position in positions:
            position_id = int(position["id"])
            open_qty = int(position.get("remaining_qty") or 0)
            if open_qty <= 0 or str(position.get("side") or "long") != "long":
                continue
            owner_key = (
                str(position.get("account_id") or ""),
                str(position["symbol"]),
            )
            if owner_key in mismatched_owners:
                results.append(
                    ProtectionRecoveryResult(
                        position_id,
                        "error",
                        "broker position quantity differs from strategy ledger",
                    )
                )
                continue
            stop = float(position.get("current_stop_price") or 0)
            if stop <= 0:
                results.append(
                    ProtectionRecoveryResult(position_id, "error", "hard stop missing")
                )
                continue
            try:
                protection = self.protection.request_entry_fill(
                    position_id=position_id,
                    filled_qty=open_qty,
                    stop_price=stop,
                )
                broker_order_id = protection.get("broker_order_id")
                force_new = False
                if broker_order_id:
                    observed = broker.fetch_hard_stop(
                        self.protection.build_request(protection)
                    )
                    if observed.exists and observed.active:
                        protection = self.repo.activate_position_protection(
                            int(protection["id"]),
                            broker_order_id=observed.broker_order_id,
                            protected_qty=int(observed.protected_qty),
                            stop_price=float(observed.stop_price),
                            payload=dict(observed.payload or {}),
                        )
                    else:
                        force_new = True
                if (
                    str(protection.get("status")) != "active"
                    or int(protection.get("protected_qty") or 0) != open_qty
                    or float(protection.get("current_stop_price") or 0) < stop
                ):
                    protection = self.protection.submit_requested(
                        protection, broker, force_new=force_new
                    )
                status = (
                    "recovered"
                    if str(protection.get("status")) == "active"
                    and int(protection.get("protected_qty") or 0) == open_qty
                    else "incomplete"
                )
                results.append(ProtectionRecoveryResult(position_id, status))
            except Exception as exc:
                results.append(
                    ProtectionRecoveryResult(
                        position_id, "error", f"{type(exc).__name__}:{exc}"
                    )
                )
        packed = tuple(results)
        self.last_protection_results[market] = packed
        return packed

    def audit_unprotected_positions(self, market: str) -> ProtectionGateSignal:
        market = str(market).upper()
        broker = self.protection_brokers.get(market)
        if broker is None:
            raise RuntimeError(f"protection broker unavailable for {market}")
        if not bool(getattr(broker, "supports_hard_stops", False)):
            reason = str(
                getattr(broker, "reason", f"hard-stop protection unsupported for {market}")
            )
            return ProtectionGateSignal(
                block_new_risk=True,
                reason="protection_broker_unavailable",
                alerts=(reason,),
            )
        recovery = self.reconcile_protections(market)
        signal = self.protection.global_gate_signal(market=market)
        failures = tuple(
            item for item in recovery if item.status in {"error", "incomplete"}
        )
        if not failures:
            return signal
        alerts = tuple(
            f"보호 복구 실패: position={item.position_id} {item.reason}"
            for item in failures
        )
        return ProtectionGateSignal(
            block_new_risk=True,
            reason="protection_recovery_incomplete",
            uncovered_positions=signal.uncovered_positions,
            alerts=tuple(signal.alerts) + alerts,
        )
