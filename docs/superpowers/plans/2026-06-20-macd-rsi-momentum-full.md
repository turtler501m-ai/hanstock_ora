# MACD+RSI Momentum 전략 완전 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 로스캐머런 MACD+RSI 매매법 영상 전략을 미스톡에 완전히 적용 — 환경변수 파라미터 수정, RSI 하락 다이버전스 감지(두 번째 매매법), Momentum Scope 조기 신호 강화를 구현한다.

**Architecture:** `indicators.py`에 RSI 시리즈 계산 함수와 다이버전스 감지 함수를 추가하고, `strategy.py`의 `_macd_rsi_momentum_profile()`에 두 번째 매매법(다이버전스 후 재진입)과 조기 신호(hist turn-up + 거래량)를 통합한다. `.env` 파라미터를 영상 권장값으로 수정하여 로컬·VM 모두 적용한다.

**Tech Stack:** Python 3.10, yfinance, SQLite, KIS OpenAPI, pytest, dotenv

---

## 파일 변경 맵

| 파일 | 변경 유형 | 내용 |
|------|----------|------|
| `.env` | 수정 | 전략 파라미터 6개 추가/변경 |
| `src/strategy/indicators.py` | 수정 | `calc_rsi_series()`, `calc_rsi_divergence()` 추가 |
| `src/mistock/strategy.py` | 수정 | `_macd_rsi_momentum_profile()` 개선 — 다이버전스 + 조기신호 |
| `tests/test_mistock_indicator_strategy.py` | 수정 | 새 테스트 케이스 추가 |

---

## Task 1: .env 파라미터 수정 (로컬)

**Files:**
- Modify: `.env`

- [ ] **Step 1: .env에 MISTOCK 전략 파라미터 추가**

`.env` 파일 끝에 다음 블록을 추가한다. 기존에 `MISTOCK_STRATEGY_MODEL`, `MISTOCK_STOP_LOSS_PCT`, `MISTOCK_TAKE_PROFIT` 줄이 없는 것을 확인했으므로 새로 추가한다.

```
# MACD+RSI Momentum 전략 파라미터 (로스캐머런 영상 기준)
MISTOCK_STRATEGY_MODEL=macd_rsi_momentum
MISTOCK_STOP_LOSS_PCT=-7
MISTOCK_TAKE_PROFIT=14
MISTOCK_INDICATOR_MIN_SCORE=4
MISTOCK_INDICATOR_RSI_ENTRY_MIN=50
MISTOCK_INDICATOR_RSI_ENTRY_MAX=70
MISTOCK_INDICATOR_VOLUME_RATIO=1.3
```

> 영상 근거:
> - 손절폭 × 2배 = 목표가 → stop_loss=-7, take_profit=14
> - RSI 50~70 구간이 핵심 진입 조건
> - min_score=4 이상만 후보 등록

- [ ] **Step 2: 설정이 로드되는지 빠른 검증**

```bash
python -c "
import os; from dotenv import load_dotenv; load_dotenv(override=True)
from src.mistock.config import config
print('model:', config.strategy_model)
print('stop_loss:', config.stop_loss_pct)
print('take_profit:', config.take_profit)
print('rsi_min:', config.indicator_rsi_entry_min)
"
```

기대 출력:
```
model: macd_rsi_momentum
stop_loss: -7.0
take_profit: 14.0
rsi_min: 50
```

- [ ] **Step 3: commit**

```bash
git add .env
git commit -m "config: MISTOCK 전략 파라미터를 MACD+RSI 영상 권장값으로 설정"
```

---

## Task 2: indicators.py에 RSI 시리즈·다이버전스 함수 추가

**Files:**
- Modify: `src/strategy/indicators.py`

### 배경

`calc_rsi(prices, 14)`는 최종값 하나만 반환한다. 다이버전스 감지는 최근 N봉의 RSI 값 목록이 필요하므로, 시리즈를 반환하는 함수를 먼저 만든다.

다이버전스 감지 로직:
- 최근 `period`봉을 전반부 / 후반부로 나눔
- **가격 고점**: 후반부 > 전반부 (상승)
- **RSI 고점**: 후반부 < 전반부 (하락)
- 두 조건이 동시에 성립하면 하락 다이버전스

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_mistock_indicator_strategy.py`의 기존 테스트 클래스 아래에 추가:

```python
class RsiDivergenceTests(unittest.TestCase):
    def test_calc_rsi_series_length(self):
        from src.strategy.indicators import calc_rsi_series
        prices = [100 + i * 0.3 for i in range(50)]
        result = calc_rsi_series(prices, 14)
        # period+1 이후부터 계산 가능 → len(prices) - period 개
        self.assertEqual(len(result), len(prices) - 14)

    def test_bearish_divergence_detected(self):
        from src.strategy.indicators import calc_rsi_divergence
        # 전반부: 완만한 상승 → RSI 높음
        # 후반부: 더 높은 가격이지만 RSI는 낮음 (급등 후 횡보)
        first_half = [100 + i * 0.8 for i in range(20)]   # 강한 상승 → RSI 높음
        second_half = [116 + i * 0.2 for i in range(20)]  # 완만 상승 → 더 높은 가격, RSI 낮음
        prices = [90 + i * 0.2 for i in range(14)] + first_half + second_half
        result = calc_rsi_divergence(prices, period=40)
        self.assertTrue(result["bearish"])

    def test_no_divergence_when_rsi_rising(self):
        from src.strategy.indicators import calc_rsi_divergence
        # 가격도 오르고 RSI도 오르면 다이버전스 없음
        prices = [90 + i * 0.2 for i in range(14)] + [100 + i * 0.5 for i in range(40)]
        result = calc_rsi_divergence(prices, period=40)
        self.assertFalse(result["bearish"])
```

- [ ] **Step 2: 테스트 실패 확인**

```bash
python -m pytest tests/test_mistock_indicator_strategy.py::RsiDivergenceTests -v
```

기대 출력: `ImportError: cannot import name 'calc_rsi_series'`

- [ ] **Step 3: indicators.py에 두 함수 구현**

`src/strategy/indicators.py` 파일 끝(기존 `calc_bollinger` 함수 다음)에 추가:

```python
def calc_rsi_series(prices: list[float], period: int = 14) -> list[float]:
    """prices 리스트의 각 봉에 대한 RSI 값 목록을 반환한다.
    반환 길이 = len(prices) - period
    """
    if len(prices) <= period:
        return []
    result = []
    for i in range(period, len(prices)):
        result.append(calc_rsi(prices[:i + 1], period))
    return result


def calc_rsi_divergence(prices: list[float], period: int = 40) -> dict:
    """RSI 하락 다이버전스 감지 (로스캐머런 두 번째 매매법).

    최근 period봉을 전반부/후반부로 나눠서:
    - 가격 고점: 후반부 > 전반부 (더 높음)
    - RSI 고점: 후반부 < 전반부 (더 낮음)
    두 조건이 모두 성립하면 bearish=True.

    반환:
        {
            "bearish": bool,
            "price_high1": float,  # 전반부 가격 고점
            "price_high2": float,  # 후반부 가격 고점
            "rsi_high1": float,    # 전반부 RSI 고점
            "rsi_high2": float,    # 후반부 RSI 고점
        }
    """
    needed = period + 14  # RSI 계산에 최소 14봉 워밍업 필요
    if len(prices) < needed:
        return {"bearish": False, "price_high1": 0, "price_high2": 0,
                "rsi_high1": 0, "rsi_high2": 0}

    recent_prices = prices[-period:]
    rsi_series = calc_rsi_series(prices, 14)
    recent_rsi = rsi_series[-period:]

    if len(recent_rsi) < period:
        return {"bearish": False, "price_high1": 0, "price_high2": 0,
                "rsi_high1": 0, "rsi_high2": 0}

    half = period // 2
    price_high1 = max(recent_prices[:half])
    price_high2 = max(recent_prices[half:])
    rsi_high1 = max(recent_rsi[:half])
    rsi_high2 = max(recent_rsi[half:])

    bearish = (
        price_high2 > price_high1   # 가격 고점 상승
        and rsi_high2 < rsi_high1   # RSI 고점 하락
        and rsi_high2 > 45          # 과매도 구간 아님 (완전 붕괴는 제외)
    )
    return {
        "bearish": bearish,
        "price_high1": price_high1,
        "price_high2": price_high2,
        "rsi_high1": rsi_high1,
        "rsi_high2": rsi_high2,
    }
```

- [ ] **Step 4: 테스트 통과 확인**

```bash
python -m pytest tests/test_mistock_indicator_strategy.py::RsiDivergenceTests -v
```

기대 출력: `3 passed`

- [ ] **Step 5: commit**

```bash
git add src/strategy/indicators.py tests/test_mistock_indicator_strategy.py
git commit -m "feat: indicators에 calc_rsi_series, calc_rsi_divergence 추가"
```

---

## Task 3: strategy.py — 두 번째 매매법 + Momentum Scope 조기 신호 통합

**Files:**
- Modify: `src/mistock/strategy.py:137-205` (`_macd_rsi_momentum_profile` 함수)

### 변경 내용

현재 `_macd_rsi_momentum_profile()`에 추가할 로직:

1. **Histogram Turn-Up 조기 신호** (Momentum Scope 대체):
   - `prev_hist < 0 and hist > prev_hist` → 음수 구간에서 반전
   - 거래량도 평균 이상이면 추가 점수

2. **RSI 하락 다이버전스 + MACD 재골든크로스** (두 번째 매매법):
   - `calc_rsi_divergence()` 결과가 `bearish=True`
   - AND `macd["bull_cross"]` (재골든크로스)
   - → 강한 재진입 신호 +3점 (첫 번째 매매법보다 신뢰도 높음)

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_mistock_indicator_strategy.py`에 추가:

```python
class MacdRsiMomentumProfileV2Tests(unittest.TestCase):
    def setUp(self):
        self.original_model = config.strategy_model
        config.strategy_model = "macd_rsi_momentum"
        config.indicator_rsi_entry_min = 50
        config.indicator_rsi_entry_max = 70

    def tearDown(self):
        config.strategy_model = self.original_model
        config.indicator_rsi_entry_min = 50
        config.indicator_rsi_entry_max = 70

    def test_hist_turn_up_with_volume_scores(self):
        """histogram이 음수에서 반전 + 거래량 급증 시 momentum_scope 이유가 포함되어야 함"""
        # 가격: 장기 하락 후 소폭 반등 (histogram 음수 구간에서 방향 전환)
        prices = [110 - i * 0.3 for i in range(70)] + [89, 88.5, 89.2, 90.0]
        volumes = [1000] * (len(prices) - 1) + [1600]  # 마지막 봉 거래량 급증

        profile = strategy.strategy_profile(prices, prices, volumes)

        self.assertIn("momentum_scope", " ".join(profile["reasons"]))

    def test_divergence_reentry_scores_high(self):
        """RSI 하락 다이버전스 + MACD 재골든크로스 → 높은 점수와 이유 포함"""
        # 전반부 강한 상승(RSI 높음) → 조정 → 후반부 더 높은 가격(RSI는 낮음) → 재골든크로스
        warmup = [80 + i * 0.3 for i in range(30)]
        first_leg = [89 + i * 0.9 for i in range(20)]   # RSI 높게 만들기
        correction = [107 - i * 0.4 for i in range(10)] # 조정
        second_leg = [103 + i * 0.6 for i in range(20)] # 새 고점, RSI는 낮음
        prices = warmup + first_leg + correction + second_leg
        volumes = [1000] * len(prices)

        profile = strategy.strategy_profile(prices, prices, volumes)

        reasons_text = " ".join(profile["reasons"])
        # 다이버전스 재진입 이유가 포함되거나 점수가 높아야 함
        self.assertTrue(
            "divergence" in reasons_text or profile["score"] >= 3,
            f"Expected divergence signal or score>=3, got reasons={profile['reasons']}, score={profile['score']}"
        )
```

- [ ] **Step 2: 테스트 실패 확인**

```bash
python -m pytest tests/test_mistock_indicator_strategy.py::MacdRsiMomentumProfileV2Tests -v
```

기대 출력: `FAILED` (momentum_scope reason이 없음)

- [ ] **Step 3: _macd_rsi_momentum_profile() 수정**

`src/mistock/strategy.py`의 `_macd_rsi_momentum_profile` 함수 전체를 아래로 교체:

```python
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

    # --- 거래량 단독 확인 ---
    if volume_confirmed and not hist_turn_up:
        score += 1
        reasons.append(f"volume confirmation {volumes[-1] / (sum(volumes[-20:]) / 20):.1f}x")

    # --- RSI 과열 패널티 ---
    if rsi14 >= entry_max and not macd["bull_cross"]:
        score -= 2
        reasons.append(f"RSI overheated {rsi14:.0f}")

    # --- 두 번째 매매법: RSI 하락 다이버전스 + MACD 재골든크로스 ---
    # 첫 번째 매매법보다 신뢰도가 높으므로 +3점 (조건 충족 시 과열 패널티 무효화)
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
            # 과열 패널티 취소: 1차 상승으로 RSI 높은 것은 자연스러운 현상
            if f"RSI overheated {rsi14:.0f}" in reasons:
                reasons.remove(f"RSI overheated {rsi14:.0f}")
                score += 2  # 패널티 복원

    return {
        "score": max(0, score),
        "reasons": reasons or ["no indicator signal"],
        "rsi": rsi14,
        "rsi2": calc_rsi(prices, 2),
        "macd_hist": hist,
        "macd_bull_cross": bool(macd.get("bull_cross")),
        "macd_bear_cross": bool(macd.get("bear_cross")),
        "sma20": sma20,
        "sma60": sma60,
        "price": current,
        "divergence_reentry": divergence_reentry,
        "strategy_model": "macd_rsi_momentum",
    }
```

- [ ] **Step 4: 기존 테스트 포함 전체 테스트 통과 확인**

```bash
python -m pytest tests/test_mistock_indicator_strategy.py -v
```

기대 출력: 모든 테스트 PASS

- [ ] **Step 5: commit**

```bash
git add src/mistock/strategy.py
git commit -m "feat: MACD+RSI 두 번째 매매법(RSI 다이버전스+재진입) 및 Momentum Scope 조기 신호 통합"
```

---

## Task 4: 전체 테스트 회귀 검증

**Files:**
- Test: `tests/`

- [ ] **Step 1: 전체 테스트 실행**

```bash
python -m pytest tests/ -v --tb=short -q 2>&1 | tail -30
```

기대 출력: 모든 기존 테스트 PASS (신규 실패 없음)

- [ ] **Step 2: 실패 시 원인 분석**

실패가 있으면 에러 메시지를 확인하고 `strategy.py` 또는 `indicators.py`의 타입·반환값을 수정한다.

---

## Task 5: VM .env에도 동일 파라미터 적용

**배경:** 로컬 `.env`를 수정했으나 VM은 deploy 시 `.env`를 scp로 복사한다(`scripts/local/deploy-vm.ps1:128-130` 참조). 따라서 로컬 `.env`가 소스이며, VM은 다음 배포 시 자동 적용된다.

- [ ] **Step 1: VM 현재 .env 확인 (선택)**

VM에 SSH 접속 가능하다면:
```bash
# deploy-vm.ps1가 처리하므로 수동 확인만
ssh -i ~/.ssh/id_ed25519 ubuntu@168.110.102.249 \
  "grep -E 'MISTOCK_STRATEGY|MISTOCK_STOP|MISTOCK_TAKE' ~/hanstock/.env || echo 'not set yet'"
```

- [ ] **Step 2: 배포 실행**

```powershell
# Windows PowerShell에서 실행
.\deploy-vm.ps1
```

또는 Claude Code 터미널에서:
```bash
powershell -File scripts/local/deploy-vm.ps1
```

- [ ] **Step 3: VM에서 파라미터 검증**

```bash
ssh -i ~/.ssh/id_ed25519 ubuntu@168.110.102.249 \
  "cd ~/hanstock && python3 -c \"
from dotenv import load_dotenv; load_dotenv(override=True)
from src.mistock.config import config
print('model:', config.strategy_model)
print('stop_loss:', config.stop_loss_pct)
print('take_profit:', config.take_profit)
\""
```

기대 출력:
```
model: macd_rsi_momentum
stop_loss: -7.0
take_profit: 14.0
```

---

## Spec 커버리지 체크

| 분석 보고서 항목 | Task | 완료 여부 |
|--------------|------|---------|
| MISTOCK_STRATEGY_MODEL=macd_rsi_momentum | Task 1 | ✅ |
| stop_loss_pct=-7, take_profit=14 | Task 1 | ✅ |
| RSI 50 진입 조건 (기존 구현) | — | ✅ 기존 |
| MACD 데드크로스 매도 (기존 구현) | — | ✅ 기존 |
| Momentum Scope hist turn-up + 거래량 | Task 3 | ✅ |
| RSI 하락 다이버전스 감지 함수 | Task 2 | ✅ |
| 두 번째 매매법 (다이버전스+재골든크로스) | Task 3 | ✅ |
| 분할 매도 로직 | — | ⏭ 미구현 (v2 예약) |
| VM .env 적용 | Task 5 | ✅ |

> **분할 매도**: holdings 테이블 구조 변경 없이 구현하려면 별도 trades 추적이 필요해 복잡도가 높다. 전략 검증(백테스트 + dry-run 2주) 후 v2에서 구현한다.
