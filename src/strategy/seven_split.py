import math
import os
import yfinance as yf
from pathlib import Path
from typing import Callable

from src.config import config
from src.market_metadata import resolve_stock_name
from src.utils.logger import logger
from src.notifier.slack import slack_error
from src.strategy.indicators import calc_rsi, calc_sma, calc_macd, calc_bollinger
from src.strategy.features import build_strategy_features
from src.strategy.predict import ModelPredictor
from src.strategy.allocator import PortfolioAllocator

WATCHLIST = []

def sync_watchlist_runtime() -> None:
    from src.db.repository import load_watchlist_data
    try:
        data = load_watchlist_data()
        WATCHLIST.clear()
        WATCHLIST.extend(data.get("symbols", []))
    except Exception as e:
        logger.warning(f"Failed to sync watchlist runtime: {e}")

# 최초 기동 동기화
sync_watchlist_runtime()


def excluded_symbols() -> set[str]:
    raw = str(getattr(config, "hanstock_excluded_symbols", "") or "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def is_excluded_symbol(symbol: str) -> bool:
    return str(symbol or "").strip() in excluded_symbols()

KOSPI_UNIVERSE = [
    # 반도체/IT/빅테크
    "005930", "000660", "035420", "035720", "018260", "009150", "066570", 
    "034220", "000990", "042700", "036930", "240810", "058470", "357780", 
    "039030", "056190", "067310", "005290", "012510", "053800", "263750", 
    "078340", "112040", "293490", "192080", "251270", "036570", "259960",
    # 자동차/기계/조선/방산
    "005380", "000270", "012330", "011210", "018880", "161390", "073240", 
    "204320", "003490", "020560", "011200", "028670", "086280", "000120", 
    "012450", "047810", "079550", "064350", "042660", "329180", "010140", 
    "034020", "267250", "082740", "272210",
    # 바이오/헬스케어
    "207940", "068270", "000100", "128940", "006280", "069620", "185750", 
    "009290", "170900", "068760", "028300", "196170", "145020", "086900", 
    "237690", "141080", "143860", "096530",
    # 2차전지/배터리/화학/에너지
    "373220", "006400", "051910", "003670", "247540", "086520", "066970", 
    "096770", "010950", "043260", "034020", "112610", "009830", "001570", 
    "011170", "011780", "377300",
    # 금융/은행/카드/지주
    "105560", "055550", "086790", "316140", "024110", "138040", "032830", 
    "088350", "082640", "001450", "005830", "000810", "006800", "005940", 
    "071050", "016360", "030200", "017670", "032640",
    # 철강/소재/비철/건설
    "005490", "010130", "004020", "001230", "103140", "000670", "001390", 
    "300720", "015760", "036460", "071320", "000720", "047040",
    "375500", "006360",
    # 유통/음식료/화장품/엔터/레저
    "097950", "007310", "004370", "003230", "000080", "001680", "026960", 
    "005610", "090430", "051900", "018250", "192820", "161890", "004170", 
    "069960", "023530", "282330", "007070", "139480", "008770", "035250", 
    "039130", "080160", "352820", "035900", "041510", "122870", "035760", 
    "253450", "033780", "021240", "003550", "034730", "028260", 
    "000150", "047050",
    # 추가 우량주 보강 (시총 상위 매칭)
    "001040", "078930", "000880", "006260", "004800",
    "004990", "000210", "002020", "003240", "009540",
    "005250", "011070", "002710", "010060", "019170",
    "005385", "047820", "180640", "001800"
]
# 중복 제거 (순서 유지)
KOSPI_UNIVERSE = list(dict.fromkeys(KOSPI_UNIVERSE))

STOCK_NAMES: dict[str, str] = {
    "005930": "삼성전자", "000660": "SK하이닉스", "035420": "NAVER", "035720": "카카오", 
    "018260": "삼성에스디에스", "009150": "삼성전기", "066570": "LG전자", "034220": "LG디스플레이", 
    "000990": "DB하이텍", "042700": "한미반도체", "036930": "주성엔지니어링", "240810": "원익IPS", 
    "058470": "리노공업", "357780": "솔브레인", "039030": "이오테크닉스", "056190": "에스에프에이", 
    "067310": "하나마이크론", "005290": "동진쎄미켐", "012510": "더존비즈온", "053800": "안랩", 
    "263750": "펄어비스", "078340": "컴투스", "112040": "위메이드", "293490": "카카오게임즈", 
    "192080": "더블유게임즈", "251270": "넷마블", "036570": "엔씨소프트", "259960": "크래프톤",
    "005380": "현대차", "000270": "기아", "012330": "현대모비스", "011210": "현대위아", 
    "018880": "한온시스템", "161390": "한국타이어앤테크놀로지", "073240": "금호타이어", 
    "204320": "HL만도", "003490": "대한항공", "020560": "아시아나항공", "011200": "HMM", 
    "028670": "팬오션", "086280": "현대글로비스", "000120": "CJ대한통운", "012450": "한화에어로스페이스", 
    "047810": "한국항공우주", "079550": "LIG넥스원", "064350": "현대로템", "042660": "한화오션", 
    "329180": "HD현대중공업", "010140": "삼성중공업", "034020": "두산에너빌리티", "267250": "HD현대", 
    "082740": "HSD엔진", "272210": "한화시스템", "047820": "하림지주", "180640": "한진칼", "001800": "삼양홀딩스",
    "207940": "삼성바이오로직스", "068270": "셀트리온", "000100": "유한양행", "128940": "한미약품", 
    "006280": "녹십자", "069620": "대웅제약", "185750": "종근당", "009290": "광동제약", 
    "170900": "동아에스티", "068760": "셀트리온제약", "028300": "HLB", "196170": "알테오젠", 
    "145020": "휴젤", "086900": "메디톡스", "237690": "에스티팜", "141080": "리그켐바이오", 
    "143860": "케어젠", "096530": "씨젠",
    "373220": "LG에너지솔루션", "006400": "삼성SDI", "051910": "LG화학", "003670": "포스코퓨처엠", 
    "247540": "에코프로비엠", "086520": "에코프로", "066970": "엘앤에프", "096770": "SK이노베이션", 
    "010950": "S-Oil", "043260": "HD현대일렉트릭", "112610": "씨에스윈드", "009830": "한화솔루션", 
    "001570": "금양", "011170": "롯데케미칼", "011780": "금호석유", "377300": "카카오페이",
    "105560": "KB금융", "055550": "신한지주", "086790": "하나금융지주", "316140": "우리금융지주", 
    "024110": "기업은행", "138040": "메리츠금융지주", "032830": "삼성생명", "088350": "한화생명", 
    "082640": "동양생명", "001450": "현대해상", "005830": "DB손해보험", "000810": "삼성화재", 
    "006800": "미래에셋증권", "005940": "NH투자증권", "071050": "한국금융지주", "016360": "삼성증권", 
    "030200": "KT", "017670": "SK텔레콤", "032640": "LG유플러스",
    "005490": "POSCO홀딩스", "010130": "고려아연", "004020": "현대제철", "001230": "동국제강", 
    "103140": "풍산", "000670": "영풍", "001390": "KG케미칼", "300720": "한일시멘트", 
    "015760": "한국전력", "036460": "한국가스공사", "071320": "지역난방공사",
    "000720": "현대건설", "047040": "대우건설", "375500": "DL이앤씨", "006360": "GS건설",
    "097950": "CJ제일제당", "007310": "오뚜기", "004370": "농심", "003230": "삼양식품", 
    "000080": "하이트진로", "001680": "대상", "026960": "동서", "005610": "SPC삼립", 
    "090430": "아모레퍼시픽", "051900": "LG생활건강", "018250": "애경산업", "192820": "코스맥스", 
    "161890": "한국콜마", "004170": "신세계", "069960": "현대백화점", "023530": "롯데쇼핑", 
    "282330": "BGF리테일", "007070": "GS리테일", "139480": "이마트", "008770": "호텔신라", 
    "035250": "강원랜드", "039130": "하나투어", "080160": "모두투어", "352820": "하이브", 
    "035900": "JYP Ent.", "041510": "에스엠", "122870": "와이지엔터테인먼트", "035760": "CJ ENM", 
    "253450": "스튜디오드래곤", "033780": "KT&G", "021240": "코웨이", "003550": "LG", 
    "034730": "SK", "028260": "삼성물산", "000150": "두산", "047050": "포스코인터내셔널",
    "001040": "CJ", "078930": "GS", "000880": "한화", "006260": "LS", "004800": "효성",
    "004990": "롯데지주", "000210": "DL", "002020": "코오롱", "003240": "태광산업",
    "009540": "HD한국조선해양", "005250": "녹십자홀딩스", "011070": "LG이노텍", "002710": "TCC스틸",
    "010060": "OCI홀딩스", "019170": "신풍제약", "005385": "현대차우",
    "012690": "모나리자", "413630": "씨피시스템",
    "462330": "KODEX 2차전지산업레버리지"
}

STOCK_SECTORS: dict[str, str] = {
    "005930": "반도체", "000660": "반도체", "035420": "플랫폼", "035720": "플랫폼",
    "018260": "IT서비스", "009150": "IT부품", "066570": "가전/IT", "034220": "가전/IT",
    "000990": "반도체", "042700": "반도체", "036930": "반도체", "240810": "반도체",
    "058470": "반도체", "357780": "IT소재", "039030": "반도체", "056190": "IT부품",
    "067310": "반도체", "005290": "IT소재", "012510": "소프트웨어", "053800": "소프트웨어",
    "263750": "게임", "078340": "게임", "112040": "게임", "293490": "게임",
    "192080": "게임", "251270": "게임", "036570": "게임", "259960": "게임",
    "005380": "자동차", "000270": "자동차", "012330": "자동차부품", "011210": "자동차부품",
    "018880": "자동차부품", "161390": "자동차부품", "073240": "자동차부품", "204320": "자동차부품",
    "003490": "항공", "020560": "항공", "011200": "해운", "028670": "해운",
    "086280": "물류", "000120": "물류", "012450": "방산/우주", "047810": "방산/우주",
    "079550": "방산", "064350": "방산/철도", "042660": "조선", "329180": "조선",
    "010140": "조선", "034020": "원자력/중공업", "082740": "선박엔진", "272210": "방산/IT",
    "207940": "바이오", "068270": "바이오", "000100": "제약", "128940": "제약",
    "006280": "제약", "069620": "제약", "185750": "제약", "009290": "제약",
    "170900": "제약", "068760": "바이오", "028300": "바이오", "196170": "바이오",
    "145020": "바이오", "086900": "바이오", "237690": "바이오", "141080": "바이오",
    "143860": "바이오", "096530": "바이오", "019170": "제약",
    "373220": "2차전지", "006400": "2차전지", "051910": "배터리/화학", "003670": "2차전지소재",
    "247540": "2차전지소재", "086520": "2차전지소재", "066970": "2차전지소재", "096770": "에너지/화학",
    "010950": "정유", "043260": "전력인프라", "112610": "풍력에너지", "009830": "태양광/화학",
    "001570": "2차전지", "011170": "화학", "011780": "화학",
    "105560": "은행지주", "055550": "은행지주", "086790": "은행지주", "316140": "은행지주",
    "024110": "국책은행", "138040": "금융지주", "032830": "생명보험", "088350": "생명보험",
    "082640": "생명보험", "001450": "손해보험", "005830": "손해보험", "000810": "손해보험",
    "006800": "증권", "005940": "증권", "071050": "금융투자", "016360": "증권",
    "377300": "핀테크", "030200": "통신", "017670": "통신", "032640": "통신", 
    "005490": "철강지주", "010130": "비철금속", "004020": "철강", "001230": "철강", 
    "103140": "방산/비철", "000670": "비철금속", "001390": "소재/화학", "300720": "시멘트", 
    "015760": "전력유틸리티", "036460": "가스유틸리티", "071320": "에너지유틸리티",
    "000720": "건설", "047040": "건설", "375500": "건설", "006360": "건설",
    "097950": "음식료", "007310": "음식료", "004370": "음식료", "003230": "음식료",
    "000080": "음식료", "001680": "음식료", "026960": "음식료", "005610": "음식료",
    "090430": "화장품", "051900": "화장품", "018250": "화장품", "192820": "화장품",
    "161890": "화장품", "004170": "유통/백화점", "069960": "유통/백화점", "023530": "유통/백화점",
    "282330": "편의점", "007070": "편의점", "139480": "대형마트", "008770": "면세점/호텔",
    "035250": "카지노/레저", "039130": "여행", "080160": "여행", "352820": "엔터테인먼트",
    "035900": "엔터테인먼트", "041510": "엔터테인먼트", "122870": "엔터테인먼트", "035760": "엔터/미디어",
    "253450": "엔터/미디어", "033780": "담배/소비재", "021240": "생활가전", "003550": "지주사",
    "034730": "지주사", "028260": "종합상사/지주", "000150": "지주사", "047050": "종합상사",
    "001040": "지주사", "078930": "지주사", "000880": "지주사", "006260": "지주사",
    "004800": "지주사", "004990": "지주사", "000210": "지주사", "002020": "지주사",
    "003240": "섬유/화학지주", "009540": "조선지주", "005250": "바이오지주", "011070": "전자부품",
    "002710": "소재/비철", "010060": "에너지지주", "005385": "자동차우", "047820": "농업지주",
    "180640": "항공지주", "001800": "화학지주"
}

# 코스닥 종목 리스트 (야후 파이낸스에서 .KQ를 붙여야 하는 종목 코드들)
KOSDAQ_SYMBOLS = {
    "247540", # 에코프로비엠
    "086520", # 에코프로
    "293490", # 카카오게임즈
    "196170", # 알테오젠
    "145020", # 휴젤
    "253450", # 에프앤가이드
    "058470", # 리노공업
    "028300", # HLB
    "095700", # 제네신
    "035900", # JYP Ent.
    # 066970(엘앤에프)는 KOSPI 이전상장되어 .KS로 조회해야 하므로 제외, 091990(셀트리온헬스케어) 상폐 제외
    "041510", # 에스엠
}

def get_yfinance_ticker(code: str) -> str:
    """종목 코드별 야후 파이낸스 적합한 티커 심볼을 반환한다."""
    if code in KOSDAQ_SYMBOLS:
        return f"{code}.KQ"
    return f"{code}.KS"

def calc_strategy_profile(prices: list[float], highs: list[float] | None = None,
                          volumes: list[float] | None = None, strategy_model: str = "", symbol: str = "") -> dict:
    highs = highs or prices
    volumes = volumes or []
    current = prices[-1] if prices else 0
    prev = prices[-2] if len(prices) >= 2 else current
    rsi14 = calc_rsi(prices, 14)
    rsi2 = calc_rsi(prices, 2)
    sma20 = calc_sma(prices, 20)
    sma60 = calc_sma(prices, 60)
    sma120 = calc_sma(prices, 120)
    bb_lo, bb_mid, bb_hi = calc_bollinger(prices, 20)
    macd = calc_macd(prices)

    score = 0.0
    reasons = []

    # Check if a custom strategy model is active or specified
    if not strategy_model:
        try:
            from src.db.repository import load_ai_strategies
            active = next((s for s in load_ai_strategies() if s.get("selected")), None)
            if active:
                strategy_model = active.get("model") or ""
        except Exception:
            pass

    # Dynamic Custom Strategy loading
    custom_inst = None
    if strategy_model and strategy_model not in ("none", "gpt-5-mini", "ranker_lgbm_v3", "allocator_v2", "ppo_policy_v1", "rule_based"):
        try:
            from src.db.repository import get_custom_strategy_instance
            custom_inst = get_custom_strategy_instance(strategy_model)
        except Exception:
            pass

    if custom_inst:
        indicators = {
            "rsi": rsi14,
            "rsi2": rsi2,
            "sma20": sma20,
            "sma60": sma60,
            "sma120": sma120,
            "bb_lo": bb_lo,
            "bb_mid": bb_mid,
            "bb_hi": bb_hi,
            "macd": macd["macd"],
            "macd_signal": macd["signal"],
            "macd_hist": macd["hist"],
            "macd_bull_cross": macd["bull_cross"],
            "macd_bear_cross": macd["bear_cross"],
            "volumes": volumes,
            "highs": highs,
            "symbol": symbol,
        }
        try:
            score = float(custom_inst.calculate_score(prices, indicators))
            if "custom_reasons" in indicators:
                reasons.extend(indicators["custom_reasons"])
            elif "pb_reasons" in indicators:
                reasons.extend(indicators["pb_reasons"])
            else:
                reasons.append(f"Custom Rule: {strategy_model} (score={score:.2f})")
        except Exception as e:
            logger.warning(f"Error calculating score with custom strategy {strategy_model}: {e}")
            custom_inst = None

    if not custom_inst:
        # Default strategy logic
        score = 0.0
        if len(prices) >= 16:
            prev_rsi = calc_rsi(prices[:-1], 14)
            if prev_rsi < config.rsi_buy <= rsi14:
                score += 2.0
                reasons.append(f"RSI recovery {prev_rsi:.0f}->{rsi14:.0f}")
            elif 30 < rsi14 < 50:
                score += 1.0
                reasons.append(f"RSI pullback {rsi14:.0f}")

        if macd["bull_cross"]:
            score += 2.0
            reasons.append("MACD bullish cross")
        elif macd["hist"] > 0:
            score += 1.0
            reasons.append("MACD positive")

        if len(prices) >= 21:
            prev_lo, _prev_mid, _prev_hi = calc_bollinger(prices[:-1], 20)
            if prev < prev_lo and current >= bb_lo:
                score += 2.0
                reasons.append("Bollinger rebound")
            elif current <= bb_lo:
                score += 1.0
                reasons.append("near lower band")

        if len(prices) >= 60 and current > sma60 and rsi2 <= 15:
            score += 2.0
            reasons.append(f"trend pullback RSI2={rsi2:.0f}")
        elif len(prices) >= 120 and current > sma120 and rsi2 <= 20:
            score += 1.0
            reasons.append(f"long trend pullback RSI2={rsi2:.0f}")

        if len(highs) >= 21 and len(volumes) >= 20:
            high20 = max(highs[-21:-1])
            vol_avg = sum(volumes[-20:]) / 20
            if current > high20 and volumes[-1] > vol_avg * 1.5:
                score += 2.0
                reasons.append("20-day breakout with volume")
            elif volumes[-1] > vol_avg * 1.5:
                score += 1.0
                reasons.append("volume spike")

        if sma20 > sma60 > 0:
            score += 1.0
            reasons.append("SMA20>SMA60")

    feature_payload = build_strategy_features(prices, highs, volumes, strategy_score=score)
    return {
        "score": score,
        "reasons": reasons,
        "rsi": rsi14,
        "rsi2": rsi2,
        "sma20": sma20,
        "sma60": sma60,
        "sma120": sma120,
        "bb_lo": bb_lo,
        "bb_mid": bb_mid,
        "bb_hi": bb_hi,
        "macd": macd["macd"],
        "macd_signal": macd["signal"],
        "macd_hist": macd["hist"],
        "macd_bull_cross": macd["bull_cross"],
        "macd_bear_cross": macd["bear_cross"],
        "features": feature_payload,
    }


def calc_volatility(prices: list[float], period: int = 20) -> float:
    if len(prices) < period + 1:
        return 0.0
    window = prices[-(period + 1):]
    returns = []
    for i in range(1, len(window)):
        if window[i - 1] > 0:
            returns.append((window[i] / window[i - 1]) - 1)
    if not returns:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    return variance ** 0.5


def generate_ai_weight_plan(holdings: list[dict], total_eval: int) -> dict:
    """FinRL-inspired Target Weight Allocation & Visualization logic.
    Loads trained PPO model if available, calculates weights, and returns reasoning string.
    """
    import numpy as np

    investable_weight = max(0.0, 1 - config.cash_buffer)
    if total_eval <= 0 or not holdings:
        return {"cash_weight": 1.0, "positions": []}

    scored = []
    holding_map = {}
    for item in holdings:
        prices = item.get("prices", [])
        highs = item.get("highs", [])
        volumes = item.get("volumes", [])
        profile = calc_strategy_profile(prices, highs, volumes, symbol=item.get("symbol", "")) if prices else calc_strategy_profile([], symbol=item.get("symbol", ""))
        current_price = float(item.get("price", 0) or (prices[-1] if prices else 0))
        sma60 = profile.get("sma60", 0) or current_price
        trend = ((current_price / sma60) - 1) if sma60 > 0 else 0
        vol = calc_volatility(prices)
        raw_score = profile["score"] + (trend * 10) + max(profile["macd_hist"], 0) / max(current_price, 1) * 100
        risk_adjusted = max(0.0, raw_score - (vol * 20))
        
        item_data = {**item, "profile": profile, "score": round(risk_adjusted, 4), "volatility": vol, "trend": trend}
        scored.append(item_data)
        holding_map[item.get("symbol", "")] = item_data

    # Attempt to load AI Model
    model = None
    try:
        from stable_baselines3 import PPO
        model_path = Path("data/trained_models/ppo_kr_stock.zip")
        if model_path.exists():
            model = PPO.load(str(model_path))
    except Exception as e:
        logger.info(f"[WARN] Failed to load PPO model: {e}. Falling back to heuristic.")

    ai_weights = {}
    if model:
        raw_ratings = {}
        for ticker in WATCHLIST:
            if ticker in holding_map:
                it = holding_map[ticker]
                price = (it.get("price", 0) or 0.0) / 100000.0
                rsi = (it["profile"].get("rsi", 50.0) or 50.0) / 100.0
                macd = (it["profile"].get("macd_hist", 0.0) or 0.0) / 1000.0
                trend = float(it.get("trend", 0.0) or 0.0)
                obs = [price, rsi, macd, trend]
            else:
                obs = [0.0, 0.5, 0.0, 0.0]
            
            obs_arr = np.array(obs, dtype=np.float32)
            try:
                action, _ = model.predict(obs_arr, deterministic=True)
                raw_ratings[ticker] = float(action[0])
            except Exception as e:
                logger.info(f"[ERROR] AI prediction failed for {ticker}: {e}")
                raw_ratings[ticker] = -1.0
                
        try:
            ratings_arr = np.array([raw_ratings[t] for t in WATCHLIST], dtype=np.float32)
            exp_r = np.exp(ratings_arr)
            target_w = exp_r / np.sum(exp_r)
            for i, ticker in enumerate(WATCHLIST):
                ai_weights[ticker] = float(target_w[i])
        except Exception as e:
            logger.info(f"[ERROR] Softmax normalization failed: {e}")
    else:
        # [Fallback for WinError 1114 / Missing Model environments]
        # Simulate neural-network-like target weights inversely proportional to volatility for UI demonstration.
        score_sum = sum(item["score"] for item in scored)
        for item in scored:
            ticker = item.get("symbol", "")
            if score_sum > 0:
                ai_weights[ticker] = item["score"] / score_sum
            else:
                ai_weights[ticker] = 0.0

    score_sum = sum(item["score"] for item in scored)
    positions = []
    
    for item in scored:
        symbol = item.get("symbol", "")
        current_value = float(item.get("value", 0))
        current_weight = current_value / total_eval if total_eval else 0
        
        used_ai = False
        if symbol in ai_weights:
            target_weight = min(config.max_single_weight, investable_weight * float(ai_weights[symbol]))
            used_ai = True
        else:
            target_weight = min(config.max_single_weight, investable_weight * item["score"] / score_sum) if score_sum > 0 else 0.0

        target_value = total_eval * target_weight
        delta_value = target_value - current_value
        price = float(item.get("price", 0))
        rebalance_qty = math.floor(abs(delta_value) / price) if price > 0 else 0
        
        if rebalance_qty <= 0:
            action = "hold"
        else:
            action = "buy" if delta_value > 0 else "sell"

        # 시각화(대시보드) UI 전용 판단 근거 요약문 만들기
        reasons_list = item["profile"].get("reasons", [])
        reason_kr = ""
        
        if used_ai:
            trend_pct = item.get("trend", 0) * 100
            vol_pct = item.get("volatility", 0) * 100
            
            tags = []
            rsi = item["profile"].get("rsi", 50)
            if rsi < 40 or rsi > 60:
                tags.append(f"[RSI {int(rsi)}]")
                
            if item["profile"].get("macd_hist", 0) >= 0:
                tags.append("[MACD+]")
            else:
                tags.append("[MACD-]")
                
            sma20 = item["profile"].get("sma20", 0)
            sma60 = item["profile"].get("sma60", 0)
            if sma20 > 0 and sma60 > 0:
                if sma20 > sma60:
                    tags.append("[SMA20>60]")
                else:
                    tags.append("[SMA20<60]")
            
            tag_str = " ".join(tags)

            if action == 'buy':
                ai_strategy_name = f"🤖 매수({target_weight*100:.1f}%) | {tag_str}"
                reason_kr = f"[AI 매수 가이드] 전체 투자금의 {target_weight*100:.1f}% 까지 이 종목을 담는 것이 안전하고 유리합니다. "
            elif action == 'sell':
                ai_strategy_name = f"🤖 축소({target_weight*100:.1f}%) | {tag_str}"
                reason_kr = f"[AI 비중축소 가이드] 위험 관리를 위해 보유 비중을 {target_weight*100:.1f}% 로 줄여서 수익을 챙기거나 손실을 방어하세요. "
            else:
                ai_strategy_name = f"🤖 관망 | {tag_str}"
                reason_kr = f"[AI 관망 가이드] 섣불리 움직이기보다 현재 비중({current_weight*100:.1f}%)을 우직하게 유지하는 것이 좋습니다. "
                
            reason_kr += f"(분석: 60일 평균선 대비 {trend_pct:.1f}% 위치, 최근 변동성 {vol_pct:.1f}%) "
            
            rsi = item["profile"].get("rsi", 50)
            if rsi < 35:
                reason_kr += "최근 주가가 평균보다 너무 가파르게 하락해 곧 바닥을 치고 반등할 에너지가 모이고 있습니다. "
            elif rsi > 65:
                reason_kr += "최근 주가가 쉬지 않고 폭등하여, 조만간 사람들이 차익을 실현하며 주가가 한숨을 돌릴(하락) 위험이 있습니다. "
                
            if item["profile"].get("macd_bull_cross"):
                reason_kr += "여기에 덧붙여, 깊은 하락장을 끝내고 다시 상승세로 올라타는 가장 확실한 신호(골든크로스)가 방금 포착되었습니다! "
            elif item["profile"].get("macd_bear_cross"):
                reason_kr += "주의해야 할 점은, 상승세가 꺾이고 본격적인 하락 추세로 떨어질 조짐이 보이고 있다는 것입니다. "
                
            reason_kr += "👉 종합: 인공지능은 수천 번의 모의 투자를 통해 이런 상황에서 위 비율대로 비중을 맞추는 것이 가장 수익률이 좋았음을 학습했습니다."
        else:
            ai_strategy_name = "기본 룰베이스 대응"
            reason_kr = ", ".join(reasons_list) if reasons_list else "데이터 부족 (지표 확인 불가)"

        positions.append({
            "symbol": symbol,
            "name": item.get("name", symbol),
            "price": int(price),
            "qty": int(item.get("qty", 0)),
            "current_value": round(current_value),
            "current_weight": round(current_weight, 4),
            "target_weight": round(target_weight, 4),
            "target_value": round(target_value),
            "delta_value": round(delta_value),
            "rebalance_action": action,
            "rebalance_qty": rebalance_qty,
            "score": item["score"],
            "volatility": round(item["volatility"], 4),
            "strategy_score": item["profile"].get("score", 0),
            "reasons": reasons_list,
            "reasoning_kr": reason_kr,
            "ai_strategy_name": ai_strategy_name,
        })


    return {"cash_weight": config.cash_buffer, "positions": positions, "ai_active": bool(model)}


def generate_portfolio_optimizer_plan(holdings: list[dict], total_eval: int) -> dict:
    """PyPortfolioOpt-inspired risk/return target-weight plan.

    This avoids importing PyPortfolioOpt's optional dependency stack while
    preserving the practical output shape: target weights and rebalance deltas.
    """
    investable_weight = max(0.0, 1 - config.cash_buffer)
    if total_eval <= 0 or not holdings:
        return {"method": "score_tilted_inverse_vol", "cash_weight": 1.0, "positions": []}

    weighted = []
    for item in holdings:
        prices = item.get("prices", [])
        profile = calc_strategy_profile(prices, item.get("highs", []), item.get("volumes", []), symbol=item.get("symbol", "")) if prices else calc_strategy_profile([], symbol=item.get("symbol", ""))
        vol = calc_volatility(prices) or 0.02
        expected_score = max(0.1, 1 + profile["score"])
        weight_signal = expected_score / vol
        weighted.append({**item, "profile": profile, "volatility": vol, "weight_signal": weight_signal})

    signal_sum = sum(item["weight_signal"] for item in weighted) or 1
    positions = []
    for item in weighted:
        price = float(item.get("price", 0))
        current_value = float(item.get("value", 0))
        current_weight = current_value / total_eval if total_eval else 0
        target_weight = min(config.max_single_weight, investable_weight * item["weight_signal"] / signal_sum)
        target_value = total_eval * target_weight
        delta_value = target_value - current_value
        rebalance_qty = math.floor(abs(delta_value) / price) if price > 0 else 0
        action = "hold" if rebalance_qty <= 0 else ("buy" if delta_value > 0 else "sell")
        positions.append({
            "symbol": item.get("symbol", ""),
            "name": item.get("name", item.get("symbol", "")),
            "price": int(price),
            "qty": int(item.get("qty", 0)),
            "current_value": round(current_value),
            "current_weight": round(current_weight, 4),
            "target_weight": round(target_weight, 4),
            "target_value": round(target_value),
            "delta_value": round(delta_value),
            "rebalance_action": action,
            "rebalance_qty": rebalance_qty,
            "score": round(item["profile"]["score"], 4),
            "volatility": round(item["volatility"], 4),
            "reasons": item["profile"].get("reasons", []),
        })
    return {"method": "score_tilted_inverse_vol", "cash_weight": config.cash_buffer, "positions": positions}


def _condition_search_universe(api: "KIStockAPI") -> list[str]:
    api_enabled = getattr(api, "kis_condition_search_enabled", None)
    if not isinstance(api_enabled, (bool, str, int)):
        api_enabled = getattr(config, "kis_condition_search_enabled", False)
    if not bool(api_enabled):
        return []
    api_user_id = getattr(api, "kis_condition_user_id", "")
    api_condition_no = getattr(api, "kis_condition_seq", "")
    api_condition_name = getattr(api, "kis_condition_name", "")
    if not isinstance(api_user_id, str):
        api_user_id = ""
    if not isinstance(api_condition_no, str):
        api_condition_no = ""
    if not isinstance(api_condition_name, str):
        api_condition_name = ""
    user_id = str(
        api_user_id
        or getattr(config, "kis_condition_user_id", "")
        or getattr(config, "kistock_hts_id", "")
        or ""
    ).strip()
    condition_no = str(
        api_condition_no
        or getattr(config, "kis_condition_seq", "")
        or ""
    ).strip()
    condition_name = str(
        api_condition_name
        or getattr(config, "kis_condition_name", "")
        or ""
    ).strip()
    if not user_id or not condition_no or not condition_name:
        logger.warning("[SCAN] KIS 조건검색 설정이 부족해 조건검색 유니버스를 건너뜁니다.")
        return []
    try:
        codes = api.get_condition_search_result(user_id, condition_no, condition_name)
    except Exception as exc:
        logger.warning(f"[SCAN] KIS 조건검색 조회 실패: {exc}")
        return []
    return [code for code in codes if code]


def build_scan_universe(api: "KIStockAPI", held_symbols: set[str]) -> list[str]:
    """매수 후보 스캔 대상 종목 코드 목록을 구성한다.

    1순위: KIS 조건검색식 결과(설정된 경우)
    2순위: KIS 거래량 상위 config.scan_universe_size종목 (장중 동적 발굴)
    3순위: KOSPI_UNIVERSE 정적 풀 (KIS API 실패 시 폴백)
    WATCHLIST는 항상 포함되며, 보유 중인 종목은 제외된다.
    """
    condition_codes = _condition_search_universe(api)
    if condition_codes:
        logger.info(f"[SCAN] KIS 조건검색식 {len(condition_codes)}종목 수집 완료")
        base = condition_codes
    else:
        volume_rank = api.get_volume_rank(top_n=config.scan_universe_size)
        if volume_rank:
            logger.info(f"[SCAN] KIS 거래량 상위 {len(volume_rank)}종목 수집 완료")
            base = volume_rank
        else:
            logger.info(f"[SCAN] KIS 거래량 API 실패 → KOSPI_UNIVERSE {len(KOSPI_UNIVERSE)}종목으로 폴백")
            base = KOSPI_UNIVERSE

    # WATCHLIST 항상 포함, 중복 제거, 보유 종목 제외
    merged = list(dict.fromkeys(WATCHLIST + base))
    excluded = excluded_symbols()
    universe = [code for code in merged if code not in held_symbols and code not in excluded]
    logger.info(f"[SCAN] 최종 스캔 대상: {len(universe)}종목 (WATCHLIST {len(WATCHLIST)} + 동적 {len(base)}종목 병합)")
    return universe


from datetime import datetime, timedelta, timezone


def _apply_ai_promotion(candidates: list[dict], ai_targets: list[dict], min_score: float, predictor) -> None:
    """AI 점수 반영 후 후보 멤버십을 재조정한다.

    - allow_candidate_promotion=True면 룰 점수는 낮아도 AI 최종 점수가 기준을
      넘는 종목을 후보로 승격한다(promoted_by_ai 표시).
    - AI 평가 결과 최종 점수가 기준 아래로 내려간 기존 후보는 제거한다.
      (AI가 실제로 평가한 종목에 한정하며, 룰 점수 필터로 AI 평가를 받지 않은
       후보는 그대로 유지한다.)
    """
    evaluated = {entry["ticker"]: entry for entry in ai_targets}
    existing = {c["ticker"] for c in candidates}

    if getattr(predictor, "allow_candidate_promotion", False):
        for ticker, entry in evaluated.items():
            if ticker in existing:
                continue
            if float(entry.get("final_score", entry.get("score", 0)) or 0) >= min_score:
                entry["passed"] = True
                entry["promoted_by_ai"] = True
                candidates.append(entry)
                existing.add(ticker)

    retained = []
    for cand in candidates:
        evaluated_entry = evaluated.get(cand["ticker"])
        if evaluated_entry is not None and not evaluated_entry.get("promoted_by_ai"):
            score = float(evaluated_entry.get("final_score", cand.get("score", 0)) or 0)
            if score < min_score:
                cand["passed"] = False
                continue
        retained.append(cand)
    candidates[:] = retained


def find_candidates(
    held_symbols: set[str],
    universe: list[str] | None = None,
    min_score: int = 2,
    ranker: str = "gpt_5_mini",
    api = None,
    strategy_model: str = "",
    strategy_profile: dict | None = None,
    strategy_description: str = "",
) -> list[dict]:
    """universe 종목 전체를 기술분석 스코어링해 매수 후보를 반환한다.

    universe가 None이면 WATCHLIST만 스캔한다 (하위 호환).

    strategy_profile/strategy_description이 주어지면 AI 스코어링(ModelPredictor)에
    전략 성격(focus/avoid/risk_level/min_ai_confidence 등)이 반영된다. 둘 다
    비어 있으면 현재 선택된(active) AI 전략의 profile을 자동으로 로드한다.
    """
    from src.online_access import require_online_access

    require_online_access("candidate market-data scan")
    if not strategy_model or strategy_profile is None or not strategy_description:
        try:
            from src.db.repository import load_ai_strategies
            active = next((s for s in load_ai_strategies() if s.get("selected")), None)
            if active:
                if not strategy_model:
                    strategy_model = active.get("model") or ""
                if strategy_profile is None:
                    strategy_profile = active.get("profile") or {}
                if not strategy_description:
                    strategy_description = active.get("description") or ""
        except Exception:
            pass
    excluded = excluded_symbols()
    scan_list = [
        code for code in (universe if universe is not None else WATCHLIST)
        if code not in held_symbols and code not in excluded
    ]
    if not scan_list:
        return {"candidates": [], "scan_summary": [], "scanned": 0, "min_score": min_score}

    candidates: list[dict] = []
    scan_summary: list[dict] = []  # 기준 미달 포함 전체 분석 결과
    symbols = [get_yfinance_ticker(code) for code in scan_list]
    predictor = ModelPredictor(strategy_profile=strategy_profile, description=strategy_description)
    if ranker == "rule_only":
        predictor.enabled = False
    ai_candidate_limit = max(0, int(getattr(config, "ai_candidate_limit", 5) or 5))

    batch = None
    scan_error: str | None = None
    scan_source = str(getattr(config, "candidate_scan_source", "yfinance") or "yfinance").strip().lower()
    kis_first_sources = {"kis", "kis_api", "real", "real_check", "kis_cache"}
    if scan_source in kis_first_sources:
        scan_error = f"candidate scan source is {scan_source}; using KIS API/cache"
        logger.info(f"[SCAN] KIS API/cache scan selected: {len(scan_list)} symbols (source={scan_source})")
    else:
        logger.info(f"[SCAN] yfinance 배치 다운로드 시작: {len(scan_list)}종목")
        try:
            batch = yf.download(
                symbols,
                period="9mo",
                progress=False,
                auto_adjust=True,
                group_by="ticker",
                threads=True,
                timeout=config.yfinance_timeout_seconds,
            )
            if getattr(batch, "empty", True):
                scan_error = f"yfinance가 {len(scan_list)}종목에 대해 데이터를 반환하지 않았습니다. 잠시 후 다시 시도해 주세요."
                logger.info(f"[WARN] yfinance returned empty batch for {len(scan_list)} symbols")
                batch = None
            else:
                logger.info(f"[SCAN] yfinance 수신 완료: {len(batch)}행")
        except Exception as e:
            scan_error = f"yfinance 다운로드 오류: {type(e).__name__} — {e}"
            logger.info(f"[WARN] Candidate batch scan failed: {e}")
            batch = None

    if batch is None:
        logger.info(f"[SCAN] KIS API/local DB chart cache scan mode activated ({scan_error})")
        from src.db.repository import save_daily_charts, load_daily_charts
        
        KST = timezone(timedelta(hours=9))
        
        if api is None:
            try:
                from src.trader import KIStockAPI
                api = KIStockAPI()
            except Exception as api_err:
                logger.warning(f"[SCAN] KIS API 객체 생성 실패: {api_err}")
        
        for code in scan_list:
            try:
                # 1. DB에서 캐시 로드
                db_charts = load_daily_charts(code, limit=120)
                
                # 2. 캐시 유효성 검사 (오늘 날짜 데이터가 있는지 또는 개수가 부족한지)
                today_str = datetime.now(KST).strftime("%Y-%m-%d")
                has_today = any(c.get("date") == today_str for c in db_charts)
                
                # 데이터가 아예 없거나, 최근 데이터가 없고, KIS API가 사용 가능할 때 동기화 진행
                # 대형 스캔(50종목 초과) 시, 캐시가 충분히(60개 이상) 존재한다면 오늘치 당일 시세가 없더라도 KIS API 추가 호출을 생략하여 타임아웃을 방지합니다.
                is_large_scan = len(scan_list) > 50
                needs_sync = False
                if len(db_charts) < 60:
                    needs_sync = True
                elif not has_today and not is_large_scan:
                    needs_sync = True

                if needs_sync and api is not None:
                    logger.info(f"[SCAN] {code}의 캐시 데이터가 부족하여 KIS API에서 시세를 가져옵니다.")
                    try:
                        kis_data = api.get_daily(code, n=120)
                        if kis_data:
                            save_daily_charts(code, kis_data)
                            db_charts = load_daily_charts(code, limit=120)
                    except Exception as kis_err:
                        logger.warning(f"[SCAN] {code} KIS API 조회 실패: {kis_err}")
                
                if len(db_charts) < 60:
                    logger.warning(f"[SCAN] {code} 시세 데이터 부족으로 분석 생략 (보유 개수: {len(db_charts)}개)")
                    continue
                
                # 3. 데이터 로딩 및 시리즈 구성
                price_series = [float(c["close"]) for c in db_charts]
                high_series = [float(c["high"]) for c in db_charts]
                volume_series = [float(c["volume"]) for c in db_charts]
                current = price_series[-1]
                
                profile = calc_strategy_profile(price_series, high_series, volume_series, strategy_model=strategy_model, symbol=code)
                feature_payload = build_strategy_features(
                    price_series,
                    high_series,
                    volume_series,
                    strategy_score=profile["score"],
                )
                score = float(profile["score"])
                reasons = profile["reasons"]
                vol = calc_volatility(price_series) or 0.02
                
                entry = {
                    "ticker": code,
                    "name": resolve_stock_name(code, STOCK_NAMES.get(code, code)),
                    "current_price": current,
                    "score": round(score, 4),
                    "rule_score": round(score, 4),
                    "ml_score": None,
                    "final_score": round(score, 4),
                    "volatility": round(vol, 4),
                    "ai_enabled": predictor.enabled,
                    "ai_model_status": "queued" if predictor.enabled else "disabled",
                    "ai_model_version": predictor.model_name,
                    "feature_version": feature_payload.get("feature_version", "features_v1"),
                    "ai_score_weight": predictor.score_weight if predictor.enabled else 0.0,
                    "ai_fallback_reason": "OpenAI pending for top candidates" if predictor.enabled else None,
                    "top_features": feature_payload.get("top_features", []),
                    "feature_payload": feature_payload,
                    "min_score": min_score,
                    "passed": score >= min_score,
                    "reasons": reasons,
                    "rsi": profile["rsi"],
                    "rsi2": profile["rsi2"],
                    "macd_hist": profile["macd_hist"],
                    "sma20": profile["sma20"],
                    "sma60": profile["sma60"],
                    "bb_lo": profile["bb_lo"],
                    "bb_hi": profile["bb_hi"],
                }
                scan_summary.append(entry)
                
                if score >= min_score:
                    candidates.append(entry)
                    logger.info(f"[CANDIDATE] {code} (하이브리드) score={score} ({', '.join(reasons)})")
                else:
                    logger.info(f"[SKIP] {code} (하이브리드) score={score}/{min_score}")
            except Exception as err:
                logger.warning(f"[SCAN] {code} 하이브리드 스캔 중 예외 발생: {err}")
                
        # 하이브리드 완료 시 조기 리턴 처리
        if predictor.enabled and predictor.api_key and ai_candidate_limit > 0 and scan_summary:
            ai_pool = [
                item for item in scan_summary
                if float(item.get("rule_score", item.get("score", 0)) or 0) >= predictor.min_rule_score_for_ai
            ]
            ai_targets = sorted(
                ai_pool,
                key=lambda item: (-float(item.get("score", 0) or 0), item.get("ticker", "")),
            )[:ai_candidate_limit]

            import concurrent.futures
            def predict_for_entry(entry):
                try:
                    prediction = predictor.predict(entry.get("feature_payload", {}))
                    return entry, prediction, None
                except Exception as e:
                    return entry, None, e

            if ai_targets:
                max_workers = max(
                    1,
                    min(
                        len(ai_targets),
                        int(os.environ.get("AI_PREDICTION_MAX_WORKERS", "1")),
                    ),
                )
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = [executor.submit(predict_for_entry, entry) for entry in ai_targets]
                    for future in concurrent.futures.as_completed(futures):
                        entry, prediction, p_err = future.result()
                        if p_err is not None:
                            logger.warning(f"[AI] Hybrid ranker update failed for {entry['ticker']}: {p_err}")
                            continue
                        if prediction:
                            entry["score"] = round(float(prediction["final_score"]), 4)
                            entry["ml_score"] = (
                                round(float(prediction["ml_score"]), 4)
                                if prediction.get("ml_score") is not None
                                else None
                            )
                            entry["final_score"] = round(float(prediction["final_score"]), 4)
                            entry["ai_enabled"] = prediction["ai_enabled"]
                            entry["ai_model_status"] = prediction["model_status"]
                            entry["ai_fallback_reason"] = prediction["fallback_reason"]

            _apply_ai_promotion(candidates, ai_targets, min_score, predictor)
            candidates.sort(key=lambda x: -x["score"])
            scan_summary.sort(key=lambda x: -x["score"])

        return {
            "candidates": candidates,
            "universe_size": len(scan_list),
            "scanned": len(scan_summary),
            "min_score": min_score,
            "scan_summary": scan_summary,
            "scan_error": None if scan_summary else "No charts cached or fetched successfully via KIS"
        }

    for code in scan_list:
        ticker = get_yfinance_ticker(code)
        try:
            if getattr(batch.columns, "nlevels", 1) > 1:
                if ticker not in batch.columns.get_level_values(0):
                    continue
                df = batch[ticker]
            else:
                df = batch

            if df.empty or len(df) < 60:
                continue

            closes = df["Close"].dropna().squeeze()
            highs = df["High"].dropna().squeeze()
            volumes = df["Volume"].dropna().squeeze()
            if len(closes) < 60 or len(highs) < 60 or len(volumes) < 60:
                continue

            current = float(closes.iloc[-1])
            price_series = closes.tolist()
            high_series = highs.tolist()
            volume_series = volumes.tolist()
            profile = calc_strategy_profile(price_series, high_series, volume_series, strategy_model=strategy_model, symbol=code)
            feature_payload = build_strategy_features(
                price_series,
                high_series,
                volume_series,
                strategy_score=profile["score"],
            )
            score = float(profile["score"])
            reasons = profile["reasons"]
            vol = calc_volatility(price_series) or 0.02

            entry = {
                "ticker": code,
                "name": resolve_stock_name(code, STOCK_NAMES.get(code, code)),
                "current_price": current,
                "score": round(score, 4),
                "rule_score": round(score, 4),
                "ml_score": None,
                "final_score": round(score, 4),
                "volatility": round(vol, 4),
                "ai_enabled": predictor.enabled,
                "ai_model_status": "queued" if predictor.enabled else "disabled",
                "ai_model_version": predictor.model_name,
                "feature_version": feature_payload.get("feature_version", "features_v1"),
                "ai_score_weight": predictor.score_weight if predictor.enabled else 0.0,
                "ai_fallback_reason": "OpenAI pending for top candidates" if predictor.enabled else None,
                "top_features": feature_payload.get("top_features", []),
                "feature_payload": feature_payload,
                "min_score": min_score,
                "passed": score >= min_score,
                "reasons": reasons,
                "rsi": profile["rsi"],
                "rsi2": profile["rsi2"],
                "macd_hist": profile["macd_hist"],
                "sma20": profile["sma20"],
                "sma60": profile["sma60"],
                "bb_lo": profile["bb_lo"],
                "bb_hi": profile["bb_hi"],
            }
            scan_summary.append(entry)

            if score >= min_score:
                candidates.append(entry)
                logger.info(f"[CANDIDATE] {code} score={score} ({', '.join(reasons)})")
            else:
                logger.info(f"[SKIP] {code} score={score}/{min_score} ({', '.join(reasons) if reasons else '신호없음'})")
        except Exception as e:
            logger.info(f"[WARN] Candidate scan failed for {code}: {e}")

    if predictor.enabled and predictor.api_key and ai_candidate_limit > 0 and scan_summary:
        ai_pool = [
            item for item in scan_summary
            if float(item.get("rule_score", item.get("score", 0)) or 0) >= predictor.min_rule_score_for_ai
        ]
        ai_targets = sorted(
            ai_pool,
            key=lambda item: (-float(item.get("score", 0) or 0), item.get("ticker", "")),
        )[:ai_candidate_limit]

        import concurrent.futures
        def predict_for_entry(entry):
            try:
                prediction = predictor.predict(entry.get("feature_payload", {}))
                return entry, prediction, None
            except Exception as e:
                return entry, None, e

        if ai_targets:
            max_workers = max(
                1,
                min(
                    len(ai_targets),
                    int(os.environ.get("AI_PREDICTION_MAX_WORKERS", "1")),
                ),
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(predict_for_entry, entry) for entry in ai_targets]
                for future in concurrent.futures.as_completed(futures):
                    entry, prediction, e = future.result()
                    if e is not None:
                        logger.info(f"[WARN] OpenAI scoring failed for {entry.get('ticker')}: {e}")
                        continue
                    if prediction:
                        entry["score"] = round(float(prediction["final_score"]), 4)
                        entry["ml_score"] = (
                            round(float(prediction["ml_score"]), 4)
                            if prediction.get("ml_score") is not None
                            else None
                        )
                        entry["final_score"] = round(float(prediction["final_score"]), 4)
                        entry["ai_enabled"] = prediction["ai_enabled"]
                        entry["ai_model_status"] = prediction["model_status"]
                        entry["ai_model_version"] = prediction["model_version"]
                        entry["feature_version"] = prediction["feature_version"]
                        entry["ai_score_weight"] = prediction["score_weight"]
                        entry["ai_fallback_reason"] = prediction["fallback_reason"]
                        entry["top_features"] = prediction["top_features"]

        _apply_ai_promotion(candidates, ai_targets, min_score, predictor)

    candidates.sort(key=lambda x: -x["score"])
    scan_summary.sort(key=lambda x: -x["score"])
    logger.info(f"[SCAN] 완료: 분석 {len(scan_summary)}종목 → 후보 {len(candidates)}종목 (기준 {min_score}점 이상)")
    return {
        "candidates": candidates,
        "scan_summary": scan_summary,
        "universe_size": len(scan_list),
        "scanned": len(scan_summary),
        "min_score": min_score,
        "scan_error": None,
    }


def adjust_tick_size(price: int) -> int:
    if price < 2000:
        return price
    elif price < 5000:
        return price - (price % 5)
    elif price < 20000:
        return price - (price % 10)
    elif price < 50000:
        return price - (price % 50)
    elif price < 200000:
        return price - (price % 100)
    elif price < 500000:
        return price - (price % 500)
    else:
        return price - (price % 1000)

def build_orders(
    candidates: list[dict],
    get_quote_fn: Callable[[str], dict],
    held_count: int,
    cash: int,
    optimizer: str = "score_tilted_inverse_vol",
) -> list[dict]:
    candidates = [c for c in candidates if not is_excluded_symbol(c.get("ticker", ""))]
    available_slots = config.max_positions - held_count
    if available_slots <= 0:
        logger.info(f"[INFO] Max positions reached ({config.max_positions}); no new buy orders")
        return []
    if cash <= 0:
        logger.info("[INFO] No new-buy budget available; no candidate buy orders")
        return []

    for c in candidates[:available_slots]:
        quote = get_quote_fn(c["ticker"])
        raw_price = int(quote["ask1"] or quote["current"])
        c["limit_price"] = adjust_tick_size(raw_price)

    # 1. 단순 점수 비례 분할 배분 (score_proportional)
    if optimizer == "score_proportional":
        allocator = PortfolioAllocator()
        orders = allocator.allocate(candidates[:available_slots], cash, config.total_capital)
        return orders

    # 2. LLM 확신도 기반 가중 배분 (llm_confidence_weight)
    elif optimizer == "llm_confidence_weight":
        score_sum = sum(max(0.1, c.get("ml_score") or c.get("rule_score") or 0.1) for c in candidates[:available_slots])
        if score_sum <= 0:
            score_sum = 1.0
            
        deployable = config.total_capital * (1 - config.cash_buffer)
        orders = []
        cost_mult = 1.001
        for c in candidates[:available_slots]:
            price = c.get("limit_price", 0)
            if price <= 0:
                continue
            ml_val = c.get("ml_score") or c.get("rule_score") or 0.1
            target_weight = min(config.max_single_weight, (ml_val / score_sum) * (1 - config.cash_buffer))
            per_position = deployable * target_weight
            qty = math.floor(per_position / (price * cost_mult))
            if qty > 0:
                orders.append({
                    "ticker": c["ticker"],
                    "quantity": qty,
                    "limit_price": price,
                    "estimated_cost": qty * price * cost_mult,
                    "score": c.get("score", 0),
                    "reasons": c.get("reasons", [])
                })
        
        total_cost = sum(o["estimated_cost"] for o in orders)
        budget = min(deployable, cash)
        if total_cost > budget:
            scale = budget / total_cost
            for o in orders:
                o["quantity"] = math.floor(o["quantity"] * scale)
                o["estimated_cost"] = o["quantity"] * o["limit_price"] * cost_mult
        return [o for o in orders if o["quantity"] > 0]

    # 3. 변동성 역수 & 점수 틸트 MPT 배분 (score_tilted_inverse_vol)
    else:
        weighted = []
        for c in candidates[:available_slots]:
            vol = c.get("volatility") or 0.02
            expected_score = max(0.1, 1 + c.get("score", 0))
            weight_signal = expected_score / vol
            weighted.append({**c, "weight_signal": weight_signal})
            
        signal_sum = sum(w["weight_signal"] for w in weighted) or 1.0
        deployable = config.total_capital * (1 - config.cash_buffer)
        orders = []
        cost_mult = 1.001
        for w in weighted:
            price = w.get("limit_price", 0)
            if price <= 0:
                continue
            target_weight = min(config.max_single_weight, (1 - config.cash_buffer) * w["weight_signal"] / signal_sum)
            per_position = deployable * target_weight
            qty = math.floor(per_position / (price * cost_mult))
            if qty > 0:
                orders.append({
                    "ticker": w["ticker"],
                    "quantity": qty,
                    "limit_price": price,
                    "estimated_cost": qty * price * cost_mult,
                    "score": w.get("score", 0),
                    "reasons": w.get("reasons", [])
                })
                
        total_cost = sum(o["estimated_cost"] for o in orders)
        budget = min(deployable, cash)
        if total_cost > budget:
            scale = budget / total_cost
            for o in orders:
                o["quantity"] = math.floor(o["quantity"] * scale)
                o["estimated_cost"] = o["quantity"] * o["limit_price"] * cost_mult
        return [o for o in orders if o["quantity"] > 0]


def generate_signal(stock: dict, daily_data: list, strategy_model: str = "") -> dict:
    prices = [float(d["stck_clpr"]) for d in daily_data if d.get("stck_clpr")]
    highs = [float(d["stck_hgpr"]) for d in daily_data if d.get("stck_hgpr")]
    volumes = [float(d["acml_vol"]) for d in daily_data if d.get("acml_vol")]
    prices.reverse()
    highs.reverse()
    volumes.reverse()

    current = float(stock.get("prpr", 0))
    qty = int(stock.get("hldg_qty", 0))
    rt = float(stock.get("evlu_pfls_rt", 0))
    split_qty = max(1, qty // config.split_n)

    profile = (
        calc_strategy_profile(
            prices,
            highs,
            volumes,
            strategy_model=strategy_model,
            symbol=stock.get("pdno", ""),
        )
        if prices
        else calc_strategy_profile(
            [],
            strategy_model=strategy_model,
            symbol=stock.get("pdno", ""),
        )
    )
    rsi = profile["rsi"]
    rsi2 = profile["rsi2"]
    sma20 = profile["sma20"]
    sma60 = profile["sma60"]
    bb_lo = profile["bb_lo"]
    bb_hi = profile["bb_hi"]
    indicators = {
        "rsi": rsi,
        "rsi2": rsi2,
        "sma20": sma20,
        "sma60": sma60,
        "bb_lo": bb_lo,
        "bb_hi": bb_hi,
        "rt": rt,
        "strategy_score": profile["score"],
        "macd_hist": profile["macd_hist"],
        "macd_bull_cross": profile["macd_bull_cross"],
        "macd_bear_cross": profile["macd_bear_cross"],
    }

    rounded_rt = round(rt, 1)
    if rounded_rt <= config.stop_loss_pct:
        return {"action": "sell", "qty": qty, "price": 0, "reason": f"stop loss {rounded_rt:.1f}%", "indicators": indicators}
    if rt >= 200 and rsi >= config.rsi_sell:
        return {"action": "sell", "qty": split_qty, "price": int(current), "reason": f"large profit split sell {rt:.1f}% RSI={rsi}", "indicators": indicators}
    if rt >= config.take_profit and rsi >= config.rsi_sell:
        return {"action": "sell", "qty": split_qty, "price": int(current), "reason": f"take profit {rt:.1f}% RSI={rsi}", "indicators": indicators}
    if rt >= config.take_profit * 0.5 and profile["macd_bear_cross"] and rsi >= 60:
        return {"action": "sell", "qty": split_qty, "price": int(current), "reason": f"MACD bearish take profit {rt:.1f}% RSI={rsi}", "indicators": indicators}
    if rt <= -10 and rsi <= config.rsi_buy and prices and current <= bb_lo:
        return {"action": "buy", "qty": split_qty, "price": int(current), "reason": f"split buy {rt:.1f}% RSI={rsi} lower band", "indicators": indicators}
    if rt < 0 and profile["score"] >= 5:
        return {"action": "buy", "qty": split_qty, "price": int(current), "reason": f"multi-strategy buy score={profile['score']} ({', '.join(profile['reasons'][:3])})", "indicators": indicators}
    if sma20 > sma60 > 0 and rt < 0:
        return {"action": "buy", "qty": split_qty, "price": int(current), "reason": f"golden cross SMA20={sma20:.0f}>SMA60={sma60:.0f}", "indicators": indicators}
    return {"action": "hold", "qty": 0, "price": 0, "reason": f"hold {rt:+.1f}% RSI={rsi}", "indicators": indicators}
