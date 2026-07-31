from __future__ import annotations

import math
from typing import Any

import yfinance as yf

from src.mistock.config import config
from src.strategy.indicators import calc_bollinger, calc_macd, calc_rsi, calc_sma
from src.strategy.technical_signals import first_wave_pullback, moving_average_cross, trade_value_surge

def fetch_wikipedia_universe() -> list[str]:
    import pandas as pd
    import requests
    import io
    from src.utils.logger import logger

    symbols = []

    # 1단계: Nasdaq-100 크롤링
    try:
        url = "https://en.wikipedia.org/wiki/Nasdaq-100"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            tables = pd.read_html(io.StringIO(resp.text))
            for table in tables:
                cols = [str(c).lower().strip() for c in table.columns]
                target_col = None
                for i, col in enumerate(cols):
                    if "symbol" in col or "ticker" in col:
                        target_col = table.columns[i]
                        break
                if target_col is not None:
                    tickers = table[target_col].dropna().tolist()
                    if len(tickers) >= 80:
                        symbols.extend([str(t).strip().upper() for t in tickers])
                        break
    except Exception as e:
        logger.warning(f"Failed to fetch dynamic NASDAQ-100 from wikipedia: {e}")

    # 2단계: S&P 500 크롤링 (폴백 및 확장)
    if len(symbols) < 50:
        try:
            url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code == 200:
                tables = pd.read_html(io.StringIO(resp.text))
                for table in tables:
                    cols = [str(c).lower().strip() for c in table.columns]
                    target_col = None
                    for i, col in enumerate(cols):
                        if "symbol" in col or "ticker" in col:
                            target_col = table.columns[i]
                            break
                    if target_col is not None:
                        tickers = table[target_col].dropna().tolist()
                        if len(tickers) >= 400:
                            symbols.extend([str(t).strip().upper() for t in tickers[:120]])
                            break
        except Exception as e:
            logger.warning(f"Failed to fetch dynamic S&P 500 from wikipedia: {e}")

    # 중복 제거 및 정규화
    unique_symbols = []
    seen = set()
    for s in symbols:
        norm = normalize_symbol(s)
        if norm and norm not in seen:
            seen.add(norm)
            unique_symbols.append(norm)

    return unique_symbols


def build_scan_universe(api: Any = None) -> list[str]:
    from src.utils.logger import logger
    from src.strategy.condition_monitor import get_fresh_condition_symbols

    monitored = get_fresh_condition_symbols("US")
    if monitored:
        logger.info(f"[MISTOCK] 장중 조건 감시 {len(monitored)}종목 사용")
        return monitored

    # 1순위: KIS API가 제공되면 해외주식 거래대금 상위 종목을 동적으로 가져온다.
    if api is not None:
        try:
            nas_symbols = api.get_overseas_volume_rank(excd="NAS", cnt=50)
            nys_symbols = api.get_overseas_volume_rank(excd="NYS", cnt=50)
            combined = list(dict.fromkeys(nas_symbols + nys_symbols))
            if len(combined) >= 20:
                logger.info(f"[MISTOCK] KIS API 해외 거래대금 상위 {len(combined)}종목 동적 수집 완료")
                return combined
        except Exception as exc:
            logger.warning(f"[MISTOCK] KIS 해외 순위 API 조회 실패: {exc}")

    # 2순위: Online Wikipedia 크롤링
    wiki_symbols = fetch_wikipedia_universe()
    if len(wiki_symbols) >= 30:
        logger.info(f"[MISTOCK] Wikipedia Nasdaq-100 / S&P500 {len(wiki_symbols)}종목 동적 크롤링 완료")
        return wiki_symbols

    # 3순위: 하드코딩 정적 풀 폴백
    logger.info(f"[MISTOCK] 동적 수집 실패 -> config.universe_list 정적 풀 {len(config.universe_list)}종목으로 폴백")
    return list(config.universe_list)


NASDAQ_UNIVERSE = list(config.universe_list)

NASDAQ_NAMES = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "NVIDIA",
    "AMZN": "Amazon",
    "META": "Meta Platforms",
    "GOOGL": "Alphabet Class A",
    "GOOG": "Alphabet Class C",
    "TSLA": "Tesla",
    "AVGO": "Broadcom",
    "COST": "Costco",
    "NFLX": "Netflix",
    "AMD": "Advanced Micro Devices",
    "PEP": "PepsiCo",
    "ADBE": "Adobe",
    "CSCO": "Cisco",
    "TMUS": "T-Mobile US",
    "INTU": "Intuit",
    "QCOM": "Qualcomm",
    "AMAT": "Applied Materials",
    "TXN": "Texas Instruments",
    "ISRG": "Intuitive Surgical",
    "AMGN": "Amgen",
    "HON": "Honeywell International",
    "BKNG": "Booking Holdings",
    "VRTX": "Vertex Pharmaceuticals",
    "SBUX": "Starbucks",
    "ADP": "Automatic Data Processing",
    "PANW": "Palo Alto Networks",
    "MU": "Micron Technology",
    "LRCX": "Lam Research",
    "GILD": "Gilead Sciences",
    "MDLZ": "Mondelez International",
    "ADI": "Analog Devices",
    "KLAC": "KLA Corporation",
    "MELI": "MercadoLibre",
    "REGN": "Regeneron Pharmaceuticals",
    "CRWD": "CrowdStrike Holdings",
    "PYPL": "PayPal Holdings",
    "CDNS": "Cadence Design Systems",
    "SNPS": "Synopsys",
    "MAR": "Marriott International",
    "CSX": "CSX Corporation",
    "ORLY": "O'Reilly Automotive",
    "ABNB": "Airbnb",
    "FTNT": "Fortinet",
    "NXPI": "NXP Semiconductors",
    "MRVL": "Marvell Technology",
    "ROP": "Roper Technologies",
    "PCAR": "PACCAR",
    "ADSK": "Autodesk",
    "CHTR": "Charter Communications",
    "WDAY": "Workday",
    "MNST": "Monster Beverage",
    "KDP": "Keurig Dr Pepper",
    "PAYX": "Paychex",
    "AEP": "American Electric Power",
    "TEAM": "Atlassian",
    "ROST": "Ross Stores",
    "KHC": "Kraft Heinz",
    "FAST": "Fastenal",
    "ASML": "ASML Holding",
    "AZN": "AstraZeneca",
    "NVO": "Novo Nordisk",
    "PDD": "PDD Holdings",
    "MCHP": "Microchip Technology",
    "CTAS": "Cintas",
    "IDXX": "IDEXX Laboratories",
    "CPRT": "Copart",
    "ODFL": "Old Dominion Freight Line",
    "VRSK": "Verisk Analytics",
    "CSGP": "CoStar Group",
    "LULU": "Lululemon Athletica",
    "EXC": "Exelon",
    "XEL": "Xcel Energy",
    "BKR": "Baker Hughes",
    "GEHC": "GE HealthCare Technologies",
    "MCO": "Moody's Corporation",
    "ALGN": "Align Technology",
    "DDOG": "Datadog",
    "DXCM": "DexCom",
    "EA": "Electronic Arts",
    "FANG": "Diamondback Energy",
    "ILMN": "Illumina",
    "MRNA": "Moderna",
    "VRSN": "VeriSign",
    "ZS": "Zscaler",
    "CTSH": "Cognizant Technology Solutions",
    "CDW": "CDW Corporation",
    "FITB": "Fifth Third Bancorp",
    "HBAN": "Huntington Bancshares",
    "JCI": "Johnson Controls International",
    "KEYS": "Keysight Technologies",
    "NVR": "NVR, Inc.",
    "PTC": "PTC Inc.",
    "VFC": "VF Corporation",
    "WDC": "Western Digital",
    "WYNN": "Wynn Resorts",
    "ZBRA": "Zebra Technologies",
    "EBAY": "eBay",
}


def normalize_symbol(symbol: str) -> str:
    raw = str(symbol or "").upper().strip()
    index_prefix = "^" if raw.startswith("^") else ""
    normalized = "".join(ch for ch in raw.lstrip("^") if ch.isalnum() or ch in {".", "-"})
    return index_prefix + normalized.replace("-", ".")


def symbol_name(symbol: str) -> str:
    symbol = normalize_symbol(symbol)
    return NASDAQ_NAMES.get(symbol, symbol)


def _macd_rsi_momentum_profile(
    prices: list[float],
    highs: list[float] | None = None,
    volumes: list[float] | None = None,
) -> dict[str, Any]:
    from src.strategy.indicators import calc_rsi_divergence

    highs = highs or prices
    volumes = volumes or []
    current = prices[-1] if prices else 0.0
    rsi14 = calc_rsi(prices, 14)
    prev_rsi = calc_rsi(prices[:-1], 14) if len(prices) >= 16 else rsi14
    sma20 = calc_sma(prices, 20)
    sma60 = calc_sma(prices, 60)
    macd = calc_macd(prices)
    ma_cross = moving_average_cross(prices)
    value_surge = trade_value_surge(prices, volumes, minimum_ratio=config.trade_value_surge_ratio)
    wave_pullback = first_wave_pullback(
        prices,
        volumes,
        minimum_wave_pct=config.first_wave_min_pct,
        minimum_pullback_pct=config.first_wave_pullback_min_pct,
        maximum_pullback_pct=config.first_wave_pullback_max_pct,
    )
    prev_macd = calc_macd(prices[:-1]) if len(prices) >= 36 else {"hist": 0.0}
    hist = float(macd.get("hist", 0.0) or 0.0)
    prev_hist = float(prev_macd.get("hist", 0.0) or 0.0)

    entry_min = float(config.indicator_rsi_entry_min)
    entry_max = float(config.indicator_rsi_entry_max)
    vol_ratio = float(config.indicator_volume_ratio)

    score = 0
    reasons: list[str] = []

    # --- 거래량 확인 (여러 신호에서 공유) ---
    volume_confirmed = False
    vol_avg = 0.0
    if len(volumes) >= 20:
        vol_avg = sum(volumes[-20:]) / 20
        if vol_avg > 0 and volumes[-1] > vol_avg * vol_ratio:
            volume_confirmed = True

    # --- MACD 기본 신호 ---
    if macd["bull_cross"]:
        score += 2
        reasons.append("MACD bullish cross")
    elif hist > 0:
        score += 1
        reasons.append("MACD positive")

    # --- Momentum Scope: histogram 음수→반전 조기 신호 ---
    hist_turn_up = prev_hist < 0 and hist > prev_hist
    if hist_turn_up:
        if volume_confirmed:
            score += 2
            reasons.append("momentum_scope: hist turn-up + volume")
        else:
            score += 1
            reasons.append("momentum_scope: hist turn-up")
    elif hist > prev_hist and hist > 0:
        score += 1
        reasons.append("MACD histogram rising")

    # --- RSI 진입 조건 ---
    if prev_rsi < entry_min <= rsi14:
        score += 2
        reasons.append(f"RSI 50 cross {prev_rsi:.0f}->{rsi14:.0f}")
    elif entry_min <= rsi14 < entry_max:
        score += 1
        reasons.append(f"RSI momentum zone {rsi14:.0f}")

    # --- 추세 필터 ---
    if len(prices) >= 60 and current > sma60:
        score += 1
        reasons.append("above SMA60 trend")
    if len(prices) >= 20 and current > sma20:
        score += 1
        reasons.append("above SMA20")
    if ma_cross["golden_cross"]:
        score += 2
        reasons.append("SMA20/SMA60 golden cross")
    elif ma_cross["dead_cross"]:
        score -= 2
        reasons.append("SMA20/SMA60 dead cross")

    # --- 거래량 단독 확인 (hist_turn_up 아닐 때) ---
    if volume_confirmed and not hist_turn_up and vol_avg > 0:
        score += 1
        reasons.append(f"volume confirmation {volumes[-1] / vol_avg:.1f}x")
    if value_surge["matched"]:
        score += 2
        reasons.append(f"trade value surge {value_surge['ratio']:.1f}x")
    if wave_pullback["matched"]:
        score += 3
        reasons.append(
            f"first wave pullback wave={wave_pullback['wave_pct']:.1f}% "
            f"pullback={wave_pullback['pullback_pct']:.1f}%"
        )

    # --- RSI 과열 패널티 ---
    overheated_reason = f"RSI overheated {rsi14:.0f}"
    if rsi14 >= entry_max and not macd["bull_cross"]:
        score -= 2
        reasons.append(overheated_reason)

    # --- 두 번째 매매법: RSI 하락 다이버전스 + MACD 재골든크로스 ---
    # 첫 번째 매매법보다 신뢰도가 높으므로 +3점 (과열 패널티 취소)
    divergence_reentry = False
    if len(prices) >= 54 and macd["bull_cross"]:
        div = calc_rsi_divergence(prices, period=40)
        if div["bearish"]:
            score += 3
            reasons.append(
                f"RSI bearish divergence + MACD reentry "
                f"(P:{div['price_high1']:.1f}->{div['price_high2']:.1f}, "
                f"RSI:{div['rsi_high1']:.0f}->{div['rsi_high2']:.0f})"
            )
            divergence_reentry = True
            # 과열 패널티 취소: 1차 상승 후 RSI 높은 것은 자연스러운 현상
            if overheated_reason in reasons:
                reasons.remove(overheated_reason)
                score += 2  # 패널티 복원

    return {
        "score": max(0, score),
        "reasons": reasons or ["no indicator signal"],
        "rsi": rsi14,
        "rsi2": calc_rsi(prices, 2),
        "macd_hist": hist,
        "macd_bull_cross": bool(macd.get("bull_cross")),
        "macd_bear_cross": bool(macd.get("bear_cross")),
        "sma_golden_cross": ma_cross["golden_cross"],
        "sma_dead_cross": ma_cross["dead_cross"],
        "trade_value_surge": value_surge,
        "first_wave_pullback": wave_pullback,
        "sma20": sma20,
        "sma60": sma60,
        "price": current,
        "divergence_reentry": divergence_reentry,
        "strategy_model": "macd_rsi_momentum",
    }


def strategy_profile(
    prices: list[float],
    highs: list[float] | None = None,
    volumes: list[float] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    active_model = (model or config.strategy_model or "default").strip().lower()
    if active_model == "macd_rsi_momentum":
        return _macd_rsi_momentum_profile(prices, highs, volumes)

    highs = highs or prices
    volumes = volumes or []
    current = prices[-1] if prices else 0.0
    prev = prices[-2] if len(prices) >= 2 else current
    rsi14 = calc_rsi(prices, 14)
    rsi2 = calc_rsi(prices, 2)
    sma20 = calc_sma(prices, 20)
    sma60 = calc_sma(prices, 60)
    sma120 = calc_sma(prices, 120)
    bb_lo, _bb_mid, _bb_hi = calc_bollinger(prices, 20)
    macd = calc_macd(prices)
    ma_cross = moving_average_cross(prices)
    value_surge = trade_value_surge(prices, volumes, minimum_ratio=config.trade_value_surge_ratio)
    wave_pullback = first_wave_pullback(
        prices,
        volumes,
        minimum_wave_pct=config.first_wave_min_pct,
        minimum_pullback_pct=config.first_wave_pullback_min_pct,
        maximum_pullback_pct=config.first_wave_pullback_max_pct,
    )
    score = 0
    reasons: list[str] = []

    if len(prices) >= 16:
        prev_rsi = calc_rsi(prices[:-1], 14)
        if prev_rsi < config.rsi_buy <= rsi14:
            score += 2
            reasons.append(f"RSI recovery {prev_rsi:.0f}->{rsi14:.0f}")
        elif 35 < rsi14 < 55:
            score += 1
            reasons.append(f"NASDAQ pullback RSI {rsi14:.0f}")

    if macd["bull_cross"]:
        score += 2
        reasons.append("MACD bullish cross")
    elif macd["hist"] > 0:
        score += 1
        reasons.append("MACD positive")

    if len(prices) >= 21:
        prev_lo, _prev_mid, _prev_hi = calc_bollinger(prices[:-1], 20)
        if prev < prev_lo and current >= bb_lo:
            score += 2
            reasons.append("Bollinger rebound")
        elif current <= bb_lo:
            score += 1
            reasons.append("near lower band")

    if len(prices) >= 60 and current > sma60 and rsi2 <= 20:
        score += 2
        reasons.append(f"trend pullback RSI2={rsi2:.0f}")
    elif len(prices) >= 120 and current > sma120 and rsi2 <= 25:
        score += 1
        reasons.append(f"long trend pullback RSI2={rsi2:.0f}")

    if len(highs) >= 21 and len(volumes) >= 20:
        high20 = max(highs[-21:-1])
        vol_avg = sum(volumes[-20:]) / 20
        if current > high20 and volumes[-1] > vol_avg * 1.4:
            score += 2
            reasons.append("20-day breakout with volume")
        elif volumes[-1] > vol_avg * 1.5:
            score += 1
            reasons.append("volume spike")

    if ma_cross["golden_cross"]:
        score += 2
        reasons.append("SMA20/SMA60 golden cross")
    elif sma20 > sma60 > 0:
        score += 1
        reasons.append("SMA20>SMA60")
    elif ma_cross["dead_cross"]:
        score -= 2
        reasons.append("SMA20/SMA60 dead cross")
    if value_surge["matched"]:
        score += 2
        reasons.append(f"trade value surge {value_surge['ratio']:.1f}x")
    if wave_pullback["matched"]:
        score += 3
        reasons.append(
            f"first wave pullback wave={wave_pullback['wave_pct']:.1f}% "
            f"pullback={wave_pullback['pullback_pct']:.1f}%"
        )

    return {
        "score": score,
        "reasons": reasons or ["no signal"],
        "rsi": rsi14,
        "rsi2": rsi2,
        "macd_hist": float(macd.get("hist", 0.0) or 0.0),
        "sma20": sma20,
        "sma60": sma60,
        "sma_golden_cross": ma_cross["golden_cross"],
        "sma_dead_cross": ma_cross["dead_cross"],
        "trade_value_surge": value_surge,
        "first_wave_pullback": wave_pullback,
        "price": current,
        "strategy_model": "default",
    }


def fetch_history(symbol: str, period: str = "6mo") -> dict[str, list[float]]:
    from src.online_access import require_online_access

    require_online_access("Mistock market-data download")
    # Yahoo Finance uses '-' for share classes (BRK-B/BF-B), while KIS uses '.'.
    yahoo_symbol = normalize_symbol(symbol).replace(".", "-")
    data = yf.download(
        yahoo_symbol,
        period=period,
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=False,
        timeout=config.yfinance_timeout_seconds,
    )
    if data is None or data.empty:
        return {"close": [], "high": [], "volume": []}
    close = data["Close"]
    high = data["High"]
    volume = data["Volume"]
    if hasattr(close, "iloc") and len(getattr(close, "shape", [])) > 1:
        close = close.iloc[:, 0]
        high = high.iloc[:, 0]
        volume = volume.iloc[:, 0]
    return {
        "close": [float(v) for v in close.dropna().tolist() if math.isfinite(float(v))],
        "high": [float(v) for v in high.dropna().tolist() if math.isfinite(float(v))],
        "volume": [float(v) for v in volume.dropna().tolist() if math.isfinite(float(v))],
    }


def quote(symbol: str) -> dict[str, float]:
    hist = fetch_history(symbol, period="5d")
    price = hist["close"][-1] if hist["close"] else 0.0
    return {"current": price, "ask1": price, "bid1": price}
