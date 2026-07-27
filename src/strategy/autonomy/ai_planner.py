"""AI adapter that produces complete, validated autonomous trade intents."""
from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence

import requests

from src.config import config
from src.db.usage_repository import update_token_usage
from src.online_access import require_online_access

from .models import (
    EntryPlan,
    ExitPlan,
    ExitTarget,
    InvalidationPlan,
    OrderPlan,
    OrderType,
    TimeInForce,
    TradeAction,
    TradeIntent,
    TrailingStopPlan,
)
from .orchestrator import MarketContext, PortfolioContext
from .validation import validate_trade_intent


class PlannerError(RuntimeError):
    """Planning failed; callers must not substitute a fallback trade."""


@dataclass(frozen=True)
class PlannerResponse:
    payload: Mapping[str, Any]
    model: str
    response_id: str | None = None
    usage: Mapping[str, int] | None = None


class PlannerProvider(Protocol):
    def plan(
        self,
        *,
        instructions: str,
        context: Mapping[str, Any],
        schema: Mapping[str, Any],
    ) -> PlannerResponse: ...


class DemoRulePlanner:
    """Deterministic structured planner for local-rule strategies."""

    def plan(
        self,
        *,
        instructions: str,
        context: Mapping[str, Any],
        schema: Mapping[str, Any],
    ) -> PlannerResponse:
        autonomy_env = str(
            getattr(config, "autonomy_trading_env", "demo")
        ).lower()
        trading_env = str(getattr(config, "trading_env", "demo")).lower()
        if autonomy_env != trading_env:
            raise PlannerError("autonomy and broker environments must match")
        if autonomy_env == "real" and not (
            bool(getattr(config, "enable_live_trading", False))
            and bool(getattr(config, "autonomy_enable_live_trading", False))
            and bool(getattr(config, "autonomy_live_opt_in", False))
        ):
            raise PlannerError("real autonomy requires every live opt-in")
        strategy = context["strategy"]
        snapshot = context["market_snapshot"]
        created = _datetime(snapshot["evaluated_at"])
        data_as_of = _datetime(snapshot["data_as_of"])
        valid_until = created + timedelta(minutes=15)
        mode = str(context.get("mode") or "")
        if mode == "position_management":
            position = context["position"]
            symbol = str(position["symbol"])
            portfolio = context.get("portfolio_snapshot") or {}
            snapshots = portfolio.get("risk_snapshots") or {}
            risk = snapshots.get(symbol) or {}
            current_price = float(risk.get("current_price") or 0)
            stop_price = float(position.get("current_stop_price") or 0)
            target_price = _first_target_price(position.get("target_plan"))
            max_holding_until = _datetime_or_none(
                position.get("max_holding_until")
            )
            action = "hold"
            reasons = ["demo_rule_position_hold"]
            if current_price > 0 and stop_price > 0 and current_price <= stop_price:
                action = "exit"
                reasons = ["hard_stop_reached"]
            elif current_price > 0 and target_price > 0 and current_price >= target_price:
                action = "exit"
                reasons = ["take_profit_reached"]
            elif max_holding_until is not None and created >= max_holding_until:
                action = "exit"
                reasons = ["max_holding_period_reached"]
            payload = _base_demo_payload(
                context, symbol, created, data_as_of, valid_until
            )
            payload.update(
                {
                    "action": action,
                    "confidence": 0.95 if action == "exit" else 0.6,
                    "thesis": (
                        "Demo exit rule triggered under deterministic risk control."
                        if action == "exit"
                        else "Demo rule position remains under deterministic risk control."
                    ),
                    "entry": None,
                    "invalidation": None,
                    "exit_plan": None,
                    "position_id": str(position["id"]),
                    "reduce_pct": 100.0 if action == "exit" else None,
                    "reasons": reasons,
                }
            )
        else:
            candidate = context["candidate"]
            symbol = str(candidate["symbol"])
            price = float(candidate.get("current_price") or 0)
            if price <= 0:
                raise PlannerError("demo rule candidate requires a positive price")
            payload = _base_demo_payload(
                context, symbol, created, data_as_of, valid_until
            )
            payload.update(
                {
                    "action": "enter_long",
                    "confidence": 0.65,
                    "thesis": "Validated local-rule candidate selected for demo execution.",
                    "entry": {
                        "order": {
                            "order_type": "limit",
                            "time_in_force": "day",
                            "limit_price": price,
                            "stop_price": None,
                            "expires_at": valid_until.isoformat(),
                        },
                        "price_min": price * 0.995,
                        "price_max": price * 1.005,
                    },
                    "invalidation": {
                        "hard_stop_price": price * 0.95,
                        "conditions": ["demo_rule_signal_invalidated"],
                    },
                    "exit_plan": {
                        "targets": [
                            {"price": price * 1.10, "reduce_pct": 100.0}
                        ],
                        "trailing_stop": None,
                        "max_holding_until": (
                            created + timedelta(days=5)
                        ).isoformat(),
                    },
                    "position_id": None,
                    "reduce_pct": None,
                    "reasons": ["local_rule_candidate", autonomy_env],
                }
            )
        return PlannerResponse(payload, "demo-rule-v1", response_id=None, usage={})


def _base_demo_payload(context, symbol, created, data_as_of, valid_until):
    strategy = context["strategy"]
    digest = hashlib.sha256(
        (
            f"{strategy['strategy_id']}|{symbol}|{context.get('mode')}|"
            f"{created.isoformat()}"
        ).encode("utf-8")
    ).hexdigest()[:24]
    return {
        "intent_id": f"demo-{digest}",
        "strategy_id": str(strategy["strategy_id"]),
        "strategy_version": int(strategy["strategy_version"]),
        "profile_hash": str(strategy["profile_hash"]),
        "symbol": symbol,
        "market": str(context["market_snapshot"]["market"]),
        "created_at": created.isoformat(),
        "data_as_of": data_as_of.isoformat(),
        "valid_until": valid_until.isoformat(),
        "evidence": {"planner": "demo-rule-v1"},
        "metadata": {"demo": True, "fallback_used": False},
    }


def _first_target_price(value: Any) -> float:
    if isinstance(value, Mapping):
        targets = value.get("targets")
    else:
        targets = value
    if not isinstance(targets, (list, tuple)):
        return 0.0
    for item in targets:
        if isinstance(item, Mapping):
            try:
                price = float(item.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if price > 0:
                return price
    return 0.0


class OpenAIResponsesPlanner:
    """Minimal Responses API provider using strict Structured Outputs."""

    endpoint = "https://api.openai.com/v1/responses"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        session: Any = requests,
    ):
        self.api_key = str(
            api_key if api_key is not None else getattr(config, "openai_api_key", "")
        ).strip()
        self.model = str(
            model if model is not None else getattr(config, "openai_model", "gpt-5-mini")
        ).strip()
        self.timeout_seconds = float(
            timeout_seconds
            if timeout_seconds is not None
            else getattr(config, "openai_timeout_seconds", 20.0)
        )
        self.session = session

    def plan(self, *, instructions, context, schema) -> PlannerResponse:
        if not self.api_key:
            raise PlannerError("OPENAI_API_KEY is not configured")
        if not self.model:
            raise PlannerError("OpenAI model is not configured")
        require_online_access("autonomous AI trade planning")
        response = self.session.post(
            self.endpoint,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "instructions": instructions,
                "input": json.dumps(_json_safe(context), ensure_ascii=False),
                "store": False,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "autonomous_trade_intent",
                        "strict": True,
                        "schema": schema,
                    }
                },
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("status") != "completed":
            raise PlannerError(
                f"OpenAI response is not complete: {body.get('status') or 'unknown'}"
            )
        refusal = _find_refusal(body)
        if refusal:
            raise PlannerError(f"OpenAI planning refusal: {refusal}")
        output_text = body.get("output_text") or _find_output_text(body)
        if not output_text:
            raise PlannerError("OpenAI response missing structured output")
        try:
            parsed = json.loads(output_text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise PlannerError("OpenAI structured output is invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise PlannerError("OpenAI structured output must be an object")

        usage = body.get("usage") or {}
        normalized_usage = {
            "prompt_tokens": int(
                usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
            ),
            "completion_tokens": int(
                usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
            ),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }
        update_token_usage(
            normalized_usage["prompt_tokens"],
            normalized_usage["completion_tokens"],
            normalized_usage["total_tokens"],
        )
        return PlannerResponse(
            parsed,
            str(body.get("model") or self.model),
            str(body.get("id")) if body.get("id") else None,
            normalized_usage,
        )


class AutonomousAIAdapter:
    """Generate one full intent per candidate and continuously manage positions."""

    def __init__(
        self,
        *,
        strategy_id: str,
        strategy_version: int,
        profile_hash: str,
        provider: PlannerProvider,
        strategy_instructions: str = "",
    ):
        self.strategy_id = str(strategy_id)
        self.strategy_version = int(strategy_version)
        self.profile_hash = str(profile_hash)
        self.provider = provider
        self.strategy_instructions = str(strategy_instructions)

    def scan(
        self, market: MarketContext, portfolio: PortfolioContext
    ) -> Sequence[TradeIntent]:
        candidates = market.features.get("candidates", ())
        if not isinstance(candidates, (list, tuple)):
            raise PlannerError("market candidates must be a sequence")
        return tuple(
            self._plan(
                mode="candidate_scan",
                market=market,
                portfolio=portfolio,
                subject={"candidate": candidate},
            )
            for candidate in candidates
        )

    def manage_position(
        self,
        position: Mapping[str, Any],
        market: MarketContext,
        portfolio: PortfolioContext,
    ) -> TradeIntent:
        if str(position.get("strategy_id")) != self.strategy_id:
            raise PlannerError("position is not owned by this strategy")
        return self._plan(
            mode="position_management",
            market=market,
            portfolio=portfolio,
            subject={
                "position": position,
                "previous_thesis": position.get("entry_thesis"),
                "invalidation_conditions": position.get("invalidation_conditions"),
                "previous_decision_id": position.get("last_decision_id"),
            },
        )

    def _plan(self, *, mode, market, portfolio, subject) -> TradeIntent:
        context = {
            "mode": mode,
            "strategy": {
                "strategy_id": self.strategy_id,
                "strategy_version": self.strategy_version,
                "profile_hash": self.profile_hash,
                "instructions": self.strategy_instructions,
            },
            "market_snapshot": _json_safe(market),
            "portfolio_snapshot": _json_safe(portfolio),
            **_json_safe(subject),
        }
        response = self.provider.plan(
            instructions=_planner_instructions(mode),
            context=context,
            schema=TRADE_INTENT_JSON_SCHEMA,
        )
        intent = trade_intent_from_payload(response.payload)
        if intent.strategy_id != self.strategy_id:
            raise PlannerError("planned strategy_id mismatch")
        if intent.strategy_version != self.strategy_version:
            raise PlannerError("planned strategy_version mismatch")
        if intent.profile_hash != self.profile_hash:
            raise PlannerError("planned profile_hash mismatch")
        if intent.market != market.market:
            raise PlannerError("planned market mismatch")
        if mode == "position_management":
            expected = str(subject["position"].get("id"))
            if intent.position_id != expected:
                raise PlannerError("planned position_id mismatch")
            if intent.symbol != str(subject["position"].get("symbol")):
                raise PlannerError("planned position symbol mismatch")
        validate_trade_intent(intent, now=market.evaluated_at)
        return intent


def trade_intent_from_payload(payload: Mapping[str, Any]) -> TradeIntent:
    """Parse strict-schema data into the domain model; never repair values."""
    try:
        entry_raw = payload.get("entry")
        entry = None
        if entry_raw is not None:
            order_raw = entry_raw["order"]
            entry = EntryPlan(
                order=OrderPlan(
                    order_type=OrderType(order_raw["order_type"]),
                    time_in_force=TimeInForce(order_raw["time_in_force"]),
                    limit_price=order_raw["limit_price"],
                    stop_price=order_raw["stop_price"],
                    expires_at=_datetime_or_none(order_raw["expires_at"]),
                ),
                price_min=entry_raw["price_min"],
                price_max=entry_raw["price_max"],
            )
        invalidation_raw = payload.get("invalidation")
        invalidation = (
            InvalidationPlan(
                hard_stop_price=invalidation_raw["hard_stop_price"],
                conditions=tuple(invalidation_raw["conditions"]),
            )
            if invalidation_raw is not None
            else None
        )
        exit_raw = payload.get("exit_plan")
        exit_plan = None
        if exit_raw is not None:
            trailing = exit_raw["trailing_stop"]
            exit_plan = ExitPlan(
                targets=tuple(
                    ExitTarget(item["price"], item["reduce_pct"])
                    for item in exit_raw["targets"]
                ),
                trailing_stop=(
                    TrailingStopPlan(
                        trailing["activate_after_r"], trailing["atr_multiple"]
                    )
                    if trailing is not None
                    else None
                ),
                max_holding_until=_datetime_or_none(exit_raw["max_holding_until"]),
            )
        return TradeIntent(
            intent_id=payload["intent_id"],
            strategy_id=payload["strategy_id"],
            strategy_version=payload["strategy_version"],
            profile_hash=payload["profile_hash"],
            symbol=payload["symbol"],
            market=payload["market"],
            action=TradeAction(payload["action"]),
            confidence=payload["confidence"],
            thesis=payload["thesis"],
            created_at=_datetime(payload["created_at"]),
            data_as_of=_datetime(payload["data_as_of"]),
            valid_until=_datetime(payload["valid_until"]),
            entry=entry,
            invalidation=invalidation,
            exit_plan=exit_plan,
            position_id=payload["position_id"],
            reduce_pct=payload["reduce_pct"],
            reasons=tuple(payload["reasons"]),
            evidence=dict(payload["evidence"]),
            metadata=dict(payload["metadata"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PlannerError("structured trade intent could not be parsed") from exc


def _planner_instructions(mode: str) -> str:
    return (
        "Create exactly one complete autonomous long-only trade intent as JSON. "
        "Use only the supplied snapshots. Never choose order quantity, loosen operator "
        "risk limits, or claim fallback approval. For enter_long/add include entry, "
        "hard-stop invalidation, and a complete exit plan. For position management "
        "preserve the supplied position identity and reassess the previous thesis. "
        "Use reduce_pct for REDUCE, exactly 100 for EXIT, and null otherwise; never "
        "output a share quantity. "
        f"Planning mode: {mode}."
    )


def _datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("datetime must be an ISO-8601 string")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _datetime_or_none(value: Any) -> datetime | None:
    return None if value is None else _datetime(value)


def _find_refusal(body: Mapping[str, Any]) -> str | None:
    for output in body.get("output") or ():
        for content in output.get("content") or ():
            if content.get("type") == "refusal":
                return str(content.get("refusal") or "refused")
    return None


def _find_output_text(body: Mapping[str, Any]) -> str | None:
    for output in body.get("output") or ():
        for content in output.get("content") or ():
            if content.get("type") == "output_text":
                return content.get("text")
    return None


def _json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


_NULL_NUMBER = {"type": ["number", "null"]}
_NULL_STRING = {"type": ["string", "null"]}
TRADE_INTENT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "intent_id": {"type": "string", "minLength": 1},
        "strategy_id": {"type": "string", "minLength": 1},
        "strategy_version": {"type": "integer", "minimum": 1},
        "profile_hash": {"type": "string", "minLength": 1},
        "symbol": {"type": "string", "minLength": 1},
        "market": {"type": "string", "minLength": 1},
        "action": {"type": "string", "enum": [item.value for item in TradeAction]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "thesis": {"type": "string", "minLength": 1},
        "created_at": {"type": "string"},
        "data_as_of": {"type": "string"},
        "valid_until": {"type": "string"},
        "entry": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "order": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "order_type": {
                                    "type": "string",
                                    "enum": [item.value for item in OrderType],
                                },
                                "time_in_force": {
                                    "type": "string",
                                    "enum": [item.value for item in TimeInForce],
                                },
                                "limit_price": _NULL_NUMBER,
                                "stop_price": _NULL_NUMBER,
                                "expires_at": _NULL_STRING,
                            },
                            "required": [
                                "order_type", "time_in_force", "limit_price",
                                "stop_price", "expires_at",
                            ],
                        },
                        "price_min": {"type": "number"},
                        "price_max": {"type": "number"},
                    },
                    "required": ["order", "price_min", "price_max"],
                },
            ]
        },
        "invalidation": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "hard_stop_price": {"type": "number"},
                        "conditions": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["hard_stop_price", "conditions"],
                },
            ]
        },
        "exit_plan": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "targets": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "price": {"type": "number"},
                                    "reduce_pct": {"type": "number"},
                                },
                                "required": ["price", "reduce_pct"],
                            },
                        },
                        "trailing_stop": {
                            "anyOf": [
                                {"type": "null"},
                                {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "activate_after_r": {"type": "number"},
                                        "atr_multiple": {"type": "number"},
                                    },
                                    "required": ["activate_after_r", "atr_multiple"],
                                },
                            ]
                        },
                        "max_holding_until": _NULL_STRING,
                    },
                    "required": ["targets", "trailing_stop", "max_holding_until"],
                },
            ]
        },
        "position_id": _NULL_STRING,
        "reduce_pct": _NULL_NUMBER,
        "reasons": {"type": "array", "items": {"type": "string"}},
        "evidence": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "signals": {"type": "array", "items": {"type": "string"}},
                "data_quality": {"type": "string"},
            },
            "required": ["signals", "data_quality"],
        },
        "metadata": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "fallback_used": {"type": "boolean"},
                "planner_mode": {"type": "string"},
            },
            "required": ["fallback_used", "planner_mode"],
        },
    },
    "required": [
        "intent_id", "strategy_id", "strategy_version", "profile_hash", "symbol",
        "market", "action", "confidence", "thesis", "created_at", "data_as_of",
        "valid_until", "entry", "invalidation", "exit_plan", "position_id",
        "reduce_pct", "reasons", "evidence", "metadata",
    ],
}
