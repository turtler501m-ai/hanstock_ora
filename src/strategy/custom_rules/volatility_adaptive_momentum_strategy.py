"""고변동성 적응형 모멘텀 전략."""

from __future__ import annotations

import os
from statistics import mean


STRATEGY_PROFILE = {
    "strategy_type": "momentum",
    "risk_level": "aggressive",
    "provider": "volatility",
    "model": "volatility_adaptive_momentum_strategy",
    "ai_weight": 0.0,
    "min_rule_score_for_ai": 2.0,
    "market_regime_filter": ["high_volatility"],
    "focus": [
        "volatility_expansion",
        "volume_confirmation",
        "trend_continuation",
        "oversold_reversal",
    ],
    "avoid": [
        "extreme_gap_up",
        "weak_liquidity",
        "medium_term_breakdown",
    ],
    "risk": {
        "max_ai_weight": 0.0,
        "max_risk_per_trade_pct": 0.5,
        "max_total_open_risk_pct": 2.0,
        "max_sector_exposure_pct": 20.0,
        "max_liquidity_participation_pct": 0.5,
        "max_strategy_exposure_pct": 30.0,
        "max_data_age_seconds": 60,
        "min_cash_reserve_pct": 20.0,
        "max_daily_ai_orders": 3,
    },
}


class VolatilityAdaptiveMomentumStrategy:
    """사용자전략 고변동성 적응형 모멘텀 전략

    변동성이 확대된 장에서 거래량을 동반한 추세 지속과 과매도 반전을
    구분해 평가합니다. 급등 추격을 제한하고 유동성·과열·추세 훼손
    필터를 함께 적용하는 단기 대응 전략입니다.
    """

    def __init__(self) -> None:
        self.min_realized_vol = float(os.environ.get("VAM_MIN_REALIZED_VOL", "1.2"))
        self.max_realized_vol = float(os.environ.get("VAM_MAX_REALIZED_VOL", "12.0"))
        self.min_trade_value = float(os.environ.get("VAM_MIN_TRADE_VALUE_KRW", "500000000"))
        self.max_one_day_return = float(os.environ.get("VAM_MAX_ONE_DAY_RETURN", "12.0"))
        self.max_five_day_return = float(os.environ.get("VAM_MAX_FIVE_DAY_RETURN", "28.0"))
        # The latest daily candle is incomplete during market hours, so use a
        # participation floor and reserve larger bonuses for real expansion.
        self.min_volume_ratio = float(os.environ.get("VAM_MIN_VOLUME_RATIO", "0.05"))

    def calculate_score(self, prices: list[float], indicators: dict) -> float:
        closes = [float(value) for value in prices if value and float(value) > 0]
        volumes = [
            float(value)
            for value in indicators.get("volumes", [])
            if value is not None and float(value) >= 0
        ]
        reasons: list[str] = []
        indicators["custom_reasons"] = reasons

        if len(closes) < 60 or len(volumes) < 21:
            reasons.append("판단 데이터 부족(종가 60일·거래량 21일 필요)")
            return 0.0

        current = closes[-1]
        previous = closes[-2]
        sma20 = float(indicators.get("sma20") or mean(closes[-20:]))
        sma60 = float(indicators.get("sma60") or mean(closes[-60:]))
        rsi = float(indicators.get("rsi") or 50.0)
        macd_hist = float(indicators.get("macd_hist") or 0.0)
        high20 = max(closes[-21:-1])
        avg_volume20 = mean(volumes[-21:-1])
        volume_ratio = volumes[-1] / avg_volume20 if avg_volume20 > 0 else 0.0
        trade_value = current * volumes[-1]

        daily_returns = [
            abs(_return_pct(closes[index], closes[index - 1]))
            for index in range(len(closes) - 10, len(closes))
        ]
        realized_vol = mean(daily_returns)
        return_1d = _return_pct(current, previous)
        return_5d = _return_pct(current, closes[-6])
        return_20d = _return_pct(current, closes[-21])
        high20_gap = _return_pct(current, high20)

        indicators["volatility_adaptive_momentum"] = {
            "realized_vol_10d": round(realized_vol, 3),
            "volume_ratio": round(volume_ratio, 3),
            "trade_value": round(trade_value, 2),
            "return_1d": round(return_1d, 3),
            "return_5d": round(return_5d, 3),
            "return_20d": round(return_20d, 3),
            "high20_gap": round(high20_gap, 3),
        }

        if not self.min_realized_vol <= realized_vol <= self.max_realized_vol:
            reasons.append(f"변동성 범위 이탈({realized_vol:.1f}%)")
            return 0.0
        if trade_value < self.min_trade_value:
            reasons.append(f"거래대금 부족({trade_value:,.0f}원)")
            return 0.0
        if return_1d > self.max_one_day_return or return_5d > self.max_five_day_return:
            reasons.append(f"단기 급등 추격 제한(1일 {return_1d:.1f}%, 5일 {return_5d:.1f}%)")
            return 0.0

        score = 0.0

        trend_setup = (
            current >= sma20
            and sma20 >= sma60
            and return_5d > 0
            and high20_gap >= -5.0
        )
        rebound_setup = (
            rsi <= 38.0
            and current > previous
            and (macd_hist > 0 or current >= sma20 * 0.97)
        )

        if trend_setup:
            score += 2.2
            reasons.append(
                f"고변동성 추세 지속(5일 {return_5d:.1f}%, 20일 고점 대비 {high20_gap:.1f}%)"
            )
        elif rebound_setup:
            score += 2.0
            reasons.append(f"과매도 반전(RSI {rsi:.1f}, 1일 {return_1d:.1f}%)")
        else:
            reasons.append("추세 지속·과매도 반전 조건 미충족")
            return 0.0

        if volume_ratio >= 1.5:
            score += 1.2
            reasons.append(f"거래량 확장({volume_ratio:.1f}배)")
        elif volume_ratio >= 0.5:
            score += 0.6
            reasons.append(f"거래량 확인({volume_ratio:.1f}배)")
        elif volume_ratio >= self.min_volume_ratio:
            score += 0.2
            reasons.append(f"장중 거래량 최소 확인({volume_ratio:.2f}배)")
        else:
            reasons.append(f"거래량 확인 부족({volume_ratio:.1f}배)")
            return 0.0

        if macd_hist > 0:
            score += 0.8
            reasons.append("MACD 양의 모멘텀")
        if 42.0 <= rsi <= 68.0:
            score += 0.5
            reasons.append(f"RSI 추세 구간({rsi:.1f})")
        if return_20d < -25.0:
            score = min(score, 1.5)
            reasons.append(f"중기 추세 훼손 제한(20일 {return_20d:.1f}%)")

        return round(min(5.0, score), 3)


def _return_pct(current: float, base: float) -> float:
    if base <= 0:
        return 0.0
    return (current / base - 1.0) * 100.0
