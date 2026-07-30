# -*- coding: utf-8 -*-
"""탭3 AI 종목 발굴 = 1차 배치 종목 점수화 (§5.3·§4.8).

유니버스(§4.6) → 데이터 품질 검사 → 룰/기술/모멘텀/내러티브 점수 → AI 평가
→ 위험 감점 → 시장 국면 보정 → 판단 → 후보 저장. AI 실패는 룰 fallback.
"""
from __future__ import annotations

from typing import Any

from src.ai_stock import briefing_service, narrative_service, universe
from src.ai_stock.ai_evaluator import evaluate as ai_evaluate
from src.ai_stock.constants import (
    DATA_GOOD,
    DATA_INSUFFICIENT,
    DECISION_INSUFFICIENT,
    SCAN_COMPLETED,
    SCAN_PARTIAL,
    SCAN_FAILED,
)
from src.ai_stock.markets import currency_of, require_storable_market
from src.ai_stock.scoring import ScoreProfile, score_candidate
from src.ai_stock.freshness import now as _now
from src.db import ai_stock_repository as repo

FEATURE_VERSION = "ai_stock_features_v1"
PROMPT_VERSION = "ai_stock_prompt_v1"


def _rsi(prices: list[float], n: int = 14) -> float | None:
    try:
        from src.strategy.indicators import calc_rsi

        return float(calc_rsi(prices, n))
    except Exception:
        return None


def _sma(prices: list[float], n: int) -> float | None:
    try:
        from src.strategy.indicators import calc_sma

        return float(calc_sma(prices, n))
    except Exception:
        return None


def _pct(series: list[float], lookback: int) -> float:
    if not series or len(series) <= lookback or series[-1 - lookback] == 0:
        return 0.0
    return (series[-1] / series[-1 - lookback] - 1.0) * 100.0


def _clamp(v: float) -> float:
    return max(0.0, min(100.0, v))


def _technical_score(series: list[float]) -> float:
    rsi = _rsi(series) or 50.0
    sma20, sma60 = _sma(series, 20), _sma(series, 60)
    score = 50.0
    # RSI: 45~65 건강한 강세 구간 가점, 과매수/과매도 감점
    if 45 <= rsi <= 65:
        score += 15
    elif rsi > 75 or rsi < 30:
        score -= 15
    # 정배열
    if sma20 and sma60 and series[-1] > sma20 > sma60:
        score += 20
    elif sma20 and series[-1] < sma20:
        score -= 10
    return _clamp(score)


def _momentum_score(series: list[float]) -> float:
    r5, r20 = _pct(series, 5), _pct(series, 20)
    return _clamp(50.0 + r5 * 2.0 + r20 * 1.0)


def _rule_score(series: list[float]) -> float:
    # 결정적 룰: 추세(20일>0) + RSI 정상 + 종가>SMA20
    rsi = _rsi(series) or 50.0
    sma20 = _sma(series, 20)
    score = 40.0
    if _pct(series, 20) > 0:
        score += 25
    if 35 <= rsi <= 70:
        score += 20
    if sma20 and series[-1] >= sma20:
        score += 15
    return _clamp(score)


def _risk_penalty(series: list[float]) -> float:
    # 변동성·과열 → 위험 감점(최종 점수를 낮추는 방향).
    r5 = _pct(series, 5)
    penalty = 0.0
    if r5 > 15:  # 단기 급등 과열
        penalty += min(20.0, (r5 - 15) * 1.5)
    # 변동성
    if len(series) >= 21:
        rets = [series[i] / series[i - 1] - 1.0 for i in range(len(series) - 20, len(series)) if series[i - 1]]
        if rets:
            mean = sum(rets) / len(rets)
            vol = (sum((x - mean) ** 2 for x in rets) / len(rets)) ** 0.5 * 100
            if vol > 3.0:
                penalty += min(20.0, (vol - 3.0) * 5.0)
    return _clamp(penalty)


def _score_one(item: dict[str, Any], market: str, profile: ScoreProfile,
               regime: str, regime_mult: float) -> dict[str, Any]:
    from src.ai_stock.market_data import get_provider

    provider = get_provider()
    symbol = str(item.get("symbol"))
    name = item.get("name") or symbol
    quote = provider.quote(market, symbol) or {}
    series = provider.daily_series(market, symbol)
    price = quote.get("price", item.get("price"))

    base = {
        "market": market, "symbol": symbol, "name": name,
        "instrument_type": item.get("instrument_type", "stock"),
        "currency": currency_of(market),
        "current_price": price, "change_pct": quote.get("change_pct"),
        "strategy_version": 1, "feature_version": FEATURE_VERSION,
        "prompt_version": PROMPT_VERSION, "market_regime": regime,
        "data_as_of": _now().isoformat(),
    }

    if not series or len(series) < 21:
        # 데이터 부족 → insufficient_data (§5.3 필수 제한)
        base.update({
            "rule_score": 0.0, "technical_score": 0.0, "momentum_score": 0.0,
            "narrative_score": 0.0, "ai_score": 0.0, "risk_score": 0.0,
            "final_score": 0.0, "confidence": None, "decision": DECISION_INSUFFICIENT,
            "data_quality": DATA_INSUFFICIENT, "fallback_used": False,
            "warnings": ["insufficient_price_data"],
        })
        return base

    narr = narrative_service.narrative_score_for(market, symbol, name)
    components = {
        "rule": _rule_score(series),
        "technical": _technical_score(series),
        "momentum": _momentum_score(series),
        "narrative": narr["narrative_score"],
        "ai": 0.0,
    }
    # AI 평가 (실패/저신뢰 시 fallback)
    features = {
        "strategy_score": components["rule"],
        "rsi": _rsi(series), "ret5": _pct(series, 5), "ret20": _pct(series, 20),
        "feature_version": FEATURE_VERSION,
    }
    ai = ai_evaluate(features, strategy_profile={"weights": profile.weights})
    components["ai"] = ai["ai_score"]

    scored = score_candidate(
        components=components,
        profile=profile,
        regime_multiplier=regime_mult,
        data_quality_multiplier=1.0,
        risk_penalty=_risk_penalty(series),
        ai_confidence=ai["confidence"],
        ai_fallback=ai["fallback_used"],
        ai_disabled=(ai.get("model_status") == "disabled"),
        insufficient=False,
    )
    base.update(scored)
    base["data_quality"] = DATA_GOOD
    base["model"] = ai.get("model")
    base["related_narratives"] = narr.get("related", [])
    base["positive_factors"] = []
    base["negative_factors"] = []
    base["warnings"] = []
    base["invalidation_conditions"] = []
    if ai["fallback_used"]:
        base["fallback_reason"] = ai.get("reason")
    return base


def run_scan(*, market: str, strategy_id: str = "ai_stock_default_v1",
             options: dict[str, Any] | None = None) -> dict[str, Any]:
    """1차 배치 스캔. 중복 활성 스캔이면 ScanConflict (라우터가 409)."""
    market = require_storable_market(market)
    options = options or {}
    scan_id = repo.create_scan(
        market=market, strategy_id=strategy_id, strategy_version=1,
        model=None, feature_version=FEATURE_VERSION, prompt_version=PROMPT_VERSION,
        data_as_of=_now().isoformat(),
    )
    candidate_count = 0
    fallback_count = 0
    errors: list[str] = []
    try:
        policy = repo.get_policy(strategy_id, market)
        profile = ScoreProfile.from_dict((policy or {}).get("score_profile") if policy else None)
        from src.ai_stock.market_data import get_provider

        items = get_provider().universe_items(market)
        if market == "KR":
            from src.db.repository import load_watchlist_data
            from src.strategy.watchlist_policy import filter_registered_items

            items = filter_registered_items(
                items,
                load_watchlist_data().get("symbols", []),
            )
        uni = universe.build(market, items, policy)
        # 시황 국면 컨텍스트 (§4.7)
        regime_info = briefing_service.compute_regime(
            market, get_provider().index_series(market)
        )
        regime = regime_info.get("regime", "insufficient_data")
        regime_mult = briefing_service.regime_multiplier(regime)

        for item in uni["passed"]:
            try:
                cand = _score_one(item, market, profile, regime, regime_mult)
                cand["scan_id"] = scan_id
                cand["strategy_id"] = strategy_id
                cand["profile_hash"] = None
                repo.save_candidate(cand)
                candidate_count += 1
                if cand.get("fallback_used"):
                    fallback_count += 1
            except Exception as exc:  # 종목 부분 실패 격리 (§5.12.3)
                errors.append(f"{item.get('symbol')}: {exc}")
        status = SCAN_PARTIAL if errors else SCAN_COMPLETED
        repo.finish_scan(
            scan_id, status=status, candidate_count=candidate_count,
            fallback_count=fallback_count,
            error_message=("; ".join(errors[:5]) if errors else None),
        )
    except Exception as exc:
        repo.finish_scan(scan_id, status=SCAN_FAILED, error_message=str(exc))
        raise

    return {
        "scan_id": scan_id,
        "summary": {
            "market": market, "strategy_id": strategy_id,
            "candidate_count": candidate_count, "fallback_count": fallback_count,
            "universe_passed": uni["passed_count"], "universe_excluded": uni["excluded_count"],
            "regime": regime, "status": status,
        },
        "errors": errors,
    }
