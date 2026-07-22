from __future__ import annotations

import os
from statistics import mean


class IssueSectorRotationStrategy:
    """이슈 섹터 순환 모멘텀 전략

    한국 주식시장에서 정책, 요금, 원전, 반도체, 방산처럼 이슈 섹터가 빠르게
    순환하며 급등락을 반복할 때 쓰는 단기 모멘텀 필터입니다. 거래대금 증가,
    20일 고점 근접, 5일/20일 상대강도, 과열 추격 회피를 함께 봅니다.
    """

    def __init__(self) -> None:
        self.min_volume_ratio = float(os.environ.get("ISSUE_ROTATION_MIN_VOLUME_RATIO", "1.8"))
        self.max_volume_ratio = float(os.environ.get("ISSUE_ROTATION_MAX_VOLUME_RATIO", "8.0"))
        self.min_value_krw = float(os.environ.get("ISSUE_ROTATION_MIN_VALUE_KRW", "3000000000"))
        self.max_value_krw = float(os.environ.get("ISSUE_ROTATION_MAX_VALUE_KRW", "1500000000000"))
        self.min_return_5d = float(os.environ.get("ISSUE_ROTATION_MIN_RETURN_5D", "3.0"))
        self.min_return_20d = float(os.environ.get("ISSUE_ROTATION_MIN_RETURN_20D", "5.0"))
        self.max_return_3d = float(os.environ.get("ISSUE_ROTATION_MAX_RETURN_3D", "25.0"))
        self.max_sma20_extension = float(os.environ.get("ISSUE_ROTATION_MAX_SMA20_EXTENSION", "14.0"))
        self.max_high20_drawdown = float(os.environ.get("ISSUE_ROTATION_MAX_HIGH20_DRAWDOWN", "8.0"))
        self.max_one_day_return = float(os.environ.get("ISSUE_ROTATION_MAX_ONE_DAY_RETURN", "18.0"))
        self.min_avg_abs_return_10d = float(os.environ.get("ISSUE_ROTATION_MIN_AVG_ABS_RETURN_10D", "1.5"))
        self.max_avg_abs_return_10d = float(os.environ.get("ISSUE_ROTATION_MAX_AVG_ABS_RETURN_10D", "8.0"))

    def calculate_score(self, prices: list[float], indicators: dict) -> float:
        reasons: list[str] = []
        indicators["custom_reasons"] = reasons

        closes = [float(p) for p in prices if p and float(p) > 0]
        highs = [float(v) for v in indicators.get("highs", []) if v and float(v) > 0]
        volumes = [float(v) for v in indicators.get("volumes", []) if v is not None and float(v) >= 0]
        if len(closes) < 60 or len(highs) < 21 or len(volumes) < 21:
            reasons.append("이슈 순환 판단 데이터 부족(종가 60일, 고가/거래량 21일 필요)")
            return 0.0

        current = closes[-1]
        prev = closes[-2]
        sma20 = float(indicators.get("sma20") or _sma(closes, 20))
        sma60 = float(indicators.get("sma60") or _sma(closes, 60))
        high20 = max(highs[-21:-1])
        avg_vol20 = mean(volumes[-21:-1])
        volume_ratio = volumes[-1] / avg_vol20 if avg_vol20 > 0 else 0.0
        trade_value = current * volumes[-1]

        ret_1d = _return_pct(current, prev)
        ret_3d = _return_pct(current, closes[-4])
        ret_5d = _return_pct(current, closes[-6])
        ret_20d = _return_pct(current, closes[-21])
        high20_drawdown = _return_pct(current, high20)
        sma20_extension = _return_pct(current, sma20)
        avg_abs_return_10d = mean(abs(_return_pct(closes[i], closes[i - 1])) for i in range(len(closes) - 10, len(closes)))
        rsi = float(indicators.get("rsi") or 50.0)
        macd_hist = float(indicators.get("macd_hist") or 0.0)

        score = 0.0

        if current >= high20 * 0.97:
            score += 1.0
            reasons.append(f"20일 고점권 유지({high20_drawdown:.1f}%)")
        elif current >= sma20 and ret_5d >= self.min_return_5d:
            score += 0.5
            reasons.append("고점 돌파 전 20일선 위 눌림")
        else:
            reasons.append(f"고점권/20일선 모멘텀 미흡({high20_drawdown:.1f}%)")

        if self.min_volume_ratio <= volume_ratio <= self.max_volume_ratio:
            score += 1.0
            reasons.append(f"거래량 순환 유입({volume_ratio:.1f}x)")
        elif volume_ratio > self.max_volume_ratio:
            score += 0.3
            reasons.append(f"거래량 과열 주의({volume_ratio:.1f}x)")
        else:
            reasons.append(f"거래량 부족({volume_ratio:.1f}x)")

        if self.min_value_krw <= trade_value <= self.max_value_krw:
            score += 0.8
            reasons.append(f"이슈주 유동성 충족({trade_value:,.0f}원)")
        else:
            reasons.append(f"거래대금 범위 이탈({trade_value:,.0f}원)")

        if ret_5d >= self.min_return_5d and ret_20d >= self.min_return_20d:
            score += 1.0
            reasons.append(f"5일/20일 상대강도 양호({ret_5d:.1f}%/{ret_20d:.1f}%)")
        else:
            reasons.append(f"상대강도 부족({ret_5d:.1f}%/{ret_20d:.1f}%)")

        if sma20 > 0 and sma60 > 0 and current > sma20 and sma20 >= sma60 * 0.98:
            score += 0.6
            reasons.append("단기 추세가 중기 추세 위")

        if macd_hist > 0 and 45.0 <= rsi <= 75.0:
            score += 0.6
            reasons.append(f"MACD 양수, RSI 과열 전({rsi:.0f})")
        elif rsi > 82.0:
            reasons.append(f"RSI 과열({rsi:.0f})")

        overheated = (
            ret_1d > self.max_one_day_return
            or ret_3d > self.max_return_3d
            or sma20_extension > self.max_sma20_extension
            or high20_drawdown < -self.max_high20_drawdown
            or not (self.min_avg_abs_return_10d <= avg_abs_return_10d <= self.max_avg_abs_return_10d)
        )
        if overheated:
            score = min(score, 2.0)
            reasons.append(
                "과열/붕괴 필터 작동"
                f"(1일 {ret_1d:.1f}%, 3일 {ret_3d:.1f}%, 20일선 이격 {sma20_extension:.1f}%,"
                f" 변동성 {avg_abs_return_10d:.1f}%)"
            )
        else:
            score += 0.5
            reasons.append("과열 추격 위험 허용 범위")

        indicators["issue_sector_rotation"] = {
            "volume_ratio": round(volume_ratio, 3),
            "trade_value": round(trade_value, 2),
            "return_1d": round(ret_1d, 3),
            "return_3d": round(ret_3d, 3),
            "return_5d": round(ret_5d, 3),
            "return_20d": round(ret_20d, 3),
            "high20_drawdown": round(high20_drawdown, 3),
            "sma20_extension": round(sma20_extension, 3),
            "avg_abs_return_10d": round(avg_abs_return_10d, 3),
        }
        return round(max(0.0, min(5.0, score)), 3)


def _sma(values: list[float], period: int) -> float:
    if len(values) < period:
        return values[-1] if values else 0.0
    return mean(values[-period:])


def _return_pct(current: float, base: float) -> float:
    if base <= 0:
        return 0.0
    return (current / base - 1.0) * 100.0
