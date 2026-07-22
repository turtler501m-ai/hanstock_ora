"""RSI \uacfc\ub9e4\ub3c4 \ubc18\ub4f1 \uc804\ub7b5."""

class CustomRSILimitStrategy:
    """
    RSI \uacfc\ub9e4\ub3c4 \ubc18\ub4f1 \uc804\ub7b5
    RSI \uacfc\ub9e4\ub3c4 \uad6c\uac04, MACD \ubc18\ub4f1 \uc804\ud658,
    \ubcfc\ub9b0\uc800\ubc34\ub4dc \ud558\ub2e8 \uc774\ud0c8\uc744 \ud568\uaed8 \ud655\uc778\ud574
    \ub2e8\uae30 \uae30\uc220\uc801 \ubc18\ub4f1 \uac00\ub2a5\uc131\uc774 \ub192\uc740 \uc885\ubaa9\uc5d0
    \uc810\uc218\ub97c \uc8fc\ub294 \ubcf4\uc218\ud615 \ub8f0 \uc804\ub7b5\uc785\ub2c8\ub2e4.
    """
    def __init__(self, rsi_period: int = 14, buy_threshold: float = 30.0):
        self.rsi_period = rsi_period
        self.buy_threshold = buy_threshold

    def calculate_score(self, prices: list[float], indicators: dict) -> float:
        """
        Calculates a trade recommendation score between 0.0 and 5.0.
        """
        score = 0.0
        rsi = indicators.get("rsi", 50.0)
        macd = indicators.get("macd_hist", 0.0)
        current = prices[-1] if prices else 0.0
        bb_lo = indicators.get("bb_lo", current)
        
        # 1. RSI oversold criteria
        if rsi < self.buy_threshold:
            score += 2.0
        elif rsi < 45.0:
            score += 0.5
            
        # 2. MACD histogram positive trend
        if macd > 0:
            score += 1.5
            
        # 3. Bollinger Band bottom boundary touch
        if current < bb_lo:
            score += 1.5
            
        return min(5.0, score)
